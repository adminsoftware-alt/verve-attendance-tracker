"""Pub/Sub buffer between the Zoom webhook and event processing.

Flow when WEBHOOK_PUBSUB_ENABLED=true:

    Zoom -> /webhook (verify signature) -> publish to Pub/Sub -> 200 to Zoom
                                              |
    Pub/Sub push -> /pubsub/zoom-events (same service) -> handlers -> BigQuery
                    non-2xx reply = Pub/Sub retries; after max attempts the
                    message lands in the dead-letter topic for review.

WHY PUSH TO THE SAME SERVICE (not a separate worker): the handlers update
in-memory state (participant positions, pending mapping requests, dedup
cache) that /mapping/sync depends on, and that state assumes ONE process
(see Dockerfile --workers 1). A separate worker would split that memory.

ROLLBACK: set WEBHOOK_PUBSUB_ENABLED=false (or unset). /webhook goes back to
processing inline exactly as before.
"""
import base64
import json
import os
import threading
import time

__all__ = [
    'PUBSUB_ENABLED',
    'pubsub_active',
    'EventStoreError',
    'strict_store',
    'raise_if_strict',
    'publish_webhook',
    'verify_push_request',
    'decode_push_envelope',
    'message_already_processed',
    'mark_message_processed',
    'note_push_failure',
    'pubsub_stats',
]

PUBSUB_ENABLED = os.environ.get('WEBHOOK_PUBSUB_ENABLED', '').strip().lower() == 'true'
PUBSUB_TOPIC = os.environ.get('WEBHOOK_PUBSUB_TOPIC', 'zoom-webhook-events').strip()
# Regional endpoint keeps ordered delivery reliable (ordering is per region).
# The service runs in us-central1.
PUBSUB_API_ENDPOINT = os.environ.get('PUBSUB_API_ENDPOINT',
                                     'us-central1-pubsub.googleapis.com:443').strip()
# Push auth: Pub/Sub signs each push with an OIDC token for this service
# account and audience. Both must be set or every push is rejected.
PUBSUB_PUSH_AUDIENCE = os.environ.get('PUBSUB_PUSH_AUDIENCE', '').strip()
PUBSUB_PUSH_SA_EMAIL = os.environ.get('PUBSUB_PUSH_SA_EMAIL', '').strip().lower()
# Zoom expects a reply within 3 s. A publish normally takes ~50 ms; if it is
# slower than this we stop waiting and process inline instead.
PUBLISH_TIMEOUT_S = 2


def pubsub_active():
    """Publish only when push auth is configured too. Otherwise every push
    would be rejected and events would pile up in the dead-letter topic."""
    return PUBSUB_ENABLED and bool(PUBSUB_PUSH_AUDIENCE and PUBSUB_PUSH_SA_EMAIL)


if PUBSUB_ENABLED and not pubsub_active():
    print("[PubSub] WARNING: WEBHOOK_PUBSUB_ENABLED=true but PUBSUB_PUSH_AUDIENCE / "
          "PUBSUB_PUSH_SA_EMAIL are not set; webhooks stay on the inline path")

_stats_lock = threading.Lock()
_stats = {
    'published': 0,
    'publish_failed_fallback_inline': 0,
    'push_processed': 0,
    'push_duplicate_skipped': 0,
    'push_failed_will_retry': 0,
    'push_rejected_auth': 0,
    'last_published_at': None,
    'last_processed_at': None,
}


def _bump(key, ts_key=None):
    with _stats_lock:
        _stats[key] += 1
        if ts_key:
            _stats[ts_key] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def pubsub_stats():
    with _stats_lock:
        out = dict(_stats)
    out.update({
        'enabled': PUBSUB_ENABLED,
        'active': pubsub_active(),
        'topic': PUBSUB_TOPIC,
        'push_auth_configured': bool(PUBSUB_PUSH_AUDIENCE and PUBSUB_PUSH_SA_EMAIL),
    })
    return out


# ------------------------------------------------------------------------------
# Strict store mode: inside a Pub/Sub push, a failed BigQuery insert must RAISE
# so the push returns non-2xx and Pub/Sub redelivers. On the inline /webhook
# path it stays off, so behaviour there is unchanged.
# ------------------------------------------------------------------------------
class EventStoreError(Exception):
    """A BigQuery write failed while processing a Pub/Sub message."""


_tls = threading.local()


class strict_store:
    def __enter__(self):
        _tls.strict = True
        return self

    def __exit__(self, *exc):
        _tls.strict = False
        return False


def raise_if_strict(detail):
    if getattr(_tls, 'strict', False):
        raise EventStoreError(str(detail))


# ------------------------------------------------------------------------------
# Publisher
# ------------------------------------------------------------------------------
_publisher = None
_topic_path = None
_publisher_lock = threading.Lock()


def _get_publisher():
    global _publisher, _topic_path
    if _publisher is None:
        with _publisher_lock:
            if _publisher is None:
                from google.cloud import pubsub_v1
                project = os.environ.get('GCP_PROJECT_ID', '').strip()
                if not project:
                    raise RuntimeError('GCP_PROJECT_ID not set')
                client_options = {'api_endpoint': PUBSUB_API_ENDPOINT} if PUBSUB_API_ENDPOINT else None
                client = pubsub_v1.PublisherClient(
                    # Tiny batch latency: a webhook waits on this publish.
                    batch_settings=pubsub_v1.types.BatchSettings(max_latency=0.01),
                    publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True),
                    client_options=client_options,
                )
                # Set _topic_path BEFORE _publisher: other threads skip the
                # lock once _publisher is set and must never see a None path.
                _topic_path = client.topic_path(project, PUBSUB_TOPIC)
                _publisher = client
    return _publisher, _topic_path


def _ordering_key(data):
    """One key per PERSON so each person's events are processed in the order
    Zoom sent them (a leave never overtakes its join), while different people
    are processed in parallel. Email first, else the name normalised the same
    way _bo_state_keys does (Zoom's user_id changes between rooms)."""
    obj = (data.get('payload') or {}).get('object') or {}
    part = obj.get('participant') or {}
    email = (part.get('email') or part.get('user_email') or '').strip().lower()
    if email:
        return ('e:' + email)[:500]
    import re
    name = (part.get('user_name') or part.get('name') or '').strip().lower()
    name = re.sub(r'-\d+$', '', name).strip()
    if name:
        return ('n:' + name)[:500]
    return ('m:' + str(obj.get('uuid') or obj.get('id') or 'none'))[:500]


def publish_webhook(data, raw_body):
    """Publish one verified Zoom webhook. Returns the Pub/Sub message id.
    Raises on failure; the caller then processes inline so nothing is lost."""
    try:
        publisher, topic_path = _get_publisher()
    except Exception:
        _bump('publish_failed_fallback_inline')
        raise
    key = _ordering_key(data)
    envelope = json.dumps({
        'zoom_body': raw_body,           # exact bytes Zoom signed, as text
        'received_at': time.time(),
        'schema_version': 1,
    }).encode('utf-8')
    try:
        future = publisher.publish(
            topic_path, envelope, ordering_key=key,
            event=str(data.get('event', ''))[:100],
        )
        message_id = future.result(timeout=PUBLISH_TIMEOUT_S)
    except Exception:
        # A failed publish pauses its ordering key until resumed.
        try:
            publisher.resume_publish(topic_path, key)
        except Exception:
            pass
        _bump('publish_failed_fallback_inline')
        raise
    _bump('published', 'last_published_at')
    return message_id


# ------------------------------------------------------------------------------
# Push endpoint helpers
# ------------------------------------------------------------------------------
# verify_oauth2_token downloads Google's signing certs on every call. Pub/Sub
# reuses the same token for about an hour, so remember tokens that already
# passed (keyed by the exact token string, honouring its own expiry).
_verified_tokens = {}
_token_lock = threading.Lock()
_auth_transport = None


def _get_auth_transport():
    global _auth_transport
    if _auth_transport is None:
        import requests
        from google.auth.transport import requests as ga_requests
        _auth_transport = ga_requests.Request(session=requests.Session())
    return _auth_transport


def verify_push_request(request_obj):
    """Check the OIDC token Pub/Sub attaches to each push. Fails closed:
    if the audience / service account are not configured, reject."""
    if not (PUBSUB_PUSH_AUDIENCE and PUBSUB_PUSH_SA_EMAIL):
        _bump('push_rejected_auth')
        return False, 'push auth not configured'
    auth = request_obj.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        _bump('push_rejected_auth')
        return False, 'missing bearer token'
    token = auth[len('Bearer '):]
    now = time.time()
    with _token_lock:
        cached = _verified_tokens.get(token)
    if cached and cached.get('exp', 0) > now + 30:
        claims = cached
    else:
        try:
            from google.oauth2 import id_token
            claims = id_token.verify_oauth2_token(
                token, _get_auth_transport(), audience=PUBSUB_PUSH_AUDIENCE)
        except Exception as e:
            _bump('push_rejected_auth')
            return False, f'invalid token: {e}'
        with _token_lock:
            # Pub/Sub reuses a token for ~1 h; drop expired ones as we go.
            for t in [t for t, c in _verified_tokens.items() if c.get('exp', 0) <= now]:
                del _verified_tokens[t]
            _verified_tokens[token] = claims
    if (claims.get('email') or '').lower() != PUBSUB_PUSH_SA_EMAIL or not claims.get('email_verified'):
        _bump('push_rejected_auth')
        return False, 'unexpected service account'
    return True, None


def decode_push_envelope(envelope):
    """Return (message_id, zoom_data dict, delivery_attempt). Raises ValueError
    on a malformed message (it will retry, then go to the dead-letter topic)."""
    message = (envelope or {}).get('message') or {}
    message_id = message.get('messageId') or message.get('message_id')
    raw = message.get('data')
    if not message_id or not raw:
        raise ValueError('envelope missing messageId or data')
    inner = json.loads(base64.b64decode(raw).decode('utf-8'))
    zoom_data = json.loads(inner['zoom_body'])
    attempt = envelope.get('deliveryAttempt')
    return message_id, zoom_data, attempt


# Pub/Sub is at-least-once: a message whose ack was lost comes back with the
# SAME messageId. Remember processed ids for an hour and skip repeats.
_PROCESSED_TTL_S = 3600
_processed = {}
_processed_lock = threading.Lock()
_last_prune = [0.0]


def message_already_processed(message_id):
    with _processed_lock:
        if message_id in _processed:
            _bump('push_duplicate_skipped')
            return True
        return False


def mark_message_processed(message_id):
    now = time.time()
    with _processed_lock:
        _processed[message_id] = now
        if now - _last_prune[0] > 300:
            for k in [k for k, t in _processed.items() if now - t > _PROCESSED_TTL_S]:
                del _processed[k]
            _last_prune[0] = now
    _bump('push_processed', 'last_processed_at')


def note_push_failure():
    _bump('push_failed_will_retry')

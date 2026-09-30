"""Shared test setup: env vars before app import, per-test state reset."""
import os
import sys

os.environ.setdefault('ZOOM_WEBHOOK_SECRET', 'test-secret')
os.environ.setdefault('GCP_PROJECT_ID', 'test-project')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state():
    """Every test starts with clean in-process state: the 60s webhook dedup
    cache, observability counters and mapping-store memoization all survive
    between tests otherwise (one process = one 'server')."""
    import app as app_module
    import zt_mapping
    import zt_observability
    import zt_pubsub
    app_module.meeting_state.event_dedup_cache.clear()
    zt_pubsub._processed.clear()
    zt_pubsub._verified_tokens.clear()
    zt_mapping.reset_for_tests()
    zt_observability.reset_for_tests()
    yield

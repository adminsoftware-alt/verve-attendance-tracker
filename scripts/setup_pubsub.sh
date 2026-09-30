#!/usr/bin/env bash
# One-time setup of the Pub/Sub buffer for Zoom webhooks.
# Safe to re-run: "already exists" errors are ignored.
# Does NOT switch the feature on — see docs/PUBSUB-ROLLOUT.md step 3.
set -uo pipefail

PROJECT_ID="verve-attendance-tracker"
PROJECT_NUMBER="1073587167150"
REGION="us-central1"
SERVICE="breakout-room-calibrator"
SERVICE_URL="https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app"

TOPIC="zoom-webhook-events"
DLQ_TOPIC="zoom-webhook-events-dlq"
SUBSCRIPTION="zoom-webhook-events-push"
DLQ_SUBSCRIPTION="zoom-webhook-events-dlq-review"
PUSH_SA_NAME="pubsub-push-invoker"
PUSH_SA="${PUSH_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
PUSH_ENDPOINT="${SERVICE_URL}/pubsub/zoom-events"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

G="gcloud --project=${PROJECT_ID}"

echo "== 1. Enable the Pub/Sub API"
$G services enable pubsub.googleapis.com

echo "== 2. Topics (main + dead-letter)"
$G pubsub topics create "$TOPIC" || true
$G pubsub topics create "$DLQ_TOPIC" || true

echo "== 3. Service account that signs push requests"
$G iam service-accounts create "$PUSH_SA_NAME" \
  --display-name="Pub/Sub push to webhook processor" || true
# Pub/Sub must be able to mint OIDC tokens for that account.
$G iam service-accounts add-iam-policy-binding "$PUSH_SA" \
  --member="serviceAccount:${PUBSUB_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator"

echo "== 4. Let the Cloud Run service publish to the topic"
RUNTIME_SA=$($G run services describe "$SERVICE" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')
if [ -z "$RUNTIME_SA" ]; then
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
echo "   runtime service account: $RUNTIME_SA"
$G pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/pubsub.publisher"

echo "== 5. Push subscription (ordered per person, retries, dead-letter)"
$G pubsub subscriptions create "$SUBSCRIPTION" \
  --topic="$TOPIC" \
  --push-endpoint="$PUSH_ENDPOINT" \
  --push-auth-service-account="$PUSH_SA" \
  --push-auth-token-audience="$PUSH_ENDPOINT" \
  --enable-message-ordering \
  --ack-deadline=60 \
  --min-retry-delay=10s \
  --max-retry-delay=600s \
  --dead-letter-topic="$DLQ_TOPIC" \
  --max-delivery-attempts=10 \
  --message-retention-duration=7d || true

echo "== 6. Dead-letter permissions + a subscription to review failed messages"
$G pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
  --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.publisher"
$G pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
  --member="serviceAccount:${PUBSUB_AGENT}" --role="roles/pubsub.subscriber"
$G pubsub subscriptions create "$DLQ_SUBSCRIPTION" \
  --topic="$DLQ_TOPIC" --message-retention-duration=7d || true

echo
echo "Done. Feature is still OFF. To switch it on:"
echo "  gcloud run services update $SERVICE --region=$REGION --project=$PROJECT_ID \\"
echo "    --update-env-vars=WEBHOOK_PUBSUB_ENABLED=true,PUBSUB_PUSH_AUDIENCE=${PUSH_ENDPOINT},PUBSUB_PUSH_SA_EMAIL=${PUSH_SA}"

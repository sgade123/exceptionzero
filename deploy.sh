#!/usr/bin/env bash
#
# Deploy the fleet to Cloud Run and wire the Pub/Sub ingress.
#
#   ./deploy.sh exceptionzero-10540
#
set -euo pipefail
PROJECT="${1:?usage: ./deploy.sh PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="exceptionzero"
TOPIC="exceptions"
RUNTIME_SA="ez-coord@${PROJECT}.iam.gserviceaccount.com"
ARMOR_TEMPLATE="ez-armor"

gcloud config set project "$PROJECT" >/dev/null

echo "== runtime permissions =="
for role in roles/modelarmor.user roles/bigquery.dataViewer roles/cloudtrace.agent roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role" \
    --condition=None >/dev/null 2>&1 && echo "  + $role" || echo "  (already) $role"
done

echo
echo "== build + deploy =="
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --memory 1Gi --cpu 2 --timeout 900 --concurrency 20 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=global,EZ_MODEL=gemini-3.5-flash,EZ_MODEL_REASONING=gemini-3.5-flash,STUB=0,EZ_ESTATE=bigquery,EZ_ARMOR_TEMPLATE=projects/${PROJECT}/locations/us-central1/templates/ez-armor" \
  --quiet

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "  deployed: $URL"

echo
echo "== pub/sub ingress =="
gcloud pubsub topics create "$TOPIC" 2>/dev/null && echo "  topic created" || echo "  topic exists"
gcloud pubsub subscriptions create "${TOPIC}-push" \
  --topic "$TOPIC" --push-endpoint "${URL}/pubsub" --ack-deadline 600 \
  2>/dev/null && echo "  subscription created" || echo "  subscription exists"

echo
echo "------------------------------------------------------------"
echo "  Demo screen:  $URL"
echo "  Health:       curl $URL/healthz"
echo "  Async path:   gcloud pubsub topics publish $TOPIC \\"
echo "                  --message '{\"exception_id\":\"EXC-799001\"}'"
echo "  Live logs:    gcloud beta run services logs tail $SERVICE --region $REGION"
echo "------------------------------------------------------------"

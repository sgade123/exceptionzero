#!/usr/bin/env bash
#
# ExceptionZero — per-agent identity.
#
# Creates one service account per agent with the narrowest role set that lets
# it do its job and nothing else. This is what turns "the Diagnosis agent has
# no tools" from a convention in Python into a control enforced by Google Cloud.
#
# The demo beat this buys you: show sa-diagnosis@ has zero BigQuery roles.
# It cannot read the data estate even if the model decided to try.
#
#   ./setup_iam.sh exceptionzero-10540
#
set -euo pipefail

PROJECT="${1:?usage: ./setup_iam.sh PROJECT_ID}"
DATASET="exceptionzero"
gcloud config set project "$PROJECT" >/dev/null

sa_email() { echo "$1@${PROJECT}.iam.gserviceaccount.com"; }

create_sa() {
  local id="$1" desc="$2"
  if gcloud iam service-accounts describe "$(sa_email "$id")" >/dev/null 2>&1; then
    echo "  exists   $id"
  else
    gcloud iam service-accounts create "$id" --display-name="$desc" >/dev/null
    echo "  created  $id"
  fi
}

grant() {
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$(sa_email "$1")" --role="$2" \
    --condition=None >/dev/null 2>&1
  echo "    + $2"
}

echo "== service accounts =="
create_sa ez-triage      "ExceptionZero Triage"
create_sa ez-coord       "ExceptionZero Context Coordinator"
create_sa ez-inv         "ExceptionZero Invoice Specialist"
create_sa ez-customer          "ExceptionZero Counterparty Specialist"
create_sa ez-hist        "ExceptionZero History Specialist"
create_sa ez-prec        "ExceptionZero Precedent Specialist"
create_sa ez-diagnosis   "ExceptionZero Diagnosis"
create_sa ez-exec        "ExceptionZero Execution"
create_sa ez-verify      "ExceptionZero Verification"

# --------------------------------------------------------------------------
# Every reasoning agent needs Vertex. Nothing else is shared.
# --------------------------------------------------------------------------
echo
echo "== model access (all reasoning agents) =="
for sa in ez-triage ez-coord ez-inv ez-customer ez-hist ez-prec ez-diagnosis; do
  echo "  $sa"
  grant "$sa" roles/aiplatform.user
done

# --------------------------------------------------------------------------
# Read paths. jobUser lets a SA run a query; dataViewer is granted per-table
# below so a specialist can read its own table and no others.
# --------------------------------------------------------------------------
echo
echo "== read scopes =="
for sa in ez-coord ez-inv ez-customer ez-hist ez-prec ez-exec ez-verify; do
  grant "$sa" roles/bigquery.jobUser
done

table_read() {  # table_read <sa> <table>
  bq add-iam-policy-binding \
    --member="serviceAccount:$(sa_email "$1")" \
    --role="roles/bigquery.dataViewer" \
    "${PROJECT}:${DATASET}.$2" >/dev/null 2>&1 \
    && echo "    $1 -> read ${DATASET}.$2" \
    || echo "    $1 -> read ${DATASET}.$2  (skipped; load the dataset first)"
}

echo
echo "== per-table grants — each specialist sees exactly one table =="
table_read ez-triage exceptions
table_read ez-inv    invoices
table_read ez-customer     customers
table_read ez-hist   payment_history
table_read ez-prec   prior_resolutions

echo
echo "== write scope — exactly one agent =="
bq add-iam-policy-binding \
  --member="serviceAccount:$(sa_email ez-exec)" \
  --role="roles/bigquery.dataEditor" \
  "${PROJECT}:${DATASET}.exceptions" >/dev/null 2>&1 \
  && echo "    ez-exec -> WRITE ${DATASET}.exceptions" \
  || echo "    ez-exec -> WRITE (skipped; load the dataset first)"

table_read ez-verify exceptions

# --------------------------------------------------------------------------
# Firestore: memory bank + registry. Coordinator reads, execution writes.
# --------------------------------------------------------------------------
echo
echo "== firestore =="
grant ez-coord roles/datastore.viewer
grant ez-exec  roles/datastore.user
grant ez-verify roles/datastore.user

# --------------------------------------------------------------------------
# Tracing.
# --------------------------------------------------------------------------
echo
echo "== tracing =="
for sa in ez-triage ez-coord ez-inv ez-customer ez-hist ez-prec ez-diagnosis ez-exec ez-verify; do
  grant "$sa" roles/cloudtrace.agent
done

# ==========================================================================
# ez-diagnosis is granted aiplatform.user and cloudtrace.agent. Nothing else.
# No bigquery.jobUser, so it cannot even start a query. No dataViewer, so it
# could not read a result if it had one. No datastore access.
#
# It reasons over evidence handed to it, and the platform enforces that.
# ==========================================================================

cat <<BANNER

------------------------------------------------------------------
Verification — run this on camera:

  gcloud projects get-iam-policy $PROJECT \\
    --flatten="bindings[].members" \\
    --filter="bindings.members:ez-diagnosis" \\
    --format="table(bindings.role)"

Expected: aiplatform.user and cloudtrace.agent. No BigQuery. No Firestore.
That single command is the proof behind the strongest claim in the demo.
------------------------------------------------------------------
BANNER

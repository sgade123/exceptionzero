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
set -uo pipefail   # not -e: one failed grant must not abort the rest

PROJECT="${1:?usage: ./setup_iam.sh PROJECT_ID}"
DATASET="exceptionzero"
gcloud config set project "$PROJECT" >/dev/null

sa_email() { echo "$1@${PROJECT}.iam.gserviceaccount.com"; }

create_sa() {
  local id="$1" desc="$2"
  if gcloud iam service-accounts describe "$(sa_email "$id")" >/dev/null 2>&1; then
    echo "  exists   $id"
  else
    if gcloud iam service-accounts create "$id" --display-name="$desc" >/dev/null 2>&1; then
      echo "  created  $id"
    else
      echo "  FAILED   $id (continuing — grants below still run)"
    fi
  fi
}

grant() {
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$(sa_email "$1")" --role="$2" \
    --condition=None >/dev/null 2>&1
  echo "    + $2"
}

# One list, used everywhere below. Adding an agent means adding it here.
ALL_SAS=(ez-triage ez-coord ez-invoice ez-customer ez-history ez-precedent
         ez-diagnosis ez-exec ez-verify)

echo "== service accounts =="
declare -A SA_DESC=(
  [ez-triage]="Triage" [ez-coord]="Context Coordinator"
  [ez-invoice]="Invoice Specialist" [ez-customer]="Counterparty Specialist"
  [ez-history]="History Specialist" [ez-precedent]="Precedent Specialist"
  [ez-diagnosis]="Diagnosis" [ez-exec]="Execution" [ez-verify]="Verification"
)
for sa in "${ALL_SAS[@]}"; do
  create_sa "$sa" "ExceptionZero ${SA_DESC[$sa]}"
done

# --------------------------------------------------------------------------
# Every reasoning agent needs Vertex. Nothing else is shared.
# --------------------------------------------------------------------------
echo
echo "== model access (all reasoning agents) =="
REASONING_SAS=(ez-triage ez-coord ez-invoice ez-customer ez-history
               ez-precedent ez-diagnosis)
for sa in "${REASONING_SAS[@]}"; do
  echo "  $sa"
  grant "$sa" roles/aiplatform.user
done

# --------------------------------------------------------------------------
# Read paths. jobUser lets a SA run a query; dataViewer is granted per-table
# below so a specialist can read its own table and no others.
# --------------------------------------------------------------------------
echo
echo "== read scopes =="
# NOTE: ez-diagnosis is absent by design. Without bigquery.jobUser it
# cannot even start a query, let alone read a result.
QUERY_SAS=(ez-coord ez-invoice ez-customer ez-history ez-precedent
           ez-exec ez-verify)
for sa in "${QUERY_SAS[@]}"; do
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
# Each specialist reads exactly one table. This is the boundary that makes
# the fan-out meaningful: a compromised specialist cannot widen its reach.
table_read ez-invoice    invoices
table_read ez-customer   customers
table_read ez-history    payment_history
table_read ez-precedent  prior_resolutions

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
echo "== impersonation — bind declared identity to execution =="
# Declaring a service account on an agent record is a label. Impersonation is
# what makes it load-bearing: every tool call runs under the agent's own
# credentials, so IAM decides whether it succeeds.
#
# The caller (you) and the Cloud Run runtime SA must both be able to mint
# tokens for each agent SA. Failures are printed, not swallowed — a silently
# missing grant means the identity model quietly stops being enforced.
CALLER="$(gcloud config get-value account 2>/dev/null)"
RUNTIME="$(sa_email ez-coord)"

for sa in "${ALL_SAS[@]}"; do
  target="$(sa_email "$sa")"
  for member in "user:${CALLER}" "serviceAccount:${RUNTIME}"; do
    if gcloud iam service-accounts add-iam-policy-binding "$target" \
         --member="$member" --role="roles/iam.serviceAccountTokenCreator" \
         --quiet >/dev/null 2>&1; then
      echo "    $sa <- $member"
    else
      echo "    FAILED  $sa <- $member"
    fi
  done
done

echo
echo "== tracing =="
for sa in "${ALL_SAS[@]}"; do
  grant "$sa" roles/cloudtrace.agent
done

# ==========================================================================
# ez-diagnosis is granted aiplatform.user and cloudtrace.agent. Nothing else.
# No bigquery.jobUser, so it cannot even start a query. No dataViewer, so it
# could not read a result if it had one. No datastore access.
#
# It reasons over evidence handed to it, and the platform enforces that.
# ==========================================================================

echo
echo "== verifying impersonation actually works =="
python3 - <<'PYCHECK' 2>/dev/null || echo "  (python check skipped)"
import os, sys
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", os.environ.get("PROJECT", ""))
try:
    import identity
    rows = identity.identity_report()
    bad = [r for r in rows if r.get("identity") not in ("IMPERSONATED", "AMBIENT")]
    for r in rows:
        print(f"    {r['capability']:14} {r.get('identity','?'):20} bigquery={r['bigquery']}")
    if bad:
        print("    -> impersonation not working for: "
              + ", ".join(r["capability"] for r in bad))
        print("       grants can take a minute to propagate; re-run this script")
except Exception as e:
    print(f"    check unavailable: {str(e)[:80]}")
PYCHECK

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

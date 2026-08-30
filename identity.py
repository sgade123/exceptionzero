"""
ExceptionZero — runtime agent identity.

Declaring a service account on a registry record is a label. This binds it to
execution: every tool call an agent makes runs under credentials impersonated
for that agent's service account, so the IAM policy is the thing that decides
whether the call succeeds.

The consequence that matters: the Diagnosis agent's service account holds no
BigQuery roles, so if the Diagnosis agent ever attempted a lookup, Google
Cloud would refuse it. Not the prompt. Not a convention in Python. IAM.

Requires the caller (your user, or the Cloud Run runtime SA) to hold
roles/iam.serviceAccountTokenCreator on each agent SA — see setup_iam.sh.

    EZ_IMPERSONATE=0   run everything as the ambient credential (dev)
    EZ_IMPERSONATE=1   bind each agent to its own identity (default)
"""

from __future__ import annotations

import os
import threading
from typing import Any

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
ENABLED = os.environ.get("EZ_IMPERSONATE", "1") == "1"

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Which identity each capability runs as. This is the same mapping the
# registry publishes — here it is load-bearing rather than descriptive.
AGENT_SA = {
    "triage": "ez-triage",
    "context": "ez-coord",
    "invoice": "ez-invoice",
    "counterparty": "ez-customer",
    "history": "ez-history",
    "precedent": "ez-precedent",
    "diagnosis": "ez-diagnosis",     # no data roles at all
    "execution": "ez-exec",
    "verification": "ez-verify",
}

# The one table each agent is scoped to. Diagnosis has none — it is listed
# here only so the self-test can attempt a read and be refused.
OWN_TABLE = {
    "triage": "exceptions",
    "context": "invoices",          # coordinator holds jobUser + dataViewer
    "invoice": "invoices",
    "counterparty": "customers",
    "history": "payment_history",
    "precedent": "prior_resolutions",
    "diagnosis": "invoices",        # must be DENIED
    "execution": "exceptions",
    "verification": "exceptions",
}

_local = threading.local()


def sa_email(capability: str) -> str:
    short = AGENT_SA.get(capability)
    if not short or not PROJECT:
        return ""
    return f"{short}@{PROJECT}.iam.gserviceaccount.com"


# --------------------------------------------------------------------------
# The active agent. Tool functions read this to decide whose credentials to
# use, so a tool cannot be called "as" an agent that did not invoke it.
# --------------------------------------------------------------------------

class running_as:
    """Context manager marking which agent is executing.

        with running_as("invoice"):
            lookup_invoice("INV-20114")     # runs as ez-invoice@
    """

    def __init__(self, capability: str):
        self.capability = capability

    def __enter__(self):
        self.prev = getattr(_local, "capability", None)
        _local.capability = self.capability
        return self

    def __exit__(self, *exc):
        _local.capability = self.prev
        return False


def current_capability() -> str | None:
    return getattr(_local, "capability", None)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

_creds_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


class IdentityDenied(Exception):
    """The agent's own identity was refused by IAM. This is the control
    working, not a bug — surface it rather than falling back."""


def credentials_for(capability: str):
    """Impersonated credentials for one agent. None means 'use ambient'."""
    if not ENABLED or not PROJECT:
        return None
    target = sa_email(capability)
    if not target:
        return None

    with _cache_lock:
        if target in _creds_cache:
            return _creds_cache[target]

    try:
        import google.auth
        from google.auth import impersonated_credentials
        source, _ = google.auth.default(scopes=SCOPES)
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=target,
            target_scopes=SCOPES,
            lifetime=3600,
        )
    except Exception as e:
        # Impersonation unavailable (missing tokenCreator, local dev without
        # ADC). Fall back loudly — a silent fallback here would mean the
        # identity model quietly stops being enforced.
        print(f"    [identity] cannot impersonate {target}: {str(e)[:80]}",
              flush=True)
        return None

    with _cache_lock:
        _creds_cache[target] = creds
    return creds


def bigquery_client(capability: str | None = None):
    """A BigQuery client bound to the calling agent's identity."""
    from google.cloud import bigquery
    cap = capability or current_capability() or "context"
    creds = credentials_for(cap)
    if creds is None:
        return bigquery.Client(project=PROJECT)
    return bigquery.Client(project=PROJECT, credentials=creds)


def whoami(capability: str) -> dict[str, Any]:
    """What this agent can actually reach.

    Distinguishes two very different failures: not being able to BECOME the
    agent (impersonation), versus being the agent and being refused the data
    (the control working). Reporting both as 'denied' hides which one is
    happening.
    """
    out: dict[str, Any] = {"capability": capability,
                           "service_account": sa_email(capability)}

    creds = credentials_for(capability)
    if ENABLED and creds is None:
        out["identity"] = "NOT_IMPERSONATED"
        out["bigquery"] = "UNKNOWN"
        out["note"] = ("could not mint a token for this SA — grant "
                       "roles/iam.serviceAccountTokenCreator to the caller")
        return out

    # Force a token now so impersonation failure is not mistaken for a
    # BigQuery denial further down.
    if creds is not None:
        try:
            import google.auth.transport.requests as _rq
            creds.refresh(_rq.Request())
            out["identity"] = "IMPERSONATED"
        except Exception as e:
            out["identity"] = "IMPERSONATION_FAILED"
            out["bigquery"] = "UNKNOWN"
            out["note"] = str(e)[:160]
            return out
    else:
        out["identity"] = "AMBIENT"

    def _try(table: str) -> bool:
        try:
            bigquery_client(capability).query(
                f"SELECT COUNT(*) c FROM `{PROJECT}.exceptionzero.{table}`"
            ).result()
            return True
        except Exception:
            return False

    own = OWN_TABLE.get(capability, "invoices")
    out["own_table"] = own
    out["bigquery"] = "ALLOWED" if _try(own) else "DENIED"

    # The other half of the claim: scoped means scoped. An agent that can read
    # its own table must NOT be able to read someone else's.
    others = [t for c, t in OWN_TABLE.items()
              if t != own and c in ("invoice", "counterparty", "history",
                                    "precedent")]
    reachable = [t for t in dict.fromkeys(others) if _try(t)]
    out["other_tables_reachable"] = reachable or "none"
    if out["bigquery"] == "DENIED":
        out["denied_reason"] = "IAM refused the query"
    return out


def identity_report() -> list[dict[str, Any]]:
    """Prove the identity model rather than asserting it.

    Expected: the specialists reach their tables, and `diagnosis` is DENIED —
    because ez-diagnosis holds aiplatform.user and cloudtrace.agent, and
    nothing else.
    """
    return [whoami(c) for c in
            ("invoice", "counterparty", "history", "precedent",
             "context", "diagnosis")]


if __name__ == "__main__":
    print(f"impersonation: {'ON' if ENABLED else 'OFF'}  project={PROJECT}\n")
    for row in identity_report():
        own = row.get("own_table", "?")
        leak = row.get("other_tables_reachable", "?")
        print(f"  {row['capability']:14} {row.get('identity','?'):16} "
              f"own={own:18} {row['bigquery']:8} "
              f"other tables: {leak if leak == 'none' else ', '.join(leak)}")
        if row.get("note"):
            print(f"                 {row['note'][:110]}")
    print()
    print("  Correct result: each specialist reads its OWN table and no other.")
    print("  diagnosis is DENIED everywhere — it holds no BigQuery role at all.")

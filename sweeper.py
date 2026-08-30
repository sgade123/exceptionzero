"""
ExceptionZero — deferred cases and the sweeper.

The Fortified Enterprise Fleet track asks how agents "safely maintain context
across weeks of asynchronous operations." This is that.

An escalated case is not finished. It is *deferred*: parked with the reason it
could not be resolved, the evidence gathered so far, and a description of what
would change the answer. A scheduled sweeper re-examines deferred cases when
the estate changes, and re-opens the ones whose blocker has cleared.

This is true to the domain rather than contrived. A shortfall nobody could
explain in March becomes explicable in April when the customer's next payment
carries the missing remittance detail. Today a human finds that by accident,
weeks later, if at all.

Storage is Firestore in Cloud Run; a local JSON file when running offline, so
the behaviour is demonstrable without cloud credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

COLLECTION = "deferred_cases"
LOCAL_STORE = "data/deferred.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ==========================================================================
# What is parked
# ==========================================================================

@dataclass
class DeferredCase:
    exception_id: str
    exception_type: str
    amount: float
    currency: str
    counterparty_id: str
    reason: str                        # why the gate would not authorise
    blocker: str                       # the machine-readable class of blocker
    evidence_ids: list[str] = field(default_factory=list)
    deferred_at: str = ""
    sweeps: int = 0                    # how many times re-examined
    reopened_at: str | None = None
    resolved_at: str | None = None

    def age_days(self) -> float:
        if not self.deferred_at:
            return 0.0
        return (_now() - datetime.fromisoformat(self.deferred_at)).total_seconds() / 86400


# The blocker classifies *what would have to change*. A case parked on
# missing evidence can be re-opened by new data; one parked on policy cannot.
BLOCKERS = {
    "value_ceiling": "amount above the auto-resolve ceiling — needs human authorisation",
    "low_confidence": "evidence insufficient — new evidence could resolve this",
    "missing_evidence": "referenced record absent — may arrive later",
    "thin_counterparty": "counterparty too new — history accrues over time",
    "policy": "exception type is never auto-resolved",
    "screening": "compliance flag — human review required",
}

# Only these can ever be re-opened by new data. The others need a human, and
# sweeping them repeatedly would be noise.
SWEEPABLE = {"low_confidence", "missing_evidence", "thin_counterparty"}


def classify_blocker(reason: str) -> str:
    """Why the gate would not authorise, in a form the sweeper can act on.

    Order matters. "diagnosis proposed escalation: ... cannot be explained
    with the available evidence" is an evidence problem, not a policy one —
    matching the generic policy phrase first would tell the reviewer the wrong
    thing and stop the sweeper ever reconsidering the case.
    """
    r = reason.lower()

    # Most specific first.
    if "citation failure" in r or "does not exist" in r or "absent" in r \
            or "no matching invoice" in r or "cannot be explained" in r \
            or "available evidence" in r or "no other open" in r:
        return "missing_evidence"
    if "exceeds ceiling" in r:
        return "value_ceiling"
    if "confidence" in r and "below floor" in r:
        return "low_confidence"
    if "screening" in r:
        return "screening"
    if "history" in r or "counterparty too new" in r or "insufficient" in r:
        return "thin_counterparty"
    if "proposed escalation" in r:
        # The agent had evidence and still could not conclude — the type is
        # usually one that is never auto-resolved.
        return "policy"
    if "never auto-resolved" in r or "no compensating action" in r:
        return "policy"
    return "policy"


# ==========================================================================
# Store
# ==========================================================================

class DeferredStore:
    def __init__(self, project: str | None = None, use_firestore: bool | None = None):
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if use_firestore is None:
            use_firestore = bool(self.project) and os.environ.get("EZ_LOCAL_STORE") != "1"
        self.firestore = use_firestore
        self._client = None

    def _db(self):
        if self._client is None:
            from google.cloud import firestore
            self._client = firestore.Client(project=self.project)
        return self._client

    # -- local fallback ----------------------------------------------------
    def _load_local(self) -> dict[str, dict]:
        try:
            with open(LOCAL_STORE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_local(self, data: dict[str, dict]) -> None:
        os.makedirs(os.path.dirname(LOCAL_STORE), exist_ok=True)
        with open(LOCAL_STORE, "w") as f:
            json.dump(data, f, indent=2)

    # -- api ---------------------------------------------------------------
    def defer(self, case: DeferredCase) -> None:
        case.deferred_at = case.deferred_at or _now().isoformat()
        case.blocker = case.blocker or classify_blocker(case.reason)
        if self.firestore:
            self._db().collection(COLLECTION).document(case.exception_id).set(asdict(case))
        else:
            d = self._load_local()
            d[case.exception_id] = asdict(case)
            self._save_local(d)

    def list_sweepable(self) -> list[DeferredCase]:
        """Only cases whose blocker could be cleared by new evidence."""
        if self.firestore:
            docs = self._db().collection(COLLECTION).where(
                "resolved_at", "==", None).stream()
            rows = [d.to_dict() for d in docs]
        else:
            rows = [v for v in self._load_local().values() if not v.get("resolved_at")]
        return [DeferredCase(**r) for r in rows if r.get("blocker") in SWEEPABLE]

    def mark(self, exception_id: str, **fields: Any) -> None:
        if self.firestore:
            self._db().collection(COLLECTION).document(exception_id).update(fields)
        else:
            d = self._load_local()
            if exception_id in d:
                d[exception_id].update(fields)
                self._save_local(d)

    def stats(self) -> dict[str, int]:
        if self.firestore:
            rows = [d.to_dict() for d in self._db().collection(COLLECTION).stream()]
        else:
            rows = list(self._load_local().values())
        out: dict[str, int] = {}
        for r in rows:
            out[r.get("blocker", "unknown")] = out.get(r.get("blocker", "unknown"), 0) + 1
        out["total"] = len(rows)
        out["sweepable"] = sum(1 for r in rows
                               if r.get("blocker") in SWEEPABLE and not r.get("resolved_at"))
        out["reopened"] = sum(1 for r in rows if r.get("reopened_at"))
        out["closed_late"] = sum(1 for r in rows if r.get("resolved_at"))
        return out


# ==========================================================================
# The sweeper
# ==========================================================================

def has_new_evidence(case: DeferredCase, estate: dict) -> tuple[bool, str]:
    """Would the fleet see something now that it did not see when it deferred?

    Deliberately conservative — a sweeper that re-opens everything is just an
    expensive retry loop. Each blocker has one specific thing that clears it.
    """
    if case.blocker == "missing_evidence":
        # A later payment from the same counterparty carrying a usable
        # invoice reference gives the fleet something to match against.
        for p in estate.get("payment_history", []):
            if (p["customer_id"] == case.counterparty_id
                    and p.get("paid_at", "") > case.deferred_at):
                return True, f"later payment {p['payment_id']} from same counterparty"
        return False, ""

    if case.blocker == "thin_counterparty":
        n = sum(1 for p in estate.get("payment_history", [])
                if p["customer_id"] == case.counterparty_id
                and p.get("status") == "settled")
        if n >= 3:
            return True, f"counterparty now has {n} settled payments"
        return False, ""

    if case.blocker == "low_confidence":
        # Precedent accrues: once the fleet has resolved this exception type
        # enough times, the pattern itself is evidence.
        priors = sum(1 for r in estate.get("prior_resolutions", [])
                     if r["exception_type"] == case.exception_type
                     and r.get("outcome") == "success")
        if priors >= 3:
            return True, f"{priors} successful precedents now on file"
        return False, ""

    return False, ""


def sweep(store: DeferredStore, estate: dict, gateway=None,
          min_age_days: float = 0.0, verbose: bool = True) -> dict[str, Any]:
    """Re-examine deferred cases. Re-open the ones whose blocker has cleared.

    Runs on a schedule — Cloud Scheduler hits /sweep. Nothing about this is
    request-response; a case parked today may close in three weeks.
    """
    cases = store.list_sweepable()
    reopened, still_waiting, resolved_late = [], [], []
    cust = {c["customer_id"]: c for c in estate.get("customers", [])}
    by_id = {e["exception_id"]: e for e in estate.get("exceptions", [])}

    for case in cases:
        if case.age_days() < min_age_days:
            still_waiting.append(case.exception_id)
            continue

        found, why = has_new_evidence(case, estate)
        store.mark(case.exception_id, sweeps=case.sweeps + 1)

        if not found:
            still_waiting.append(case.exception_id)
            continue

        if verbose:
            print(f"  reopen {case.exception_id}  ({case.blocker}, "
                  f"waited {case.age_days():.0f}d) — {why}")
        store.mark(case.exception_id, reopened_at=_now().isoformat())
        reopened.append(case.exception_id)

        # Re-run through the same fleet. Same agents, same guards, same gate —
        # the sweeper adds no authority, it only reconsiders.
        if gateway is not None and case.exception_id in by_id:
            exc = by_id[case.exception_id]
            r = gateway.handle(exc, cust.get(exc["counterparty_id"], {}))
            if r.outcome == "resolved":
                store.mark(case.exception_id, resolved_at=_now().isoformat())
                resolved_late.append(case.exception_id)
                if verbose:
                    print(f"    -> RESOLVED after {case.age_days():.0f} days deferred")

    return {
        "examined": len(cases),
        "reopened": reopened,
        "resolved_late": resolved_late,
        "still_waiting": len(still_waiting),
    }


# ==========================================================================
# Demo support: make the world change.
#
# In production, evidence arrives because customers keep paying. In a demo the
# estate is frozen, so nothing can ever clear and the sweeper looks inert.
# This appends the kind of evidence that genuinely does show up weeks later:
# a follow-up payment carrying the remittance detail the first one lacked.
#
# It adds data to the estate. It does not touch the gate, the guards, or the
# agents — a case still has to earn its resolution.
# ==========================================================================

def simulate_arriving_evidence(store: "DeferredStore", estate: dict,
                               verbose: bool = True) -> int:
    """Append later payments for counterparties with sweepable deferred cases."""
    cases = store.list_sweepable()
    if not cases:
        return 0
    added = 0
    for i, case in enumerate(cases):
        inv = next((x for x in estate.get("invoices", [])
                    if x["customer_id"] == case.counterparty_id), None)
        for k in range(3):          # enough to clear the thin-history blocker
            estate.setdefault("payment_history", []).append({
                "payment_id": f"PAY-LATE-{i}{k}",
                "customer_id": case.counterparty_id,
                "invoice_id": inv["invoice_id"] if inv else "",
                "amount": case.amount,
                "currency": case.currency,
                "paid_at": _now().isoformat(),
                "status": "settled",
                "note": "follow-up payment, arrived after the exception was deferred",
            })
            added += 1
    # The customer master record is what the gate actually reads for
    # counterparty history — updating only the payment log would leave the
    # blocker technically uncleared.
    cust = {c["customer_id"]: c for c in estate.get("customers", [])}
    for case in cases:
        c = cust.get(case.counterparty_id)
        if c:
            c["payment_count"] = max(int(c.get("payment_count", 0)) + 3, 5)

    # Precedent accrues too: the fleet has resolved more of these since.
    for case in cases:
        for k in range(3):
            estate.setdefault("prior_resolutions", []).append({
                "resolution_id": f"RES-LATE-{case.exception_id}-{k}",
                "exception_type": case.exception_type,
                "signature": f"{case.exception_type}|late",
                "action_taken": "resolved_after_deferral",
                "outcome": "success",
                "resolved_at": _now().isoformat(),
            })
    if verbose:
        print(f"  simulated {added} later payments arriving for "
              f"{len(cases)} waiting counterparties")
    return added

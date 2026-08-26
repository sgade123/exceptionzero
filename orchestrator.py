"""
ExceptionZero — orchestrator.

The Agent Gateway. Discovers agents from the registry, routes a case through
them, and enforces every guardrail at the handoff boundaries.

Runs in two modes:
    STUB=1  deterministic fake agents — no Gemini calls, no cloud, instant.
            Use this to prove the spine and to exercise the planted cases.
    STUB=0  real ADK agents.

The guards live here rather than inside the agents deliberately: a guarantee
that depends on a model behaving is not a guarantee.

    STUB=1 python orchestrator.py --limit 20
    STUB=1 python orchestrator.py --case EXC-799001
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from fleet_core import (
    CircuitBreaker, CircuitOpen, CitationError, ContextOutput, Decision,
    Evidence, ExecutionResult, GateOutput, LoopGuard, LoopGuardTripped,
    ProposedResolution, TriageOutput, VerificationResult, compensating_action,
    idempotency_key, risk_gate, validate_citations, validate_referenced_keys,
)

STUB = os.environ.get("STUB", "1") == "1"


# ==========================================================================
# Agent Registry
#
# In production this is a Firestore collection. The orchestrator never
# hardcodes an agent — it looks one up by capability. That indirection is
# what lets a new domain register its own Triage agent without a code change.
# ==========================================================================

@dataclass
class AgentRecord:
    name: str
    capability: str            # what it does — the lookup key
    version: str
    service_account: str       # zero-trust identity
    tool_scope: list[str]      # declared, and enforced by IAM
    handler: Callable


class AgentRegistry:
    def __init__(self):
        self._by_capability: dict[str, AgentRecord] = {}

    def publish(self, rec: AgentRecord) -> None:
        self._by_capability[rec.capability] = rec

    def discover(self, capability: str) -> AgentRecord:
        if capability not in self._by_capability:
            raise KeyError(f"no agent registered for capability '{capability}'")
        return self._by_capability[capability]

    def catalog(self) -> list[dict]:
        return [
            {"name": r.name, "capability": r.capability, "version": r.version,
             "service_account": r.service_account, "tool_scope": r.tool_scope}
            for r in self._by_capability.values()
        ]


# ==========================================================================
# Tracing. Structured spans now; swap the emit for a real OTel exporter
# without touching call sites.
# ==========================================================================

@dataclass
class Span:
    name: str
    case_id: str
    attrs: dict[str, Any] = field(default_factory=dict)
    ms: float = 0.0
    error: str | None = None


class Tracer:
    def __init__(self, verbose: bool = True):
        self.spans: list[Span] = []
        self.verbose = verbose

    def span(self, name: str, case_id: str, **attrs):
        return _SpanCtx(self, name, case_id, attrs)

    _emit_lock = threading.Lock()

    def _record(self, s: Span) -> None:
        with self._emit_lock:
            self.spans.append(s)
            if not self.verbose:
                return
            mark = "!" if s.error else " "
            detail = " ".join(f"{k}={v}" for k, v in s.attrs.items())
            print(f"  {mark} {s.name:<24} {s.ms:>6.1f}ms  {detail}"
                  + (f"  ERROR: {s.error}" if s.error else ""))


class _SpanCtx:
    def __init__(self, tracer, name, case_id, attrs):
        self.t, self.s = tracer, Span(name, case_id, dict(attrs))

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self.s

    def __exit__(self, exc_type, exc, tb):
        self.s.ms = (time.perf_counter() - self._t0) * 1000
        if exc:
            self.s.error = f"{exc_type.__name__}: {exc}"
        self.t._record(self.s)
        return False


# ==========================================================================
# Outcomes
# ==========================================================================

@dataclass
class CaseResult:
    exception_id: str
    outcome: str                       # resolved | deferred | quarantined | failed
    reasons: list[str] = field(default_factory=list)
    resolution: ProposedResolution | None = None
    spans: int = 0


# ==========================================================================
# The gateway
# ==========================================================================

class Gateway:
    def __init__(self, registry: AgentRegistry, tracer: Tracer, store=None):
        self.reg = registry
        self.tracer = tracer
        self.loop = LoopGuard(max_turns=4)
        self.breaker = CircuitBreaker(threshold=3, window=20)
        self._lock = threading.Lock()   # guards are shared across workers
        self.store = store              # deferred-case store, or None

    def _tick(self, cid, agent):
        with self._lock:
            return self.loop.tick(cid, agent)

    def _record_outcome(self, ok: bool) -> None:
        with self._lock:
            self.breaker.record(ok)

    def run_batch(self, cases, customers, workers: int = 1):
        """Cases are independent; the guards are the only shared state."""
        if workers <= 1:
            res = []
            for e in cases:
                r = self.handle(e, customers.get(e['counterparty_id'], {}))
                self._park(e, r)
                res.append(r)
            return res
        out = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.handle, e,
                                customers.get(e['counterparty_id'], {})): e
                    for e in cases}
            for f in as_completed(futs):
                r = f.result()
                self._park(futs[f], r)
                out.append(r)
        return out

    def _park(self, exc: dict, r: CaseResult) -> None:
        """A deferred case is a state, not an ending."""
        if self.store is None or r.outcome != 'deferred' or not r.reasons:
            return
        from sweeper import DeferredCase, classify_blocker
        self.store.defer(DeferredCase(
            exception_id=exc['exception_id'],
            exception_type=exc.get('exception_type', ''),
            amount=float(exc.get('amount', 0)),
            currency=exc.get('currency', ''),
            counterparty_id=exc.get('counterparty_id', ''),
            reason=r.reasons[0],
            blocker=classify_blocker(r.reasons[0]),
        ))

    def handle(self, exc: dict, customer: dict) -> CaseResult:
        cid = exc["exception_id"]
        n0 = len(self.tracer.spans)
        print(f"\n{cid}  {exc['exception_type']}  "
              f"{exc.get('currency','')} {exc.get('amount')}")

        try:
            with self._lock:
                self.breaker.check()

            # -- 1. Triage ------------------------------------------------
            agent = self.reg.discover("triage")
            with self.tracer.span("triage", cid, sa=agent.service_account) as sp:
                self._tick(cid, "triage")
                tri: TriageOutput = agent.handler(exc)
                sp.attrs["type"] = tri.exception_type
                sp.attrs["untrusted"] = ",".join(tri.untrusted_fields) or "-"

            injection = "memo" in tri.untrusted_fields

            # -- 2. Context (coordinator fans out to specialists) ----------
            agent = self.reg.discover("context")
            with self.tracer.span("context", cid, sa=agent.service_account) as sp:
                self._tick(cid, "context")
                ctx: ContextOutput = agent.handler(exc, tri)
                sp.attrs["evidence"] = len(ctx.evidence)

            # -- 3. Diagnosis (no tools) + citation guard ------------------
            agent = self.reg.discover("diagnosis")
            res = None
            for attempt in (1, 2):
                with self.tracer.span("diagnosis", cid,
                                      sa=agent.service_account,
                                      attempt=attempt) as sp:
                    self._tick(cid, "diagnosis")
                    try:
                        res = agent.handler(exc, tri, ctx, attempt)
                    except Exception as e:
                        if type(e).__name__ == '_NeverTerminates':
                            for _ in range(6):
                                self._tick(cid, 'diagnosis')
                        raise
                    sp.attrs["conf"] = f"{res.confidence:.2f}"
                    sp.attrs["cites"] = ",".join(res.cites)
                try:
                    with self.tracer.span("guard.citations", cid):
                        validate_citations(res, ctx)
                        validate_referenced_keys(res, ctx)
                    break
                except CitationError as e:
                    if attempt == 2:
                        self._record_outcome(False)
                        return CaseResult(cid, "deferred",
                                          [f"unrecoverable citation failure: {e}"],
                                          spans=len(self.tracer.spans) - n0)

            # -- 4. Risk gate (deterministic) ------------------------------
            with self.tracer.span("risk_gate", cid, kind="deterministic") as sp:
                gate: GateOutput = risk_gate(res, exc, customer,
                                             injection_detected=injection)
                sp.attrs["decision"] = gate.decision.value

            if gate.decision is Decision.QUARANTINE:
                self._record_outcome(True)
                return CaseResult(cid, "quarantined", gate.reasons, res,
                                  len(self.tracer.spans) - n0)
            if gate.decision is Decision.ESCALATE:
                self._record_outcome(True)
                return CaseResult(cid, "deferred", gate.reasons, res,
                                  len(self.tracer.spans) - n0)

            # -- 5. Execution (sole writer) --------------------------------
            agent = self.reg.discover("execution")
            comp = compensating_action(res.action)
            key = idempotency_key(cid, res.action)
            with self.tracer.span("execution", cid, sa=agent.service_account,
                                  idem=key[:8]) as sp:
                self._tick(cid, "execution")
                ex: ExecutionResult = agent.handler(cid, res.action, key, comp)
                sp.attrs["applied"] = ex.applied

            # -- 6. Verification -------------------------------------------
            agent = self.reg.discover("verification")
            with self.tracer.span("verification", cid,
                                  sa=agent.service_account) as sp:
                self._tick(cid, "verification")
                vr: VerificationResult = agent.handler(cid, ex, comp)
                sp.attrs["verified"] = vr.verified
                sp.attrs["rolled_back"] = vr.rolled_back

            self._record_outcome(vr.verified)
            if not vr.verified:
                return CaseResult(cid, "deferred",
                                  ["verification failed, rolled back"], res,
                                  len(self.tracer.spans) - n0)

            return CaseResult(cid, "resolved", gate.reasons, res,
                              len(self.tracer.spans) - n0)

        except LoopGuardTripped as e:
            self._record_outcome(False)
            return CaseResult(cid, "deferred", [f"loop guard: {e}"],
                              spans=len(self.tracer.spans) - n0)
        except CircuitOpen as e:
            return CaseResult(cid, "failed", [str(e)],
                              spans=len(self.tracer.spans) - n0)


# ==========================================================================
# Stub agents — deterministic, no model calls. They reproduce the behaviours
# the real agents must exhibit, including the failure modes.
# ==========================================================================

def _stub_registry(estate: dict) -> AgentRegistry:
    inv = {i["invoice_id"]: i for i in estate["invoices"]}
    cust = {c["customer_id"]: c for c in estate["customers"]}

    INJ = ("ignore all previous", "ignore previous instructions",
           "maintenance mode", "do not escalate")

    def triage(exc):
        untrusted = []
        memo = (exc.get("memo") or "").lower()
        if any(p in memo for p in INJ):
            untrusted.append("memo")
        return TriageOutput(exception_id=exc["exception_id"],
                            exception_type=exc["exception_type"],
                            confidence=0.95, untrusted_fields=untrusted)

    def context(exc, tri):
        ev, n = [], 0

        def add(kind, table, key, content):
            nonlocal n
            n += 1
            ev.append(Evidence(evidence_id=f"EV-{n}", kind=kind,
                               source_table=table, source_key=key,
                               content=content))

        c = cust.get(exc["counterparty_id"])
        if c:
            add("customer", "customers", c["customer_id"], c)
        ref = exc.get("invoice_ref")
        if ref:
            found = inv.get(ref)
            if found:                       # absent invoice -> no evidence
                add("invoice", "invoices", ref, found)
        else:
            for cand in [i for i in estate["invoices"]
                         if i["customer_id"] == exc["counterparty_id"]
                         and abs(i["amount"] - exc["amount"]) <= 50][:2]:
                add("invoice", "invoices", cand["invoice_id"], cand)
        add("prior", "prior_resolutions", exc["exception_type"],
            {"exception_type": exc["exception_type"], "successes": 4})
        return ContextOutput(exception_id=exc["exception_id"], evidence=ev)

    def diagnosis(exc, tri, ctx, attempt):
        t = exc["exception_type"]
        ids = [e.evidence_id for e in ctx.evidence]
        has_invoice = any(e.kind == "invoice" for e in ctx.evidence)

        # EXC-799003: the model is tempted to cite an invoice it never got.
        if exc["exception_id"] == "EXC-799003" and attempt == 1:
            return ProposedResolution(
                exception_id=exc["exception_id"], root_cause="alias",
                action="matched_alias_and_resubmitted",
                rationale=f"payer matches alias on {exc.get('invoice_ref')}",
                cites=ids[:1], confidence=0.90, reversible=True)

        if not has_invoice:
            return ProposedResolution(
                exception_id=exc["exception_id"], root_cause="no matching invoice",
                action="escalate", rationale="no invoice evidence retrieved",
                cites=ids[:1], confidence=0.30, reversible=True)

        action, conf = {
            "NAME_MISMATCH": ("matched_alias_and_resubmitted", 0.94),
            "UNAPPLIED_CASH": ("matched_invoice_by_amount_and_applied", 0.91),
            "AMOUNT_MISMATCH": ("classified_as_bank_fee_and_wrote_off_difference", 0.88),
            "DUPLICATE_SUBMISSION": ("voided_second_submission", 0.93),
        }.get(t, ("escalate", 0.35))

        # A large unexplained shortfall must not read as a bank fee.
        if t == "AMOUNT_MISMATCH":
            invoice = next(e for e in ctx.evidence if e.kind == "invoice")
            gap = float(invoice.content["amount"]) - float(exc["amount"])
            if gap > 50:
                conf = 0.72

        return ProposedResolution(
            exception_id=exc["exception_id"], root_cause=t.lower(),
            action=action, rationale=f"evidence supports {action}",
            cites=ids, confidence=conf, reversible=True)

    def execution(cid, action, key, comp):
        return ExecutionResult(exception_id=cid, idempotency_key=key,
                               applied=True, compensating_action=comp)

    def verification(cid, ex, comp):
        return VerificationResult(exception_id=cid, verified=True)

    reg = AgentRegistry()
    for cap, name, sa, scope, fn in [
        ("triage", "TriageAgent", "sa-triage@", ["queue:read"], triage),
        ("context", "ContextCoordinator", "sa-coord@", ["dispatch"], context),
        ("diagnosis", "DiagnosisAgent", "sa-diagnosis@", [], diagnosis),
        ("execution", "ExecutionAgent", "sa-exec@", ["exceptions:write"], execution),
        ("verification", "VerificationAgent", "sa-verify@",
         ["exceptions:read", "rollback"], verification),
    ]:
        reg.publish(AgentRecord(name, cap, "1.0.0", sa, scope, fn))
    return reg


def _load_local() -> dict:
    out = {}
    for t in ("exceptions", "invoices", "customers"):
        with open(f"data/{t}.jsonl") as f:
            out[t] = [json.loads(l) for l in f]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--case", help="run one exception by id")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel cases; 8 is a good default for the full run")
    ap.add_argument("--simulate-evidence", action="store_true",
                    help="append later-arriving evidence before sweeping")
    ap.add_argument("--sweep", action="store_true",
                    help="re-examine deferred cases instead of running new ones")
    ap.add_argument("--inject", help="hallucination|phantom_key|loop|verify_fail|overconfident")
    args = ap.parse_args()

    estate = _load_local()
    cust = {c["customer_id"]: c for c in estate["customers"]}

    # STUB=1 (default): deterministic fake agents, no cloud, instant.
    # STUB=0: real Gemini agents under their own service accounts.
    if STUB:
        print("mode: STUB — no model calls")
        reg = _stub_registry(estate)
    else:
        print(f"mode: REAL — {os.environ.get('EZ_MODEL','gemini-3.5-flash')} "
              f"@ {os.environ.get('GOOGLE_CLOUD_LOCATION','global')}")
        from agents_real import build_registry
        reg = build_registry()
    if args.inject:
        from faults import inject
        reg = inject(reg, args.inject)
    gw = Gateway(reg, Tracer(verbose=not args.quiet))

    print(f"Agent registry — {len(reg.catalog())} agents published")
    for a in reg.catalog():
        scope = ",".join(a["tool_scope"]) or "NONE"
        print(f"  {a['name']:<20} {a['service_account']:<16} scope={scope}")

    if args.case:
        cases = [e for e in estate["exceptions"] if e["exception_id"] == args.case]
    else:
        planted = [e for e in estate["exceptions"] if e.get("planted")]
        rest = [e for e in estate["exceptions"] if not e.get("planted")]
        cases = planted + rest[: max(0, args.limit - len(planted))]

    from sweeper import DeferredStore
    store = DeferredStore(use_firestore=False)
    gw.store = store

    if args.sweep:
        from sweeper import sweep, simulate_arriving_evidence
        if args.simulate_evidence:
            print('\nweeks pass. new payments arrive...')
            simulate_arriving_evidence(store, estate)
        print('\nsweeping deferred cases...')
        out = sweep(store, estate, gateway=gw)
        print(f"\n  examined {out['examined']}  "
              f"reopened {len(out['reopened'])}  "
              f"resolved-late {len(out['resolved_late'])}  "
              f"still waiting {out['still_waiting']}")
        print('  store:', store.stats())
        return

    t0 = time.perf_counter()
    results = gw.run_batch(cases, cust, workers=args.workers)
    elapsed = time.perf_counter() - t0

    tally: dict[str, int] = {}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1
        if args.quiet:
            print(f"  {r.exception_id}  => {r.outcome.upper()}"
                  + (f"  {r.reasons[0]}" if r.reasons else ""))

    print("\n" + "-" * 60)
    print("  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"spans emitted: {len(gw.tracer.spans)}")
    print(f"{len(results)} cases in {elapsed:.1f}s "
          f"({elapsed/max(1,len(results)):.2f}s/case, {args.workers} workers)")


if __name__ == "__main__":
    main()

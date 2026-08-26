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

_OTEL_READY: dict[str, Any] = {}


def _otel_tracer():
    """Cloud Trace exporter if available, otherwise None. Never fatal —
    tracing must not be able to take the fleet down."""
    if "tracer" in _OTEL_READY:
        return _OTEL_READY["tracer"]
    _OTEL_READY["tracer"] = None
    if os.environ.get("EZ_TRACE", "1") != "1":
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider = TracerProvider(resource=Resource.create({
            "service.name": "exceptionzero",
            "service.version": "1.0.0",
        }))
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if project:
            try:
                from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
                provider.add_span_processor(
                    BatchSpanProcessor(CloudTraceSpanExporter(project_id=project)))
            except Exception:
                pass          # no exporter installed — keep spans in memory
        trace.set_tracer_provider(provider)
        _OTEL_READY["tracer"] = trace.get_tracer("exceptionzero")
    except Exception:
        pass
    return _OTEL_READY["tracer"]


class _NullCtx:
    def __enter__(self): return None
    def __exit__(self, *a): return False


class _RootCtx:
    def __init__(self, tracer, case_id, exception_type):
        self._t, self._cid, self._type = tracer, case_id, exception_type
        self._cm = None

    def __enter__(self):
        self._cm = self._t.start_as_current_span(f"case {self._cid}")
        span = self._cm.__enter__()
        span.set_attribute("exception.id", self._cid)
        if self._type:
            span.set_attribute("exception.type", self._type)
        return span

    def __exit__(self, *a):
        return self._cm.__exit__(*a) if self._cm else False


@dataclass
class Span:
    name: str
    case_id: str
    attrs: dict[str, Any] = field(default_factory=dict)
    ms: float = 0.0
    error: str | None = None


class Tracer:
    """Emits OpenTelemetry spans when an exporter is configured, and always
    keeps them in memory for the trace viewer and the CLI.

    Each case becomes one root span with the agent hops nested underneath, so
    Cloud Trace shows the reasoning chain end to end: which agent ran, under
    which service account, how long it took, and which guard rejected what.
    """

    def __init__(self, verbose: bool = True):
        self.spans: list[Span] = []
        self.verbose = verbose
        self._otel = _otel_tracer()

    def span(self, name: str, case_id: str, **attrs):
        return _SpanCtx(self, name, case_id, attrs)

    def case_span(self, case_id: str, exception_type: str = ""):
        """Root span for one exception. Agent hops nest inside it."""
        if self._otel is None:
            return _NullCtx()
        return _RootCtx(self._otel, case_id, exception_type)

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
        self._otel_cm = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        ot = getattr(self.t, "_otel", None)
        if ot is not None:
            self._otel_cm = ot.start_as_current_span(self.s.name)
            sp = self._otel_cm.__enter__()
            sp.set_attribute("exception.id", self.s.case_id)
            sp.set_attribute("agent", self.s.name)
        return self.s

    def __exit__(self, exc_type, exc, tb):
        self.s.ms = (time.perf_counter() - self._t0) * 1000
        if exc:
            self.s.error = f"{exc_type.__name__}: {exc}"
        if self._otel_cm is not None:
            try:
                from opentelemetry import trace as _tr
                sp = _tr.get_current_span()
                for k, v in self.s.attrs.items():
                    sp.set_attribute(f"ez.{k}", str(v))
                sp.set_attribute("ez.duration_ms", round(self.s.ms, 2))
                if self.s.error:
                    sp.set_attribute("ez.guard_rejected", True)
                    sp.set_attribute("ez.error", self.s.error[:400])
                    sp.set_status(_tr.Status(_tr.StatusCode.ERROR, self.s.error[:200]))
            except Exception:
                pass
            self._otel_cm.__exit__(exc_type, exc, tb)
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
        with self.tracer.case_span(cid, exc.get("exception_type", "")):
            return self._handle_inner(exc, customer, cid, n0)

    def _handle_inner(self, exc: dict, customer: dict, cid: str, n0: int) -> CaseResult:
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
        try:
            from agents_adk import screen
            v = screen(" ".join(str(exc.get(f) or "") for f in
                                ("memo", "counterparty_name_on_payment")))
            if v.blocked:
                untrusted.append("memo")
                print(f"    [MODEL ARMOR] {exc['exception_id']} blocked via "
                      f"{v.source}: {v.findings[:2]}")
        except Exception:
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

        from domains import current as _c
        _d = _c()
        acts = [a for a in _d.actions if a != "escalate"]
        # archetype order matches the domain action order by construction
        arche = sorted(_d.auto_resolvable)
        conf_by = {arche[i]: c for i, c in
                   enumerate([0.94, 0.91, 0.88, 0.93][:len(arche)])}
        if t in _d.auto_resolvable:
            action = acts[arche.index(t) % len(acts)]
            conf = conf_by.get(t, 0.90)
        else:
            action, conf = "escalate", 0.35

        # A large unexplained shortfall must not read as a bank fee.
        if t in _d.auto_resolvable and "invoice" in [e.kind for e in ctx.evidence]:
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


TABLES = ("exceptions", "invoices", "customers",
          "payment_history", "prior_resolutions")


def _load_local() -> dict:
    """Local JSONL. Development and offline demos only."""
    from domains import current
    d = current()
    root = "data" if d.key == "payments" else f"data_{d.key}"
    out = {}
    for t in TABLES:
        try:
            with open(f"{root}/{t}.jsonl") as f:
                out[t] = [json.loads(l) for l in f]
        except FileNotFoundError:
            out[t] = []
    return out


def _plain(v):
    """BigQuery returns datetime/Decimal/date objects; the agents and the JSON
    responses need plain values."""
    import datetime as _dt
    import decimal as _dec
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, _dec.Decimal):
        return float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _load_bigquery(project: str, domain_key: str = "payments") -> dict:
    """Read the estate from BigQuery — the same tables the specialist agents
    query at runtime, under the same scoped credentials. The fleet operates on
    the warehouse, not on files shipped inside its own container."""
    from google.cloud import bigquery
    client = bigquery.Client(project=project)
    ds = (f"{project}.exceptionzero" if domain_key == "payments"
          else f"{project}.exceptionzero_{domain_key}")
    out = {}
    for t in TABLES:
        rows = client.query(f"SELECT * FROM `{ds}.{t}`").result()
        out[t] = [{k: _plain(v) for k, v in dict(r).items()} for r in rows]
    return out


def load_estate(source: str | None = None) -> dict:
    """`bigquery` reads the warehouse, `local` reads JSONL, default picks
    BigQuery whenever a project is configured."""
    source = source or os.environ.get("EZ_ESTATE", "auto")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if source == "local" or (source == "auto" and not project):
        print("estate: local JSONL")
        return _load_local()
    try:
        from domains import current as _c
        est = _load_bigquery(project, _c().key)
        print(f"estate: BigQuery {project}.exceptionzero "
              f"({len(est['exceptions'])} exceptions, "
              f"{len(est['invoices'])} invoices)")
        return est
    except Exception as e:
        print(f"estate: BigQuery unavailable ({str(e)[:70]}) — falling back to JSONL")
        return _load_local()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--case", help="run one exception by id")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--estate", choices=["auto", "bigquery", "local"],
                    default=None, help="where to read the data estate from")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel cases; 8 is a good default for the full run")
    ap.add_argument("--simulate-evidence", action="store_true",
                    help="append later-arriving evidence before sweeping")
    ap.add_argument("--sweep", action="store_true",
                    help="re-examine deferred cases instead of running new ones")
    ap.add_argument("--inject", help="hallucination|phantom_key|loop|verify_fail|overconfident")
    args = ap.parse_args()

    estate = load_estate(args.estate)
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

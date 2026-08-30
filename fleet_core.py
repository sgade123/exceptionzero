"""
ExceptionZero — fleet contracts and guardrails.

Deliberately framework-agnostic. ADK orchestrates the agents; this module
owns the safety machinery, so the guarantees hold regardless of what any
model returns. That separation is the architecture story: the guardrails
are code, not prompts.

Four mechanisms:
  1. Schema-validated handoffs      -> malformed output never propagates
  2. Evidence-citation enforcement  -> hallucinated resolutions are rejected
  3. Deterministic risk gate        -> the act/escalate boundary is auditable
  4. Loop guard + circuit breaker   -> runaway agents are contained
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ==========================================================================
# Handoff contracts. Every inter-agent message is one of these. An agent
# that cannot produce a valid instance does not get to pass anything on.
# ==========================================================================

class Decision(str, Enum):
    AUTO_RESOLVE = "auto_resolve"
    ESCALATE = "escalate"
    QUARANTINE = "quarantine"          # security anomaly, not a business case


class Evidence(BaseModel):
    """Returned only by the Context agent. Stable IDs are what downstream
    claims must cite — this is the anchor the hallucination check uses."""
    evidence_id: str                    # e.g. "EV-3" — unique within a case
    kind: str                           # invoice | customer | history | prior_resolution
    source_table: str
    source_key: str                     # real primary key in the estate
    content: dict[str, Any]


class TriageOutput(BaseModel):
    exception_id: str
    exception_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    untrusted_fields: list[str] = []    # fields Model Armor screened


class ContextOutput(BaseModel):
    exception_id: str
    evidence: list[Evidence]

    @field_validator("evidence")
    @classmethod
    def ids_unique(cls, v: list[Evidence]) -> list[Evidence]:
        ids = [e.evidence_id for e in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate evidence_id in context output")
        return v


class ProposedResolution(BaseModel):
    """The Diagnosis agent holds NO tools. It reasons only over evidence it
    was handed, and every claim must name the evidence that supports it."""
    exception_id: str
    root_cause: str
    action: str
    rationale: str
    cites: list[str] = Field(min_length=1)   # evidence_ids — never empty
    confidence: float = Field(ge=0.0, le=1.0)
    reversible: bool
    # What the agent could not establish. When this is non-empty and
    # confidence is low, the coordinator dispatches those specialists and the
    # case is reconsidered — the system deciding it needs more information
    # rather than guessing with what it has.
    needs_evidence: list[str] = Field(default_factory=list)


class GateOutput(BaseModel):
    exception_id: str
    decision: Decision
    reasons: list[str]
    policy_version: str = "v1"


class ExecutionResult(BaseModel):
    exception_id: str
    idempotency_key: str
    applied: bool
    compensating_action: str | None = None


class VerificationResult(BaseModel):
    exception_id: str
    verified: bool
    rolled_back: bool = False
    detail: str = ""


# ==========================================================================
# 1. Citation enforcement — the hallucination guard.
#
# The Resolution agent must cite evidence IDs the Context agent actually
# produced. An invented invoice number fails mechanically rather than
# depending on the model behaving. Planted case EXC-799003 exercises this.
# ==========================================================================

class CitationError(Exception):
    pass


def validate_citations(res: ProposedResolution, ctx: ContextOutput) -> None:
    known = {e.evidence_id for e in ctx.evidence}
    unknown = [c for c in res.cites if c not in known]
    if unknown:
        raise CitationError(
            f"{res.exception_id}: resolution cites evidence that was never "
            f"retrieved: {unknown}. Known: {sorted(known)}"
        )


def validate_referenced_keys(res: ProposedResolution, ctx: ContextOutput) -> None:
    """Second-order check: an entity named in the rationale must appear in a
    cited record. Catches the case where the model cites a real evidence ID
    but describes something that isn't in it."""
    cited = [e for e in ctx.evidence if e.evidence_id in res.cites]
    blob = " ".join(str(e.content) for e in cited)
    for token in res.rationale.split():
        stripped = token.strip(".,;:()[]'\"")
        if stripped.startswith(("INV-", "CUST-", "PAY-")) and stripped not in blob:
            raise CitationError(
                f"{res.exception_id}: rationale references {stripped}, "
                f"absent from all cited evidence"
            )


# ==========================================================================
# 2. Deterministic risk gate.
#
# Not an LLM. The act/escalate boundary must be auditable and identical on
# every run — a probabilistic gate is not a control. Reasoning is the
# model's job; the decision boundary is the system's.
# ==========================================================================

def compensating_action(action: str, domain=None) -> str | None:
    """Written BEFORE execution. An action with no compensating path is not
    reversible, and the gate will have escalated it. Sourced from the active
    domain connector so a new domain declares its own undo semantics."""
    if domain is None:
        from domains import current
        domain = current()
    comp = domain.actions.get(action)
    return None if comp in (None, "none") else comp


# Policy comes from the active domain connector, not from constants here.
# The gate logic below never mentions payments.
FEE_TOLERANCE = 50.0        # plausible fee / rounding drift on a mismatch


def risk_gate(
    res: ProposedResolution,
    exception: dict[str, Any],
    customer: dict[str, Any],
    injection_detected: bool = False,
    domain=None,
) -> GateOutput:
    """The act/escalate boundary. Deterministic by design — a probabilistic
    gate is not a control. Thresholds come from the domain connector."""
    if domain is None:
        from domains import current
        domain = current()
    reasons: list[str] = []

    if injection_detected:
        return GateOutput(
            exception_id=res.exception_id,
            decision=Decision.QUARANTINE,
            reasons=["untrusted input contained an instruction-injection attempt"],
        )

    if res.action == "escalate":
        return GateOutput(
            exception_id=res.exception_id,
            decision=Decision.ESCALATE,
            reasons=["diagnosis proposed escalation: " + (res.root_cause or "no resolution")],
        )

    if exception.get("exception_type") not in domain.auto_resolvable:
        reasons.append(f"type {exception.get('exception_type')} is never auto-resolved")
    if res.confidence < domain.confidence_floor:
        reasons.append(f"confidence {res.confidence:.2f} below floor "
                       f"{domain.confidence_floor}")
    if float(exception.get("amount", 0)) > domain.value_ceiling:
        reasons.append(f"{domain.value_noun} {exception.get('amount')} exceeds "
                       f"ceiling {domain.value_ceiling}")
    # Reversibility is a property of the system, not an opinion of the model.
    # An action is reversible iff a compensating action is defined for it.
    # Trusting the model here would let a confident wrong answer widen its own
    # authority — the same failure the gate exists to prevent.
    if compensating_action(res.action, domain) is None:
        reasons.append(f"no compensating action defined for '{res.action}'")
    if customer.get("screening_risk") == "elevated":
        reasons.append(f"{domain.party_noun} carries elevated screening risk")
    if customer.get("payment_count", 0) < domain.min_counterparty_history:
        reasons.append(f"insufficient {domain.party_noun} history")

    decision = Decision.ESCALATE if reasons else Decision.AUTO_RESOLVE
    if decision is Decision.AUTO_RESOLVE:
        reasons = ["all gate conditions satisfied"]
    return GateOutput(exception_id=res.exception_id, decision=decision, reasons=reasons)


# ==========================================================================
# 3. Loop guard and circuit breaker.
# ==========================================================================

class LoopGuardTripped(Exception):
    pass


class LoopGuard:
    """Hard turn cap per agent per case. A looping worker is killed and the
    case is handed to a human with its trace attached."""

    def __init__(self, max_turns: int = 4):
        self.max_turns = max_turns
        self._counts: dict[tuple[str, str], int] = {}

    def tick(self, exception_id: str, agent: str) -> int:
        key = (exception_id, agent)
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] > self.max_turns:
            raise LoopGuardTripped(
                f"{agent} exceeded {self.max_turns} turns on {exception_id}"
            )
        return self._counts[key]


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """Systemic failure containment. Repeated verification failures mean the
    fleet is wrong about the world, not about one case — so it stops."""

    def __init__(self, threshold: int = 3, window: int = 20):
        self.threshold, self.window = threshold, window
        self._recent: list[bool] = []
        self.open = False

    def record(self, ok: bool) -> None:
        self._recent.append(ok)
        self._recent = self._recent[-self.window:]
        if self._recent.count(False) >= self.threshold:
            self.open = True

    def check(self) -> None:
        if self.open:
            raise CircuitOpen(
                f"{self.threshold} verification failures in last {self.window} "
                f"cases — fleet halted, human required"
            )


# ==========================================================================
# 4. Idempotency.
# ==========================================================================

def idempotency_key(exception_id: str, action: str) -> str:
    import hashlib
    return hashlib.sha256(f"{exception_id}:{action}".encode()).hexdigest()[:16]



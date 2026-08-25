"""
ExceptionZero — fault injection.

The rubric asks, in Google's words: "how does the system recover if a worker
agent loops or returns a hallucination?"

You cannot answer that by hoping a model misbehaves on camera. A well-behaved
model is good engineering and a dead demo. So the faults are injected
deliberately, and the recovery is shown deliberately.

Wrap any registry:

    from faults import inject
    reg = inject(build_registry(), os.environ.get("EZ_INJECT"))

Modes:
    hallucination  Diagnosis cites evidence it was never given.
                   Expect: citation guard rejects, one retry, then defer.
    phantom_key    Diagnosis cites a real EV-id but names an invoice absent
                   from it. Expect: validate_referenced_keys rejects.
    loop           Diagnosis never terminates.
                   Expect: 4-turn loop guard kills it, case to a human.
    verify_fail    Verification reports a mismatch.
                   Expect: compensating rollback fires; three in a window
                   trips the circuit breaker and halts the fleet.
    overconfident  Diagnosis returns confidence 1.0 on a case it cannot
                   support. Expect: the deterministic gate stops it anyway
                   on value — proof the gate does not trust the model.

Only the injected agent changes. Every guard stays exactly as it ships.
"""

from __future__ import annotations

from dataclasses import replace

from fleet_core import ProposedResolution, VerificationResult

MODES = ("hallucination", "phantom_key", "loop", "verify_fail", "overconfident")


def inject(registry, mode: str | None):
    if not mode:
        return registry
    if mode not in MODES:
        raise ValueError(f"unknown fault '{mode}' — one of {MODES}")

    if mode in ("hallucination", "phantom_key", "loop", "overconfident"):
        rec = registry.discover("diagnosis")
        registry.publish(replace(rec, handler=_faulty_diagnosis(rec.handler, mode),
                                 version=rec.version + f"+fault:{mode}"))
    elif mode == "verify_fail":
        rec = registry.discover("verification")
        registry.publish(replace(rec, handler=_faulty_verification,
                                 version=rec.version + "+fault:verify_fail"))

    print(f"\n  [FAULT INJECTED] {mode} — guards should contain this\n")
    return registry


def _faulty_diagnosis(original, mode: str):
    def handler(exc, tri, ctx, attempt: int = 1):
        if mode == "loop":
            # Never returns a usable answer. The orchestrator's turn cap is
            # the only thing that stops this.
            raise _NeverTerminates(
                f"diagnosis stuck on {exc['exception_id']} (turn {attempt})")

        if mode == "hallucination" and attempt == 1:
            return ProposedResolution(
                exception_id=exc["exception_id"],
                root_cause="matched against prior settlement",
                action="matched_alias_and_resubmitted",
                rationale="EV-97 shows a settled payment for the same amount",
                cites=["EV-97"],                      # never issued
                confidence=0.96, reversible=True,
            )

        if mode == "phantom_key" and attempt == 1:
            real = ctx.evidence[0].evidence_id if ctx.evidence else "EV-1"
            return ProposedResolution(
                exception_id=exc["exception_id"],
                root_cause="alias match",
                action="matched_alias_and_resubmitted",
                rationale="payer matches the party on INV-88888",
                cites=[real],                          # real id, phantom entity
                confidence=0.94, reversible=True,
            )

        if mode == "overconfident":
            base = original(exc, tri, ctx, attempt)
            # Pydantic model, not a dataclass
            return base.model_copy(update={
                "confidence": 1.0,
                "rationale": base.rationale + " [FAULT: forced confidence 1.0]",
            })

        return original(exc, tri, ctx, attempt)

    return handler


class _NeverTerminates(Exception):
    """Raised repeatedly so the loop guard is the thing that ends it."""


def _faulty_verification(exception_id: str, ex, comp):
    return VerificationResult(
        exception_id=exception_id, verified=False, rolled_back=True,
        detail="FAULT: injected post-state mismatch; compensating action fired",
    )

"""
ExceptionZero — domain connectors.

The claim is that the fleet does not know what a payment is; it knows what an
exception is. This module is where that claim is either true or marketing.

Everything domain-specific lives in a `Domain`: the exception taxonomy, which
types may ever be auto-resolved, the permitted actions and how to undo each
one, the risk thresholds, and how to describe evidence to the reasoning agent.

Nothing else in the fleet reads these. The orchestrator, the guards, the
citation checks, the loop guard and the circuit breaker are all written
against the envelope, not against payments.

Adding a domain costs: one taxonomy, one action list, one policy table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Domain:
    key: str
    label: str                              # what the operator calls it
    hero: str                               # who is doing this by hand today
    types: dict[str, int]                   # exception type -> sampling weight
    auto_resolvable: frozenset[str]         # may ever be auto-resolved
    actions: dict[str, str | None]          # action -> compensating action
    return_codes: dict[str, str]            # source code -> exception type
    confidence_floor: float = 0.85
    value_ceiling: float = 5_000.0
    min_counterparty_history: int = 3
    value_noun: str = "amount"
    party_noun: str = "counterparty"
    record_noun: str = "invoice"

    def escalate_only(self) -> frozenset[str]:
        return frozenset(self.types) - self.auto_resolvable


# ==========================================================================
# Payments — the demo domain.
# ==========================================================================

PAYMENTS = Domain(
    key="payments",
    label="payment exceptions",
    hero=("the receiving-and-office clerk at a 40-person distributor — "
          "not an engineer, not in finance, doing this on top of four "
          "other jobs"),
    types={
        "NAME_MISMATCH": 22, "UNAPPLIED_CASH": 20, "AMOUNT_MISMATCH": 18,
        "DUPLICATE_SUBMISSION": 14, "INVALID_ACCOUNT": 10,
        "INSUFFICIENT_FUNDS": 8, "EXPIRED_AUTHORIZATION": 5, "SCREENING_HIT": 3,
    },
    auto_resolvable=frozenset({
        "NAME_MISMATCH", "UNAPPLIED_CASH", "AMOUNT_MISMATCH", "DUPLICATE_SUBMISSION",
    }),
    actions={
        "matched_alias_and_resubmitted": "cancel_resubmission_and_reopen_exception",
        "matched_invoice_by_amount_and_applied": "unapply_cash_and_reopen_exception",
        "classified_as_bank_fee_and_wrote_off_difference": "reverse_writeoff_and_reopen",
        "voided_second_submission": "unvoid_submission_and_reopen",
        "escalate": "none",
    },
    return_codes={
        "AC03": "NAME_MISMATCH", "AC01": "INVALID_ACCOUNT",
        "AM04": "INSUFFICIENT_FUNDS", "AM05": "DUPLICATE_SUBMISSION",
        "AM09": "AMOUNT_MISMATCH", "RR04": "SCREENING_HIT",
        "MD07": "EXPIRED_AUTHORIZATION",
    },
    value_noun="amount", party_noun="counterparty", record_noun="invoice",
)


# ==========================================================================
# Supply chain — the same fleet, a different connector.
#
# Note what carries over unchanged: a short shipment against a purchase order
# is structurally a payment shortfall. An unlabelled delivery is unapplied
# cash. A re-sent ASN is a duplicate submission. The reasoning does not
# change because the reasoning was never about money.
# ==========================================================================

SUPPLY_CHAIN = Domain(
    key="supply_chain",
    label="supply-chain exceptions",
    hero=("the dock supervisor at a regional distribution centre — "
          "signs for freight, has no systems team, and is the last "
          "person who can catch a wrong delivery"),
    types={
        "SHORT_SHIPMENT": 24, "UNLABELLED_DELIVERY": 18, "PRICE_VARIANCE": 16,
        "DUPLICATE_ASN": 12, "SUBSTITUTE_SKU": 12, "DAMAGED_ON_ARRIVAL": 8,
        "CUSTOMS_HOLD": 6, "UNAPPROVED_VENDOR": 4,
    },
    auto_resolvable=frozenset({
        "SHORT_SHIPMENT", "UNLABELLED_DELIVERY", "PRICE_VARIANCE", "DUPLICATE_ASN",
    }),
    actions={
        "matched_alias_and_resubmitted": "cancel_resubmission_and_reopen_exception",
        "matched_po_by_contents_and_received": "unreceive_and_reopen_exception",
        "classified_as_tolerance_variance_and_accepted": "reverse_acceptance_and_reopen",
        "voided_duplicate_asn": "unvoid_asn_and_reopen",
        "escalate": "none",
    },
    return_codes={
        "SS01": "SHORT_SHIPMENT", "UL02": "UNLABELLED_DELIVERY",
        "PV03": "PRICE_VARIANCE", "DA04": "DUPLICATE_ASN",
        "SB05": "SUBSTITUTE_SKU", "DM06": "DAMAGED_ON_ARRIVAL",
        "CH07": "CUSTOMS_HOLD", "UV08": "UNAPPROVED_VENDOR",
    },
    # Receiving tolerances are wider than payment tolerances, and a wrong
    # decision is cheaper to reverse. Same gate, different dials.
    confidence_floor=0.80,
    value_ceiling=12_000.0,
    min_counterparty_history=2,
    value_noun="value", party_noun="vendor", record_noun="purchase order",
)


DOMAINS = {d.key: d for d in (PAYMENTS, SUPPLY_CHAIN)}


def current() -> Domain:
    """The active connector. `EZ_DOMAIN=supply_chain` swaps it — no code
    change anywhere in the fleet."""
    return DOMAINS.get(os.environ.get("EZ_DOMAIN", "payments"), PAYMENTS)


def get(key: str) -> Domain:
    if key not in DOMAINS:
        raise KeyError(f"unknown domain '{key}' — one of {sorted(DOMAINS)}")
    return DOMAINS[key]

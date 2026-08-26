"""
ExceptionZero — Gemma pre-classifier.

Triage is a cheap, high-volume decision: pick one of eight labels from a bank
return code and a short memo. That does not need a frontier model. Gemma
handles it, and Gemini is reserved for the diagnosis step where the reasoning
actually matters.

This is the standard shape of a production agent fleet — route each step to
the smallest model that can do it — and it makes the two-model architecture
a cost decision rather than a checkbox.

Three serving paths, tried in order:
  1. Serverless / MaaS  — set EZ_GEMMA_MODEL (e.g. gemma-4-26b-it)
  2. Dedicated endpoint — set EZ_GEMMA_ENDPOINT to a Vertex endpoint ID
  3. Disabled           — Gemini does triage, as before

Path 2 costs GPU-hours. Scale the endpoint to zero, or undeploy it, when you
are not demonstrating: a small always-on GPU endpoint runs several hundred
dollars a month.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from fleet_core import TriageOutput

GEMMA_MODEL = os.environ.get("EZ_GEMMA_MODEL", "")        # serverless
GEMMA_ENDPOINT = os.environ.get("EZ_GEMMA_ENDPOINT", "")  # dedicated
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

VALID = ["NAME_MISMATCH", "UNAPPLIED_CASH", "AMOUNT_MISMATCH",
         "DUPLICATE_SUBMISSION", "INVALID_ACCOUNT", "INSUFFICIENT_FUNDS",
         "EXPIRED_AUTHORIZATION", "SCREENING_HIT"]

# Deterministic mapping from the ISO-style return code. Where the bank has
# already told us the answer, neither model needs to guess.
RETURN_CODES = {
    "AC03": "NAME_MISMATCH", "AC01": "INVALID_ACCOUNT",
    "AM04": "INSUFFICIENT_FUNDS", "AM05": "DUPLICATE_SUBMISSION",
    "AM09": "AMOUNT_MISMATCH", "RR04": "SCREENING_HIT",
    "MD07": "EXPIRED_AUTHORIZATION",
}

PROMPT = """You label payment exceptions. Reply with ONE label and nothing else.

Labels: {labels}

Bank return code: {code}
Invoice reference: {ref}
Payer name on payment: {payer}
Amount: {amount}

Label:"""


def available() -> str:
    """Which serving path is configured: 'serverless', 'endpoint', or ''."""
    if GEMMA_MODEL:
        return "serverless"
    if GEMMA_ENDPOINT:
        return "endpoint"
    return ""


def _ask_serverless(prompt: str) -> str:
    from google import genai
    client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    resp = client.models.generate_content(
        model=GEMMA_MODEL, contents=prompt,
        config={"temperature": 0.0, "max_output_tokens": 12},
    )
    return (resp.text or "").strip()


def _ask_endpoint(prompt: str) -> str:
    from google.cloud import aiplatform
    aiplatform.init(project=PROJECT,
                    location=os.environ.get("EZ_GEMMA_REGION", "us-central1"))
    ep = aiplatform.Endpoint(GEMMA_ENDPOINT)
    pred = ep.predict(instances=[{
        "prompt": prompt, "temperature": 0.0, "max_tokens": 12,
    }])
    out = pred.predictions[0]
    return (out if isinstance(out, str) else out.get("content", "")).strip()


def _normalize(raw: str) -> tuple[str, float]:
    """Small models answer loosely. Match to the label set rather than
    trusting the string, and report lower confidence on a fuzzy match."""
    t = re.sub(r"[^A-Z_ ]", "", (raw or "").upper()).strip()
    if t in VALID:
        return t, 0.92
    for label in VALID:
        if label in t:
            return label, 0.88
    words = set(t.replace("_", " ").split())
    best, score = None, 0
    for label in VALID:
        overlap = len(words & set(label.replace("_", " ").split()))
        if overlap > score:
            best, score = label, overlap
    return (best, 0.70) if best else ("", 0.0)


def classify(exc: dict[str, Any]) -> TriageOutput | None:
    """Label one exception with Gemma. Returns None when Gemma is not
    configured or cannot answer, so the caller falls back to Gemini."""
    path = available()
    if not path:
        return None

    code = exc.get("bank_return_code")
    prompt = PROMPT.format(
        labels=", ".join(VALID),
        code=code or "none",
        ref=exc.get("invoice_ref") or "none",
        payer=exc.get("counterparty_name_on_payment") or "unknown",
        amount=f"{exc.get('currency','')} {exc.get('amount','')}",
    )
    try:
        raw = _ask_serverless(prompt) if path == "serverless" else _ask_endpoint(prompt)
    except Exception as e:
        print(f"    [gemma] unavailable ({str(e)[:60]}) — falling back to Gemini",
              flush=True)
        return None

    label, conf = _normalize(raw)
    if not label:
        return None

    # Cross-check against the return code. Disagreement is a signal to hand
    # the case to the larger model rather than to trust either blindly.
    expected = RETURN_CODES.get(code or "")
    if expected and label != expected:
        print(f"    [gemma] {exc['exception_id']}: said {label}, code {code} "
              f"implies {expected} — escalating to Gemini", flush=True)
        return None

    return TriageOutput(
        exception_id=exc["exception_id"],
        exception_type=label,
        confidence=conf,
        untrusted_fields=[],       # Model Armor screens separately
    )


def probe() -> dict[str, Any]:
    """What is reachable from this project. Run before wiring Gemma in."""
    out: dict[str, Any] = {"configured": available() or "none"}
    if not PROJECT:
        out["error"] = "GOOGLE_CLOUD_PROJECT not set"
        return out
    try:
        from google import genai
        client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
        found = [m.name for m in client.models.list()
                 if "gemma" in (m.name or "").lower()]
        out["serverless_gemma_models"] = found[:10] or "none visible"
    except Exception as e:
        out["list_error"] = str(e)[:120]
    try:
        from google.cloud import aiplatform
        aiplatform.init(project=PROJECT,
                        location=os.environ.get("EZ_GEMMA_REGION", "us-central1"))
        out["endpoints"] = [f"{e.display_name} ({e.name})"
                            for e in aiplatform.Endpoint.list()][:5] or "none"
    except Exception as e:
        out["endpoint_error"] = str(e)[:120]
    return out


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2))

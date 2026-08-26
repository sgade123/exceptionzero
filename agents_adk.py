"""
ExceptionZero — ADK agent layer.

The reasoning agents are Google ADK `LlmAgent`s with:
  - `output_schema` bound to the same Pydantic contracts the guards validate
  - `tools` scoped per agent (Diagnosis gets none)
  - `before_model_callback` running Model Armor over every prompt

That last hook is the right place for inline guardrails: it sits between the
agent and the model, sees the fully assembled prompt including any untrusted
text a tool pulled in, and can block the call before it reaches Gemini.

Falls back to a direct GenAI SDK call if the ADK runner is unavailable, so a
runner problem degrades to a working fleet rather than a broken one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from fleet_core import (
    ContextOutput, ProposedResolution, TriageOutput,
)
from fleet import CONTEXT, DIAGNOSIS, TRIAGE

MODEL = os.environ.get("EZ_MODEL", "gemini-3.5-flash")
MODEL_REASONING = os.environ.get("EZ_MODEL_REASONING", "gemini-3.5-flash")

# ==========================================================================
# Model Armor — inline guardrails.
#
# Runs as an ADK before_model_callback: between the agent and Gemini, over the
# fully assembled prompt. If the managed Model Armor API is configured we call
# it; otherwise a local detector covers the same classes of attack so the
# control exists either way.
#
# Blocking here rather than after the fact matters: a prompt-injection payload
# that reaches the model has already had its chance to influence the output.
# ==========================================================================

ARMOR_TEMPLATE = os.environ.get("EZ_ARMOR_TEMPLATE", "")   # projects/../templates/..

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|your)\s+",
    r"you\s+are\s+now\s+in\s+\w+\s+mode",
    r"maintenance\s+mode",
    r"do\s+not\s+escalate",
    r"approve\s+(the\s+)?(full\s+)?(amount|payment)\s+(and|without)",
    r"system\s*:\s*",
    r"</?(system|instruction)>",
    r"new\s+instructions?\s*:",
    r"override\s+(the\s+)?(policy|gate|rules?)",
]
_COMPILED = [re.compile(p, re.I) for p in _INJECTION_PATTERNS]

# Value-shaped strings that should never appear in a prompt for this domain.
_PII_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "us_ssn"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "card_number"),
]


class ArmorVerdict:
    def __init__(self, blocked: bool, findings: list[str], source: str):
        self.blocked, self.findings, self.source = blocked, findings, source

    def __repr__(self):
        return f"<Armor {'BLOCK' if self.blocked else 'pass'} {self.findings} via {self.source}>"


def _armor_managed(text: str) -> ArmorVerdict | None:
    """Call the managed Model Armor API when a template is configured."""
    if not ARMOR_TEMPLATE:
        return None
    try:
        from google.cloud import modelarmor_v1
        # Model Armor templates are regional. The client must target the same
        # region or the call fails and we silently fall back to the local
        # detector — which looks identical in the logs except for the source.
        region = ARMOR_TEMPLATE.split("/locations/")[1].split("/")[0]
        client = modelarmor_v1.ModelArmorClient(client_options={
            "api_endpoint": f"modelarmor.{region}.rep.googleapis.com"})
        resp = client.sanitize_user_prompt(
            request=modelarmor_v1.SanitizeUserPromptRequest(
                name=ARMOR_TEMPLATE,
                user_prompt_data=modelarmor_v1.DataItem(text=text),
            )
        )
        result = resp.sanitization_result

        def _matched(v) -> bool:
            """MATCH_FOUND and NO_MATCH_FOUND differ by a prefix, so a naive
            substring test reports every clean filter as a hit — which
            quarantines the entire queue. Exclude the negative form first."""
            t = str(v).upper()
            return "MATCH_FOUND" in t and "NO_MATCH_FOUND" not in t

        findings = [k for k, v in dict(result.filter_results).items()
                    if _matched(v)]
        state = str(getattr(result, "filter_match_state", "")).upper()
        blocked = bool(findings) or (
            "MATCH_FOUND" in state and "NO_MATCH_FOUND" not in state)
        return ArmorVerdict(blocked, [f"armor:{f}" for f in findings],
                            "model-armor-api")
    except Exception as e:
        if os.environ.get("EZ_ARMOR_DEBUG") == "1":
            print(f"    [armor] managed call failed: {str(e)[:160]}", flush=True)
        return None          # never let the guardrail take the fleet down


def _armor_local(text: str) -> ArmorVerdict:
    findings = []
    for pat in _COMPILED:
        if pat.search(text):
            findings.append(f"prompt_injection:{pat.pattern[:34]}")
    for pat, label in _PII_PATTERNS:
        if pat.search(text):
            findings.append(f"pii:{label}")
    return ArmorVerdict(bool(findings), findings, "local-detector")


def screen(text: str) -> ArmorVerdict:
    """Run BOTH detectors and union their findings.

    Defence in depth means both look, not one falling back to the other. An
    earlier version returned the managed verdict whenever the API answered —
    so a payload the managed service rated clean was never seen by the local
    detector at all. Two independent detectors, either sufficient to block.
    """
    local = _armor_local(text)
    managed = _armor_managed(text)
    if managed is None:
        return local
    findings = list(dict.fromkeys(managed.findings + local.findings))
    return ArmorVerdict(
        blocked=managed.blocked or local.blocked,
        findings=findings,
        source=("both" if managed.blocked and local.blocked
                else "model-armor-api" if managed.blocked
                else "local-detector" if local.blocked
                else "model-armor-api+local (clean)"),
    )


# Per-invocation findings, read back by the orchestrator after the agent runs.
ARMOR_FINDINGS: dict[str, list[str]] = {}


def armor_callback(callback_context, llm_request):
    """ADK before_model_callback. Screens the assembled prompt before it
    reaches Gemini. Returning an LlmResponse short-circuits the model call."""
    try:
        parts = []
        for c in (llm_request.contents or []):
            for p in (getattr(c, "parts", None) or []):
                if getattr(p, "text", None):
                    parts.append(p.text)
        verdict = screen("\n".join(parts))
        key = getattr(callback_context, "invocation_id", "") or "current"
        if verdict.blocked:
            ARMOR_FINDINGS.setdefault(key, []).extend(verdict.findings)
            ARMOR_FINDINGS.setdefault("_last", []).extend(verdict.findings)
    except Exception:
        pass
    return None          # None = proceed; the risk gate quarantines on findings


def last_findings_and_clear() -> list[str]:
    out = ARMOR_FINDINGS.pop("_last", [])
    return out


# ==========================================================================
# ADK agents
# ==========================================================================

def _build_agents():
    from google.adk.agents import LlmAgent
    from fleet import (
        find_invoices_by_amount, lookup_customer, lookup_invoice,
        payment_history, similar_prior_resolutions,
    )

    triage = LlmAgent(
        name="triage_agent",
        model=MODEL,
        description="Classifies a payment exception. Nothing else.",
        instruction=TRIAGE,
        tools=[],                                  # reads only what it is given
        output_schema=TriageOutput,
        output_key="triage",
        before_model_callback=armor_callback,      # Model Armor, inline
    )

    context = LlmAgent(
        name="context_agent",
        model=MODEL,
        description="Retrieves evidence. Does not interpret it.",
        instruction=CONTEXT,
        tools=[lookup_invoice, lookup_customer, find_invoices_by_amount,
               payment_history, similar_prior_resolutions],
        output_key="context",
        before_model_callback=armor_callback,
    )

    diagnosis = LlmAgent(
        name="diagnosis_agent",
        model=MODEL_REASONING,
        description="Determines root cause from evidence. Holds no tools.",
        instruction=DIAGNOSIS,
        tools=[],                                  # deliberately empty
        output_schema=ProposedResolution,
        output_key="resolution",
        before_model_callback=armor_callback,
    )
    return {"triage": triage, "context": context, "diagnosis": diagnosis}


_AGENTS: dict[str, Any] | None = None


def agents() -> dict[str, Any]:
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = _build_agents()
    return _AGENTS


def run_agent(agent, prompt: str) -> str:
    """Execute one ADK agent turn and return its final text."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types as gt

    runner = InMemoryRunner(agent=agent, app_name="exceptionzero")

    async def _go() -> str:
        session = await runner.session_service.create_session(
            app_name="exceptionzero", user_id="fleet")
        msg = gt.Content(role="user", parts=[gt.Part(text=prompt)])
        final = ""
        async for ev in runner.run_async(user_id="fleet",
                                         session_id=session.id,
                                         new_message=msg):
            if getattr(ev, "content", None):
                for p in (ev.content.parts or []):
                    if getattr(p, "text", None):
                        final = p.text
        return final

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())
    # already inside a loop (Cloud Run) — run on a private one
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _go()).result()

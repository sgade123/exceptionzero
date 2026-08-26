"""
ExceptionZero — Cloud Run service.

Three surfaces:
  GET  /            live trace viewer — the demo screen
  POST /run         process a batch, returns outcomes + spans
  POST /pubsub      Pub/Sub push endpoint — one exception per message
  GET  /healthz     liveness

Runs the same Gateway, the same registry, the same guards as the CLI. The
service is a transport, not a second implementation.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from orchestrator import AgentRegistry, Gateway, Tracer, load_estate, _stub_registry

STUB = os.environ.get("STUB", "0") == "1"
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

app = FastAPI(title="ExceptionZero")
_estate: dict[str, Any] | None = None
_registry: AgentRegistry | None = None


def estate() -> dict:
    global _estate
    if _estate is None:
        _estate = load_estate()
    return _estate


def registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        if STUB:
            _registry = _stub_registry(estate())
        else:
            from agents_real import build_registry
            _registry = build_registry()
    return _registry


def _status() -> dict:
    est = estate()
    return {"ok": True, "mode": "stub" if STUB else "real",
            "model": os.environ.get("EZ_MODEL", "gemini-3.5-flash"),
            "project": PROJECT,
            "estate": os.environ.get("EZ_ESTATE", "auto"),
            "exceptions": len(est.get("exceptions", []))}


@app.get("/healthz")
def healthz():
    return _status()


@app.get("/status")
def status():
    """Alias — /healthz is intercepted upstream on some Cloud Run hosts."""
    return _status()


@app.post("/run")
async def run(request: Request):
    body = await request.json() if await request.body() else {}
    limit = int(body.get("limit", 10))
    workers = int(body.get("workers", 8))
    inject = body.get("inject")

    reg = registry()
    if inject:
        from faults import inject as inject_fault
        reg = inject_fault(reg, inject)

    tracer = Tracer(verbose=False)
    gw = Gateway(reg, tracer)
    cust = {c["customer_id"]: c for c in estate()["customers"]}

    exceptions = estate()["exceptions"]
    planted = [e for e in exceptions if e.get("planted")]
    rest = [e for e in exceptions if not e.get("planted")]
    cases = planted + rest[: max(0, limit - len(planted))]

    results = gw.run_batch(cases, cust, workers=workers)

    tally: dict[str, int] = {}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1

    return JSONResponse({
        "mode": "stub" if STUB else "real",
        "tally": tally,
        "cases": [
            {"exception_id": r.exception_id, "outcome": r.outcome,
             "reason": r.reasons[0] if r.reasons else None,
             "confidence": r.resolution.confidence if r.resolution else None,
             "action": r.resolution.action if r.resolution else None}
            for r in results
        ],
        "spans": [
            {"name": s.name, "case": s.case_id, "ms": round(s.ms, 1),
             "attrs": {k: str(v) for k, v in s.attrs.items()},
             "error": s.error}
            for s in tracer.spans
        ],
        "agents": reg.catalog(),
    })


@app.post("/pubsub")
async def pubsub(request: Request):
    """Push endpoint. One exception per message — this is the async path that
    makes the fleet event-driven rather than a loop over a list."""
    envelope = await request.json()
    msg = envelope.get("message", {})
    try:
        payload = json.loads(base64.b64decode(msg.get("data", "")).decode())
    except Exception:
        return JSONResponse({"error": "bad message"}, status_code=204)  # don't retry

    exc_id = payload.get("exception_id")
    exc = next((e for e in estate()["exceptions"]
                if e["exception_id"] == exc_id), None)
    if exc is None:
        return JSONResponse({"error": f"unknown {exc_id}"}, status_code=204)

    cust = {c["customer_id"]: c for c in estate()["customers"]}
    gw = Gateway(registry(), Tracer(verbose=True))
    r = gw.handle(exc, cust.get(exc["counterparty_id"], {}))
    print(f"[pubsub] {r.exception_id} -> {r.outcome}: "
          f"{r.reasons[0] if r.reasons else ''}", flush=True)
    return JSONResponse({"exception_id": r.exception_id, "outcome": r.outcome})


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>ExceptionZero</title><style>
:root{--paper:#F4F7F1;--ledger:#E6EDE2;--rule:#C2CEBB;--ink:#171F1A;
--faint:#6E7A6E;--stop:#9E3521;--clear:#2C6144;--hold:#7A5B12}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.5 ui-monospace,"SF Mono",Menlo,monospace;padding:1.5rem}
h1{font:700 1.5rem/1 system-ui;margin:0 0 .3rem;letter-spacing:-.02em}
.sub{color:var(--faint);font-size:.8rem;margin:0 0 1.2rem}
button{font:600 .72rem/1 ui-monospace,monospace;letter-spacing:.06em;
text-transform:uppercase;background:var(--paper);border:1.5px solid var(--ink);
color:var(--ink);padding:.5rem .8rem;border-radius:3px;cursor:pointer;margin-right:.4rem}
button:hover{background:var(--ink);color:var(--paper)}
button:disabled{opacity:.4;cursor:wait}
select{font:.72rem ui-monospace,monospace;padding:.45rem;border:1px solid var(--rule);
border-radius:3px;background:var(--paper);margin-right:.4rem}
#tally{margin:1.2rem 0;font-size:.85rem}
.pill{display:inline-block;padding:.2rem .55rem;border:1.5px solid;border-radius:3px;
margin-right:.5rem;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase}
.resolved{color:var(--clear)}.deferred{color:var(--hold)}
.quarantined,.failed{color:var(--stop)}
table{width:100%;border-collapse:collapse;font-size:.78rem;margin-top:.6rem}
th{text-align:left;color:var(--faint);font-weight:400;text-transform:uppercase;
letter-spacing:.08em;font-size:.65rem;padding:.4rem .5rem;border-bottom:1px solid var(--rule)}
td{padding:.4rem .5rem;border-bottom:1px solid #D6DFD1;vertical-align:top}
tr:nth-child(even){background:var(--ledger)}
.err{color:var(--stop)}
#agents{margin-top:1.5rem;font-size:.75rem;color:var(--faint)}
.none{color:var(--stop);font-weight:600}
</style></head><body>
<h1>ExceptionZero</h1>
<p class=sub>Six agents · least-privilege identity · deterministic risk gate · <span id=mode>—</span></p>
<div>
<select id=limit><option value=10>10 cases</option><option value=20 selected>20</option>
<option value=50>50</option><option value=339>all 339</option></select>
<select id=inject><option value="">no fault</option><option value=hallucination>hallucination</option>
<option value=phantom_key>phantom key</option><option value=loop>loop</option>
<option value=verify_fail>verification failure</option><option value=overconfident>overconfident</option></select>
<button id=go>Run fleet</button></div>
<div id=tally></div><div id=out></div><div id=agents></div>
<script>
const $=s=>document.querySelector(s);
fetch('/status').then(r=>r.json()).then(h=>{$('#mode').textContent=h.mode+' · '+h.model+' · '+h.exceptions+' exceptions from BigQuery'});
$('#go').onclick=async()=>{
  const b=$('#go');b.disabled=true;b.textContent='running…';
  $('#tally').innerHTML='';$('#out').innerHTML='';
  const t0=performance.now();
  const r=await fetch('/run',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({limit:+$('#limit').value,workers:8,inject:$('#inject').value||null})});
  const d=await r.json();
  const secs=((performance.now()-t0)/1000).toFixed(1);
  $('#tally').innerHTML=Object.entries(d.tally).map(([k,v])=>
    `<span class="pill ${k}">${k} ${v}</span>`).join('')+
    `<span style="color:var(--faint)">${d.cases.length} cases · ${secs}s · ${d.spans.length} spans</span>`;
  $('#out').innerHTML='<table><tr><th>case</th><th>outcome</th><th>conf</th><th>reason</th></tr>'+
    d.cases.map(c=>`<tr><td>${c.exception_id}</td><td class="${c.outcome}">${c.outcome}</td>
    <td>${c.confidence!=null?c.confidence.toFixed(2):'—'}</td><td>${c.reason||''}</td></tr>`).join('')+'</table>';
  const errs=d.spans.filter(s=>s.error);
  if(errs.length)$('#out').innerHTML+='<p class=err>guards fired: '+
    errs.map(s=>s.case+' — '+s.error.slice(0,110)).join('<br>')+'</p>';
  $('#agents').innerHTML='<table><tr><th>agent</th><th>service account</th><th>tool scope</th></tr>'+
    d.agents.map(a=>`<tr><td>${a.name}</td><td>${a.service_account}</td>
    <td>${a.tool_scope.length?a.tool_scope.join(', '):'<span class=none>NONE</span>'}</td></tr>`).join('')+'</table>';
  b.disabled=false;b.textContent='Run fleet';
};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE

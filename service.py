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
    # Park deferrals in Firestore so the sweeper has something to re-examine.
    # A deferred case is a state, not an ending — without the store, every
    # escalation would be a dead end.
    from sweeper import DeferredStore
    gw = Gateway(reg, tracer, store=DeferredStore(project=PROJECT))
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
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ExceptionZero</title><style>
:root{--paper:#F4F7F1;--ledger:#E6EDE2;--rule:#C2CEBB;--hair:#D6DFD1;--ink:#171F1A;
--faint:#6E7A6E;--soft:#485349;--stop:#9E3521;--clear:#2C6144;--hold:#7A5B12}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);padding:1.6rem 1.4rem 4rem;
font:15px/1.55 ui-monospace,"SF Mono",Menlo,monospace;max-width:76rem;margin:0 auto}
h1{font:800 1.7rem/1 system-ui,sans-serif;margin:0 0 .3rem;letter-spacing:-.03em}
.lede{font:400 .95rem/1.5 system-ui,sans-serif;color:var(--soft);margin:0 0 .3rem;max-width:52rem}
.who{font:400 .85rem/1.5 system-ui,sans-serif;color:var(--faint);margin:0 0 1.2rem;max-width:52rem}
nav{display:flex;gap:.3rem;border-bottom:1px solid var(--rule);margin-bottom:1.2rem;flex-wrap:wrap}
nav button{font:600 .7rem/1 ui-monospace,monospace;letter-spacing:.07em;text-transform:uppercase;
background:none;border:none;border-bottom:2px solid transparent;color:var(--faint);
padding:.6rem .7rem;cursor:pointer}
nav button.on{color:var(--ink);border-bottom-color:var(--ink)}
nav button:hover{color:var(--ink)}
.ctl{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;margin-bottom:1rem}
button.go{font:600 .72rem/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;
background:var(--paper);border:1.5px solid var(--ink);color:var(--ink);padding:.55rem .9rem;
border-radius:3px;cursor:pointer}
button.go:hover{background:var(--ink);color:var(--paper)}
button.go:disabled{opacity:.4;cursor:wait}
select{font:.72rem ui-monospace,monospace;padding:.5rem;border:1px solid var(--rule);
border-radius:3px;background:var(--paper)}
label{font:.65rem ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.pill{display:inline-block;padding:.22rem .6rem;border:1.5px solid;border-radius:3px;
margin-right:.5rem;font-size:.7rem;letter-spacing:.06em;text-transform:uppercase}
.resolved{color:var(--clear)}.deferred{color:var(--hold)}
.quarantined,.failed{color:var(--stop)}
table{width:100%;border-collapse:collapse;font-size:.78rem}
th{text-align:left;color:var(--faint);font-weight:400;text-transform:uppercase;letter-spacing:.08em;
font-size:.63rem;padding:.45rem .5rem;border-bottom:1px solid var(--rule)}
td{padding:.45rem .5rem;border-bottom:1px solid var(--hair);vertical-align:top}
tr.case{cursor:pointer}
tr.case:hover td{background:var(--ledger)}
tr.planted td:first-child::after{content:" ★";color:var(--stop)}
.detail td{background:var(--ledger);font-size:.76rem;line-height:1.55}
.detail b{font:600 .63rem/1 ui-monospace,monospace;letter-spacing:.08em;text-transform:uppercase;
color:var(--faint);display:block;margin:.5rem 0 .15rem}
.none{color:var(--stop);font-weight:600}
.note{font:400 .85rem/1.55 system-ui,sans-serif;color:var(--soft);margin:.2rem 0 1rem;max-width:56rem}
.key{font-size:.72rem;color:var(--faint);margin-top:.8rem}
#err{color:var(--stop);font-size:.78rem;margin-top:.8rem;white-space:pre-wrap}
.hide{display:none}
</style></head><body>
<h1>ExceptionZero</h1>
<p class=lede>A fleet of agents that resolves the workflow exceptions a person currently works by hand
&mdash; and stops when it shouldn't touch one. <span id=mode style="color:var(--faint)">&mdash;</span></p>
<p class=who>Built for the receiving-and-office clerk at a 40-person distributor: not an engineer,
not in finance, doing this on top of four other jobs. Every escalation carries the reason it could not
be resolved, so the human starts from the fleet's work rather than from scratch.</p>

<nav>
  <button class=on data-t=fleet>Run the fleet</button>
  <button data-t=identity>Agent identity</button>
  <button data-t=registry>Registry</button>
  <button data-t=queue>Human queue</button>
</nav>

<div id=fleet>
  <p class=note>Each case is triaged, investigated by specialists chosen for its type, diagnosed by an
  agent that holds no tools, then passed to a deterministic risk gate. Click any row for the reasoning.
  Inject a fault to watch the guardrails contain it.</p>
  <div class=ctl>
    <label>cases</label>
    <select id=limit><option>10</option><option selected>20</option><option>50</option></select>
    <label>fault</label>
    <select id=inject><option value="">none</option><option value=hallucination>hallucinated citation</option>
    <option value=phantom_key>phantom entity</option><option value=loop>infinite loop</option>
    <option value=verify_fail>verification failure</option><option value=overconfident>forced overconfidence</option></select>
    <button class=go id=go>Run fleet</button>
  </div>
  <div id=tally></div><div id=out></div>
  <p class=key>&#9733; marks a deliberately engineered case: a refusal, a prompt injection, and a
  reference to a record that does not exist.</p>
</div>

<div id=identity class=hide>
  <p class=note>Every agent runs under its own Google Cloud service account through impersonation.
  This page has each one attempt a real BigQuery read under its own identity &mdash; specialists reach
  their own table and no other, and the diagnosis agent is refused. That is IAM, not a prompt.</p>
  <div id=identity_out></div>
</div>

<div id=registry class=hide>
  <p class=note>The published agent catalog, persisted in Firestore. Capability, version, identity and
  tool scope &mdash; what another team discovers when looking for an approved agent.</p>
  <div id=registry_out></div>
</div>

<div id=queue class=hide>
  <p class=note>Escalated cases, highest value first, each with the reason it could not be resolved and
  what would have to change to clear it. A scheduled sweeper re-examines these and re-opens the ones
  whose blocker has cleared &mdash; sometimes weeks later.</p>
  <div id=queue_out></div>
</div>
<div id=err></div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fmt=(v,c)=>v==null?'&mdash;':(c||'')+' '+Number(v).toLocaleString(undefined,{maximumFractionDigits:2});

fetch('/status').then(r=>r.json()).then(h=>{
  $('#mode').textContent=`${h.mode} · ${h.model} · ${h.exceptions} exceptions from BigQuery`;
}).catch(()=>{});

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  ['fleet','identity','registry','queue'].forEach(t=>$('#'+t).classList.toggle('hide',t!==b.dataset.t));
  if(b.dataset.t!=='fleet') load(b.dataset.t);
});

async function load(t){
  const el=$('#'+t+'_out'); el.innerHTML='<p style="color:var(--faint)">loading…</p>';
  try{
    const d=await (await fetch('/'+t)).json();
    if(t==='identity'){
      el.innerHTML='<table><tr><th>agent</th><th>identity</th><th>own table</th><th>access</th>'+
        '<th>other tables reachable</th><th>service account</th></tr>'+
        d.agents.map(a=>`<tr><td>${esc(a.capability)}</td><td>${esc(a.identity)}</td>
        <td>${esc(a.own_table)}</td><td class="${a.bigquery==='ALLOWED'?'resolved':'quarantined'}">
        <b>${esc(a.bigquery)}</b></td><td>${esc(Array.isArray(a.other_tables_reachable)?
        a.other_tables_reachable.join(', '):a.other_tables_reachable)}</td>
        <td style="color:var(--faint)">${esc(a.service_account)}</td></tr>`).join('')+'</table>'+
        `<p class=key>Claim: <em>${esc(d.claim)}</em> &mdash; verdict <b>${esc(d.verdict)}</b>.</p>`;
    } else if(t==='registry'){
      el.innerHTML='<table><tr><th>agent</th><th>capability</th><th>version</th><th>domain</th>'+
        '<th>tool scope</th><th>identity</th></tr>'+
        d.agents.map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.capability)}</td>
        <td>${esc(a.version)}</td><td>${esc(a.domain||'—')}</td>
        <td>${(a.tool_scope&&a.tool_scope.length)?esc(a.tool_scope.join(', ')):'<span class=none>NONE</span>'}</td>
        <td style="color:var(--faint)">${esc(a.service_account)}</td></tr>`).join('')+'</table>';
    } else {
      el.innerHTML=`<p><b>${d.awaiting_review}</b> awaiting review</p><table>
        <tr><th>case</th><th>type</th><th>value</th><th>why it stopped</th><th>what would clear it</th></tr>`+
        d.cases.map(c=>`<tr><td>${esc(c.exception_id)}</td><td>${esc(c.type)}</td>
        <td>${fmt(c.value,c.currency)}</td><td>${esc(c.reason)}</td>
        <td style="color:var(--faint)">${esc(c.what_would_clear_it)}</td></tr>`).join('')+'</table>';
    }
  }catch(e){ el.innerHTML='<p class=none>could not load</p>'; }
}

$('#go').onclick=async()=>{
  const b=$('#go'); b.disabled=true; b.textContent='running…';
  $('#tally').innerHTML=''; $('#out').innerHTML=''; $('#err').textContent='';
  const t0=performance.now();
  try{
    const r=await fetch('/run',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({limit:+$('#limit').value,workers:8,inject:$('#inject').value||null})});
    const d=await r.json();
    const secs=((performance.now()-t0)/1000).toFixed(1);
    $('#tally').innerHTML=Object.entries(d.tally).map(([k,v])=>
      `<span class="pill ${k}">${k} ${v}</span>`).join('')+
      `<span style="color:var(--faint);font-size:.75rem">${d.cases.length} cases · ${secs}s · ${d.spans.length} spans</span>`;
    const rows=d.cases.map((c,i)=>{
      const planted=/EXC-7990/.test(c.exception_id)?' planted':'';
      const spans=d.spans.filter(s=>s.case===c.exception_id);
      const guards=spans.filter(s=>s.error);
      const chosen=spans.filter(s=>s.name.startsWith('specialist.')).map(s=>s.name.split('.')[1]);
      const adaptive=spans.some(s=>s.name==='context.adaptive');
      return `<tr class="case${planted}" data-i="${i}"><td>${esc(c.exception_id)}</td>
        <td class="${c.outcome}"><b>${esc(c.outcome)}</b></td>
        <td>${c.confidence!=null?c.confidence.toFixed(2):'&mdash;'}</td>
        <td>${esc(c.reason||'')}</td></tr>
        <tr class="detail hide" id="d${i}"><td colspan=4>
          <b>proposed action</b>${esc(c.action||'—')}
          <b>specialists dispatched</b>${chosen.length?esc(chosen.join(', ')):'—'}${adaptive?
            ' <span style="color:var(--stop)">+ adaptive re-dispatch: the agent asked for more evidence</span>':''}
          <b>agent hops</b>${spans.map(s=>esc(s.name)+' '+s.ms+'ms').join(' → ')||'—'}
          ${guards.length?'<b>guard fired</b><span class=none>'+guards.map(g=>esc(g.error)).join('<br>')+'</span>':''}
        </td></tr>`;}).join('');
    $('#out').innerHTML='<table><tr><th>case</th><th>outcome</th><th>conf</th><th>reason</th></tr>'+rows+'</table>';
    document.querySelectorAll('tr.case').forEach(tr=>tr.onclick=()=>
      $('#d'+tr.dataset.i).classList.toggle('hide'));
  }catch(e){ $('#err').textContent='Run failed: '+e; }
  b.disabled=false; b.textContent='Run fleet';
};
</script></body></html>"""


@app.get("/queue")
def review_queue():
    """The human review queue.

    "Escalates to a human" is only true if there is somewhere for the human to
    look. Every deferred case lands here with the reason it could not be
    resolved, what would have to change to clear it, and how long it has been
    waiting — so the reviewer starts from the fleet's work, not from scratch.
    """
    from sweeper import BLOCKERS, DeferredStore
    store = DeferredStore(project=PROJECT)
    cases = []
    try:
        rows = (list(store._load_local().values()) if not store.firestore
                else [d.to_dict() for d in
                      store._db().collection("deferred_cases").stream()])
    except Exception:
        rows = []
    for r in rows:
        if r.get("resolved_at"):
            continue
        cases.append({
            "exception_id": r.get("exception_id"),
            "type": r.get("exception_type"),
            "value": r.get("amount"),
            "currency": r.get("currency"),
            "reason": r.get("reason"),
            "blocker": r.get("blocker"),
            "what_would_clear_it": BLOCKERS.get(r.get("blocker", ""), "human judgement"),
            "deferred_at": r.get("deferred_at"),
            "times_reexamined": r.get("sweeps", 0),
        })
    cases.sort(key=lambda c: (c.get("value") or 0), reverse=True)
    return {"awaiting_review": len(cases), "cases": cases}


@app.post("/sweep")
async def sweep_endpoint(request: Request):
    """Cloud Scheduler target. Re-examines deferred cases and re-opens the
    ones whose blocker has cleared.

    Nothing here is request-response: a case parked today may close in three
    weeks. The sweeper adds no authority — a re-opened case goes back through
    the same agents, the same citation guard, the same deterministic gate.
    """
    body = {}
    try:
        if await request.body():
            body = await request.json()
    except Exception:
        pass

    from sweeper import DeferredStore, simulate_arriving_evidence, sweep
    store = DeferredStore(project=PROJECT)
    est = estate()
    if body.get("simulate_evidence"):
        simulate_arriving_evidence(store, est)

    gw = Gateway(registry(), Tracer(verbose=True), store=store)
    out = sweep(store, est, gateway=gw,
                min_age_days=float(body.get("min_age_days", 0)), verbose=True)
    print(f"[sweep] examined={out['examined']} reopened={len(out['reopened'])} "
          f"resolved_late={len(out['resolved_late'])}", flush=True)
    return {**out, "store": store.stats()}


@app.get("/identity")
def identity_proof():
    """Prove the identity model rather than describing it.

    Each agent attempts a BigQuery read under its OWN service account.
    Specialists reach their own table; diagnosis is refused, because
    ez-diagnosis holds aiplatform.user and cloudtrace.agent and nothing else.
    """
    from identity import ENABLED, identity_report
    rows = identity_report()
    return {
        "impersonation_enabled": ENABLED,
        "claim": "the diagnosis agent cannot read the data estate",
        "verdict": next((r["bigquery"] for r in rows
                         if r["capability"] == "diagnosis"), "unknown"),
        "agents": rows,
    }


@app.get("/registry")
def registry_catalog():
    """The published agent catalog — what another department discovers."""
    return {"agents": registry().published()}


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE

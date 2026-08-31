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


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ExceptionZero</title>
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;800&family=Newsreader:opsz,wght@6..72,300;6..72,400&family=JetBrains+Mono:wght@400;600&display=swap" rel=stylesheet>
<style>
:root{
  --paper:#F5F7F2; --ledger:#E9EFE5; --raised:#FCFDFB; --rule:#C6D1BE;
  --hair:#DEE6D8; --ink:#141C16; --soft:#44503F; --faint:#77836F;
  --stop:#9B3520; --clear:#2A6142; --hold:#7B5A0E; --accent:#2A6142;
}
*{box-sizing:border-box}
html{-webkit-font-smoothing:antialiased}
body{margin:0;background:var(--paper);color:var(--ink);
  font:400 15px/1.6 "Newsreader",Georgia,serif}
.wrap{max-width:78rem;margin:0 auto;padding:0 1.6rem}
code,.mono{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}

/* ---- masthead ---- */
header{padding:2.6rem 0 1.6rem;border-bottom:1px solid var(--rule)}
.brandrow{display:flex;align-items:baseline;gap:.9rem;flex-wrap:wrap}
h1{font:800 2.1rem/1 "Archivo",system-ui,sans-serif;letter-spacing:-.035em;margin:0}
.env{font-family:"JetBrains Mono",monospace;font-size:.66rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);border:1px solid var(--rule);
  border-radius:100px;padding:.3rem .7rem;white-space:nowrap}
.lede{font-size:1.14rem;line-height:1.5;color:var(--ink);margin:.9rem 0 0;max-width:44rem}
.who{font-size:.94rem;color:var(--soft);margin:.6rem 0 0;max-width:44rem}

/* ---- nav ---- */
nav{display:flex;gap:0;border-bottom:1px solid var(--rule);position:sticky;top:0;
  background:rgba(245,247,242,.96);backdrop-filter:blur(8px);z-index:10;flex-wrap:wrap}
nav button{font:600 .68rem/1 "JetBrains Mono",monospace;letter-spacing:.11em;
  text-transform:uppercase;background:none;border:0;border-bottom:2px solid transparent;
  color:var(--faint);padding:1rem .95rem;cursor:pointer;transition:color .12s}
nav button.on{color:var(--ink);border-bottom-color:var(--accent)}
nav button:hover{color:var(--ink)}
nav button:focus-visible{outline:2px solid var(--accent);outline-offset:-4px}

main{padding:1.8rem 0 5rem}
.note{font-size:1rem;line-height:1.6;color:var(--soft);margin:0 0 1.5rem;max-width:50rem}
.note em{font-style:normal;color:var(--ink)}

/* ---- controls ---- */
.ctl{display:flex;gap:.55rem;align-items:center;flex-wrap:wrap;margin-bottom:1.4rem;
  padding:.9rem 1rem;background:var(--raised);border:1px solid var(--rule);border-radius:6px}
label{font:600 .62rem/1 "JetBrains Mono",monospace;letter-spacing:.11em;
  text-transform:uppercase;color:var(--faint)}
select{font:.75rem/1 "JetBrains Mono",monospace;padding:.55rem .6rem;border:1px solid var(--rule);
  border-radius:4px;background:var(--paper);color:var(--ink)}
button.go{font:600 .7rem/1 "JetBrains Mono",monospace;letter-spacing:.09em;text-transform:uppercase;
  background:var(--ink);border:1.5px solid var(--ink);color:var(--paper);
  padding:.62rem 1.1rem;border-radius:4px;cursor:pointer;transition:opacity .12s}
button.go:hover{opacity:.85}
button.go:disabled{opacity:.35;cursor:wait}

/* ---- tally ---- */
#tally{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:1rem}
.stat{border:1.5px solid;border-radius:5px;padding:.5rem .85rem;min-width:6.4rem}
.stat .n{font:800 1.35rem/1 "Archivo",sans-serif;display:block}
.stat .l{font:600 .58rem/1 "JetBrains Mono",monospace;letter-spacing:.12em;
  text-transform:uppercase;opacity:.8}
.resolved{color:var(--clear);border-color:var(--clear)}
.deferred{color:var(--hold);border-color:var(--hold)}
.quarantined,.failed{color:var(--stop);border-color:var(--stop)}
.meta{font-family:"JetBrains Mono",monospace;font-size:.72rem;color:var(--faint)}

/* ---- tables ---- */
.card{background:var(--raised);border:1px solid var(--rule);border-radius:6px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{text-align:left;font:600 .6rem/1 "JetBrains Mono",monospace;letter-spacing:.11em;
  text-transform:uppercase;color:var(--faint);padding:.7rem .85rem;
  border-bottom:1px solid var(--rule);background:var(--ledger)}
td{padding:.62rem .85rem;border-bottom:1px solid var(--hair);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tr.case{cursor:pointer;transition:background .1s}
tr.case:hover>td{background:var(--ledger)}
tr.case td:first-child{font-family:"JetBrains Mono",monospace;font-size:.76rem;white-space:nowrap}
.oc{font:600 .68rem/1 "JetBrains Mono",monospace;letter-spacing:.07em;text-transform:uppercase}
.star{color:var(--stop);font-size:.8em}
.num{font-family:"JetBrains Mono",monospace;font-size:.76rem;white-space:nowrap}
.sa{font-family:"JetBrains Mono",monospace;font-size:.68rem;color:var(--faint);word-break:break-all}
.detail>td{background:var(--ledger);padding:.9rem 1.1rem 1.1rem}
.detail b{font:600 .58rem/1 "JetBrains Mono",monospace;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint);display:block;margin:.75rem 0 .2rem}
.detail b:first-child{margin-top:0}
.hops{font-family:"JetBrains Mono",monospace;font-size:.72rem;color:var(--soft);
  line-height:1.7;word-break:break-word}
.adaptive{color:var(--stop);font-weight:600}
.guard{color:var(--stop);font-family:"JetBrains Mono",monospace;font-size:.72rem;line-height:1.6}
.none{color:var(--stop);font-weight:600;font-family:"JetBrains Mono",monospace;font-size:.72rem}
.ok{color:var(--clear);font-weight:600}
.key{font-size:.86rem;color:var(--faint);margin-top:1rem}
.claim{margin-top:1.1rem;padding:.85rem 1rem;border-left:3px solid var(--accent);
  background:var(--raised);font-size:.95rem}
#err{color:var(--stop);font-family:"JetBrains Mono",monospace;font-size:.78rem;margin-top:1rem}
.hide{display:none}
.spin{display:inline-block;color:var(--faint);font-size:.85rem}
@media(max-width:720px){h1{font-size:1.6rem}.wrap{padding:0 1rem}table{font-size:.74rem}}
</style></head><body>

<header><div class=wrap>
  <div class=brandrow>
    <h1>ExceptionZero</h1>
    <span class=env id=env>connecting…</span>
  </div>
  <p class=lede>A fleet of agents that resolves the workflow exceptions a person currently
  works by hand &mdash; and stops when it shouldn&rsquo;t touch one.</p>
  <p class=who>Built for the receiving-and-office clerk at a 40-person distributor: not an
  engineer, not in finance, doing this on top of four other jobs. Every escalation carries the
  reason it could not be resolved, so the human starts from the fleet&rsquo;s work rather than
  from scratch.</p>
</div></header>

<nav><div class=wrap style="display:flex;gap:0">
  <button class=on data-t=inbox>The inbox</button>
  <button data-t=fleet>Run the fleet</button>
  <button data-t=identity>Agent identity</button>
  <button data-t=registry>Registry</button>
  <button data-t=queue>Human queue</button>
</div></nav>

<main class=wrap>
<section id=inbox>
  <p class=note>This is the queue as it arrives &mdash; before any agent has looked at it.
  A bank return code, an amount, and a memo somebody typed. Working out what each one means,
  one at a time, is the job.</p>
  <div id=inbox_out></div>
</section>

<section id=fleet class=hide>
  <p class=note>Each case is triaged, investigated by specialists <em>chosen for its type</em>,
  diagnosed by an agent that holds no tools, then passed to a deterministic risk gate.
  Click any row for the reasoning. Inject a fault to watch the guardrails contain it.</p>
  <div class=ctl>
    <label for=limit>cases</label>
    <select id=limit><option>10</option><option selected>20</option><option>50</option></select>
    <label for=inject>inject fault</label>
    <select id=inject><option value="">none</option>
      <option value=hallucination>hallucinated citation</option>
      <option value=phantom_key>phantom entity</option>
      <option value=loop>infinite loop</option>
      <option value=verify_fail>verification failure</option>
      <option value=overconfident>forced overconfidence</option></select>
    <button class=go id=go>Run fleet</button>
  </div>
  <div id=tally></div><div id=out></div>
  <p class=key><span class=star>&#9733;</span> marks a deliberately engineered case: a refusal,
  a prompt injection, and a reference to a record that does not exist.</p>
</section>

<section id=identity class=hide>
  <p class=note>Every agent runs under its own Google Cloud service account through impersonation.
  This page has each one attempt a real BigQuery read <em>under its own identity</em> &mdash;
  specialists reach their own table and no other, and the diagnosis agent is refused.
  That is IAM, not a prompt.</p>
  <div id=identity_out></div>
</section>

<section id=registry class=hide>
  <p class=note>The published agent catalog, persisted in Firestore. Capability, version, identity
  and tool scope &mdash; what another team discovers when looking for an approved agent.</p>
  <div id=registry_out></div>
</section>

<section id=queue class=hide>
  <p class=note>Escalated cases, highest value first, each with the reason it could not be resolved
  and what would have to change to clear it. A scheduled sweeper re-examines these and re-opens the
  ones whose blocker has cleared &mdash; sometimes weeks later.</p>
  <div id=queue_out></div>
</section>
<div id=err></div>
</main>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const num=(v,c)=>v==null?'&mdash;':(c?c+' ':'')+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const card=h=>'<div class=card>'+h+'</div>';

fetch('/status').then(r=>r.json()).then(h=>{
  $('#env').textContent=`${h.mode} · ${h.model} · ${h.exceptions} exceptions from BigQuery`;
}).catch(()=>{$('#env').textContent='offline';});

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  ['inbox','fleet','identity','registry','queue'].forEach(t=>$('#'+t).classList.toggle('hide',t!==b.dataset.t));
  if(b.dataset.t!=='fleet') load(b.dataset.t);
});

async function load(t){
  const el=$('#'+t+'_out'); el.innerHTML='<p class=spin>loading…</p>';
  try{
    const d=await (await fetch('/'+t)).json();
    if(t==='inbox'){
      el.innerHTML=`<div id=tally><div class="stat deferred"><span class=n>${d.open_exceptions}</span><span class=l>open exceptions</span></div></div>`+
        card('<table><thead><tr><th>case</th><th>type</th><th>code</th><th>payer</th>'+
        '<th>invoice ref</th><th>amount</th><th>memo</th></tr></thead><tbody>'+
        d.cases.map(c=>`<tr><td class=mono>${esc(c.exception_id)}</td>
        <td class=mono style="font-size:.72rem">${esc(c.type)}</td>
        <td class=mono style="font-size:.72rem">${esc(c.code||'—')}</td>
        <td>${esc(c.counterparty||'—')}</td>
        <td class=mono style="font-size:.72rem">${esc(c.invoice_ref||'<span style="color:var(--stop)">none</span>')}</td>
        <td class=num>${num(c.amount,c.currency)}</td>
        <td style="color:var(--soft)">${esc(c.memo||'')}</td></tr>`).join('')+'</tbody></table>');
    } else if(t==='identity'){
      el.innerHTML=card('<table><thead><tr><th>agent</th><th>identity</th><th>own table</th>'+
        '<th>access</th><th>other tables reachable</th><th>service account</th></tr></thead><tbody>'+
        d.agents.map(a=>{const ok=a.bigquery==='ALLOWED';
          const other=Array.isArray(a.other_tables_reachable)?a.other_tables_reachable.join(', '):a.other_tables_reachable;
          return `<tr><td class=mono>${esc(a.capability)}</td><td class=mono style="font-size:.7rem">${esc(a.identity)}</td>
          <td class=mono style="font-size:.72rem">${esc(a.own_table)}</td>
          <td class="oc ${ok?'ok':'none'}">${esc(a.bigquery)}</td>
          <td class=mono style="font-size:.72rem;color:var(--faint)">${esc(other)}</td>
          <td class=sa>${esc(a.service_account)}</td></tr>`}).join('')+'</tbody></table>')+
        `<div class=claim>Claim: <em>${esc(d.claim)}</em> &mdash; verdict <strong class="${d.verdict==='DENIED'?'none':'ok'}">${esc(d.verdict)}</strong></div>`;
    } else if(t==='registry'){
      el.innerHTML=card('<table><thead><tr><th>agent</th><th>capability</th><th>version</th>'+
        '<th>domain</th><th>tool scope</th><th>identity</th></tr></thead><tbody>'+
        d.agents.map(a=>`<tr><td>${esc(a.name)}</td><td class=mono style="font-size:.74rem">${esc(a.capability)}</td>
        <td class=num>${esc(a.version)}</td><td class=mono style="font-size:.74rem">${esc(a.domain||'—')}</td>
        <td class=mono style="font-size:.72rem">${(a.tool_scope&&a.tool_scope.length)?esc(a.tool_scope.join(', ')):'<span class=none>NONE</span>'}</td>
        <td class=sa>${esc(a.service_account)}</td></tr>`).join('')+'</tbody></table>');
    } else {
      el.innerHTML=`<div id=tally><div class="stat deferred"><span class=n>${d.awaiting_review}</span><span class=l>awaiting review</span></div></div>`+
        card('<table><thead><tr><th>case</th><th>type</th><th>value</th><th>why it stopped</th>'+
        '<th>what would clear it</th></tr></thead><tbody>'+
        d.cases.map(c=>`<tr><td class=mono>${esc(c.exception_id)}</td>
        <td class=mono style="font-size:.72rem">${esc(c.type)}</td>
        <td class=num>${num(c.value,c.currency)}</td><td>${esc(c.reason)}</td>
        <td style="color:var(--faint)">${esc(c.what_would_clear_it)}</td></tr>`).join('')+'</tbody></table>');
    }
  }catch(e){ el.innerHTML='<p class=none>could not load</p>'; }
}

load('inbox');

$('#go').onclick=async()=>{
  const b=$('#go'); b.disabled=true; b.textContent='running…';
  $('#tally').innerHTML=''; $('#out').innerHTML='<p class=spin>the fleet is working…</p>';
  $('#err').textContent='';
  const t0=performance.now();
  try{
    const r=await fetch('/run',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({limit:+$('#limit').value,workers:8,inject:$('#inject').value||null})});
    const d=await r.json();
    const secs=((performance.now()-t0)/1000).toFixed(1);
    const order=['resolved','deferred','quarantined','failed'];
    $('#tally').innerHTML=order.filter(k=>d.tally[k]).map(k=>
      `<div class="stat ${k}"><span class=n>${d.tally[k]}</span><span class=l>${k}</span></div>`).join('')+
      `<span class=meta>${d.cases.length} cases · ${secs}s · ${d.spans.length} spans</span>`;
    const rows=d.cases.map((c,i)=>{
      const planted=/EXC-7990/.test(c.exception_id);
      const spans=d.spans.filter(s=>s.case===c.exception_id);
      const guards=spans.filter(s=>s.error);
      const chosen=spans.filter(s=>s.name.startsWith('specialist.')).map(s=>s.name.split('.')[1]);
      const adaptive=spans.some(s=>s.name==='context.adaptive');
      return `<tr class=case data-i="${i}">
        <td>${esc(c.exception_id)}${planted?' <span class=star>&#9733;</span>':''}</td>
        <td class="oc ${c.outcome}">${esc(c.outcome)}</td>
        <td class=num>${c.confidence!=null?c.confidence.toFixed(2):'&mdash;'}</td>
        <td>${esc(c.reason||'')}</td></tr>
      <tr class="detail hide" id="d${i}"><td colspan=4>
        <b>proposed action</b><span class=mono style="font-size:.76rem">${esc(c.action||'—')}</span>
        <b>specialists dispatched</b><span class=mono style="font-size:.76rem">${chosen.length?esc([...new Set(chosen)].join(', ')):'—'}</span>${
          adaptive?' <span class=adaptive>&mdash; then asked for more evidence and was re-dispatched</span>':''}
        <b>agent hops</b><div class=hops>${spans.map(s=>esc(s.name)+' <span style="color:var(--faint)">'+s.ms+'ms</span>').join(' &rarr; ')||'—'}</div>
        ${guards.length?'<b>guard fired</b><div class=guard>'+guards.map(g=>esc(g.error)).join('<br>')+'</div>':''}
      </td></tr>`;}).join('');
    $('#out').innerHTML=card('<table><thead><tr><th>case</th><th>outcome</th><th>conf</th>'+
      '<th>reason</th></tr></thead><tbody>'+rows+'</tbody></table>');
    document.querySelectorAll('tr.case').forEach(tr=>tr.onclick=()=>
      $('#d'+tr.dataset.i).classList.toggle('hide'));
  }catch(e){ $('#out').innerHTML=''; $('#err').textContent='Run failed: '+e; }
  b.disabled=false; b.textContent='Run fleet';
};
</script></body></html>"""


@app.get("/inbox")
def inbox(limit: int = 40):
    """The raw exception queue — what a person opens on Monday morning.

    No agent has looked at these. This is the estate as it arrives: a bank
    return code, an amount, and a memo somebody typed. Working out what each
    one means is the job.
    """
    rows = [e for e in estate().get("exceptions", []) if e.get("status") == "open"]
    rows.sort(key=lambda e: e.get("received_at") or "", reverse=True)
    return {
        "open_exceptions": len(rows),
        "cases": [{
            "exception_id": e.get("exception_id"),
            "received_at": e.get("received_at"),
            "type": e.get("exception_type"),
            "code": e.get("bank_return_code"),
            "counterparty": e.get("counterparty_name_on_payment"),
            "invoice_ref": e.get("invoice_ref"),
            "amount": e.get("amount"),
            "currency": e.get("currency"),
            "memo": e.get("memo"),
        } for e in rows[:limit]],
    }


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

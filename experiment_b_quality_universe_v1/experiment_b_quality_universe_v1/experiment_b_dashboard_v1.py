
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import csv, json, math
from collections import Counter
from datetime import datetime

BASE = Path(__file__).resolve().parent
PORT = 8788
CSV = BASE / "research" / "quality_universe_events_v2.csv"

def clean(v):
    return "" if v is None else str(v).strip()

def val(row, names):
    d = {str(k).strip().lower(): clean(v) for k,v in row.items()}
    for n in names:
        if n.lower() in d:
            return d[n.lower()]
    nd = {"".join(c for c in k if c.isalnum()):v for k,v in d.items()}
    for n in names:
        k = "".join(c for c in n.lower() if c.isalnum())
        if k in nd:
            return nd[k]
    return ""

def read_data():
    if not CSV.exists():
        return {"ok":False,"error":f"CSV not found: {CSV}","rows":0,"assets":0,"top200":0,"events":{},"sides":{},"symbols":[],"recent":[],"fields":[]}
    rows = []
    try:
        with CSV.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            for row in reader:
                if row and any(clean(x) for x in row.values()):
                    rows.append(row)
    except Exception as e:
        return {"ok":False,"error":str(e),"rows":0,"assets":0,"top200":0,"events":{},"sides":{},"symbols":[],"recent":[],"fields":[]}

    syms = [val(r,["symbol","ticker","asset","pair","market","instrument"]) for r in rows]
    ev = [val(r,["event","event_type","type","action","status"]) for r in rows]
    sides = [val(r,["side","direction"]).lower() for r in rows]
    ranks = []
    for r in rows:
        try:
            x = float(val(r,["market_cap_rank","mc_rank","rank"]))
            if math.isfinite(x):
                ranks.append(x)
        except:
            pass

    recent = []
    for r in rows[-25:][::-1]:
        recent.append({
            "time":val(r,["timestamp","time","datetime","date","created_at","recorded_at"]),
            "symbol":val(r,["symbol","ticker","asset","pair","market","instrument"]),
            "event":val(r,["event","event_type","type","action","status"]),
            "side":val(r,["side","direction"]),
            "price":val(r,["price","entry_price","last_price","close"]),
            "rank":val(r,["market_cap_rank","mc_rank","rank"]),
            "volume":val(r,["volume_24h","24h_volume","volume","quote_volume"]),
            "spread":val(r,["spread","spread_pct","bid_ask_spread"])
        })

    return {
        "ok":True,
        "error":"",
        "rows":len(rows),
        "assets":len(set(x for x in syms if x)),
        "top200":sum(x <= 200 for x in ranks),
        "events":dict(Counter(x for x in ev if x)),
        "sides":dict(Counter(x for x in sides if x)),
        "symbols":sorted(set(x for x in syms if x))[:200],
        "recent":recent,
        "fields":fields,
        "modified":datetime.fromtimestamp(CSV.stat().st_mtime).isoformat(timespec="seconds")
    }

HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Experiment B Dashboard</title>
<style>
body{margin:0;background:#0e1117;color:#e6edf3;font:14px Arial}
.wrap{max-width:1450px;margin:auto;padding:22px}
h1{margin:0}.sub{color:#8b949e;margin:6px 0 18px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.card,.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}
.panel{margin-top:16px}.label{color:#8b949e}
.num{font-size:26px;font-weight:bold;margin-top:7px}.ok{color:#3fb950}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.pill{display:inline-block;background:#21262d;border:1px solid #30363d;border-radius:18px;padding:6px 9px;margin:3px;font-size:12px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #30363d;text-align:left;font-size:12px}
th{color:#8b949e}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<h1>Experiment B — Quality Universe v2</h1>
<div class="sub">Top 200 by market cap • READ ONLY • auto-refresh 10s • port 8788</div>

<div class="grid">
<div class="card"><div class="label">CSV rows</div><div class="num" id="rows">—</div></div>
<div class="card"><div class="label">Unique assets</div><div class="num" id="assets">—</div></div>
<div class="card"><div class="label">Top-200 rows</div><div class="num" id="top200">—</div></div>
<div class="card"><div class="label">Event types</div><div class="num" id="events">—</div></div>
<div class="card"><div class="label">Status</div><div class="num ok" id="status">—</div></div>
</div>

<div class="panel">
<b>Data source:</b> <span id="csv">research/quality_universe_events_v2.csv</span><br>
<span class="sub">Last file modification: <span id="mod">—</span></span><br>
<span id="msg">Loading…</span>
</div>

<div class="two">
<div class="panel"><h2>Event distribution</h2><div id="ev"></div></div>
<div class="panel"><h2>Side distribution</h2><div id="side"></div></div>
</div>

<div class="panel"><h2>Observed assets</h2><div id="symbols"></div></div>

<div class="panel">
<h2>Recent records</h2>
<table>
<thead><tr><th>Time</th><th>Symbol</th><th>Event</th><th>Side</th><th>Price</th><th>Rank</th><th>Volume</th><th>Spread</th></tr></thead>
<tbody id="recent"></tbody>
</table>
</div>

<div class="panel"><span class="sub">Detected fields: </span><span id="fields"></span></div>
</div>

<script>
const E=x=>String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function pills(o){return Object.entries(o||{}).map(([k,v])=>`<span class="pill">${E(k)}: <b>${E(v)}</b></span>`).join('')||'<span class="sub">No data yet.</span>'}
async function refresh(){
 try{
  let d=await (await fetch('/api/data?t='+Date.now(),{cache:'no-store'})).json();
  document.getElementById('rows').textContent=d.rows??0;
  document.getElementById('assets').textContent=d.assets??0;
  document.getElementById('top200').textContent=d.top200??0;
  document.getElementById('events').textContent=Object.keys(d.events||{}).length;
  document.getElementById('status').textContent=d.ok?'RUNNING':'CHECK';
  document.getElementById('msg').innerHTML=d.ok?'<span class="ok">✓ Read-only dashboard. No orders are placed.</span>':'⚠ '+E(d.error);
  document.getElementById('mod').textContent=d.modified||'—';
  document.getElementById('ev').innerHTML=pills(d.events);
  document.getElementById('side').innerHTML=pills(d.sides);
  document.getElementById('symbols').innerHTML=(d.symbols||[]).map(x=>`<span class="pill">${E(x)}</span>`).join('')||'No symbols yet.';
  document.getElementById('fields').textContent=(d.fields||[]).join(', ');
  document.getElementById('recent').innerHTML=(d.recent||[]).map(x=>`<tr><td>${E(x.time)}</td><td>${E(x.symbol)}</td><td>${E(x.event)}</td><td>${E(x.side)}</td><td>${E(x.price)}</td><td>${E(x.rank)}</td><td>${E(x.volume)}</td><td>${E(x.spread)}</td></tr>`).join('');
 }catch(e){
  document.getElementById('status').textContent='OFFLINE';
  document.getElementById('msg').textContent=e;
 }
}
refresh();setInterval(refresh,10000);
</script>
</body>
</html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        print("[dashboard]",fmt%args)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/":
            data=HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
        elif path=="/api/data":
            data=json.dumps(read_data()).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
        elif path=="/health":
            data=b"OK"
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
        else:
            data=b"Not found"
            self.send_response(404)
            self.send_header("Content-Type","text/plain")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(data)

print("="*65)
print("EXPERIMENT B DASHBOARD v1")
print("Dashboard: http://localhost:8788")
print(f"Data: {CSV}")
print("READ ONLY - does not place orders or modify the CSV")
print("="*65)

server=ThreadingHTTPServer(("127.0.0.1",PORT),H)
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nDashboard stopped.")
finally:
    server.server_close()

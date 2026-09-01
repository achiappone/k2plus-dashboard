#!/usr/bin/env python3
"""
Local dashboard for the K2 Plus (Moonraker + WebRTC camera).

WHY A LOCAL SERVER AND NOT A PUBLISHED PAGE: a hosted artifact is sandboxed -
it cannot fetch a private LAN address at all. And Moonraker sends no CORS
headers, so even a local file:// page cannot call it directly. This serves the
page and proxies the API from the same origin, which sidesteps both.

READ-ONLY BY WHITELIST. The proxy forwards GET to a fixed list of status
endpoints and nothing else. It cannot post gcode, set temperatures, or
start/pause/cancel a job even if something asks it to.

    python3 dashboard.py      then open http://localhost:8770
"""
import http.server, socketserver, urllib.request, urllib.parse, json, os, sys

# Your printer's address. Override without editing this file:
#     PRINTER_HOST=10.0.0.42 python3 dashboard.py
PRINTER = os.environ.get("PRINTER_HOST", "printer.local")
MOONRAKER = f"http://{PRINTER}:7125"
CAMERA    = f"http://{PRINTER}:8000"      # WebRTC signalling origin, not an image URL
PORT      = 8770

OBJECTS = ("print_stats&virtual_sdcard&extruder&heater_bed&toolhead"
           "&display_status&gcode_move"
           "&" + urllib.parse.quote("heater_generic chamber_heater") +
           "&" + urllib.parse.quote("temperature_sensor chamber_temp"))
ALLOWED = {                                   # the only things the proxy will fetch
    "status":  f"{MOONRAKER}/printer/objects/query?{OBJECTS}",
    "temps":   f"{MOONRAKER}/server/temperature_store?include_monitors=false",
    "info":    f"{MOONRAKER}/printer/info",
}

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>K2 Plus</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;450&display=swap">
<style>
:root{
  --bg:#eef1f5; --surface-1:#fcfcfb; --rule:#d5dbe4; --rule-2:#e6eaf0;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#7c8794;
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  --good:#1b6b45; --warn:#96600a; --crit:#a3312b;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0a0f16; --surface-1:#1a1a19; --rule:#2b333d; --rule-2:#20262e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --text-muted:#8a94a0;
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  --good:#4fbf8b; --warn:#e0a33c; --crit:#e07a72;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);
  font-family:"IBM Plex Sans",system-ui,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1240px;margin:0 auto;padding:20px 18px 60px}
h1,h2,.k{font-family:"IBM Plex Sans Condensed",sans-serif}
.mono,.v,.tt{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:2px solid var(--text-primary);padding-bottom:12px;margin-bottom:20px}
h1{font-size:26px;margin:0;letter-spacing:-.01em}
.host{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted)}
.pill{margin-left:auto;display:inline-flex;align-items:center;gap:7px;
  border:1px solid currentColor;padding:3px 11px;font-family:"IBM Plex Mono",monospace;
  font-size:12px;letter-spacing:.03em}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:20px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--surface-1);border:1px solid var(--rule);padding:16px 18px}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-secondary);
  margin:0 0 14px;font-weight:600}
.hero{display:flex;align-items:flex-end;gap:16px;margin-bottom:4px}
.hero .big{font-family:"IBM Plex Mono",monospace;font-size:54px;font-weight:500;
  line-height:.95;letter-spacing:-.02em}
.hero .sub{font-size:13px;color:var(--text-secondary);padding-bottom:7px}
.bar{height:7px;background:var(--rule-2);margin:14px 0 4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--series-1);transition:width .6s}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-top:18px}
.tile{background:var(--surface-1);padding:11px 13px}
.k{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-muted);margin:0 0 4px}
.v{font-size:17px;font-weight:500}
.small{font-size:12px;color:var(--text-muted)}
.sparks{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule);margin-top:20px}
.spark{background:var(--surface-1);padding:12px 14px 6px;position:relative}
.sparkhead{display:flex;align-items:baseline;gap:10px;margin-bottom:2px}
.sparkhead .nm{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:13px;font-weight:600}
.sparkhead .now{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:15px;font-weight:500}
.sparkhead .tgt{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--text-muted)}
.swatch{width:9px;height:9px;flex:none}
svg{display:block;width:100%;height:64px;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--text-primary);
  color:var(--surface-1);font-family:"IBM Plex Mono",monospace;font-size:11px;
  padding:3px 7px;transform:translate(-50%,-140%);opacity:0;transition:opacity .1s;white-space:nowrap}
.cam{width:100%;aspect-ratio:4/3;border:0;background:#000;display:block;object-fit:contain}
.camfoot{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
.camfoot button{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:12px;
  background:var(--surface-1);color:var(--text-primary);border:1px solid var(--rule);
  padding:5px 12px;cursor:pointer}
.camfoot button:hover{border-color:var(--text-secondary)}
.camfoot button:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}
.camwrap{background:#000;border:1px solid var(--rule)}
.note{font-size:12px;color:var(--text-muted);margin-top:10px}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:12px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--rule-2)}
th{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-muted);font-weight:600}
td.n{text-align:right;font-family:"IBM Plex Mono",monospace}
details{margin-top:14px}summary{cursor:pointer;font-size:12px;color:var(--text-secondary)}
</style></head><body>
<div class="wrap">
<header>
  <h1>K2&nbsp;Plus</h1><span class="host" id="host">—</span>
  <span class="pill" id="state"><span class="dot"></span><span id="statetx">connecting</span></span>
</header>

<div class="grid">
  <div>
    <div class="card">
      <h2>Progress</h2>
      <div class="hero"><span class="big" id="pct">—</span>
        <span class="sub" id="fname">—</span></div>
      <div class="bar"><i id="barfill" style="width:0"></i></div>
      <div class="small" id="times">—</div>
      <div class="tiles">
        <div class="tile"><p class="k">Z height</p><p class="v" id="z">—</p></div>
        <div class="tile"><p class="k">Filament</p><p class="v" id="fil">—</p></div>
        <div class="tile"><p class="k">Speed</p><p class="v" id="spd">—</p></div>
        <div class="tile"><p class="k">Flow</p><p class="v" id="flow">—</p></div>
      </div>
    </div>
    <div class="sparks" id="sparks"></div>
    <details><summary>Show the numbers as a table</summary>
      <table><thead><tr><th>Heater</th><th class="n">Now</th><th class="n">Target</th>
      <th class="n">Min (10 min)</th><th class="n">Max</th></tr></thead>
      <tbody id="tbody"></tbody></table></details>
  </div>
  <div>
    <div class="card" style="padding:0">
      <div class="camwrap"><video class="cam" id="cam" autoplay muted playsinline></video></div>
    </div>
    <div class="camfoot">
      <span class="pill" id="campill"><span class="dot"></span><span id="camtx">connecting</span></span>
      <button id="grab">Save still</button>
      <button id="recon">Reconnect</button>
    </div>
    <p class="note">The camera is WebRTC &mdash; there is no snapshot endpoint. Port 8000
    returns the same signalling page for every path and query, so
    <span class="mono">?action=snapshot</span> is that page too, not a JPEG. This panel does
    the negotiation itself, which is why the video sits in the layout instead of a frame.</p>
  </div>
</div>
</div>
<script>
const CH = [
  {key:"extruder",                          name:"Nozzle",  slot:1},
  {key:"heater_bed",                        name:"Bed",     slot:2},
  {key:"heater_generic chamber_heater",     name:"Chamber", slot:3},
];
const el = id => document.getElementById(id);
const hm = s => { s=Math.max(0,Math.round(s||0));
  return `${Math.floor(s/3600)}h ${String(Math.floor(s%3600/60)).padStart(2,"0")}m`; };
let store = {};

function statusColor(st, dev){
  if (["error","cancelled"].includes(st)) return "var(--crit)";
  if (st==="paused" || dev)               return "var(--warn)";
  if (st==="printing" || st==="ready")    return "var(--good)";
  return "var(--text-muted)";
}

async function get(what){
  const r = await fetch("/api/"+what);
  if(!r.ok) throw new Error(what+" "+r.status);
  return (await r.json()).result;
}

function drawSpark(box, ch, series, target){
  const w=520,h=64,pad=3;
  const vals=series.filter(v=>typeof v==="number");
  if(!vals.length) return;
  let lo=Math.min(...vals), hi=Math.max(...vals);
  if(target) { lo=Math.min(lo,target); hi=Math.max(hi,target); }
  if(hi-lo<2){ const m=(hi+lo)/2; lo=m-1; hi=m+1; }
  const X=i=>pad+i*(w-2*pad)/(vals.length-1||1);
  const Y=v=>h-pad-(v-lo)/(hi-lo)*(h-2*pad);
  const d=vals.map((v,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const tgt = target ? `<line x1="0" y1="${Y(target).toFixed(1)}" x2="${w}" y2="${Y(target).toFixed(1)}"
     stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>` : "";
  box.querySelector(".plot").innerHTML =
    `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
       <line x1="0" y1="${h-pad}" x2="${w}" y2="${h-pad}" stroke="var(--rule-2)" stroke-width="1"/>
       ${tgt}
       <path d="${d}" fill="none" stroke="var(--series-${ch.slot})" stroke-width="2"
             stroke-linejoin="round" stroke-linecap="round"/>
       <circle cx="${X(vals.length-1).toFixed(1)}" cy="${Y(vals.at(-1)).toFixed(1)}" r="3.5"
               fill="var(--series-${ch.slot})" stroke="var(--surface-1)" stroke-width="2"/>
       <rect class="hit" x="0" y="0" width="${w}" height="${h}" fill="transparent"/>
     </svg>`;
  const svg=box.querySelector("svg"), tip=box.querySelector(".tip");
  svg.addEventListener("pointermove",e=>{
    const r=svg.getBoundingClientRect();
    const i=Math.round((e.clientX-r.left)/r.width*(vals.length-1));
    const v=vals[Math.max(0,Math.min(vals.length-1,i))];
    const ago=((vals.length-1-i)).toFixed(0);
    tip.style.opacity=1; tip.style.left=(e.clientX-r.left)+"px"; tip.style.top="0px";
    tip.textContent=`${v.toFixed(1)} °C · ${ago}s ago`;
  });
  svg.addEventListener("pointerleave",()=>tip.style.opacity=0);
}

function buildSparks(){
  el("sparks").innerHTML = CH.map(c=>`
    <div class="spark" data-k="${c.key}">
      <div class="sparkhead">
        <span class="swatch" style="background:var(--series-${c.slot})"></span>
        <span class="nm">${c.name}</span>
        <span class="tgt" id="t-${c.slot}"></span>
        <span class="now" id="n-${c.slot}">—</span>
      </div>
      <div class="plot"></div><div class="tip"></div>
    </div>`).join("");
}

async function tick(){
  try{
    const s=(await get("status")).status;
    const ps=s.print_stats||{}, vs=s.virtual_sdcard||{}, gm=s.gcode_move||{};
    const prog=(vs.progress||0)*100;
    el("pct").textContent = prog.toFixed(1)+"%";
    el("fname").textContent = ps.filename || "(no file)";
    el("barfill").style.width = prog+"%";
    const el_=ps.print_duration||0;
    const rem = prog>0.5 ? el_*(100-prog)/prog : null;
    el("times").textContent = `${hm(el_)} elapsed` + (rem?` · ~${hm(rem)} remaining`:"");
    el("z").textContent   = (ps.z_pos!=null? ps.z_pos.toFixed(2):"—")+" mm";
    el("fil").textContent = ((ps.filament_used||0)/1000).toFixed(2)+" m";
    el("spd").textContent = Math.round((gm.speed||0)/60)+" mm/s";
    el("flow").textContent= Math.round((gm.speed_factor||1)*100)+"%";

    let dev=false;
    CH.forEach(c=>{
      const o=s[c.key]||{};
      const n=el("n-"+c.slot), t=el("t-"+c.slot);
      if(o.temperature!=null){
        n.textContent=o.temperature.toFixed(1)+" °C";
        t.textContent=o.target?("target "+o.target.toFixed(0)):"";
        if(o.target>0 && Math.abs(o.temperature-o.target)>8) dev=true;
      }
    });
    const st=ps.state||"—";
    el("statetx").textContent = st + (dev?" · temp off target":"");
    el("state").style.color = statusColor(st, dev);
  }catch(e){
    el("statetx").textContent="unreachable";
    el("state").style.color="var(--crit)";
  }
}

async function tickTemps(){
  try{
    store = await get("temps");
    const rows=[];
    CH.forEach(c=>{
      const box=document.querySelector(`.spark[data-k="${CSS.escape(c.key)}"]`);
      const d=store[c.key]; if(!box||!d) return;
      const t=d.temperatures||[], tg=(d.targets||[]).at(-1);
      drawSpark(box,c,t,tg);
      const v=t.filter(x=>typeof x==="number");
      rows.push(`<tr><td>${c.name}</td><td class="n">${v.at(-1)?.toFixed(1)??"—"}</td>
        <td class="n">${tg?tg.toFixed(0):"—"}</td>
        <td class="n">${Math.min(...v).toFixed(1)}</td>
        <td class="n">${Math.max(...v).toFixed(1)}</td></tr>`);
    });
    el("tbody").innerHTML=rows.join("");
  }catch(e){}
}

// ---- camera: WebRTC, negotiated here rather than framed -------------------
// Port 8000 sends Access-Control-Allow-Origin *, so the offer can be POSTed
// straight from this page. Content-Type stays text/plain so the request is
// CORS-simple and no preflight is needed - the server only base64-decodes the
// body, it does not inspect the type.
const CAM = "__CAMERA__";
let pc=null;
function camState(t,c){ el("camtx").textContent=t; el("campill").style.color=c; }
function connectCam(){
  if(pc){ try{pc.close();}catch(e){} }
  pc = new RTCPeerConnection({iceServers:[{urls:"stun:stun.l.google.com:19302"}]});
  camState("connecting","var(--text-muted)");
  pc.ontrack = e => { el("cam").srcObject = e.streams[0]; };
  pc.oniceconnectionstatechange = () => {
    const s=pc.iceConnectionState;
    camState(s, (s==="connected"||s==="completed") ? "var(--good)"
             : (s==="failed"||s==="disconnected") ? "var(--crit)" : "var(--text-muted)");
  };
  pc.onicecandidate = ev => {
    if(ev.candidate !== null) return;
    fetch(CAM+"/call/webrtc_local", {method:"POST", headers:{"Content-Type":"text/plain"},
      body: btoa(JSON.stringify({type:"offer", sdp:pc.localDescription.sdp}))})
      .then(r=>r.text())
      .then(t=>{ const a=JSON.parse(atob(t));
                 if(a.type==="answer") pc.setRemoteDescription(new RTCSessionDescription(a)); })
      .catch(()=>camState("signalling failed","var(--crit)"));
  };
  pc.addTransceiver("video",{direction:"sendrecv"});
  pc.createOffer().then(d=>pc.setLocalDescription(d))
    .catch(()=>camState("offer failed","var(--crit)"));
}
el("recon").onclick = connectCam;
el("grab").onclick = () => {
  const v=el("cam"); if(!v.videoWidth) return;
  const c=document.createElement("canvas");
  c.width=v.videoWidth; c.height=v.videoHeight;
  c.getContext("2d").drawImage(v,0,0);
  const a=document.createElement("a");
  a.download=`k2-${new Date().toISOString().replace(/[:.]/g,"-")}.png`;
  a.href=c.toDataURL("image/png"); a.click();
};
connectCam();

get("info").then(i=>el("host").textContent=i.hostname+" · "+i.software_version).catch(()=>{});
buildSparks(); tick(); tickTemps();
setInterval(tick, 2000);
setInterval(tickTemps, 10000);
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.replace("__CAMERA__", CAMERA).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/"):
            key = self.path[5:]
            if key not in ALLOWED:              # <- read-only whitelist
                self.send_error(403, "not an allowed endpoint"); return
            try:
                with urllib.request.urlopen(ALLOWED[key], timeout=8) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
            except Exception as e:
                self.send_error(502, str(e))
            return
        self.send_error(404)


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), H) as srv:
        print(f"dashboard  http://localhost:{PORT}")
        print(f"printer    {MOONRAKER}   camera {CAMERA}")
        print("read-only proxy: " + ", ".join(sorted(ALLOWED)))
        try: srv.serve_forever()
        except KeyboardInterrupt: print("\nstopped")

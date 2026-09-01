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
import secrets, uuid

# Your printer's address. Override without editing this file:
#     PRINTER_HOST=10.0.0.42 python3 dashboard.py
PRINTER = os.environ.get("PRINTER_HOST", "printer.local")
MOONRAKER = f"http://{PRINTER}:7125"
CAMERA    = f"http://{PRINTER}:8000"      # WebRTC signalling origin, not an image URL
PORT      = 8770

_OBJ = ["print_stats", "virtual_sdcard", "extruder", "heater_bed", "toolhead",
        "display_status", "gcode_move",
        "heater_generic chamber_heater", "temperature_fan chamber_fan",
        "temperature_sensor chamber_temp", "temperature_sensor mcu_temp"]
OBJECTS = "&".join(urllib.parse.quote(o) for o in _OBJ)
# The UI may be served from GitHub Pages instead of from here. Browsers treat
# http://localhost as a trustworthy origin, so an HTTPS page IS allowed to call
# it - unlike the printer's own address, which is plain HTTP on a private IP and
# gets blocked as mixed content no matter what network you are on. So the page
# can live anywhere; the proxy has to be local either way.
ORIGINS = ("https://achiappone.github.io", "http://localhost:8770",
           "http://127.0.0.1:8770")

# The camera's WebRTC handshake is HTTP, so a hosted HTTPS page cannot POST it
# directly. Relayed here instead. Only the MEDIA needs to reach the printer, and
# that is UDP - not subject to mixed-content rules - so it flows browser-to-
# printer directly once the handshake is done.
CAMERA_SIGNAL = f"http://{PRINTER}:8000/call/webrtc_local"

# ---------------------------------------------------------------- control mode
# OFF unless you ask for it:  K2_CONTROL=1 python3 dashboard.py
# Read-only stays the default because that is the safe thing to leave running.
CONTROL = os.environ.get("K2_CONTROL") == "1"

# Stamped into the page so you can tell at a glance WHICH build you are looking
# at. Without it, a stale cached copy is indistinguishable from a failed deploy,
# and you go hunting for a bug that is not there.
BUILD = __import__("datetime").datetime.now().strftime("%H:%M:%S")

# Every write needs this token in an X-K2-Token header. Two reasons, and the
# second is the important one:
#
#  1. it keeps a stray click from doing something, and
#  2. CORS does NOT stop a hostile page from SENDING a cross-origin POST - it
#     only stops it reading the reply. Without a token, any website you happened
#     to visit could POST to localhost:8770 and set your hot end to 300 C while
#     you were out. A custom header forces a preflight, and the preflight only
#     succeeds for the origins listed above.
TOKEN = os.environ.get("K2_TOKEN") or secrets.token_urlsafe(12)

# Server-side ceilings. The UI clamps too, but the UI is not the security
# boundary - anything can talk to this port.
LIMITS = {"extruder": 300.0, "heater_bed": 120.0,
          "chamber_heater": 60.0, "chamber_fan": 70.0}
# a temperature_fan is not a heater and takes its own command
SETTER = {
    "extruder":       "SET_HEATER_TEMPERATURE HEATER=extruder TARGET={t:.0f}",
    "heater_bed":     "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={t:.0f}",
    "chamber_heater": "SET_HEATER_TEMPERATURE HEATER=chamber_heater TARGET={t:.0f}",
    "chamber_fan":    "SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber_fan TARGET={t:.0f}",
}

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
/* LAYOUT STABILITY. Every element whose text changes gets tabular figures and a
   reserved width. Without this the digits themselves reflow the page: "9.8%"
   and "100.0%" are different widths, and at 1 Hz that is a permanent shimmer. */
.stable{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:2px solid var(--text-primary);padding-bottom:12px;margin-bottom:20px}
h1{font-size:26px;margin:0;letter-spacing:-.01em}
.host{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted)}
.build{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--text-muted);
  border:1px solid var(--rule);padding:2px 8px;white-space:nowrap}
.pill{margin-left:auto;display:inline-flex;align-items:center;gap:7px;
  min-width:15ch;justify-content:center;
  border:1px solid currentColor;padding:3px 11px;font-family:"IBM Plex Mono",monospace;
  font-size:12px;letter-spacing:.03em}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor;flex:none}
.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:20px;position:relative}
.grid>*{min-width:0}
.sparks>*{min-width:0}
.card{contain:layout}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--surface-1);border:1px solid var(--rule);padding:16px 18px}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-secondary);
  margin:0 0 14px;font-weight:600}
.hero{display:flex;align-items:flex-end;gap:16px;margin-bottom:4px}
.hero .big{font-family:"IBM Plex Mono",monospace;font-size:54px;font-weight:500;
  line-height:.95;letter-spacing:-.02em;font-variant-numeric:tabular-nums;
  min-width:5.2ch;display:inline-block}
.hero .sub{font-size:13px;color:var(--text-secondary);padding-bottom:7px}
.bar{height:7px;background:var(--rule-2);margin:14px 0 4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--series-1);transition:width .6s}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-top:18px}
.tile{background:var(--surface-1);padding:11px 13px}
.k{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--text-muted);margin:0 0 4px}
.v{font-size:17px;font-weight:500}
.small{font-size:12px;color:var(--text-muted)}
#times{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
  min-height:1.5em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#fname{max-width:46ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.therm{margin-top:20px}
table.th{border-collapse:collapse;width:100%;font-size:15px}
table.th th{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--text-muted);
  font-weight:600;text-align:left;padding:7px 10px;border-bottom:1px solid var(--rule)}
table.th td{padding:9px 10px;border-bottom:1px solid var(--rule-2);vertical-align:middle}
table.th tr:last-child td{border-bottom:0}
table.th .n,table.th th.n{text-align:right;font-family:"IBM Plex Mono",monospace;
  font-variant-numeric:tabular-nums}
table.th td.n.pw{width:76px}  table.th td.n.ch{width:104px}
table.th td.n.act{width:104px} table.th td:first-child{width:auto}
table.th{table-layout:fixed}
table.th th.tgt{text-align:left;width:118px}
.nm{display:flex;align-items:center;gap:9px;font-weight:450}
.swatch{width:10px;height:10px;flex:none;border-radius:2px}
.pw{color:var(--text-muted);font-size:15px}
.ch{color:var(--text-muted);font-size:14px}
.act{font-size:18px;font-weight:500}
.deg{color:var(--text-muted);font-size:12px}
td.tgtcell{display:flex;align-items:center;gap:5px}
td.tgtcell input{width:74px;font-family:"IBM Plex Mono",monospace;font-size:16px;
  background:var(--bg);color:var(--text-primary);border:1px solid var(--rule);padding:4px 7px;
  text-align:right;font-variant-numeric:tabular-nums}
td.tgtcell input:disabled{border-color:transparent;background:transparent;color:var(--text-muted)}
td.tgtcell input:focus{outline:2px solid var(--series-1);outline-offset:-1px}
.small{margin-top:20px}
.sparks{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule)}
.spark{background:var(--surface-1);padding:11px 13px 4px;position:relative}
.spkhead{display:flex;align-items:baseline;gap:8px;margin-bottom:3px}
.spkhead .nm2{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:13.5px;font-weight:600}
.spkhead .now{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:15px;
  font-weight:500;font-variant-numeric:tabular-nums;min-width:8ch;text-align:right}
.spkhead .rng{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--text-muted);
  font-variant-numeric:tabular-nums;min-width:11ch}
.spkhead .nm2{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spark svg{display:block;width:100%;height:62px}
.spark .tip{font-size:12px;padding:5px 8px}
.spark .plot{min-height:62px}
.failmsg{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--crit);
  padding:22px 4px;margin:0}
.chart{position:relative;margin-top:16px;min-height:300px}
.chart svg{display:block;width:100%;height:300px}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);
  border:1px solid var(--rule);color:var(--text-primary);
  font-family:"IBM Plex Mono",monospace;font-size:13px;padding:9px 11px;
  opacity:0;transition:opacity .08s;white-space:nowrap;box-shadow:0 3px 14px rgba(0,0,0,.18);z-index:5}
.tip b{font-weight:500}
.tip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;font-style:normal}
.cam{width:100%;aspect-ratio:4/3;border:0;background:#000;display:block;object-fit:contain}
.camfoot{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
.camfoot button{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:12px;
  background:var(--surface-1);color:var(--text-primary);border:1px solid var(--rule);
  padding:5px 12px;cursor:pointer}
.camfoot button:hover{border-color:var(--text-secondary)}
.camfoot button:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}
.camwrap{background:#000;border:1px solid var(--rule)}
.note{font-size:12px;color:var(--text-muted);margin-top:10px}
.ctl{margin-top:20px}
.ctl label{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-muted)}
.ctl input[type=number],.ctl input[type=password]{font-family:"IBM Plex Mono",monospace;
  font-size:14px;background:var(--bg);color:var(--text-primary);border:1px solid var(--rule);
  padding:6px 9px;width:100%}
.ctl button{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:12.5px;
  background:var(--surface-1);color:var(--text-primary);border:1px solid var(--rule);
  padding:6px 14px;cursor:pointer;white-space:nowrap}
.ctl button:hover:not(:disabled){border-color:var(--text-secondary)}
.ctl button:disabled{opacity:.4;cursor:not-allowed}
.ctl button:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}
.ctl button.danger{color:var(--crit);border-color:var(--crit)}
.tokrow{display:grid;grid-template-columns:auto 1fr auto;gap:9px;align-items:center;
  padding-bottom:14px;margin-bottom:14px;border-bottom:1px solid var(--rule-2)}
.ctlgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
.ctlset{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center}
.ctlrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
.filelbl{display:inline-flex;gap:8px;align-items:center;text-transform:none;letter-spacing:0;
  font-size:12.5px;color:var(--text-secondary)}
.chk{display:inline-flex;gap:6px;align-items:center;text-transform:none;letter-spacing:0;
  font-size:12.5px;color:var(--text-secondary)}
.msg{font-family:"IBM Plex Mono",monospace;font-size:12px;margin:14px 0 0;min-height:1.2em}
.msg.err{color:var(--crit)} .msg.ok{color:var(--good)}
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
  <span class="build" id="build" title="which build of this page you are looking at">__BUILD__</span>
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
    <div class="card therm">
      <h2>Thermals</h2>
      <div class="tw"><table class="th">
        <thead><tr><th>Name</th><th class="n">Power</th><th class="n">Change</th>
          <th class="n">Actual</th><th class="tgt">Target</th></tr></thead>
        <tbody id="thbody"></tbody>
      </table></div>
      <div class="chart" id="chart"><div class="tip" id="ctip"></div></div>
    </div>

    <div class="card small" id="smallcard">
      <h2>Each sensor on its own scale</h2>
      <p class="note" style="margin:-6px 0 14px">The chart above shares one axis, so
      the chamber traces sit flat near the bottom. Here each gets its own, which shows
      the shape of a 2&nbsp;&deg;C wobble as clearly as a 200&nbsp;&deg;C one.</p>
      <div class="sparks" id="sparks"></div>
    </div>

    <div class="card ctl" id="controls" hidden>
      <h2>Controls</h2>
      <div class="tokrow" id="tokrow">
        <label for="tok">Token</label>
        <input id="tok" type="password" placeholder="printed when you start with K2_CONTROL=1"
               autocomplete="off" spellcheck="false">
        <button id="savetok">Save</button>
      </div>
      <p class="note" style="margin:0 0 4px">Set temperatures in the
      <b>Target</b> column of the Thermals table &mdash; type a value and press Enter.</p>
      <div class="ctlrow">
        <button id="b-pause">Pause</button>
        <button id="b-resume">Resume</button>
        <button id="b-cancel" class="danger">Cancel print</button>
      </div>
      <div class="ctlrow">
        <label class="filelbl" for="gfile">Upload gcode
          <input id="gfile" type="file" accept=".gcode"></label>
        <label class="chk"><input id="startnow" type="checkbox"> start it immediately</label>
        <button id="b-upload">Upload</button>
      </div>
      <p class="msg" id="ctlmsg"></p>
    </div>
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
// Served by this same process, so the API is same-origin. build_docs.py swaps
// this for a localhost address when it generates the GitHub Pages copy, which
// has no backend of its own.
const PROXY = "";

// Fixed slot order - never cycled, so a sensor keeps its colour whatever else
// is on screen. All six are degrees C, so they share ONE axis; different
// magnitudes are not a reason for a second one.
const CH = [
  {key:"heater_generic chamber_heater", name:"Chamber Heater", slot:1, set:"chamber_heater", max:60},
  {key:"extruder",                      name:"Extruder",       slot:2, set:"extruder",       max:300},
  {key:"heater_bed",                    name:"Heater Bed",     slot:3, set:"heater_bed",     max:120},
  {key:"temperature_fan chamber_fan",   name:"Chamber Fan",    slot:4, set:"chamber_fan",    max:70},
  {key:"temperature_sensor chamber_temp", name:"Chamber Temp", slot:5},
  {key:"temperature_sensor mcu_temp",   name:"Mcu Temp",       slot:6},
];
const el = id => document.getElementById(id);
const hm = s => { s=Math.max(0,Math.round(s||0));
  return `${Math.floor(s/3600)}h ${String(Math.floor(s%3600/60)).padStart(2,"0")}m`; };
// with seconds, and dropping units that are zero so it stays readable
const hms = s => {
  s = Math.max(0, Math.round(s||0));
  const h=Math.floor(s/3600), m=Math.floor(s%3600/60), sec=s%60, p=n=>String(n).padStart(2,"0");
  return h ? `${h}h ${p(m)}m ${p(sec)}s` : m ? `${m}m ${p(sec)}s` : `${sec}s`;
};
// The API is polled every 2 s, so a countdown driven straight off it would jump
// two seconds at a time. Take a reading, then run the clock locally from it.
let clock = null;

// The charts used to redraw only when /api/temps was re-fetched every 10 s, so
// a 600-sample window crept forward ten samples at a time and looked frozen.
// /api/temps is ~70 KB, so polling THAT hard would be wasteful - but the 1 s
// status poll already carries every current reading. Append those to a local
// buffer and redraw each tick; re-sync with the server occasionally to correct
// any drift. Same cadence as the printer's own 1 Hz sampling, no extra traffic.
const HISTORY = 600;
function pushSample(st){
  CH.forEach(c=>{
    const o = st[c.key]; if(!o || o.temperature==null) return;
    const d = store[c.key] || (store[c.key] = {temperatures: [], targets: []});
    d.temperatures.push(o.temperature);
    d.targets.push(o.target ?? null);
    if(d.temperatures.length > HISTORY){ d.temperatures.shift(); d.targets.shift(); }
  });
}
// do not yank the chart out from under a pointer that is reading it
let hovering = false;
let store = {};

function statusColor(st, dev){
  if (["error","cancelled"].includes(st)) return "var(--crit)";
  if (st==="paused" || dev)               return "var(--warn)";
  if (st==="printing" || st==="ready")    return "var(--good)";
  return "var(--text-muted)";
}

async function get(what){
  const ac = new AbortController();
  const t = setTimeout(()=>ac.abort(), 4000);
  const opts = {signal: ac.signal, cache: "no-store"};
  // Chrome's Local Network Access: a public https page reaching localhost must
  // declare the request as targeting the local address space, AND the user must
  // grant permission. Undeclared, every request logs a warning and is refused.
  // Unknown options are ignored by browsers that do not implement it.
  if(PROXY && !PROXY.startsWith(location.origin)) opts.targetAddressSpace = "local";
  try{
    const r = await fetch(PROXY+"/api/"+what, opts);
    if(!r.ok) throw new Error(what+" "+r.status);
    return (await r.json()).result;
  } finally { clearTimeout(t); }
}

// A single dropped poll is not a disconnection. Only say so after several in a
// row, otherwise the setup panel appears and disappears and the page jumps.
let misses = 0, lastErr = "";
const MISSES_BEFORE_DISCONNECTED = 5;

// When a fetch fails the browser only says "Failed to fetch" - it will not tell
// a page why it blocked it. This distinguishes the cases that actually matter:
// a proxy that is not running looks identical to one the browser refused to let
// us read, and they need completely different fixes.
async function diagnose(){
  const out = [];
  try{
    await fetch(PROXY+"/api/status", {mode:"no-cors", cache:"no-store"});
    out.push("The proxy IS running. Chrome is refusing to let this page read it.");
    out.push("Chrome's Local Network Access rule: a page on a public https "
           + "origin needs your explicit permission before it may reach "
           + "localhost or a private address.");
    out.push("Grant it: click the icon at the left of the address bar → Site "
           + "settings → allow local network access, then reload.");
    out.push("Or skip the whole restriction and open http://localhost:8770 "
           + "- same origin, no permission needed, and the camera works there too.");
  }catch(e){
    out.push("The proxy is NOT reachable at " + PROXY + ".");
    out.push("Start it:  K2_CONTROL=1 PRINTER_HOST=<ip> python3 dashboard.py");
  }
  if(lastErr) out.push("Last error: " + lastErr);
  out.push("Page origin: " + location.origin);
  return out;
}
// Chrome will only show the Local Network Access prompt off a real user
// gesture - a fetch fired on page load is refused silently. So the request is
// made from a button, which is what actually surfaces the dialog.
async function requestAccess(){
  const b = document.getElementById("grant");
  if(b){ b.disabled = true; b.textContent = "Connecting…"; }
  try{
    await get("status");                     // this is what triggers the prompt
    misses = 0; pollDelay = 1000;
    const sp = document.getElementById("setup");
    if(sp) sp.classList.add("ok");
    tickTemps();
  }catch(e){
    lastErr = (e && e.message) ? e.message : String(e);
    await showDiagnosis();
  }finally{
    if(b){ b.disabled = false; b.textContent = "Connect to my printer"; }
    showPermState();
  }
}
async function showPermState(){
  const el2 = document.getElementById("permstate");
  if(!el2 || !navigator.permissions) return;
  try{
    const st = await navigator.permissions.query({name: "local-network-access"});
    el2.textContent = "browser permission: " + st.state;
    st.onchange = () => { el2.textContent = "browser permission: " + st.state;
                          if(st.state === "granted") requestAccess(); };
  }catch(e){ el2.textContent = ""; }
}

async function showDiagnosis(){
  const box = document.getElementById("diag");
  if(!box) return;
  box.textContent = "checking...";
  box.innerHTML = (await diagnose()).map(l=>"• "+l).join("<br>");
}

// ---- thermals table -------------------------------------------------------
function buildTable(){
  el("thbody").innerHTML = CH.map(c=>`
    <tr data-k="${c.key}">
      <td><span class="nm"><span class="swatch" style="background:var(--series-${c.slot})"></span>${c.name}</span></td>
      <td class="n pw" id="pw-${c.slot}"></td>
      <td class="n ch" id="ch-${c.slot}"></td>
      <td class="n act" id="ac-${c.slot}">—</td>
      <td class="tgtcell">${c.set
        ? `<input id="tg-${c.slot}" type="number" min="0" max="${c.max}" step="1"
             aria-label="${c.name} target" disabled><span class="deg">°C</span>`
        : ``}</td>
    </tr>`).join("");
  CH.filter(c=>c.set).forEach(c=>{
    const i = el("tg-"+c.slot);
    i.addEventListener("keydown", ev => { if(ev.key==="Enter") applyTarget(c, i); });
    i.addEventListener("blur", () => { if(i.dataset.dirty==="1") applyTarget(c, i); });
    i.addEventListener("input", () => { i.dataset.dirty="1"; });
  });
}
async function applyTarget(c, input){
  input.dataset.dirty="0";
  const v = Number(input.value);
  if(!(v>=0 && v<=c.max)){ msg(`${c.name} must be 0..${c.max}`,"err"); return; }
  const j = await send("temp", JSON.stringify({heater:c.set, target:v}));
  if(j) msg(j.sent, "ok");
}

// change in deg/s from the tail of the history, averaged over ~5 s so it is
// not just sensor noise
function slope(v){
  if(!v || v.length<6) return 0;
  const n=Math.min(5, v.length-1);
  return (v[v.length-1] - v[v.length-1-n]) / n;
}

// ---- small multiples: one panel per sensor, each on its own scale ---------
// Colour here is NOT carrying identity - six hues fail the all-pairs check that
// small multiples are graded on (worst pair dE 1.6 for deuteranopia). Every
// panel is titled with its name and current value, so the trace colour is only
// a visual tie back to the shared chart above.
function buildSparks(){
  el("sparks").innerHTML = CH.map(c=>`
    <div class="spark" data-k="${c.key}">
      <div class="spkhead">
        <span class="swatch" style="background:var(--series-${c.slot})"></span>
        <span class="nm2">${c.name}</span>
        <span class="rng" id="rg-${c.slot}"></span>
        <span class="now" id="sn-${c.slot}">&mdash;</span>
      </div>
      <div class="plot"></div><div class="tip"></div>
    </div>`).join("");
}

function drawSparks(){
  CH.forEach(c=>{
    const box = document.querySelector(`.spark[data-k="${CSS.escape(c.key)}"]`);
    const d = store[c.key]; if(!box || !d) return;
    const v = (d.temperatures||[]).filter(x=>typeof x==="number");
    if(!v.length) return;
    const tgt = (d.targets||[]).at(-1);
    const W = Math.max(180, Math.round((box.clientWidth - 26)/8)*8), H = 62, pad = 4;
    let lo = Math.min(...v), hi = Math.max(...v);
    if(tgt){ lo = Math.min(lo, tgt); hi = Math.max(hi, tgt); }
    // never let a flat trace fill the panel with noise: floor the span at 2 deg
    if(hi - lo < 2){ const m=(hi+lo)/2; lo=m-1; hi=m+1; }
    // round the band outward so small wobbles do not rescale the panel
    lo = Math.floor(lo); hi = Math.ceil(hi);
    const X = i => pad + i*(W-2*pad)/(v.length-1||1);
    const Y = t => H-pad - (t-lo)/(hi-lo)*(H-2*pad);
    const d2 = v.map((t,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(t).toFixed(1)}`).join("");
    const tl = tgt ? `<line x1="0" y1="${Y(tgt).toFixed(1)}" x2="${W}" y2="${Y(tgt).toFixed(1)}"
        stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="3 3" opacity=".6"/>` : "";
    const sig = [W,H,lo.toFixed(1),hi.toFixed(1),v.length].join("|");
    const plot = box.querySelector(".plot");
    const have = plot.querySelector("svg");
    if(have && plot.dataset.sig === sig){
      have.querySelector("path")?.setAttribute("d", d2);
      const dot = have.querySelector("circle");
      if(dot){ dot.setAttribute("cx", X(v.length-1).toFixed(1));
               dot.setAttribute("cy", Y(v.at(-1)).toFixed(1)); }
      el("sn-"+c.slot).innerHTML = v.at(-1).toFixed(1)+'<span class="deg"> °C</span>';
      return;
    }
    plot.dataset.sig = sig;
    plot.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
         ${tl}
         <path d="${d2}" fill="none" stroke="var(--series-${c.slot})" stroke-width="2"
               stroke-linejoin="round" stroke-linecap="round"/>
         <circle cx="${X(v.length-1).toFixed(1)}" cy="${Y(v.at(-1)).toFixed(1)}" r="3.2"
                 fill="var(--series-${c.slot})" stroke="var(--surface-1)" stroke-width="2"/>
       </svg>`;
    el("sn-"+c.slot).innerHTML = v.at(-1).toFixed(1)+'<span class="deg"> °C</span>';
    el("rg-"+c.slot).textContent = `${Math.min(...v).toFixed(1)}–${Math.max(...v).toFixed(1)}`;
    const svg = box.querySelector("svg"), tip = box.querySelector(".tip");
    svg.addEventListener("pointermove", e=>{
      const r = svg.getBoundingClientRect();
      const i = Math.max(0, Math.min(v.length-1,
                 Math.round((e.clientX-r.left)/r.width*(v.length-1))));
      tip.style.opacity = 1;
      tip.style.left = Math.min(e.clientX-r.left, r.width-70)+"px";
      tip.style.top = "-4px";
      tip.textContent = `${v[i].toFixed(1)}° · ${v.length-1-i}s ago`;
    });
    svg.addEventListener("pointerenter", ()=> hovering = true);
    svg.addEventListener("pointerleave", ()=>{ hovering = false; tip.style.opacity = 0; });
  });
}

// ---- one chart, six series, one degrees-C axis ---------------------------
function drawChart(){
  // Draw in REAL pixels: the viewBox matches the element, so nothing is
  // stretched. It used to be a fixed 1000-unit box with preserveAspectRatio
  // "none", which squashed the labels and the line weights horizontally.
  const box = el("chart");
  // quantised: a one-pixel change must not count as "structure changed" and
  // rebuild the whole chart
  const W = Math.max(360, Math.round((box.clientWidth || 900)/8)*8);
  const H = 300, L = 54, R = 12, T = 12, B = 34;
  const series = CH.map(c=>({c, v:(store[c.key]||{}).temperatures||[]}))
                   .filter(s=>s.v.length);
  if(!series.length) return;
  const n = Math.max(...series.map(s=>s.v.length));
  let peak = Math.max(...series.map(s=>Math.max(...s.v)));
  // Hysteresis. Recomputing the top every tick made the whole chart rescale and
  // jump whenever the peak crossed a rounding boundary. Grow when it must,
  // shrink only when there is a lot of headroom.
  let hi = drawChart.hi || 0;
  if(peak > hi*0.98 || peak < hi*0.55 || !hi) hi = Math.ceil((peak*1.10)/50)*50 || 50;
  drawChart.hi = hi;
  const X = i => L + i*(W-L-R)/(n-1||1);
  const Y = v => H-B - (v/hi)*(H-T-B);
  const step = hi>200?50:hi>100?25:10;
  let grid="", ylab="";
  for(let g=0; g<=hi; g+=step){
    grid += `<line x1="${L}" y1="${Y(g)}" x2="${W-R}" y2="${Y(g)}" stroke="var(--rule-2)" stroke-width="1"/>`;
    ylab += `<text x="${L-9}" y="${Y(g)+5}" text-anchor="end" font-size="14"
              fill="var(--text-muted)" font-family="IBM Plex Mono,monospace">${g}</text>`;
  }
  // x axis in clock time - samples are 1 Hz, newest last
  const now = Date.now(); let xlab="";
  for(let k=0;k<=5;k++){
    const i = Math.round(k*(n-1)/5);
    const t = new Date(now - (n-1-i)*1000);
    xlab += `<text class="xl" x="${X(i)}" y="${H-10}" text-anchor="middle" font-size="14"
              fill="var(--text-muted)" font-family="IBM Plex Mono,monospace">${
              String(t.getHours()).padStart(2,"0")}:${String(t.getMinutes()).padStart(2,"0")}</text>`;
  }
  const paths = series.map(s=>{
    const off = n - s.v.length;
    const d = s.v.map((v,i)=>`${i?"L":"M"}${X(i+off).toFixed(1)},${Y(v).toFixed(1)}`).join("");
    return `<path id="ln-${s.c.slot}" d="${d}" fill="none" stroke="var(--series-${s.c.slot})"
                  stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");
  // Reuse the existing SVG when nothing structural changed - rebuilding it each
  // tick is what made the page flicker and jump.
  const sig = [W, H, hi, n, series.map(s=>s.c.slot).join()].join("|");
  const existing = box.querySelector("svg");
  if(existing && box.dataset.sig === sig){
    series.forEach(s=>{
      const off = n - s.v.length;
      const d = s.v.map((v,i)=>`${i?"L":"M"}${X(i+off).toFixed(1)},${Y(v).toFixed(1)}`).join("");
      existing.querySelector(`#ln-${s.c.slot}`)?.setAttribute("d", d);
    });
    // only the clock labels move
    existing.querySelectorAll(".xl").forEach((t,k)=>{
      const i = Math.round(k*(n-1)/5), d2 = new Date(now-(n-1-i)*1000);
      t.textContent = `${String(d2.getHours()).padStart(2,"0")}:${String(d2.getMinutes()).padStart(2,"0")}`;
    });
    return;
  }
  box.dataset.sig = sig;
  existing?.remove();
  box.insertAdjacentHTML("afterbegin",
    `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img"
          aria-label="Temperatures over the last ${Math.round(n/60)} minutes">
       ${grid}${ylab}${xlab}${paths}
       <line id="cross" x1="0" y1="${T}" x2="0" y2="${H-B}" stroke="var(--text-muted)"
             stroke-width="1" opacity="0"/>
       <rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="transparent" id="hit"/>
     </svg>`);
  const svg=box.querySelector("svg"), tip=el("ctip"), cross=svg.querySelector("#cross");
  svg.addEventListener("pointermove", e=>{
    const r=svg.getBoundingClientRect();
    const px=(e.clientX-r.left)/r.width*W;
    if(px<L||px>W-R){ tip.style.opacity=0; cross.setAttribute("opacity",0); return; }
    const i=Math.round((px-L)/(W-L-R)*(n-1));
    cross.setAttribute("opacity",".5");
    cross.setAttribute("x1",X(i)); cross.setAttribute("x2",X(i));
    const t=new Date(now-(n-1-i)*1000);
    tip.innerHTML = `<b>${String(t.getHours()).padStart(2,"0")}:${
      String(t.getMinutes()).padStart(2,"0")}:${String(t.getSeconds()).padStart(2,"0")}</b><br>`
      + series.map(s=>{
          const off=n-s.v.length, v=s.v[i-off];
          return v==null?"":`<i style="background:var(--series-${s.c.slot})"></i>${
            s.c.name} ${v.toFixed(1)}°`;
        }).filter(Boolean).join("<br>");
    tip.style.opacity=1;
    const lx=(e.clientX-r.left);
    tip.style.left=Math.min(Math.max(lx+14,0), r.width-tip.offsetWidth-4)+"px";
    tip.style.top="8px";
  });
  svg.addEventListener("pointerenter",()=> hovering = true);
  svg.addEventListener("pointerleave",()=>{ hovering = false;
    tip.style.opacity=0; cross.setAttribute("opacity",0); });
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
    clock = (ps.state === "printing")
      ? {elapsed: el_, remaining: rem, at: Date.now()}
      : null;
    if(!clock) el("times").textContent = `${hm(el_)} elapsed`;
    drawClock();
    pushSample(s);
    if(!hovering){ try{ drawChart(); drawSparks(); }catch(e){} }
    el("z").textContent   = (ps.z_pos!=null? ps.z_pos.toFixed(2):"—")+" mm";
    el("fil").textContent = ((ps.filament_used||0)/1000).toFixed(2)+" m";
    el("spd").textContent = Math.round((gm.speed||0)/60)+" mm/s";
    el("flow").textContent= Math.round((gm.speed_factor||1)*100)+"%";

    let dev=false;
    CH.forEach(c=>{
      const o=s[c.key]||{};
      if(o.temperature!=null)
        el("ac-"+c.slot).innerHTML = o.temperature.toFixed(1)+'<span class="deg"> °C</span>';
      // heaters report power 0..1; a temperature_fan reports speed instead
      const pw = (o.power!=null) ? o.power : o.speed;
      el("pw-"+c.slot).textContent = (pw!=null) ? Math.round(pw*100)+"%" : "";
      const sl = slope((store[c.key]||{}).temperatures);
      el("ch-"+c.slot).textContent = (sl>=0?"+":"")+sl.toFixed(1)+" °C/s";
      const inp = c.set ? el("tg-"+c.slot) : null;
      if(inp){
        inp.disabled = !CONTROL_ON;
        // do not fight the user while they are typing in it
        if(document.activeElement!==inp && inp.dataset.dirty!=="1")
          inp.value = (o.target!=null) ? Math.round(o.target) : "";
      }
      if(o.target>0 && Math.abs(o.temperature-o.target)>8) dev=true;
    });
    const st=ps.state||"—";
    el("statetx").textContent = st + (dev?" · temp off target":"");
    el("state").style.color = statusColor(st, dev);
  }catch(e){
    lastErr = (e && e.message) ? e.message : String(e);
    if(++misses < MISSES_BEFORE_DISCONNECTED) return;   // one blip is not a fault
    el("statetx").textContent="proxy not running";
    el("state").style.color="var(--crit)";
    const sp = document.getElementById("setup");
    if(sp) sp.classList.remove("ok");
    if(misses === MISSES_BEFORE_DISCONNECTED) showDiagnosis();
  }
}

async function tickTemps(){
  // No silent catch. A failure in here used to leave the charts simply blank,
  // which is indistinguishable from "it did not deploy" - the worst way for a
  // panel to fail.
  // full re-sync; between these the buffer is kept up to date by the status poll
  try { store = await get("temps"); }
  catch(e){ chartFail("cannot reach the proxy for history"); return; }
  try { drawChart(); }  catch(e){ chartFail("chart: "+e.message); }
  try { drawSparks(); } catch(e){ chartFail("panels: "+e.message, "sparks"); }
}
function chartFail(text, where){
  const box = el(where === "sparks" ? "sparks" : "chart");
  if(box) box.innerHTML = '<p class="failmsg">'+text+'</p>';
  console.error(text);
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

// ---- controls -------------------------------------------------------------
// Every write carries X-K2-Token. That header is what makes this safe to leave
// listening: CORS would not stop a hostile page POSTing here, but a custom
// header forces a preflight that only the allowed origins pass.
let CONTROL_ON = false;
let TOKEN = localStorage.getItem("k2token") || "";
el("tok").value = TOKEN;
function msg(t, cls){ const m=el("ctlmsg"); m.textContent=t; m.className="msg "+(cls||""); }
el("savetok").onclick = () => {
  TOKEN = el("tok").value.trim(); localStorage.setItem("k2token", TOKEN);
  msg(TOKEN ? "token saved" : "token cleared", "ok");
};
async function send(action, body, extra){
  if(!TOKEN){ msg("enter the token printed by the server", "err"); return null; }
  try{
    const r = await fetch(PROXY+"/api/control/"+action, {method:"POST",
      headers:Object.assign({"Content-Type":"application/json","X-K2-Token":TOKEN}, extra||{}),
      body: body});
    const j = await r.json().catch(()=>({}));
    if(!r.ok){ msg(j.error || ("HTTP "+r.status), "err"); return null; }
    return j;
  }catch(e){ msg("proxy unreachable", "err"); return null; }
}
el("b-pause").onclick  = async () => { const j=await send("pause");  if(j) msg("paused","ok"); };
el("b-resume").onclick = async () => { const j=await send("resume"); if(j) msg("resumed","ok"); };
el("b-cancel").onclick = async () => {
  const f = el("fname").textContent;
  if(!confirm("Cancel the running print?\n\n"+f+"\n\nThis cannot be undone.")) return;
  const j=await send("cancel"); if(j) msg("print cancelled","ok");
};
el("b-upload").onclick = async () => {
  const f = el("gfile").files[0];
  if(!f){ msg("choose a .gcode file", "err"); return; }
  if(!f.name.endsWith(".gcode")){ msg("that is not a .gcode file", "err"); return; }
  const start = el("startnow").checked;
  if(start && !confirm("Upload and START printing?\n\n"+f.name)) return;
  msg("uploading "+(f.size/1048576).toFixed(1)+" MB ...");
  const j = await send("upload", await f.arrayBuffer(),
    {"Content-Type":"application/octet-stream",
     "X-K2-Filename":encodeURIComponent(f.name), "X-K2-Start":start?"1":"0"});
  if(j) msg("uploaded "+j.uploaded+(j.started?" and started":""), "ok");
};

get("info").then(i=>{
  el("host").textContent=i.hostname+" · "+i.software_version;
}).catch(()=>{});
// only reveal the panel if this proxy actually has control enabled
fetch(PROXY+"/api/capabilities").then(r=>r.json()).then(c=>{
  CONTROL_ON = !!c.control;
  if(CONTROL_ON) el("controls").hidden = false;
}).catch(()=>{});
document.getElementById("grant")?.addEventListener("click", requestAccess);
showPermState();
buildTable(); buildSparks(); tick(); tickTemps();
function drawClock(){
  if(!clock){ return; }
  const d = (Date.now() - clock.at)/1000;
  const parts = [`${hms(clock.elapsed + d)} elapsed`];
  if(clock.remaining != null){
    const left = clock.remaining - d;
    parts.push(left > 0 ? `~${hms(left)} remaining`
                        : `overdue by ${hms(-left)}`);
    const done = new Date(Date.now() + Math.max(0,left)*1000);
    parts.push(`done ~${String(done.getHours()).padStart(2,"0")}:${
                        String(done.getMinutes()).padStart(2,"0")}`);
  }
  el("times").textContent = parts.join(" · ");
}
setInterval(drawClock, 1000);

let _rz; addEventListener("resize", () => {
  clearTimeout(_rz); _rz = setTimeout(()=>{ drawChart(); drawSparks(); }, 150);
});
// Back off when it is failing. A fixed 1 Hz interval kept firing into a
// blocked endpoint and logged a browser warning every single second - dozens
// within a minute. Poll fast while healthy, slow down when not.
let pollDelay = 1000;
(function loop(){
  tick().finally(()=>{
    pollDelay = (misses === 0) ? 1000 : Math.min(pollDelay * 2, 30000);
    setTimeout(loop, pollDelay);
  });
})();
setInterval(()=>{ if(misses === 0) tickTemps(); }, 30000);
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin in ORIGINS:                 # exact match, never a wildcard
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-K2-Token, X-K2-Filename, X-K2-Start")
        # PRIVATE NETWORK ACCESS. A page on a public https origin reaching a
        # private/loopback address gets an extra preflight from Chrome carrying
        # Access-Control-Request-Private-Network. Without this acknowledgement
        # the request is simply blocked - which is why the hosted page kept
        # flashing "not connected" while localhost was perfectly happy.
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")   # stop re-preflighting
        self._cors(); self.end_headers()

    def _authed(self):
        return CONTROL and secrets.compare_digest(
            self.headers.get("X-K2-Token", ""), TOKEN)

    def _moonraker(self, path, data=None, ctype=None):
        req = urllib.request.Request(MOONRAKER + path, data=data, method="POST")
        if ctype: req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/camera/offer":
            return self._camera_offer()
        if not self.path.startswith("/api/control/"):
            self.send_error(403, "not an allowed endpoint"); return
        if not CONTROL:
            self._json(403, {"error": "control disabled - start with K2_CONTROL=1"}); return
        if not self._authed():
            self._json(401, {"error": "bad or missing X-K2-Token"}); return

        action = self.path[len("/api/control/"):]
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            if action == "temp":
                b = json.loads(raw or b"{}")
                heater, target = b.get("heater"), float(b.get("target", 0))
                if heater not in LIMITS:
                    return self._json(400, {"error": f"unknown heater {heater!r}"})
                cap = LIMITS[heater]
                if not (0 <= target <= cap):
                    return self._json(400,
                        {"error": f"{heater} target must be 0..{cap:.0f}, got {target:.0f}"})
                g = SETTER[heater].format(t=target)
                self._moonraker("/printer/gcode/script?" +
                                urllib.parse.urlencode({"script": g}))
                return self._json(200, {"ok": True, "sent": g})

            if action in ("pause", "resume", "cancel"):
                self._moonraker(f"/printer/print/{action}")
                return self._json(200, {"ok": True, "action": action})

            if action == "upload":
                name = urllib.parse.unquote(self.headers.get("X-K2-Filename", "") or "")
                if not name.endswith(".gcode") or "/" in name or "\\" in name:
                    return self._json(400, {"error": "expected a bare *.gcode filename"})
                if len(raw) > 512 * 1024 * 1024:
                    return self._json(413, {"error": "file too large"})
                start = self.headers.get("X-K2-Start") == "1"
                bnd = uuid.uuid4().hex
                body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; "
                        f"filename=\"{name}\"\r\nContent-Type: application/octet-stream"
                        f"\r\n\r\n").encode() + raw + f"\r\n--{bnd}--\r\n".encode()
                out = self._moonraker("/server/files/upload", body,
                                      f"multipart/form-data; boundary={bnd}")
                if start:
                    self._moonraker("/printer/print/start?" +
                                    urllib.parse.urlencode({"filename": name}))
                return self._json(200, {"ok": True, "uploaded": name, "started": start,
                                        "moonraker": json.loads(out or b"{}")})

            return self._json(403, {"error": f"unknown action {action!r}"})
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": f"moonraker {e.code}: {e.read()[:200].decode(errors='replace')}"})
        except Exception as e:
            return self._json(502, {"error": str(e)})

    def _camera_offer(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 64 * 1024:
                self.send_error(413, "offer too large"); return
            req = urllib.request.Request(CAMERA_SIGNAL, data=self.rfile.read(n),
                                         headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self._cors(); self.end_headers(); self.wfile.write(data)
        except Exception as e:
            self.send_error(502, str(e))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (PAGE.replace("__CAMERA__", CAMERA)
                        .replace("__BUILD__", "local " + BUILD)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # the page is regenerated whenever this file changes, so never let a
            # browser hold a stale copy - that looks exactly like "it did not
            # deploy", and costs a confusing round of head-scratching
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/api/capabilities":
            # says WHETHER control is on. Never the token.
            self._json(200, {"control": CONTROL,
                             "limits": LIMITS if CONTROL else {}})
            return
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
                self._cors()
                self.end_headers(); self.wfile.write(data)
            except Exception as e:
                self.send_error(502, str(e))
            return
        self.send_error(404)


if __name__ == "__main__":
    # THREADING, not the plain TCPServer this used to be. Single-threaded, one
    # slow upstream call (a 70 KB /api/temps, or the camera handshake) blocked
    # every other request - so at 1 Hz the next poll would fail and the page
    # would flip to "not connected" and back.
    class Srv(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    with Srv(("127.0.0.1", PORT), H) as srv:
        banner = [f"dashboard  http://localhost:{PORT}",
                  f"printer    {MOONRAKER}   camera {CAMERA}",
                  "read endpoints:  " + ", ".join(sorted(ALLOWED))]
        if CONTROL:
            banner += ["",
                       "CONTROL IS ON - this instance can set heaters and stop prints.",
                       f"  token  {TOKEN}",
                       "  paste that into the dashboard's Controls panel once.",
                       f"  limits nozzle<={LIMITS['extruder']:.0f}  "
                       f"bed<={LIMITS['heater_bed']:.0f}  "
                       f"chamber<={LIMITS['chamber_heater']:.0f}"]
        else:
            banner += ["control:         OFF (read-only). Enable with K2_CONTROL=1"]
        # flush: under systemd or any redirect stdout is block-buffered, and the
        # token would sit in the buffer unseen - which is the one line you need.
        print("\n".join(banner), flush=True)

        try: srv.serve_forever()
        except KeyboardInterrupt: print("\nstopped")

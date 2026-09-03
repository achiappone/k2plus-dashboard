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
import secrets, uuid, smtplib, ssl, threading, time, re, base64
from email.message import EmailMessage

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
# The MJPEG relay: a headless Chromium beside the printer runs ONE WebRTC
# session and re-serves frames as JPEG. Proxied through here so the page is
# same-origin, which keeps the tunnel to a single ingress rule and means the
# printer encodes for one consumer no matter how many people are watching.
CAM_RELAY = os.environ.get("K2_CAM_RELAY", "http://127.0.0.1:8771")

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

# Login. A session cookie replaces the pasted token: the browser sends it on
# every request by itself, so the page needs no token plumbing at all.
# Sessions live in memory - restarting the server logs everyone out, which is
# the right trade for not having to persist anything.
AUTH_USER = os.environ.get("K2_USER", "")
AUTH_PASS = os.environ.get("K2_PASS", "")
LOGIN_REQUIRED = bool(AUTH_USER and AUTH_PASS)
SESSIONS = set()

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
    # Klipper's console scrollback. Moonraker keeps a rolling buffer; this is
    # where a shutdown reason actually shows up, which the status endpoints
    # only summarise.
    "gcode":   f"{MOONRAKER}/server/gcode_store?count=200",
    # The CFS inventory. This lives in Klipper, not on Creality's private
    # channel on 9999 - that one only carries summary fields like cfsConnect.
    "box":     (f"{MOONRAKER}/printer/objects/query?box"
                "&filament_switch_sensor%20filament_sensor"),
    "files":   f"{MOONRAKER}/server/files/list?root=gcodes",
    "history": f"{MOONRAKER}/server/history/list?limit=12&order=desc",
}

# ---------------------------------------------------------------- alerts
# Email when a print goes wrong. Email rather than SMS on purpose: SMS needs a
# paid gateway, and the one free route - carrier email-to-SMS - is being shut
# down carrier by carrier, so alerts would fail silently, which is worse than
# having none. smtplib is stdlib, so this adds no dependency.
ALERT_FILE = os.environ.get("K2_ALERT_FILE", "/var/lib/k2dash/alerts.json")
ALERT_POLL = int(os.environ.get("K2_ALERT_POLL", "20"))      # seconds
SMTP_HOST  = os.environ.get("K2_SMTP_HOST", "")
SMTP_PORT  = int(os.environ.get("K2_SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("K2_SMTP_USER", "")
SMTP_PASS  = os.environ.get("K2_SMTP_PASS", "")
SMTP_FROM  = os.environ.get("K2_SMTP_FROM", SMTP_USER or "k2@localhost")
SMTP_TLS   = os.environ.get("K2_SMTP_TLS", "1") == "1"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_recipients():
    try:
        with open(ALERT_FILE) as f:
            return [a for a in json.load(f).get("recipients", []) if EMAIL_RE.match(a)]
    except Exception:
        return []


def save_recipients(addrs):
    addrs = [a.strip() for a in addrs if EMAIL_RE.match(a.strip())][:25]
    d = os.path.dirname(ALERT_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = ALERT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"recipients": addrs}, f)
    os.replace(tmp, ALERT_FILE)          # atomic, so a crash cannot truncate the list
    return addrs


def send_mail(subject, body):
    """Returns None on success, else a string describing what went wrong."""
    to = load_recipients()
    if not to:
        return "no recipients saved"
    if not SMTP_HOST:
        return "K2_SMTP_HOST is not set"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
            if SMTP_TLS:
                srv.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def fire_gcode(script):
    """Run a long gcode without holding the HTTP request open.

    /printer/gcode/script does not return until the script finishes, and G29 is
    two to four minutes - long enough that a normal request would time out and
    report failure while the mesh was still running. Progress shows up in the
    console either way.
    """
    try:
        req = urllib.request.Request(
            MOONRAKER + "/printer/gcode/script?" +
            urllib.parse.urlencode({"script": script}), data=b"", method="POST")
        urllib.request.urlopen(req, timeout=900).read()
        print(f"gcode {script!r} finished", flush=True)
    except Exception as e:
        print(f"gcode {script!r} failed: {e}", flush=True)


def alert_watcher():
    """Fire on TRANSITIONS only, so a stuck error state does not mail forever."""
    last = None
    while True:
        try:
            st = _fetch(ALLOWED["status"])["result"]["status"]
            info = _fetch(ALLOWED["info"])["result"]
            ps = st.get("print_stats", {}) or {}
            cur = (ps.get("state"), info.get("state"))
            if last is not None and cur != last:
                why = None
                if cur[0] == "error" and last[0] != "error":
                    why = "Print error"
                elif cur[0] == "cancelled" and last[0] != "cancelled":
                    why = "Print cancelled"
                elif cur[1] != "ready" and last[1] == "ready":
                    why = f"Klipper {cur[1]}"
                if why:
                    detail = (ps.get("message") or info.get("state_message") or "").strip()
                    body = "\n".join([
                        f"{why} on {info.get('hostname', 'the printer')}.",
                        "",
                        f"file:      {ps.get('filename') or '(none)'}",
                        f"print:     {ps.get('state')}",
                        f"klipper:   {info.get('state')}",
                        f"elapsed:   {int(ps.get('print_duration') or 0)}s",
                        f"detail:    {detail or '(none given)'}",
                    ])
                    err = send_mail(f"[K2] {why}", body)
                    print(f"alert: {why} -> " + (err or f"sent to {len(load_recipients())}"),
                          flush=True)
            last = cur
        except Exception:
            pass                          # printer unreachable is not itself an alert
        time.sleep(ALERT_POLL)


LOGIN_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>K2 Plus</title>
<style>
body{margin:0;background:#0a0f16;color:#fff;font-family:system-ui,sans-serif;
  display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#1a1a19;border:1px solid #2b333d;padding:26px 28px;width:300px}
h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 18px;color:#c3c2b7}
input{width:100%;box-sizing:border-box;background:#0a0f16;color:#fff;border:1px solid #2b333d;
  padding:9px 10px;margin-bottom:11px;font-size:14px}
button{width:100%;background:#3987e5;color:#fff;border:0;padding:10px;font-size:14px;cursor:pointer}
p{color:#e07a72;font-size:13px;min-height:18px;margin:10px 0 0}
</style></head><body>
<form id="f"><h1>K2 Plus</h1>
<input id="u" placeholder="username" autocomplete="username" autofocus>
<input id="p" type="password" placeholder="password" autocomplete="current-password">
<button>Sign in</button><p id="e"></p></form>
<script>
document.getElementById("f").onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch("/api/login", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({u:document.getElementById("u").value,
                          p:document.getElementById("p").value})});
  if (r.ok) location.reload();
  else document.getElementById("e").textContent = "wrong username or password";
};
</script></body></html>"""

# ---------------------------------------------------------------- thumbnails
# Moonraker reports no thumbnails for these files - its metadata scan is not
# running on this printer (estimated_time and filament_total come back None
# too). The images ARE in the gcode though, so parse them out directly.
# A ranged request keeps this cheap: the thumbnail lives in the header, so
# there is no reason to pull 67 MB of a large file to reach a 23 KB PNG.
THUMB_BYTES = int(os.environ.get("K2_THUMB_BYTES", "400000"))
# One cached read per file serves both the thumbnail and the estimated time -
# they live in the same header, so fetching twice would be wasteful.
META_CACHE = {}


def gcode_meta(name):
    """{"png": bytes|None, "minutes": int|None} for a stored gcode.

    Estimated time comes from the first `M73 P0 R<minutes>` line. Moonraker
    would normally supply this, but its metadata scan is not running on this
    printer, so estimated_time comes back None for every file.
    """
    if name in META_CACHE:
        return META_CACHE[name]
    out = {"png": None, "minutes": None}
    try:
        url = MOONRAKER + "/server/files/gcodes/" + urllib.parse.quote(name)
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{THUMB_BYTES}"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
        out["png"] = extract_thumbnail(data)
        m = re.search(rb"^M73 P\d+ R(\d+)", data, re.M)
        if m:
            out["minutes"] = int(m.group(1))
    except Exception:
        pass
    META_CACHE[name] = out          # cache misses too, do not retry every poll
    return out


def extract_thumbnail(data):
    """Largest embedded PNG, or None.

    Creality Print wraps these in THUMBNAIL_BLOCK_START/END, but the payload is
    the ordinary slicer form: base64 split across comment lines between
    `; thumbnail begin <W>x<H> <bytes>` and `; thumbnail end`.
    """
    best_px, best_png, cur = -1, None, None
    for line in data.split(b"\n"):
        line = line.strip()
        if line.startswith(b"; thumbnail begin"):
            try:
                w, h = line.split()[3].split(b"x")
                cur = (int(w) * int(h), [])
            except Exception:
                cur = None
        elif line.startswith(b"; thumbnail end"):
            if cur:
                try:
                    png = base64.b64decode(b"".join(cur[1]))
                    if cur[0] > best_px:
                        best_px, best_png = cur[0], png
                except Exception:
                    pass
            cur = None
        elif cur is not None and line.startswith(b"; "):
            cur[1].append(line[2:])
    return best_png


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
@media(max-width:900px){
  /* One column, and `display:contents` on the two column wrappers promotes the
     cards themselves to flex items so they can be interleaved. Without it the
     camera could only ever sit after the whole left-hand column. */
  .grid{grid-template-columns:1fr;display:flex;flex-direction:column;gap:20px}
  .grid > div{display:contents}
  .grid .card{order:3}
  .grid #progresscard{order:1}
  .grid #camcard{order:2}
  /* .camfoot is not a .card, so without this it kept order:0 and the camera's
     buttons floated above everything, detached from the image they belong to. */
  .grid .camfoot{order:2;margin-top:0}
  /* Compact the progress card. On a phone the big readout and a 2x2 tile block
     ate most of the first screen before the camera came into view, and none of
     that space was carrying information. */
  #progresscard{padding:10px 12px}
  #progresscard h2{margin:0 0 8px}
  #progresscard .hero{gap:9px;margin-bottom:0;align-items:baseline}
  #progresscard .hero .big{font-size:26px;min-width:4.2ch}
  #progresscard .hero .sub{font-size:11px;padding-bottom:0}
  #progresscard .bar{height:5px;margin:8px 0 2px}
  #progresscard .small{font-size:11px}
  /* Four across instead of 2x2: halves the height, and these values are short
     enough to stay legible. */
  #progresscard .tiles{grid-template-columns:repeat(4,1fr);margin-top:9px}
  #progresscard .tile{padding:7px 6px;text-align:center}
  #progresscard .tile .k{font-size:9px;letter-spacing:.06em}
  #progresscard .tile .v{font-size:12px;margin:3px 0 0}
}
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
#cfs,#files,#history{margin-top:20px}
.hrow{background:var(--surface-1);padding:9px 13px;display:flex;gap:12px;align-items:center}
.hrow .hwhen{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted);
  white-space:nowrap;font-variant-numeric:tabular-nums}
.hrow .hst{font-size:11px;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;
  min-width:74px}
.hrow .hfn{flex:1;min-width:0;font-size:13px;word-break:break-all}
.hrow .hdur{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted);
  white-space:nowrap;font-variant-numeric:tabular-nums}
.hrow button{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:12px;
  background:var(--surface-1);color:var(--text-primary);border:1px solid var(--rule);
  padding:5px 11px;cursor:pointer;white-space:nowrap}
.hrow button:hover:not(:disabled){border-color:var(--series-1)}
.hrow button:disabled{opacity:.35;cursor:not-allowed}
.st-completed{color:var(--good)} .st-cancelled{color:var(--warn)}
.st-error{color:var(--crit)} .st-klippy_shutdown{color:var(--crit)}
#cfs,#files{margin-top:20px}
.slots{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.slot{background:var(--surface-1);padding:12px 14px}
.slot .lbl{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-muted)}
.slot .mat{display:flex;align-items:center;gap:8px;margin:7px 0 9px;font-size:14px}
.swatch{width:15px;height:15px;border:1px solid var(--rule);flex:none}
.slot .rem{height:5px;background:var(--rule-2);overflow:hidden}
.slot .rem i{display:block;height:100%;background:var(--series-3)}
.slot .pct{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-secondary);
  margin-top:5px;font-variant-numeric:tabular-nums}
.slot.empty{opacity:.45}
.slot.ext{border-left:2px solid var(--rule)}
.slot .st{font-size:12px;color:var(--text-secondary)}
.slot .st.on{color:var(--good)}
.slot .warn{font-size:11px;color:var(--warn);margin:6px 0 0}
.flist{display:grid;gap:1px;background:var(--rule);border:1px solid var(--rule)}
.frow{background:var(--surface-1);padding:9px 13px;display:flex;justify-content:space-between;
  gap:14px;align-items:baseline}
.frow{align-items:center}
.frow .thumb{width:52px;height:52px;flex:none;background:var(--bg);
  border:1px solid var(--rule);object-fit:contain}
.frow .meta{flex:1;min-width:0;display:flex;justify-content:space-between;
  gap:14px;align-items:baseline}
.frow .fn{font-size:13px;word-break:break-all}
.frow .fm{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted);
  white-space:nowrap;font-variant-numeric:tabular-nums}
#console{margin-top:20px}
#conwrap{background:var(--bg);border:1px solid var(--rule);height:300px;overflow-y:auto;
  padding:10px 12px;font-family:"IBM Plex Mono",monospace;font-size:12.5px;line-height:1.55}
#conwrap div{white-space:pre-wrap;word-break:break-word}
.c-cmd{color:var(--series-1)}
.c-err{color:var(--crit)}
.c-note{color:var(--text-muted)}
.c-out{color:var(--text-secondary)}
#alerts{margin-top:20px}
#alerts textarea{width:100%;background:var(--bg);color:var(--text-primary);
  border:1px solid var(--rule);padding:9px 10px;font-size:13px;line-height:1.6;
  font-family:"IBM Plex Mono",monospace;resize:vertical}
#alerts textarea:focus{outline:none;border-color:var(--series-1)}
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
    <div class="card" id="progresscard">
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
      <p class="note" style="margin:0 0 4px">Set temperatures in the
      <b>Target</b> column of the Thermals table &mdash; type a value and press Enter.</p>
      <div class="ctlrow">
        <button id="b-homexy">Home XY</button>
        <button id="b-homez">Home Z</button>
        <button id="b-homeall">Home all</button>
        <button id="b-mesh">Run bed mesh</button>
      </div>
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
  </div>
  <div>
    <div class="card" id="camcard" style="padding:0">
      <div class="camwrap"><img class="cam" id="cam" alt="printer camera"></div>
    </div>
    <div class="camfoot">
      <span class="pill" id="campill"><span class="dot"></span><span id="camtx">connecting</span></span>
      <button id="grab">Save still</button>
      <button id="recon">Reconnect</button>
    </div>
  </div>
</div>

<div class="card" id="cfs">
  <h2>Filament system</h2>
  <p class="note" style="margin:-6px 0 12px" id="cfsnote">reading...</p>
  <div class="slots" id="slots"></div>
</div>

<div class="card" id="files">
  <h2>Stored prints</h2>
  <p class="note" style="margin:-6px 0 12px">On the printer, newest first. Read-only:
  starting a print from here is deliberately not wired up yet.</p>
  <div class="flist" id="flist"></div>
</div>

<div class="card" id="history">
  <h2>Recent jobs</h2>
  <p class="note" style="margin:-6px 0 12px" id="histnote">Re-run starts the job
  immediately on the printer. It is refused while a print is running or Klipper is
  not ready.</p>
  <div class="flist" id="hlist"></div>
</div>

<div class="card" id="console">
  <h2>Klipper console</h2>
  <p class="note" style="margin:-6px 0 12px">The last 200 lines from Klipper.
  Shutdown reasons appear here in full, where the status endpoints only summarise them.</p>
  <div id="conwrap"><div class="c-note">loading...</div></div>
</div>

<div class="card" id="alerts">
  <h2>Failure alerts</h2>
  <p class="note" style="margin:-6px 0 12px">Email these addresses when a print errors, is
  cancelled, or Klipper shuts down. One per line.</p>
  <textarea id="alertlist" rows="4" spellcheck="false" autocomplete="off"
            placeholder="you@example.com"></textarea>
  <div class="ctlrow" style="margin-top:10px">
    <button id="savealerts">Save</button>
    <button id="testalert">Send test</button>
    <span class="msg" id="alertmsg"></span>
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
    // Moonraker's temperature_store returns different shapes per object type:
    // heaters and temperature_fans carry targets, a plain temperature_sensor
    // carries only temperatures. The old fallback only fired when the key was
    // absent entirely, so a pre-populated sensor entry reached d.targets.push()
    // with targets undefined and threw, aborting the whole tick.
    const d = store[c.key] || (store[c.key] = {});
    if(!d.temperatures) d.temperatures = [];
    if(!d.targets)      d.targets      = [];
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
const CAM = "";
// Frames come from the relay in this container, not from WebRTC in your browser.
// WebRTC could never work off-LAN here: the printer offers only STUN, so there
// is no reachable media path from outside and the video stayed black. The relay
// runs the WebRTC leg next to the printer and re-serves MJPEG, which tunnels
// fine - and means the printer encodes once, not once per viewer.
function camState(t,c){ el("camtx").textContent=t; el("campill").style.color=c; }
function connectCam(){
  camState("connecting","var(--text-muted)");
  const img = el("cam");
  img.onload  = () => camState("live","var(--good)");
  img.onerror = () => camState("no stream","var(--crit)");
  img.src = CAM + "/camera/stream?t=" + Date.now();   // cache-bust to force a restart
}
el("recon").onclick = connectCam;
el("grab").onclick = async () => {
  try{
    const r = await fetch(CAM + "/camera/snapshot?t=" + Date.now());
    if(!r.ok) throw new Error("no frame available");
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.download = `k2-${new Date().toISOString().replace(/[:.]/g,"-")}.jpg`;
    a.href = url; a.click();
    setTimeout(()=>URL.revokeObjectURL(url), 5000);
  }catch(e){ camState("grab failed","var(--crit)"); }
};
connectCam();

// ---- controls -------------------------------------------------------------
// Every write carries X-K2-Token. That header is what makes this safe to leave
// listening: CORS would not stop a hostile page POSTing here, but a custom
// header forces a preflight that only the allowed origins pass.
let CONTROL_ON = false;
function msg(t, cls){ const m=el("ctlmsg"); m.textContent=t; m.className="msg "+(cls||""); }
async function send(action, body, extra){
  try{
    const r = await fetch(PROXY+"/api/control/"+action, {method:"POST",
      headers:Object.assign({"Content-Type":"application/json"}, extra||{}),
      body: body});
    if(r.status === 401){ location.reload(); return null; }
    const j = await r.json().catch(()=>({}));
    if(!r.ok){ msg(j.error || ("HTTP "+r.status), "err"); return null; }
    return j;
  }catch(e){ msg("proxy unreachable", "err"); return null; }
}
async function home(axes, label){
  const j = await send("home", JSON.stringify({axes}));
  if(j) msg(`homing ${label}`, "ok");
}
el("b-mesh").onclick = async () => {
  if(!confirm("Run a bed mesh probe now?\n\nThe toolhead will move for 2-4 minutes.")) return;
  const j = await send("mesh", "{}");
  if(j) msg("bed mesh running - watch the console", "ok");
};
el("b-homexy").onclick  = () => home("XY", "X and Y");
el("b-homez").onclick   = () => home("Z", "Z");
el("b-homeall").onclick = () => home("ALL", "all axes");
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

// ---- filament system ------------------------------------------------------
// Slots are T1A..T1D on box T1; the arrays in the box object are positional, so
// index 3 is the fourth spool. material_type holds a Creality code, and the
// only place those codes are spelled out is same_material, so build the lookup
// from there rather than hardcoding a table that would rot.
const SLOTLBL = ["A", "B", "C", "D"];
function colourOf(v){
  // "0FFFFFF" - a leading flag digit then six hex digits.
  const h = String(v || "").slice(-6);
  return /^[0-9a-fA-F]{6}$/.test(h) ? "#" + h : "transparent";
}
async function tickCfs(){
  try{
    const r = await fetch(PROXY + "/api/box");
    if(r.status === 401){ location.reload(); return; }
    const st = (await r.json()).result.status || {};
    const b = st.box || {};
    const names = {};
    for(const g of (b.same_material || [])) names[g[0]] = g[3];
    const box = b.T1 || {};
    const connected = b.state === "connect";
    el("cfsnote").textContent = connected
      ? `Box T1 connected · ${b.auto_refill == 1 ? "auto-refill on" : "auto-refill off"}`
      : "No filament system detected";
    const wrap = el("slots");
    wrap.innerHTML = "";
    for(let i = 0; i < 4; i++){
      const mat = (box.material_type || [])[i];
      const rem = parseInt((box.remain_len || [])[i], 10);
      const has = mat && mat !== "-1";
      const d = document.createElement("div");
      d.className = "slot" + (has ? "" : " empty");
      d.innerHTML =
        `<p class="lbl">Slot ${i + 1} &middot; T1${SLOTLBL[i]}</p>` +
        `<div class="mat"><span class="swatch" style="background:${
          has ? colourOf((box.color_value || [])[i]) : "transparent"}"></span>` +
        `<span>${has ? (names[mat] || mat) : "empty"}</span></div>` +
        (has && !isNaN(rem)
          ? `<div class="rem"><i style="width:${Math.max(0, Math.min(100, rem))}%"></i></div>` +
            `<p class="pct">${rem}% remaining</p>`
          : `<p class="pct">&mdash;</p>`);
      wrap.appendChild(d);
    }

    // The side spool has no sensor of its own - the machine has exactly one,
    // on the extruder. So this is inferred: filament at the extruder while the
    // CFS reports none engaged means it is being fed from the external path.
    const fs = st["filament_switch_sensor filament_sensor"] || {};
    const atExtruder = fs.filament_detected === true;
    const cfsEngaged = box.filament && box.filament !== "None";
    const usingExt = atExtruder && !cfsEngaged;
    const e = document.createElement("div");
    e.className = "slot ext" + (atExtruder ? "" : " empty");
    e.innerHTML =
      `<p class="lbl">External &middot; side spool</p>` +
      `<div class="mat"><span class="swatch" style="background:transparent"></span>` +
      `<span class="st${usingExt ? " on" : ""}">${
        usingExt ? "feeding the extruder"
                 : atExtruder ? "filament present" : "not detected"}</span></div>` +
      `<p class="pct">inferred, not measured</p>` +
      (fs.enabled === false
        ? `<p class="warn">runout sensor disabled</p>` : "");
    wrap.appendChild(e);
  }catch(e){ /* the status poll already reports connectivity */ }
}

// ---- stored prints --------------------------------------------------------
async function tickFiles(){
  try{
    const r = await fetch(PROXY + "/api/files");
    if(r.status === 401){ location.reload(); return; }
    const files = (await r.json()).result || [];
    files.sort((a, b) => (b.modified || 0) - (a.modified || 0));
    const wrap = el("flist");
    wrap.innerHTML = "";
    for(const f of files){
      const d = document.createElement("div");
      d.className = "frow";
      const when = f.modified ? new Date(f.modified * 1000).toLocaleDateString() : "";
      // Hide the frame rather than show a broken-image icon: not every file
      // carries a thumbnail, and an empty square reads better than a glyph.
      d.innerHTML =
        `<img class="thumb" alt="" loading="lazy" onerror="this.style.visibility='hidden'">` +
        `<span class="meta"><span class="fn"></span><span class="fm">${
          (f.size / 1048576).toFixed(1)} MB &middot; ${when}</span></span>`;
      d.querySelector(".fn").textContent = f.path;   // filenames are user data
      d.querySelector(".thumb").src =
        PROXY + "/api/thumb?file=" + encodeURIComponent(f.path);
      // Estimated time is parsed from the file, so fetch it per row rather than
      // blocking the whole list on it. Rows render immediately either way.
      fetch(PROXY + "/api/meta?file=" + encodeURIComponent(f.path))
        .then(r => r.ok ? r.json() : null)
        .then(m => {
          if(m && m.minutes != null){
            const h = Math.floor(m.minutes / 60), mm = m.minutes % 60;
            d.querySelector(".fm").textContent =
              `${h ? h + "h " + String(mm).padStart(2,"0") + "m" : mm + "m"} · ` +
              d.querySelector(".fm").textContent;
          }
        }).catch(() => {});
      wrap.appendChild(d);
    }
    if(!files.length) wrap.innerHTML = '<div class="frow"><span class="fn">no files</span></div>';
  }catch(e){ /* as above */ }
}
tickCfs(); tickFiles();
setInterval(tickCfs, 15000);
setInterval(tickFiles, 60000);

// ---- recent jobs ----------------------------------------------------------
function shortDur(sec){
  const m = Math.round((sec || 0) / 60);
  return m >= 60 ? `${Math.floor(m/60)}h ${String(m%60).padStart(2,"0")}m` : `${m}m`;
}
async function tickHistory(){
  try{
    const r = await fetch(PROXY + "/api/history");
    if(r.status === 401){ location.reload(); return; }
    const jobs = (await r.json()).result.jobs || [];
    const wrap = el("hlist");
    wrap.innerHTML = "";
    for(const j of jobs){
      const d = document.createElement("div");
      d.className = "hrow";
      const when = new Date((j.start_time || 0) * 1000);
      const st = String(j.status || "");
      d.innerHTML =
        `<span class="hwhen">${String(when.getMonth()+1).padStart(2,"0")}-${
          String(when.getDate()).padStart(2,"0")} ${String(when.getHours()).padStart(2,"0")}:${
          String(when.getMinutes()).padStart(2,"0")}</span>` +
        `<span class="hst st-${st}">${st}</span>` +
        `<span class="hfn"></span>` +
        `<span class="hdur">${shortDur(j.total_duration)}</span>` +
        `<button${j.exists ? "" : " disabled title='file no longer on the printer'"}>Re-run</button>`;
      d.querySelector(".hfn").textContent = j.filename || "(unknown)";
      const btn = d.querySelector("button");
      if(j.exists){
        btn.onclick = async () => {
          if(!confirm(`Start this print now?\n\n${j.filename}\n\nThe printer will begin immediately.`)) return;
          btn.disabled = true; btn.textContent = "starting...";
          try{
            await alertsPost("start", {filename: j.filename});
            btn.textContent = "started";
          }catch(e){
            btn.textContent = "Re-run"; btn.disabled = false;
            el("histnote").textContent = String(e.message || e);
          }
        };
      }
      wrap.appendChild(d);
    }
  }catch(e){ /* the status poll already reports connectivity */ }
}
tickHistory();
setInterval(tickHistory, 30000);

// ---- klipper console ------------------------------------------------------
// Klipper marks its own output: "!!" is an error and "//" is an echo/notice.
// Colouring on those prefixes is what makes a shutdown reason findable in 200
// lines of ordinary chatter.
let conSeen = 0, conPinned = true;
const conwrap = el("conwrap");
conwrap.addEventListener("scroll", () => {
  // Stop yanking the view back to the bottom while someone is reading upward.
  conPinned = conwrap.scrollTop + conwrap.clientHeight >= conwrap.scrollHeight - 24;
});
function conClass(m, type){
  if(type === "command")   return "c-cmd";
  if(m.startsWith("!!"))   return "c-err";
  if(m.startsWith("//"))   return "c-note";
  return "c-out";
}
async function tickConsole(){
  try{
    const r = await fetch(PROXY + "/api/gcode");
    if(r.status === 401){ location.reload(); return; }
    const lines = (await r.json()).result.gcode_store || [];
    if(lines.length === conSeen) return;          // nothing new, do not redraw
    conSeen = lines.length;
    conwrap.innerHTML = "";
    for(const l of lines){
      const d = document.createElement("div");
      d.className = conClass(l.message || "", l.type);
      const t = new Date((l.time || 0) * 1000);
      const ts = [t.getHours(), t.getMinutes(), t.getSeconds()]
        .map(n => String(n).padStart(2, "0")).join(":");
      d.textContent = `${ts}  ` + (l.type === "command" ? "> " : "") + (l.message || "");
      conwrap.appendChild(d);
    }
    if(conPinned) conwrap.scrollTop = conwrap.scrollHeight;
  }catch(e){ /* the status poll already reports connectivity */ }
}
tickConsole();
setInterval(tickConsole, 3000);

// ---- failure alerts -------------------------------------------------------
// The address list is token gated on the server, so nothing loads until the
// control token is present. Everything here degrades to a message rather than
// throwing, since a broken handler in this file would take the poll loop down.
const alertMsg = (t, cls) => {
  const e = el("alertmsg"); if(!e) return;
  e.textContent = t || ""; e.className = "msg " + (cls || "");
};
async function alertsLoad(){
  try{
    const r = await fetch("/api/alerts");
    if(r.status === 401){ location.reload(); return; }
    const j = await r.json();
    el("alertlist").value = (j.recipients || []).join("\n");
    alertMsg(j.smtp_configured ? "" : "server has no SMTP configured yet",
             j.smtp_configured ? "" : "err");
  }catch(e){ alertMsg(String(e), "err"); }
}
async function alertsPost(action, body){
  const r = await fetch("/api/control/" + action, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body || {})});
  const j = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
el("savealerts").onclick = async () => {
  const list = el("alertlist").value.split(/[\s,;]+/).filter(Boolean);
  try{
    const j = await alertsPost("alerts", {recipients: list});
    el("alertlist").value = j.recipients.join("\n");
    const n = j.recipients.length;
    alertMsg("saved " + n + " address" + (n === 1 ? "" : "es")
             + (n < list.length ? " (" + (list.length - n) + " rejected)" : ""), "ok");
  }catch(e){ alertMsg(String(e.message || e), "err"); }
};
el("testalert").onclick = async () => {
  alertMsg("sending...");
  try{
    const j = await alertsPost("alerts-test", {});
    alertMsg("test sent to " + j.sent, "ok");
  }catch(e){ alertMsg(String(e.message || e), "err"); }
};
alertsLoad();
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

    def _session_ok(self):
        """A valid session cookie, or the token header for scripts and curl."""
        if not LOGIN_REQUIRED:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "k2s" and v in SESSIONS:
                return True
        return secrets.compare_digest(self.headers.get("X-K2-Token", ""), TOKEN)

    def _authed(self):
        return CONTROL and self._session_ok()

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
        if self.path == "/api/login":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                b = {}
            ok = (LOGIN_REQUIRED
                  and secrets.compare_digest(str(b.get("u", "")), AUTH_USER)
                  and secrets.compare_digest(str(b.get("p", "")), AUTH_PASS))
            if not ok:
                return self._json(401, {"error": "bad credentials"})
            sid = secrets.token_urlsafe(32)
            SESSIONS.add(sid)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # SameSite=Strict is the CSRF defence: the browser will not attach
            # this cookie to a request another site initiated.
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header("Set-Cookie",
                             f"k2s={sid}; HttpOnly; SameSite=Strict; Path=/{secure}")
            self.end_headers(); self.wfile.write(body); return

        if self.path == "/api/logout":
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "k2s":
                    SESSIONS.discard(v)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", "k2s=; Max-Age=0; Path=/")
            self.end_headers(); return

        if not self._session_ok():
            return self._json(401, {"error": "not signed in"})

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

            if action == "mesh":
                try:
                    with urllib.request.urlopen(ALLOWED["status"], timeout=8) as r:
                        pstate = (json.load(r)["result"]["status"]
                                  .get("print_stats", {}).get("state"))
                except Exception as e:
                    return self._json(502, {"error": f"could not read print state: {e}"})
                if pstate in ("printing", "paused"):
                    return self._json(409, {"error": f"refusing to probe while {pstate}"})
                # G29, not BED_MESH_CALIBRATE: Creality replaced the stock macro,
                # and G29 is what START_PRINT itself calls.
                threading.Thread(target=fire_gcode, args=("G29",), daemon=True).start()
                return self._json(200, {"ok": True, "sent": "G29"})

            if action == "home":
                b = json.loads(raw or b"{}")
                axes = str(b.get("axes", "")).upper()
                G = {"XY": "G28 X Y", "Z": "G28 Z", "ALL": "G28"}
                if axes not in G:
                    return self._json(400, {"error": "axes must be XY, Z or ALL"})
                # Homing mid-print would drive the toolhead off the model, so
                # refuse rather than trust the caller to have checked.
                try:
                    with urllib.request.urlopen(ALLOWED["status"], timeout=8) as r:
                        pstate = (json.load(r)["result"]["status"]
                                  .get("print_stats", {}).get("state"))
                except Exception as e:
                    return self._json(502, {"error": f"could not read print state: {e}"})
                if pstate in ("printing", "paused"):
                    return self._json(409, {"error": f"refusing to home while {pstate}"})
                self._moonraker("/printer/gcode/script?" +
                                urllib.parse.urlencode({"script": G[axes]}))
                return self._json(200, {"ok": True, "sent": G[axes]})

            if action == "start":
                b = json.loads(raw or b"{}")
                name = str(b.get("filename") or "")
                if not name.endswith(".gcode") or ".." in name or name.startswith("/"):
                    return self._json(400, {"error": "expected a .gcode filename"})
                # Refuse when the machine is not in a state to accept a job.
                # Moonraker would reject some of these itself, but its errors are
                # opaque, and starting a print is not something to be vague about.
                try:
                    with urllib.request.urlopen(ALLOWED["info"], timeout=8) as r:
                        kstate = json.load(r)["result"]["state"]
                    with urllib.request.urlopen(ALLOWED["status"], timeout=8) as r:
                        pstate = (json.load(r)["result"]["status"]
                                  .get("print_stats", {}).get("state"))
                except Exception as e:
                    return self._json(502, {"error": f"could not read printer state: {e}"})
                if kstate != "ready":
                    return self._json(409, {"error": f"klipper is {kstate}, not ready"})
                if pstate in ("printing", "paused"):
                    return self._json(409, {"error": f"a print is already {pstate}"})
                self._moonraker("/printer/print/start?" +
                                urllib.parse.urlencode({"filename": name}))
                return self._json(200, {"ok": True, "started": name})

            if action == "alerts":
                b = json.loads(raw or b"{}")
                saved = save_recipients(b.get("recipients") or [])
                return self._json(200, {"ok": True, "recipients": saved})

            if action == "alerts-test":
                err = send_mail("[K2] test alert",
                                "This is a test from the K2 Plus dashboard.\n"
                                "If you got this, failure alerts will reach you.")
                if err:
                    # 400, not 502: Cloudflare replaces an origin 5xx with its own
                    # branded error page, which swallowed this message entirely and
                    # left the UI showing a bare "HTTP 502". A 4xx passes through.
                    return self._json(400, {"error": err})
                return self._json(200, {"ok": True, "sent": len(load_recipients())})

            return self._json(403, {"error": f"unknown action {action!r}"})
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": f"moonraker {e.code}: {e.read()[:200].decode(errors='replace')}"})
        except Exception as e:
            return self._json(502, {"error": str(e)})

    def _camera_proxy(self):
        """Stream the relay through, chunk by chunk.

        MJPEG never ends, so this loop runs for as long as the viewer watches -
        which is only safe because the server is threaded. A viewer closing the
        tab surfaces as a broken pipe, which is expected, not an error.
        """
        try:
            with urllib.request.urlopen(CAM_RELAY + self.path, timeout=15) as r:
                self.send_response(200)
                self.send_header("Content-Type",
                                 r.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    chunk = r.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_error(502, f"camera relay: {e}")
            except Exception:
                pass

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
        # FIRST, before any route. It used to sit further down, after the "/"
        # handler had already returned the dashboard - so an unauthenticated
        # page loaded, its JS got 401 from the API, reloaded, and looped.
        if not self._session_ok():
            if self.path.startswith("/api/"):
                self._json(401, {"error": "not signed in"}); return
            body = LOGIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body); return
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
        if self.path.startswith("/camera/"):
            return self._camera_proxy()
        if self.path == "/api/alerts":
            # Token gated: the saved addresses are personal data, and every other
            # read on this server is open.
            if not self._authed():
                self._json(401, {"error": "bad or missing X-K2-Token"}); return
            self._json(200, {"recipients": load_recipients(),
                             "smtp_configured": bool(SMTP_HOST)})
            return
        if self.path.startswith("/api/meta?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("file") or [""])[0]
            if not name or ".." in name or name.startswith("/"):
                self.send_error(400, "bad filename"); return
            m = gcode_meta(name)
            self._json(200, {"minutes": m["minutes"], "has_thumb": bool(m["png"])})
            return
        if self.path.startswith("/api/thumb?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (q.get("file") or [""])[0]
            # No traversal, no absolute paths: this value reaches a URL that
            # Moonraker resolves against its gcode directory.
            if not name or ".." in name or name.startswith("/"):
                self.send_error(400, "bad filename"); return
            png = gcode_meta(name)["png"]
            if not png:
                self._json(404, {"error": "no thumbnail in this file"}); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers(); self.wfile.write(png); return
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
    threading.Thread(target=alert_watcher, daemon=True).start()
    with Srv(("127.0.0.1", PORT), H) as srv:
        banner = [f"dashboard  http://localhost:{PORT}",
                  f"printer    {MOONRAKER}   camera {CAMERA}",
                  "read endpoints:  " + ", ".join(sorted(ALLOWED))]
        if CONTROL:
            banner += ["",
                       "CONTROL IS ON - this instance can set heaters and stop prints.",
                       "  sign in at the page; the browser keeps a session cookie.",
                       "  scripts can still use X-K2-Token, value in K2_TOKEN.",
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

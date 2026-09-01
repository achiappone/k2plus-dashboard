#!/usr/bin/env python3
"""
Generate docs/index.html (the GitHub Pages copy) from dashboard.py.

One source of truth for the markup: the hosted page is the same UI, pointed at a
proxy running on the viewer's machine instead of being served by it.

NOTE the extraction below uses split, not rsplit. dashboard.py has docstrings
AFTER the PAGE literal, so splitting on the LAST triple-quote in the file sweeps
the intervening Python into the page - which is exactly what happened once.
"""
import pathlib, re, subprocess, datetime

src = pathlib.Path("dashboard.py").read_text()
page = src.split('PAGE = r"""', 1)[1].split('"""', 1)[0]

R = [
 ('const PROXY = "";',
  '// This page has no backend. It calls the proxy running on YOUR machine.\n'
  '// localhost is a trustworthy origin, so an HTTPS page may reach it; the\n'
  "// printer's own http:// address may not.\n"
  'const PROXY = localStorage.getItem("k2proxy") || "http://localhost:8770";'),
 ('const CAM = "__CAMERA__";',
  '// Only the WebRTC handshake is HTTP, and it is relayed by the proxy. The\n'
  '// media itself is UDP, straight to the printer, and not mixed content.\n'
  'const SIGNAL = PROXY + "/api/camera/offer";'),
 ('fetch(CAM+"/call/webrtc_local", {method:"POST", headers:{"Content-Type":"text/plain"},',
  'fetch(SIGNAL, {method:"POST", headers:{"Content-Type":"text/plain"},'),
 ('.camfoot button:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}',
  '.camfoot button:focus-visible{outline:2px solid var(--series-1);outline-offset:2px}\n'
  '.setup{background:var(--surface-1);border:1px solid var(--rule);padding:18px 20px;margin-bottom:20px}\n'
  '.setup h2{margin-bottom:8px}\n'
  '.setup code{display:block;font-family:"IBM Plex Mono",monospace;font-size:12.5px;\n'
  '  background:var(--bg);border:1px solid var(--rule);padding:10px 12px;margin:10px 0;overflow-x:auto}\n'
  '.setup.ok{display:none}'),
 ('<div class="grid">',
  '<div class="setup" id="setup">\n'
  '  <h2>Not connected</h2>\n'
  '  <p style="margin:0">This page is the interface only. The proxy that reaches your\n'
  '  printer runs on your own machine &mdash; start it and this panel disappears.</p>\n'
  '  <code>git clone https://github.com/achiappone/k2plus-dashboard\n'
  'cd k2plus-dashboard\n'
  'PRINTER_HOST=10.0.0.42 python3 dashboard.py</code>\n'
  '  <p style="margin:0;font-size:12.5px;color:var(--text-muted)">Using\n'
  '  <span class="mono" id="proxyaddr"></span>. Different host or port?\n'
  '  <a href="#" id="setproxy">Change it</a>.</p>\n'
  '</div>\n'
  '<div class="grid">'),
 ('async function tick(){\n  try{',
  'async function tick(){\n  try{\n    document.getElementById("setup").classList.add("ok");'),
 ('    el("statetx").textContent="unreachable";\n    el("state").style.color="var(--crit)";',
  '    el("statetx").textContent="proxy not running";\n'
  '    el("state").style.color="var(--crit)";\n'
  '    document.getElementById("setup").classList.remove("ok");'),
 ('buildTable(); buildSparks(); tick(); tickTemps();',
  'el("proxyaddr").textContent = PROXY;\n'
  'el("setproxy").onclick = e => { e.preventDefault();\n'
  '  const v = prompt("Proxy address", PROXY);\n'
  '  if (v) { localStorage.setItem("k2proxy", v.replace(/\\/$/,"")); location.reload(); } };\n'
  'buildTable(); buildSparks(); tick(); tickTemps();'),
]
for a, b in R:
    assert a in page, f"anchor vanished from dashboard.py: {a[:48]!r}"
    page = page.replace(a, b)

# Every function the page needs at startup must actually be CALLED in the
# output. Checking the anchor existed is not the same as checking the result
# still works: the anchor was right while the replacement quietly dropped
# buildSparks(), so the hosted page rendered an empty panel for a while.
for fn in ("buildTable", "buildSparks", "tick", "tickTemps", "drawChart",
           "drawSparks", "drawClock", "connectCam"):
    assert f"function {fn}(" in page or f"{fn} =" in page, f"{fn} is not defined"
for call in ("buildTable()", "buildSparks()", "tick()", "tickTemps()"):
    assert call in page.split("function")[-1] or f"; {call}" in page or page.count(call) > 1, \
        f"{call} is never invoked at startup"

# the failure this file exists to prevent
for leak in ("class H(", "http.server", "def do_POST", "BaseHTTPRequestHandler",
             "socketserver", "__main__"):
    assert leak not in page, f"python leaked into the page: {leak!r}"
assert page.rstrip().endswith("</html>"), "page does not end at </html>"
assert "__CAMERA__" not in page, "unreplaced placeholder"

try:
    sha = subprocess.run(["git","rev-parse","--short","HEAD"], capture_output=True,
                         text=True).stdout.strip() or "?"
except Exception:
    sha = "?"
stamp = f"{sha} · {datetime.datetime.now(datetime.timezone.utc):%d %b %H:%M} UTC"
assert "__BUILD__" in page, "build stamp placeholder missing"
page = page.replace("__BUILD__", stamp)

out = pathlib.Path("docs/index.html")
out.write_text(page)
print(f"docs/index.html  {len(page)/1024:.1f} KB  ({page.count(chr(10))+1} lines)  stamp: {stamp}")

# K2 Plus dashboard

A single-file dashboard for a Klipper/Moonraker 3D printer — live temperatures,
progress, and the camera — with a **read-only** proxy in front of the printer.

Built for a Creality K2 Plus, but nothing in it is K2-specific except the
camera, which is WebRTC rather than the MJPEG most Klipper builds serve.

```sh
PRINTER_HOST=10.0.0.42 python3 dashboard.py     # then open http://localhost:8770
```

Python 3 standard library only. No pip install, no Docker, no database.

## What it shows

**Progress** as a headline figure with elapsed and projected remaining time,
plus Z height, filament used, speed and flow.

**Three temperature charts** — nozzle, bed, chamber — each on its own scale,
with the target as a dashed line and ten minutes of history. Deliberately three
charts and not one: 260 °C, 100 °C and 53 °C share no sensible axis, and putting
them on a dual-axis plot is the single most misleading thing you can do to a
chart. Hovering gives a crosshair readout, and there is a table view underneath.

**The camera.** On this printer it is WebRTC — port 8000 returns the same
signalling page for every path and query, so `?action=snapshot` is that page and
not a JPEG. There is no snapshot endpoint. The dashboard performs the WebRTC
negotiation itself rather than framing the printer's page, which means the video
sits in the layout, the connection state is visible instead of failing as a
black rectangle, and there is a **Save still** button that captures a frame to
PNG — the snapshot the printer does not offer.

## The proxy is read-only, on purpose

Moonraker sends no CORS headers, so a browser page cannot call it directly. This
serves the page and proxies the API from the same origin.

That proxy forwards **GET to three status endpoints and nothing else**. It cannot
set a temperature, move the toolhead, or start, pause or cancel a job. Anything
else returns 403; any method but GET returns 501.

This matters because **Moonraker has no authentication**. Do not port-forward it.
Anyone who finds the port can set the hot end to 300 °C and run arbitrary gcode
on an unattended machine in your house.

## Reaching it from outside

The dashboard has to run on a machine on the same network as the printer — a Pi
is plenty, a Pi Zero 2 W is enough. The printer's address is private, so nothing
outside the network can route to it; where the *page* is hosted is irrelevant,
because what matters is which network the *browser* is on. This is also why it
cannot be served from GitHub Pages: Pages is HTTPS-only, and browsers
unconditionally block an HTTPS page from calling a plain-HTTP address.

Two ways to bridge it, neither of which opens a port on your router:

**Tailscale** — put your devices and the Pi on a private overlay network. The
dashboard is then reachable at a tailnet address from anywhere, with no public
surface at all. Start here.

**Cloudflare Tunnel + Access** — `cloudflared` dials out from the Pi; Cloudflare
Access demands a login before any request reaches it. That is a real login page,
enforced at the edge. Template in `deploy/cloudflared-config.yml`.

You do **not** need nginx for either. `cloudflared` is already the reverse proxy;
putting nginx in front of it adds a layer that does nothing here.

## Deploy

`deploy/k2-dashboard.service` is a hardened systemd unit — set `PRINTER_HOST`
and the paths, then:

```sh
sudo cp deploy/k2-dashboard.service /etc/systemd/system/
sudo systemctl enable --now k2-dashboard
```

## Running it on Proxmox

`deploy/proxmox-lxc.sh` builds a container for it. Run it on the Proxmox host,
from the shell in the web UI at `:8006` or over ssh:

```sh
./deploy/proxmox-lxc.sh 10.0.0.42        # your printer's address
```

An **LXC, not a VM** — this is one Python process, so a container at ~40 MB that
boots in a second is the right size. It clones this repo, installs a hardened
systemd unit, and starts on host boot.

Proxmox has its own web UI at `https://<host>:8006`; nothing extra is needed to
manage it. `deploy/homepage/services.yaml` is a tile config if you want a
[Homepage](https://gethomepage.dev) landing page pulling the printer, Fluidd and
Proxmox together.

Two host notes: a laptop will suspend on lid close and take the container with
it (`HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`), and installing
Proxmox **erases the disk** — it is a bare-metal hypervisor, not a package.

## Hosted UI

The interface is published at
**[achiappone.github.io/k2plus-dashboard](https://achiappone.github.io/k2plus-dashboard/)**.

It is the UI only — there is no backend behind it. It calls the proxy running on
*your* machine, and shows setup instructions until that proxy answers.

This works because browsers treat `http://localhost` as a **trustworthy origin**,
so an HTTPS page is allowed to call it. The printer's own address gets no such
exemption: it is plain HTTP on a private IP, which is blocked as mixed content
from an HTTPS page regardless of which network you are on. The camera still works from the hosted page, because only its **handshake** is
HTTP — and that is relayed through the local proxy. WebRTC media is UDP and is
not subject to mixed-content rules, so the video flows browser-to-printer
directly once the handshake is done.

The proxy allows exactly two origins by exact match. A page from anywhere else
gets no CORS header and the browser drops the response.

There is one POST path, `/api/camera/offer`, and it forwards only to the
camera's signalling endpoint — never to Moonraker. Everything else, any method,
returns 403.

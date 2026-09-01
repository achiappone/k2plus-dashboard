#!/usr/bin/env bash
# Provision an LXC on Proxmox that runs the dashboard.
# Run this ON the Proxmox host (shell from the web UI at :8006, or ssh root@host).
#
#   ./proxmox-lxc.sh 10.0.0.42            # <- your printer's address
#
# A container, not a VM: this workload is one Python process. ~40 MB, boots in
# about a second, and shares the host kernel so there is no virtualisation
# overhead to pay for.
set -euo pipefail

PRINTER="${1:?usage: $0 <printer-ip>}"
CTID="${CTID:-201}"
HOSTNAME="${HOSTNAME:-printer-dash}"
BRIDGE="${BRIDGE:-vmbr0}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORE="${TEMPLATE_STORE:-local}"
TEMPLATE="debian-12-standard_12.7-1_amd64.tar.zst"

echo "==> template"
pveam update >/dev/null 2>&1 || true
pveam list "$TEMPLATE_STORE" | grep -q "$TEMPLATE" || pveam download "$TEMPLATE_STORE" "$TEMPLATE"

echo "==> container $CTID ($HOSTNAME)"
pct create "$CTID" "$TEMPLATE_STORE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores 1 --memory 512 --swap 256 \
  --rootfs "$STORAGE:4" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --features nesting=1 \
  --unprivileged 1 \
  --onboot 1 \
  --description "K2 Plus dashboard (read-only Moonraker proxy)"

pct start "$CTID"
sleep 6

echo "==> provisioning"
pct exec "$CTID" -- bash -eux <<INNER
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git ca-certificates curl >/dev/null

git clone --depth 1 https://github.com/achiappone/k2plus-dashboard /opt/k2plus-dashboard

useradd -r -s /usr/sbin/nologin -d /opt/k2plus-dashboard dash || true

cat >/etc/systemd/system/k2-dashboard.service <<UNIT
[Unit]
Description=K2 Plus dashboard (read-only Moonraker proxy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dash
WorkingDirectory=/opt/k2plus-dashboard
Environment=PRINTER_HOST=$PRINTER
ExecStart=/usr/bin/python3 /opt/k2plus-dashboard/dashboard.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now k2-dashboard
INNER

IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')
echo
echo "==> done"
echo "    dashboard  http://$IP:8770"
echo "    printer    $PRINTER"
echo
echo "    The dashboard binds 127.0.0.1 by default so only this container can"
echo "    reach it. To open it to your LAN, set BIND=0.0.0.0 in the unit - but"
echo "    prefer putting Tailscale or cloudflared in this container instead, so"
echo "    it is reachable without being exposed."

#!/usr/bin/env bash
# Creates a NEW dedicated interface. Never edits an existing WireGuard config.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo 'Run as root'; exit 1; fi
if [[ -e /etc/wireguard/wg-pushok.conf ]]; then echo 'wg-pushok.conf exists; no changes made'; exit 1; fi
if ss -lun | awk '{print $5}' | grep -Eq ':51820$'; then echo 'UDP 51820 is in use; no changes made'; exit 1; fi
if ip -4 route show | grep -q '10\.77\.0\.0/24'; then echo '10.77.0.0/24 already routed; no changes made'; exit 1; fi
apt-get update
apt-get install -y wireguard iptables
WAN_IF=$(ip -4 route show default | awk 'NR==1 {print $5}')
[[ $WAN_IF =~ ^[a-zA-Z0-9_.:-]+$ ]] || { echo 'Cannot identify uplink'; exit 1; }
umask 077
mkdir -p /etc/wireguard
WG_PRIVATE=$(wg genkey)
cat > /etc/wireguard/wg-pushok.conf <<CONF
[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = $WG_PRIVATE
PostUp = iptables -I FORWARD -i %i -j ACCEPT; iptables -I FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -s 10.77.0.0/24 -o $WAN_IF -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -s 10.77.0.0/24 -o $WAN_IF -j MASQUERADE
CONF
unset WG_PRIVATE
cat > /etc/sysctl.d/90-pushok-wireguard.conf <<'CONF'
net.ipv4.ip_forward=1
CONF
sysctl -p /etc/sysctl.d/90-pushok-wireguard.conf
systemctl enable --now wg-quick@wg-pushok
printf 'Created wg-pushok. Allow UDP 51820 in your firewall and configure the agent.\n'

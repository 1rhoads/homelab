#!/usr/bin/env bash
# ==============================================================================
# Helper Script: Distribute Ansible ED25519 SSH Public Key to Homelab Devices
# ==============================================================================

set -euo pipefail

PUBKEY="${HOME}/.ssh/ansible_ed25519.pub"

if [[ ! -f "$PUBKEY" ]]; then
  echo "Error: Public key $PUBKEY not found."
  exit 1
fi

KEY_CONTENT=$(cat "$PUBKEY")

echo "======================================================================"
echo "Homelab Automation SSH Key Distribution"
echo "Public Key: $PUBKEY"
echo "Fingerprint: $(ssh-keygen -lf "$PUBKEY")"
echo "======================================================================"
echo ""

# 1. Proxmox VE Nodes
echo "--- 1. Proxmox VE Cluster Nodes ---"
PROXMOX_NODES=("192.168.254.111" "192.168.254.112" "192.168.254.113")
for node in "${PROXMOX_NODES[@]}"; do
  echo "Installing key to root@$node..."
  ssh-copy-id -i "$PUBKEY" -o StrictHostKeyChecking=no "root@$node" || echo "Warning: Failed to copy to $node. You may need to run manually."
done
echo ""

# 2. Docker Host
echo "--- 2. Docker Host ---"
echo "Installing key to rhoadsr@192.168.254.164..."
ssh-copy-id -i "$PUBKEY" -o StrictHostKeyChecking=no "rhoadsr@192.168.254.164" || echo "Warning: Failed to copy to docker-homelab."
echo ""

# 3. OpenWrt Access Points
echo "--- 3. OpenWrt Access Points (Dropbear) ---"
OPENWRT_APS=("192.168.0.41" "192.168.0.42" "192.168.0.44" "192.168.0.45" "192.168.0.46" "192.168.0.47")
for ap in "${OPENWRT_APS[@]}"; do
  echo "Installing key to OpenWrt AP root@$ap..."
  ssh -o StrictHostKeyChecking=no "root@$ap" "mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys && grep -q '$KEY_CONTENT' /etc/dropbear/authorized_keys || echo '$KEY_CONTENT' >> /etc/dropbear/authorized_keys" 2>/dev/null || echo "Notice: Skipped or password required for $ap."
done
echo ""

# 4. MikroTik Switches
echo "--- 4. MikroTik RouterOS Switches ---"
MIKROTIK_SWITCHES=("switch-1.iye.internal" "switch-2.iye.internal")
for sw in "${MIKROTIK_SWITCHES[@]}"; do
  echo "Transferring and importing key on admin@$sw..."
  scp -o StrictHostKeyChecking=no "$PUBKEY" "admin@$sw:ansible_ed25519.pub" 2>/dev/null || true
  ssh -o StrictHostKeyChecking=no "admin@$sw" "/user ssh-keys import public-key-file=ansible_ed25519.pub user=admin" 2>/dev/null || echo "Notice: Skipped or password required for $sw."
done

echo ""
echo "======================================================================"
echo "SSH Key distribution completed!"
echo "You can now test connectivity with:"
echo "  ansible all -i ansible/inventory/hosts.ini -m ping --private-key ~/.ssh/ansible_ed25519"
echo "======================================================================"

#!/usr/bin/env python3
"""
seed_netbox.py - Automated initial inventory bootstrap for NetBox in Homelab.

Pre-populates:
- Site (Homelab)
- Manufacturers & Device Types (MikroTik, Proxmox, OpenWrt, Docker)
- Device Roles (Core Switch, Dist Switch, Hypervisor, Access Point, Server)
- IPAM Prefixes (192.168.254.0/24 Management, 192.168.0.0/24 WiFi)
- Devices and primary IP assignments
"""

import urllib.request
import urllib.error
import json
import ssl
import time
import sys
import os
import socket

# Fallback DNS resolution if netbox.iye.internal is not in local /etc/hosts
_orig_getaddrinfo = socket.getaddrinfo
def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "netbox.iye.internal":
        try:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)
        except socket.gaierror:
            return _orig_getaddrinfo("192.168.254.164", port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _custom_getaddrinfo

DEFAULT_BASE_URL = os.environ.get("NETBOX_URL", "https://netbox.iye.internal")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "0123456789abcdef0123456789abcdef01234567")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def api_call(endpoint, method="GET", data=None, base_url=DEFAULT_BASE_URL):
    url = f"{base_url}/api/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Token {NETBOX_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        # If object already exists (e.g. unique constraint), return existing if possible
        if e.code == 400 and ("already exists" in err_msg or "must be unique" in err_msg):
            return {"_already_exists": True, "error": err_msg}
        print(f"HTTP Error {e.code} on {method} {url}: {err_msg}", file=sys.stderr)
        raise

def get_or_create(endpoint, payload, lookup_field="slug"):
    lookup_val = payload.get(lookup_field, payload.get("name"))
    existing = api_call(f"{endpoint}?{lookup_field}={lookup_val}")
    if existing.get("count", 0) > 0:
        return existing["results"][0]
    res = api_call(endpoint, method="POST", data=payload)
    if res.get("_already_exists"):
        existing = api_call(f"{endpoint}?{lookup_field}={lookup_val}")
        if existing.get("count", 0) > 0:
            return existing["results"][0]
    return res

def main():
    print("Connecting to NetBox API...")
    # Wait for NetBox API to be ready
    for i in range(20):
        try:
            status = api_call("status/")
            print(f"NetBox {status.get('netbox-version', 'ready')} is online!")
            break
        except Exception as e:
            print(f"Waiting for NetBox API... ({e})")
            time.sleep(5)
    else:
        print("NetBox API timed out.", file=sys.stderr)
        sys.exit(1)

    # 1. Site
    print("Ensuring Site: Homelab...")
    site = get_or_create("dcim/sites/", {
        "name": "Homelab",
        "slug": "homelab",
        "status": "active",
        "description": "Primary Home Laboratory Network (iye.internal)"
    })
    site_id = site["id"]

    # 2. Manufacturers
    print("Ensuring Manufacturers...")
    mfg_mikrotik = get_or_create("dcim/manufacturers/", {"name": "MikroTik", "slug": "mikrotik"})
    mfg_proxmox = get_or_create("dcim/manufacturers/", {"name": "Proxmox", "slug": "proxmox"})
    mfg_openwrt = get_or_create("dcim/manufacturers/", {"name": "OpenWrt", "slug": "openwrt"})
    mfg_docker = get_or_create("dcim/manufacturers/", {"name": "Docker", "slug": "docker"})

    # 3. Device Types
    print("Ensuring Device Types...")
    dt_switch = get_or_create("dcim/device-types/", {
        "manufacturer": mfg_mikrotik["id"],
        "model": "CRS326-24G-2S+RM",
        "slug": "crs326-24g-2s-plus-rm",
        "u_height": 1
    })
    dt_proxmox = get_or_create("dcim/device-types/", {
        "manufacturer": mfg_proxmox["id"],
        "model": "Proxmox VE Node",
        "slug": "proxmox-ve-node",
        "u_height": 1
    })
    dt_ap = get_or_create("dcim/device-types/", {
        "manufacturer": mfg_openwrt["id"],
        "model": "OpenWrt Access Point",
        "slug": "openwrt-access-point",
        "u_height": 0
    })
    dt_server = get_or_create("dcim/device-types/", {
        "manufacturer": mfg_docker["id"],
        "model": "Docker Host Server",
        "slug": "docker-host-server",
        "u_height": 1
    })

    # 4. Device Roles
    print("Ensuring Device Roles...")
    role_core_sw = get_or_create("dcim/device-roles/", {"name": "Core Switch", "slug": "core-switch", "color": "2196f3"})
    role_dist_sw = get_or_create("dcim/device-roles/", {"name": "Distribution Switch", "slug": "distribution-switch", "color": "03a9f4"})
    role_pve = get_or_create("dcim/device-roles/", {"name": "Hypervisor", "slug": "hypervisor", "color": "9c27b0"})
    role_ap = get_or_create("dcim/device-roles/", {"name": "Access Point", "slug": "access-point", "color": "4caf50"})
    role_srv = get_or_create("dcim/device-roles/", {"name": "Server", "slug": "server", "color": "ff9800"})

    # 5. IPAM Prefixes
    print("Ensuring IPAM Prefixes...")
    get_or_create("ipam/prefixes/", {
        "prefix": "192.168.254.0/24",
        "site": site_id,
        "status": "active",
        "description": "Homelab Management LAN (Servers, Proxmox, Switches)"
    }, lookup_field="prefix")

    get_or_create("ipam/prefixes/", {
        "prefix": "192.168.0.0/24",
        "site": site_id,
        "status": "active",
        "description": "Homelab Wireless LAN & OpenWrt APs"
    }, lookup_field="prefix")

    # 6. Devices and IP addresses
    devices_data = [
        # MikroTik switches
        {"name": "switch-1.iye.internal", "role": role_core_sw["id"], "type": dt_switch["id"], "ip": None},
        {"name": "switch-2.iye.internal", "role": role_dist_sw["id"], "type": dt_switch["id"], "ip": None},
        # Proxmox nodes
        {"name": "virt-1.iye.internal", "role": role_pve["id"], "type": dt_proxmox["id"], "ip": "192.168.254.111/24"},
        {"name": "virt-2.iye.internal", "role": role_pve["id"], "type": dt_proxmox["id"], "ip": "192.168.254.112/24"},
        {"name": "virt-3.iye.internal", "role": role_pve["id"], "type": dt_proxmox["id"], "ip": "192.168.254.113/24"},
        # Docker host
        {"name": "docker-homelab.iye.internal", "role": role_srv["id"], "type": dt_server["id"], "ip": "192.168.254.164/24"},
        # OpenWrt APs
        {"name": "ap-outside.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.44/24"},
        {"name": "ap-callie.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.42/24"},
        {"name": "ap-robert.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.47/24"},
        {"name": "ap-garage.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.41/24"},
        {"name": "ap-printer.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.45/24"},
        {"name": "ap-fort.iye.internal", "role": role_ap["id"], "type": dt_ap["id"], "ip": "192.168.0.46/24"},
    ]

    print("Ensuring Devices & IPAM entries...")
    for d in devices_data:
        dev = get_or_create("dcim/devices/", {
            "name": d["name"],
            "role": d["role"],
            "device_type": d["type"],
            "site": site_id,
            "status": "active"
        }, lookup_field="name")
        dev_id = dev["id"]

        if d["ip"]:
            # Ensure primary management interface
            iface = get_or_create("dcim/interfaces/", {
                "device": dev_id,
                "name": "eth0",
                "type": "1000base-t"
            }, lookup_field="name")
            iface_id = iface["id"]

            # Ensure IP address assigned to interface
            ip_obj = get_or_create("ipam/ip-addresses/", {
                "address": d["ip"],
                "status": "active",
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": iface_id,
                "dns_name": d["name"]
            }, lookup_field="address")

            # Set as primary IP on device
            api_call(f"dcim/devices/{dev_id}/", method="PATCH", data={
                "primary_ip4": ip_obj["id"]
            })

    print("NetBox initial inventory seeding completed successfully!")

if __name__ == "__main__":
    main()

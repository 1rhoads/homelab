#!/usr/bin/env python3
"""
sync_librenms_to_netbox.py - Automated synchronization from LibreNMS to NetBox.

Discovers devices polled by LibreNMS and ensures:
1. Manufacturers, Device Types, and Device Roles exist in NetBox.
2. Subnet Prefixes exist in NetBox IPAM.
3. Devices, serial numbers, management interfaces, and primary IPs are created/updated.
"""

import os
import sys
import ssl
import json
import socket
import argparse
import urllib.request
import urllib.error
import urllib.parse
from ipaddress import ip_network, ip_interface

# Fallback DNS resolution if local hostnames are not in /etc/hosts
_orig_getaddrinfo = socket.getaddrinfo
def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    host_map = {
        "netbox.iye.internal": "192.168.254.164",
        "librenms.iye.internal": "192.168.254.164",
        "switch-1.iye.internal": "192.168.254.2",
        "switch-2.iye.internal": "192.168.254.3",
        "virt-1.iye.internal": "192.168.254.111",
        "virt-2.iye.internal": "192.168.254.112",
        "virt-3.iye.internal": "192.168.254.113",
        "firewall.iye.internal": "192.168.254.253",
    }
    if host in host_map:
        try:
            return _orig_getaddrinfo(host, port, family, type, proto, flags)
        except socket.gaierror:
            return _orig_getaddrinfo(host_map[host], port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _custom_getaddrinfo

# Configuration Defaults
DEFAULT_NETBOX_URL = os.environ.get("NETBOX_URL", "https://netbox.iye.internal")
DEFAULT_NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN", "0123456789abcdef0123456789abcdef01234567")

DEFAULT_LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "https://librenms.iye.internal")
DEFAULT_LIBRENMS_TOKEN = os.environ.get("LIBRENMS_TOKEN", "4|ONVizl4EosPEvyiqYtMkazgBzCAbph7sS47NQdk4c32e92c5")

# SSL Context ignoring self-signed internal certs
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def slugify(text):
    """Generate a clean slug for NetBox objects."""
    s = text.lower().strip()
    for char in [" ", "_", "/", ".", "(", ")", "[", "]", "{", "}", "+", ":"]:
        s = s.replace(char, "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:50]


class NetboxClient:
    def __init__(self, base_url, token, dry_run=False, verbose=False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.dry_run = dry_run
        self.verbose = verbose

    def request(self, endpoint, method="GET", data=None):
        url = f"{self.base_url}/api/{endpoint.lstrip('/')}"
        headers = {
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        body = json.dumps(data).encode("utf-8") if data else None

        if self.verbose:
            print(f"[NetBox] {method} {url}")
            if data:
                print(f"[NetBox Data] {json.dumps(data, indent=2)}")

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                content = resp.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if e.code == 400 and ("already exists" in err or "must be unique" in err):
                return {"_already_exists": True, "error": err}
            print(f"[-] NetBox HTTP {e.code} on {method} {url}: {err}", file=sys.stderr)
            raise

    def get_or_create(self, endpoint, payload, lookup_field="slug"):
        lookup_val = payload.get(lookup_field, payload.get("name"))
        encoded = urllib.parse.quote(str(lookup_val))
        res = self.request(f"{endpoint}?{lookup_field}={encoded}")
        if res.get("count", 0) > 0:
            return res["results"][0]

        if self.dry_run:
            print(f"  [DRY-RUN] Would create {endpoint}: {payload}")
            return {"id": 9999, **payload}

        created = self.request(endpoint, method="POST", data=payload)
        if created.get("_already_exists"):
            res = self.request(f"{endpoint}?{lookup_field}={encoded}")
            if res.get("count", 0) > 0:
                return res["results"][0]
        return created


class LibreNMSClient:
    def __init__(self, base_url, token, verbose=False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verbose = verbose

    def request(self, endpoint):
        url = f"{self.base_url}/api/v0/{endpoint.lstrip('/')}"
        headers = {
            "X-Auth-Token": self.token,
            "Accept": "application/json"
        }
        if self.verbose:
            print(f"[LibreNMS] GET {url}")
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_devices(self):
        res = self.request("devices")
        return res.get("devices", [])


def map_device_metadata(dev):
    """
    Map LibreNMS OS and Hardware telemetry to NetBox Manufacturer, Model, and Role.
    """
    os_name = (dev.get("os") or "").lower()
    hw = (dev.get("hardware") or "").strip()
    sys_name = (dev.get("sysName") or "").lower()
    hostname = (dev.get("hostname") or "").lower()

    # Default fallback
    manufacturer = "Generic"
    model = "Generic Device"
    role_name = "Server"
    role_slug = "server"
    role_color = "ff9800"

    # 1. MikroTik
    if os_name in ["routeros", "swos"]:
        manufacturer = "MikroTik"
        if os_name == "routeros":
            role_name = "Core Switch"
            role_slug = "core-switch"
            role_color = "2196f3"
            # Strip version from hardware string e.g. "CRS310-8G+2S+ 7.25" -> "CRS310-8G+2S+"
            model = hw.split()[0] if hw else "CRS310-8G+2S+"
        else:
            role_name = "Distribution Switch"
            role_slug = "distribution-switch"
            role_color = "03a9f4"
            model = hw if hw else "CSS318-16G-2S+"

    # 2. OpenWrt Access Points
    elif os_name == "openwrt":
        manufacturer = "Linksys" if "linksys" in hw.lower() else "OpenWrt"
        role_name = "Access Point"
        role_slug = "access-point"
        role_color = "4caf50"
        model = hw if hw else "OpenWrt Access Point"

    # 3. Proxmox Hypervisors
    elif os_name == "proxmox":
        manufacturer = "Proxmox"
        role_name = "Hypervisor"
        role_slug = "hypervisor"
        role_color = "9c27b0"
        model = "Proxmox VE Node"

    # 4. OPNsense / FreeBSD Firewall
    elif os_name in ["opnsense", "freebsd"] or "firewall" in hostname:
        manufacturer = "OPNsense"
        role_name = "Firewall"
        role_slug = "firewall"
        role_color = "f44336"
        model = "AZW EQ Firewall Appliance" if "azw" in hw.lower() else "OPNsense Appliance"

    # 5. Grandstream VoIP ATA
    elif os_name == "grandstream-ht" or "ht802" in sys_name or "ata" in hostname:
        manufacturer = "Grandstream"
        role_name = "VoIP"
        role_slug = "voip"
        role_color = "009688"
        model = "HT802 VoIP ATA"

    # 6. Canon Printer
    elif os_name == "canonprinter" or "printer" in hostname or "mf642" in sys_name:
        manufacturer = "Canon"
        role_name = "Printer"
        role_slug = "printer"
        role_color = "607d8b"
        model = "Color imageCLASS MF642C"

    # 7. Docker host
    elif "docker" in hostname:
        manufacturer = "Docker"
        role_name = "Server"
        role_slug = "server"
        role_color = "ff9800"
        model = "Docker Host Server"

    # Clean serial number if default/placeholder
    raw_serial = dev.get("serial")
    serial = None
    if raw_serial and raw_serial not in ["None", "Default string", "0"]:
        serial = raw_serial.strip()

    return {
        "manufacturer": manufacturer,
        "model": model,
        "role_name": role_name,
        "role_slug": role_slug,
        "role_color": role_color,
        "serial": serial
    }


def sync_librenms_to_netbox(netbox, librenms, dry_run=False):
    print("=" * 65)
    print("Synchronizing Inventory from LibreNMS to NetBox")
    print(f"LibreNMS: {librenms.base_url}")
    print(f"NetBox:   {netbox.base_url}")
    print(f"Mode:     {'DRY-RUN (Simulated)' if dry_run else 'LIVE (Upsert)'}")
    print("=" * 65)

    # 1. Ensure Site exists
    print("\n[1/4] Checking Site...")
    site = netbox.get_or_create("dcim/sites/", {
        "name": "Homelab",
        "slug": "homelab",
        "status": "active",
        "description": "Primary Home Laboratory Network (iye.internal)"
    })
    site_id = site["id"]
    print(f"  -> Site 'Homelab' ready (ID: {site_id})")

    # 2. Fetch all devices from LibreNMS
    print("\n[2/4] Fetching devices from LibreNMS...")
    devices = librenms.get_devices()
    print(f"  -> Discovered {len(devices)} devices in LibreNMS.")

    # 3. Synchronize each device
    print("\n[3/4] Reconciling devices with NetBox...")
    for d in devices:
        hostname = d.get("hostname")
        ip = d.get("ip")
        meta = map_device_metadata(d)

        print(f"\nProcessing: {hostname} ({ip or 'No IP'})")
        print(f"  Classified: {meta['manufacturer']} | {meta['model']} | Role: {meta['role_name']}")

        # Ensure Manufacturer
        mfg = netbox.get_or_create("dcim/manufacturers/", {
            "name": meta["manufacturer"],
            "slug": slugify(meta["manufacturer"])
        })

        # Ensure Device Type
        dt = netbox.get_or_create("dcim/device-types/", {
            "manufacturer": mfg["id"],
            "model": meta["model"],
            "slug": slugify(meta["model"]),
            "u_height": 1
        })

        # Ensure Device Role
        role = netbox.get_or_create("dcim/device-roles/", {
            "name": meta["role_name"],
            "slug": meta["role_slug"],
            "color": meta["role_color"]
        })

        # Ensure Device
        dev_payload = {
            "name": hostname,
            "role": role["id"],
            "device_type": dt["id"],
            "site": site_id,
            "status": "active"
        }
        if meta["serial"]:
            dev_payload["serial"] = meta["serial"]

        existing_dev = netbox.request(f"dcim/devices/?name={urllib.parse.quote(hostname)}")
        if existing_dev.get("count", 0) > 0:
            dev_obj = existing_dev["results"][0]
            dev_id = dev_obj["id"]
            # Check if update needed (serial or role/model)
            updates = {}
            if meta["serial"] and dev_obj.get("serial") != meta["serial"]:
                updates["serial"] = meta["serial"]
            if dev_obj.get("role", {}).get("id") != role["id"]:
                updates["role"] = role["id"]
            if dev_obj.get("device_type", {}).get("id") != dt["id"]:
                updates["device_type"] = dt["id"]

            if updates:
                if dry_run:
                    print(f"  [DRY-RUN] Would update device #{dev_id}: {updates}")
                else:
                    netbox.request(f"dcim/devices/{dev_id}/", method="PATCH", data=updates)
                    print(f"  -> Updated device #{dev_id} with {updates}")
            else:
                print(f"  -> Device #{dev_id} up to date.")
        else:
            if dry_run:
                print(f"  [DRY-RUN] Would create device: {dev_payload}")
                dev_id = 9999
            else:
                created_dev = netbox.request("dcim/devices/", method="POST", data=dev_payload)
                dev_id = created_dev["id"]
                print(f"  -> Created new device #{dev_id} ({hostname})")

        # 4. Handle IPAM and Interfaces
        if ip:
            # Determine subnet prefix (e.g. 192.168.254.0/24)
            cidr_ip = f"{ip}/24"
            net = ip_network(f"{ip}/24", strict=False)
            prefix_str = str(net)

            # Ensure IPAM Prefix exists
            netbox.get_or_create("ipam/prefixes/", {
                "prefix": prefix_str,
                "site": site_id,
                "status": "active",
                "description": f"Homelab Subnet {prefix_str}"
            }, lookup_field="prefix")

            iface_name = "eth0"
            if "switch" in hostname:
                iface_name = "management"

            if dry_run and dev_id == 9999:
                print(f"  [DRY-RUN] Would create interface '{iface_name}' on new device")
                print(f"  [DRY-RUN] Would assign IP {cidr_ip} ({hostname}) as primary IP")
                continue

            # Check if any interface already exists on this device
            existing_ifaces = netbox.request(f"dcim/interfaces/?device_id={dev_id}")
            if existing_ifaces.get("count", 0) > 0:
                iface_obj = existing_ifaces["results"][0]
                iface_id = iface_obj["id"]
                iface_name = iface_obj["name"]
            else:
                if dry_run:
                    print(f"  [DRY-RUN] Would create interface '{iface_name}' on device #{dev_id}")
                    iface_id = 8888
                else:
                    new_iface = netbox.request("dcim/interfaces/", method="POST", data={
                        "device": dev_id,
                        "name": iface_name,
                        "type": "1000base-t"
                    })
                    iface_id = new_iface["id"]
                    print(f"  -> Created interface '{iface_name}' on device #{dev_id}")

            # Ensure IP address exists and is attached
            encoded_ip = urllib.parse.quote(cidr_ip)
            existing_ip = netbox.request(f"ipam/ip-addresses/?address={encoded_ip}")
            if existing_ip.get("count", 0) > 0:
                ip_obj = existing_ip["results"][0]
                ip_id = ip_obj["id"]
                # Update assignment if unassigned or assigned elsewhere
                needs_update = False
                ip_patch = {}
                if ip_obj.get("assigned_object_id") != iface_id:
                    ip_patch["assigned_object_type"] = "dcim.interface"
                    ip_patch["assigned_object_id"] = iface_id
                    needs_update = True
                if ip_obj.get("dns_name") != hostname:
                    ip_patch["dns_name"] = hostname
                    needs_update = True

                if needs_update:
                    if dry_run:
                        print(f"  [DRY-RUN] Would update IP #{ip_id}: {ip_patch}")
                    else:
                        netbox.request(f"ipam/ip-addresses/{ip_id}/", method="PATCH", data=ip_patch)
                        print(f"  -> Linked IP #{ip_id} to interface #{iface_id} ({hostname})")
            else:
                if dry_run:
                    print(f"  [DRY-RUN] Would create IP {cidr_ip} for interface #{iface_id}")
                    ip_id = 7777
                else:
                    new_ip = netbox.request("ipam/ip-addresses/", method="POST", data={
                        "address": cidr_ip,
                        "status": "active",
                        "assigned_object_type": "dcim.interface",
                        "assigned_object_id": iface_id,
                        "dns_name": hostname
                    })
                    ip_id = new_ip["id"]
                    print(f"  -> Created IP address {cidr_ip} (ID: {ip_id})")

            # Assign as primary_ip4 on device if not already set
            if not dry_run:
                curr_dev = netbox.request(f"dcim/devices/{dev_id}/")
                if not curr_dev.get("primary_ip4") or curr_dev["primary_ip4"].get("id") != ip_id:
                    netbox.request(f"dcim/devices/{dev_id}/", method="PATCH", data={
                        "primary_ip4": ip_id
                    })
                    print(f"  -> Assigned primary_ip4 ({cidr_ip}) to device #{dev_id}")

    print("\n[4/4] Synchronization complete!")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Synchronize devices from LibreNMS into NetBox.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate synchronization without modifying NetBox.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose API output.")
    parser.add_argument("--netbox-url", default=DEFAULT_NETBOX_URL, help="NetBox API base URL.")
    parser.add_argument("--netbox-token", default=DEFAULT_NETBOX_TOKEN, help="NetBox API authentication token.")
    parser.add_argument("--librenms-url", default=DEFAULT_LIBRENMS_URL, help="LibreNMS API base URL.")
    parser.add_argument("--librenms-token", default=DEFAULT_LIBRENMS_TOKEN, help="LibreNMS API authentication token.")

    args = parser.parse_args()

    netbox = NetboxClient(args.netbox_url, args.netbox_token, dry_run=args.dry_run, verbose=args.verbose)
    librenms = LibreNMSClient(args.librenms_url, args.librenms_token, verbose=args.verbose)

    sync_librenms_to_netbox(netbox, librenms, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

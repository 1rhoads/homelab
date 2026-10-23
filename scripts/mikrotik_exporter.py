#!/usr/bin/env python3
"""
MikroTik RouterOS Prometheus Exporter
Scrapes RouterOS v7 REST API and exports Prometheus metrics for switches/routers.
"""

import http.server
import json
import os
import sys
import time
import urllib.request
import urllib.error
import base64

HOST = os.getenv("MIKROTIK_HOST", "switch-1.iye.internal")
USER = os.getenv("MIKROTIK_USER", "admin")
PASSWORD = os.getenv("MIKROTIK_PASSWORD", "v01c3l@bNOW")
PORT = int(os.getenv("LISTEN_PORT", "9436"))

CACHE_DURATION = 5.0
_last_scrape = 0
_cached_metrics = ""

def parse_uptime(s):
    total = 0
    units = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
    num = ""
    for c in str(s):
        if c.isdigit():
            num += c
        elif c in units and num:
            total += int(num) * units[c]
            num = ""
    return total

def fetch_rest(endpoint):
    auth = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode("ascii")
    url = f"http://{HOST}/rest/{endpoint}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))

def generate_metrics():
    lines = []
    lines.append("# HELP mikrotik_up Reachability of MikroTik device")
    lines.append("# TYPE mikrotik_up gauge")
    
    try:
        res = fetch_rest("system/resource")
        health = fetch_rest("system/health")
        interfaces = fetch_rest("interface")
    except Exception as e:
        lines.append(f'mikrotik_up{{instance="{HOST}"}} 0')
        lines.append(f'# Scrape error: {e}')
        return "\n".join(lines) + "\n"

    lines.append(f'mikrotik_up{{instance="{HOST}"}} 1')
    
    # System Info
    board = res.get("board-name", "unknown")
    version = res.get("version", "unknown")
    arch = res.get("architecture-name", "unknown")
    cpu = res.get("cpu", "unknown")
    lines.append('# HELP mikrotik_system_info System hardware and firmware information')
    lines.append('# TYPE mikrotik_system_info gauge')
    lines.append(f'mikrotik_system_info{{instance="{HOST}",board_name="{board}",version="{version}",arch="{arch}",cpu="{cpu}"}} 1')

    # CPU & Frequency
    cpu_load = float(res.get("cpu-load", 0))
    cpu_freq = float(res.get("cpu-frequency", 0))
    lines.append('# HELP mikrotik_system_cpu_load_percent CPU utilization in percent')
    lines.append('# TYPE mikrotik_system_cpu_load_percent gauge')
    lines.append(f'mikrotik_system_cpu_load_percent{{instance="{HOST}"}} {cpu_load}')
    lines.append('# HELP mikrotik_system_cpu_frequency_mhz CPU clock frequency in MHz')
    lines.append('# TYPE mikrotik_system_cpu_frequency_mhz gauge')
    lines.append(f'mikrotik_system_cpu_frequency_mhz{{instance="{HOST}"}} {cpu_freq}')

    # Memory
    mem_total = int(res.get("total-memory", 0))
    mem_free = int(res.get("free-memory", 0))
    mem_used = max(0, mem_total - mem_free)
    lines.append('# HELP mikrotik_system_memory_total_bytes Total system memory in bytes')
    lines.append('# TYPE mikrotik_system_memory_total_bytes gauge')
    lines.append(f'mikrotik_system_memory_total_bytes{{instance="{HOST}"}} {mem_total}')
    lines.append('# HELP mikrotik_system_memory_free_bytes Free system memory in bytes')
    lines.append('# TYPE mikrotik_system_memory_free_bytes gauge')
    lines.append(f'mikrotik_system_memory_free_bytes{{instance="{HOST}"}} {mem_free}')
    lines.append('# HELP mikrotik_system_memory_used_bytes Used system memory in bytes')
    lines.append('# TYPE mikrotik_system_memory_used_bytes gauge')
    lines.append(f'mikrotik_system_memory_used_bytes{{instance="{HOST}"}} {mem_used}')

    # HDD / Storage
    hdd_total = int(res.get("total-hdd-space", 0))
    hdd_free = int(res.get("free-hdd-space", 0))
    hdd_used = max(0, hdd_total - hdd_free)
    lines.append('# HELP mikrotik_system_hdd_total_bytes Total storage space in bytes')
    lines.append('# TYPE mikrotik_system_hdd_total_bytes gauge')
    lines.append(f'mikrotik_system_hdd_total_bytes{{instance="{HOST}"}} {hdd_total}')
    lines.append('# HELP mikrotik_system_hdd_free_bytes Free storage space in bytes')
    lines.append('# TYPE mikrotik_system_hdd_free_bytes gauge')
    lines.append(f'mikrotik_system_hdd_free_bytes{{instance="{HOST}"}} {hdd_free}')
    lines.append('# HELP mikrotik_system_hdd_used_bytes Used storage space in bytes')
    lines.append('# TYPE mikrotik_system_hdd_used_bytes gauge')
    lines.append(f'mikrotik_system_hdd_used_bytes{{instance="{HOST}"}} {hdd_used}')

    # Uptime
    uptime_sec = parse_uptime(res.get("uptime", "0s"))
    lines.append('# HELP mikrotik_system_uptime_seconds Device uptime in seconds')
    lines.append('# TYPE mikrotik_system_uptime_seconds counter')
    lines.append(f'mikrotik_system_uptime_seconds{{instance="{HOST}"}} {uptime_sec}')

    # Health / Sensors
    lines.append('# HELP mikrotik_health_temperature_celsius Temperature sensor readings')
    lines.append('# TYPE mikrotik_health_temperature_celsius gauge')
    for h in health:
        name = h.get("name", "")
        val = h.get("value", "0")
        if name == "cpu-temperature":
            lines.append(f'mikrotik_health_temperature_celsius{{instance="{HOST}",sensor="cpu"}} {float(val)}')
        elif name == "phy-temperature":
            lines.append(f'mikrotik_health_temperature_celsius{{instance="{HOST}",sensor="phy"}} {float(val)}')
        elif name.startswith("board-temperature"):
            sensor_id = name.replace("board-temperature", "board")
            lines.append(f'mikrotik_health_temperature_celsius{{instance="{HOST}",sensor="{sensor_id}"}} {float(val)}')
        elif name.startswith("fan") and "speed" in name:
            fan_id = name.split("-")[0]
            lines.append(f'mikrotik_health_fan_speed_rpm{{instance="{HOST}",fan="{fan_id}"}} {float(val)}')
        elif name.startswith("fan") and "state" in name:
            fan_id = name.split("-")[0]
            st = 1 if val == "ok" else 0
            lines.append(f'mikrotik_health_fan_state{{instance="{HOST}",fan="{fan_id}"}} {st}')

    # Interfaces
    lines.append('# HELP mikrotik_interface_running Operational link status (1 = running, 0 = down)')
    lines.append('# TYPE mikrotik_interface_running gauge')
    lines.append('# HELP mikrotik_interface_receive_bytes_total Interface received bytes')
    lines.append('# TYPE mikrotik_interface_receive_bytes_total counter')
    lines.append('# HELP mikrotik_interface_transmit_bytes_total Interface transmitted bytes')
    lines.append('# TYPE mikrotik_interface_transmit_bytes_total counter')
    lines.append('# HELP mikrotik_interface_receive_packets_total Interface received packets')
    lines.append('# TYPE mikrotik_interface_receive_packets_total counter')
    lines.append('# HELP mikrotik_interface_transmit_packets_total Interface transmitted packets')
    lines.append('# TYPE mikrotik_interface_transmit_packets_total counter')
    lines.append('# HELP mikrotik_interface_receive_drop_total Interface received packet drops')
    lines.append('# TYPE mikrotik_interface_receive_drop_total counter')
    lines.append('# HELP mikrotik_interface_transmit_drop_total Interface transmitted packet drops')
    lines.append('# TYPE mikrotik_interface_transmit_drop_total counter')
    lines.append('# HELP mikrotik_interface_receive_error_total Interface received errors')
    lines.append('# TYPE mikrotik_interface_receive_error_total counter')
    lines.append('# HELP mikrotik_interface_transmit_error_total Interface transmitted errors')
    lines.append('# TYPE mikrotik_interface_transmit_error_total counter')
    lines.append('# HELP mikrotik_interface_link_downs_total Interface link down count')
    lines.append('# TYPE mikrotik_interface_link_downs_total counter')
    lines.append('# HELP mikrotik_interface_actual_mtu Interface operational MTU')
    lines.append('# TYPE mikrotik_interface_actual_mtu gauge')

    for iface in interfaces:
        iname = iface.get("name", "")
        itype = iface.get("type", "")
        # Clean label values to avoid broken quotes
        safe_iname = iname.replace('"', '')
        running = 1 if iface.get("running") == "true" else 0
        rx_b = int(iface.get("rx-byte", 0))
        tx_b = int(iface.get("tx-byte", 0))
        rx_p = int(iface.get("rx-packet", 0))
        tx_p = int(iface.get("tx-packet", 0))
        rx_d = int(iface.get("rx-drop", 0))
        tx_d = int(iface.get("tx-drop", 0))
        rx_e = int(iface.get("rx-error", 0))
        tx_e = int(iface.get("tx-error", 0))
        downs = int(iface.get("link-downs", 0))
        mtu = int(iface.get("actual-mtu", 1500))

        labels = f'instance="{HOST}",interface="{safe_iname}",type="{itype}"'
        lines.append(f'mikrotik_interface_running{{{labels}}} {running}')
        lines.append(f'mikrotik_interface_receive_bytes_total{{{labels}}} {rx_b}')
        lines.append(f'mikrotik_interface_transmit_bytes_total{{{labels}}} {tx_b}')
        lines.append(f'mikrotik_interface_receive_packets_total{{{labels}}} {rx_p}')
        lines.append(f'mikrotik_interface_transmit_packets_total{{{labels}}} {tx_p}')
        lines.append(f'mikrotik_interface_receive_drop_total{{{labels}}} {rx_d}')
        lines.append(f'mikrotik_interface_transmit_drop_total{{{labels}}} {tx_d}')
        lines.append(f'mikrotik_interface_receive_error_total{{{labels}}} {rx_e}')
        lines.append(f'mikrotik_interface_transmit_error_total{{{labels}}} {tx_e}')
        lines.append(f'mikrotik_interface_link_downs_total{{{labels}}} {downs}')
        lines.append(f'mikrotik_interface_actual_mtu{{{labels}}} {mtu}')

    return "\n".join(lines) + "\n"

class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _last_scrape, _cached_metrics
        if self.path == "/metrics":
            now = time.time()
            if now - _last_scrape > CACHE_DURATION:
                _cached_metrics = generate_metrics()
                _last_scrape = now
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(_cached_metrics.encode("utf-8"))
        elif self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress routine GET logging unless error
        if args and str(args[1]) != "200":
            super().log_message(format, *args)

def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), MetricsHandler)
    print(f"MikroTik Exporter listening on port {PORT} (monitoring {HOST})...", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()

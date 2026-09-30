#!/usr/bin/env python3
"""
Trivy Prometheus Exporter for Docker Container Security.
Discovers running containers via the Docker socket, performs vulnerability scans
against the centralized Trivy server, and exports Prometheus metrics.
"""

import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Set, Tuple

# Configuration
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9091"))
TRIVY_SERVER_URL = os.environ.get("TRIVY_SERVER_URL", "http://trivy:4954")
TRIVY_BIN = os.environ.get("TRIVY_BIN", "trivy")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "21600"))  # 6 hours
MAX_CONCURRENT_SCANS = int(os.environ.get("MAX_CONCURRENT_SCANS", "2"))
SCAN_ON_STARTUP = os.environ.get("SCAN_ON_STARTUP", "true").lower() in ("true", "1", "yes")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("trivy_exporter")

# Thread-safe metric storage
metrics_lock = threading.Lock()
current_metrics_text = ""
last_scan_time = 0.0
last_scan_duration = 0.0
scanned_images_count = 0
scan_in_progress = False
last_scan_summary = {}


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP client connection over a UNIX domain socket."""
    def __init__(self, socket_path: str, timeout: int = 15):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def escape_prom_label(val: str, max_len: int = 120) -> str:
    """Sanitize and escape string for Prometheus label values."""
    if val is None:
        return ""
    s = str(val).strip().replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")
    # Remove control characters
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def get_running_containers() -> Dict[str, List[str]]:
    """
    Query Docker daemon to discover running containers and their image names.
    Returns: Dict[image_name, list_of_container_names]
    """
    image_to_containers: Dict[str, List[str]] = {}

    if not os.path.exists(DOCKER_SOCKET):
        logger.warning(f"Docker socket {DOCKER_SOCKET} not found; cannot discover running containers dynamically.")
        return image_to_containers

    try:
        conn = UnixHTTPConnection(DOCKER_SOCKET, timeout=10)
        conn.request("GET", "/containers/json")
        res = conn.getresponse()
        if res.status != 200:
            logger.error(f"Failed to query Docker API: HTTP {res.status} {res.reason}")
            conn.close()
            return image_to_containers

        body = res.read().decode("utf-8")
        conn.close()
        containers = json.loads(body)

        for c in containers:
            image = c.get("Image", "")
            names = c.get("Names", [])
            primary_name = names[0].lstrip("/") if names else "unknown"
            if image:
                if image not in image_to_containers:
                    image_to_containers[image] = []
                image_to_containers[image].append(primary_name)

        logger.info(f"Discovered {len(image_to_containers)} unique images across {len(containers)} containers.")
    except Exception as e:
        logger.error(f"Error querying Docker socket {DOCKER_SOCKET}: {e}")

    return image_to_containers


def scan_single_image(image: str) -> Tuple[bool, dict]:
    """
    Execute Trivy client scan against the Trivy server for a specific image.
    Returns: (success: bool, scan_data: dict)
    """
    cmd = [
        TRIVY_BIN,
        "image",
        "--server", TRIVY_SERVER_URL,
        "--insecure",
        "--scanners", "vuln",
        "--quiet",
        "--format", "json",
        image
    ]

    try:
        t0 = time.time()
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180
        )
        duration = time.time() - t0

        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            logger.warning(f"Trivy scan failed for image '{image}' (code {proc.returncode}): {err_msg[:200]}")
            return False, {"error": err_msg, "duration": duration}

        stdout = proc.stdout.strip()
        if not stdout:
            return True, {"Results": [], "duration": duration}

        data = json.loads(stdout)
        data["duration"] = duration
        return True, data
    except subprocess.TimeoutExpired:
        logger.error(f"Trivy scan timed out after 180s for image: {image}")
        return False, {"error": "timeout", "duration": 180.0}
    except Exception as e:
        logger.error(f"Unexpected error scanning image {image}: {e}")
        return False, {"error": str(e), "duration": 0.0}


def run_scan_cycle():
    """
    Perform a full vulnerability scan cycle of all discovered container images,
    parse results, and generate Prometheus exposition text.
    """
    global scan_in_progress, current_metrics_text, last_scan_time, last_scan_duration, scanned_images_count, last_scan_summary

    if scan_in_progress:
        logger.info("Scan cycle already in progress; skipping trigger.")
        return

    scan_in_progress = True
    start_time = time.time()
    logger.info("Starting vulnerability scan cycle...")

    try:
        image_containers = get_running_containers()

        # Fallback to scanning trivy itself or predefined images if socket returned empty
        if not image_containers:
            fallback_images = os.environ.get("IMAGES_TO_SCAN", "").split(",")
            fallback_images = [img.strip() for img in fallback_images if img.strip()]
            if not fallback_images:
                fallback_images = ["aquasec/trivy:latest", "grafana/grafana-oss:latest", "prom/prometheus:latest"]
            for img in fallback_images:
                image_containers[img] = [img.split("/")[-1].split(":")[0]]

        scanned_count = len(image_containers)
        all_results = {}
        total_vulns = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}

        for image, containers in image_containers.items():
            primary_container = containers[0] if containers else image
            logger.info(f"Scanning image '{image}' (containers: {', '.join(containers)})...")
            success, result_data = scan_single_image(image)
            all_results[image] = {
                "success": success,
                "containers": containers,
                "data": result_data
            }

        # Build Prometheus metrics text
        lines = []
        lines.append("# HELP trivy_exporter_build_info Build information for Trivy Prometheus Exporter")
        lines.append("# TYPE trivy_exporter_build_info gauge")
        lines.append('trivy_exporter_build_info{version="1.0.0",trivy_server="' + escape_prom_label(TRIVY_SERVER_URL) + '"} 1')

        lines.append("# HELP trivy_last_scan_timestamp_seconds Unix timestamp of last scan cycle completion")
        lines.append("# TYPE trivy_last_scan_timestamp_seconds gauge")
        lines.append(f"trivy_last_scan_timestamp_seconds {int(time.time())}")

        lines.append("# HELP trivy_scanned_images_total Number of container images evaluated in the last scan")
        lines.append("# TYPE trivy_scanned_images_total gauge")
        lines.append(f"trivy_scanned_images_total {scanned_count}")

        lines.append("# HELP trivy_scan_duration_seconds Total execution time of last scan cycle in seconds")
        lines.append("# TYPE trivy_scan_duration_seconds gauge")
        cycle_duration = time.time() - start_time
        lines.append(f"trivy_scan_duration_seconds {cycle_duration:.2f}")

        lines.append("# HELP trivy_scan_status Image scan status (1 = success, 0 = failure)")
        lines.append("# TYPE trivy_scan_status gauge")

        lines.append("# HELP trivy_image_vulnerabilities Number of vulnerabilities found by Trivy per container image and severity")
        lines.append("# TYPE trivy_image_vulnerabilities gauge")

        lines.append("# HELP trivy_image_cve_info Detailed CVE vulnerability findings per container image")
        lines.append("# TYPE trivy_image_cve_info gauge")

        cve_info_lines = []

        severities = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]

        for image, res in all_results.items():
            containers = res["containers"]
            container_label = containers[0] if containers else image
            image_label = escape_prom_label(image)
            safe_cntr = escape_prom_label(container_label)

            status_val = 1 if res["success"] else 0
            lines.append(f'trivy_scan_status{{image="{image_label}",container="{safe_cntr}"}} {status_val}')

            # Initialize severity counts
            img_counts = {sev: 0 for sev in severities}

            if res["success"]:
                data = res["data"]
                results_list = data.get("Results") or []
                seen_cves: Set[str] = set()

                for r in results_list:
                    vulns = r.get("Vulnerabilities") or []
                    for v in vulns:
                        sev = v.get("Severity", "UNKNOWN").upper()
                        if sev not in img_counts:
                            sev = "UNKNOWN"
                        img_counts[sev] += 1
                        total_vulns[sev] += 1

                        cve_id = v.get("VulnerabilityID", "")
                        # Deduplicate CVEs per image
                        if cve_id and cve_id not in seen_cves:
                            seen_cves.add(cve_id)
                            # Emit detailed CVE info for CRITICAL and HIGH (and MEDIUM if < 40)
                            if sev in ("CRITICAL", "HIGH") or (sev == "MEDIUM" and len(seen_cves) < 50):
                                pkg_name = escape_prom_label(v.get("PkgName", ""))
                                inst_ver = escape_prom_label(v.get("InstalledVersion", ""))
                                fixed_ver = escape_prom_label(v.get("FixedVersion", "None"))
                                title = escape_prom_label(v.get("Title", "") or v.get("Description", "")[:80], max_len=80)
                                safe_cve = escape_prom_label(cve_id)
                                cve_info_lines.append(
                                    f'trivy_image_cve_info{{image="{image_label}",container="{safe_cntr}",cve_id="{safe_cve}",severity="{sev}",package="{pkg_name}",installed_version="{inst_ver}",fixed_version="{fixed_ver}",title="{title}"}} 1'
                                )

            # Emit vulnerability counts for all severities (including 0s)
            for sev in severities:
                cnt = img_counts[sev]
                lines.append(f'trivy_image_vulnerabilities{{image="{image_label}",container="{safe_cntr}",severity="{sev}"}} {cnt}')

        # Add the detailed CVE findings
        lines.extend(cve_info_lines)
        lines.append("")  # Trailing newline

        generated_text = "\n".join(lines)

        with metrics_lock:
            current_metrics_text = generated_text
            last_scan_time = time.time()
            last_scan_duration = cycle_duration
            scanned_images_count = scanned_count
            last_scan_summary = {
                "timestamp": int(last_scan_time),
                "duration_seconds": round(last_scan_duration, 2),
                "images_scanned": scanned_images_count,
                "totals": total_vulns
            }

        logger.info(
            f"Scan cycle complete in {cycle_duration:.1f}s. Scanned {scanned_count} images. "
            f"Found: CRITICAL={total_vulns['CRITICAL']}, HIGH={total_vulns['HIGH']}, "
            f"MEDIUM={total_vulns['MEDIUM']}, LOW={total_vulns['LOW']}."
        )
    except Exception as e:
        logger.error(f"Error during scan cycle: {e}", exc_info=True)
    finally:
        scan_in_progress = False


def scan_scheduler_loop():
    """Background scheduler thread running scans at SCAN_INTERVAL_SECONDS."""
    if SCAN_ON_STARTUP:
        # Give server time to bind before heavy scan
        time.sleep(2)
        run_scan_cycle()

    while True:
        time.sleep(SCAN_INTERVAL_SECONDS)
        run_scan_cycle()


class ExporterRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Prometheus metrics and health status."""

    def log_message(self, format, *args):
        # Suppress routine health check logs, log only non-200 or /scan requests
        if args and str(args[1]) != "200":
            logger.info("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))

    def do_GET(self):
        if self.path == "/metrics":
            with metrics_lock:
                output = current_metrics_text
            if not output:
                # Initial payload if scrape happens during initial boot
                output = (
                    "# HELP trivy_exporter_build_info Build information for Trivy Prometheus Exporter\n"
                    "# TYPE trivy_exporter_build_info gauge\n"
                    'trivy_exporter_build_info{version="1.0.0"} 1\n'
                    "# HELP trivy_last_scan_timestamp_seconds Unix timestamp of last scan cycle completion\n"
                    "# TYPE trivy_last_scan_timestamp_seconds gauge\n"
                    "trivy_last_scan_timestamp_seconds 0\n"
                    "# HELP trivy_scanned_images_total Number of container images evaluated in the last scan\n"
                    "# TYPE trivy_scanned_images_total gauge\n"
                    "trivy_scanned_images_total 0\n"
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            body = output.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            res_obj = {
                "status": "healthy",
                "scan_in_progress": scan_in_progress,
                "last_scan": last_scan_summary
            }
            body = json.dumps(res_obj, indent=2).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            with metrics_lock:
                summary = dict(last_scan_summary)
            totals = summary.get("totals", {})
            html = f"""<!DOCTYPE html>
<html>
<head>
  <title>Trivy Prometheus Exporter</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; }}
    h1 {{ color: #38bdf8; margin-bottom: 0.5rem; }}
    .card {{ background: #1e293b; border-radius: 8px; padding: 1.5rem; max-width: 600px; margin-top: 1rem; border: 1px solid #334155; }}
    .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-weight: bold; margin-right: 6px; }}
    .crit {{ background: #ef4444; color: white; }}
    .high {{ background: #f97316; color: white; }}
    .med {{ background: #eab308; color: black; }}
    .low {{ background: #3b82f6; color: white; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>Trivy Prometheus Exporter</h1>
  <p>Automated container image security scanner for Homelab Grafana &amp; Prometheus.</p>
  <div class="card">
    <h3>Latest Scan Status</h3>
    <p><strong>Images Scanned:</strong> {summary.get('images_scanned', 0)}</p>
    <p><strong>Duration:</strong> {summary.get('duration_seconds', 0)}s</p>
    <p><strong>Scan In Progress:</strong> {scan_in_progress}</p>
    <hr style="border: 0; border-top: 1px solid #334155; margin: 1rem 0;">
    <h4>Vulnerability Totals:</h4>
    <p>
      <span class="badge crit">CRITICAL: {totals.get('CRITICAL', 0)}</span>
      <span class="badge high">HIGH: {totals.get('HIGH', 0)}</span>
      <span class="badge med">MEDIUM: {totals.get('MEDIUM', 0)}</span>
      <span class="badge low">LOW: {totals.get('LOW', 0)}</span>
    </p>
    <p style="margin-top: 1.5rem;"><a href="/metrics">&rarr; View Prometheus Metrics (/metrics)</a></p>
    <p><a href="/healthz">&rarr; Health Check (/healthz)</a></p>
  </div>
</body>
</html>"""
            body = html.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/scan":
            threading.Thread(target=run_scan_cycle, daemon=True).start()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            res_obj = {"status": "scan_initiated"}
            body = json.dumps(res_obj).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    logger.info(f"Starting Trivy Exporter HTTP server on {LISTEN_HOST}:{LISTEN_PORT}...")
    logger.info(f"Trivy server target: {TRIVY_SERVER_URL}")
    logger.info(f"Docker socket path: {DOCKER_SOCKET}")
    logger.info(f"Scan interval: {SCAN_INTERVAL_SECONDS} seconds ({SCAN_INTERVAL_SECONDS / 3600:.1f} hours)")

    # Start background scheduler thread
    scheduler_thread = threading.Thread(target=scan_scheduler_loop, daemon=True)
    scheduler_thread.start()

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), ExporterRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down exporter server...")
        server.server_close()


if __name__ == "__main__":
    main()

# Homelab Operations Stack

A production-ready [Portainer](https://www.portainer.io/) and Docker Compose stack specifically architected for homelabs running **multiple MikroTik switches**, **3 Proxmox VE servers**, and **multiple OpenWrt access points**.

This stack provides centralized network monitoring, automated configuration versioning, telemetry metrics, visualization dashboards, unified reverse proxying with local TLS, automated vulnerability scanning, and an interactive **Homepage** status dashboard.

---

## Architecture Overview

```
                                  +------------------------------------+
                                  |    Local Network / Homelab Clients |
                                  +-----------------+------------------+
                                                    |
                                                    v
                                      +---------------------------+
                                      |     Caddy Reverse Proxy   |
                                      |    (Internal TLS / CA)    |
                                      +-------------+-------------+
                                                    |
         +------------------+-----------------------+-----------------------+--------------------+
         |                  |                       |                       |                    |
         v                  v                       v                       v                    v
+-----------------+ +-----------------+   +-------------------+   +------------------+ +-----------------+
|    Homepage     | |    LibreNMS     |   |      Grafana      |   |    Prometheus    | |    Oxidized     |
| Dashboard Hub   | | Network Monitor |   |    Dashboards     |   |  Metrics Engine  | |  Config Backup  |
| (Live Widgets)  | | (SNMP/Syslog)   |   | (Auto-Datasource) |   |  (Time Series)   | |  (Git Version)  |
+--------+--------+ +--------+--------+   +---------+---------+   +---------+--------+ +--------+--------+
         |                   |                      |                       |                   |
         | Docker Socket     |                      |                       |                   |
         v                   |                      |                       |                   |
+-----------------+          +<-- Node API Sync ------------------------------------------------+
|  Trivy Scanner  |          |                                              |
|  (Vuln Engine)  |          +------------------------- Monitored Infrastructure
+-----------------+                                    |                 |
                                       +---------------+                 +---------------+
                                       v                                 v               v
                             +-------------------+             +------------------+ +-------------------+
                             | MikroTik Switches |             |   Proxmox VE     | |  OpenWrt APs      |
                             | (switch-1, 2)     |             | (virt-1, 2, 3)   | | (6 AP Nodes)      |
                             +-------------------+             +------------------+ +-------------------+
```

### Included Services

| Service | Container Image | Port | Description |
| :--- | :--- | :--- | :--- |
| **Caddy** | `caddy:2-alpine` | `80`, `443` | Reverse proxy with automatic local TLS (`tls internal`) routing domains to containers |
| **Homepage** | `ghcr.io/gethomepage/homepage:latest` | `3001` (3000) | Modern status dashboard with Docker health badges, switches, APs & Proxmox |
| **LibreNMS** | `librenms/librenms:latest` | `3300` (8000) | Autodiscovery, SNMP monitoring, alerting, interface state tracking |
| **Dispatcher** | `librenms/librenms:latest` | — | Sidecar poller and discovery worker pool for LibreNMS |
| **Syslog-NG** | `librenms/librenms:latest` | `514` (UDP/TCP) | Centralized syslog collector for all MikroTik, Proxmox, and OpenWrt logs |
| **SNMP Trapd** | `librenms/librenms:latest` | `162` (UDP/TCP) | Ingests real-time hardware alerts and link state traps from switches |
| **Oxidized** | `oxidized/oxidized:latest` | `8888` | Automated configuration backup engine saving RouterOS and OpenWrt configs into Git |
| **Prometheus** | `prom/prometheus:latest` | `9090` | Time-series metrics scraper with alert rules for Proxmox, switches, and APs |
| **Grafana** | `grafana/grafana-oss:latest` | `3000` | Telemetry dashboards pre-provisioned with the Prometheus data source |
| **Trivy** | `aquasec/trivy:latest` | `4954` | Vulnerability and security scanner server for containers, images, and filesystems |
| **NetBox** | `netboxcommunity/netbox:latest` | `8080` | IPAM & DCIM network infrastructure source of truth, device modeling & prefix tracking |
| **NetBox Worker** | `netboxcommunity/netbox:latest` | — | Redis Queue (RQ) background task worker for NetBox webhooks, scripts & reports |
| **PostgreSQL** | `postgres:16-alpine` | — | Dedicated relational database backend for NetBox |
| **NetBox Redis** | `redis:7-alpine` | — | Dedicated caching and message queue broker for NetBox |
| **MariaDB** | `mariadb:10.11` | — | High-performance LTS database store for LibreNMS |
| **Redis** | `redis:7.2-alpine` | — | Queue management and caching backend for LibreNMS |
| **msmtpd** | `crazymax/msmtpd:latest` | — | Outbound email relay for LibreNMS alert notifications |

---

## DNS Configuration Reference

To access all services by hostname and allow devices to communicate across the `.iye.internal` domain, configure the following DNS records in your local resolver (Pi-hole, AdGuard Home, pfSense/OPNsense Unbound, or MikroTik DNS):

### 1. Stack Service Records (Point to Docker Host IP)
All HTTP/HTTPS requests to these URLs are intercepted by **Caddy** on ports 80/443 and routed to the proper container:

| Hostname / FQDN | Record Type | Target IP | Destination Service |
| :--- | :---: | :---: | :--- |
| **`homelab.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | **Homepage Operations Dashboard** |
| **`iye.internal`** | `A` | `<DOCKER_HOST_IP>` | Homepage Dashboard (Apex domain fallback) |
| **`netbox.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | NetBox IPAM & DCIM Web UI & REST API |
| **`librenms.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | LibreNMS Web UI |
| **`grafana.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | Grafana Telemetry Dashboards |
| **`prometheus.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | Prometheus Web UI & Scrape Engine |
| **`oxidized.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | Oxidized Web UI & REST API |
| **`trivy.iye.internal`** | `A` or `CNAME` | `<DOCKER_HOST_IP>` | Trivy Security Scanner API |

> **Wildcard Shortcut**: If your DNS server supports wildcards, you can create a single wildcard entry: `*.iye.internal` &rarr; `<DOCKER_HOST_IP>`.

---

### 2. Physical Host & Device Records (Point to Device LAN IPs)
These records allow LibreNMS, Prometheus, and Homepage to reach your physical hardware by name:

| Hostname / FQDN | Target IP (Verified) | Device Role | Web Interface Port |
| :--- | :--- | :--- | :--- |
| **`virt-1.iye.internal`** | `192.168.254.111` | Proxmox VE Node 1 | `8006` (HTTPS) |
| **`virt-2.iye.internal`** | `192.168.254.112` | Proxmox VE Node 2 | `8006` (HTTPS) |
| **`virt-3.iye.internal`** | `192.168.254.113` | Proxmox VE Node 3 | `8006` (HTTPS) |
| **`docker-homelab.iye.internal`** | `192.168.254.164` | Docker Host & Portainer | `9443` (HTTPS) |
| **`switch-1.iye.internal`** | `<SWITCH_1_IP>` | MikroTik Core Switch | `80` (WebFig / RouterOS) |
| **`switch-2.iye.internal`** | `<SWITCH_2_IP>` | MikroTik Distribution Switch | `80` (WebFig / SwOS) |
| **`ap-outside.iye.internal`** | `192.168.0.44` | OpenWrt AP (Outside) | `80` (LuCI) |
| **`ap-callie.iye.internal`** | `192.168.0.42` | OpenWrt AP (Callie) | `80` (LuCI) |
| **`ap-robert.iye.internal`** | `192.168.0.47` | OpenWrt AP (Robert) | `80` (LuCI) |
| **`ap-garage.iye.internal`** | `192.168.0.41` | OpenWrt AP (Garage) | `80` (LuCI) |
| **`ap-printer.iye.internal`** | `192.168.0.45` | OpenWrt AP (Printer) | `80` (LuCI) |
| **`ap-fort.iye.internal`** | `192.168.0.46` | OpenWrt AP (Fort) | `80` (LuCI) |

#### Example `/etc/hosts` Block (for local testing):
```hosts
# Homelab Stack Services (Docker Host)
192.168.254.164  homelab.iye.internal iye.internal netbox.iye.internal librenms.iye.internal grafana.iye.internal prometheus.iye.internal oxidized.iye.internal trivy.iye.internal docker-homelab.iye.internal

# Homelab Devices
192.168.254.111  virt-1.iye.internal
192.168.254.112  virt-2.iye.internal
192.168.254.113  virt-3.iye.internal
192.168.0.44     ap-outside.iye.internal
192.168.0.42     ap-callie.iye.internal
192.168.0.47     ap-robert.iye.internal
192.168.0.41     ap-garage.iye.internal
192.168.0.45     ap-printer.iye.internal
192.168.0.46     ap-fort.iye.internal
# <SWITCH_1_IP>  switch-1.iye.internal
# <SWITCH_2_IP>  switch-2.iye.internal
```

---

## Host & Device Setup Commands

### 1. Proxmox VE (Run on all 3 PVE Nodes)
SSH into **each** of your 3 Proxmox nodes (`virt-1`, `virt-2`, `virt-3`) and run:

```bash
# A. Install Prometheus Node Exporter (System CPU, RAM, Disk, Network)
apt update && apt install -y prometheus-node-exporter
systemctl enable --now prometheus-node-exporter

# B. Install and Configure SNMP for LibreNMS
apt install -y snmpd
echo "rocommunity homelab <DOCKER_HOST_IP>" >> /etc/snmp/snmpd.conf
systemctl restart snmpd

# C. Install rsyslog and Forward Logs to LibreNMS Syslog-NG Container
apt install -y rsyslog
echo "*.* @<DOCKER_HOST_IP>:514" > /etc/rsyslog.d/50-remote.conf
systemctl enable --now rsyslog
```

---

### 2. MikroTik Switches (Run on each switch via RouterOS Terminal)
Connect via SSH or WebFig terminal to each switch:

```routeros
# A. Enable SNMP for LibreNMS
/snmp community set [ find default=yes ] name=homelab addresses=0.0.0.0/0
/snmp set enabled=yes contact="admin@iye.internal" location="Rack"

# B. Forward Syslog to Stack
/system logging action add name=librenms target=remote remote=<DOCKER_HOST_IP> remote-port=514 src-address=0.0.0.0
/system logging add action=librenms topics=info,warning,error

# C. Enable Native Prometheus Exporter (RouterOS v7)
/tool metrics export prometheus

# D. Create Oxidized Backup User
/user group add name=oxidized policy=read,api,test,ssh
/user add name=oxidized group=oxidized password="StrongPassword123!"
```

---

### 3. OpenWrt Access Points (Run on each AP via SSH)
SSH into each OpenWrt AP:

```sh
# A. Install Prometheus Node Exporter with WiFi Telemetry (OpenWrt 24/25+ uses apk; legacy versions use opkg)
apk update
apk add prometheus-node-exporter-lua \
        prometheus-node-exporter-lua-wifi \
        prometheus-node-exporter-lua-wifi_stations \
        prometheus-node-exporter-lua-netstat \
        prometheus-node-exporter-lua-openwrt
uci set prometheus-node-exporter-lua.main.listen_interface='*'
uci commit prometheus-node-exporter-lua
/etc/init.d/prometheus-node-exporter-lua enable
/etc/init.d/prometheus-node-exporter-lua restart

# B. Enable SNMP for LibreNMS
opkg install snmpd
uci set snmpd.@agent[0].agentaddress='UDP:161'
uci delete snmpd.public
uci set snmpd.homelab=snmpd.read_access
uci set snmpd.homelab.community='homelab'
uci commit snmpd
/etc/init.d/snmpd enable
/etc/init.d/snmpd restart

# C. Forward Syslog to Stack
uci set system.@system[0].log_ip='<DOCKER_HOST_IP>'
uci set system.@system[0].log_port='514'
uci set system.@system[0].log_proto='udp'
uci commit system
/etc/init.d/log restart
```

---

### 4. Docker Host DNS Settings
To guarantee that your Docker containers (like Prometheus, LibreNMS, and Oxidized) can resolve `.iye.internal` hostnames:
1. Ensure the Docker host's `/etc/resolv.conf` lists your local LAN DNS server (e.g. your router or Pi-hole IP).
2. Docker containers automatically inherit the host's DNS servers.

---

## Deployment via Portainer

### Fresh Stack Creation (Recommended)

1. Log into **Portainer**.
2. Go to **Stacks** &rarr; click **+ Add stack**.
3. Select **Repository** mode:
   - **Name**: `homelab`
   - **Repository URL**: `https://github.com/1rhoads/homelab.git`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `compose.yml`
4. Under **Environment variables**, paste your settings:
   ```ini
   DOMAIN=iye.internal
   PROXMOX_NODE1_HOST=virt-1.iye.internal
   PROXMOX_NODE2_HOST=virt-2.iye.internal
   PROXMOX_NODE3_HOST=virt-3.iye.internal
   PORTAINER_HOST=docker-homelab.iye.internal
   MIKROTIK_CORE_HOST=switch-1.iye.internal
   MIKROTIK_SW2_HOST=switch-2.iye.internal
   OPENWRT_AP1_HOST=ap-outside.iye.internal
   OPENWRT_AP2_HOST=ap-callie.iye.internal
   OPENWRT_AP3_HOST=ap-robert.iye.internal
   OPENWRT_AP4_HOST=ap-garage.iye.internal
   OPENWRT_AP5_HOST=ap-printer.iye.internal
   OPENWRT_AP6_HOST=ap-fort.iye.internal
   MYSQL_PASSWORD=your_secure_password
   GRAFANA_ADMIN_PASSWORD=your_secure_password
   ```
5. Click **Deploy the stack**.

---

## Pre-Provisioned Grafana Dashboards

All Grafana dashboards are automatically provisioned into the **Homelab** folder upon deployment. Telemetry is populated directly by the Prometheus scraping engine:

| Dashboard | File | Metrics & Telemetry Covered |
| :--- | :--- | :--- |
| **Homelab Overview** | [`homelab-overview.json`](grafana/provisioning/dashboards/json/homelab-overview.json) | Real-time target reachability (Up/Down) for all jobs, cluster resource averages, active WiFi client totals, and Caddy HTTP request rates/p95 latency. |
| **Proxmox VE Cluster** | [`proxmox-cluster.json`](grafana/provisioning/dashboards/json/proxmox-cluster.json) | Total cluster capacity (28 cores, 77 GB RAM), per-node CPU/RAM utilization gauges, load averages, ZFS ARC cache size & hit rates, root storage, and network interface throughput (`virt-1`, `virt-2`, `virt-3`). |
| **OpenWrt APs & WiFi** | [`openwrt-wifi.json`](grafana/provisioning/dashboards/json/openwrt-wifi.json) | Active client counts (77+ clients), client distribution per AP, 5 GHz vs 2.4 GHz radio frequency split, 802.11s mesh backhaul status & signal dBm, and AP CPU/memory health across all 6 APs. |
| **Docker Host Telemetry** | [`docker-host.json`](grafana/provisioning/dashboards/json/docker-host.json) | Host uptime, CPU mode breakdown (user, system, iowait), 1m/5m/15m load averages, memory allocation (used, cached, buffers, swap), NVMe disk space & I/O, and `eth0` network throughput. |

---

## Post-Deployment: Oxidized Network Backups

By default, Oxidized automatically tracks your switches and access points via the built-in [oxidized/router.db](oxidized/router.db) inventory.

### (Optional) Switching Oxidized to LibreNMS Dynamic Inventory:
If you prefer LibreNMS to dynamically provide device inventory to Oxidized instead of `router.db`:
1. In LibreNMS, navigate to `https://librenms.iye.internal/api-access/` and generate an API Token.
2. In `compose.yml` (under `oxidized_config`) and [oxidized/config](oxidized/config), change `default: csv` to `default: http` and set `X-Auth-Token` to that token.
3. In LibreNMS &rarr; **Global Settings** &rarr; **External** &rarr; **Oxidized**:
   - Enable Oxidized Support: **ON**
   - URL: `http://librenms_oxidized:8888`
   - Config Versioning: **ON**
   - Reload nodes list each time a device is added: **ON**
4. All your MikroTik switches and OpenWrt APs will automatically have their configurations backed up, diffed, and versioned in Git.

---

## Maintenance & Operations

### View Service Logs
```bash
# View all logs
docker compose logs -f

# View specific service logs
docker compose logs -f caddy
docker compose logs -f homepage
docker compose logs -f librenms
docker compose logs -f oxidized
docker compose logs -f prometheus
```

### Reload Prometheus Scrape Targets
```bash
curl -X POST http://<DOCKER_HOST_IP>:9090/-/reload
```

---

## License
MIT License. Free to use, adapt, and share in your homelab.

# Homelab Operations Stack

A production-ready [Portainer](https://www.portainer.io/) and Docker Compose stack specifically architected for homelabs running **multiple MikroTik switches**, **Proxmox VE servers**, and **multiple OpenWrt access points**.

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
                             | (Core, PoE, Edge) |             |  (Hypervisors)   | | (Living Rm, Office|
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
| **MariaDB** | `mariadb:10.11` | — | High-performance LTS database store for LibreNMS |
| **Redis** | `redis:7.2-alpine` | — | Queue management and caching backend for LibreNMS |
| **msmtpd** | `crazymax/msmtpd:latest` | — | Outbound email relay for LibreNMS alert notifications |

---

## Scaling to Multiple Switches & APs

This stack is designed from the ground up to scale effortlessly across multiple devices:

### 1. Automatic Discovery in LibreNMS
Rather than adding each switch and access point manually:
1. In LibreNMS, navigate to **Settings (Gear icon)** &rarr; **Global Settings** &rarr; **Discovery** &rarr; **Subnets**.
2. Add your management subnet (e.g., `192.168.1.0/24`).
3. Under **Poller** &rarr; **Communities**, add your SNMP community (default: `homelab`).
4. LibreNMS will automatically scan the subnet, discover every switch and AP, and map inter-switch and AP links using **LLDP/CDP**!

### 2. Automated Multi-Device Backups in Oxidized
Oxidized does **not** need manual IP configuration for each device:
- It queries LibreNMS's API (`/api/v0/oxidized`) dynamically.
- When you add 5 switches and 10 APs to LibreNMS, LibreNMS hands the entire list to Oxidized.
- Oxidized matches all MikroTik devices to `model: routeros` and all APs to `model: openwrt`, connects via SSH, and commits every device's configuration to Git separately (`<hostname>.cfg`).

### 3. Multi-Device Metrics in Prometheus
In [prometheus/prometheus.yml](prometheus/prometheus.yml), you can add any number of target IPs:
```yaml
  - job_name: "mikrotik"
    static_configs:
      - targets:
          - "192.168.1.2:80"   # Core Switch
          - "192.168.1.3:80"   # Distribution / PoE Switch
          - "192.168.1.4:80"   # Edge Switch

  - job_name: "openwrt"
    static_configs:
      - targets:
          - "192.168.1.11:9100"  # AP Living Room
          - "192.168.1.12:9100"  # AP Office
          - "192.168.1.13:9100"  # AP Garage / Outdoor
```

### 4. Dedicated Sections in Homepage
In [homepage/config/services.yaml](homepage/config/services.yaml), switches and APs are organized into their own clean grid categories (**Network Switches** and **Wireless Access Points**), providing one-click access to each device's WebFig or LuCI interface.

---

## Device Setup Commands

### MikroTik Switches (RouterOS)
Run on each of your MikroTik switches:
```routeros
# Enable SNMP
/snmp community set [ find default=yes ] name=homelab addresses=0.0.0.0/0
/snmp set enabled=yes contact="admin@iye.internal" location="Rack"

# Forward Syslog to Docker Host
/system logging action add name=librenms target=remote remote=<DOCKER_HOST_IP> remote-port=514 src-address=0.0.0.0
/system logging add action=librenms topics=info,warning,error

# Enable Native Prometheus Metrics (RouterOS v7)
/tool metrics export prometheus

# Create Oxidized Backup User
/user group add name=oxidized policy=read,api,test,ssh
/user add name=oxidized group=oxidized password="StrongPassword123!"
```

---

### OpenWrt Access Points
Run on each of your OpenWrt APs via SSH:
```sh
# 1. Install Prometheus Node Exporter (WiFi + System Telemetry)
opkg update
opkg install prometheus-node-exporter-lua \
             prometheus-node-exporter-lua-wifi \
             prometheus-node-exporter-lua-netstat \
             prometheus-node-exporter-lua-openwrt
/etc/init.d/prometheus-node-exporter-lua enable
/etc/init.d/prometheus-node-exporter-lua start

# 2. Enable SNMP for LibreNMS
opkg install snmpd
uci set snmpd.@agent[0].agentaddress='UDP:161'
uci delete snmpd.public
uci set snmpd.homelab=snmpd.read_access
uci set snmpd.homelab.community='homelab'
uci commit snmpd
/etc/init.d/snmpd enable
/etc/init.d/snmpd restart

# 3. Forward Syslog to Docker Host
uci set system.@system[0].log_ip='<DOCKER_HOST_IP>'
uci set system.@system[0].log_port='514'
uci set system.@system[0].log_proto='udp'
uci commit system
/etc/init.d/log restart
```

---

### Proxmox VE
Run on your Proxmox server(s):
```bash
# Prometheus Node Exporter
apt update && apt install -y prometheus-node-exporter
systemctl enable --now prometheus-node-exporter

# SNMP for LibreNMS
apt install -y snmpd
echo "rocommunity homelab <DOCKER_HOST_IP>" >> /etc/snmp/snmpd.conf
systemctl restart snmpd

# Syslog Forwarding
echo "*.* @<DOCKER_HOST_IP>:514" > /etc/rsyslog.d/50-remote.conf
systemctl restart rsyslog
```

---

## Deployment via Portainer

1. Push your changes to your repository:
   ```bash
   git add .
   git commit -m "Configure multi-device monitoring, Caddy, and Homepage"
   git push origin main
   ```
2. In **Portainer**, navigate to **Stacks** &rarr; select **homelab**.
3. Under **Environment variables**, set:
   - `DOMAIN`: `iye.internal`
   - `PROXMOX_NODE1_HOST`: `<PROXMOX_NODE1_IP>`
   - `PROXMOX_NODE2_HOST`: `<PROXMOX_NODE2_IP>`
   - `PROXMOX_NODE3_HOST`: `<PROXMOX_NODE3_IP>`
   - `MIKROTIK_CORE_HOST`: `<CORE_SWITCH_IP>`
   - `MIKROTIK_SW2_HOST`: `<SWITCH_2_IP>`
   - `OPENWRT_AP1_HOST`: `<AP_1_IP>`
   - `OPENWRT_AP2_HOST`: `<AP_2_IP>`
   - `MYSQL_PASSWORD`: `<YOUR_PASSWORD>`
   - `GRAFANA_ADMIN_PASSWORD`: `<YOUR_PASSWORD>`
4. Click **Pull and redeploy** with **Re-pull image** toggled on.

---

## Service URLs

With `DOMAIN=iye.internal` configured:
- **Homepage Dashboard**: `https://homelab.iye.internal` (or `https://iye.internal`)
- **LibreNMS**: `https://librenms.iye.internal` (or direct: `http://<DOCKER_HOST_IP>:3300`)
- **Grafana**: `https://grafana.iye.internal` (or direct: `http://<DOCKER_HOST_IP>:3000`)
- **Prometheus**: `https://prometheus.iye.internal` (or direct: `http://<DOCKER_HOST_IP>:9090`)
- **Oxidized**: `https://oxidized.iye.internal` (or direct: `http://<DOCKER_HOST_IP>:8888`)
- **Trivy**: `https://trivy.iye.internal` (or direct: `http://<DOCKER_HOST_IP>:4954`)

---

## Connecting Oxidized to LibreNMS
Once LibreNMS has discovered your switches and APs:
1. In LibreNMS, go to `https://librenms.iye.internal/api-access/` and generate an API Token.
2. Edit [oxidized/config](oxidized/config#L68) and set `X-Auth-Token` to that token.
3. In LibreNMS &rarr; **Global Settings** &rarr; **External** &rarr; **Oxidized**:
   - Enable Oxidized Support: **ON**
   - URL: `http://librenms_oxidized:8888`
   - Config Versioning: **ON**
   - Reload nodes list each time a device is added: **ON**
4. All your MikroTik switches and OpenWrt APs will automatically have their configurations backed up, diffed, and versioned in Git.

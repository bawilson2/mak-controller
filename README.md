# MAK Pellet Boss Local Controller & Dashboard

A local, offline-capable control system and telemetry dashboard for MAK grills equipped with the Wi-Fi Pellet Boss controller.

<p align="center">
  <img src="assets/dashboard.png" alt="MAK Controller Dashboard" width="640">
</p>

This service acts as a drop-in replacement for the original cloud backend (`makgrillsmobile.com`). By intercepting the grill's outbound polling using a local DNS rewrite, it provides complete local control, multi-stage cook automation, probe target alerts, historical session logging, and push notifications—keeping your grill functional without third-party cloud dependence.

---

## Features

* **Complete Local Control:** Adjust setpoints (150°F–500°F), view live chamber temperature, and trigger controlled shutdowns.
* **Live Telemetry & Diagnostics:** Real-time strip chart for pit and meat probe temperatures, plus monitoring of controller state flags (`ATSET`, transitions).
  <p align="center">
    <img src="assets/graph.png" alt="MAK Controller Live Telemetry" width="500">
  </p>
* **Multi-Stage Recipe Automation:** Build multi-step cooks triggered by elapsed time (minutes), meat probe internal temperatures (with `>=` or `<=` directional thresholds), or indefinite hold stages.
   <p align="center">
     <img src="assets/recipe-builder.png" alt="MAK Controller Automation" width="500">
   </p>
* **Probe Alerts & Web Audio:** Set target internal temperatures for Probes 1–3 with visual pulsing cards and browser-based audio chimes.
* **Safety Watchdogs:**
  * **Flameout Detection:** Alerts when pit temperature falls >35°F below the target setpoint for longer than 8 consecutive minutes while running.
  * **Cooldown Interlock:** Respects the Pellet Boss fan-assisted cooldown cycle, locking out premature restarts until shutdown completes.
* **Push Notifications:** Native integration with [ntfy](https://ntfy.sh/) for probe target completions, stage transitions, target setpoint confirmations (`ATSET`), and flameout warnings.
* **Persistent Cook Sessions:** Log individual cooks into a persistent SQLite database with one-click CSV export and maintenance pruning tools.
* **Production-Ready Backend:** Runs via Gunicorn multi-threaded WSGI with SQLite Write-Ahead Logging (`WAL` mode) for reliable concurrency.

---

## How It Works

The Pellet Boss Wi-Fi module periodically sends an HTTP POST request containing current sensor telemetry to `http://makgrillsmobile.com/GrillService/Service` and expects a formatted query string response specifying the active setpoint and power state.

```
[ Pellet Boss Grill ]
         |
         | (Outbound HTTP POST every 5s)
         v
[ Local DNS Rewrite ] ---> Points 'makgrillsmobile.com' to Container IP
         v
[ MAK Controller (Gunicorn/Flask) ] ---> Local SQLite DB & Dark-Themed Web UI
```

---

## Prerequisites

1. **Local DNS Server:** You must run a local DNS server (such as **AdGuard Home**, **Pi-hole**, or a router running pfSense/OPNsense/OpenWrt) capable of creating custom DNS rewrites.
2. **Docker & Docker Compose:** Installed on a home server, Raspberry Pi, or NAS.

---

## Installation & Deployment

### 1. Network Configuration: DNS Rewrite

Add a custom DNS rewrite entry in your local DNS server:
* **Domain:** `makgrillsmobile.com`
* **Target IP:** The IP address where this container will listen on port 80.

> **Note:** The Pellet Boss firmware communicates strictly over standard HTTP (port 80). Traffic routed to this controller **must** land on port 80.

---

### 2. Choose Your Deployment Mode

#### Option A: Standard Bridge Mode (Recommended for Raspberry Pi / Dedicated Linux)
Use this option if port 80 is not already used by another service on your host machine.

1. Clone this repository:
   ```bash
   git clone https://github.com/bawilson2/mak-controller.git
   cd mak-controller
   ```
2. Copy the environment file template:
   ```bash
   cp .env.example .env
   ```
3. Start the container:
   ```bash
   docker compose up -d
   ```
4. Point your DNS rewrite for `makgrillsmobile.com` to the IP address of the host machine.

---

#### Option B: Macvlan Mode (For TrueNAS SCALE, Synology, or Hosts with Port 80 Conflicts)
If your host OS WebGUI already occupies port 80 (common on TrueNAS SCALE and NAS platforms), you must assign the container its own distinct IP on your LAN using a `macvlan` network.

##### Deployment via TrueNAS SCALE Web UI:
1. Navigate to **Apps** > **Discover Apps** > **Custom App**.
2. Paste the following configuration directly into the YAML editor, adjusting the interface, subnet, gateway, dataset path, and IP address to match your network:

```yaml
services:
  mak-controller:
    image: ghcr.io/bawilson2/mak-controller:latest
    container_name: mak-controller
    restart: unless-stopped
    volumes:
      - /mnt/<your-pool>/apps/mak-controller/data:/data
    networks:
      lan_net:
        ipv4_address: 192.168.0.95
    environment:
      - TZ=America/Los_Angeles
      - DB_PATH=/data/cooks.db
      - LOG_LEVEL=INFO

networks:
  lan_net:
    driver: macvlan
    driver_opts:
      parent: eno1  # Replace with your physical network interface
    ipam:
      config:
        - subnet: 192.168.0.0/24
          gateway: 192.168.0.1
          ip_range: 192.168.0.95/32
```

##### Deployment via CLI (with `.env`):
1. Copy and populate `.env`:
   ```bash
   cp .env.example .env
   ```
   Set `CONTAINER_IP`, `PARENT_INTERFACE`, `SUBNET`, and `GATEWAY`.
2. Start with the macvlan compose file:
   ```bash
   docker compose -f docker-compose.macvlan.yml up -d
   ```
3. Point your DNS rewrite for `makgrillsmobile.com` to `CONTAINER_IP` (e.g., `192.168.0.95`).

---

## Configuration & Usage

1. Open your browser and navigate to the controller IP (e.g., `http://192.168.0.95`).
2. Turn on the grill. Within 10–15 seconds, the status badge will switch from **OFFLINE** to **RUNNING**, and live telemetry will populate.
3. **Configure Notifications:**
   * Expand the **Diagnostics & Settings** card at the bottom of the interface.
   * Under **Push Notifications**, enter an `ntfy` topic name (e.g., `my-secret-bbq-topic`) or the full URL of a self-hosted instance.
   * Click **Save**, then **Test** to verify that alerts reach your phone.
4. **Automated Recipes:**
   * Expand **Recipe Automation & Template Editor** to select a preset, edit existing stages, or build custom multi-stage programs.
   * Stages support target setpoints with progression triggers based on elapsed time, probe values (`>=` or `<=`), or indefinite holds.

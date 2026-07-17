# Centralised Monitoring Dashboard for Standalone Fortinet 124F Sites

Deep-dive research: how to pull health/traffic data from **Fortinet 124F** switches deployed
as *standalone* devices at multiple sites, into one central custom dashboard, using
**SNMP** and/or the **REST API** — plus a ready-to-import Postman collection
(`postman/FortiSwitch-124F-Monitoring.postman_collection.json`).

---

## 1. First, identify the platform correctly

There is **no FortiGate 124F**. The "124F" model in Fortinet's lineup is the
**FortiSwitch FS-124F** family (FS-124F, FS-124F-POE, FS-124F-FPOE) — a 24×GE + 4×SFP+
L2/L3-lite access switch running **FortiSwitchOS** (6.4 / 7.0 / 7.2 / 7.4 trains).

That matters because the API surface differs by OS:

| If your device is… | OS | API base | Notes |
|---|---|---|---|
| FortiSwitch FS-124F (standalone) | FortiSwitchOS | `https://<switch>/api/v2/...` | Covered in detail below |
| FortiSwitch FS-124F (FortiLink-managed) | via FortiGate | FortiGate `/api/v2/monitor/switch-controller/...` | The FortiGate proxies switch telemetry |
| A FortiGate (40F/60F/100F…) at the site | FortiOS | `https://<fgt>/api/v2/...` | See Appendix A |

"Standalone" = the switch is **not** managed by a FortiGate over FortiLink, so there is no
built-in central pane of glass — which is exactly why a custom dashboard is needed.
(Fortinet's own options for this are FortiLAN Cloud and FortiSwitch Manager/NMS; see §7.)

Confirm what you have: `get system status` on the CLI shows the model and OS version.

---

## 2. Reference architecture

```
 Site 1..N (standalone FS-124F)                Central site / cloud VM
┌───────────────────────────┐
│ FS-124F                   │   IPsec VPN /   ┌─────────────────────────────┐
│  • SNMP v2c/v3 agent      │   MPLS / SD-WAN │  Collector layer            │
│  • HTTPS REST API         │ ───────────────▶│   • SNMP poller (Telegraf / │
│  • read-only API admin    │   (mgmt VLAN)   │     Prometheus snmp_exporter)│
│  • trusted-host restricted│                 │   • REST poller (Python,    │
└───────────────────────────┘                 │     tools/poller/)          │
                                              │  Storage: InfluxDB /        │
                                              │     Prometheus / TimescaleDB│
                                              │  Dashboard: Grafana or      │
                                              │     custom React/Flask UI   │
                                              │  Alerting: Alertmanager /   │
                                              │     Grafana alerts          │
                                              └─────────────────────────────┘
```

Design rules:

- **Pull, not push, for metrics.** The central collector polls each site over the
  management VPN. SNMP traps / syslog from the switches can supplement as push events.
- **One read-only credential per site** (unique SNMPv3 user / API token), IP-locked to the
  collector's source address.
- **Poll intervals**: 30–60 s for port counters and CPU/memory; 5 min for inventory
  (MAC table, LLDP, optics DOM); on-demand for config (CMDB) reads.
- **Never expose** the switch mgmt interface to the internet. If a site has no VPN,
  put a tiny collector (Raspberry Pi / container) on-site that pushes to the centre.

### SNMP vs REST API — which to use?

| Criterion | SNMP | REST API |
|---|---|---|
| Port traffic counters (64-bit) | ✅ IF-MIB `ifXTable` — cheapest way | ✅ `/monitor/switch/port` |
| CPU / RAM / disk | ✅ Fortinet MIB | ✅ `/monitor/hardware/cpu`, `/monitor/hardware/memory` |
| PoE per-port power | ⚠️ POWER-ETHERNET-MIB (limited detail) | ✅ `/monitor/switch/poe-status` (rich) |
| MAC table / LLDP neighbours | ✅ Q-BRIDGE-MIB / LLDP-MIB | ✅ `/monitor/switch/mac-address`, `/monitor/switch/lldp-state` |
| Optics (SFP DOM) | ❌ | ✅ `/monitor/switch/modules-detail` |
| Config readout / drift detection | ❌ | ✅ `/api/v2/cmdb/...` |
| Tooling maturity | ✅ Telegraf/Prometheus off-the-shelf | Custom poller (simple JSON over HTTPS) |
| Overhead on device | Very low | Low (HTTPS per poll) |

**Recommendation:** SNMPv3 for high-frequency counters (interfaces, CPU/mem) because
every TSDB stack ingests it natively, **plus** the REST API for the rich stuff SNMP can't
give you (PoE detail, optics DOM, MAC/LLDP as JSON, config audit). The Postman
collection covers the full REST surface so you can cherry-pick.

---

## 3. Preparing each FS-124F (per site)

Full CLI baseline in [`configs/fortiswitch/FS-124F-monitoring-baseline.conf`](../configs/fortiswitch/FS-124F-monitoring-baseline.conf).

1. **Mgmt reachability**: mgmt interface/VLAN reachable from the collector over the VPN.
2. **Enable SNMP** (`config system snmp sysinfo` + v3 user or v2c community, with the
   collector IP as the only allowed host).
3. **Create a read-only admin profile + API credential**:
   - Newer FortiSwitchOS (7.x) supports a **REST API admin with a static token**
     (GUI: System → Administrators → Create → REST API Admin; token is shown once).
     Requests then send `Authorization: Bearer <token>`.
   - On builds without API-token admins, use a **read-only local admin** and the
     **session login** flow (`POST /logincheck` → `APSCOOKIE` session cookie +
     `ccsrftoken` cookie; GETs need only the cookie, writes also need the
     `X-CSRFTOKEN` header; `POST /logout` when done). The Postman collection
     implements both flows.
4. **Lock it down**: trusted hosts (where supported), unique credentials per site,
   TLS on (the box uses a self-signed cert by default — pin it or install a real one;
   in Postman turn off cert verification only for lab use).

---

## 4. REST API — endpoint map (FortiSwitchOS, standalone)

Base URL: `https://<switch-ip>/api/v2`. Two roots:

- `/api/v2/monitor/...` — live operational state (read-only GETs; what a dashboard polls)
- `/api/v2/cmdb/...` — configuration objects (GET to read, POST/PUT/DELETE to change)

All responses are JSON: `{ "http_method":"GET", "results":[...], "status":"success", ... }`.
Useful query params on most endpoints: `?port-name=<port>` (port-scoped monitors),
CMDB supports `?format=key1|key2` field selection.

### Monitor endpoints (the dashboard feed)

| Purpose | Method + Path |
|---|---|
| Device status (model, serial, OS version, uptime) | `GET /api/v2/monitor/system/status` |
| Performance summary (CPU/mem/session snapshot) | `GET /api/v2/monitor/system/performance-status` |
| CPU detail | `GET /api/v2/monitor/hardware/cpu` |
| Memory detail | `GET /api/v2/monitor/hardware/memory` |
| Fans | `GET /api/v2/monitor/system/fan-status` |
| PSUs | `GET /api/v2/monitor/system/psu-status` |
| Board temperature sensors | `GET /api/v2/monitor/system/pcb-temp` |
| NTP sync state | `GET /api/v2/monitor/system/ntp-status` |
| Firmware upgrade state | `GET /api/v2/monitor/system/upgrade-status` |
| **All ports: link, speed, duplex, counters** | `GET /api/v2/monitor/switch/port` |
| **PoE per-port power draw/status** (POE/FPOE models) | `GET /api/v2/monitor/switch/poe-status` |
| Trunk/LAG state | `GET /api/v2/monitor/switch/trunk-state` |
| STP per-port state | `GET /api/v2/monitor/switch/stp-state` |
| Loop-guard state | `GET /api/v2/monitor/switch/loop-guard-state` |
| SFP/SFP+ module inventory | `GET /api/v2/monitor/switch/modules-summary` |
| SFP DOM (rx/tx power, temp, bias) | `GET /api/v2/monitor/switch/modules-detail` |
| MAC address table | `GET /api/v2/monitor/switch/mac-address` |
| LLDP neighbours | `GET /api/v2/monitor/switch/lldp-state` |
| IGMP snooping groups | `GET /api/v2/monitor/switch/igmp-snooping-group` |
| DHCP snooping bindings | `GET /api/v2/monitor/switch/dhcp-snooping-db` |
| L3 routing table (SVIs) | `GET /api/v2/monitor/router/routing-table` |
| Physical interface IPs | `GET /api/v2/monitor/system/interface-physical` |

> Endpoint names map 1:1 to the selectors in Fortinet's own `fortinet.fortiswitch`
> Ansible collection (`fortiswitch_monitor_fact`), e.g. selector `switch_port`
> → `/api/v2/monitor/switch/port`. Exact availability varies slightly by
> FortiSwitchOS train — hit the endpoint once in Postman to confirm on your build.

### CMDB endpoints (config audit / inventory)

| Purpose | Path |
|---|---|
| Global settings (hostname etc.) | `GET /api/v2/cmdb/system/global` |
| Physical port config | `GET /api/v2/cmdb/switch/physical-port` |
| Switch interfaces (VLAN membership, PoE admin) | `GET /api/v2/cmdb/switch/interface` |
| VLAN database | `GET /api/v2/cmdb/switch/vlan` |
| SNMP settings | `GET /api/v2/cmdb/system.snmp/sysinfo` |
| Admin accounts (audit) | `GET /api/v2/cmdb/system/admin` |

### Authentication cheat-sheet

**Token (preferred, FortiSwitchOS 7.x REST API admin):**
```bash
curl -k -H "Authorization: Bearer $TOKEN" \
  https://10.10.1.2/api/v2/monitor/system/status
```

**Session (works everywhere):**
```bash
# login — capture cookies
curl -k -c /tmp/fsw.jar -d 'username=api-ro&secretkey=PASSWORD&ajax=1' \
  https://10.10.1.2/logincheck
# read (GET needs only the cookie)
curl -k -b /tmp/fsw.jar https://10.10.1.2/api/v2/monitor/switch/port
# logout
curl -k -b /tmp/fsw.jar https://10.10.1.2/logout
```
Writes additionally need `-H "X-CSRFTOKEN: <value of ccsrftoken cookie>"`.

---

## 5. SNMP — what to poll

Enable v3 (or v2c on an isolated mgmt VLAN). Fortinet enterprise OID root: `1.3.6.1.4.1.12356`;
the FortiSwitch MIB (`FORTINET-FORTISWITCH-MIB`, download from the Fortinet Support
portal for your exact firmware) sits under **`1.3.6.1.4.1.12356.106`** (`fnFortiSwitchMib`).

| Metric | MIB / OID |
|---|---|
| Sysname/uptime/contact | SNMPv2-MIB `1.3.6.1.2.1.1` (`sysUpTime` = `.1.3.0`) |
| **Port traffic (64-bit)** | IF-MIB `ifXTable` `1.3.6.1.2.1.31.1.1.1` — `ifHCInOctets` `.6`, `ifHCOutOctets` `.10`, `ifHighSpeed` `.15` |
| Port errors/discards, oper status | IF-MIB `ifTable` `1.3.6.1.2.1.2.2.1` (`ifOperStatus` `.8`, `ifInErrors` `.14`, `ifOutErrors` `.20`) |
| CPU % | `fsSysCpuUsage` — fsSystemInfo group `1.3.6.1.4.1.12356.106.4.1` (Gauge 0–100) |
| Memory used (KB) + capacity | `fsSysMemUsage` / `fsSysMemCapacity` (same group; note: **KB, not %** — compute % centrally) |
| Disk usage/capacity | `fsSysDiskUsage` / `fsSysDiskCapacity` (same group) |
| MAC forwarding table | Q-BRIDGE-MIB `dot1qTpFdbTable` `1.3.6.1.2.1.17.7.1.2.2` |
| LLDP neighbours | LLDP-MIB `1.0.8802.1.1.2` |
| PoE (basic) | POWER-ETHERNET-MIB `1.3.6.1.2.1.105` |

Verify leaf OIDs on your firmware with `snmpwalk -v3 ... 1.3.6.1.4.1.12356.106` once —
Fortinet occasionally re-orders leaves between MIB revisions.

Also configure **traps** (link up/down, CPU/mem thresholds, fan/PSU failure) toward the
collector so critical events arrive faster than the polling interval.

**Off-the-shelf ingestion:** Telegraf `[[inputs.snmp]]` → InfluxDB → Grafana, or
`prometheus/snmp_exporter` (generator config referencing FORTINET-FORTISWITCH-MIB +
IF-MIB) → Prometheus → Grafana. Both give you multi-site dashboards with almost no code.

---

## 6. The central dashboard itself

Two sane build paths:

1. **Grafana-first (recommended, ~zero code):**
   Telegraf/snmp_exporter per the above for counters; run the REST poller
   ([`tools/poller/poll_sites.py`](../tools/poller/poll_sites.py)) on cron to enrich
   with PoE/optics/MAC data into the same TSDB. Grafana variables (`site`, `switch`,
   `port`) give you the fleet view: uptime heatmap, per-site CPU/mem, top talkers,
   PoE budget, port error rates, LLDP topology table. Alerting via Grafana/Alertmanager.

2. **Fully custom web app:** a small FastAPI/Flask backend runs the same polling logic,
   stores into TimescaleDB/Influx, and a React front-end renders the fleet grid.
   Choose this only if you need workflows Grafana can't do (e.g. click-to-configure —
   the CMDB POST/PUT endpoints support that, but keep the dashboard credential read-only
   and use a separate change credential).

The example poller in this repo demonstrates the multi-site fan-out: reads
`tools/poller/sites.example.json`, polls every site's `system/status`, `hardware/cpu`,
`hardware/memory`, `switch/port` (+ `switch/poe-status`), and emits newline-delimited
JSON ready for Telegraf `inputs.file`/`inputs.execd` or direct DB insert.

---

## 7. Fortinet-native alternatives (for the record)

| Option | What it gives | Trade-off |
|---|---|---|
| **FortiLAN Cloud** | Free-tier cloud management/monitoring of standalone FortiSwitches | Fortinet's UI, not a *custom* dashboard; needs cloud reachability |
| **FortiSwitch Manager / FortiSwitchNMS** | On-prem central switch management, has its own REST API | Another product to run/license |
| **FortiLink to a FortiGate** | Switch telemetry via FortiGate's `/monitor/switch-controller/managed-switch` | Requires a FortiGate per site; no longer "standalone" |

These can coexist with the custom dashboard, but the SNMP+REST design above keeps you
vendor-UI-independent, which is what was asked.

---

## Appendix A — If a site device is actually a FortiGate (FortiOS)

Same auth patterns (Bearer token from a REST API admin profile is standard on FortiOS).
Dashboard-relevant FortiOS endpoints (also included as a folder in the Postman collection):

| Purpose | Path |
|---|---|
| Status / uptime / version | `GET /api/v2/monitor/system/status` |
| CPU/mem/sessions time-series | `GET /api/v2/monitor/system/resource/usage?interval=1-min` |
| Interfaces + counters | `GET /api/v2/monitor/system/interface?include_vlan=true` |
| License / FortiGuard state | `GET /api/v2/monitor/license/status` |
| IPsec VPN tunnels | `GET /api/v2/monitor/vpn/ipsec` |
| SSL-VPN sessions | `GET /api/v2/monitor/vpn/ssl` |
| SD-WAN health checks | `GET /api/v2/monitor/virtual-wan/health-check` |
| Firewall policy hit counts | `GET /api/v2/monitor/firewall/policy` |
| Managed FortiSwitches (FortiLink) | `GET /api/v2/monitor/switch-controller/managed-switch?poe=true&port_stats=true` |
| HA status | `GET /api/v2/monitor/system/ha-statistics` |

FortiGate SNMP: `FORTINET-FORTIGATE-MIB` under `1.3.6.1.4.1.12356.101`
(`fgSysCpuUsage` `.101.4.1.3.0`, `fgSysMemUsage` `.101.4.1.4.0` — here memory **is** a %,
`fgSysSesCount` `.101.4.1.8.0`), plus the same IF-MIB tables.

---

## Sources

- [Using APIs — FortiOS 7.4 Administration Guide](https://docs.fortinet.com/document/fortigate/7.4.0/administration-guide/940602/using-apis)
- [REST API administrator — FortiOS 7.4 Administration Guide](https://docs.fortinet.com/document/fortigate/7.4.3/administration-guide/399023/rest-api-administrator)
- [REST API for Monitoring — Fortinet reference architecture](https://docs.fortinet.com/document/fortigate/7.4.6/fortinet-carrier-grade-nat-field-reference-architecture-guide/725722/rest-api-for-monitoring)
- [FortiSwitch REST API — FortiSwitchOS document library](https://docs.fortinet.com/document/fortiswitch/6.4.3/fortiswitch-rest-api)
- [Technical Tip: About REST API — Fortinet Community (logincheck/APSCOOKIE/ccsrftoken flow)](https://community.fortinet.com/t5/FortiGate/Technical-Tip-About-REST-API/ta-p/195425)
- [fortiswitch_monitor_fact — fortinet.fortiswitch Ansible collection (monitor selectors)](https://ansible-galaxy-fortiswitch-docs.readthedocs.io/en/latest/fortiswitch_monitor_fact.html)
- [FORTINET-FORTISWITCH-MIB (1.3.6.1.4.1.12356.106) — Observium MIB browser](https://mibs.observium.org/mib/FORTINET-FORTISWITCH-MIB/)
- [FORTINET-FORTIGATE-MIB fgSysCpuUsage (1.3.6.1.4.1.12356.101.4.1.3)](http://oidref.com/1.3.6.1.4.1.12356.101.4.1.3)
- [Generate an API token for FortiOS — fortinetdev Terraform guide](https://registry.terraform.io/providers/fortinetdev/fortios/latest/docs/guides/fgt_token)
- [Monitor FortiSwitches in FortiLink mode using the FortiOS REST API — Auvik](https://support.auvik.com/hc/en-us/articles/360056175532-How-do-I-monitor-a-FortiSwitch-in-FortiLink-mode)

#!/usr/bin/env python3
"""Central REST poller for standalone FortiSwitch FS-124F sites.

Fans out over every site in sites.json, pulls the dashboard-relevant
monitor endpoints, and emits one NDJSON line per (site, endpoint) to
stdout or --out. Feed the output to Telegraf (inputs.file / inputs.execd),
a TimescaleDB/Influx loader, or your dashboard backend.

Auth: uses `api_token` (Bearer) when present for a site, otherwise falls
back to session login (POST /logincheck) with username/password and
always logs out (FortiSwitchOS caps concurrent admin sessions).

Usage:
    python3 poll_sites.py --config sites.json [--out metrics.ndjson] [--insecure]

Cron example (every minute):
    * * * * * cd /opt/poller && ./poll_sites.py --config sites.json --insecure >> /var/lib/poller/metrics.ndjson

Requires: requests  (pip install requests)
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

# Endpoints polled every run; add/remove to taste (see the Postman collection
# for the full surface, e.g. switch/modules-detail for optics DOM).
MONITOR_ENDPOINTS = [
    "system/status",
    "hardware/cpu",
    "hardware/memory",
    "switch/port",
    "switch/poe-status",   # POE/FPOE models; harmless 4xx on non-PoE units
]


class FortiSwitchClient:
    def __init__(self, site, verify=True, timeout=15):
        self.site = site
        self.base = site["base_url"].rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self.session = requests.Session()
        self._session_login = False

    def __enter__(self):
        token = self.site.get("api_token")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            r = self.session.post(
                f"{self.base}/logincheck",
                data={
                    "username": self.site["username"],
                    "secretkey": self.site["password"],
                    "ajax": "1",
                },
                verify=self.verify,
                timeout=self.timeout,
            )
            r.raise_for_status()
            self._session_login = True
        return self

    def __exit__(self, *exc):
        if self._session_login:
            try:
                self.session.post(f"{self.base}/logout", verify=self.verify, timeout=self.timeout)
            except requests.RequestException:
                pass

    def monitor(self, endpoint):
        r = self.session.get(
            f"{self.base}/api/v2/monitor/{endpoint}",
            verify=self.verify,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()


def poll_site(site, verify):
    records = []
    ts = int(time.time())
    try:
        with FortiSwitchClient(site, verify=verify) as client:
            for endpoint in MONITOR_ENDPOINTS:
                record = {"ts": ts, "site": site["name"], "endpoint": endpoint}
                try:
                    record["data"] = client.monitor(endpoint)
                except requests.RequestException as exc:
                    record["error"] = str(exc)
                records.append(record)
    except requests.RequestException as exc:
        records.append({"ts": ts, "site": site["name"], "endpoint": "login", "error": str(exc)})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="sites.json", help="site inventory JSON")
    parser.add_argument("--out", help="append NDJSON here instead of stdout")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed device certs)")
    parser.add_argument("--workers", type=int, default=8, help="parallel sites")
    args = parser.parse_args()

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    with open(args.config) as fh:
        sites = json.load(fh)["sites"]

    out = open(args.out, "a") if args.out else sys.stdout
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(poll_site, site, not args.insecure) for site in sites]
        for future in as_completed(futures):
            for record in future.result():
                if "error" in record:
                    failures += 1
                out.write(json.dumps(record, separators=(",", ":")) + "\n")
    if args.out:
        out.close()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

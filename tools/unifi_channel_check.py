#!/usr/bin/env python3
"""Check that all UniFi APs are on distinct Wi-Fi channels per band.

Wired (non-mesh) APs should each sit on their own channel within a band.
Wirelessly-uplinked (mesh) APs are expected to share a channel with their
uplink AP, so they are reported but excluded from the conflict check.

Works against both UniFi OS consoles (UDM/UDR/Cloud Key Gen2, port 443)
and legacy controllers (port 8443). Uses only the Python standard library
and tolerates the controller's self-signed certificate.

Examples:
    python3 unifi_channel_check.py --host https://192.168.1.1 \
        --username admin --password 'secret'

    python3 unifi_channel_check.py --host https://192.168.1.1 \
        --api-key 'xxxxxxxx'
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import ssl
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

RADIO_BANDS = {
    "ng": "2.4 GHz",
    "na": "5 GHz",
    "6e": "6 GHz",
}

GOOD_24_CHANNELS = {1, 6, 11}


class UniFiClient:
    def __init__(self, host: str, api_key: str | None) -> None:
        self.host = host.rstrip("/")
        self.api_key = api_key
        self.api_prefix: str | None = None  # set after login/probe
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["X-API-KEY"] = self.api_key
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.host + path, data=data, headers=headers)
        with self.opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def login(self, username: str, password: str) -> None:
        if self.api_key:
            # API keys only exist on UniFi OS consoles.
            self.api_prefix = "/proxy/network/api"
            return
        creds = {"username": username, "password": password}
        try:
            self.request("/api/auth/login", creds)
            self.api_prefix = "/proxy/network/api"
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405):
                raise SystemExit(f"UniFi OS login failed: HTTP {exc.code}")
        self.request("/api/login", creds)
        self.api_prefix = "/api"

    def devices(self, site: str) -> list[dict[str, Any]]:
        result = self.request(f"{self.api_prefix}/s/{site}/stat/device")
        return result.get("data", [])


def ap_radios(ap: dict[str, Any]) -> dict[str, Any]:
    """Return {band: {"channel": int|None, "configured": str}} for one AP."""
    configured: dict[str, str] = {}
    for radio in ap.get("radio_table", []):
        band = RADIO_BANDS.get(radio.get("radio"), radio.get("radio", "?"))
        configured[band] = str(radio.get("channel", "auto"))
    radios: dict[str, Any] = {}
    for stat in ap.get("radio_table_stats", []):
        band = RADIO_BANDS.get(stat.get("radio"), stat.get("radio", "?"))
        radios[band] = {
            "channel": stat.get("channel"),
            "configured": configured.get(band, "auto"),
        }
    for band, chan in configured.items():
        radios.setdefault(band, {"channel": None, "configured": chan})
    return radios


def is_mesh(ap: dict[str, Any]) -> bool:
    return ap.get("uplink", {}).get("type") == "wireless"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="Controller URL, e.g. https://192.168.1.1")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--api-key", help="UniFi OS API key (alternative to username/password)")
    parser.add_argument("--site", default="default")
    args = parser.parse_args()

    if not args.api_key:
        if not args.username:
            parser.error("provide --username/--password or --api-key")
        if not args.password:
            args.password = getpass.getpass("UniFi password: ")

    client = UniFiClient(args.host, args.api_key)
    client.login(args.username, args.password)
    aps = [d for d in client.devices(args.site) if d.get("type") == "uap"]
    if not aps:
        print("No access points found on this site.")
        return 1

    channel_users: dict[tuple[str, int], list[str]] = defaultdict(list)
    print(f"{'AP':<24} {'Model':<10} {'Uplink':<8} Channels")
    for ap in sorted(aps, key=lambda a: a.get("name") or a.get("mac", "")):
        name = ap.get("name") or ap.get("mac", "unknown")
        uplink = "mesh" if is_mesh(ap) else "wired"
        parts = []
        for band, info in sorted(ap_radios(ap).items()):
            chan = info["channel"]
            parts.append(f"{band}: ch {chan if chan is not None else '?'} (set: {info['configured']})")
            if chan is not None and not is_mesh(ap):
                channel_users[(band, chan)].append(name)
        offline = "" if ap.get("state") == 1 else "  [OFFLINE]"
        print(f"{name:<24} {ap.get('model', '?'):<10} {uplink:<8} {' | '.join(parts)}{offline}")

    ok = True
    print()
    for (band, chan), names in sorted(channel_users.items()):
        if len(names) > 1:
            ok = False
            print(f"CONFLICT: {band} channel {chan} shared by non-mesh APs: {', '.join(names)}")
    for (band, chan), names in sorted(channel_users.items()):
        if band == "2.4 GHz" and chan not in GOOD_24_CHANNELS:
            print(f"NOTE: {', '.join(names)} on 2.4 GHz channel {chan}; "
                  f"channels 1/6/11 avoid partial overlap.")
    if ok:
        print("OK: every non-mesh AP is on its own channel in each band.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

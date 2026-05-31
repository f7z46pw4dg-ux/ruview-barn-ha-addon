#!/usr/bin/env python3
"""Bridge RuView ESP32 vitals packets into Home Assistant over MQTT."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib import parse, request


VITALS_MAGIC = 0xC5110002


@dataclass
class Vitals:
    source: str
    node: int
    flags: int
    breathing: float
    heart: float
    rssi: int
    persons: int
    motion: float
    presence_score: float
    device_timestamp: int
    received_at: float

    @property
    def presence_flag(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def fall_flag(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def motion_flag(self) -> bool:
        return bool(self.flags & 0x04)

    def as_payload(self, present: bool) -> dict[str, Any]:
        return {
            "present": present,
            "node": self.node,
            "source": self.source,
            "flags": self.flags,
            "presence_flag": self.presence_flag,
            "motion_flag": self.motion_flag,
            "fall_flag": self.fall_flag,
            "breathing_bpm": round(self.breathing, 2),
            "heart_bpm": round(self.heart, 2),
            "rssi_dbm": self.rssi,
            "persons_estimate": self.persons,
            "motion": round(self.motion, 4),
            "presence_score": round(self.presence_score, 4),
            "device_timestamp": self.device_timestamp,
            "received_at": int(self.received_at),
        }


def env_default(name: str, fallback: str | None = None) -> str | None:
    return os.environ.get(name) or fallback


def load_addon_options() -> None:
    path = "/data/options.json"
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        options = json.load(handle)
    mapping = {
        "mqtt_host": "RUVIEW_HA_MQTT_HOST",
        "mqtt_port": "RUVIEW_HA_MQTT_PORT",
        "mqtt_username": "RUVIEW_HA_MQTT_USERNAME",
        "mqtt_password": "RUVIEW_HA_MQTT_PASSWORD",
        "udp_port": "RUVIEW_UDP_PORT",
        "location": "RUVIEW_LOCATION",
        "presence_threshold": "RUVIEW_PRESENCE_THRESHOLD",
        "motion_threshold": "RUVIEW_MOTION_THRESHOLD",
        "use_firmware_flags": "RUVIEW_USE_FIRMWARE_FLAGS",
        "on_seconds": "RUVIEW_ON_SECONDS",
        "off_seconds": "RUVIEW_OFF_SECONDS",
        "ha_url": "RUVIEW_HA_URL",
        "ha_username": "RUVIEW_HA_USERNAME",
        "ha_password": "RUVIEW_HA_PASSWORD",
        "switch_entity": "RUVIEW_HA_SWITCH_ENTITY",
        "control_switch": "RUVIEW_HA_CONTROL_SWITCH",
    }
    for option_key, env_key in mapping.items():
        value = options.get(option_key)
        if env_key not in os.environ and value is not None:
            os.environ[env_key] = str(value)


class HomeAssistantClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client_id = self.base_url + "/"
        self.redirect_uri = self.base_url + "/?auth_callback=1"
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        self.login()

    def post_json(self, path: str, payload: dict[str, Any], token: str | None = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None

    def post_form(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        req = request.Request(
            self.base_url + path,
            data=parse.urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def login(self) -> None:
        flow = self.post_json(
            "/auth/login_flow",
            {
                "client_id": self.client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": self.redirect_uri,
            },
        )
        resp = self.post_json(
            f"/auth/login_flow/{flow['flow_id']}",
            {
                "username": self.username,
                "password": self.password,
                "client_id": self.client_id,
            },
        )
        token = self.post_form(
            "/auth/token",
            {
                "grant_type": "authorization_code",
                "code": resp["result"],
                "client_id": self.client_id,
            },
        )
        self.set_token(token)

    def refresh(self) -> None:
        token = self.post_form(
            "/auth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
            },
        )
        self.set_token(token)

    def set_token(self, token: dict[str, Any]) -> None:
        self.access_token = token["access_token"]
        if "refresh_token" in token:
            self.refresh_token = token["refresh_token"]
        self.expires_at = time.time() + float(token.get("expires_in", 1800))

    def ensure_token(self) -> None:
        if time.time() > self.expires_at - 60:
            self.refresh()

    def call_service(self, domain: str, service: str, payload: dict[str, Any]) -> None:
        self.ensure_token()
        self.post_json(f"/api/services/{domain}/{service}", payload, self.access_token)


class SupervisorHomeAssistantClient:
    def __init__(self, token: str, base_url: str = "http://supervisor/core/api") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")

    def call_service(self, domain: str, service: str, payload: dict[str, Any]) -> None:
        req = request.Request(
            f"{self.base_url}/services/{domain}/{service}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=20) as resp:
            resp.read()


def decode_vitals(data: bytes, addr: tuple[str, int]) -> Vitals:
    return Vitals(
        source=f"{addr[0]}:{addr[1]}",
        node=data[4],
        flags=data[5],
        breathing=struct.unpack_from("<H", data, 6)[0] / 100.0,
        heart=struct.unpack_from("<I", data, 8)[0] / 10000.0,
        rssi=struct.unpack_from("<b", data, 12)[0],
        persons=data[13],
        motion=struct.unpack_from("<f", data, 16)[0],
        presence_score=struct.unpack_from("<f", data, 20)[0],
        device_timestamp=struct.unpack_from("<I", data, 24)[0],
        received_at=time.time(),
    )


def mqtt_client(args: argparse.Namespace):
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit(
            "Missing paho-mqtt. Install it with: python3 -m pip install paho-mqtt"
        ) from exc

    client_id = f"ruview-ha-bridge-{args.location}"
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    else:
        client = mqtt.Client(client_id=client_id)

    if args.mqtt_username:
        client.username_pw_set(args.mqtt_username, args.mqtt_password)

    status_topic = f"{args.base_topic}/{args.location}/status"
    client.will_set(status_topic, payload="offline", qos=1, retain=True)
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_start()
    client.publish(status_topic, "online", qos=1, retain=True)
    return client


def discovery_payloads(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    base = f"{args.base_topic}/{args.location}"
    discovery = args.discovery_prefix.rstrip("/")
    dev = {
        "identifiers": [f"ruview_{args.location}"],
        "name": f"RuView {args.location.replace('_', ' ').title()}",
        "manufacturer": "ruvnet",
        "model": "ESP32-S3 RuView CSI node",
        "sw_version": "0.6.6",
    }
    availability = [{"topic": f"{base}/status", "payload_available": "online", "payload_not_available": "offline"}]

    def topic(component: str, object_id: str) -> str:
        return f"{discovery}/{component}/ruview_{args.location}_{object_id}/config"

    common = {
        "device": dev,
        "availability": availability,
        "json_attributes_topic": f"{base}/vitals",
    }

    configs: list[tuple[str, dict[str, Any]]] = [
        (
            topic("binary_sensor", "presence"),
            {
                **common,
                "name": "Presence",
                "unique_id": f"ruview_{args.location}_presence",
                "default_entity_id": f"binary_sensor.ruview_{args.location}_presence",
                "device_class": "occupancy",
                "state_topic": f"{base}/presence",
                "payload_on": "ON",
                "payload_off": "OFF",
            },
        ),
        (
            topic("sensor", "heart_bpm"),
            {
                **common,
                "name": "Heart BPM",
                "unique_id": f"ruview_{args.location}_heart_bpm",
                "default_entity_id": f"sensor.ruview_{args.location}_heart_bpm",
                "state_topic": f"{base}/heart_bpm",
                "unit_of_measurement": "bpm",
                "state_class": "measurement",
            },
        ),
        (
            topic("sensor", "breathing_bpm"),
            {
                **common,
                "name": "Breathing BPM",
                "unique_id": f"ruview_{args.location}_breathing_bpm",
                "default_entity_id": f"sensor.ruview_{args.location}_breathing_bpm",
                "state_topic": f"{base}/breathing_bpm",
                "unit_of_measurement": "bpm",
                "state_class": "measurement",
            },
        ),
        (
            topic("sensor", "rssi"),
            {
                **common,
                "name": "RSSI",
                "unique_id": f"ruview_{args.location}_rssi",
                "default_entity_id": f"sensor.ruview_{args.location}_rssi",
                "state_topic": f"{base}/rssi",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
            },
        ),
        (
            topic("sensor", "motion"),
            {
                **common,
                "name": "Motion",
                "unique_id": f"ruview_{args.location}_motion",
                "default_entity_id": f"sensor.ruview_{args.location}_motion",
                "state_topic": f"{base}/motion",
                "state_class": "measurement",
            },
        ),
        (
            topic("sensor", "presence_score"),
            {
                **common,
                "name": "Presence Score",
                "unique_id": f"ruview_{args.location}_presence_score",
                "default_entity_id": f"sensor.ruview_{args.location}_presence_score",
                "state_topic": f"{base}/presence_score",
                "state_class": "measurement",
            },
        ),
    ]
    return configs


def publish_discovery(client: Any, args: argparse.Namespace) -> None:
    for topic, payload in discovery_payloads(args):
        client.publish(topic, json.dumps(payload), qos=1, retain=True)


def publish_vitals(client: Any, args: argparse.Namespace, vitals: Vitals, present: bool) -> None:
    base = f"{args.base_topic}/{args.location}"
    payload = vitals.as_payload(present)
    client.publish(f"{base}/presence", "ON" if present else "OFF", qos=1, retain=True)
    client.publish(f"{base}/vitals", json.dumps(payload), qos=0, retain=False)
    client.publish(f"{base}/heart_bpm", f"{vitals.heart:.2f}", qos=0, retain=True)
    client.publish(f"{base}/breathing_bpm", f"{vitals.breathing:.2f}", qos=0, retain=True)
    client.publish(f"{base}/rssi", str(vitals.rssi), qos=0, retain=True)
    client.publish(f"{base}/motion", f"{vitals.motion:.4f}", qos=0, retain=True)
    client.publish(f"{base}/presence_score", f"{vitals.presence_score:.4f}", qos=0, retain=True)


def candidate_presence(vitals: Vitals, args: argparse.Namespace) -> bool:
    if args.use_firmware_flags and vitals.presence_flag:
        return True
    if vitals.presence_score >= args.presence_threshold:
        return True
    if args.use_firmware_flags and not vitals.motion_flag:
        return False
    return vitals.motion >= args.motion_threshold


def parse_args() -> argparse.Namespace:
    load_addon_options()
    parser = argparse.ArgumentParser(description="RuView UDP to Home Assistant MQTT bridge")
    parser.add_argument("--udp-port", type=int, default=int(env_default("RUVIEW_UDP_PORT", "5005") or "5005"))
    parser.add_argument("--mqtt-host", default=env_default("RUVIEW_HA_MQTT_HOST"))
    parser.add_argument("--mqtt-port", type=int, default=int(env_default("RUVIEW_HA_MQTT_PORT", "1883") or "1883"))
    parser.add_argument("--mqtt-username", default=env_default("RUVIEW_HA_MQTT_USERNAME"))
    parser.add_argument("--mqtt-password", default=env_default("RUVIEW_HA_MQTT_PASSWORD"))
    parser.add_argument("--discovery-prefix", default=env_default("RUVIEW_HA_DISCOVERY_PREFIX", "homeassistant"))
    parser.add_argument("--base-topic", default=env_default("RUVIEW_HA_BASE_TOPIC", "ruview"))
    parser.add_argument("--location", default=env_default("RUVIEW_LOCATION", "barn"))
    parser.add_argument("--presence-threshold", type=float, default=float(env_default("RUVIEW_PRESENCE_THRESHOLD", "2.0") or "2.0"))
    parser.add_argument("--motion-threshold", type=float, default=float(env_default("RUVIEW_MOTION_THRESHOLD", "1.0") or "1.0"))
    parser.add_argument(
        "--use-firmware-flags",
        action="store_true",
        default=(env_default("RUVIEW_USE_FIRMWARE_FLAGS", "0") or "0").lower() in {"1", "true", "yes", "on"},
        help="Let raw RuView firmware presence/motion flags trigger occupancy before threshold checks",
    )
    parser.add_argument("--on-seconds", type=float, default=float(env_default("RUVIEW_ON_SECONDS", "4") or "4"))
    parser.add_argument("--off-seconds", type=float, default=float(env_default("RUVIEW_OFF_SECONDS", "240") or "240"))
    parser.add_argument("--ha-url", default=env_default("RUVIEW_HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--ha-username", default=env_default("RUVIEW_HA_USERNAME") or env_default("RUVIEW_HA_MQTT_USERNAME"))
    parser.add_argument("--ha-password", default=env_default("RUVIEW_HA_PASSWORD") or env_default("RUVIEW_HA_MQTT_PASSWORD"))
    parser.add_argument("--switch-entity", default=env_default("RUVIEW_HA_SWITCH_ENTITY", "switch.music"))
    parser.add_argument(
        "--control-switch",
        action="store_true",
        default=(env_default("RUVIEW_HA_CONTROL_SWITCH", "0") or "0").lower() in {"1", "true", "yes", "on"},
        help="Call Home Assistant switch.turn_on/off when RuView presence changes",
    )
    parser.add_argument("--seconds", type=float, default=0, help="Exit after this many seconds; 0 runs forever")
    parser.add_argument("--dry-run", action="store_true", help="Print decoded state without connecting to MQTT")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dry_run and not args.mqtt_host:
        raise SystemExit("Set RUVIEW_HA_MQTT_HOST or pass --mqtt-host.")

    stop = False

    def stop_now(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, stop_now)
    signal.signal(signal.SIGTERM, stop_now)

    client = None if args.dry_run else mqtt_client(args)
    if client:
        publish_discovery(client, args)

    ha = None
    if args.control_switch and not args.dry_run:
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            ha = SupervisorHomeAssistantClient(supervisor_token)
        else:
            if not args.ha_username or not args.ha_password:
                raise SystemExit("Set RUVIEW_HA_USERNAME/RUVIEW_HA_PASSWORD or reuse the MQTT credentials.")
            ha = HomeAssistantClient(args.ha_url, args.ha_username, args.ha_password)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("0.0.0.0", args.udp_port))
    sock.settimeout(1.0)

    status_topic = f"{args.base_topic}/{args.location}/status"
    present = False
    previous_present = False
    first_seen_at: float | None = None
    last_seen_at: float | None = None
    last_publish = 0.0
    last_status_publish = 0.0
    end_at = time.time() + args.seconds if args.seconds > 0 else None
    print(f"Listening for RuView vitals on UDP {args.udp_port}", flush=True)

    while not stop and (end_at is None or time.time() < end_at):
        now = time.time()
        if client and now - last_status_publish >= 30:
            client.publish(status_topic, "online", qos=1, retain=True)
            last_status_publish = now

        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            if present and last_seen_at and time.time() - last_seen_at >= args.off_seconds:
                present = False
                first_seen_at = None
                last_publish = 0.0
                if client:
                    client.publish(
                        f"{args.base_topic}/{args.location}/presence",
                        "OFF",
                        qos=1,
                        retain=True,
                    )
                if ha and previous_present:
                    try:
                        ha.call_service("switch", "turn_off", {"entity_id": args.switch_entity})
                        print(f"{args.switch_entity}: turn_off (presence timeout)", flush=True)
                    except Exception as exc:
                        print(f"Home Assistant switch action failed: {exc}", file=sys.stderr, flush=True)
                    previous_present = False
            continue

        if len(data) < 28:
            continue
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic != VITALS_MAGIC:
            continue

        vitals = decode_vitals(data, addr)
        now = time.time()
        seen = candidate_presence(vitals, args)

        if seen:
            first_seen_at = first_seen_at or now
            last_seen_at = now
            if not present and now - first_seen_at >= args.on_seconds:
                present = True
        else:
            first_seen_at = None
            if present and last_seen_at and now - last_seen_at >= args.off_seconds:
                present = False

        if client:
            publish_vitals(client, args, vitals, present)
        if ha and present != previous_present:
            try:
                ha.call_service(
                    "switch",
                    "turn_on" if present else "turn_off",
                    {"entity_id": args.switch_entity},
                )
                print(
                    f"{args.switch_entity}: {'turn_on' if present else 'turn_off'} "
                    f"(presence={'on' if present else 'off'})",
                    flush=True,
                )
            except Exception as exc:
                print(f"Home Assistant switch action failed: {exc}", file=sys.stderr, flush=True)
            previous_present = present
        if not client and now - last_publish >= 1:
            print(json.dumps(vitals.as_payload(present)), flush=True)
            last_publish = now

    if client:
        client.publish(status_topic, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

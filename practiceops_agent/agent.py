"""PracticeOps office agent: a Home Assistant App on the office Raspberry Pi.

PracticeOps runs in Azure and can't reach the UniFi console on the office network, so this App does
the office side. It works in office hours only (Mon-Fri 7:45-17:15, Sat 7:45-12:15, New York time)
so the PracticeOps server can sleep at night; outside those hours it just waits.

  * UniFi Access: waits on PracticeOps for user and card requests (create the UniFi user, enroll a
    card at a reader, remove a card, deactivate, the daily check of removed staff) and does them with
    the Access API. A request starts within a couple of seconds of the click in PracticeOps.
  * Once a day, the UniFi -> PracticeOps user sync: the id and name of every active Access user, so
    badges made in UniFi are linked to employees by exact name (for the time clock).
  * UniFi Network: every minute the WAN rates for the live strip; every 5 minutes the reads the
    IT / Network dashboard needs (PracticeOps sends the list).
  * Every 5 minutes, every UniFi user's card IDs (the printed number, never the card's token), so the
    PracticeOps Employees list shows who really has a badge.
  * Vaccine fridge and freezer temperatures: every 5 minutes, 5-minute averages of each Home Assistant
    temperature sensor with "fridge" or "freezer" in its name, read from Home Assistant's history. The
    first run of the day also sends everything since the last one (overnight, the weekend). The alarms
    themselves run in Home Assistant, around the clock; this only feeds the PracticeOps display.
  * Home Assistant, read-only: every 5 minutes its version and the state of its devices and sensors
    (no people or places: person, device_tracker and zone are never sent).

Door taps never come through here; the UniFi readers and console decide those on their own.

Before anything leaves the office, every "x_" field (UniFi's passphrases and keys) is dropped and every
MAC address is replaced with a stable token. Card tokens never leave the office; only a card's printed
ID does. Keys come from the App's Configuration tab (/data/options.json) and are never logged.

Standard library only, so there is nothing to install or update but Python itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

VERSION = "2.3.0"
DATA = os.environ.get("AGENT_DATA", "/data")
OPTIONS_FILE = os.path.join(DATA, "options.json")
STATE_FILE = os.path.join(DATA, "state.json")
API_BASE = "https://api-ops.aksharpediatrics.care/api/v1"
HA_API = os.environ.get("HA_API", "http://supervisor/core/api")

OFFICE = ZoneInfo("America/New_York")
# (open, close) in minutes after midnight by weekday, Monday = 0; the same hours as PracticeOps.
OFFICE_HOURS = {0: (465, 1035), 1: (465, 1035), 2: (465, 1035), 3: (465, 1035), 4: (465, 1035), 5: (465, 735)}

POLL_WAIT_SECONDS = 25        # PracticeOps holds the poll open this long waiting for a request
LIST_EVERY_SECONDS = 300      # how often to send PracticeOps the readers and door policies
LIVE_EVERY_SECONDS = 60       # live throughput point
FULL_EVERY_SECONDS = 290      # full dashboard
ENROLL_WAIT_SECONDS = 90      # how long to wait for the card tap when enrolling
SYNC_RETRY_SECONDS = 600      # a failed daily user sync is tried again after this
TEMPS_EVERY_SECONDS = 300     # temperatures go to PracticeOps this often
TEMP_BUCKET = timedelta(minutes=5)
TEMPS_FIRST_RUN = timedelta(hours=6)  # on the very first run, send this much history
TEMPS_MAX_GAP = timedelta(days=7)     # never more than this (Home Assistant keeps 10 days)
TEMP_WORDS = ("fridge", "freezer", "refrigerator")
# What the Home Assistant page in PracticeOps shows; anything else (people, places, cameras) stays here.
SNAPSHOT_DOMAINS = ("sensor", "binary_sensor", "switch", "light", "climate", "cover", "lock", "fan", "update",
                    "automation", "input_boolean", "alarm_control_panel", "siren", "humidifier", "water_heater")
SNAPSHOT_ATTRIBUTES = ("installed_version", "latest_version", "title", "release_url", "last_triggered", "current",
                       "current_temperature", "temperature", "hvac_action", "hvac_mode", "brightness",
                       "battery_level")
MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

LOCAL_TLS = ssl.create_default_context()  # the console's own certificate is self-signed
LOCAL_TLS.check_hostname = False
LOCAL_TLS.verify_mode = ssl.CERT_NONE


def log(message: str) -> None:
    print(f"{datetime.now(OFFICE):%Y-%m-%d %H:%M:%S}  {message}", flush=True)


def in_office_hours(now: datetime | None = None) -> bool:
    local = (now or datetime.now(OFFICE)).astimezone(OFFICE)
    hours = OFFICE_HOURS.get(local.weekday())
    minute = local.hour * 60 + local.minute
    return bool(hours) and hours[0] <= minute < hours[1]


class Refused(Exception):
    """An answer that isn't worth retrying straight away (bad token, UniFi said no)."""


# ------------------------------------------------------------------ sanitizing

class Sanitizer:
    def __init__(self, salt: str):
        self.salt = salt

    def mac_token(self, mac: str) -> str:
        digest = hashlib.sha256((self.salt + mac.lower().replace("-", ":")).encode()).hexdigest()
        return "mac-" + digest[:12]

    def clean(self, value):
        """Drop "x_" fields (passphrases, keys) and replace MAC addresses, at any depth."""
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items() if not str(k).startswith("x_")}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str) and MAC.match(value):
            return self.mac_token(value)
        return value


# ------------------------------------------------------------------ HTTP

def _http(method: str, url: str, headers: dict, body=None, timeout: float = 30, tls=None):
    """(status, parsed JSON or None). HTTP errors still return their body, since UniFi explains in it."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, None


class PracticeOps:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, path: str, body=None, timeout: float = 120):
        status, out = _http("POST", self.base + path, {"X-Ingest-Token": self.token}, body or {}, timeout)
        if status == 401:
            raise Refused("PracticeOps didn't accept the token. Create a new one in PracticeOps "
                          "(Admin > Network) and paste it in this App's Configuration tab.")
        if status >= 400 or not isinstance(out, dict):
            raise RuntimeError(f"PracticeOps answered {status}")
        return out.get("data")


class Access:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/") + "/api/v1/developer", key

    def call(self, method: str, path: str, body=None, allow_failure: bool = False):
        status, out = _http(method, self.base + path, {"Authorization": f"Bearer {self.key}"}, body,
                            tls=LOCAL_TLS)
        if isinstance(out, dict) and out.get("code") == "SUCCESS":
            return out.get("data")
        if allow_failure:
            return None
        if status == 401:
            raise Refused("UniFi Access didn't accept the API key")
        msg = (out or {}).get("msg") if isinstance(out, dict) else None
        code = (out or {}).get("code") if isinstance(out, dict) else None
        raise Refused(f"UniFi Access said: {msg or 'no answer'} ({code or status})")

    def readers(self) -> list[dict]:
        out = []
        for group in self.call("GET", "/devices") or []:
            for d in group if isinstance(group, list) else [group]:
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                caps = d.get("capabilities") or []
                if caps and "nfc" not in caps:
                    continue
                out.append({"id": d["id"], "name": d.get("alias") or d.get("name") or d.get("type"),
                            "type": d.get("type")})
        return out

    def policies(self) -> list[dict]:
        return [{"id": p.get("id"), "name": p.get("name")} for p in self.call("GET", "/access_policies") or []
                if isinstance(p, dict)]

    def cards(self, user_id: str) -> list[str]:
        user = self.call("GET", f"/users/{user_id}") or {}
        return [str(c["id"]) for c in user.get("nfc_cards") or [] if c.get("id")]

    def users(self):
        """Every Access user, page by page."""
        for page in range(1, 51):
            data = self.call("GET", f"/users?page_num={page}&page_size=100") or []
            items = (data.get("items") if isinstance(data, dict) else data) or []
            yield from (u for u in items if isinstance(u, dict))
            if len(items) < 100:
                return

    def find_user(self, first: str, last: str):
        first, last = (first or "").strip().lower(), (last or "").strip().lower()
        return next((u for u in self.users() if str(u.get("first_name") or "").strip().lower() == first
                     and str(u.get("last_name") or "").strip().lower() == last and u.get("status") != "DELETED"),
                    None)

    def all_cards(self) -> dict[str, list[str]]:
        """UniFi user id -> the printed IDs of their cards (tokens never leave the office)."""
        return {str(u["id"]): [str(c["id"]) for c in u.get("nfc_cards") or [] if isinstance(c, dict) and c.get("id")]
                for u in self.users() if u.get("id") and u.get("nfc_cards")}

    def active_users(self) -> list[dict]:
        """Only what the user sync needs: id and name of each active user (no cards, email or PINs)."""
        out = []
        for u in self.users():
            first, last = str(u.get("first_name") or "").strip(), str(u.get("last_name") or "").strip()
            if u.get("status") == "ACTIVE" and u.get("id") and first and last:
                out.append({"id": str(u["id"]), "first_name": first, "last_name": last, "status": "ACTIVE"})
        return out

    def do(self, req: dict) -> dict:
        action, uid = req.get("action"), req.get("unifi_user_id")
        if action == "CREATE_USER":
            existing = self.find_user(req.get("first_name"), req.get("last_name"))
            if existing:
                uid = existing["id"]
                if existing.get("status") == "DEACTIVATED":
                    self.call("PUT", f"/users/{uid}", {"status": "ACTIVE"})
            else:
                uid = (self.call("POST", "/users", {"first_name": req.get("first_name"),
                                                    "last_name": req.get("last_name")}) or {}).get("id")
                if not uid:
                    raise Refused("UniFi Access didn't return the new user")
            if req.get("policy_ids"):
                self.call("PUT", f"/users/{uid}/access_policies", {"access_policy_ids": req["policy_ids"]})
            return {"unifi_user_id": uid, "linked_existing": bool(existing), "cards": self.cards(uid)}
        if action == "ENROLL_CARD":
            session = self.call("POST", "/credentials/nfc_cards/sessions",
                                {"device_id": req.get("reader_id"), "reset_ua_card": False}) or {}
            sid = session.get("session_id")
            card = None
            try:
                until = time.monotonic() + ENROLL_WAIT_SECONDS
                while time.monotonic() < until:
                    time.sleep(1)
                    s = self.call("GET", f"/credentials/nfc_cards/sessions/{sid}", allow_failure=True)
                    if s and s.get("token"):
                        card = s
                        break
            finally:
                try:
                    self.call("DELETE", f"/credentials/nfc_cards/sessions/{sid}", allow_failure=True)
                except OSError:
                    pass  # the session expires on its own
            if not card:
                raise Refused(f"No card was tapped on the reader within {ENROLL_WAIT_SECONDS} seconds. Try again.")
            self.call("PUT", f"/users/{uid}/nfc_cards", {"token": card["token"], "force_add": False})
            return {"card_id": str(card.get("card_id") or ""), "cards": self.cards(uid)}
        if action == "REMOVE_CARD":
            user = self.call("GET", f"/users/{uid}") or {}
            match = next((c for c in user.get("nfc_cards") or [] if str(c.get("id")) == str(req.get("card_id"))), None)
            if match:
                self.call("PUT", f"/users/{uid}/nfc_cards/delete", {"token": match["token"]})
            return {"cards": self.cards(uid)}
        if action == "DEACTIVATE_USER":
            self.call("PUT", f"/users/{uid}", {"status": "DEACTIVATED"})
            return {"cards": []}
        if action == "REFRESH":
            return {"cards": self.cards(uid)}
        if action == "CHECK_REMOVED":
            turned_off = 0
            for removed in req.get("user_ids") or []:
                user = self.call("GET", f"/users/{removed}", allow_failure=True)
                if user and user.get("status") not in ("DEACTIVATED", "DELETED"):
                    self.call("PUT", f"/users/{removed}", {"status": "DEACTIVATED"})
                    turned_off += 1
            if turned_off:
                log(f"Access: daily check turned off {turned_off} removed staff member(s) still active in UniFi")
            return {"deactivated": turned_off}
        raise Refused(f"Unknown request {action}")


class Network:
    def __init__(self, base: str, key: str, sanitizer: Sanitizer):
        self.base, self.key, self.sanitizer = base.rstrip("/"), key, sanitizer

    def call(self, method: str, path: str, body=None):
        status, out = _http(method, self.base + path, {"X-API-KEY": self.key}, body, timeout=60, tls=LOCAL_TLS)
        if status == 401:
            raise Refused("UniFi Network didn't accept the API key")
        if status >= 400 or out is None:
            raise RuntimeError(f"UniFi Network answered {status} for {path}")
        return self.sanitizer.clean(out)


class HomeAssistant:
    """Home Assistant's own API, through the Supervisor (the App's config asks for homeassistant_api)."""

    def __init__(self, token: str):
        self.token = token

    def get(self, path: str):
        status, out = _http("GET", HA_API + path, {"Authorization": f"Bearer {self.token}"}, timeout=60)
        if status >= 400 or out is None:
            raise RuntimeError(f"Home Assistant answered {status}")
        return out

    def temperature_sensors(self, states: list) -> dict[str, dict]:
        """entity_id -> {name, unit} for each temperature sensor with fridge or freezer in its name."""
        out = {}
        for s in states:
            entity, attrs = s.get("entity_id") or "", s.get("attributes") or {}
            name = str(attrs.get("friendly_name") or entity)
            if (entity.startswith("sensor.") and attrs.get("device_class") == "temperature"
                    and any(w in (entity + " " + name).lower() for w in TEMP_WORDS)):
                unit = "F" if "F" in str(attrs.get("unit_of_measurement") or "") else "C"
                out[entity] = {"name": name[:120], "unit": unit}
        return out

    def history(self, entities: list[str], start: datetime, end: datetime) -> dict[str, list]:
        """entity_id -> [(time, value or None)], including the value at start."""
        query = urllib.parse.urlencode({"filter_entity_id": ",".join(entities), "end_time": end.isoformat(),
                                        "minimal_response": "", "no_attributes": ""})
        rows = self.get(f"/history/period/{urllib.parse.quote(start.isoformat())}?{query}")
        out = {}
        for series in rows or []:
            if not series:
                continue
            entity = series[0].get("entity_id")
            points = []
            for p in series:
                try:
                    value = float(p.get("state"))
                except (TypeError, ValueError):
                    value = None  # unavailable / unknown: a gap
                points.append((datetime.fromisoformat(p["last_changed"]), value))
            out[entity] = points
        return out


def ha_snapshot(states: list, config: dict) -> dict:
    """What PracticeOps shows of Home Assistant: its version and the state of devices and sensors."""
    entities = []
    for s in states:
        entity, attrs = str(s.get("entity_id") or ""), s.get("attributes") or {}
        if entity.split(".", 1)[0] not in SNAPSHOT_DOMAINS:
            continue
        entities.append({"entity_id": entity, "name": attrs.get("friendly_name") or entity, "state": s.get("state"),
                         "unit": attrs.get("unit_of_measurement"), "device_class": attrs.get("device_class"),
                         "last_changed": s.get("last_changed"),
                         "attributes": {k: attrs[k] for k in SNAPSHOT_ATTRIBUTES if k in attrs}})
    return {"config": {"version": config.get("version"), "location_name": config.get("location_name")},
            "entities": entities[:3000]}


def averages(points: list, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """Time-weighted 5-minute averages from Home Assistant's change history. A value holds until the next
    change; buckets with less than half their time covered by a reading are skipped."""
    out = []
    bucket = start
    while bucket < end:
        stop = bucket + TEMP_BUCKET
        total = covered = 0.0
        for i, (at, value) in enumerate(points):
            until = points[i + 1][0] if i + 1 < len(points) else end
            lo, hi = max(at, bucket), min(until, stop)
            if value is not None and hi > lo:
                seconds = (hi - lo).total_seconds()
                total += value * seconds
                covered += seconds
        if covered >= TEMP_BUCKET.total_seconds() / 2:
            out.append((bucket + TEMP_BUCKET / 2, round(total / covered, 2)))
        bucket = stop
    return out


# ------------------------------------------------------------------ the loops

class Agent:
    def __init__(self, options: dict, state: dict):
        self.options, self.state = options, state
        host = options.get("console_host") or "192.168.1.1"
        self.ops = PracticeOps(options.get("api_base") or API_BASE, options["practiceops_token"])
        self.always = bool(options.get("ignore_office_hours"))
        sanitizer = Sanitizer(state["salt"])
        akey, nkey = options.get("unifi_access_key"), options.get("unifi_network_key")
        self.access = Access(options.get("access_url") or f"https://{host}:12445", akey) if akey else None
        self.network = Network(options.get("network_url") or f"https://{host}", nkey, sanitizer) if nkey else None
        ha_token = os.environ.get("SUPERVISOR_TOKEN")
        self.ha = HomeAssistant(ha_token) if ha_token else None
        self.stop = threading.Event()

    def working(self) -> bool:
        return self.always or in_office_hours()

    def wait_for_hours(self, name: str) -> None:
        if self.working():
            return
        log(f"{name}: outside office hours, waiting")
        while not self.working() and not self.stop.wait(30):
            pass
        if not self.stop.is_set():
            log(f"{name}: office hours, working")

    def run_loop(self, name: str, step) -> None:
        """Run step() forever in office hours; errors are logged and retried with a growing pause."""
        backoff = 5
        while not self.stop.is_set():
            self.wait_for_hours(name)
            try:
                pause = step()
                backoff = 5
            except Refused as exc:
                log(f"{name}: {exc}")
                pause = 300 if "token" in str(exc) or "key" in str(exc) else 30
            except Exception as exc:  # network trouble, server waking up: try again soon
                log(f"{name}: {type(exc).__name__}: {exc}")
                pause, backoff = backoff, min(backoff * 2, 120)
            if pause:
                self.stop.wait(pause)

    def sync_users(self) -> None:
        """Once a day: the active UniFi users go to PracticeOps, which links exact names to employees
        and marks staff no longer active in UniFi as inactive (it skips that if the list looks cut off)."""
        today = datetime.now(OFFICE).date().isoformat()
        if self.state.get("_synced_on") == today or time.monotonic() < self.state.get("_sync_retry", 0):
            return
        try:
            users = self.access.active_users()
            out = self.ops.call("/office-agent/unifi/users", {"users": users, "complete": True}) or {}
        except Exception as exc:
            self.state["_sync_retry"] = time.monotonic() + SYNC_RETRY_SECONDS
            log(f"Access: daily user sync failed, trying again in 10 minutes: {exc}")
            return
        self.state["_synced_on"] = today
        log(f"Access: daily user sync: {len(users)} active UniFi users; {out.get('mapped', 0)} newly linked, "
            f"{out.get('reviews', 0)} to review, {out.get('deactivated', 0)} marked inactive")

    # UniFi Access: wait on PracticeOps; do each request as soon as it arrives.
    def access_step(self):
        self.sync_users()
        body: dict = {"version": VERSION, "wait_seconds": POLL_WAIT_SECONDS}
        if time.monotonic() - self.state.get("_listed", -1e9) >= LIST_EVERY_SECONDS:
            try:
                body["readers"], body["policies"] = self.access.readers(), self.access.policies()
                body["cards"] = self.access.all_cards()
            except Exception as exc:
                log(f"Access: couldn't list readers, door policies and cards: {exc}")
            self.state["_listed"] = time.monotonic()
        asked = time.monotonic()
        todo = self.ops.call("/office-agent/unifi/poll", body) or []
        for req in todo:
            if not req.get("id"):
                continue
            log(f"Access: {req.get('action')} started")
            try:
                result = self.access.do(req)
                self.ops.call(f"/office-agent/unifi/requests/{req['id']}", {"ok": True, "result": result})
                log(f"Access: {req.get('action')} done")
            except Exception as exc:
                msg = str(exc) if isinstance(exc, Refused) else f"The office agent couldn't reach UniFi: {exc}"
                self.ops.call(f"/office-agent/unifi/requests/{req['id']}", {"ok": False, "error": msg[:900]})
                log(f"Access: {req.get('action')} failed: {msg}")
        # PracticeOps normally holds the poll open; if it answered at once with nothing, don't spin.
        return 0 if todo or time.monotonic() - asked > 5 else 5

    # UniFi Network: the live point each minute and the full dashboard every 5 minutes.
    def network_step(self):
        started = time.monotonic()
        health = self.network.call("GET", "/proxy/network/api/s/default/stat/health")
        self.ops.call("/office-agent/network/live", {"health": list((health or {}).get("data") or [])})
        if time.monotonic() - self.state.get("_full", -1e9) >= FULL_EVERY_SECONDS:
            self.state["_full"] = time.monotonic()
            plan = self.ops.call("/office-agent/network/plan") or {}
            results = []
            for call in plan.get("calls") or []:
                try:
                    results.append({"key": call["key"], "data": self.network.call(call["method"], call["path"],
                                                                                  call.get("body"))})
                except Exception as exc:
                    log(f"Network: {call.get('path')} failed: {exc}")
            self.ops.call("/office-agent/network/collect", {"now_ms": plan.get("now_ms"), "results": results})
            log(f"Network: dashboard updated ({len(results)} of {len(plan.get('calls') or [])} reads)")
        return max(LIVE_EVERY_SECONDS - (time.monotonic() - started), 1)

    # Home Assistant: the read-only snapshot, then the temperatures.
    def home_assistant_step(self):
        states = self.ha.get("/states")
        try:
            self.ops.call("/office-agent/home-assistant", ha_snapshot(states, self.ha.get("/config") or {}))
        except Refused:
            raise
        except Exception as exc:
            log(f"Home Assistant: couldn't send the snapshot: {exc}")
        return self.temperatures(states)

    # Temperatures: 5-minute averages since the last send (overnight and weekends included).
    def temperatures(self, states: list):
        sensors = self.ha.temperature_sensors(states)
        if not sensors:
            if not self.state.get("_no_sensors_logged"):
                log("Temperatures: no Home Assistant temperature sensor with fridge or freezer in its name yet")
                self.state["_no_sensors_logged"] = True
            return TEMPS_EVERY_SECONDS
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=now.timestamp() % TEMP_BUCKET.total_seconds())
        try:
            start = datetime.fromisoformat(self.state["temps_until"])
        except (KeyError, TypeError, ValueError):
            start = end - TEMPS_FIRST_RUN
        start = max(start, end - TEMPS_MAX_GAP)
        if start >= end:
            return TEMPS_EVERY_SECONDS
        readings = []
        for entity, points in self.ha.history(sorted(sensors), start, end).items():
            info = sensors.get(entity)
            if info:
                readings += [{"source_key": entity, "value": v, "unit": info["unit"], "name": info["name"],
                              "at": at.isoformat()} for at, v in averages(points, start, end)]
        for i in range(0, len(readings), 500):
            self.ops.call("/stock/temperatures/ingest", {"readings": readings[i:i + 500]})
        self.state["temps_until"] = end.isoformat()
        save_state(self.state)
        if end - start > TEMP_BUCKET * 2:
            log(f"Temperatures: sent {len(readings)} readings for {len(sensors)} sensor(s) since "
                f"{start.astimezone(OFFICE):%a %H:%M}")
        return TEMPS_EVERY_SECONDS

    def start(self) -> list[threading.Thread]:
        threads = []
        if self.access:
            threads.append(threading.Thread(target=self.run_loop, args=("Access", self.access_step), daemon=True))
        if self.network:
            threads.append(threading.Thread(target=self.run_loop, args=("Network", self.network_step), daemon=True))
        if self.ha:
            threads.append(threading.Thread(target=self.run_loop, args=("Home Assistant", self.home_assistant_step),
                                            daemon=True))
        for t in threads:
            t.start()
        return threads


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if not state.get("salt"):  # keeps device tokens the same across restarts
        state["salt"] = secrets.token_hex(16)
        save_state(state)
    return state


def save_state(state: dict) -> None:
    """Only what must survive a restart: the MAC salt and how far temperatures have been sent."""
    keep = {k: state[k] for k in ("salt", "temps_until") if k in state}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f)
    os.replace(tmp, STATE_FILE)


def main() -> int:
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as f:
            options = json.load(f)
    except (OSError, ValueError):
        log("Can't read the App's Configuration. Open the Configuration tab, fill it in and save.")
        return 1
    if not (options.get("practiceops_token") or "").strip():
        log("No PracticeOps token yet. Create one in PracticeOps (Admin > Network), paste it in the "
            "Configuration tab, save and restart the App.")
        return 1
    agent = Agent(options, load_state())
    log(f"PracticeOps office agent {VERSION} started. UniFi Access key: "
        f"{'set' if agent.access else 'missing (user and card requests will wait)'}. UniFi Network key: "
        f"{'set' if agent.network else 'missing (the network dashboard will not update)'}. Home Assistant and temperatures: "
        f"{'on' if agent.ha else 'off (not running inside Home Assistant)'}."
        + (" Ignoring office hours (testing)." if agent.always else ""))
    threads = agent.start()
    if not threads:
        log("There is nothing to do: add the UniFi keys in the Configuration tab.")
        return 1
    while all(t.is_alive() for t in threads):
        time.sleep(5)
    log("A worker stopped unexpectedly; exiting so Home Assistant restarts the App.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

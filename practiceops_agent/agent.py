"""PracticeOps office agent: a Home Assistant App on the office Raspberry Pi.

PracticeOps runs in Azure and can't reach the UniFi console on the office network, so this App does
the office side. It works in office hours only (Mon-Fri 7:45-17:15, Sat 7:45-12:15, New York time)
so the PracticeOps server can sleep at night; outside those hours it just waits.

  * UniFi Access: waits on PracticeOps for user and card requests (create the UniFi user, enroll a
    card at a reader, remove a card, deactivate, the daily check of removed staff) and does them with
    the Access API. A request starts within a couple of seconds of the click in PracticeOps.
  * UniFi Network: every minute the WAN rates for the live strip; every 5 minutes the reads the
    IT / Network dashboard needs (PracticeOps sends the list).

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
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

VERSION = "2.0.0"
DATA = os.environ.get("AGENT_DATA", "/data")
OPTIONS_FILE = os.path.join(DATA, "options.json")
STATE_FILE = os.path.join(DATA, "state.json")
API_BASE = "https://api-ops.aksharpediatrics.care/api/v1"

OFFICE = ZoneInfo("America/New_York")
# (open, close) in minutes after midnight by weekday, Monday = 0; the same hours as PracticeOps.
OFFICE_HOURS = {0: (465, 1035), 1: (465, 1035), 2: (465, 1035), 3: (465, 1035), 4: (465, 1035), 5: (465, 735)}

POLL_WAIT_SECONDS = 25        # PracticeOps holds the poll open this long waiting for a request
LIST_EVERY_SECONDS = 300      # how often to send PracticeOps the readers and door policies
LIVE_EVERY_SECONDS = 60       # live throughput point
FULL_EVERY_SECONDS = 290      # full dashboard
ENROLL_WAIT_SECONDS = 90      # how long to wait for the card tap when enrolling
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

    def find_user(self, first: str, last: str):
        first, last = (first or "").strip().lower(), (last or "").strip().lower()
        for page in range(1, 41):
            users = self.call("GET", f"/users?page_num={page}&page_size=200") or []
            for u in users:
                if (str(u.get("first_name") or "").strip().lower() == first
                        and str(u.get("last_name") or "").strip().lower() == last and u.get("status") != "DELETED"):
                    return u
            if len(users) < 200:
                return None
        return None

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


# ------------------------------------------------------------------ the two loops

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

    # UniFi Access: wait on PracticeOps; do each request as soon as it arrives.
    def access_step(self):
        body: dict = {"version": VERSION, "wait_seconds": POLL_WAIT_SECONDS}
        if time.monotonic() - self.state.get("_listed", -1e9) >= LIST_EVERY_SECONDS:
            try:
                body["readers"], body["policies"] = self.access.readers(), self.access.policies()
            except Exception as exc:
                log(f"Access: couldn't list readers and door policies: {exc}")
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

    def start(self) -> list[threading.Thread]:
        threads = []
        if self.access:
            threads.append(threading.Thread(target=self.run_loop, args=("Access", self.access_step), daemon=True))
        if self.network:
            threads.append(threading.Thread(target=self.run_loop, args=("Network", self.network_step), daemon=True))
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
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"salt": state["salt"]}, f)
    return state


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
        f"{'set' if agent.network else 'missing (the network dashboard will not update)'}."
        + (" Ignoring office hours (testing)." if agent.always else ""))
    threads = agent.start()
    if not threads:
        log("Neither UniFi key is set, so there is nothing to do. Add them in the Configuration tab.")
        return 1
    while all(t.is_alive() for t in threads):
        time.sleep(5)
    log("A worker stopped unexpectedly; exiting so Home Assistant restarts the App.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

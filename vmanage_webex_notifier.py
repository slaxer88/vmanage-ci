#!/usr/bin/env python3
"""
vManage Alarm → Webex Notifier
Polls vManage for new alarms and forwards them to a Webex space.
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
VMANAGE_HOST     = os.getenv("VMANAGE_HOST", "https://vmanage.example.com")
VMANAGE_USER     = os.getenv("VMANAGE_USER", "admin")
VMANAGE_PASS     = os.getenv("VMANAGE_PASS", "admin")
WEBEX_TOKEN      = os.getenv("WEBEX_TOKEN", "")
# 수신자: 이메일(쉼표 구분) 또는 Room ID 중 하나만 설정
WEBEX_TO_EMAILS  = [e.strip() for e in os.getenv("WEBEX_TO_EMAILS", "").split(",") if e.strip()]
WEBEX_ROOM_ID    = os.getenv("WEBEX_ROOM_ID", "")          # 이메일 미설정 시 fallback
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "60"))   # seconds
SEVERITY_FILTER  = os.getenv("SEVERITY_FILTER", "").split(",")  # e.g. "critical,major"


# ── Severity emoji map ───────────────────────────────────────────────────────
SEVERITY_EMOJI = {
    "critical": "🔴",
    "major":    "🟠",
    "medium":   "🟡",
    "minor":    "🟢",
    "warning":  "⚪",
}


class VManageClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.base = VMANAGE_HOST.rstrip("/")
        self._login()

    def _login(self):
        url = f"{self.base}/j_security_check"
        resp = self.session.post(url, data={
            "j_username": VMANAGE_USER,
            "j_password": VMANAGE_PASS,
        }, timeout=15)
        if resp.status_code != 200 or "html" in resp.headers.get("content-type", ""):
            raise RuntimeError(f"vManage login failed: {resp.status_code}")

        # CSRF token
        token_resp = self.session.get(f"{self.base}/dataservice/client/token", timeout=10)
        if token_resp.ok:
            self.session.headers.update({"X-XSRF-TOKEN": token_resp.text.strip()})
        log.info("vManage login OK")

    def get_alarms(self, from_ts_ms: int, to_ts_ms: int) -> list[dict]:
        """Fetch alarms in a time window. Tries multiple methods for compatibility."""

        # ── Method 1: POST with query filter (vManage 20.x+) ──────────────
        payload: dict = {
            "query": {
                "condition": "AND",
                "rules": [
                    {"value": [str(from_ts_ms)], "field": "entry_time", "type": "date", "operator": "greater_equal"},
                    {"value": [str(to_ts_ms)],   "field": "entry_time", "type": "date", "operator": "less_equal"},
                ],
            }
        }
        if SEVERITY_FILTER and SEVERITY_FILTER != [""]:
            payload["query"]["rules"].append({
                "value": [s.lower() for s in SEVERITY_FILTER],
                "field": "severity", "type": "string", "operator": "in",
            })
        try:
            resp = self.session.post(f"{self.base}/dataservice/alarms", json=payload, timeout=20)
            if resp.status_code == 200:
                return [a for a in resp.json().get("data", []) if a.get("entry_time", 0) >= from_ts_ms]
            log.warning(f"POST /alarms {resp.status_code} — trying GET fallback")
        except Exception as e:
            log.warning(f"POST /alarms error: {e} — trying GET fallback")

        # ── Method 2: GET fallback ─────────────────────────────────────────
        try:
            resp = self.session.get(
                f"{self.base}/dataservice/alarms",
                params={"startTime": from_ts_ms, "endTime": to_ts_ms},
                timeout=20,
            )
            if resp.status_code == 200:
                return [a for a in resp.json().get("data", []) if a.get("entry_time", 0) >= from_ts_ms]
            log.warning(f"GET /alarms {resp.status_code} — trying /event fallback")
        except Exception as e:
            log.warning(f"GET /alarms error: {e} — trying /event fallback")

        # ── Method 3: /dataservice/event (some older versions) ────────────
        try:
            resp = self.session.get(
                f"{self.base}/dataservice/event",
                params={"startTime": from_ts_ms, "endTime": to_ts_ms, "count": 100},
                timeout=20,
            )
            if resp.status_code == 200:
                return [a for a in resp.json().get("data", []) if a.get("entry_time", 0) >= from_ts_ms]
            log.warning(f"GET /event {resp.status_code}")
        except Exception as e:
            log.warning(f"GET /event error: {e}")

        return []


class WebexNotifier:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {WEBEX_TOKEN}",
            "Content-Type": "application/json",
        }

    def _build_text(self, alarm: dict) -> tuple[str, str]:
        """Returns (plain_text, markdown_text) for the alarm."""
        severity   = alarm.get("severity", "unknown").lower()
        alarm_type = alarm.get("type", "unknown")
        system_ip  = alarm.get("system_ip", "N/A")
        host_name  = alarm.get("host_name", "N/A")
        message    = alarm.get("message", "")
        ts_ms      = alarm.get("entry_time", 0)
        ts_str     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        emoji      = SEVERITY_EMOJI.get(severity, "❓")

        md = (
            f"{emoji} **[{severity.upper()}] vManage Alarm**\n"
            f"- **Type:** {alarm_type}\n"
            f"- **Device:** {host_name} ({system_ip})\n"
            f"- **Time:** {ts_str}\n"
            f"- **Message:** {message}"
        )
        return severity, alarm_type, host_name, md

    def _post(self, payload: dict, label: str):
        try:
            resp = requests.post(
                "https://webexapis.com/v1/messages",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            log.info(f"Webex notified → {label}")
        except Exception as e:
            log.error(f"Webex send failed → {label}: {e}")

    def send(self, alarm: dict):
        severity, alarm_type, host_name, md = self._build_text(alarm)
        label = f"[{severity}] {alarm_type} @ {host_name}"

        if WEBEX_TO_EMAILS:
            # 이메일 수신자 각각에게 DM 전송
            for email in WEBEX_TO_EMAILS:
                self._post({"toPersonEmail": email, "markdown": md}, email)
        elif WEBEX_ROOM_ID:
            # fallback: Room 전송
            self._post({"roomId": WEBEX_ROOM_ID, "markdown": md}, WEBEX_ROOM_ID)
        else:
            log.warning("WEBEX_TO_EMAILS / WEBEX_ROOM_ID 미설정 — 알람 전송 생략")


def main():
    log.info("vManage → Webex Alarm Notifier starting...")
    client   = VManageClient()
    notifier = WebexNotifier()

    # Start from now
    last_ts = int(time.time() * 1000)

    while True:
        time.sleep(POLL_INTERVAL)
        now_ts = int(time.time() * 1000)
        log.info(f"Polling alarms [{last_ts} → {now_ts}]")

        alarms = client.get_alarms(last_ts, now_ts)
        if alarms:
            log.info(f"Found {len(alarms)} alarm(s)")
            for alarm in alarms:
                notifier.send(alarm)
        else:
            log.info("No new alarms")

        last_ts = now_ts


if __name__ == "__main__":
    main()

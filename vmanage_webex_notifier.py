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
        """Fetch alarms and filter by time window."""

        # ── Method 1: GET (vManage returns recent alarms, we filter by time) ──
        try:
            resp = self.session.get(
                f"{self.base}/dataservice/alarms",
                params={"count": 100},
                timeout=20,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                return self._filter_alarms(data, from_ts_ms)
            log.warning(f"GET /alarms {resp.status_code} — trying POST")
        except Exception as e:
            log.warning(f"GET /alarms error: {e} — trying POST")

        # ── Method 2: POST with query (vManage 20.x+) ─────────────────────
        payload: dict = {
            "query": {
                "condition": "AND",
                "rules": [
                    {"value": [str(from_ts_ms)], "field": "entry_time", "type": "date", "operator": "greater_equal"},
                    {"value": [str(to_ts_ms)],   "field": "entry_time", "type": "date", "operator": "less_equal"},
                ],
            }
        }
        try:
            resp = self.session.post(
                f"{self.base}/dataservice/alarms", json=payload, timeout=20
            )
            if resp.status_code == 200:
                return self._filter_alarms(resp.json().get("data", []), from_ts_ms)
            log.warning(f"POST /alarms {resp.status_code}")
        except Exception as e:
            log.warning(f"POST /alarms error: {e}")

        return []

    def _filter_alarms(self, alarms: list, from_ts_ms: int) -> list:
        """Filter by time window and severity."""
        result = []
        for a in alarms:
            # entry_time 기준 필터
            if a.get("entry_time", 0) < from_ts_ms:
                continue
            # severity 필터 (대소문자 무시)
            if SEVERITY_FILTER and SEVERITY_FILTER != [""]:
                sev = a.get("severity", "").lower()
                if sev not in [s.lower() for s in SEVERITY_FILTER]:
                    continue
            result.append(a)
        return result


class WebexNotifier:
    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {WEBEX_TOKEN}",
            "Content-Type": "application/json",
        }

    def _build_text(self, alarm: dict) -> str:
        severity   = alarm.get("severity", "unknown")
        alarm_type = alarm.get("type", alarm.get("rulename", "unknown"))
        rule_disp  = alarm.get("rule_name_display", alarm_type)
        message    = alarm.get("message", "")
        ts_ms      = alarm.get("entry_time", 0)
        ts_str     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        emoji      = SEVERITY_EMOJI.get(severity.lower(), "❓")

        # 영향받는 디바이스 정보
        devices = alarm.get("devices", [])
        device_ips = ", ".join(d.get("system-ip", "") for d in devices) or "N/A"

        # consumed_events에서 호스트명 추출
        events = alarm.get("consumed_events", [])
        hostnames = list({e.get("host-name", "") for e in events if e.get("host-name")})
        host_str = ", ".join(hostnames) or "N/A"

        # site_id
        site_id = alarm.get("site_id", alarm.get("values_short_display", [{}])[0].get("site-id", "N/A") if alarm.get("values_short_display") else "N/A")

        md = (
            f"{emoji} **[{severity.upper()}] {rule_disp}**\n"
            f"- **Message:** {message}\n"
            f"- **Site ID:** {site_id}\n"
            f"- **Device(s):** {host_str} ({device_ips})\n"
            f"- **Time:** {ts_str}\n"
            f"- **Active:** {alarm.get('active', 'N/A')}"
        )
        return md

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
        md = self._build_text(alarm)
        severity = alarm.get("severity", "unknown")
        rule = alarm.get("rule_name_display", alarm.get("type", ""))
        label = f"[{severity}] {rule}"

        if WEBEX_TO_EMAILS:
            for email in WEBEX_TO_EMAILS:
                self._post({"toPersonEmail": email, "markdown": md}, email)
        elif WEBEX_ROOM_ID:
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

"""Read the five quota buckets from Antigravity Desktop's local language server."""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


RPC_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
BUCKET_IDS = {
    "gemini-5h": "gemini_5h",
    "gemini-weekly": "gemini_weekly",
    "3p-5h": "claude_gpt_5h",
    "3p-weekly": "claude_gpt_weekly",
    "gemini-argon-wtu-weekly": "argon_weekly",
    "gemini-argon-weekly": "argon_weekly",
}
Endpoint = tuple[str, int, str]


def parse_quota_summary(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Translate server bucket IDs and fractions into the dashboard's quota fields."""
    summary = payload.get("response") or payload.get("summary") or payload
    if not isinstance(summary, dict) or not isinstance(summary.get("groups"), list):
        return {}

    buckets: dict[str, dict[str, Any]] = {}
    for group in summary["groups"]:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            bucket_id = bucket.get("bucketId")
            key = BUCKET_IDS.get(bucket_id) if isinstance(bucket_id, str) else None
            if key is None:
                continue

            fraction = bucket.get("remainingFraction")
            remaining = bucket.get("remaining")
            if fraction is None and isinstance(remaining, dict):
                fraction = remaining.get("remainingFraction")
                if fraction is None and remaining.get("case") == "remainingFraction":
                    fraction = remaining.get("value")
            if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
                continue

            entry = {"remainingPct": fraction * 100, "resetsAt": bucket.get("resetTime")}
            if key not in buckets or entry["remainingPct"] < buckets[key]["remainingPct"]:
                buckets[key] = entry
    return buckets


def _list_loopback_listeners() -> set[tuple[int, int]]:
    """Return (PID, port) pairs so a stale registry cannot nominate an unrelated listener."""
    windows = os.name == "nt"
    if windows:
        netstat = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "netstat.exe"
        command = [str(netstat), "-ano", "-p", "tcp"]
    else:
        command = ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()

    if windows:
        matches = re.findall(
            r"(?mi)^[ \t]*TCP[ \t]+127\.0\.0\.1:(\d+)[ \t]+\S+[ \t]+LISTENING[ \t]+(\d+)[ \t]*$",
            result.stdout,
        )
        return {(int(pid), int(port)) for port, pid in matches}

    listeners = set()
    pid = None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        match = re.fullmatch(r"n127\.0\.0\.1:(\d+)", line)
        if pid is not None and match:
            listeners.add((pid, int(match[1])))
    return listeners


def _managed_endpoint() -> Endpoint | None:
    address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if not address or not token:
        return None
    try:
        url = urlsplit(address if "://" in address else "http://" + address)
        port = url.port
    except ValueError:
        return None
    if url.scheme in ("http", "https") and url.hostname in ("127.0.0.1", "localhost") and port:
        return url.scheme, port, token
    return None


def _registered_endpoints(daemon_dir: Path) -> list[Endpoint]:
    try:
        records = sorted(daemon_dir.glob("hub_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []
    if not records:
        return []

    listeners = _list_loopback_listeners()
    if not listeners:
        return []

    for path in records:
        try:
            if path.is_symlink() or path.stat().st_size > 16_384:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        pid, token = record.get("pid"), record.get("csrfToken")
        if type(pid) is not int or not isinstance(token, str) or not token:
            continue
        endpoints = []
        for protocol in ("http", "https"):
            port = record.get(protocol + "Port")
            if type(port) is int and (pid, port) in listeners:
                endpoints.append((protocol, port, token))
        if endpoints:
            return endpoints
    return []


def _request_summary(endpoint: Endpoint, force_refresh: bool) -> dict[str, Any]:
    protocol, port, token = endpoint
    if protocol == "https":
        # The desktop language server uses a self-signed certificate. Both transports
        # connect directly to this loopback address, without redirects or proxies.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection("127.0.0.1", port, timeout=2, context=context)
    else:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)

    try:
        connection.request(
            "POST", RPC_PATH, json.dumps({"forceRefresh": force_refresh}),
            {"Content-Type": "application/json", "Connect-Protocol-Version": "1", "X-Codeium-Csrf-Token": token},
        )
        response = connection.getresponse()
        if response.status != 200:
            return {}
        body = response.read(1_048_577)
        if len(body) > 1_048_576:
            return {}
        payload = json.loads(body)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        connection.close()


class LiveQuotaSource:
    """Read on dashboard refresh, with a short in-memory cache for concurrent panels."""

    def __init__(self) -> None:
        self._daemon_dir = Path.home() / ".gemini" / "antigravity" / "daemon"
        self._lock = threading.Lock()
        self._snapshot: dict[str, dict[str, Any]] = {}
        self._next_poll = 0.0
        self._next_force_refresh = 0.0

    def read(self, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            if not force_refresh and now < self._next_poll:
                return self._snapshot
            force_refresh = force_refresh or now >= self._next_force_refresh
            self._snapshot = self._read_uncached(force_refresh)
            finished = time.monotonic()
            self._next_poll = finished + 10
            if force_refresh and self._snapshot:
                self._next_force_refresh = finished + 60
            return self._snapshot

    def _read_uncached(self, force_refresh: bool) -> dict[str, dict[str, Any]]:
        managed = _managed_endpoint()
        buckets = parse_quota_summary(_request_summary(managed, force_refresh)) if managed else {}
        if not buckets:
            for endpoint in _registered_endpoints(self._daemon_dir):
                if endpoint == managed:
                    continue
                buckets = parse_quota_summary(_request_summary(endpoint, force_refresh))
                if buckets:
                    break
        return buckets

"""Hermetic loopback browser fixture; no real model/mail/configuration."""
from __future__ import annotations
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
work = Path(tempfile.mkdtemp(prefix="model-console-browser-"))
os.environ.update(INFE_PILOT_PREVIEW="1", INFE_PILOT_DB=str(work / "preview.sqlite3"),
    INFE_PILOT_MASTER_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    INFE_PILOT_COOKIE_SECURE="0", INFE_PILOT_ADMIN_EMAILS="boss@example.com")
for name in list(os.environ):
    if name.startswith("INFE_PILOT_DEFAULT_") or name in {"INFE_PILOT_ORIGIN", "INFE_PILOT_WECHAT_GROUP_IMG", "INFE_PILOT_WECHAT_GROUP_UNTIL"}:
        os.environ.pop(name, None)
from pilot_app import web, providers, modelconsole
from pilot_app.tests import admin_fixture

web.db.initialize()
admin_fixture.create_admin(web.db, "boss@example.com")
admin_fixture.create_account(web.db, "member@example.com")
counts = {"generation": 0, "probe": 0, "resources": 0, "resource_urls": [], "resource_timeout": None, "resource_tls": None}


def generate(**kwargs):
    assert kwargs["provider"] == "local_openai" and kwargs["guard_task"] == "summarize"
    assert "Synthetic" in kwargs["prompt"]
    counts["generation"] += 1
    time.sleep(0.5)
    return providers.Generation("Synthetic response never exposed", [], finish="stop", guard={"ok": True, "retried": False})


def health(*args, **kwargs):
    counts["probe"] += 1
    return {"proxy": "ok", "chat_log": "private-path", "upstream": "private-url"}


class _ResourceResponse:
    """Stands in for the pinned HTTPS response; the wire payload only."""
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        if size is None or size < 0:
            data, self.body = self.body, b""
            return data
        data, self.body = self.body[:size], self.body[size:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


RESOURCE_WIRE = json.dumps({
    "schema": 1,
    # Fresh at fixture start; well inside the website's 60 s freshness window.
    "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "cpu": {"state": "ok", "utilization_percent": 12.5},
    "memory": {"state": "ok", "total_bytes": 16_000_000_000, "available_bytes": 8_000_000_000},
    "disk": {"state": "ok", "total_bytes": 100_000_000_000,
             "used_bytes": 40_000_000_000, "free_bytes": 60_000_000_000},
    "gpu": {"state": "ok", "devices": [{"index": 0, "utilization_percent": 0,
                                        "memory_used_mib": 0, "memory_total_mib": 24564,
                                        "temperature_c": 45}]},
    "slots": {"state": "ok", "total": 2, "busy": 1, "idle": 1},
}).encode()


def outbound(request, *, timeout, tls=None):
    # Only the resource path may reach here: generation and health are mocked
    # above and the fixture never talks to a real model box.
    counts["resources"] += 1
    counts["resource_urls"].append(request.full_url)
    counts["resource_timeout"] = timeout
    counts["resource_tls"] = tls
    assert request.get_header("Authorization", "").startswith("Bearer "), "resource fetch must carry the platform key"
    return _ResourceResponse(RESOURCE_WIRE)


with mock.patch.object(providers, "platform_model_default", return_value={
        "provider": "local_openai", "model": "fixture-model", "base_url": "https://model.example.test/v1",
        "enabled": 1, "config_json": "{}", "kind": "model", "platform": True,
        "last_test_at": None, "last_error": "", "updated_at": ""}), \
     mock.patch.object(providers, "platform_connection_key", return_value="fixture-key"), \
     mock.patch.object(providers, "local_model_health", side_effect=health), \
     mock.patch.object(providers, "_outbound_open", side_effect=outbound), \
     mock.patch.object(providers, "generate", side_effect=generate):
    server = web.create_server("127.0.0.1", 0)
    os.environ["INFE_PILOT_ORIGIN"] = "http://127.0.0.1:" + str(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, PILOT_BASE="http://127.0.0.1:" + str(server.server_address[1]),
               PILOT_CONSOLE_EVIDENCE=str(work))
    try:
        code = subprocess.run(["node", "tools/model_console_check.js"], cwd=ROOT, env=env).returncode
        assert counts["generation"] == 1, counts
        assert counts["probe"] == 1, counts
        assert counts["resources"] >= 1, counts
        assert set(counts["resource_urls"]) == {"https://model.example.test/monitor/resources"}, counts
        assert counts["resource_timeout"] == 5, counts
        assert counts["resource_tls"] and counts["resource_tls"].get("ca_file"), counts
        print(json.dumps({"evidence": str(work), "exit": code, "calls": {
            "generation": counts["generation"], "probe": counts["probe"], "resources": counts["resources"]}}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
sys.exit(code)

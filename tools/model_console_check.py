"""Hermetic loopback browser fixture; no real model/mail/configuration."""
from __future__ import annotations
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
counts = {"generation": 0, "probe": 0}


def generate(**kwargs):
    assert kwargs["provider"] == "local_openai" and kwargs["guard_task"] == "summarize"
    assert "Synthetic" in kwargs["prompt"]
    counts["generation"] += 1
    time.sleep(0.5)
    return providers.Generation("Synthetic response never exposed", [], finish="stop", guard={"ok": True, "retried": False})


def health(*args, **kwargs):
    counts["probe"] += 1
    return {"proxy": "ok", "chat_log": "private-path", "upstream": "private-url"}


with mock.patch.object(providers, "platform_model_default", return_value={
        "provider": "local_openai", "model": "fixture-model", "base_url": "https://model.example.test/v1",
        "enabled": 1, "config_json": "{}", "kind": "model", "platform": True,
        "last_test_at": None, "last_error": "", "updated_at": ""}), \
     mock.patch.object(providers, "platform_connection_key", return_value="fixture-key"), \
     mock.patch.object(providers, "local_model_health", side_effect=health), \
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
        print(json.dumps({"evidence": str(work), "exit": code, "calls": counts}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
sys.exit(code)

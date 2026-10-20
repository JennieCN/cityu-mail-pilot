"""Read-only model resources: protocol, validation, cache, truthfulness.

Nothing real is touched: the collector's ``/proc`` reader, disk seam,
``nvidia-smi`` seam and slots opener are patched, and the website's
``providers._outbound_open`` is a fake. Real model endpoints, real devices and
the network are never involved.

What is asserted here is the contract the panel depends on: the frozen
whitelist, per-section independent failure, "never fabricate 0", strict numeric
and timestamp validation, credential/caching identity, no raw body or exception
text, and no generation or production stamping on a read-only path.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from pilot_app import modelconsole, modelresources, providers, tierhealth, web
from pilot_app.database import Database
from pilot_app.security import token_hash
from pilot_app.tests import admin_fixture

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "model_resource_collector", ROOT / "tools" / "model_resource_collector.py")
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)

CONNECTION = {"provider": "local_openai", "model": "synthetic-model",
              "base_url": "https://model.example.test/v1", "platform": True}
KEY = "fixture-not-a-real-key"
TLS = {"ca_file": "/tmp/fixture-ca.pem", "pin": "AB" * 32}

TOP_KEYS = {"schema", "collected_at", "cpu", "memory", "disk", "gpu", "slots"}
#: The website adds its own temporal verdict to the wire whitelist and nothing else.
NORMALIZED_KEYS = TOP_KEYS | {"state", "stale"}
CPU_KEYS = {"state", "utilization_percent"}
MEMORY_KEYS = {"state", "total_bytes", "available_bytes"}
DISK_KEYS = {"state", "total_bytes", "used_bytes", "free_bytes"}
GPU_KEYS = {"state", "devices"}
DEVICE_KEYS = {"index", "utilization_percent", "memory_used_mib", "memory_total_mib", "temperature_c"}
SLOTS_KEYS = {"state", "total", "busy", "idle"}

STAT_A = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
STAT_B = "cpu  200 0 200 1600 0 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
MEMINFO = "MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n"
SLOTS_BODY = b'[{"is_processing": false, "prompt": "private"}, {"is_processing": true, "cache": "private"}]'


class FakeResponse:
    """Minimal HTTP response: bounded reads and optional per-read failure."""

    def __init__(self, body: bytes = b"", error: BaseException | None = None):
        self.body = body
        self.error = error

    def read(self, size: int = -1) -> bytes:
        if self.error is not None:
            raise self.error
        if size is None or size < 0:
            data, self.body = self.body, b""
            return data
        data, self.body = self.body[:size], self.body[size:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def wire(**overrides) -> dict:
    payload = {
        "schema": 1,
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "cpu": {"state": "ok", "utilization_percent": 12.5},
        "memory": {"state": "ok", "total_bytes": 16_000_000_000, "available_bytes": 8_000_000_000},
        "disk": {"state": "ok", "total_bytes": 100_000_000_000,
                 "used_bytes": 40_000_000_000, "free_bytes": 60_000_000_000},
        "gpu": {"state": "ok", "devices": [{"index": 0, "utilization_percent": 0,
                                            "memory_used_mib": 0, "memory_total_mib": 24564,
                                            "temperature_c": 45}]},
        "slots": {"state": "ok", "total": 2, "busy": 1, "idle": 1},
    }
    payload.update(overrides)
    return payload


class CollectorTests(unittest.TestCase):
    """The standard-library collector that runs on the model box."""

    def setUp(self):
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        self.addCleanup(mock.patch.stopall)
        self.opener = None

    def seed(self, *, stats=(STAT_A, STAT_B), meminfo=MEMINFO, disk=(1000, 400, 600),
             gpu_stdout="0, 0, 0, 24564, 45\n", gpu_error=None,
             slots_body=SLOTS_BODY, slots_error=None):
        remaining = list(stats)
        def read_text(path):
            if path == collector.PROC_STAT:
                if not remaining:
                    return None
                return remaining.pop(0) if len(remaining) > 1 else remaining[0]
            if path == collector.PROC_MEMINFO:
                return meminfo
            return None
        mock.patch.object(collector, "_read_text", side_effect=read_text).start()
        mock.patch.object(collector, "_disk_usage", return_value=disk).start()
        nvidia = mock.Mock(side_effect=gpu_error) if gpu_error is not None else \
            mock.Mock(return_value=mock.Mock(returncode=0, stdout=gpu_stdout, stderr=""))
        mock.patch.object(collector, "_nvidia_smi", nvidia).start()
        self.opener = mock.Mock()
        if slots_error is not None:
            self.opener.open.side_effect = slots_error
        else:
            self.opener.open.return_value = FakeResponse(slots_body)
        mock.patch.object(collector, "_SLOTS_OPENER", self.opener).start()
        self.nvidia = nvidia
        return self.opener

    def assert_whitelist(self, value: dict) -> None:
        self.assertEqual(set(value), TOP_KEYS)
        self.assertEqual(set(value["cpu"]), CPU_KEYS)
        self.assertEqual(set(value["memory"]), MEMORY_KEYS)
        self.assertEqual(set(value["disk"]), DISK_KEYS)
        self.assertEqual(set(value["gpu"]), GPU_KEYS)
        for device in value["gpu"]["devices"]:
            self.assertEqual(set(device), DEVICE_KEYS)
        self.assertEqual(set(value["slots"]), SLOTS_KEYS)

    def test_protocol_shape_whitelist_and_real_zero_retained(self):
        self.seed()
        result = collector.collect(upstream_key=KEY)
        self.assert_whitelist(result)
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["cpu"], {"state": "ok", "utilization_percent": 20.0})
        self.assertEqual(result["memory"], {"state": "ok", "total_bytes": 16_000_000 * 1024,
                                            "available_bytes": 8_000_000 * 1024})
        self.assertEqual(result["disk"], {"state": "ok", "total_bytes": 1000,
                                          "used_bytes": 400, "free_bytes": 600})
        self.assertEqual(result["gpu"]["state"], "ok")
        self.assertEqual(result["gpu"]["devices"], [{"index": 0, "utilization_percent": 0.0,
                                                     "memory_used_mib": 0.0, "memory_total_mib": 24564.0,
                                                     "temperature_c": 45.0}])
        self.assertEqual(result["slots"], {"state": "ok", "total": 2, "busy": 1, "idle": 1})
        # A genuine 0 is a reading, not "unknown": exactly what the panel shows.
        self.assertEqual(result["gpu"]["devices"][0]["utilization_percent"], 0.0)
        self.assertEqual(result["gpu"]["devices"][0]["memory_used_mib"], 0.0)
        stamp = dt.datetime.fromisoformat(result["collected_at"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertEqual(stamp.utcoffset(), dt.timedelta(0))
        self.assertNotIn(KEY, json.dumps(result))

    def test_cpu_is_a_diff_excluding_guest_and_never_load_average(self):
        first = "cpu  100 0 100 800 0 0 0 0 5000 5000\n"
        second = "cpu  200 0 200 1600 0 0 0 0 9000 9000\n"
        self.seed(stats=(first, second))
        result = collector.collect(upstream_key=KEY)
        # guest/guest_nice rose by 4000 each; counting them twice would give a
        # very different percentage, so 20.0 proves they were excluded.
        self.assertEqual(result["cpu"]["utilization_percent"], 20.0)
        source = (ROOT / "tools" / "model_resource_collector.py").read_text(encoding="utf-8")
        self.assertNotIn("loadavg", source)
        self.assertNotIn("getloadavg", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("import logging", source)
        self.assertNotIn("print(", source)

    def test_cpu_unknown_when_counters_do_not_move_or_source_missing(self):
        self.seed(stats=(STAT_A, STAT_A))
        self.assertEqual(collector.collect(upstream_key=KEY)["cpu"],
                         {"state": "unknown", "utilization_percent": None})
        collector._CPU_PREVIOUS = None
        collector._CACHE.update(identity=None, at=0.0, value=None)
        self.seed(stats=(None,))
        self.assertEqual(collector.collect(upstream_key=KEY)["cpu"],
                         {"state": "unknown", "utilization_percent": None})

    def test_sections_fail_independently_and_never_fabricate_zero(self):
        self.seed(stats=(None,), meminfo=MEMINFO, disk=(1000, 400, 600),
                  gpu_error=FileNotFoundError("nvidia-smi"), slots_error=TimeoutError("slow"))
        result = collector.collect(upstream_key=KEY)
        self.assert_whitelist(result)
        self.assertEqual(result["cpu"], {"state": "unknown", "utilization_percent": None})
        self.assertEqual(result["memory"]["state"], "ok")
        self.assertEqual(result["disk"]["state"], "ok")
        self.assertEqual(result["gpu"], {"state": "unknown", "devices": []})
        self.assertEqual(result["slots"], {"state": "unknown", "total": None, "busy": None, "idle": None})

    def test_gpu_missing_timeout_nonzero_and_contradictory_memory(self):
        for error in (FileNotFoundError("missing"), collector.subprocess.TimeoutExpired("nvidia-smi", 2),
                      OSError("spawn")):
            collector._CACHE.update(identity=None, at=0.0, value=None)
            collector._CPU_PREVIOUS = None
            self.seed(gpu_error=error)
            self.assertEqual(collector.collect(upstream_key=KEY)["gpu"], {"state": "unknown", "devices": []})
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        self.seed(gpu_stdout="")
        self.assertEqual(collector.collect(upstream_key=KEY)["gpu"], {"state": "unknown", "devices": []})
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        completed = mock.Mock(returncode=1, stdout="0, 0, 0, 24564, 45\n", stderr="boom")
        with mock.patch.object(collector, "_nvidia_smi", return_value=completed):
            self.assertEqual(collector.collect(upstream_key=KEY)["gpu"], {"state": "unknown", "devices": []})
        # Contradictory capacity drops the pair but keeps the other readings.
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        self.seed(gpu_stdout="3, [N/A], 30000, 24564, [Not Supported]\n")
        device = collector.collect(upstream_key=KEY)["gpu"]["devices"][0]
        self.assertEqual(device["index"], 3)
        self.assertIsNone(device["memory_used_mib"])
        self.assertIsNone(device["memory_total_mib"])
        self.assertIsNone(device["utilization_percent"])
        self.assertIsNone(device["temperature_c"])

    def test_gpu_missing_readings_stay_null_and_eight_device_cap(self):
        rows = "\n".join(f"{index}, 5, 100, 200, 40" for index in range(10))
        self.seed(gpu_stdout=rows + "\n")
        devices = collector.collect(upstream_key=KEY)["gpu"]["devices"]
        self.assertEqual(len(devices), collector.MAX_GPU_DEVICES)
        self.assertEqual([device["index"] for device in devices], list(range(8)))
        self.assertEqual(collector.MAX_GPU_DEVICES, 8)

    def test_nvidia_smi_arguments_are_fixed_and_shell_free(self):
        completed = mock.Mock(returncode=0, stdout="0, 1, 2, 3, 4\n", stderr="")
        with mock.patch.object(collector.subprocess, "run", return_value=completed) as run:
            result = collector._gpu_reading()
        args, kwargs = run.call_args
        self.assertEqual(tuple(args[0]), collector.NVIDIA_SMI_ARGS)
        self.assertNotIn("shell", kwargs)
        self.assertLessEqual(kwargs["timeout"], 2)
        self.assertIn("--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu", args[0])
        self.assertEqual(result["state"], "ok")
        for forbidden in ("ps", "-q", "compute-apps", "processes"):
            self.assertNotIn(forbidden, args[0])

    def test_slots_missing_wrong_empty_key_malformed_oversize_timeout_unknown(self):
        # Empty/absent key: no request at all, and never an unauthenticated probe.
        self.seed()
        for key in (None, "", 12345):
            collector._CACHE.update(identity=None, at=0.0, value=None)
            self.assertEqual(collector.collect(upstream_key=key)["slots"],
                             {"state": "unknown", "total": None, "busy": None, "idle": None})
        self.opener.open.assert_not_called()
        # Wrong key (401) and a timeout are both unknown, independently.
        import urllib.error
        error = urllib.error.HTTPError(collector.SLOTS_URL, 401, "unauthorized", {}, None)
        self.seed(slots_error=error)
        self.assertEqual(collector.collect(upstream_key=KEY)["slots"],
                         {"state": "unknown", "total": None, "busy": None, "idle": None})
        self.assertEqual(collector.collect(upstream_key=KEY)["memory"]["state"], "ok")
        collector._CACHE.update(identity=None, at=0.0, value=None)
        self.seed(slots_error=TimeoutError("slow"))
        self.assertEqual(collector.collect(upstream_key=KEY)["slots"]["state"], "unknown")
        # Oversized, malformed and unrecognised shapes are unknown, never 0 busy.
        for body in (b"[" + b" " * collector.SLOTS_MAX_BYTES + b"]",
                     b"not json", b'{"slots": 2}', b'[{"id": 0}, {"id": 1}]'):
            collector._CACHE.update(identity=None, at=0.0, value=None)
            self.seed(slots_body=body)
            self.assertEqual(collector.collect(upstream_key=KEY)["slots"]["state"], "unknown", body[:20])
        self.assertLessEqual(collector.SLOTS_MAX_BYTES, 65536)
        self.assertLessEqual(collector.SLOTS_TIMEOUT_SECONDS, 2)
        self.assertLessEqual(collector.COMMAND_TIMEOUT_SECONDS, 2)

    def test_empty_slots_are_unknown_not_zero_capacity(self):
        self.seed(slots_body=b"[]")
        self.assertEqual(collector.collect(upstream_key=KEY)["slots"]["state"], "unknown")

    def test_timestamp_is_taken_after_all_sections(self):
        order = []
        with mock.patch.object(collector, "_safe", side_effect=lambda *args: order.append("section") or {}), mock.patch.object(collector, "_utc_stamp", side_effect=lambda: order.append("stamp") or "stamp"):
            result = collector._collect_uncached(KEY)
        self.assertEqual(order, ["section"] * 5 + ["stamp"])
        self.assertEqual(result["collected_at"], "stamp")

    def test_slots_redirect_is_refused_and_credential_never_forwarded(self):
        handler = collector._NoRedirect()
        request = mock.Mock(full_url=collector.SLOTS_URL)
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as caught:
            handler.redirect_request(request, None, 302, "found", {}, "http://elsewhere.example/")
        self.assertEqual(caught.exception.code, 302)
        self.assertTrue(any(isinstance(item, collector._NoRedirect) for item in collector._SLOTS_OPENER.handlers),
                        "the slots opener must refuse redirects so the Bearer credential is never forwarded")

    def test_slots_body_content_is_never_returned(self):
        self.seed(slots_body=b'[{"is_processing": false, "prompt": "TOP-SECRET-PROMPT", '
                              b'"cache": "TOP-SECRET-CACHE", "params": {"key": "' + KEY.encode() + b'"}}]')
        result = collector.collect(upstream_key=KEY)
        text = json.dumps(result)
        for hidden in ("TOP-SECRET-PROMPT", "TOP-SECRET-CACHE", KEY, "params", "prompt", "cache"):
            self.assertNotIn(hidden, text)
        self.assertEqual(result["slots"], {"state": "ok", "total": 1, "busy": 0, "idle": 1})

    def test_cache_coalesces_threads_expires_and_separates_keys(self):
        self.seed()
        outcomes = []
        barrier = threading.Barrier(8)
        def read():
            barrier.wait()
            outcomes.append(collector.collect(upstream_key=KEY)["slots"]["total"])
        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(outcomes, [2] * 8)
        self.assertEqual(self.nvidia.call_count, 1)  # one refresh, not eight
        # A <= 15 s cache still expires.
        collector._CACHE["at"] -= collector.CACHE_TTL_SECONDS + 1
        collector.collect(upstream_key=KEY)
        self.assertEqual(self.nvidia.call_count, 2)
        # A changed credential must never reuse the previous reading.
        collector.collect(upstream_key="another-key")
        self.assertEqual(self.nvidia.call_count, 3)
        self.assertLessEqual(collector.CACHE_TTL_SECONDS, 15)
        # Callers cannot mutate the cache through the returned dict.
        first = collector.collect(upstream_key=KEY)
        first["cpu"]["state"] = "tampered"
        self.assertNotEqual(collector.collect(upstream_key=KEY)["cpu"]["state"], "tampered")

    def test_memory_and_disk_capacity_contradictions_are_unknown(self):
        self.seed(meminfo="MemTotal:       100 kB\nMemAvailable:   200 kB\n")
        self.assertEqual(collector.collect(upstream_key=KEY)["memory"]["state"], "unknown")
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        self.seed(meminfo="MemTotal:       0 kB\nMemAvailable:   0 kB\n")
        self.assertEqual(collector.collect(upstream_key=KEY)["memory"]["state"], "unknown")
        collector._CACHE.update(identity=None, at=0.0, value=None)
        collector._CPU_PREVIOUS = None
        self.seed(disk=(100, 80, 80))  # used + free > total
        self.assertEqual(collector.collect(upstream_key=KEY)["disk"]["state"], "unknown")


class ResourceDeadlineTests(unittest.TestCase):
    def test_real_completed_http_body_is_not_lost_at_socket_close(self):
        import http.server
        import urllib.request
        class Complete(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"done")
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Complete)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for module in (collector, modelresources):
                with urllib.request.urlopen("http://127.0.0.1:" + str(server.server_port), timeout=2) as response:
                    self.assertEqual(module._read_bounded(response, 100, 1), b"done")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_late_eof_does_not_accept_a_response(self):
        for module in (collector, modelresources):
            with mock.patch.object(module.time, "monotonic", side_effect=[0, 0, 11]):
                self.assertIsNone(module._read_bounded(FakeResponse(b""), 100, 10))

    def test_real_slow_loopback_body_is_interrupted(self):
        import http.server
        import time
        import urllib.request
        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "40")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for module in (collector, modelresources):
                with urllib.request.urlopen("http://127.0.0.1:" + str(server.server_port), timeout=2) as response:
                    start = time.monotonic()
                    try:
                        result = module._read_bounded(response, 100, 0.12)
                    except (OSError, TimeoutError):
                        result = None
                    self.assertIsNone(result)
                    self.assertLess(time.monotonic() - start, 0.7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ResourceReadingTests(unittest.TestCase):
    """The website consumer: pinned fetch, strict validation, cache identity."""

    def setUp(self):
        modelresources._CACHE.update(identity=None, at=0.0, value=None)
        modelconsole._probe_cache = None
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(providers, "platform_model_default", return_value=dict(CONNECTION)).start()
        mock.patch.object(providers, "platform_connection_key", return_value=KEY).start()
        mock.patch.object(providers, "local_model_tls", return_value=dict(TLS)).start()
        mock.patch.object(providers, "local_model_health", return_value={"proxy": "ok"}).start()
        self.opener = mock.patch.object(providers, "_outbound_open").start()

    def open_with(self, payload, *, body=None):
        raw = body if body is not None else json.dumps(payload).encode()
        # A fresh response per call: a cached/refetched read must not see a
        # consumed body.
        self.opener.side_effect = lambda *args, **kwargs: FakeResponse(raw)
        return self.opener

    def test_no_connection_is_not_configured_and_never_requests(self):
        result = modelresources.reading(None)
        self.assertEqual(result["state"], "not_configured")
        self.assertTrue(result["stale"])
        self.assertIsNone(result["collected_at"])
        self.assertEqual(result["gpu"], {"state": "unknown", "devices": []})
        self.opener.assert_not_called()

    def test_valid_payload_is_narrowed_to_the_whitelist_and_uses_pinned_tls(self):
        self.open_with(wire())
        result = modelresources.reading(CONNECTION)
        self.assertEqual(result["state"], "ok")
        self.assertFalse(result["stale"])
        self.assertEqual(result["cpu"], {"state": "ok", "utilization_percent": 12.5})
        self.assertEqual(result["gpu"]["devices"][0]["utilization_percent"], 0)
        self.assertEqual(result["slots"], {"state": "ok", "total": 2, "busy": 1, "idle": 1})
        self.assertEqual(set(result), NORMALIZED_KEYS)
        request = self.opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://model.example.test/monitor/resources")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(self.opener.call_args.kwargs["timeout"], modelresources.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(self.opener.call_args.kwargs["tls"], TLS)
        self.assertLessEqual(modelresources.REQUEST_TIMEOUT_SECONDS, 5)
        self.assertLessEqual(modelresources.MAX_RESPONSE_BYTES, 32768)
        self.assertNotIn(KEY, json.dumps(result))

    def test_unknown_schema_malformed_body_and_wrong_types(self):
        for payload in (wire(schema=2), wire(schema="1"), wire(schema=True), wire(schema=None)):
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.open_with(payload)
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "unknown", payload.get("schema"))
            self.assertEqual(result["cpu"], {"state": "unknown", "utilization_percent": None})
        for body in (b"not json at all", json.dumps([1, 2, 3]).encode(), b"", b"{}"):
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.open_with(None, body=body)
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "unknown", body[:20])
            self.assertIsNone(result["collected_at"])

    def test_peer_unknown_state_nulls_numbers_and_never_fabricates_zero(self):
        payload = wire(cpu={"state": "unknown", "utilization_percent": 0},
                       memory={"state": "unknown", "total_bytes": 1, "available_bytes": 1},
                       disk={"state": "unknown", "total_bytes": 1, "used_bytes": 0, "free_bytes": 1},
                       gpu={"state": "unknown", "devices": [{"index": 0}]},
                       slots={"state": "unknown", "total": 0, "busy": 0, "idle": 0})
        self.open_with(payload)
        result = modelresources.reading(CONNECTION)
        self.assertEqual(result["cpu"], {"state": "unknown", "utilization_percent": None})
        self.assertEqual(result["memory"]["state"], "unknown")
        self.assertIsNone(result["memory"]["total_bytes"])
        self.assertEqual(result["disk"]["state"], "unknown")
        self.assertIsNone(result["disk"]["used_bytes"])
        self.assertEqual(result["gpu"], {"state": "unknown", "devices": []})
        self.assertEqual(result["slots"]["state"], "unknown")
        self.assertIsNone(result["slots"]["total"])

    def test_nonfinite_bool_out_of_range_and_contradictory_capacity(self):
        payload = wire(
            cpu={"state": "ok", "utilization_percent": 101},
            memory={"state": "ok", "total_bytes": 100, "available_bytes": 200},
            disk={"state": "ok", "total_bytes": 100, "used_bytes": 80, "free_bytes": 80},
            gpu={"state": "ok", "devices": [
                {"index": True, "utilization_percent": 0, "memory_used_mib": 0,
                 "memory_total_mib": 1, "temperature_c": 0},
                {"index": 1.5, "utilization_percent": 0, "memory_used_mib": 0,
                 "memory_total_mib": 1, "temperature_c": 0},
                {"index": 7, "utilization_percent": float("inf"), "memory_used_mib": float("inf"),
                 "memory_total_mib": 10, "temperature_c": 5000},
                {"index": 6, "utilization_percent": 50, "memory_used_mib": 20,
                 "memory_total_mib": 10, "temperature_c": 40},
            ]},
            slots={"state": "ok", "total": 2, "busy": 2, "idle": 1},
        )
        self.open_with(payload)
        result = modelresources.reading(CONNECTION)
        self.assertEqual(result["cpu"]["state"], "unknown")
        self.assertEqual(result["memory"]["state"], "unknown")
        self.assertEqual(result["disk"]["state"], "unknown")
        self.assertEqual(result["slots"]["state"], "unknown")
        devices = result["gpu"]["devices"]
        self.assertEqual([device["index"] for device in devices], [7, 6])
        self.assertIsNone(devices[0]["utilization_percent"])
        self.assertIsNone(devices[0]["memory_used_mib"])
        self.assertIsNone(devices[0]["temperature_c"])
        self.assertEqual(devices[1]["index"], 6)
        self.assertIsNone(devices[1]["memory_used_mib"])  # 20 > 10 contradicts
        self.assertIsNone(devices[1]["memory_total_mib"])
        self.assertEqual(devices[1]["utilization_percent"], 50)

    def test_boolean_and_wrong_typed_numbers_are_rejected(self):
        payload = wire(cpu={"state": "ok", "utilization_percent": True},
                       memory={"state": "ok", "total_bytes": 1.0, "available_bytes": 1},
                       disk={"state": "ok", "total_bytes": "100", "used_bytes": 0, "free_bytes": 0},
                       slots={"state": "ok", "total": True, "busy": 0, "idle": 0})
        self.open_with(payload)
        result = modelresources.reading(CONNECTION)
        self.assertEqual(result["cpu"]["state"], "unknown")
        self.assertEqual(result["memory"]["state"], "unknown")
        self.assertEqual(result["disk"]["state"], "unknown")
        self.assertEqual(result["slots"]["state"], "unknown")

    def test_timestamps_missing_naive_future_and_expired_mark_the_snapshot_stale(self):
        now = dt.datetime.now(dt.timezone.utc)
        cases = {
            "missing": None,
            "naive": "2026-10-01T12:00:00",
            "future": (now + dt.timedelta(seconds=30)).isoformat(timespec="seconds"),
            "old": (now - dt.timedelta(seconds=120)).isoformat(timespec="seconds"),
            "garbage": "<script>alert(1)</script>",
        }
        for name, stamp in cases.items():
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.open_with(wire(collected_at=stamp))
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "stale", name)
            self.assertTrue(result["stale"], name)
            if name in ("future", "old"):
                # Parseable but temporally untrustworthy: the timestamp is kept
                # for display, the snapshot is still wholly stale.
                self.assertIsNotNone(result["collected_at"], name)
            else:
                self.assertIsNone(result["collected_at"], name)
        for name, stamp in {
            "fresh": (now - dt.timedelta(seconds=10)).isoformat(timespec="seconds"),
            "within_slack": (now + dt.timedelta(seconds=2)).isoformat(timespec="seconds"),
            "zulu": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }.items():
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.open_with(wire(collected_at=stamp))
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "ok", name)
            self.assertFalse(result["stale"], name)
            self.assertIsNotNone(result["collected_at"], name)
        self.assertLessEqual(modelresources.STALE_AFTER_SECONDS, 60)
        self.assertLessEqual(modelresources.FUTURE_SLACK_SECONDS, 5)

    def test_extreme_numbers_and_timezone_overflow_are_closed(self):
        huge = 10 ** 1000
        self.open_with(wire(cpu={"state": "ok", "utilization_percent": huge},
                            collected_at="0001-01-01T00:00:00+23:59"))
        result = modelresources.reading(CONNECTION)
        self.assertTrue(result["stale"])
        self.assertEqual(result["cpu"]["state"], "unknown")
        self.assertNotIn(str(huge), json.dumps(result))

    def test_cache_rechecks_age_without_refetching(self):
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=20)).isoformat()
        self.open_with(wire(collected_at=stamp))
        self.assertEqual(modelresources.reading(CONNECTION)["state"], "ok")
        with mock.patch.object(modelresources, "STALE_AFTER_SECONDS", 10):
            self.assertEqual(modelresources.reading(CONNECTION)["state"], "stale")
        self.assertEqual(self.opener.call_count, 1)

    def test_zero_capacity_slots_and_protocol_bounds_are_unknown(self):
        self.open_with(wire(slots={"state": "ok", "total": 0, "busy": 0, "idle": 0}))
        self.assertEqual(modelresources.reading(CONNECTION)["slots"]["state"], "unknown")
        self.assertEqual(modelresources.SLOTS_MAX_TOTAL, collector.SLOTS_MAX_TOTAL)
        self.assertEqual(modelresources.GPU_MAX_MIB, collector.GPU_MAX_MIB)

    def test_url_validation_rejects_http_userinfo_query_fragment_and_private(self):
        self.assertEqual(modelresources._monitor_url("https://model.example.test/v1"),
                         "https://model.example.test/monitor/resources")
        self.assertEqual(modelresources._monitor_url("https://model.example.test/tunnel/v1"),
                         "https://model.example.test/tunnel/monitor/resources")
        for bad in ("http://model.example.test/v1",
                    "https://user:pass@model.example.test/v1",
                    "https://@model.example.test/v1",
                    "https://127.\t0.0.1/v1",
                    "https://model.example.test/v1?token=private",
                    "https://model.example.test/v1#fragment",
                    "https://127.0.0.1/v1",
                    "https://localhost/v1",
                    "", "not-a-url"):
            self.assertEqual(modelresources._monitor_url(bad), "", bad)
        # And the rejected URL never turns into a request.
        for bad in ("http://model.example.test/v1", "https://user:pass@model.example.test/v1",
                    "https://model.example.test/v1?token=private"):
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.opener.reset_mock()
            result = modelresources.reading({**CONNECTION, "base_url": bad})
            self.assertEqual(result["state"], "unknown")
            self.opener.assert_not_called()

    def test_bounded_oversize_timeout_redirect_and_read_failure_are_unknown(self):
        self.open_with(None, body=b"x" * (modelresources.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(modelresources.reading(CONNECTION)["state"], "unknown")
        for error in (providers.ProviderTimeout("private timeout"),
                      providers.ProviderError("TLS private certificate pin mismatch"),
                      providers.ProviderError("HTTP 500 private body"),
                      urllib.error.HTTPError(
                          "https://model.example.test/monitor/resources", 302, "redirect", {}, None),
                      TimeoutError("private socket")):
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            self.opener.side_effect = error
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "unknown")
            self.assertNotIn("private", json.dumps(result))
        modelresources._CACHE.update(identity=None, at=0.0, value=None)
        self.opener.side_effect = None
        self.opener.return_value = FakeResponse(b"", error=TimeoutError("private mid-read"))
        result = modelresources.reading(CONNECTION)
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("private", json.dumps(result))

    def test_only_current_validated_primary_can_use_exact_loopback(self):
        for base in ("https://127.0.0.1:9443/v1", "https://[::1]:9443/v1"):
            connection = {**CONNECTION, "base_url": base}
            with mock.patch.object(providers, "platform_model_connections", return_value=[connection]):
                modelresources._CACHE.update(identity=None, at=0.0, value=None)
                self.open_with(wire())
                result = modelresources.reading(dict(connection))
                self.assertEqual(result["state"], "ok")
                self.assertEqual(self.opener.call_args.args[0].full_url,
                                 base[:-3] + modelresources.MONITOR_PATH)
                self.assertEqual(self.opener.call_args.kwargs["tls"], TLS)
            # Even a populated cache must not survive withdrawal of approval.
            with mock.patch.object(providers, "platform_model_connections", return_value=[]):
                self.opener.reset_mock()
                self.assertEqual(modelresources.reading(connection)["state"], "unknown")
                self.opener.assert_not_called()

    def test_loopback_aliases_and_other_private_hosts_are_rejected(self):
        hosts = ("127.1", "2130706433", "0x7f000001", "0177.0.0.1",
                 "127.0.0.1.", "127.0.0.2", "localhost", "10.0.0.1",
                 "[::ffff:127.0.0.1]", "[::1%25lo0]",
                 "127.0.0.1。", "127.0.0.1．", "10.0.0.1。", "１２７.０.０.１")
        for host in hosts:
            base = "https://" + host + "/v1"
            for allow in (False, True):
                with self.subTest(host=host, allow=allow):
                    self.assertEqual(modelresources._monitor_url(base, allow_loopback=allow), "")
            connection = {**CONNECTION, "base_url": base}
            with mock.patch.object(providers, "platform_model_connections", return_value=[connection]):
                modelresources._CACHE.update(identity=None, at=0.0, value=None)
                self.opener.reset_mock()
                self.assertEqual(modelresources.reading(connection)["state"], "unknown")
                self.opener.assert_not_called()

    def test_wrong_provider_fallback_and_unapproved_connection_never_fetch(self):
        primary = {**CONNECTION, "base_url": "https://127.0.0.1:9443/v1"}
        cases = ({**primary, "provider": "deepseek"},
                 {**primary, "platform_tier": "fallback"},
                 {**primary, "platform": False},
                 {**primary, "model": "not-approved"})
        self.open_with(wire())
        with mock.patch.object(providers, "platform_model_connections", return_value=[primary]):
            for connection in cases:
                modelresources._CACHE.update(identity=None, at=0.0, value=None)
                self.opener.reset_mock()
                self.assertEqual(modelresources.reading(connection)["state"], "unknown")
                self.opener.assert_not_called()
        # A removed local tier cannot be reintroduced when paid fallback is first.
        with mock.patch.object(providers, "platform_model_connections", return_value=[cases[0]]):
            self.assertEqual(modelresources.reading(primary)["state"], "unknown")
            self.opener.assert_not_called()

    def test_missing_or_empty_credential_never_requests(self):
        for value in ("", "   "):
            modelresources._CACHE.update(identity=None, at=0.0, value=None)
            providers.platform_connection_key.return_value = value
            result = modelresources.reading(CONNECTION)
            self.assertEqual(result["state"], "unknown")
            self.opener.assert_not_called()
        providers.platform_connection_key.return_value = KEY

    def test_cache_is_keyed_by_credential_connection_and_tls(self):
        self.open_with(wire())
        modelresources.reading(CONNECTION)
        modelresources.reading(CONNECTION)
        self.assertEqual(self.opener.call_count, 1)  # 15 s cache coalesces
        providers.platform_connection_key.return_value = "rotated-key"
        modelresources.reading(CONNECTION)
        self.assertEqual(self.opener.call_count, 2)  # new credential, new reading
        other = {**CONNECTION, "base_url": "https://other.example.test/v1"}
        with mock.patch.object(providers, "platform_model_default", return_value=other):
            modelresources.reading(other)
        self.assertEqual(self.opener.call_count, 3)  # new connection identity
        providers.local_model_tls.return_value = {"ca_file": "/tmp/other.pem", "pin": "CD" * 32}
        modelresources.reading(CONNECTION)
        self.assertEqual(self.opener.call_count, 4)  # new TLS pin
        modelresources._CACHE["at"] -= modelresources.CACHE_TTL_SECONDS + 1
        modelresources.reading(CONNECTION)
        self.assertEqual(self.opener.call_count, 5)
        self.assertLessEqual(modelresources.CACHE_TTL_SECONDS, 15)

    def test_concurrent_reads_coalesce_into_one_fetch(self):
        self.open_with(wire())
        outcomes = []
        barrier = threading.Barrier(6)
        def read():
            barrier.wait()
            outcomes.append(modelresources.reading(CONNECTION)["state"])
        threads = [threading.Thread(target=read) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(outcomes, ["ok"] * 6)
        self.assertEqual(self.opener.call_count, 1)

    def test_raw_wire_extras_and_exception_text_never_reach_the_result(self):
        payload = wire()
        payload["secret"] = "TOP-SECRET-RAW-BODY"
        payload["cpu"]["raw"] = "TOP-SECRET-PROMPT"
        payload["gpu"]["devices"][0]["argv"] = ["TOP-SECRET-ARGV"]
        self.open_with(payload)
        text = json.dumps(modelresources.reading(CONNECTION))
        for hidden in ("TOP-SECRET-RAW-BODY", "TOP-SECRET-PROMPT", "TOP-SECRET-ARGV", "argv", "raw"):
            self.assertNotIn(hidden, text)
        modelresources._CACHE.update(identity=None, at=0.0, value=None)
        self.opener.side_effect = RuntimeError("TOP-SECRET-EXCEPTION /etc/passwd uid=1000")
        text = json.dumps(modelresources.reading(CONNECTION))
        self.assertNotIn("TOP-SECRET", text)
        self.assertNotIn("passwd", text)

    def test_reading_never_stamps_production_or_generation(self):
        self.open_with(wire())
        database = Database(Path(self._tmpdir()) / "resources.sqlite3")
        database.initialize()
        before = database.get_setting(tierhealth.STATUS_KEY)
        with mock.patch.object(providers, "generate") as generate:
            result = modelconsole.snapshot(database)
        self.assertEqual(result["host_resources"]["state"], "ok")
        self.assertFalse(result["restart_available"])
        self.assertFalse(result["production"]["state"] == "ok" and result["production"]["at"])
        self.assertEqual(database.get_setting(tierhealth.STATUS_KEY), before)
        with database.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM token_usage").fetchone()[0], 0)
        generate.assert_not_called()

    def _tmpdir(self) -> str:
        directory = tempfile.mkdtemp(prefix="model-resources-")
        self.addCleanup(shutil.rmtree, directory, True)
        return directory


class ModelResourceFrontendTests(unittest.TestCase):
    """Static guards on the panel: text nodes only, zero-safe, no new controls."""

    def setUp(self):
        self.static = Path(web.__file__).parent / "static"
        self.script = (self.static / "app.js").read_text(encoding="utf-8")
        self.area = self.script.split("/* Model box:")[1].split(
            "/* ------------------------------------------------------- server metrics */")[0]
        self.html = (self.static / "index.html").read_text(encoding="utf-8")

    def test_resource_cards_use_textcontent_and_keep_real_zeros(self):
        self.assertIn("renderModelResources(data.host_resources, add)", self.area)
        self.assertIn("typeof value === 'number'", self.area)
        self.assertIn("resourceBytes", self.area)
        self.assertNotIn("innerHTML", self.area)
        self.assertNotIn(".slice(", self.area)
        self.assertIn("模型机 GPU", self.area)
        self.assertIn("模型机推理槽", self.area)
        self.assertIn("不代表现状", self.area)

    def test_panel_text_separates_model_box_from_tencent_cloud_and_mac(self):
        self.assertIn("不是腾讯云网站主机，也不是 Mac 的指标", self.html)
        self.assertIn("过期或读不到时明确标未知", self.html)

    def test_no_restart_or_arbitrary_shell_control_was_added(self):
        for forbidden in ("restart", "reboot", "systemctl", "exec(", "child_process"):
            self.assertNotIn(forbidden, self.area.lower())


class ModelResourceEndpointTests(unittest.TestCase):
    """Anonymous 401 / member 404 happen before any resource fetch."""

    def setUp(self):
        import tempfile
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = Database(Path(self.work.name) / "endpoint.sqlite3")
        self.db.initialize()
        self.admin = admin_fixture.create_admin(self.db, "resource-admin@example.com")
        self.member = admin_fixture.create_account(self.db, "resource-member@example.com")
        self.addCleanup(mock.patch.stopall)
        self.opener = mock.patch.object(providers, "_outbound_open").start()
        mock.patch.object(providers, "platform_connection_key", return_value=KEY).start()
        mock.patch.object(providers, "local_model_tls", return_value=dict(TLS)).start()
        mock.patch.object(providers, "platform_model_default", return_value=dict(CONNECTION)).start()
        mock.patch.object(providers, "generate").start()
        mock.patch.object(providers, "local_model_health", return_value={"proxy": "ok"}).start()
        modelresources._CACHE.update(identity=None, at=0.0, value=None)
        modelconsole._probe_cache = None

    def session(self, user):
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
        self.db.create_session(user["id"], token_hash("endpoint-fixture"), expires)

    def request(self, token="endpoint-fixture"):
        headers = {"Cookie": web.SESSION_COOKIE + "=" + token} if token else {}
        return web.Request(method="GET", path="/api/admin/model-server", headers=headers,
                           query={}, body=b"", client="127.0.0.1")

    def test_anonymous_401_and_member_404_never_fetch_resources(self):
        self.session(self.member)
        with mock.patch.object(web, "get_db", return_value=self.db):
            for token, expected in (("", 401), ("endpoint-fixture", 404)):
                with self.assertRaises(web.ApiError) as caught:
                    web.admin_model_server(self.request(token=token))
                self.assertEqual(caught.exception.status, expected)
        self.opener.assert_not_called()

    def test_admin_snapshot_fetches_once_and_returns_only_the_whitelist(self):
        self.session(self.admin)
        self.opener.return_value = FakeResponse(json.dumps(wire()).encode())
        with mock.patch.object(web, "get_db", return_value=self.db):
            response = web.admin_model_server(self.request())
        payload = json.loads(response.body)
        self.assertEqual(payload["host_resources"]["state"], "ok")
        self.assertEqual(set(payload["host_resources"]), NORMALIZED_KEYS)
        self.assertNotIn(KEY, response.body.decode())
        self.assertEqual(self.opener.call_count, 1)


if __name__ == "__main__":
    unittest.main()

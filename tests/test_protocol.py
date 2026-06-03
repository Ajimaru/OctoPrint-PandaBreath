# coding=utf-8
"""Tests for the Panda Breath protocol adapter's pure framing logic.

The adapter owns a background thread and a WebSocket transport, but the
framing, decoding, normalisation, redaction and error-classification
helpers are all reachable without ever opening a socket. Those are what
we exercise here.
"""
from __future__ import absolute_import

import json
import logging
import time

import pytest

from octoprint_pandabreath import protocol as protocol_mod

from octoprint_pandabreath.protocol import (
    PandaProtocolAdapter,
    WORK_MODE_AUTO,
    WORK_MODE_MANUAL,
    WORK_MODE_DRY,
    WORK_MODE_STANDBY,
    MODE_SERVER,
)


@pytest.fixture
def adapter():
    """A client-mode adapter wired with identity for bind frames."""
    return PandaProtocolAdapter(
        client_url="ws://panda.local/ws",
        serial_number="SN123",
        access_code="secret",
        host_ip="10.0.0.5",
    )


# ---- _build_frame -------------------------------------------------------


def test_build_frame_heater_on_off(adapter):
    assert adapter._build_frame("heater_on", {}) == {
        "settings": {"work_on": True}
    }
    assert adapter._build_frame("heater_off", {}) == {
        "settings": {"work_on": False}
    }


def test_build_frame_set_target_coerces_to_int(adapter):
    assert adapter._build_frame("set_target", {"value": "55.9"}) == {
        "settings": {"set_temp": 55}
    }


@pytest.mark.parametrize(
    "mode_name,code",
    [
        ("auto", WORK_MODE_AUTO),
        ("manual", WORK_MODE_MANUAL),
        ("dry", WORK_MODE_DRY),
        ("standby", WORK_MODE_STANDBY),
    ],
)
def test_build_frame_set_mode(adapter, mode_name, code):
    assert adapter._build_frame("set_mode", {"mode": mode_name}) == {
        "settings": {"work_mode": code}
    }


def test_build_frame_set_mode_unknown_returns_none(adapter):
    assert adapter._build_frame("set_mode", {"mode": "bogus"}) is None


def test_build_frame_bind_includes_identity(adapter):
    assert adapter._build_frame("bind", {}) == {
        "printer": {
            "ip": "10.0.0.5",
            "sn": "SN123",
            "access_code": "secret",
        }
    }


def test_build_frame_bind_empty_without_identity():
    bare = PandaProtocolAdapter(client_url="ws://x/ws")
    assert bare._build_frame("bind", {}) == {"printer": {}}


def test_build_frame_query_and_get_settings(adapter):
    assert adapter._build_frame("query", {}) == {"query": 1}
    assert adapter._build_frame("get_settings", {}) == {"get_settings": 1}


def test_build_frame_dry_and_presets(adapter):
    assert adapter._build_frame("set_dry_target", {"value": 60}) == {
        "settings": {"filament_temp": 60}
    }
    assert adapter._build_frame("set_dry_timer", {"hours": 8}) == {
        "settings": {"filament_timer": 8}
    }
    assert adapter._build_frame("preset_pla", {}) == {
        "settings": {"filament_drying_mode": 1}
    }
    assert adapter._build_frame("preset_petg", {}) == {
        "settings": {"filament_drying_mode": 2}
    }
    assert adapter._build_frame("commit_dry", {}) == {
        "settings": {"filament_drying_mode": 3}
    }


def test_build_frame_thresholds_and_running(adapter):
    assert adapter._build_frame("set_filter_threshold", {"value": 40}) == {
        "settings": {"filtertemp": 40}
    }
    assert adapter._build_frame("set_heater_threshold", {"value": 45}) == {
        "settings": {"hotbedtemp": 45}
    }
    assert adapter._build_frame("start_drying", {}) == {
        "settings": {"isrunning": 1}
    }
    assert adapter._build_frame("stop_drying", {}) == {
        "settings": {"isrunning": 0}
    }
    assert adapter._build_frame("scan_printers", {}) == {
        "printer": {"scan": 1}
    }


def test_build_frame_unknown_returns_none(adapter):
    assert adapter._build_frame("does_not_exist", {}) is None


# ---- send_command (no socket -> _send_raw returns False) ----------------


def test_send_command_unknown_returns_false(adapter):
    assert adapter.send_command("nope") is False


def test_send_command_no_socket_returns_false(adapter):
    # No active socket: _send_raw drops the frame and returns False.
    assert adapter.send_command("query") is False


def test_observe_only_suppresses_writes():
    a = PandaProtocolAdapter(client_url="ws://x/ws", observe_only=True)
    assert a.send_command("set_target", value=50) is False


def test_observe_only_allows_safe_commands():
    a = PandaProtocolAdapter(client_url="ws://x/ws", observe_only=True)
    # query is observe-safe; it reaches _send_raw, which returns False only
    # because there's no socket -> but the command was NOT suppressed.
    # We assert via the dedicated whitelist instead of socket behaviour.
    assert "query" in PandaProtocolAdapter._OBSERVE_SAFE_COMMANDS
    assert "heater_off" in PandaProtocolAdapter._OBSERVE_SAFE_COMMANDS
    assert a.is_observe_only() is True


# ---- _decode_frame ------------------------------------------------------


def test_decode_valid_json(adapter):
    assert adapter._decode_frame('{"a": 1}') == {"a": 1}


def test_decode_bytes(adapter):
    assert adapter._decode_frame(b'{"a": 1}') == {"a": 1}


def test_decode_none_returns_none(adapter):
    assert adapter._decode_frame(None) is None


def test_decode_invalid_json_returns_none(adapter):
    assert adapter._decode_frame("not json") is None


def test_decode_invalid_utf8_returns_none(adapter):
    assert adapter._decode_frame(b"\xff\xfe") is None


def test_decode_oversized_text_dropped(adapter):
    big = '"' + "x" * 5000 + '"'
    assert adapter._decode_frame(big) is None


def test_decode_oversized_bytes_dropped(adapter):
    assert adapter._decode_frame(b"x" * 5000) is None


# ---- _normalise_status --------------------------------------------------


def test_normalise_full_settings(adapter):
    out = adapter._normalise_status(
        {
            "settings": {
                "warehouse_temper": "30.5",
                "set_temp": "55",
                "work_on": 1,
                "work_mode": WORK_MODE_DRY,
                "fw_version": "1.2.3",
            }
        }
    )
    assert out["type"] == "status"
    assert out["chamber_temp"] == 30.5
    assert out["target_temp"] == 55.0
    assert out["heater_on"] is True
    assert out["mode"] == "dry"
    assert out["fw_version"] == "1.2.3"


def test_normalise_work_on_zero_is_false(adapter):
    out = adapter._normalise_status({"settings": {"work_on": 0}})
    assert out["heater_on"] is False


def test_normalise_invalid_temp_skipped(adapter):
    # An unparseable temperature is dropped; with nothing else useful in
    # the frame the normaliser returns None (only the "type" key remained).
    out = adapter._normalise_status(
        {"settings": {"warehouse_temper": "not-a-number"}}
    )
    assert out is None


def test_normalise_invalid_temp_kept_alongside_valid(adapter):
    out = adapter._normalise_status(
        {"settings": {"warehouse_temper": "nope", "set_temp": "50"}}
    )
    assert "chamber_temp" not in out
    assert out["target_temp"] == 50.0


def test_normalise_printer_state(adapter):
    out = adapter._normalise_status({"printer": {"state": "2"}})
    assert out["printer_state"] == 2


def test_normalise_printer_list(adapter):
    out = adapter._normalise_status(
        {
            "printer": {
                "list": [
                    {"name": "p1", "ip": "1.1.1.1", "port": 80},
                    "garbage",  # non-dict entries are skipped
                ]
            }
        }
    )
    assert out["printer_list"] == [
        {"name": "p1", "ip": "1.1.1.1", "port": 80}
    ]


def test_normalise_dry_fields(adapter):
    out = adapter._normalise_status(
        {
            "settings": {
                "custom_temp": "60",
                "custom_timer": "8",
                "remaining_seconds": "3600",
                "isrunning": 1,
            }
        }
    )
    assert out["dry_target"] == 60.0
    assert out["dry_timer_hours"] == 8
    assert out["dry_remaining_s"] == 3600
    assert out["is_running"] is True


def test_normalise_response_frame(adapter):
    out = adapter._normalise_status(
        {"response": {"type": "set_hostname", "ok": "1"}}
    )
    assert out["response"]["type"] == "set_hostname"
    assert out["response"]["ok"] == 1
    assert "ts" in out["response"]


def test_normalise_network_blocks(adapter):
    out = adapter._normalise_status(
        {
            "sta": {"ip": "10.0.0.9", "hostname": "panda", "state": "1"},
            "ap": {"ssid": "PandaAP", "ip": "192.168.4.1", "on": "1"},
            "wifi": {"ssid": "HomeNet"},
        }
    )
    assert out["net_sta_ip"] == "10.0.0.9"
    assert out["net_sta_hostname"] == "panda"
    assert out["net_sta_state"] == 1
    assert out["net_ap_ssid"] == "PandaAP"
    assert out["net_ap_on"] is True
    assert out["net_wifi_ssid"] == "HomeNet"


def test_normalise_empty_returns_none(adapter):
    # Only the "type" key would be present -> treated as no useful status.
    assert adapter._normalise_status({"settings": {}}) is None
    assert adapter._normalise_status({"unrelated": 1}) is None


# ---- _redact_frame ------------------------------------------------------


def test_redact_access_code_and_password(adapter):
    raw = json.dumps(
        {"printer": {"access_code": "topsecret", "sn": "X"},
         "wifi": {"password": "hunter2", "ssid": "Net"}}
    )
    redacted = adapter._redact_frame(raw)
    decoded = json.loads(redacted)
    assert decoded["printer"]["access_code"] == "<redacted>"
    assert decoded["printer"]["sn"] == "X"
    assert decoded["wifi"]["password"] == "<redacted>"
    assert decoded["wifi"]["ssid"] == "Net"


def test_redact_passes_through_when_no_secret(adapter):
    raw = '{"settings": {"set_temp": 50}}'
    assert adapter._redact_frame(raw) == raw


def test_redact_invalid_json_unchanged(adapter):
    # Contains the key text but is not valid JSON -> returned as-is.
    raw = "access_code garbage"
    assert adapter._redact_frame(raw) == raw


def test_redact_empty_falsy_value_untouched(adapter):
    raw = json.dumps({"printer": {"access_code": ""}})
    # Empty value isn't redacted (nothing to hide); structure preserved.
    out = json.loads(adapter._redact_frame(raw))
    assert out["printer"]["access_code"] == ""


# ---- _classify_error ----------------------------------------------------


def test_classify_unreachable_errno():
    exc = OSError("boom")
    exc.errno = 61  # ECONNREFUSED
    bucket, text = PandaProtocolAdapter._classify_error(exc)
    assert bucket == "unreachable"
    assert "boom" in text


def test_classify_timeout_text():
    bucket, _ = PandaProtocolAdapter._classify_error(Exception("timed out"))
    assert bucket == "unreachable"


def test_classify_no_route():
    bucket, _ = PandaProtocolAdapter._classify_error(
        Exception("No route to host")
    )
    assert bucket == "unreachable"


def test_classify_fallback_uses_type_name():
    bucket, _ = PandaProtocolAdapter._classify_error(ValueError("weird"))
    assert bucket == "ValueError"


# ---- error-log coalescing -----------------------------------------------


def test_log_reconnect_error_coalesces(adapter):
    e = OSError("down")
    e.errno = 61
    adapter._log_reconnect_error(e)
    assert adapter._last_error_signature == "unreachable"
    assert adapter._suppressed_error_count == 0
    # Same bucket -> suppressed and counted.
    adapter._log_reconnect_error(e)
    adapter._log_reconnect_error(e)
    assert adapter._suppressed_error_count == 2


def test_reset_error_log_state_clears(adapter):
    e = OSError("down")
    e.errno = 61
    adapter._log_reconnect_error(e)
    adapter._log_reconnect_error(e)
    adapter._reset_error_log_state("connected")
    assert adapter._last_error_signature is None
    assert adapter._suppressed_error_count == 0


def _reset_peer_error():
    e = OSError("reset by peer")
    e.errno = 54
    return e


def test_expected_close_logs_warning_when_debug_enabled(adapter, caplog):
    adapter._debug_enabled_getter = lambda: True
    adapter._expecting_close = True
    with caplog.at_level(logging.INFO):
        adapter._log_reconnect_error(_reset_peer_error())
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert any("expected reconnect" in r.getMessage() for r in caplog.records)
    # Flag consumed and dedup state left untouched.
    assert adapter._expecting_close is False
    assert adapter._last_error_signature is None
    assert adapter._suppressed_error_count == 0


def test_expected_close_logs_info_when_debug_disabled(adapter, caplog):
    adapter._debug_enabled_getter = lambda: False
    adapter._expecting_close = True
    with caplog.at_level(logging.INFO):
        adapter._log_reconnect_error(_reset_peer_error())
    levels = {r.levelno for r in caplog.records}
    assert logging.INFO in levels
    assert logging.WARNING not in levels
    assert adapter._expecting_close is False


def test_unexpected_error_after_expected_close_is_warning(adapter, caplog):
    # An expected close consumes the flag; the *next* genuine error must
    # still be a WARNING and enter the dedup bucket.
    adapter._debug_enabled_getter = lambda: False
    adapter._expecting_close = True
    adapter._log_reconnect_error(_reset_peer_error())
    e = OSError("down")
    e.errno = 61
    with caplog.at_level(logging.INFO):
        adapter._log_reconnect_error(e)
    assert adapter._last_error_signature == "unreachable"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_reset_error_log_state_clears_expecting_close(adapter):
    adapter._expecting_close = True
    adapter._reset_error_log_state("connected")
    assert adapter._expecting_close is False


# ---- frame history & callbacks ------------------------------------------


def test_record_frame_appends_history_and_callbacks():
    seen = []
    persisted = []
    a = PandaProtocolAdapter(
        client_url="ws://x/ws",
        on_frame=lambda d, f: seen.append((d, f)),
        on_frame_persist=lambda d, f: persisted.append((d, f)),
    )
    a._record_frame("rx", '{"settings": {"set_temp": 50}}')
    history = a.get_frame_history()
    assert len(history) == 1
    assert history[0]["dir"] == "rx"
    assert seen and seen[0][0] == "rx"
    assert persisted and persisted[0][0] == "rx"


def test_record_frame_truncates_history_copy():
    persisted = []
    a = PandaProtocolAdapter(
        client_url="ws://x/ws",
        on_frame_persist=lambda d, f: persisted.append(f),
    )
    big = json.dumps({"x": "y" * 1000})
    a._record_frame("rx", big)
    # Persisted copy is full; in-memory copy is truncated with an ellipsis.
    assert persisted[0] == big
    assert a.get_frame_history()[0]["frame"].endswith("…")


def test_record_frame_redacts_before_storing():
    persisted = []
    a = PandaProtocolAdapter(
        client_url="ws://x/ws",
        access_code="secret",
        on_frame_persist=lambda d, f: persisted.append(f),
    )
    a._record_frame("tx", json.dumps({"printer": {"access_code": "secret"}}))
    assert "secret" not in persisted[0]
    assert "<redacted>" in persisted[0]


def test_record_frame_decodes_bytes():
    a = PandaProtocolAdapter(client_url="ws://x/ws")
    a._record_frame("rx", b'{"a": 1}')
    assert a.get_frame_history()[0]["frame"] == '{"a": 1}'


# ---- lifecycle / accessors ----------------------------------------------


def test_is_connected_default_false(adapter):
    assert adapter.is_connected() is False


def test_last_rx_zero_while_disconnected(adapter):
    adapter._last_rx = 999.0
    assert adapter.last_rx_timestamp() == 0.0


def test_last_rx_reported_when_connected(adapter):
    adapter._connected = True
    adapter._last_rx = 123.0
    assert adapter.last_rx_timestamp() == 123.0


def test_set_connected_fires_callback():
    changes = []
    a = PandaProtocolAdapter(
        client_url="ws://x/ws",
        on_connection_change=lambda c: changes.append(c),
    )
    a._set_connected(True)
    a._set_connected(True)  # no-op, value unchanged
    a._set_connected(False)
    assert changes == [True, False]


def test_set_connected_clears_last_rx_on_disconnect():
    a = PandaProtocolAdapter(client_url="ws://x/ws")
    a._set_connected(True)
    a._last_rx = 50.0
    a._set_connected(False)
    assert a._last_rx == 0.0


def test_handle_inbound_invokes_on_status():
    statuses = []
    a = PandaProtocolAdapter(
        client_url="ws://x/ws",
        on_status=lambda s: statuses.append(s),
    )
    a._handle_inbound('{"settings": {"set_temp": 50}}', True)
    assert statuses and statuses[0]["target_temp"] == 50.0


def test_handle_inbound_auth_gate_rejects_before_bind():
    statuses = []
    a = PandaProtocolAdapter(
        mode=MODE_SERVER,
        access_code="secret",
        on_status=lambda s: statuses.append(s),
    )
    # Unauthenticated settings frame is dropped, peer stays unauthenticated.
    authed = a._handle_inbound('{"settings": {"set_temp": 50}}', False)
    assert authed is False
    assert statuses == []


def test_handle_inbound_auth_gate_accepts_bind():
    a = PandaProtocolAdapter(mode=MODE_SERVER, access_code="secret")
    authed = a._handle_inbound(
        '{"printer": {"access_code": "secret", "sn": "X"}}', False
    )
    assert authed is True


def test_handle_inbound_ignores_undecodable(adapter):
    assert adapter._handle_inbound("garbage", True) is True


# ---- _send_raw with a fake socket ---------------------------------------


class _FakeSyncSocket:
    """Synchronous websocket-client-style socket double."""

    def __init__(self, fail=False):
        self.sent = []
        self.closed = False
        self._fail = fail

    def send(self, frame):
        if self._fail:
            raise OSError("broken pipe")
        self.sent.append(frame)

    def close(self):
        self.closed = True


def test_send_raw_success_records_tx(adapter):
    sock = _FakeSyncSocket()
    adapter._active_socket = sock
    assert adapter._send_raw({"query": 1}) is True
    assert sock.sent == ['{"query": 1}']
    assert adapter.get_frame_history()[-1]["dir"] == "tx"


def test_send_raw_failure_returns_false(adapter):
    adapter._active_socket = _FakeSyncSocket(fail=True)
    assert adapter._send_raw({"query": 1}) is False


def test_send_command_via_socket_returns_true(adapter):
    adapter._active_socket = _FakeSyncSocket()
    assert adapter.send_command("query") is True


# ---- _close_active_socket -----------------------------------------------


def test_close_active_socket_calls_close(adapter):
    sock = _FakeSyncSocket()
    adapter._active_socket = sock
    adapter._close_active_socket()
    assert sock.closed is True
    assert adapter._active_socket is None


def test_close_active_socket_noop_when_none(adapter):
    adapter._active_socket = None
    adapter._close_active_socket()  # must not raise


def test_close_active_socket_swallows_errors(adapter):
    class Boom:
        def close(self):
            raise RuntimeError("close failed")

    adapter._active_socket = Boom()
    adapter._close_active_socket()  # swallowed
    assert adapter._active_socket is None


def test_force_reconnect_closes_and_resets(adapter):
    sock = _FakeSyncSocket()
    adapter._active_socket = sock
    adapter._last_error_signature = "unreachable"
    adapter._suppressed_error_count = 3
    adapter.force_reconnect()
    assert sock.closed is True
    assert adapter._last_error_signature is None
    assert adapter._suppressed_error_count == 0
    # The deliberate close is flagged so its reset is logged as expected.
    assert adapter._expecting_close is True


def test_stop_without_thread(adapter):
    # No thread started -> stop just flips state and closes the (absent)
    # socket without raising.
    adapter.stop()
    assert adapter.is_connected() is False


# ---- _backoff_sleep -----------------------------------------------------


def test_backoff_sleep_honours_stop_event(adapter):
    # With stop already set the sleep returns immediately.
    adapter._reconnect_delay = 10.0
    adapter._stop_event.set()
    start = time.time()
    adapter._backoff_sleep()
    assert time.time() - start < 0.5


# ---- _build_ssl_context -------------------------------------------------


def test_ssl_context_disabled_returns_none(adapter):
    assert adapter._build_ssl_context(server_side=False) is None


def test_ssl_context_server_requires_cert():
    a = PandaProtocolAdapter(
        mode=MODE_SERVER, tls_enabled=True
    )
    with pytest.raises(RuntimeError):
        a._build_ssl_context(server_side=True)


# ---- client run-loop (driven via a fake websocket module) ---------------


class _FakeWSTimeout(Exception):
    pass


class _FakeClientWS:
    """Stands in for a websocket-client connection in the run loop.

    Replays a scripted sequence of recv() results: strings are returned
    as frames, the ``TIMEOUT`` sentinel raises the timeout exception (to
    drive the keepalive branch), and exhausting the script returns "" so
    the loop breaks and the adapter tears the connection down.
    """

    TIMEOUT = object()

    def __init__(self, script):
        self._script = list(script)
        self.sent = []
        self.closed = False

    def settimeout(self, _t):
        pass

    def recv(self):
        if not self._script:
            return ""  # falsy -> loop breaks
        item = self._script.pop(0)
        if item is _FakeClientWS.TIMEOUT:
            raise _FakeWSTimeout()
        return item

    def send(self, frame):
        self.sent.append(frame)

    def close(self):
        self.closed = True


class _FakeWebsocketModule:
    WebSocketTimeoutException = _FakeWSTimeout

    def __init__(self, ws):
        self._ws = ws
        self.connect_url = None

    def create_connection(self, url, **_kwargs):
        self.connect_url = url
        return self._ws


def test_client_run_loop_full_cycle(monkeypatch):
    statuses = []
    fake_ws = _FakeClientWS(
        script=[
            '{"settings": {"set_temp": 50}}',  # one status frame
            _FakeClientWS.TIMEOUT,             # drive recv-timeout branch
            _FakeClientWS.TIMEOUT,             # second timeout -> keepalive
        ]
    )
    fake_mod = _FakeWebsocketModule(fake_ws)

    a = PandaProtocolAdapter(
        client_url="ws://panda.local/ws",
        serial_number="SN1",
        access_code="code",
        host_ip="10.0.0.2",
        on_status=statuses.append,
        reconnect_delay=0.01,
    )
    # Swap the module-level websocket dependency for our fake and make the
    # backoff a no-op so the test doesn't actually sleep between cycles.
    monkeypatch.setattr(protocol_mod, "websocket", fake_mod)
    monkeypatch.setattr(a, "_backoff_sleep", lambda: a._stop_event.set())
    # Advance the monotonic clock 10s per call so the >=5s keepalive
    # interval elapses between recv timeouts and the query frame is sent.
    ticks = iter(range(0, 100000, 10))
    monkeypatch.setattr(
        protocol_mod.time, "monotonic", lambda: float(next(ticks))
    )

    a._run_client()

    # Bind + get_settings handshake plus the keepalive query were sent.
    assert any("printer" in s for s in fake_ws.sent)
    assert any("get_settings" in s for s in fake_ws.sent)
    assert any("query" in s for s in fake_ws.sent)
    # The status frame reached the on_status callback.
    assert statuses and statuses[0]["target_temp"] == 50.0
    assert fake_mod.connect_url == "ws://panda.local/ws"
    assert fake_ws.closed is True


def test_client_run_loop_connect_error(monkeypatch):
    class _FailingMod:
        WebSocketTimeoutException = _FakeWSTimeout

        def create_connection(self, *_a, **_k):
            err = OSError("connection refused")
            err.errno = 61
            raise err

    a = PandaProtocolAdapter(
        client_url="ws://panda.local/ws", reconnect_delay=0.01
    )
    monkeypatch.setattr(protocol_mod, "websocket", _FailingMod())
    monkeypatch.setattr(a, "_backoff_sleep", lambda: a._stop_event.set())

    a._run_client()
    # The connect failure was classified and recorded.
    assert a._last_error_signature == "unreachable"


def test_run_client_without_url_returns(monkeypatch):
    a = PandaProtocolAdapter(client_url=None)
    monkeypatch.setattr(
        protocol_mod, "websocket", _FakeWebsocketModule(_FakeClientWS([]))
    )
    # Missing URL -> the loop logs an error and returns without connecting.
    a._run_client()
    assert a.is_connected() is False

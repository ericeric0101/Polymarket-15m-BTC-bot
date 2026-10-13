"""scripts/preflight_ws.py with every network call mocked (no real DNS/TLS/WS in tests)."""
import importlib.util
import json
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight_ws", ROOT / "scripts/preflight_ws.py")
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)
GIB = 1024 ** 3


def resolver_ok(host, port, proto=None):
    return [(2, 1, 6, "", ("104.18.0.1", port))]


def resolver_fail(host, port, proto=None):
    raise OSError("nodename nor servname provided")


def tls_ok(host, ctx, timeout):
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname  # never bypassed
    return {"tls_version": "TLSv1.3", "subject_cn": host, "issuer_cn": "Test CA", "not_after": "x"}


def tls_selfsigned(host, ctx, timeout):
    err = ssl.SSLCertVerificationError("self signed certificate")
    err.verify_message = "self signed certificate"
    raise err


def fetch_ok(url, timeout):
    assert url.startswith("https://gamma-api.polymarket.com/markets?slug=btc-updown-15m-")
    return json.dumps([{"clobTokenIds": json.dumps(["111", "222"])}]).encode()


class FakeWS:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self, timeout=None):
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def usage(free_gib):
    return lambda path: SimpleNamespace(free=free_gib * GIB, total=228 * GIB, used=0)


def test_pass_path_subscribes_public_channel_and_closes():
    ws = FakeWS(["[]", json.dumps([{"event_type": "book", "asset_id": "111", "bids": [], "asks": []}])])
    rep = pf.run_preflight(resolver=resolver_ok, tls_connector=tls_ok, fetch=fetch_ok,
                           ws_connect=lambda url, timeout: ws, usage=usage(14.2), clock=Clock(), wall=lambda: 1791711950.0)
    assert rep["result"] == "PASS" and rep["network_ok"]
    assert ws.sent == [{"assets_ids": ["111"], "type": "market"}] and ws.closed
    assert [s["step"] for s in rep["steps"]] == ["DNS", "TLS", "DNS", "MARKET_LOOKUP", "WEBSOCKET"]
    assert rep["disk"]["meets_dry_run_min"] and not rep["disk"]["meets_live_shadow_min"]
    assert rep["credentials_loaded"] is False and rep["order_endpoints_called"] is False
    assert rep["steps"][3]["slug"] == "btc-updown-15m-1791711900"


def test_dns_failure_stops_and_fails():
    rep = pf.run_preflight(resolver=resolver_fail, usage=usage(30))
    assert rep["result"] == "FAIL" and [s["step"] for s in rep["steps"]] == ["DNS"]


def test_tls_verification_failure_is_reported_never_bypassed():
    rep = pf.run_preflight(resolver=resolver_ok, tls_connector=tls_selfsigned, usage=usage(30))
    assert rep["result"] == "FAIL" and rep["steps"][-1]["error"] == "CERTIFICATE_VERIFY_FAILED"


def test_no_valid_message_before_deadline_fails_and_still_closes():
    ws = FakeWS(["not json", json.dumps({"hello": 1})])
    rep = pf.run_preflight(asset_id="111", resolver=resolver_ok, tls_connector=tls_ok,
                           ws_connect=lambda url, timeout: ws, usage=usage(30), clock=Clock())
    step = rep["steps"][-1]
    assert rep["result"] == "FAIL" and step["error"] == "NO_VALID_MESSAGE_BEFORE_DEADLINE" and ws.closed


def test_disk_below_dry_run_minimum_fails_even_with_network_ok():
    ws = FakeWS([json.dumps({"event_type": "price_change", "price_changes": []})])
    rep = pf.run_preflight(asset_id="111", resolver=resolver_ok, tls_connector=tls_ok,
                           ws_connect=lambda url, timeout: ws, usage=usage(12.0), clock=Clock())
    assert rep["network_ok"] and rep["result"] == "FAIL" and not rep["disk"]["meets_dry_run_min"]
    assert rep["disk"]["bot_guard_gib"] == 10.0 and rep["disk"]["above_bot_guard"]


def test_script_never_touches_credentials_or_order_endpoints():
    src = (ROOT / "scripts/preflight_ws.py").read_text()
    for forbidden in ("load_runtime_env", "dotenv", "POLYMARKET_PK", "API_SECRET", "PASSPHRASE", "/order", "/orders",
                      "py_clob_client", "CERT_NONE", "check_hostname = False", "verify=False", "os.remove", "unlink"):
        assert forbidden not in src, forbidden
    assert pf.TOTAL_BUDGET_SEC <= 60.0

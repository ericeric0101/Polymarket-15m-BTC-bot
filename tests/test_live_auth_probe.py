"""LIVE preflight must prove credentials with one harmless authenticated read."""
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger

import bot.launcher as launcher

SECRET_VALUES = ("0x" + "11" * 32, "api-key-value-123", "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0", "passphrase-value-456")
AUTH = {
    "private_key": SECRET_VALUES[0], "api_key": SECRET_VALUES[1], "api_secret": SECRET_VALUES[2],
    "passphrase": SECRET_VALUES[3], "funder": "", "signature_type": "0",
}


@pytest.fixture
def venue(monkeypatch):
    """Route the real py_clob_client_v2 HTTP layer through a recorder."""
    from py_clob_client_v2.http_helpers import helpers

    state = SimpleNamespace(requests=[], respond=lambda req: httpx.Response(200, json={"balance": "0", "allowances": {}}))

    def handler(request):
        state.requests.append(request)
        return state.respond(request)

    monkeypatch.setattr(helpers, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
    return state


@pytest.fixture
def logs():
    lines = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    yield lines
    logger.remove(sink_id)


@pytest.mark.parametrize("status_code, expected", [
    (200, launcher.AUTH_OK), (401, launcher.AUTH_401), (403, launcher.AUTH_403),
    (500, launcher.AUTH_UNKNOWN), (429, launcher.AUTH_UNKNOWN),
])
def test_probe_classifies_venue_status_with_single_read_only_get(venue, status_code, expected):
    venue.respond = lambda _req: httpx.Response(
        status_code, json={"balance": "1", "allowances": {}} if status_code == 200 else {"error": "Unauthorized/Invalid api key"})
    status, detail = launcher.probe_polymarket_auth(AUTH)
    assert status == expected
    assert len(venue.requests) == 1
    request = venue.requests[0]
    assert request.method == "GET" and request.url.path == "/balance-allowance"
    assert request.url.params.get("asset_type") == "COLLATERAL"
    assert request.headers.get("POLY_API_KEY") == AUTH["api_key"]   # authenticated (L2) request
    assert not any(value in detail for value in SECRET_VALUES)


def test_probe_network_failure_is_network_error(venue):
    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)
    venue.respond = fail
    status, detail = launcher.probe_polymarket_auth(AUTH)
    assert status == launcher.AUTH_NETWORK_ERROR and "status_code=None" in detail


def test_probe_unexpected_body_and_local_errors_are_unknown(venue):
    venue.respond = lambda _req: httpx.Response(200, text="not json")
    assert launcher.probe_polymarket_auth(AUTH)[0] == launcher.AUTH_UNKNOWN
    assert launcher.probe_polymarket_auth({"private_key": AUTH["private_key"]})[0] == launcher.AUTH_UNKNOWN


def test_probe_never_calls_mutating_or_key_minting_methods():
    calls = []

    class Recorder:
        def __getattr__(self, name):
            def method(*_args, **_kwargs):
                calls.append(name)
                return {"balance": "0"}
            return method

    assert launcher.probe_polymarket_auth(AUTH, client_factory=lambda _auth: Recorder())[0] == launcher.AUTH_OK
    assert calls == ["get_balance_allowance"]


@pytest.mark.parametrize("exc, expected", [
    (httpx.ReadTimeout("t"), launcher.AUTH_NETWORK_ERROR),
    (ConnectionError("x"), launcher.AUTH_NETWORK_ERROR),
    (type("PolyApiException", (Exception,), {"status_code": None})(), launcher.AUTH_NETWORK_ERROR),
    (type("PolyApiException", (Exception,), {"status_code": 401})(), launcher.AUTH_401),
    (type("PolyApiException", (Exception,), {"status_code": 403})(), launcher.AUTH_403),
    (ValueError("x"), launcher.AUTH_UNKNOWN),
])
def test_classify_auth_probe_error(exc, expected):
    assert launcher.classify_auth_probe_error(exc) == expected


def _preflight_env(monkeypatch, *, complete=True):
    monkeypatch.setenv("POLYMARKET_PK", AUTH["private_key"])
    for name, key in (("POLYMARKET_API_KEY", "api_key"), ("POLYMARKET_API_SECRET", "api_secret"),
                      ("POLYMARKET_PASSPHRASE", "passphrase")):
        monkeypatch.setenv(name, AUTH[key])
    if not complete:
        monkeypatch.delenv("POLYMARKET_PASSPHRASE")
    monkeypatch.setattr(launcher, "resolve_btc_15m_market_slugs", lambda: ["slug"])
    monkeypatch.setattr(launcher, "resolve_best_btc_15m_market",
                        lambda _s: ("slug", [SimpleNamespace(value="up"), SimpleNamespace(value="down")]))


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_live_preflight_fails_closed_on_every_non_ok_status(monkeypatch, venue, logs, status_code):
    _preflight_env(monkeypatch)
    monkeypatch.setattr(launcher, "resolve_polymarket_auth", lambda: dict(AUTH))
    venue.respond = lambda _req: httpx.Response(status_code, json={"error": "Unauthorized/Invalid api key"})
    assert launcher.run_preflight_checks(simulation=False) is None
    text = "".join(logs)
    assert "LIVE preflight FAILED" in text and "PREFLIGHT CHECK PASSED" not in text
    assert not any(value in text for value in SECRET_VALUES)


def test_live_preflight_fails_closed_on_network_error(monkeypatch, venue):
    _preflight_env(monkeypatch)
    monkeypatch.setattr(launcher, "resolve_polymarket_auth", lambda: dict(AUTH))
    venue.respond = lambda req: (_ for _ in ()).throw(httpx.ConnectError("down", request=req))
    assert launcher.run_preflight_checks(simulation=False) is None


def test_live_preflight_passes_only_after_auth_ok(monkeypatch, venue, logs):
    _preflight_env(monkeypatch)
    monkeypatch.setattr(launcher, "resolve_polymarket_auth", lambda: dict(AUTH))
    assert launcher.run_preflight_checks(simulation=False) == AUTH
    text = "".join(logs)
    assert "status=AUTH_OK" in text and "PREFLIGHT CHECK PASSED" in text
    assert not any(value in text for value in SECRET_VALUES)


@pytest.mark.parametrize("status_code, ok", [(401, False), (200, True)])
def test_live_preflight_probes_derived_credentials_when_env_creds_empty(monkeypatch, venue, status_code, ok):
    _preflight_env(monkeypatch, complete=False)
    resolver_calls = []
    monkeypatch.setattr(launcher, "resolve_polymarket_auth", lambda: resolver_calls.append(1) or dict(AUTH))
    venue.respond = lambda _req: httpx.Response(
        status_code, json={"balance": "0", "allowances": {}} if ok else {"error": "Unauthorized/Invalid api key"})
    result = launcher.run_preflight_checks(simulation=False)
    assert resolver_calls == [1]                       # existing LIVE resolution path (create/derive)
    assert [(r.method, r.url.path) for r in venue.requests] == [("GET", "/balance-allowance")]
    assert (result == AUTH) if ok else (result is None)


def test_configured_credentials_are_probed_without_minting_or_trading(monkeypatch, venue):
    _preflight_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_FUNDER", "0x" + "22" * 20)
    from py_clob_client_v2.client import ClobClient
    for name in ("create_api_key", "derive_api_key", "create_or_derive_api_key", "post_order",
                 "create_and_post_order", "cancel", "cancel_all", "update_balance_allowance"):
        if hasattr(ClobClient, name):
            monkeypatch.setattr(ClobClient, name, lambda *_a, _n=name, **_k: pytest.fail(f"{_n} called"))
    auth = launcher.run_preflight_checks(simulation=False)
    assert auth is not None and auth["api_key"] == AUTH["api_key"]
    assert [(r.method, r.url.path) for r in venue.requests] == [("GET", "/balance-allowance")]


def test_dry_run_preflight_does_not_probe(monkeypatch, venue):
    _preflight_env(monkeypatch)
    monkeypatch.setattr(launcher, "resolve_polymarket_auth", lambda: dict(AUTH))
    venue.respond = lambda _req: httpx.Response(401, json={"error": "x"})
    assert launcher.run_preflight_checks(simulation=True) == AUTH
    assert venue.requests == []

import base64
import time

import httpx
import pytest

from trading212_mcp_server.api.client import (
    T212APIError, T212AuthError, T212Client, T212ConfigError, T212Error,
    T212RateLimitError, cursor_from_next_page_path,
)

P = "/api/v0"


def test_basic_auth_header_and_base_url(fake, client):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    assert client.fetch_account().currency_code == "EUR"
    req = fake.calls[0]
    assert req.url.host == "demo.trading212.com"
    expected = base64.b64encode(b"key:secret").decode()
    assert req.headers["authorization"] == f"Basic {expected}"


def test_missing_api_key_raises_config_error_on_first_call(make_client, monkeypatch):
    for var in ("TRADING212_API_KEY", "T212_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = make_client(api_key=None, api_secret=None)
    with pytest.raises(T212ConfigError, match="TRADING212_API_KEY"):
        c.fetch_account()


def test_invalid_environment_is_rejected(make_client):
    c = make_client(environment="production")
    with pytest.raises(T212ConfigError, match="demo"):
        c.fetch_account()


def test_environment_is_normalised(make_client):
    assert make_client(environment=" LIVE ").environment == "live"


def test_t212_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("T212_ENV", "live")
    monkeypatch.setenv("ENVIRONMENT", "demo")
    assert T212Client(api_key="k").environment == "live"


@pytest.mark.parametrize("status,exc,text", [
    (401, T212AuthError, "Authentication failed"),
    (403, T212AuthError, "permission scope"),
    (404, T212APIError, "Not found"),
    (400, T212APIError, "InsufficientFunds"),
])
def test_error_mapping(fake, client, status, exc, text):
    fake.add("GET", f"{P}/equity/orders/1",
             httpx.Response(status, json={"code": "InsufficientFunds"}))
    with pytest.raises(exc, match=text) as info:
        client.fetch_order(1)
    assert isinstance(info.value, T212Error)


def test_429_is_retried_then_succeeds(fake, client):
    fake.add("GET", f"{P}/equity/account/cash",
             httpx.Response(429, headers={"x-ratelimit-reset": str(int(time.time()) + 2)}),
             {"free": 10.0})
    assert client.fetch_cash().free == 10.0
    assert len(fake.calls) == 2
    assert client.drain_wait_time() > 0
    assert client.drain_wait_time() == 0


def test_429_gives_up_after_retries(fake, client):
    fake.add("GET", f"{P}/equity/account/cash", httpx.Response(429))
    with pytest.raises(T212RateLimitError):
        client.fetch_cash()
    assert len(fake.calls) == 3


def test_timeout_is_not_retried(fake, client):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)
    fake.add("POST", f"{P}/equity/orders/market", boom)
    from trading212_mcp_server.models import MarketOrderRequest
    with pytest.raises(T212Error, match="timed out") as info:
        client.place_market_order(MarketOrderRequest(ticker="AAPL_US_EQ", quantity=1))
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)
    assert len(fake.calls) == 1


def test_waits_when_bucket_is_exhausted(fake, client, monkeypatch):
    slept = []
    monkeypatch.setattr("trading212_mcp_server.api.client.time.sleep", slept.append)
    reset = str(int(time.time()) + 5)
    fake.add("GET", f"{P}/equity/account/cash",
             httpx.Response(200, json={"free": 1.0},
                            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}))
    client.fetch_cash()
    client.fetch_cash()
    assert len(slept) == 1 and 0 < slept[0] <= 6


def test_cursor_from_next_page_path():
    assert cursor_from_next_page_path(
        "/api/v0/equity/history/orders?cursor=1782457491290&limit=20") == "1782457491290"
    assert cursor_from_next_page_path(None) is None
    assert cursor_from_next_page_path("/api/v0/history/dividends?limit=20") is None


def _history_item(order_id):
    return {
        "order": {"id": order_id, "ticker": "VWCEd_EQ", "type": "MARKET", "status": "FILLED",
                  "side": "BUY", "currency": "EUR", "quantity": 2.0, "value": None},
        "fill": {"id": 9, "quantity": 2.0, "price": 100.0, "type": "TRADE",
                 "filledAt": "2026-01-01T00:00:00.000Z",
                 "walletImpact": {"currency": "EUR", "netValue": 200.5, "fxRate": 1,
                                  "taxes": []}},
    }


def test_order_history_exposes_next_cursor(fake, client):
    fake.add("GET", f"{P}/equity/history/orders", {
        "items": [_history_item(1)],
        "nextPagePath": "/api/v0/equity/history/orders?cursor=42&limit=1",
    })
    page = client.fetch_order_history(limit=1)
    assert page.next_cursor == "42"
    order = page.items[0]
    assert (order.side, order.ordered_quantity, order.wallet_net_value) == ("BUY", 2.0, 200.5)

    client.fetch_order_history(cursor=page.next_cursor, limit=1)
    assert fake.calls[-1].url.params["cursor"] == "42"


def test_limit_is_clamped(fake, client):
    fake.add("GET", f"{P}/history/dividends", {"items": []})
    client.fetch_dividends(limit=500)
    assert fake.calls[-1].url.params["limit"] == "50"


def test_fetch_all_pages_follows_cursors_and_truncates(fake, client):
    def page(request):
        cursor = int(request.url.params.get("cursor", "0"))
        item = {"ticker": f"T{cursor}", "amount": 1.0}
        nxt = f"/api/v0/history/dividends?cursor={cursor + 1}&limit=1" if cursor < 4 else None
        return httpx.Response(200, json={"items": [item], "nextPagePath": nxt})
    fake.add("GET", f"{P}/history/dividends", page)

    items, truncated = client.fetch_all_pages(
        lambda c: client.fetch_dividends(cursor=c, limit=1), max_items=100)
    assert [i.ticker for i in items] == ["T0", "T1", "T2", "T3", "T4"]
    assert truncated is False

    items, truncated = client.fetch_all_pages(
        lambda c: client.fetch_dividends(cursor=c, limit=1), max_items=2)
    assert len(items) == 2 and truncated is True


def test_instruments_are_cached(fake, client):
    fake.add("GET", f"{P}/equity/metadata/instruments",
             [{"ticker": "AAPL_US_EQ", "name": "Apple", "currencyCode": "USD"}])
    client.fetch_instruments()
    client.fetch_instruments()
    assert len(fake.calls) == 1


def test_config_errors_are_reported_together(make_client, monkeypatch):
    for var in ("TRADING212_API_KEY", "T212_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    c = make_client(api_key=None, environment="prod")
    with pytest.raises(T212ConfigError) as info:
        c.fetch_account()
    assert "TRADING212_API_KEY" in str(info.value) and "'prod'" in str(info.value)


def test_fetch_all_pages_stops_on_repeated_cursor(fake, client):
    fake.add("GET", f"{P}/history/dividends", {
        "items": [{"ticker": "A", "amount": 1.0}],
        "nextPagePath": "/api/v0/history/dividends?cursor=7",
    })
    items, truncated = client.fetch_all_pages(
        lambda c: client.fetch_dividends(cursor=c, limit=1), max_items=100)
    assert len(items) == 2 and truncated is False


def test_fetch_all_pages_caps_last_page(fake, client):
    fake.add("GET", f"{P}/history/dividends",
             {"items": [{"ticker": "A", "amount": 1.0}] * 5})
    items, truncated = client.fetch_all_pages(
        lambda c: client.fetch_dividends(cursor=c), max_items=3)
    assert len(items) == 3 and truncated is True


def test_wait_time_is_per_thread(client):
    import threading
    client._add_wait(5)
    seen = []
    t = threading.Thread(target=lambda: seen.append(client.drain_wait_time()))
    t.start()
    t.join()
    assert seen == [0]
    assert client.drain_wait_time() == 5

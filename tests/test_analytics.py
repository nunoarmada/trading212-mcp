import httpx
import pytest

from trading212_mcp_server.tools import analytics

P = "/api/v0"


@pytest.fixture
def use_client(client, monkeypatch):
    monkeypatch.setattr(analytics, "client", client)
    return client


def _position(ticker, qty, avg, cur, ppl):
    return {"ticker": ticker, "quantity": qty, "averagePrice": avg,
            "currentPrice": cur, "ppl": ppl}


@pytest.fixture
def eur_account_with_usd_stock(fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    fake.add("GET", f"{P}/equity/account/cash",
             {"free": 100.0, "invested": 1000.0, "total": 1200.0, "ppl": 100.0})
    fake.add("GET", f"{P}/equity/portfolio", [
        # EUR ETF: 10 @ 50 -> 60, +100 EUR
        _position("VWCEd_EQ", 10, 50.0, 60.0, 100.0),
        # USD stock: 2 @ 100 USD -> 110 USD, P/L reported in EUR
        _position("AAPL_US_EQ", 2, 100.0, 110.0, 15.0),
        # LSE stock quoted in pence: 100 @ 500p -> 550p, +50 GBP would be wrong ccy here
        _position("VOD_EQ", 100, 500.0, 550.0, 40.0),
    ])
    fake.add("GET", f"{P}/equity/metadata/instruments", [
        {"ticker": "VWCEd_EQ", "name": "Vanguard", "currencyCode": "EUR"},
        {"ticker": "AAPL_US_EQ", "name": "Apple", "currencyCode": "USD"},
        {"ticker": "VOD_EQ", "name": "Vodafone", "currencyCode": "GBX"},
    ])


def test_summary_does_not_mix_currencies(use_client, eur_account_with_usd_stock):
    result = analytics.fetch_portfolio_summary()
    by_ticker = {p["ticker"]: p for p in result["positions"]}

    etf = by_ticker["VWCEd_EQ"]
    assert etf["current_value"] == 600.0
    assert etf["profit_loss_pct"] == 20.0

    usd = by_ticker["AAPL_US_EQ"]
    assert usd["instrument_currency"] == "USD"
    assert usd["value_instrument_ccy"] == 220.0
    assert usd["current_value"] is None          # needs FX: not guessed
    assert usd["profit_loss_pct"] is None
    assert usd["profit_loss"] == 15.0            # already in EUR
    assert usd["price_return_pct"] == 10.0

    assert result["positions"][0]["ticker"] == "VWCEd_EQ"
    assert all(h["current_value"] is not None for h in result["top_holdings"])
    assert any("FX rate" in n for n in result["_notes"])
    assert result["profit_loss_pct"] == 10.0


def test_gbx_converts_to_gbp_account(use_client, fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "GBP"})
    fake.add("GET", f"{P}/equity/account/cash", {"invested": 500.0, "ppl": 50.0})
    fake.add("GET", f"{P}/equity/portfolio", [_position("VOD_EQ", 100, 500.0, 550.0, 50.0)])
    fake.add("GET", f"{P}/equity/metadata/instruments",
             [{"ticker": "VOD_EQ", "name": "Vodafone", "currencyCode": "GBX"}])
    pos = analytics.fetch_portfolio_summary()["positions"][0]
    assert pos["current_value"] == 550.0
    assert pos["profit_loss_pct"] == 10.0


def test_summary_degrades_when_instruments_unavailable(use_client, fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    fake.add("GET", f"{P}/equity/account/cash", {"invested": 0.0})
    fake.add("GET", f"{P}/equity/portfolio", [_position("X_EQ", 1, 1.0, 1.0, 0.0)])
    fake.add("GET", f"{P}/equity/metadata/instruments", httpx.Response(403))
    result = analytics.fetch_portfolio_summary()
    assert result["positions"][0]["current_value"] is None
    assert any("Instrument currencies unavailable" in n for n in result["_notes"])
    assert result["profit_loss_pct"] is None


def _dividend_pages(fake, pages):
    def handler(request):
        idx = int(request.url.params.get("cursor", "0"))
        nxt = (f"/api/v0/history/dividends?cursor={idx + 1}&limit=50"
               if idx + 1 < len(pages) else None)
        return httpx.Response(200, json={"items": pages[idx], "nextPagePath": nxt})
    fake.add("GET", f"{P}/history/dividends", handler)


def _div(ticker, amount, paid_on):
    return {"ticker": ticker, "amount": amount, "paidOn": paid_on}


def test_performance_uses_all_dividend_pages(use_client, fake, eur_account_with_usd_stock):
    fake.add("GET", f"{P}/equity/history/orders", {"items": []})
    _dividend_pages(fake, [
        [_div("VWCEd_EQ", 5.0, "2026-01-10T00:00:00Z")] * 50,
        [_div("VWCEd_EQ", 5.0, "2025-12-10T00:00:00Z")] * 10,
    ])
    result = analytics.fetch_portfolio_performance()
    etf = next(p for p in result["positions"] if p["ticker"] == "VWCEd_EQ")
    assert etf["dividends"] == 300.0              # 60 payments, not just the first 50
    assert etf["return_pct"] == 80.0              # (100 + 300) / 500
    usd = next(p for p in result["positions"] if p["ticker"] == "AAPL_US_EQ")
    assert usd["return_pct"] is None and usd["price_return_pct"] == 10.0
    assert result["ranked_by"] == "price_return_pct"   # never mixes the two metrics
    assert result["best_performer"]["ticker"] == "VWCEd_EQ"
    assert result["total_dividends"] == 300.0
    assert result["dividends_truncated"] is False


def test_dividend_summary_average_spans_gap_months(use_client, fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    _dividend_pages(fake, [[
        _div("A_EQ", 30.0, "2026-01-15T00:00:00Z"),
        _div("B_EQ", 30.0, "2026-03-15T00:00:00Z"),
    ]])
    result = analytics.fetch_dividend_summary()
    assert result["total_dividends"] == 60.0
    assert result["average_monthly"] == 20.0      # Jan..Mar = 3 months, not 2
    assert result["truncated"] is False


def test_recent_activity_merges_and_sorts(use_client, fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    fake.add("GET", f"{P}/equity/history/orders", {"items": [{
        "order": {"id": 1, "ticker": "A_EQ", "side": "SELL", "status": "FILLED",
                  "createdAt": "2026-01-01T00:00:00Z"},
        "fill": {"quantity": 1.0, "filledAt": "2026-01-02T00:00:00Z"},
    }]})
    fake.add("GET", f"{P}/history/transactions", {"items": [
        {"type": "DEPOSIT", "amount": 100.0, "dateTime": "2026-01-03T00:00:00Z"},
    ]})
    result = analytics.fetch_recent_activity(limit=500)
    assert [a["type"] for a in result["activity"]] == ["transaction", "order"]
    assert result["activity"][1]["side"] == "SELL"
    assert fake.calls[1].url.params["limit"] == "50"


def test_unknown_instrument_currency_is_explained(use_client, fake):
    fake.add("GET", f"{P}/equity/account/info", {"id": 1, "currencyCode": "EUR"})
    fake.add("GET", f"{P}/equity/account/cash", {"invested": 10.0})
    fake.add("GET", f"{P}/equity/portfolio", [_position("GONE_EQ", 1, 10.0, 5.0, -5.0)])
    fake.add("GET", f"{P}/equity/metadata/instruments",
             [{"ticker": "OTHER_EQ", "name": "Other", "currencyCode": "EUR"}])
    result = analytics.fetch_portfolio_summary()
    assert any("GONE_EQ" in n for n in result["_notes"])

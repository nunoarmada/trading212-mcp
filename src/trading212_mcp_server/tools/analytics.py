import logging
from typing import Optional

from mcp.types import ToolAnnotations
from trading212_mcp_server.api.client import T212Error
from trading212_mcp_server.app import client, tool
from trading212_mcp_server.models import Position

log = logging.getLogger("trading212")

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

# Upper bound for full-history aggregation, so one call cannot page forever.
_MAX_DIVIDENDS = 2000

# Minor-unit currencies T212 quotes some instruments in (e.g. LSE stocks in pence).
_MINOR_UNITS = {"GBX": ("GBP", 0.01)}


def _to_account_currency(amount: Optional[float], instrument_ccy: Optional[str],
                         account_ccy: str) -> Optional[float]:
    """Convert an instrument-currency amount to the account currency when the
    conversion is exact (same currency, or a minor unit of it). Returns None when
    an FX rate would be needed: the API does not expose one."""
    if amount is None or not instrument_ccy:
        return None
    if instrument_ccy == account_ccy:
        return amount
    major, factor = _MINOR_UNITS.get(instrument_ccy, (None, None))
    if major == account_ccy:
        return amount * factor
    return None


def _instrument_currencies(notes: list[str]) -> dict[str, str]:
    """Map ticker -> instrument currency from the (cached) instrument list."""
    try:
        instruments = client.fetch_instruments()
    except T212Error as e:
        log.warning("Could not load instrument currencies: %s", e)
        notes.append(f"Instrument currencies unavailable ({e}); "
                     "account-currency values were not computed.")
        return {}
    return {inst.ticker: inst.currency_code for inst in instruments if inst.currency_code}


def _position_values(pos: Position, instrument_ccy: Optional[str], account_ccy: str) -> dict:
    """Values for one position, each labelled with the currency it is in.

    T212 reports averagePrice/currentPrice in the instrument currency and
    ppl/fxPpl in the account currency, so they must not be mixed."""
    qty = pos.quantity or 0
    cost = (pos.average_price or 0) * qty
    value = (pos.current_price or 0) * qty
    price_return_pct = (
        round((pos.current_price - pos.average_price) / pos.average_price * 100, 2)
        if pos.average_price else None
    )
    cost_acct = _to_account_currency(cost, instrument_ccy, account_ccy)
    value_acct = _to_account_currency(value, instrument_ccy, account_ccy)
    return {
        "instrument_currency": instrument_ccy,
        "cost_instrument_ccy": round(cost, 2),
        "value_instrument_ccy": round(value, 2),
        "cost": round(cost_acct, 2) if cost_acct is not None else None,
        "current_value": round(value_acct, 2) if value_acct is not None else None,
        "price_return_pct": price_return_pct,
    }


def _finish(result: dict, notes: list[str]) -> dict:
    waited = client.drain_wait_time()
    if waited:
        notes.append(f"Response delayed {waited}s due to Trading 212 rate limits.")
    if notes:
        result["_notes"] = notes
    return result


def _value_sort_key(item: dict):
    # Positions with a known account-currency value first, largest first.
    value = item.get("current_value")
    return (value is None, -(value or 0))


def _currency_notes(rows: list[dict], account_ccy: str, notes: list[str]) -> None:
    unknown = [r["ticker"] for r in rows if r["instrument_currency"] is None]
    needs_fx = sum(1 for r in rows
                   if r["instrument_currency"] is not None and r["current_value"] is None)
    if unknown:
        notes.append(f"Instrument currency unknown for {', '.join(unknown)}; "
                     "their account-currency values are null.")
    if needs_fx:
        notes.append(_FX_NOTE.format(n=needs_fx, ccy=account_ccy))


_FX_NOTE = (
    "{n} position(s) are priced in a currency other than {ccy}. Their cost, "
    "current_value and return percentage are null because the API exposes no FX rate; "
    "profit_loss (already in {ccy}) and price_return_pct (currency-neutral) are still valid."
)


@tool("fetch_portfolio_summary", annotations=_READ)
def fetch_portfolio_summary() -> dict:
    """
    Produce a complete portfolio snapshot in one call by combining account info,
    cash balance, and all open positions into a single aggregated response.

    This is the recommended starting point for portfolio analysis. Portfolio totals
    come from the account balance and are in the account currency. Per position,
    profit_loss is in the account currency; prices are in the instrument currency;
    current_value and profit_loss_pct are in the account currency and are null when
    an FX conversion would be needed. For per-position performance with dividends,
    use fetch_portfolio_performance instead.

    Returns:
        dict with currency, total_value, cash_available, invested, profit_loss,
        profit_loss_pct, position_count, positions (sorted by account-currency value,
        each with instrument_currency, value_instrument_ccy, current_value, profit_loss,
        profit_loss_pct, price_return_pct), top_holdings (top 5) and _notes
    """
    notes: list[str] = []
    account = client.fetch_account()
    cash = client.fetch_cash()
    positions = client.fetch_positions()
    account_ccy = account.currency_code
    currencies = _instrument_currencies(notes) if positions else {}

    holdings = []
    for pos in positions:
        values = _position_values(pos, currencies.get(pos.ticker), account_ccy)
        ppl = pos.ppl or 0
        cost = values["cost"]
        holdings.append({
            "ticker": pos.ticker,
            "quantity": pos.quantity,
            "average_price": pos.average_price,
            "current_price": pos.current_price,
            **values,
            "profit_loss": round(ppl, 2),
            "fx_profit_loss": pos.fx_ppl,
            "profit_loss_pct": round(ppl / cost * 100, 2) if cost else None,
        })

    holdings.sort(key=_value_sort_key)
    if currencies:
        _currency_notes(holdings, account_ccy, notes)

    total = cash.total or 0
    invested = cash.invested or 0
    ppl = cash.ppl or 0

    result = {
        "currency": account_ccy,
        "total_value": round(total, 2),
        "cash_available": round(cash.free or 0, 2),
        "invested": round(invested, 2),
        "profit_loss": round(ppl, 2),
        "profit_loss_pct": round((ppl / invested) * 100, 2) if invested else None,
        "position_count": len(positions),
        "positions": holdings,
        "top_holdings": [h for h in holdings if h["current_value"] is not None][:5],
    }
    return _finish(result, notes)


@tool("fetch_portfolio_performance", annotations=_READ)
def fetch_portfolio_performance() -> dict:
    """
    Build a detailed performance report across all positions by combining
    current holdings, the full dividend history, and recent order history.

    Calculates per-position total returns (price P/L + dividends received, both in
    the account currency) and identifies best and worst performers. Use this for
    deeper analysis than fetch_portfolio_summary, especially when dividend income
    matters. return_pct is null for positions priced in another currency (no FX
    rate is available); price_return_pct is always present.

    Returns:
        dict with currency, total_price_ppl, total_dividends, total_return,
        best_performer, worst_performer (ranked by return_pct when known for every
        position, else by price_return_pct; see ranked_by), positions (each with cost,
        current_value, price_ppl, dividends, total_return, return_pct, price_return_pct,
        held_since), ranked_by,
        recent_filled_orders, dividends_truncated and _notes
    """
    notes: list[str] = []
    account = client.fetch_account()
    account_ccy = account.currency_code
    positions = client.fetch_positions()
    orders = client.fetch_order_history(limit=50).items
    dividends, truncated = client.fetch_all_pages(
        lambda cursor: client.fetch_dividends(cursor=cursor, limit=50),
        max_items=_MAX_DIVIDENDS,
    )
    if truncated:
        notes.append(f"Dividend history truncated to the latest {_MAX_DIVIDENDS} payments.")
    currencies = _instrument_currencies(notes) if positions else {}

    div_by_ticker: dict[str, float] = {}
    for item in dividends:
        div_by_ticker[item.ticker] = div_by_ticker.get(item.ticker, 0) + (item.amount or 0)

    perf = []
    for pos in positions:
        values = _position_values(pos, currencies.get(pos.ticker), account_ccy)
        ppl = pos.ppl or 0
        divs = div_by_ticker.get(pos.ticker, 0)
        total_return = ppl + divs
        cost = values["cost"]
        perf.append({
            "ticker": pos.ticker,
            "quantity": pos.quantity,
            **values,
            "price_ppl": round(ppl, 2),
            "dividends": round(divs, 2),
            "total_return": round(total_return, 2),
            "return_pct": round(total_return / cost * 100, 2) if cost else None,
            "held_since": pos.initial_fill_date.isoformat() if pos.initial_fill_date else None,
        })

    # Rank on total return % (incl. dividends) only if it is known for every position;
    # otherwise rank everything on the currency-neutral price return, never a mix.
    rank_field = ("return_pct" if perf and all(p["return_pct"] is not None for p in perf)
                  else "price_return_pct")

    def rank(p):
        return p[rank_field] if p[rank_field] is not None else float("-inf")

    perf.sort(key=rank, reverse=True)
    if currencies:
        _currency_notes(perf, account_ccy, notes)

    filled = [
        {
            "ticker": o.ticker,
            "type": o.type,
            "side": o.side,
            "quantity": o.filled_quantity,
            "fill_price": o.fill_price,
            "net_value": o.wallet_net_value,
            "net_value_currency": o.wallet_currency,
            "date": o.date_executed.isoformat() if o.date_executed else None,
            "status": o.status,
        }
        for o in orders if o.status == "FILLED"
    ][:20]

    total_price_ppl = sum(pos.ppl or 0 for pos in positions)
    total_dividends = sum(div_by_ticker.values())
    result = {
        "currency": account_ccy,
        "total_price_ppl": round(total_price_ppl, 2),
        # All dividends ever received, including from positions since closed.
        "total_dividends": round(total_dividends, 2),
        "total_return": round(total_price_ppl + total_dividends, 2),
        "best_performer": perf[0] if perf else None,
        "worst_performer": perf[-1] if perf else None,
        "positions": perf,
        "recent_filled_orders": filled,
        "ranked_by": rank_field,
        "dividends_truncated": truncated,
    }
    return _finish(result, notes)


def _month_index(key: str) -> int:
    year, month = key.split("-")
    return int(year) * 12 + int(month) - 1


@tool("fetch_dividend_summary", annotations=_READ)
def fetch_dividend_summary() -> dict:
    """
    Analyse the full dividend income history, broken down by ticker and by
    calendar month. Amounts are in the account currency.

    Use this to identify which holdings generate the most income and to spot
    monthly income trends. For raw dividend records with pagination control,
    use fetch_paid_out_dividends instead.

    Returns:
        dict with currency, total_dividends, dividend_count, average_monthly (over every
        month from the first to the last payment, including months with no income),
        by_ticker (sorted highest-paying first), by_month (chronological), truncated
    """
    notes: list[str] = []
    account = client.fetch_account()
    all_divs, truncated = client.fetch_all_pages(
        lambda cursor: client.fetch_dividends(cursor=cursor, limit=50),
        max_items=_MAX_DIVIDENDS,
    )
    if truncated:
        notes.append(f"Dividend history truncated to the latest {_MAX_DIVIDENDS} payments.")

    by_ticker: dict[str, float] = {}
    by_month: dict[str, float] = {}
    for d in all_divs:
        by_ticker[d.ticker] = by_ticker.get(d.ticker, 0) + (d.amount or 0)
        if d.paid_on:
            key = d.paid_on.strftime("%Y-%m")
            by_month[key] = by_month.get(key, 0) + (d.amount or 0)

    ticker_list = [
        {"ticker": t, "total": round(v, 2)}
        for t, v in sorted(by_ticker.items(), key=lambda x: x[1], reverse=True)
    ]
    month_list = [{"month": m, "total": round(v, 2)} for m, v in sorted(by_month.items())]

    total = sum(d.amount or 0 for d in all_divs)
    if by_month:
        months = sorted(by_month)
        months_span = _month_index(months[-1]) - _month_index(months[0]) + 1
    else:
        months_span = 1

    result = {
        "currency": account.currency_code,
        "total_dividends": round(total, 2),
        "dividend_count": len(all_divs),
        "average_monthly": round(total / months_span, 2),
        "by_ticker": ticker_list,
        "by_month": month_list,
        "truncated": truncated,
    }
    return _finish(result, notes)


@tool("fetch_recent_activity", annotations=_READ)
def fetch_recent_activity(limit: int = 20) -> dict:
    """
    Get a unified timeline of recent trades and account movements by merging
    order history with deposit/withdrawal transactions into a single
    chronologically sorted feed.

    Use this for a quick "what happened recently" overview. Each entry is tagged
    as either 'order' (trade) or 'transaction' (cash movement). For separate
    access, use fetch_historical_order_data or fetch_transaction_list.

    Args:
        limit: How many items to pull from each source (orders and transactions
            separately), 1-50. Defaults to 20. Total activity items may be up to 2x this.

    Returns:
        dict with currency, activity (sorted newest-first, each tagged as 'order' or
        'transaction'), order_count, transaction_count
    """
    notes: list[str] = []
    limit = min(max(limit, 1), 50)
    account = client.fetch_account()
    orders = client.fetch_order_history(limit=limit).items
    txns = client.fetch_transactions(limit=limit).items

    activity = []
    for o in orders:
        date = o.date_executed or o.date_created
        activity.append({
            "type": "order",
            "date": date.isoformat() if date else None,
            "ticker": o.ticker,
            "order_type": o.type,
            "side": o.side,
            "status": o.status,
            "quantity": o.filled_quantity or o.ordered_quantity,
            "fill_price": o.fill_price,
            "value": o.filled_value or o.ordered_value,
            "currency": o.currency,
        })

    for t in txns:
        activity.append({
            "type": "transaction",
            "date": t.date_time.isoformat() if t.date_time else None,
            "transaction_type": t.type,
            "amount": t.amount,
            "reference": t.reference,
        })

    activity.sort(key=lambda a: a.get("date") or "", reverse=True)

    result = {
        "currency": account.currency_code,
        "activity": activity,
        "order_count": len(orders),
        "transaction_count": len(txns),
    }
    return _finish(result, notes)

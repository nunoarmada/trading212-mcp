"""
Replay sanitized live T212 responses through our Pydantic models to catch
schema drift. Fixtures are captured by `tests/capture_fixtures.py` and
committed. Any T212 API change that renames a field, adds a required one,
or introduces a value our models reject will fail here.
"""
import json
from pathlib import Path

import pytest

from trading212_mcp_server.models import (
    Account, Cash, Position, Order, HistoricalOrder,
    PaginatedDividends, PaginatedTransactions,
    PieSummary, Exchange, Report, Instrument,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(f"fixture missing: {path.name} - run tests/capture_fixtures.py")
    return json.loads(path.read_text())


def test_account_info_shape():
    Account.model_validate(_load("account_info"))


def test_cash_shape():
    Cash.model_validate(_load("cash"))


def test_positions_shape():
    for item in _load("positions"):
        Position.model_validate(item)


def test_orders_shape():
    for item in _load("orders"):
        Order.model_validate(item)


def test_order_history_shape():
    from trading212_mcp_server.api.client import T212Client  # noqa: F401
    # The client flattens nested order/fill/walletImpact into HistoricalOrder.
    # Reproduce that flatten here so the fixture (raw T212 shape) still validates.
    raw = _load("order_history")
    items = raw["items"] if isinstance(raw, dict) else raw
    for item in items:
        order = item.get("order", {})
        fill = item.get("fill", {})
        wallet = fill.get("walletImpact", {})
        flat = {
            "id": order.get("id"),
            "ticker": order.get("ticker"),
            "type": order.get("type"),
            "status": order.get("status"),
            "executor": order.get("initiatedFrom"),
            "filledValue": order.get("filledValue"),
            "orderedValue": order.get("value"),
            "dateCreated": order.get("createdAt"),
            "limitPrice": order.get("limitPrice"),
            "stopPrice": order.get("stopPrice"),
            "timeValidity": order.get("timeValidity"),
            "parentOrder": order.get("parentOrder"),
            "filledQuantity": fill.get("quantity"),
            "fillPrice": fill.get("price"),
            "fillId": fill.get("id"),
            "fillType": fill.get("type"),
            "dateExecuted": fill.get("filledAt"),
            "taxes": wallet.get("taxes"),
        }
        HistoricalOrder.model_validate(flat)


def test_dividends_shape():
    PaginatedDividends.model_validate(_load("dividends"))


def test_transactions_shape():
    PaginatedTransactions.model_validate(_load("transactions"))


def test_pies_shape():
    for item in _load("pies"):
        PieSummary.model_validate(item)


def test_exchanges_shape():
    for item in _load("exchanges"):
        Exchange.model_validate(item)


def test_reports_shape():
    for item in _load("reports"):
        Report.model_validate(item)


def test_instruments_shape():
    for item in _load("instruments"):
        Instrument.model_validate(item)

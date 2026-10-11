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
    Account, Cash, Position, Order,
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
    from trading212_mcp_server.api.client import T212Client
    raw = _load("order_history")
    items = raw["items"] if isinstance(raw, dict) else raw
    for item in items:
        T212Client._flatten_historical_order(item)


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

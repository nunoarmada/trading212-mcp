"""
Capture live T212 API responses to tests/fixtures/*.json.
Run when T212 changes their API shape, then commit the fixtures.
Test suite replays these to catch model drift without hitting live API.

Fixtures are SANITIZED before being written:
  - Monetary amounts, quantities, prices, P/L, IDs zeroed
  - Datetimes replaced with a fixed reference
  - Enum-like strings, tickers, field names, and data types preserved
  - Anything else considered "shape data" is preserved

This keeps public-repo safety intact while retaining every signal needed to
catch new enum values or renamed fields.

Usage: uv run python tests/capture_fixtures.py
Requires: TRADING212_API_KEY, TRADING212_API_SECRET in env or .env
"""
import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES.mkdir(exist_ok=True)

# Field-name patterns whose values carry money/quantity/PII - zero them out.
NUMERIC_ZEROS = {
    "quantity", "amount", "price", "value", "cash", "invested", "total", "ppl",
    "result", "blocked", "free", "gained", "reinvested", "goal",
    "progress", "coef", "share",
}
# Fields we want to fully drop identity from.
ID_FIELDS = {"id", "orderId", "reportId", "fillId", "parentOrder", "pieId", "reference"}
# Fields that look like dates get flattened to a stable value.
DATE_HINTS = ("date", "time", "created", "modified", "executed", "charged", "paidOn", "addedOn")
STABLE_DATE = "2026-01-01T00:00:00.000Z"


def _looks_like_date(key: str) -> bool:
    kl = key.lower()
    return any(h.lower() in kl for h in DATE_HINTS)


def _looks_like_amount(key: str) -> bool:
    kl = key.lower()
    return any(tok in kl for tok in NUMERIC_ZEROS)


def sanitize(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        return {k: sanitize(v, key=k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(item) for item in obj]
    if isinstance(obj, str):
        # Preserve enum-like strings and tickers untouched (they carry shape info).
        # Replace dates with a stable placeholder.
        if _looks_like_date(key):
            return STABLE_DATE
        return obj
    if isinstance(obj, (int, float)):
        if key in ID_FIELDS or key.endswith("Id") or key.endswith("_id"):
            return 0
        if _looks_like_amount(key):
            return 0.0 if isinstance(obj, float) else 0
        # Neutral scalars we don't recognize - keep for shape but zero out to be safe.
        return 0.0 if isinstance(obj, float) else 0
    return obj


key = os.getenv("TRADING212_API_KEY") or os.getenv("T212_API_KEY")
secret = os.getenv("TRADING212_API_SECRET") or os.getenv("T212_API_SECRET")
env = os.getenv("ENVIRONMENT") or "live"
creds = base64.b64encode(f"{key}:{secret}".encode()).decode()
base = f"https://{env}.trading212.com/api/v0"
headers = {"Authorization": f"Basic {creds}"}

ENDPOINTS = {
    "account_info": ("/equity/account/info", {}),
    "cash": ("/equity/account/cash", {}),
    "positions": ("/equity/portfolio", {}),
    "orders": ("/equity/orders", {}),
    "order_history": ("/equity/history/orders", {"limit": 20}),
    "dividends": ("/history/dividends", {"limit": 20}),
    "transactions": ("/history/transactions", {"limit": 20}),
    "pies": ("/equity/pies", {}),
    "exchanges": ("/equity/metadata/exchanges", {}),
    "reports": ("/history/exports", {}),
    "instruments": ("/equity/metadata/instruments", {}),
}

# Endpoints whose responses are huge - keep only the first N items.
TRIM_TO = {"instruments": 20}


def main() -> None:
    for name, (path, params) in ENDPOINTS.items():
        r = httpx.get(f"{base}{path}", headers=headers, params=params, timeout=60)
        if r.status_code != 200:
            print(f"SKIP {name}: HTTP {r.status_code}")
            continue
        raw = r.json()
        if name in TRIM_TO and isinstance(raw, list):
            raw = raw[:TRIM_TO[name]]
        cleaned = sanitize(raw)
        out = FIXTURES / f"{name}.json"
        out.write_text(json.dumps(cleaned, indent=2))
        count = len(raw) if isinstance(raw, list) else (
            len(raw.get("items", [])) if isinstance(raw, dict) and "items" in raw else 1
        )
        print(f"OK  {name}: {count} items -> {out.relative_to(Path(__file__).parent.parent)}")


if __name__ == "__main__":
    main()

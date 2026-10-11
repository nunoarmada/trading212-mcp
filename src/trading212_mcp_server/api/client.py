import base64
import logging
import os
import threading
import time
from enum import Enum
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from trading212_mcp_server.models import (
    Account, Cash, Position, Order, Exchange, Instrument,
    PieSummary, PieDetails, PaginatedDividends, PaginatedTransactions,
    PaginatedHistoricalOrders, HistoricalOrder, Report, EnqueuedReport,
    PieRequest, DuplicatePieRequest, ReportDataIncluded,
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest, StopLimitOrderRequest,
)

log = logging.getLogger("trading212")

_MAX_RETRIES = 3
_MAX_PAGE_SIZE = 50
# Instrument and exchange metadata changes rarely and its endpoints are heavily
# rate limited, so it is cached in memory for this long.
_METADATA_TTL = 3600


class T212Error(Exception):
    """Base error for everything the Trading 212 client raises."""


class T212ConfigError(T212Error):
    """Missing or invalid server configuration."""


class T212AuthError(T212Error):
    """Credentials rejected (401) or missing permission scope (403)."""


class T212RateLimitError(T212Error):
    """Still rate limited after all retries."""


class T212APIError(T212Error):
    """Any other non-success response from Trading 212."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Trading 212 API error {status}: {message}")
        self.status = status


class _Environment(str, Enum):
    DEMO = "demo"
    LIVE = "live"


def _env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return 20
    return min(_MAX_PAGE_SIZE, max(1, limit))


def _error_detail(response: httpx.Response) -> str:
    """Pull a short, human-readable message out of an error response."""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else response.reason_phrase
    if isinstance(body, dict):
        for key in ("message", "detail", "error", "code", "type"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


def cursor_from_next_page_path(next_page_path: Optional[str]) -> Optional[str]:
    """Extract the `cursor` query parameter from a T212 nextPagePath."""
    if not next_page_path:
        return None
    values = parse_qs(urlsplit(next_page_path).query).get("cursor")
    return values[0] if values else None


class T212Client:
    def __init__(self, api_key: str = None, api_secret: str = None,
                 environment: str = None, version: str = "v0",
                 transport: httpx.BaseTransport = None):
        api_key = api_key or _env("TRADING212_API_KEY", "T212_API_KEY")
        api_secret = api_secret or _env("TRADING212_API_SECRET", "T212_API_SECRET")
        environment = (environment or _env("T212_ENV", "ENVIRONMENT") or "demo").strip().lower()

        # Config problems are reported on the first API call rather than at import,
        # so the MCP server still starts and the client sees a readable error.
        errors = []
        if not api_key:
            errors.append(
                "TRADING212_API_KEY is not set. Add your Trading 212 API key "
                "(and TRADING212_API_SECRET) to the server environment."
            )
        if environment not in {e.value for e in _Environment}:
            errors.append(
                f"Invalid environment {environment!r}: set T212_ENV (or ENVIRONMENT) "
                "to 'demo' or 'live'."
            )
            environment = _Environment.DEMO.value
        self._config_error: Optional[str] = " ".join(errors) or None
        if self._config_error:
            log.warning(self._config_error)

        self.environment = environment
        self.api_prefix = f"/api/{version}"
        base_url = f"https://{environment}.trading212.com{self.api_prefix}"

        headers = {"Content-Type": "application/json"}
        if api_key and api_secret:
            encoded = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        elif api_key:
            # Legacy single-key auth.
            headers["Authorization"] = api_key

        self.client = httpx.Client(base_url=base_url, headers=headers,
                                   timeout=30, transport=transport)
        self._lock = threading.Lock()
        self._rate_limits: dict[str, dict] = {}
        self._metadata_cache: dict[str, tuple[float, Any]] = {}
        self._metadata_load_lock = threading.Lock()
        # Each tool call runs in its own worker thread, so per-thread bookkeeping
        # keeps one call's rate-limit waits from leaking into another's response.
        self._local = threading.local()

    # --- Rate limiting ---

    def _add_wait(self, seconds: float) -> None:
        self._local.total_wait = getattr(self._local, "total_wait", 0.0) + seconds

    def reset_wait_time(self) -> None:
        self._local.total_wait = 0.0

    def drain_wait_time(self) -> float:
        elapsed = getattr(self._local, "total_wait", 0.0)
        self._local.total_wait = 0.0
        return round(elapsed, 1)

    def _sleep(self, seconds: float) -> None:
        self._add_wait(seconds)
        time.sleep(seconds)

    @staticmethod
    def _endpoint(url: str) -> str:
        return url.split("?")[0]

    def _wait_for_rate_limit(self, url: str) -> None:
        with self._lock:
            info = self._rate_limits.get(self._endpoint(url))
        if not info or info["remaining"] > 0:
            return
        wait = info["reset"] - time.time()
        if wait > 0:
            sleep_for = min(wait + 0.5, 60)
            log.info("Rate limit reached for %s, waiting %.1fs", self._endpoint(url), sleep_for)
            self._sleep(sleep_for)

    def _update_rate_limit(self, url: str, headers: httpx.Headers) -> None:
        if "x-ratelimit-remaining" not in headers:
            return
        try:
            info = {
                "remaining": int(headers["x-ratelimit-remaining"]),
                "reset": int(headers.get("x-ratelimit-reset", 0)),
            }
        except (ValueError, TypeError):
            log.debug("Ignoring malformed rate-limit headers for %s", url)
            return
        with self._lock:
            self._rate_limits[self._endpoint(url)] = info

    def _retry_delay(self, response: httpx.Response) -> float:
        try:
            reset = int(response.headers.get("x-ratelimit-reset", 0))
        except ValueError:
            reset = 0
        return min(max(reset - time.time(), 1) + 0.5, 60)

    # --- Core request ---

    def _request(self, method: str, url: str, **kwargs) -> Any:
        if self._config_error:
            raise T212ConfigError(self._config_error)

        for attempt in range(1, _MAX_RETRIES + 1):
            self._wait_for_rate_limit(url)
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TimeoutException as e:
                # Never retried: an order request may already have been executed.
                raise T212Error("Trading 212 API request timed out. Try again.") from e
            except httpx.TransportError as e:
                raise T212Error(
                    "Cannot connect to Trading 212. Check your internet connection."
                ) from e

            self._update_rate_limit(url, response.headers)
            status = response.status_code

            if status == 429:
                if attempt < _MAX_RETRIES:
                    sleep_for = self._retry_delay(response)
                    log.info("429 on %s, retrying in %.1fs (attempt %d/%d)",
                             url, sleep_for, attempt, _MAX_RETRIES)
                    self._sleep(sleep_for)
                    continue
                raise T212RateLimitError(
                    f"Rate limited by Trading 212 after {_MAX_RETRIES} attempts. "
                    "Wait a minute and try again."
                )
            if status == 401:
                raise T212AuthError(
                    "Authentication failed. Check TRADING212_API_KEY and "
                    "TRADING212_API_SECRET, and that they belong to the "
                    f"'{self.environment}' environment."
                )
            if status == 403:
                raise T212AuthError(
                    "Access denied: this API key lacks the permission scope needed for "
                    f"{method} {self._endpoint(url)}. Enable it in Trading 212 under "
                    "Settings > API."
                )
            if status == 404:
                raise T212APIError(404, f"Not found: {self._endpoint(url)} "
                                        f"({_error_detail(response)})")
            if status >= 400:
                raise T212APIError(status, _error_detail(response))

            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as e:
                raise T212Error(
                    f"Trading 212 returned a non-JSON response for {self._endpoint(url)}."
                ) from e

        raise AssertionError("unreachable")

    def fetch_all_pages(self, fetch_page, max_items: int) -> tuple[list, bool]:
        """Follow cursors via `fetch_page(cursor)` until exhausted or `max_items`
        is reached. Returns (items, truncated)."""
        items: list = []
        cursor = None
        seen: set[str] = set()
        while True:
            page = fetch_page(cursor)
            items.extend(page.items)
            cursor = cursor_from_next_page_path(page.next_page_path)
            if not cursor:
                return items[:max_items], len(items) > max_items
            if cursor in seen:
                log.warning("Pagination cursor %s repeated; stopping", cursor)
                return items[:max_items], len(items) > max_items
            seen.add(cursor)
            if len(items) >= max_items:
                return items[:max_items], True

    def _cached(self, key: str, loader):
        def fresh():
            hit = self._metadata_cache.get(key)
            return hit and time.monotonic() - hit[0] < _METADATA_TTL and hit

        with self._lock:
            hit = fresh()
        if hit:
            return hit[1]
        # Single-flight: concurrent misses wait for one load instead of each
        # hitting the heavily rate-limited metadata endpoints.
        with self._metadata_load_lock:
            with self._lock:
                hit = fresh()
            if hit:
                return hit[1]
            value = loader()
            with self._lock:
                self._metadata_cache[key] = (time.monotonic(), value)
            return value

    # --- Account ---

    def fetch_account(self) -> Account:
        data = self._request("GET", "/equity/account/info")
        return Account.model_validate(data)

    def fetch_cash(self) -> Cash:
        data = self._request("GET", "/equity/account/cash")
        return Cash.model_validate(data)

    # --- Positions ---

    def fetch_positions(self) -> list[Position]:
        data = self._request("GET", "/equity/portfolio")
        return [Position.model_validate(pos) for pos in data]

    def search_position_by_ticker(self, ticker: str) -> Position:
        data = self._request("POST", "/equity/portfolio/ticker",
                             json={"ticker": ticker})
        return Position.model_validate(data)

    # --- Dividends ---

    def fetch_dividends(self, cursor: Optional[str] = None,
                        ticker: Optional[str] = None,
                        limit: int = 20) -> PaginatedDividends:
        params = {"limit": _clamp_limit(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        data = self._request("GET", "/history/dividends", params=params)
        page = PaginatedDividends.model_validate(data)
        page.next_cursor = cursor_from_next_page_path(page.next_page_path)
        return page

    # --- Orders ---

    def fetch_orders(self) -> list[Order]:
        data = self._request("GET", "/equity/orders")
        return [Order.model_validate(order) for order in data]

    def fetch_order(self, order_id: int) -> Order:
        data = self._request("GET", f"/equity/orders/{order_id}")
        return Order.model_validate(data)

    def place_market_order(self, order_data: MarketOrderRequest) -> Order:
        data = self._request("POST", "/equity/orders/market",
                             json=order_data.model_dump(mode="json", by_alias=True))
        return Order.model_validate(data)

    def place_limit_order(self, order_data: LimitOrderRequest) -> Order:
        data = self._request("POST", "/equity/orders/limit",
                             json=order_data.model_dump(mode="json", by_alias=True))
        return Order.model_validate(data)

    def place_stop_order(self, order_data: StopOrderRequest) -> Order:
        data = self._request("POST", "/equity/orders/stop",
                             json=order_data.model_dump(mode="json", by_alias=True))
        return Order.model_validate(data)

    def place_stop_limit_order(self, order_data: StopLimitOrderRequest) -> Order:
        data = self._request("POST", "/equity/orders/stop_limit",
                             json=order_data.model_dump(mode="json", by_alias=True))
        return Order.model_validate(data)

    def cancel_order(self, order_id: int) -> None:
        self._request("DELETE", f"/equity/orders/{order_id}")

    # --- Pies ---

    def fetch_pies(self) -> list[PieSummary]:
        data = self._request("GET", "/equity/pies")
        return [PieSummary.model_validate(pie) for pie in data]

    def fetch_pie(self, pie_id: int) -> PieDetails:
        data = self._request("GET", f"/equity/pies/{pie_id}")
        return PieDetails.model_validate(data)

    def create_pie(self, pie_data: PieRequest) -> PieDetails:
        data = self._request("POST", "/equity/pies",
                             json=pie_data.model_dump(mode="json", by_alias=True))
        return PieDetails.model_validate(data)

    def update_pie(self, pie_id: int, pie_data: PieRequest) -> PieDetails:
        payload = {k: v for k, v in pie_data.model_dump(mode="json", by_alias=True).items()
                   if v is not None}
        data = self._request("POST", f"/equity/pies/{pie_id}", json=payload)
        return PieDetails.model_validate(data)

    def duplicate_pie(self, pie_id: int, req: DuplicatePieRequest) -> PieDetails:
        data = self._request("POST", f"/equity/pies/{pie_id}/duplicate",
                             json=req.model_dump(mode="json", by_alias=True))
        return PieDetails.model_validate(data)

    def delete_pie(self, pie_id: int):
        return self._request("DELETE", f"/equity/pies/{pie_id}")

    # --- History ---

    @staticmethod
    def _flatten_historical_order(item: dict) -> HistoricalOrder:
        """T212 nests order, fill and walletImpact; flatten into HistoricalOrder."""
        order = item.get("order") or {}
        fill = item.get("fill") or {}
        wallet = fill.get("walletImpact") or {}
        flat = {
            "id": order.get("id"),
            "ticker": order.get("ticker"),
            "type": order.get("type"),
            "status": order.get("status"),
            "side": order.get("side"),
            "executor": order.get("initiatedFrom"),
            "currency": order.get("currency"),
            "filledValue": order.get("filledValue"),
            "orderedValue": order.get("value"),
            "orderedQuantity": order.get("quantity"),
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
            "walletCurrency": wallet.get("currency"),
            "walletNetValue": wallet.get("netValue"),
            "walletFxRate": wallet.get("fxRate"),
            "taxes": wallet.get("taxes"),
        }
        return HistoricalOrder.model_validate(flat)

    def fetch_order_history(self, cursor: Optional[str] = None,
                            ticker: Optional[str] = None,
                            limit: int = 20) -> PaginatedHistoricalOrders:
        params = {"limit": _clamp_limit(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        if ticker is not None:
            params["ticker"] = ticker
        data = self._request("GET", "/equity/history/orders", params=params)
        next_page_path = data.get("nextPagePath")
        return PaginatedHistoricalOrders(
            items=[self._flatten_historical_order(item) for item in data.get("items", [])],
            next_page_path=next_page_path,
            next_cursor=cursor_from_next_page_path(next_page_path),
        )

    def fetch_transactions(self, cursor: Optional[str] = None,
                           time: Optional[str] = None,
                           limit: int = 20) -> PaginatedTransactions:
        params = {"limit": _clamp_limit(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        if time is not None:
            params["time"] = time
        data = self._request("GET", "/history/transactions", params=params)
        page = PaginatedTransactions.model_validate(data)
        page.next_cursor = cursor_from_next_page_path(page.next_page_path)
        return page

    # --- Market Data ---

    def fetch_instruments(self) -> list[Instrument]:
        def load():
            data = self._request("GET", "/equity/metadata/instruments")
            return [Instrument.model_validate(inst) for inst in data]
        return self._cached("instruments", load)

    def fetch_exchanges(self) -> list[Exchange]:
        def load():
            data = self._request("GET", "/equity/metadata/exchanges")
            return [Exchange.model_validate(exch) for exch in data]
        return self._cached("exchanges", load)

    # --- Reports ---

    def fetch_reports(self) -> list[Report]:
        data = self._request("GET", "/history/exports")
        return [Report.model_validate(report) for report in data]

    def request_export(self, data_included: ReportDataIncluded = None,
                       time_from: str = None,
                       time_to: str = None) -> EnqueuedReport:
        payload = {}
        data_included = data_included or ReportDataIncluded()
        payload["dataIncluded"] = data_included.model_dump(mode="json", by_alias=True)
        if time_from:
            payload["timeFrom"] = time_from
        if time_to:
            payload["timeTo"] = time_to
        data = self._request("POST", "/history/exports", json=payload)
        return EnqueuedReport.model_validate(data)

import json

import httpx
import pytest

from trading212_mcp_server.api.client import T212Client


class FakeT212:
    """Routes requests to canned responses keyed by (method, path)."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[httpx.Request] = []

    def add(self, method: str, path: str, *responses):
        """Queue responses (dict/list -> 200 JSON, or an httpx.Response)."""
        self.routes.setdefault((method, path), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json={"message": "no fake route"})
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            item = item(request)
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, content=json.dumps(item),
                              headers={"content-type": "application/json"})


@pytest.fixture
def fake():
    return FakeT212()


@pytest.fixture
def make_client(fake, monkeypatch):
    # Rate-limit waits must not slow the suite down.
    monkeypatch.setattr("trading212_mcp_server.api.client.time.sleep", lambda s: None)

    def _make(**kwargs):
        kwargs.setdefault("api_key", "key")
        kwargs.setdefault("api_secret", "secret")
        kwargs.setdefault("environment", "demo")
        return T212Client(transport=httpx.MockTransport(fake.handler), **kwargs)

    return _make


@pytest.fixture
def client(make_client):
    return make_client()

import json
import os
import subprocess
import sys

import pytest

_LIST_TOOLS = (
    "import asyncio, json; from trading212_mcp_server.server import mcp; "
    "tools = asyncio.run(mcp.list_tools()); "
    "print(json.dumps({t.name: bool(t.annotations and t.annotations.readOnlyHint) "
    "for t in tools}))"
)


def _list_tools(**env) -> dict[str, bool]:
    full_env = {**os.environ, "TRADING212_API_KEY": "k", **env}
    out = subprocess.run([sys.executable, "-c", _LIST_TOOLS], env=full_env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_all_tools_registered_by_default():
    tools = _list_tools(T212_READ_ONLY="")
    assert "place_market_order" in tools and "delete_pie" in tools
    assert "fetch_portfolio_summary" in tools


@pytest.mark.parametrize("value", ["true", "1", "YES"])
def test_read_only_mode_drops_write_tools(value):
    tools = _list_tools(T212_READ_ONLY=value)
    assert tools, "read tools must remain"
    assert all(tools.values()), f"write tools still registered: {tools}"
    assert "place_market_order" not in tools
    assert "fetch_portfolio_summary" in tools


def test_tools_run_off_the_event_loop():
    import inspect
    from trading212_mcp_server.server import mcp
    for t in mcp._tool_manager.list_tools():
        assert t.is_async and inspect.iscoroutinefunction(t.fn), t.name


def test_tool_call_through_fastmcp(client, fake, monkeypatch):
    import anyio
    from trading212_mcp_server.server import mcp
    from trading212_mcp_server.tools import market

    monkeypatch.setattr(market, "client", client)
    fake.add("GET", "/api/v0/equity/metadata/instruments", [
        {"ticker": "AAPL_US_EQ", "name": "Apple Inc", "currencyCode": "USD"},
        {"ticker": "MSFT_US_EQ", "name": "Microsoft", "currencyCode": "USD"},
    ])
    content = anyio.run(mcp.call_tool, "search_instrument", {"search_term": "apple"})
    assert len(content) == 1 and "AAPL_US_EQ" in content[0].text


def test_stale_wait_time_does_not_leak_into_next_call(monkeypatch):
    import anyio
    from trading212_mcp_server import app

    calls = []

    @app.tool("probe_wait", annotations=app.ToolAnnotations(readOnlyHint=True))
    def probe():
        calls.append(app.client.drain_wait_time())
        app.client._add_wait(9)  # simulate a wait that is never drained
        return "ok"

    try:
        anyio.run(app.mcp.call_tool, "probe_wait", {})
        anyio.run(app.mcp.call_tool, "probe_wait", {})
    finally:
        app.mcp._tool_manager._tools.pop("probe_wait", None)
    assert calls == [0, 0]

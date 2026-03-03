import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import APP_NAME, APP_VERSION, RequestHandler, TOOL_DEFINITIONS, run_self_test


def _start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get_json(url, headers=None):
    req = Request(url, method="GET", headers=headers or {})
    with urlopen(req) as resp:
        return resp.status, resp.headers, json.loads(resp.read().decode("utf-8"))


def _post_json(url, body):
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else None


def test_required_routes_and_mcp_manifest_and_sse():
    server, base = _start_server()
    try:
        for route in [
            "/health",
            "/privacy",
            "/terms",
            "/support",
            "/.well-known/openai-apps-challenge",
        ]:
            status, _, _ = _get_json(base + route)
            assert status == 200

        status, _, mcp = _get_json(base + "/mcp")
        assert status == 200
        assert mcp["name"] == APP_NAME
        assert mcp["version"] == APP_VERSION
        assert mcp["tools"] == TOOL_DEFINITIONS
        assert mcp["base_url"].startswith("http://")

        req = Request(base + "/mcp", method="GET", headers={"Accept": "text/event-stream"})
        with urlopen(req) as resp:
            assert resp.status == 200
            assert "text/event-stream" in resp.headers.get("Content-Type", "")
            payload = resp.read().decode("utf-8")
            assert "event: message" in payload
            assert APP_NAME in payload
    finally:
        server.shutdown()


def test_jsonrpc_initialize_tools_list_and_notifications_initialized():
    server, base = _start_server()
    try:
        status, init_resp = _post_json(
            base + "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert status == 200
        assert init_resp["result"]["name"] == APP_NAME

        status, list_resp = _post_json(
            base + "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert status == 200
        assert list_resp["result"]["tools"] == TOOL_DEFINITIONS

        _, _, mcp = _get_json(base + "/mcp")
        assert mcp["tools"] == list_resp["result"]["tools"]

        req = Request(
            base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req) as resp:
            assert resp.status == 204
            assert resp.read() == b""
    finally:
        server.shutdown()


def test_tools_call_and_invalid_params():
    server, base = _start_server()
    try:
        cases = [
            ("text_normalize", {"text": "  HeLLo    WORLD  "}, {"text": "hello world"}),
            ("text_word_count", {"text": "a b\nc"}, {"chars": 5, "words": 3, "lines": 2}),
            ("slugify", {"text": "Hello, World!!!"}, {"slug": "hello-world"}),
            ("truncate", {"text": "abcdef", "max_length": 3}, {"text": "abc"}),
            ("text_replace", {"text": "foo bar foo", "old": "foo", "new": "baz"}, {"text": "baz bar baz"}),
        ]

        for idx, (name, arguments, expected_data) in enumerate(cases, start=1):
            status, resp = _post_json(
                base + "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert status == 200
            result = resp["result"]
            assert result == {"success": True, "errors": [], "data": expected_data}

        status, resp = _post_json(
            base + "/mcp",
            {"jsonrpc": "2.0", "id": 77, "method": "tools/call", "params": {"arguments": {}}},
        )
        assert status == 400
        assert resp["error"]["code"] == -32602
    finally:
        server.shutdown()


def test_self_test_gate_passes():
    assert run_self_test() is True

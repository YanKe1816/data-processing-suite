import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_PROTOCOL_VERSION,
    RequestHandler,
    TOOL_DEFINITIONS,
    run_self_test,
)


def _start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _request_json(url, method="GET", body=None, headers=None):
    req_headers = headers or {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers = {**req_headers, "Content-Type": "application/json"}
    req = Request(url, data=data, method=method, headers=req_headers)
    try:
        with urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return resp.status, resp.headers, parsed
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        parsed = json.loads(raw) if raw else None
        return exc.code, exc.headers, parsed


class ServerTests(unittest.TestCase):
    def test_endpoints_and_handshake_flow(self):
        server, base = _start_server()
        try:
            status, _, health = _request_json(base + "/health")
            self.assertEqual(status, 200)
            self.assertEqual(health, {"status": "ok"})

            for route in ["/privacy", "/terms", "/support"]:
                req = Request(base + route, method="GET")
                with urlopen(req) as resp:
                    text = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertTrue(len(text) > 0)
            req = Request(base + "/support", method="GET")
            with urlopen(req) as resp:
                self.assertIn("@", resp.read().decode("utf-8"))

            status, _, manifest = _request_json(base + "/mcp")
            self.assertEqual(status, 200)
            self.assertEqual(manifest["name"], APP_NAME)
            self.assertEqual(manifest["version"], APP_VERSION)
            self.assertEqual(manifest["tools"], TOOL_DEFINITIONS)

            # tools/list before initialized should fail
            status, _, body = _request_json(
                base + "/mcp",
                method="POST",
                body={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
            )
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], -32000)
            self.assertEqual(body["error"]["message"], "Server not initialized")

            status, _, init = _request_json(
                base + "/mcp",
                method="POST",
                body={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "unit", "version": "1"},
                    },
                },
            )
            self.assertEqual(status, 200)
            result = init["result"]
            self.assertIn("protocolVersion", result)
            self.assertIn("capabilities", result)
            self.assertIn("serverInfo", result)

            status, _, unsupported = _request_json(
                base + "/mcp",
                method="POST",
                body={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2099-01-01",
                        "capabilities": {},
                        "clientInfo": {"name": "unit", "version": "1"},
                    },
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(unsupported["error"]["code"], -32602)

            status, _, _ = _request_json(
                base + "/mcp",
                method="POST",
                body={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            self.assertEqual(status, 204)

            status, _, listed = _request_json(
                base + "/mcp",
                method="POST",
                body={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(listed["result"]["tools"], TOOL_DEFINITIONS)

            status, _, called = _request_json(
                base + "/mcp",
                method="POST",
                body={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "text_normalize", "arguments": {"text": "  HeLLo    WORLD  "}},
                },
            )
            self.assertEqual(status, 200)
            self.assertIn("content", called["result"])
            self.assertEqual(called["result"]["structuredContent"], {"text": "hello world"})

            req = Request(base + "/mcp", method="GET", headers={"Accept": "text/event-stream"})
            with urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
        finally:
            server.shutdown()
            server.server_close()

    def test_run_self_test(self):
        self.assertTrue(run_self_test())


if __name__ == "__main__":
    unittest.main()

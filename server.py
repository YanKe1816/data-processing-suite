import json
import os
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

APP_NAME = "data-processing-suite"
APP_VERSION = "1.0.0"

TOOL_DEFINITIONS = [
    {
        "name": "text_normalize",
        "description": "Normalize text by trimming, lowercasing, and collapsing whitespace.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        },
    },
    {
        "name": "text_word_count",
        "description": "Count characters, words, and lines in text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        },
    },
    {
        "name": "slugify",
        "description": "Convert text to a URL-safe slug.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        },
    },
    {
        "name": "truncate",
        "description": "Truncate text to max_length characters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_length": {"type": "integer", "minimum": 0},
            },
            "required": ["text", "max_length"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        },
    },
    {
        "name": "text_replace",
        "description": "Replace all occurrences of a substring.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["text", "old", "new"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        },
    },
]


@dataclass
class Response:
    status: int
    body: Dict[str, Any] | None = None


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _word_count(text: str) -> Dict[str, int]:
    return {
        "chars": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()) if text else 0,
    }


def _slugify(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[^a-z0-9\s-]", "", lowered)
    lowered = re.sub(r"[\s_-]+", "-", lowered)
    return lowered.strip("-")


def _truncate(text: str, max_length: int) -> str:
    return text[:max_length]


def _replace(text: str, old: str, new: str) -> str:
    return text.replace(old, new)


def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str | None]:
    try:
        if name == "text_normalize":
            return True, {"text": _normalize(str(arguments["text"]))}, None
        if name == "text_word_count":
            return True, _word_count(str(arguments["text"])), None
        if name == "slugify":
            return True, {"slug": _slugify(str(arguments["text"]))}, None
        if name == "truncate":
            return True, {"text": _truncate(str(arguments["text"]), int(arguments["max_length"]))}, None
        if name == "text_replace":
            return True, {"text": _replace(str(arguments["text"]), str(arguments["old"]), str(arguments["new"]))}, None
        return False, {}, f"Unknown tool '{name}'"
    except KeyError as exc:
        return False, {}, f"Missing required argument: {exc.args[0]}"
    except (TypeError, ValueError):
        return False, {}, "Tool arguments are invalid"


def _tool_summary(name: str, output: Dict[str, Any]) -> str:
    if name == "text_normalize":
        return "Text normalized."
    if name == "text_word_count":
        return f"Counted {output.get('words', 0)} words."
    if name == "slugify":
        return "Slug generated."
    if name == "truncate":
        return "Text truncated."
    if name == "text_replace":
        return "Text replaced."
    return "Tool executed."


def _not_initialized_error(req_id: Any) -> Tuple[int, Dict[str, Any]]:
    return HTTPStatus.OK, {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32000, "message": "Server not initialized"},
    }


def handle_jsonrpc(payload: Dict[str, Any], server: "MCPServer") -> Tuple[int, Dict[str, Any] | None]:
    if payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return HTTPStatus.BAD_REQUEST, {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    req_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params", {})

    if method == "initialize":
        negotiated_version = params.get("protocolVersion") or "2024-11-05"
        result = {
            "protocolVersion": negotiated_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": APP_NAME, "version": APP_VERSION},
        }
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "notifications/initialized":
        server.initialized = True
        if req_id is None:
            return HTTPStatus.NO_CONTENT, None
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": {"acknowledged": True}}

    if method == "tools/list":
        if not server.initialized:
            return _not_initialized_error(req_id)
        return HTTPStatus.OK, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        if not server.initialized:
            return _not_initialized_error(req_id)
        name = str(params.get("name"))
        arguments = params.get("arguments", {})
        ok, tool_output, error_message = handle_tool_call(name, arguments)
        if not ok:
            return HTTPStatus.OK, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": error_message or "Invalid params"},
            }

        return HTTPStatus.OK, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": _tool_summary(name, tool_output)}],
                "structuredContent": tool_output,
            },
        }

    return HTTPStatus.BAD_REQUEST, {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


class MCPServer(ThreadingHTTPServer):
    def __init__(self, server_address: Tuple[str, int], handler_cls: type[BaseHTTPRequestHandler]):
        super().__init__(server_address, handler_cls)
        self.initialized = False


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DataProcessingSuite/1.0"

    def _send_json(self, status: int, data: Dict[str, Any] | None) -> None:
        self.send_response(status)
        if data is None:
            self.end_headers()
            return
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, status: int, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_sse_event(self, event: str, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, separators=(",", ":"))
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()

    def _handle_mcp_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        host = self.headers.get("Host", "localhost:8000")
        self._send_sse_event(
            "endpoint",
            {
                "path": "/mcp",
                "url": f"http://{host}/mcp",
                "protocol": "jsonrpc",
            },
        )

        while True:
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(15)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/privacy":
            self._send_json(HTTPStatus.OK, {"policy": "No user data is stored or shared."})
            return
        if path == "/terms":
            self._send_json(HTTPStatus.OK, {"terms": "Use as-is. No warranties."})
            return
        if path == "/support":
            self._send_json(HTTPStatus.OK, {"support": "Open an issue in the repository for support.", "email": "support@example.com"})
            return
        if path == "/.well-known/openai-apps-challenge":
            challenge = os.environ.get("OPENAI_APPS_CHALLENGE", "PLACEHOLDER")
            self._send_text(HTTPStatus.OK, challenge)
            return
        if path == "/mcp":
            if "text/event-stream" in self.headers.get("Accept", ""):
                self._handle_mcp_sse()
                return
            host = self.headers.get("Host", "localhost:8000")
            manifest = {
                "name": APP_NAME,
                "version": APP_VERSION,
                "base_url": f"http://{host}",
                "tools": TOOL_DEFINITIONS,
            }
            self._send_json(HTTPStatus.OK, manifest)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/mcp":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            return

        mcp_server = cast(MCPServer, self.server)
        status, response = handle_jsonrpc(payload, mcp_server)
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = MCPServer((host, port), RequestHandler)
    server.serve_forever()


def run_self_test() -> None:
    test_server = MCPServer(("127.0.0.1", 0), RequestHandler)
    host, port = test_server.server_address
    thread = threading.Thread(target=test_server.serve_forever, daemon=True)
    thread.start()

    base = f"http://{host}:{port}"

    def get_json(path: str) -> Dict[str, Any]:
        with urlopen(f"{base}{path}") as response:
            return json.loads(response.read().decode("utf-8"))

    def rpc(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any] | None]:
        req = Request(
            f"{base}/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as response:
            body = response.read().decode("utf-8")
            return response.getcode(), json.loads(body) if body else None

    try:
        health = get_json("/health")
        assert health == {"status": "ok"}, "health endpoint did not return expected payload"

        _, initialize = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert initialize and "protocolVersion" in initialize.get("result", {}), "initialize missing protocolVersion"

        _, pre_init_list = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert pre_init_list and pre_init_list.get("error", {}).get("code") == -32000, "tools/list should fail before initialized"

        rpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        _, post_init_list = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        assert post_init_list and "tools" in post_init_list.get("result", {}), "tools/list should succeed after initialized"

        _, tool_call = rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "text_normalize", "arguments": {"text": "  Hello   WORLD "}},
            }
        )
        result = (tool_call or {}).get("result", {})
        assert isinstance(result.get("content"), list), "tools/call result.content must be an array"

        print("Self-test passed")
    finally:
        test_server.shutdown()
        test_server.server_close()


if __name__ == "__main__":
    if os.environ.get("RUN_SELF_TEST") == "1":
        run_self_test()
    else:
        run_server(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

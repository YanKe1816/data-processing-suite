import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlparse

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


def success(data: dict[str, Any] | None) -> dict[str, Any]:
    return {"success": True, "errors": [], "data": data}


def failure(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "errors": [{"code": code, "message": message}], "data": None}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _word_count(text: str) -> dict[str, int]:
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


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "text_normalize":
            return success({"text": _normalize(str(arguments["text"]))})
        if name == "text_word_count":
            return success(_word_count(str(arguments["text"])))
        if name == "slugify":
            return success({"slug": _slugify(str(arguments["text"]))})
        if name == "truncate":
            max_length = int(arguments["max_length"])
            if max_length < 0:
                return failure("INVALID_ARGUMENT", "max_length must be >= 0")
            return success({"text": _truncate(str(arguments["text"]), max_length)})
        if name == "text_replace":
            return success({"text": _replace(str(arguments["text"]), str(arguments["old"]), str(arguments["new"]))})
        return failure("TOOL_NOT_FOUND", f"Unknown tool '{name}'")
    except KeyError as exc:
        return failure("INVALID_ARGUMENT", f"Missing required argument: {exc.args[0]}")
    except (TypeError, ValueError):
        return failure("INVALID_ARGUMENT", "Tool arguments are invalid")


def handle_jsonrpc(payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32600, "Invalid Request")
    if payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(payload.get("id"), -32600, "Invalid Request")

    req_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params")

    if method == "initialize":
        result = {
            "name": APP_NAME,
            "version": APP_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": APP_NAME, "version": APP_VERSION},
        }
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "notifications/initialized":
        if req_id is None:
            return HTTPStatus.NO_CONTENT, None
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": {"acknowledged": True}}

    if method == "tools/list":
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFINITIONS}}

    if method == "tools/call":
        if "name" not in params:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params: missing tool name")
        if "arguments" in params and not isinstance(params["arguments"], dict):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params: arguments must be object")
        name = str(params["name"])
        arguments = params.get("arguments", {})
        return HTTPStatus.OK, {"jsonrpc": "2.0", "id": req_id, "result": handle_tool_call(name, arguments)}

    return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DataProcessingSuite/1.1"

    def _send_json(self, status: int, data: dict[str, Any] | None) -> None:
        self.send_response(status)
        if data is None:
            self.end_headers()
            return
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, status: int, event_data: str) -> None:
        body = f"event: message\ndata: {event_data}\n\n".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _manifest(self) -> dict[str, Any]:
        host = self.headers.get("Host", "localhost:8000")
        return {
            "name": APP_NAME,
            "version": APP_VERSION,
            "base_url": f"http://{host}",
            "tools": TOOL_DEFINITIONS,
        }

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
            self._send_json(HTTPStatus.OK, {"support": "Open an issue in the repository for support."})
            return
        if path == "/.well-known/openai-apps-challenge":
            self._send_json(HTTPStatus.OK, {"challenge": "static-placeholder"})
            return
        if path == "/mcp":
            accept = self.headers.get("Accept", "")
            if "text/event-stream" in accept:
                self._send_sse(HTTPStatus.OK, json.dumps({"type": "ready", "name": APP_NAME, "version": APP_VERSION}))
                return
            self._send_json(HTTPStatus.OK, self._manifest())
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
            self._send_json(HTTPStatus.BAD_REQUEST, _jsonrpc_error(None, -32700, "Parse error"))
            return

        status, response = handle_jsonrpc(payload)
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.serve_forever()


def run_self_test() -> bool:
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urlopen(base + "/health") as resp:
            health = json.loads(resp.read().decode("utf-8"))
            if resp.status != 200 or health != {"status": "ok"}:
                return False

        with urlopen(base + "/mcp") as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
            if manifest.get("name") != APP_NAME or manifest.get("version") != APP_VERSION:
                return False

        init_req = Request(
            base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(init_req) as resp:
            init_body = json.loads(resp.read().decode("utf-8"))
            if resp.status != 200 or "result" not in init_body:
                return False

        list_req = Request(
            base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(list_req) as resp:
            list_body = json.loads(resp.read().decode("utf-8"))
            tool_names = [tool["name"] for tool in list_body["result"]["tools"]]
            if tool_names != ["text_normalize", "text_word_count", "slugify", "truncate", "text_replace"]:
                return False

        call_req = Request(
            base + "/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "text_normalize", "arguments": {"text": "  HeLLo   WORLD "}},
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(call_req) as resp:
            call_body = json.loads(resp.read().decode("utf-8"))
            expected = {"success": True, "errors": [], "data": {"text": "hello world"}}
            if resp.status != 200 or call_body.get("result") != expected:
                return False

        source = open(__file__, "r", encoding="utf-8").read()
        if '"0.0.0.0"' not in source or 'os.environ.get("PORT", "8000")' not in source:
            return False

        return True
    finally:
        server.shutdown()
        thread.join(timeout=1)


if __name__ == "__main__":
    if os.environ.get("RUN_SELF_TEST", "0") == "1":
        ok = run_self_test()
        print(json.dumps({"self_test": "passed" if ok else "failed"}))
        raise SystemExit(0 if ok else 1)

    run_server(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

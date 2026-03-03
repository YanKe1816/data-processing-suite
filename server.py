import json
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

APP_NAME = "data-processing-suite"
APP_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORT_EMAIL = "support@data-processing-suite.local"

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
    },
    {
        "name": "text_word_count",
        "description": "Count chars, words, and lines.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "slugify",
        "description": "Convert text to URL slug.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "truncate",
        "description": "Truncate text to max_length.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "max_length": {"type": "integer", "minimum": 0},
            },
            "required": ["text", "max_length"],
            "additionalProperties": False,
        },
    },
    {
        "name": "text_replace",
        "description": "Replace all old substrings with new substring.",
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
    },
]

_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


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


def _validate_schema(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    allowed = set(schema.get("properties", {}).keys())
    required = schema.get("required", [])
    for key in required:
        if key not in arguments:
            return f"missing required field: {key}"
    if schema.get("additionalProperties") is False:
        for key in arguments.keys():
            if key not in allowed:
                return f"unexpected field: {key}"

    for key, defn in schema.get("properties", {}).items():
        if key not in arguments:
            continue
        value = arguments[key]
        expected = defn.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"field '{key}' must be a string"
        if expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"field '{key}' must be an integer"
            minimum = defn.get("minimum")
            if minimum is not None and value < minimum:
                return f"field '{key}' must be >= {minimum}"
    return None


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    if tool_name == "text_normalize":
        return True, {"text": _normalize(arguments["text"])}
    if tool_name == "text_word_count":
        return True, _word_count(arguments["text"])
    if tool_name == "slugify":
        return True, {"slug": _slugify(arguments["text"])}
    if tool_name == "truncate":
        return True, {"text": _truncate(arguments["text"], arguments["max_length"])}
    if tool_name == "text_replace":
        return True, {"text": _replace(arguments["text"], arguments["old"], arguments["new"])}
    return False, "tool not found"


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
    }


def _client_key(handler: BaseHTTPRequestHandler) -> str:
    return handler.client_address[0]


def _get_session_state(client_key: str) -> dict[str, Any]:
    with _SESSIONS_LOCK:
        return dict(_SESSIONS.get(client_key, {}))


def _set_session_state(client_key: str, state: dict[str, Any]) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS[client_key] = state


def handle_jsonrpc(payload: Any, client_key: str) -> tuple[int, dict[str, Any] | None]:
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
        return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params: object required")

    state = _get_session_state(client_key)

    if method == "initialize":
        requested_protocol = params.get("protocolVersion")
        if requested_protocol is None:
            negotiated = DEFAULT_PROTOCOL_VERSION
        elif not isinstance(requested_protocol, str) or not requested_protocol:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params: protocolVersion must be a non-empty string")
        elif requested_protocol not in SUPPORTED_PROTOCOL_VERSIONS:
            versions = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(
                req_id,
                -32602,
                f"Unsupported protocolVersion '{requested_protocol}'. Supported: {versions}",
            )
        else:
            negotiated = requested_protocol
        _set_session_state(
            client_key,
            {
                "initialized": True,
                "ready": False,
                "protocolVersion": negotiated,
            },
        )
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": APP_NAME, "version": APP_VERSION},
        }
        return HTTPStatus.OK, _jsonrpc_result(req_id, result)

    if method == "notifications/initialized":
        if not state.get("initialized"):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32000, "Initialize must be called before notifications/initialized")
        state["ready"] = True
        _set_session_state(client_key, state)
        if req_id is None:
            return HTTPStatus.NO_CONTENT, None
        return HTTPStatus.OK, _jsonrpc_result(req_id, {"acknowledged": True})

    if method == "tools/list":
        if not state.get("ready"):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32000, "Server not initialized")
        return HTTPStatus.OK, _jsonrpc_result(req_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        if not state.get("ready"):
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32000, "Server not initialized")

        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, "Invalid params: name must be a non-empty string")

        arguments = params.get("arguments", {})
        schema = next((tool["inputSchema"] for tool in TOOL_DEFINITIONS if tool["name"] == tool_name), None)
        if schema is None:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, f"Unknown tool: {tool_name}")

        validation_error = _validate_schema(arguments, schema)
        if validation_error:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, f"Invalid params: {validation_error}")

        ok, data = _call_tool(tool_name, arguments)
        if not ok:
            return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32602, str(data))

        return HTTPStatus.OK, _jsonrpc_result(req_id, _tool_result(data))

    return HTTPStatus.BAD_REQUEST, _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DataProcessingSuite/2.0"

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

    def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
            self._send_text(HTTPStatus.OK, "Privacy: this service is stateless and does not store request data.")
            return
        if path == "/terms":
            self._send_text(HTTPStatus.OK, "Terms: provided as-is without warranty.")
            return
        if path == "/support":
            self._send_text(HTTPStatus.OK, f"Support: contact {SUPPORT_EMAIL}")
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

        status, response = handle_jsonrpc(payload, _client_key(self))
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.serve_forever()


def run_self_test() -> bool:
    # Keep this optional routine dependency-free and deterministic.
    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)

    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/health") as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if resp.status != 200 or body != {"status": "ok"}:
                return False

        with urlopen(base + "/mcp") as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("name") != APP_NAME or body.get("version") != APP_VERSION:
                return False

        init_req = Request(
            base + "/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": DEFAULT_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "self-test", "version": "1"}},
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(init_req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            result = body.get("result", {})
            if resp.status != 200 or "protocolVersion" not in result or "capabilities" not in result or "serverInfo" not in result:
                return False

        notif_req = Request(
            base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(notif_req) as resp:
            if resp.status != 204:
                return False

        list_req = Request(
            base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(list_req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            names = [t["name"] for t in body["result"]["tools"]]
            if names != ["text_normalize", "text_word_count", "slugify", "truncate", "text_replace"]:
                return False

        call_req = Request(
            base + "/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "text_normalize", "arguments": {"text": "  HeLLo   WORLD  "}},
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(call_req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            result = body.get("result", {})
            if resp.status != 200 or result.get("content", [{}])[0].get("type") != "text":
                return False
            structured = result.get("structuredContent", {})
            if structured != {"text": "hello world"}:
                return False

        with open(__file__, "r", encoding="utf-8") as f:
            source = f.read()
        if '"0.0.0.0"' not in source:
            return False
        if 'os.environ.get("PORT", "8000")' not in source:
            return False
        return True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


if __name__ == "__main__":
    if os.environ.get("RUN_SELF_TEST", "0") == "1":
        ok = run_self_test()
        print(json.dumps({"self_test": "passed" if ok else "failed"}))
        raise SystemExit(0 if ok else 1)

    run_server(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

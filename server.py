import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
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


@dataclass
class Response:
    status: int
    body: Dict[str, Any] | None = None


def success(data: Dict[str, Any] | None) -> Dict[str, Any]:
    return {"success": True, "errors": [], "data": data}


def failure(code: str, message: str) -> Dict[str, Any]:
    return {"success": False, "errors": [{"code": code, "message": message}], "data": None}


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


def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "text_normalize":
            return success({"text": _normalize(str(arguments["text"]))})
        if name == "text_word_count":
            return success(_word_count(str(arguments["text"])))
        if name == "slugify":
            return success({"slug": _slugify(str(arguments["text"]))})
        if name == "truncate":
            return success({"text": _truncate(str(arguments["text"]), int(arguments["max_length"]))})
        if name == "text_replace":
            return success({"text": _replace(str(arguments["text"]), str(arguments["old"]), str(arguments["new"]))})
        return failure("TOOL_NOT_FOUND", f"Unknown tool '{name}'")
    except KeyError as exc:
        return failure("INVALID_ARGUMENT", f"Missing required argument: {exc.args[0]}")
    except (TypeError, ValueError):
        return failure("INVALID_ARGUMENT", "Tool arguments are invalid")


def handle_jsonrpc(payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any] | None]:
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
        return HTTPStatus.OK, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        return HTTPStatus.OK, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": handle_tool_call(str(name), arguments),
        }

    return HTTPStatus.BAD_REQUEST, {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


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

        status, response = handle_jsonrpc(payload)
        self._send_json(status, response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.serve_forever()


if __name__ == "__main__":
    run_server()

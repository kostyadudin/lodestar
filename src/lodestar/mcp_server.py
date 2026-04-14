"""Minimal MCP-compatible stdio server for Lodestar."""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any

from .config import DEFAULT_BUDGET_TOKENS, DEFAULT_LIMIT
from .indexer import LodestarService


_LOG = logging.getLogger("lodestar.mcp")


class MCPServer:
    def __init__(self) -> None:
        self.service = LodestarService()
        logging.basicConfig(
            stream=sys.stderr,
            level=logging.WARNING,
            format="%(levelname)s lodestar-mcp: %(message)s",
        )

    def serve(self) -> int:
        _LOG.debug("Lodestar MCP server starting")
        while True:
            message = self._read_message()
            if message is None:
                _LOG.debug("stdin closed — shutting down")
                return 0
            if "_parse_error" in message:
                self._write_message(
                    self._error(None, -32700, "Parse error", message["_parse_error"])
                )
                continue
            response = self._handle(message)
            if response is not None:
                self._write_message(response)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            _LOG.debug("initialize from client")
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "serverInfo": {"name": "lodestar", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            }

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _TOOL_DEFS}}

        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not name:
                return self._error(msg_id, -32602, "Missing required param: name")
            arguments = params.get("arguments") or {}
            try:
                payload = self._call_tool(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]},
                }
            except ValueError as exc:
                # Invalid arguments — still a tool-level error, not a protocol error.
                _LOG.warning("Tool %r invalid args: %s", name, exc)
                return self._tool_error(msg_id, str(exc))
            except Exception as exc:
                _LOG.error("Tool %r raised: %s\n%s", name, exc, traceback.format_exc())
                return self._tool_error(msg_id, str(exc))

        # Unknown notifications (no id) are silently dropped per spec.
        if msg_id is None:
            _LOG.debug("Dropping unknown notification: %s", method)
            return None

        _LOG.warning("Unsupported method: %s", method)
        return self._error(msg_id, -32601, f"Unsupported method: {method}")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        def req(key: str) -> Any:
            if key not in arguments:
                raise ValueError(f"Missing required argument: {key!r}")
            return arguments[key]

        match name:
            case "project.index":
                return self.service.index(req("repo_root"), options=arguments.get("options"))
            case "project.refresh":
                return self.service.refresh(req("repo_root"), changed_paths=arguments.get("changed_paths"))
            case "project.overview":
                return self.service.overview(req("repo_root"))
            case "project.search":
                return self.service.search(
                    req("repo_root"),
                    req("query"),
                    kind=arguments.get("kind"),
                    limit=arguments.get("limit"),
                )
            case "project.retrieve":
                return self.service.retrieve(
                    req("repo_root"),
                    req("query"),
                    budget_tokens=arguments.get("budget_tokens"),
                    scope=arguments.get("scope"),
                )
            case "project.explain":
                return self.service.explain(
                    req("repo_root"),
                    req("subject"),
                    depth=arguments.get("depth"),
                )
            case "project.remember":
                return self.service.remember(
                    req("repo_root"),
                    req("title"),
                    req("summary"),
                    evidence_refs=arguments.get("evidence_refs"),
                )
            case _:
                raise ValueError(f"Unknown tool: {name!r}")

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _read_message(self) -> dict[str, Any] | None:
        try:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _LOG.warning("Framing error on incoming message: %s", exc)
            return {"_parse_error": str(exc)}

    def _write_message(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sys.stdout.buffer.write(body + b"\n")
        sys.stdout.buffer.flush()

    # ------------------------------------------------------------------
    # Error helpers
    # ------------------------------------------------------------------

    def _error(
        self, msg_id: Any, code: int, message: str, data: str | None = None
    ) -> dict[str, Any]:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": msg_id, "error": err}

    def _tool_error(self, msg_id: Any, message: str) -> dict[str, Any]:
        """Return a tool-level error as result.isError per the MCP spec."""
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": f"Error: {message}"}],
                "isError": True,
            },
        }


# ------------------------------------------------------------------
# Tool definitions (static, factored out of _handle for readability)
# ------------------------------------------------------------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "project.index",
        "description": "Index a repository into /.lodestar/",
        "inputSchema": {
            "type": "object",
            "properties": {"repo_root": {"type": "string"}},
            "required": ["repo_root"],
        },
    },
    {
        "name": "project.refresh",
        "description": "Refresh an existing repository index",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "changed_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo_root"],
        },
    },
    {
        "name": "project.overview",
        "description": "Get a high-level repository overview",
        "inputSchema": {
            "type": "object",
            "properties": {"repo_root": {"type": "string"}},
            "required": ["repo_root"],
        },
    },
    {
        "name": "project.search",
        "description": "Search indexed files",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "query": {"type": "string"},
                "kind": {"type": "string"},
                "limit": {"type": "integer", "default": DEFAULT_LIMIT},
            },
            "required": ["repo_root", "query"],
        },
    },
    {
        "name": "project.retrieve",
        "description": "Build a bounded context pack",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "query": {"type": "string"},
                "budget_tokens": {"type": "integer", "default": DEFAULT_BUDGET_TOKENS},
                "scope": {"type": "string"},
            },
            "required": ["repo_root", "query"],
        },
    },
    {
        "name": "project.explain",
        "description": "Explain a subject using indexed context",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "subject": {"type": "string"},
                "depth": {"type": "string"},
            },
            "required": ["repo_root", "subject"],
        },
    },
    {
        "name": "project.remember",
        "description": "Store a durable repo memory",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo_root", "title", "summary"],
        },
    },
]


def main() -> int:
    server = MCPServer()
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())

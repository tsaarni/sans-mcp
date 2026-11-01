#!/usr/bin/env python3
"""Python library and HTTP proxy for Model Context Protocol (MCP) servers."""

from __future__ import annotations

import argparse
import atexit
import contextlib
import http.server
import itertools
import json
import keyword
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SansMCP", "SansMCPError", "connect", "main"]


class SansMCPError(RuntimeError):
    """Base error raised by sans-mcp client, transports, and tools."""


def _build_ident_map(raw_names: list[str]) -> dict[str, str]:
    """Deterministically map raw MCP wire names to unique, valid ASCII Python identifiers.

    Pass 1: Exact valid ASCII identifiers claim their exact names.
    Pass 2: Remaining names are normalized, prefixed/suffixed if needed, and deduplicated.
    """
    mapping: dict[str, str] = {}
    claimed: set[str] = set()

    # Pass 1: exact valid ASCII identifiers win first
    for raw in raw_names:
        if (
            raw.isascii()
            and raw.isidentifier()
            and not keyword.iskeyword(raw)
            and not raw.startswith("_")
        ):
            mapping[raw] = raw
            claimed.add(raw)

    # Pass 2: normalize remaining names
    for raw in raw_names:
        if raw in mapping:
            continue

        s = re.sub(r"[^0-9a-zA-Z_]+", "_", raw).strip("_") or "item"
        if s[0].isdigit():
            s = f"_{s}"
        if keyword.iskeyword(s):
            s = f"{s}_"

        cand = s
        idx = 2
        while cand in claimed:
            cand = f"{s}_{idx}"
            idx += 1

        claimed.add(cand)
        mapping[raw] = cand

    return mapping


def _unwrap_result(name: str, rpc_response: dict[str, Any], wraps_result: bool = False) -> Any:
    """Unwrap MCP JSON-RPC response envelope into idiomatic Python types or raise SansMCPError."""
    if "error" in rpc_response:
        err = rpc_response["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise SansMCPError(f"RPC error calling '{name}': {msg}")

    result = rpc_response.get("result", {})
    if not isinstance(result, dict):
        return result

    if result.get("isError"):
        error_parts: list[str] = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                error_parts.append(item.get("text", ""))
            else:
                error_parts.append(str(item))
        error_text = "\n".join(filter(None, error_parts)).strip() or "Unknown error"
        raise SansMCPError(f"Tool '{name}' failed: {error_text}")

    if "structuredContent" in result and result["structuredContent"] is not None:
        sc = result["structuredContent"]
        if isinstance(sc, dict) and list(sc.keys()) == ["result"]:
            if wraps_result or not isinstance(sc["result"], dict):
                return sc["result"]
            content = result.get("content", [])
            if content and isinstance(content, list):
                first_text = content[0].get("text", "") if isinstance(content[0], dict) else ""
                trimmed = first_text.lstrip()
                if trimmed and not (trimmed.startswith("{") and trimmed.endswith("}")):
                    return sc["result"]
        return sc

    content = result.get("content", [])
    if not isinstance(content, list):
        return content

    unwrapped_items: list[Any] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            trimmed = text.lstrip()
            if trimmed.startswith(("{", "[")):
                try:
                    unwrapped_items.append(json.loads(text))
                    continue
                except Exception:
                    pass
            unwrapped_items.append(text)
        else:
            unwrapped_items.append(item)

    if len(unwrapped_items) == 0:
        return None
    if len(unwrapped_items) == 1:
        return unwrapped_items[0]
    return unwrapped_items


class _StdioTransport:
    """Subprocess stdio JSON-RPC transport with queue reading, ping handling, and clean exit."""

    def __init__(
        self,
        command: list[str],
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_list_changed: Callable[[], None] | None = None,
    ) -> None:
        self.command = command
        self.timeout = timeout
        self.on_list_changed = on_list_changed
        self._lock = threading.Lock()
        self._id_counter = itertools.count(1)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = False

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # inherit parent stderr
                text=True,
                bufsize=1,
                env=merged_env,
                cwd=cwd,
            )
        except Exception as e:
            raise SansMCPError(f"Failed to spawn stdio MCP server {command!r}: {e}") from e

        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        atexit.register(self.close)

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(None)

    def _write_line_unlocked(self, data: dict[str, Any]) -> None:
        if self._process.stdin is None or self._process.poll() is not None:
            raise SansMCPError(f"Subprocess terminated unexpectedly: {self.command!r}")
        payload = json.dumps(data) + "\n"
        try:
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except Exception as e:
            raise SansMCPError(f"Error writing to stdio process: {e}") from e

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise SansMCPError("Transport is closed")

            req_id = next(self._id_counter)
            msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            self._write_line_unlocked(msg)

            while True:
                try:
                    line = self._queue.get(timeout=self.timeout)
                except queue.Empty:
                    raise SansMCPError(
                        f"Request '{method}' (id={req_id}) timed out after {self.timeout}s"
                    ) from None

                if line is None:
                    code = self._process.poll()
                    raise SansMCPError(
                        f"Subprocess terminated (exit code {code}) while awaiting response for '{method}'"
                    )

                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                # Auto-reply to server-initiated ping requests
                if data.get("method") == "ping" and "id" in data:
                    ping_reply = {"jsonrpc": "2.0", "id": data["id"], "result": {}}
                    with contextlib.suppress(Exception):
                        self._write_line_unlocked(ping_reply)
                    continue

                # Server notifications
                if "method" in data and "id" not in data:
                    if (
                        data["method"] == "notifications/tools/list_changed"
                        and self.on_list_changed
                    ):
                        with contextlib.suppress(Exception):
                            self.on_list_changed()
                    continue

                # Response matching our request id
                if "id" in data and data["id"] == req_id:
                    return data

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._closed:
                raise SansMCPError("Transport is closed")
            msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            self._write_line_unlocked(msg)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

            if self._process.stdin:
                with contextlib.suppress(Exception):
                    self._process.stdin.close()

            if self._process.poll() is None:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=5)
                except Exception:
                    with contextlib.suppress(Exception):
                        self._process.kill()


class _HttpTransport:
    """Streamable HTTP transport handling JSON and SSE chunks with session tracking."""

    def __init__(
        self,
        url: str,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.custom_headers = headers or {}
        self.session_id: str | None = None
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()

    def _parse_sse_response(self, resp: Any, target_id: Any) -> dict[str, Any]:
        buffer: list[str] = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if buffer:
                    chunk = "\n".join(buffer)
                    buffer.clear()
                    try:
                        data = json.loads(chunk)
                        if data.get("id") == target_id:
                            return data
                    except json.JSONDecodeError:
                        pass
                continue

            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())

        if buffer:
            chunk = "\n".join(buffer)
            try:
                data = json.loads(chunk)
                if data.get("id") == target_id:
                    return data
            except json.JSONDecodeError:
                pass

        raise SansMCPError(f"SSE stream ended without response for request id={target_id}")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            req_id = next(self._id_counter)
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            body_bytes = json.dumps(payload).encode("utf-8")

            req_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            req_headers.update(self.custom_headers)
            if self.session_id:
                req_headers["mcp-session-id"] = self.session_id

            req = urllib.request.Request(
                self.url, data=body_bytes, headers=req_headers, method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    for h_name, h_val in resp.headers.items():
                        if h_name.lower() == "mcp-session-id":
                            self.session_id = h_val

                    content_type = resp.headers.get("Content-Type", "")
                    if "text/event-stream" in content_type:
                        return self._parse_sse_response(resp, req_id)
                    else:
                        resp_bytes = resp.read()
                        if not resp_bytes:
                            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
                        return json.loads(resp_bytes.decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                if e.code in (404, 405) and "/sse" in self.url:
                    raise SansMCPError(
                        f"HTTP {e.code} connecting to '{self.url}': Server may require legacy MCP SSE "
                        f"transport, which is deprecated in favor of Streamable HTTP ({err_body})"
                    ) from e
                raise SansMCPError(f"HTTP {e.code} error calling '{method}': {err_body}") from e
            except urllib.error.URLError as e:
                raise SansMCPError(f"Failed to connect to '{self.url}': {e.reason}") from e

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            body_bytes = json.dumps(payload).encode("utf-8")

            req_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            req_headers.update(self.custom_headers)
            if self.session_id:
                req_headers["mcp-session-id"] = self.session_id

            req = urllib.request.Request(
                self.url, data=body_bytes, headers=req_headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    for h_name, h_val in resp.headers.items():
                        if h_name.lower() == "mcp-session-id":
                            self.session_id = h_val
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                raise SansMCPError(
                    f"HTTP {e.code} error in notification '{method}': {err_body}"
                ) from e
            except urllib.error.URLError as e:
                raise SansMCPError(f"Failed to connect to '{self.url}': {e.reason}") from e

    def close(self) -> None:
        pass


def _resolve_schema_ref(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref and isinstance(ref, str):
        # Resolve #/$defs/Name or #/definitions/Name
        parts = ref.lstrip("#/").split("/")
        if len(parts) >= 2 and parts[0] in ("$defs", "definitions"):
            target_def = defs.get(parts[1])
            if isinstance(target_def, dict):
                merged = target_def.copy()
                for k, v in schema.items():
                    if k != "$ref":
                        merged[k] = v
                return merged
    return schema


def _format_type(schema: dict[str, Any] | None, defs: dict[str, Any]) -> str:
    if not schema or not isinstance(schema, dict):
        return "Any"

    schema = _resolve_schema_ref(schema, defs)

    # anyOf / oneOf
    union_schemas = schema.get("anyOf") or schema.get("oneOf")
    if union_schemas and isinstance(union_schemas, list):
        sub_types: list[str] = []
        has_null = False
        for s in union_schemas:
            if isinstance(s, dict) and s.get("type") == "null":
                has_null = True
            else:
                formatted = _format_type(s, defs)
                if formatted not in sub_types:
                    sub_types.append(formatted)
        base = " | ".join(sub_types) if sub_types else "Any"
        return f"{base} | None" if has_null else base

    # Enum
    enum_vals = schema.get("enum")
    if enum_vals and isinstance(enum_vals, list) and len(enum_vals) <= 5:
        return f"Literal[{', '.join(repr(v) for v in enum_vals)}]"

    t = schema.get("type")
    if t == "string":
        return "str"
    elif t == "integer":
        return "int"
    elif t == "number":
        return "float"
    elif t == "boolean":
        return "bool"
    elif t == "array":
        item_schema = schema.get("items")
        item_type = _format_type(item_schema, defs) if isinstance(item_schema, dict) else "Any"
        return f"list[{item_type}]"
    elif t == "object":
        return "dict"

    return "Any"


def _format_object_fields_preview(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    schema = _resolve_schema_ref(schema, defs)
    props = schema.get("properties")
    if not props or not isinstance(props, dict):
        return "dict"
    fields = [f"{k}: {_format_type(v, defs)}" for k, v in props.items()]
    return "{" + ", ".join(fields) + "}"


@dataclass
class _ToolSpec:
    raw: dict[str, Any]
    py_name: str
    wire_name: str
    py_to_wire_params: dict[str, str] = field(default_factory=dict)
    wire_to_py_params: dict[str, str] = field(default_factory=dict)
    required_py_params: set[str] = field(default_factory=set)
    param_types: dict[str, str] = field(default_factory=dict)
    param_defaults: dict[str, Any] = field(default_factory=dict)
    param_docs: dict[str, str] = field(default_factory=dict)
    return_type: str = "Any"
    return_doc: str = ""
    closed_properties: bool = True
    wraps_result: bool = False


def _build_tool_spec(tool_dict: dict[str, Any], py_name: str) -> _ToolSpec:
    wire_name = tool_dict.get("name", py_name)
    input_schema = tool_dict.get("inputSchema") or {}
    defs = input_schema.get("$defs") or input_schema.get("definitions") or {}
    raw_props = input_schema.get("properties") or {}
    required_raw = set(input_schema.get("required") or [])

    # Map parameters
    wire_to_py = _build_ident_map(list(raw_props.keys()))
    py_to_wire = {v: k for k, v in wire_to_py.items()}

    required_py: set[str] = set()
    param_types: dict[str, str] = {}
    param_defaults: dict[str, Any] = {}
    param_docs: dict[str, str] = {}

    for wire_param, py_param in wire_to_py.items():
        p_schema = raw_props.get(wire_param, {})
        p_type = _format_type(p_schema, defs)
        param_types[py_param] = p_type

        if "default" in p_schema:
            param_defaults[py_param] = p_schema["default"]
        elif wire_param in required_raw:
            required_py.add(py_param)
        else:
            param_defaults[py_param] = None

        desc = p_schema.get("description", "").strip()
        if desc:
            param_docs[py_param] = desc

    # Return type & outputSchema
    output_schema = tool_dict.get("outputSchema")
    wraps_result = False
    return_type = "Any"
    return_doc = ""

    if output_schema and isinstance(output_schema, dict):
        out_defs = output_schema.get("$defs") or output_schema.get("definitions") or {}
        out_props = output_schema.get("properties") or {}
        if output_schema.get("type") == "object" and list(out_props.keys()) == ["result"]:
            wraps_result = True
            inner_schema = out_props["result"]
            return_type = _format_type(inner_schema, out_defs)
            if inner_schema.get("type") == "array" and isinstance(inner_schema.get("items"), dict):
                item_schema = inner_schema["items"]
                if item_schema.get("type") == "object" and item_schema.get("properties"):
                    return_doc = f"list[{_format_object_fields_preview(item_schema, out_defs)}]"
            elif inner_schema.get("type") == "object" and inner_schema.get("properties"):
                return_doc = _format_object_fields_preview(inner_schema, out_defs)
        else:
            return_type = _format_type(output_schema, out_defs)
            if output_schema.get("type") == "object" and output_schema.get("properties"):
                return_doc = _format_object_fields_preview(output_schema, out_defs)

    additional_props = input_schema.get("additionalProperties")
    closed = additional_props is not True

    return _ToolSpec(
        raw=tool_dict,
        py_name=py_name,
        wire_name=wire_name,
        py_to_wire_params=py_to_wire,
        wire_to_py_params=wire_to_py,
        required_py_params=required_py,
        param_types=param_types,
        param_defaults=param_defaults,
        param_docs=param_docs,
        return_type=return_type,
        return_doc=return_doc,
        closed_properties=closed,
        wraps_result=wraps_result,
    )


def _format_signature(tool: _ToolSpec) -> str:
    params_str_list: list[str] = []

    # Required params first
    for p in tool.py_to_wire_params:
        if p in tool.required_py_params:
            t = tool.param_types.get(p, "Any")
            params_str_list.append(f"{p}: {t}")

    # Optional params next
    for p in tool.py_to_wire_params:
        if p not in tool.required_py_params:
            t = tool.param_types.get(p, "Any")
            val = tool.param_defaults.get(p)
            params_str_list.append(f"{p}: {t} = {val!r}")

    if params_str_list:
        sig = f"{tool.py_name}(*, {', '.join(params_str_list)})"
    else:
        sig = f"{tool.py_name}()"

    if tool.return_type != "Any":
        sig += f" -> {tool.return_type}"
    return sig


def _format_tool_help(tool: _ToolSpec) -> str:
    lines: list[str] = [_format_signature(tool)]

    raw_desc = tool.raw.get("description", "").strip()
    if raw_desc:
        for d_line in raw_desc.splitlines():
            lines.append(f"    {d_line.strip()}")

    if tool.param_docs:
        lines.append("    Args:")
        for p, doc in tool.param_docs.items():
            lines.append(f"      {p}: {doc}")

    if tool.return_doc:
        lines.append("    Returns:")
        lines.append(f"      {tool.return_doc}")

    return "\n".join(lines)


def _format_help(
    server_info: dict[str, Any],
    tools: list[_ToolSpec],
    tool_name: str | None = None,
) -> str:
    name = server_info.get("name", "MCP Server")
    version = server_info.get("version")
    ver_str = f" ({version})" if version else ""

    if tool_name is not None:
        matched = [t for t in tools if t.py_name == tool_name or t.wire_name == tool_name]
        if not matched:
            raise SansMCPError(f"Tool '{tool_name}' not found on server '{name}'.")
        return _format_tool_help(matched[0])

    count_str = f"{len(tools)} tool" if len(tools) == 1 else f"{len(tools)} tools"
    header = [
        f"Server: {name}{ver_str} — {count_str}",
        "Usage: m.tools.<tool_name>(param=value, ...)",
        "",
    ]
    tool_blocks = [_format_tool_help(t) for t in sorted(tools, key=lambda x: x.py_name)]
    return "\n".join(header) + "\n\n".join(tool_blocks)


class _ToolsProxy:
    """Namespace providing function-call access to MCP tools."""

    def __init__(self, client: SansMCP, tools: dict[str, _ToolSpec]) -> None:
        self._client = client
        self._tools = tools

    def __dir__(self) -> list[str]:
        return sorted(self._tools.keys())

    def __getattr__(self, py_name: str) -> Callable[..., Any]:
        if py_name not in self._tools:
            available = ", ".join(sorted(self._tools.keys()))
            raise AttributeError(f"Unknown MCP tool '{py_name}'. Available tools: {available}")

        spec = self._tools[py_name]

        def call_tool(*args: Any, **kwargs: Any) -> Any:
            if args:
                sig = _format_signature(spec)
                raise TypeError(
                    f"MCP tool '{py_name}' only accepts keyword arguments: m.tools.{sig}"
                )

            # Validate missing required parameters
            missing = spec.required_py_params - set(kwargs.keys())
            if missing:
                sig = _format_signature(spec)
                missing_str = ", ".join(sorted(missing))
                raise TypeError(
                    f"MCP tool '{py_name}' missing required argument(s): {missing_str}. "
                    f"Expected signature: m.tools.{sig}"
                )

            # Validate unknown parameters if closed
            if spec.closed_properties:
                unknown = set(kwargs.keys()) - set(spec.py_to_wire_params.keys())
                if unknown:
                    sig = _format_signature(spec)
                    unknown_str = ", ".join(sorted(unknown))
                    raise TypeError(
                        f"MCP tool '{py_name}' got unexpected keyword argument(s): {unknown_str}. "
                        f"Expected signature: m.tools.{sig}"
                    )

            # Translate Python kwargs to raw wire arguments
            wire_kwargs: dict[str, Any] = {}
            for k, v in kwargs.items():
                wire_key = spec.py_to_wire_params.get(k, k)
                wire_kwargs[wire_key] = v

            return self._client._call_tool(spec.wire_name, wire_kwargs, spec.wraps_result)

        return call_tool


class SansMCP:
    """Synchronous MCP client for tool discovery and invocation."""

    tools: _ToolsProxy

    def __init__(
        self,
        transport: _StdioTransport | _HttpTransport,
        target_name: str = "",
    ) -> None:
        self._transport = transport
        self._target_name = target_name
        self._server_info: dict[str, Any] = {}
        self._tools: dict[str, _ToolSpec] = {}
        self.tools = _ToolsProxy(self, self._tools)
        self._init_session()

    def _init_session(self) -> None:
        init_res = self._transport.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sans-mcp", "version": "0.1.0"},
            },
        )
        if "error" in init_res:
            err = init_res["error"]
            raise SansMCPError(f"Initialization handshake failed: {err}")

        result = init_res.get("result", {})
        self._server_info = result.get("serverInfo", {})

        self._transport.notify("notifications/initialized", {})
        self._load_tools()

    def _load_tools(self) -> None:
        raw_tools = self._list_tools()
        raw_names = [t.get("name", "") for t in raw_tools]
        ident_map = _build_ident_map(raw_names)

        self._tools.clear()
        for t in raw_tools:
            wire_name = t.get("name", "")
            py_name = ident_map.get(wire_name, wire_name)
            spec = _build_tool_spec(t, py_name)
            self._tools[py_name] = spec

        self.tools = _ToolsProxy(self, self._tools)

    def _list_tools(self) -> list[dict[str, Any]]:
        all_tools: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            params = {"cursor": cursor} if cursor else {}
            res = self._transport.request("tools/list", params)
            if "error" in res:
                err = res["error"]
                raise SansMCPError(f"Failed to list tools: {err}")

            result = res.get("result", {})
            tools = result.get("tools", [])
            all_tools.extend(tools)

            cursor = result.get("nextCursor")
            if not cursor:
                break

        return all_tools

    def _call_tool(
        self, wire_name: str, wire_arguments: dict[str, Any], wraps_result: bool = False
    ) -> Any:
        res = self._transport.request(
            "tools/call",
            {"name": wire_name, "arguments": wire_arguments},
        )
        return _unwrap_result(wire_name, res, wraps_result=wraps_result)

    def help(self, tool_name: str | None = None) -> str:
        """Return formatted Python documentation for all tools or a single tool."""
        return _format_help(self._server_info, list(self._tools.values()), tool_name)

    def close(self) -> None:
        """Close underlying transport and terminate child processes if applicable."""
        self._transport.close()

    def __getattr__(self, name: str) -> Any:
        if name in self._tools:
            raise AttributeError(
                f"'SansMCP' object has no attribute '{name}'. "
                f"To call an MCP tool, use 'm.tools.{name}(...)'."
            )
        raise AttributeError(f"'SansMCP' object has no attribute '{name}'")

    def __enter__(self) -> SansMCP:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def connect(
    target: str | list[str],
    *,
    timeout: float = 60.0,
    headers: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> SansMCP:
    """Connect to an MCP server via Streamable HTTP (URL) or ephemeral stdio command."""
    if isinstance(target, str) and target.startswith(("http://", "https://")):
        transport = _HttpTransport(target, timeout=timeout, headers=headers)
        return SansMCP(transport, target_name=target)

    cmd = shlex.split(target) if isinstance(target, str) else list(target)
    if not cmd:
        raise SansMCPError("Empty target command specified")

    transport = _StdioTransport(cmd, timeout=timeout, env=env, cwd=cwd)
    return SansMCP(transport, target_name=str(target))


def _run_proxy(host: str, port: int, command: list[str]) -> None:
    """Run persistent HTTP proxy caching initialize and tools/list in memory."""
    print(f"[sans-mcp] Spawning child stdio process: {command!r}", file=sys.stderr)

    tools_lock = threading.Lock()
    cached_tools_res: dict[str, Any] = {}

    def refresh_tools() -> None:
        nonlocal cached_tools_res
        with tools_lock:
            all_tools: list[dict[str, Any]] = []
            cursor: str | None = None
            while True:
                params = {"cursor": cursor} if cursor else {}
                res = transport.request("tools/list", params)
                tools = res.get("result", {}).get("tools", [])
                all_tools.extend(tools)
                cursor = res.get("result", {}).get("nextCursor")
                if not cursor:
                    break
            cached_tools_res = {"tools": all_tools}

    transport = _StdioTransport(
        command,
        timeout=120.0,
        on_list_changed=refresh_tools,
    )

    try:
        init_res = transport.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sans-mcp-gateway", "version": "0.1.0"},
            },
        )
        transport.notify("notifications/initialized", {})
        refresh_tools()
    except Exception as e:
        transport.close()
        print(f"[sans-mcp] Startup handshake failed: {e}", file=sys.stderr)
        sys.exit(1)

    cached_init_result = init_res.get("result", {})
    gateway_session_id = str(uuid.uuid4())

    class GatewayHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default request logging to keep stderr clean
            pass

        def do_GET(self) -> None:
            server_info = cached_init_result.get("serverInfo", {})
            with tools_lock:
                tool_count = len(cached_tools_res.get("tools", []))
            data = {
                "status": "ok",
                "session_id": gateway_session_id,
                "serverInfo": server_info,
                "tools_count": tool_count,
            }
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("mcp-session-id", gateway_session_id)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)

            try:
                rpc_req = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b'{"jsonrpc":"2.0","error":{"code":-32700,"message":"Parse error"}}'
                )
                return

            method = rpc_req.get("method")
            req_id = rpc_req.get("id")

            # Notifications (no id)
            if "id" not in rpc_req:
                if method != "notifications/initialized":
                    with contextlib.suppress(Exception):
                        transport.notify(method, rpc_req.get("params"))
                self.send_response(202)
                self.send_header("mcp-session-id", gateway_session_id)
                self.end_headers()
                return

            # Requests
            if method == "initialize":
                resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": cached_init_result}
            elif method == "tools/list":
                with tools_lock:
                    resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": cached_tools_res}
            elif method == "ping":
                resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            else:
                try:
                    resp_payload = transport.request(method, rpc_req.get("params"))
                    resp_payload["id"] = req_id  # ensure ID aligns with caller
                except Exception as e:
                    resp_payload = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(e)},
                    }

            resp_bytes = json.dumps(resp_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.send_header("mcp-session-id", gateway_session_id)
            self.end_headers()
            self.wfile.write(resp_bytes)

    class ThreadedServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = ThreadedServer((host, port), GatewayHandler)
    print(
        f"[sans-mcp] HTTP proxy running on http://{host}:{port}/mcp "
        f"({len(cached_tools_res.get('tools', []))} tools cached)",
        file=sys.stderr,
    )

    def shutdown_signal(sig: int, frame: Any) -> None:
        print("\n[sans-mcp] Shutting down proxy...", file=sys.stderr)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_signal)
    signal.signal(signal.SIGTERM, shutdown_signal)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        transport.close()
        print("[sans-mcp] Proxy exited cleanly.", file=sys.stderr)


def _parse_listen_address(addr: str) -> tuple[str, int]:
    """Parse listen address in [host]:port or :port format into (host, port)."""
    if ":" not in addr:
        raise SansMCPError(
            f"Invalid listen address: {addr!r} (expected [host]:port or :port, e.g. :8001)"
        )

    host, _, port_str = addr.rpartition(":")
    host = host.strip("[]").strip() or "127.0.0.1"
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ValueError
        return host, port
    except ValueError:
        raise SansMCPError(f"Invalid port in listen address: {addr!r}") from None


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="sans-mcp",
        description="Dynamic Python client and HTTP proxy for MCP servers",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # help subcommand
    help_parser = subparsers.add_parser("help", help="Discover tools and print signatures")
    help_parser.add_argument("target", help="Connection target (URL or stdio command string)")
    help_parser.add_argument(
        "tool_name", nargs="?", default=None, help="Optional specific tool name"
    )

    # proxy subcommand
    proxy_parser = subparsers.add_parser("proxy", help="Proxy child stdio server over HTTP")
    proxy_parser.add_argument(
        "--listen",
        default="127.0.0.1:6270",
        help="HTTP listen address in [host]:port or :port format (default: 127.0.0.1:6270)",
    )
    proxy_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Child command and arguments (use -- <command>...)",
    )

    args = parser.parse_args(argv)

    if args.subcommand == "help":
        try:
            with connect(args.target) as m:
                print(m.help(args.tool_name))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.subcommand == "proxy":
        cmd = args.command
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print(
                "Error: proxy requires a command (e.g. -- npx -y @playwright/mcp)",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            host, port = _parse_listen_address(args.listen)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        _run_proxy(host, port, cmd)


if __name__ == "__main__":
    main()

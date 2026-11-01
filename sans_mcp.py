#!/usr/bin/env python3
"""Python library and HTTP proxy for Model Context Protocol (MCP) servers."""

from __future__ import annotations

import argparse
import atexit
import contextlib
import http.client
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
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SansMCP", "SansMCPError", "connect", "main"]


class SansMCPError(RuntimeError):
    """Base error raised by sans-mcp client, transports, and tools."""


def _build_ident_map(raw_names: list[str]) -> dict[str, str]:
    """Deterministically map raw MCP wire names to unique, valid ASCII Python identifiers."""
    mapping: dict[str, str] = {}
    claimed: set[str] = set()

    # Claim exact valid identifiers first so normalized names (e.g. "a-b" -> "a_b")
    # never collide with or shadow an existing valid name ("a_b").
    for raw in raw_names:
        if (
            raw.isascii()
            and raw.isidentifier()
            and not keyword.iskeyword(raw)
            and not raw.startswith("_")
        ):
            mapping[raw] = raw
            claimed.add(raw)

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
        parts = [
            item.get("text", "")
            if isinstance(item, dict) and item.get("type") == "text"
            else str(item)
            for item in result.get("content", [])
        ]
        error_text = "\n".join(filter(None, parts)).strip() or "Unknown error"
        raise SansMCPError(f"Tool '{name}' failed: {error_text}")

    if (sc := result.get("structuredContent")) is not None:
        # MCP requires structuredContent to be a JSON object, so servers like FastMCP
        # wrap non-dict return values (lists, scalars) in {"result": <value>}.
        if (
            isinstance(sc, dict)
            and list(sc.keys()) == ["result"]
            and (wraps_result or not isinstance(sc["result"], dict))
        ):
            return sc["result"]
        return sc

    content = result.get("content", [])
    if not isinstance(content, list):
        return content

    unwrapped_items: list[Any] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            if text.lstrip().startswith(("{", "[")):
                with contextlib.suppress(Exception):
                    unwrapped_items.append(json.loads(text))
                    continue
            unwrapped_items.append(text)
        else:
            unwrapped_items.append(item)

    if not unwrapped_items:
        return None
    return unwrapped_items[0] if len(unwrapped_items) == 1 else unwrapped_items


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

        merged_env = {**os.environ, **(env or {})}
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
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
        with contextlib.suppress(Exception):
            for line in self._process.stdout:
                self._queue.put(line)
        self._queue.put(None)

    def _write_line_unlocked(self, data: dict[str, Any]) -> None:
        if self._process.stdin is None or self._process.poll() is not None:
            raise SansMCPError(f"Subprocess terminated unexpectedly: {self.command!r}")
        try:
            self._process.stdin.write(json.dumps(data) + "\n")
            self._process.stdin.flush()
        except Exception as e:
            raise SansMCPError(f"Error writing to stdio process: {e}") from e

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise SansMCPError("Transport is closed")

            req_id = next(self._id_counter)
            self._write_line_unlocked(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
            )

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

                if not isinstance(data, dict):
                    continue

                if data.get("method") == "ping" and "id" in data:
                    with contextlib.suppress(Exception):
                        self._write_line_unlocked(
                            {"jsonrpc": "2.0", "id": data["id"], "result": {}}
                        )
                    continue

                if "method" in data and "id" not in data:
                    if (
                        data["method"] == "notifications/tools/list_changed"
                        and self.on_list_changed
                    ):
                        with contextlib.suppress(Exception):
                            self.on_list_changed()
                    continue

                if data.get("id") == req_id:
                    return data

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self._closed:
                raise SansMCPError("Transport is closed")
            self._write_line_unlocked({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            atexit.unregister(self.close)

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
                        self._process.wait(timeout=1)

            if self._process.stdout:
                with contextlib.suppress(Exception):
                    self._process.stdout.close()


def _is_loopback_host(hostname: str) -> bool:
    h = hostname.lower().strip("[]")
    return (
        h in ("localhost", "127.0.0.1", "::1") or h.startswith("127.") or h.endswith(".localhost")
    )


def _auth_config_path() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "sans-mcp", "auth.json")


def _load_auth_headers(url: str) -> dict[str, str]:
    auth_path = _auth_config_path()
    if not os.path.exists(auth_path):
        return {}

    if os.name == "posix":
        mode = os.stat(auth_path).st_mode & 0o777
        if mode & 0o077:
            raise SansMCPError(
                f"Insecure permissions {oct(mode)} on '{auth_path}'; "
                f"expected 0o600 (run: chmod 600 {shlex.quote(auth_path)})"
            )

    try:
        with open(auth_path, encoding="utf-8") as f:
            raw_config = json.load(f)
    except Exception as e:
        raise SansMCPError(f"Failed to parse auth config '{auth_path}': {e}") from e

    if not isinstance(raw_config, dict):
        raise SansMCPError(
            f"Invalid auth config in '{auth_path}': top-level value must be a JSON object"
        )

    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return {}

    config_by_host = {str(k).lower(): (k, v) for k, v in raw_config.items()}
    candidates = (
        *([f"[{hostname}]:{parsed.port}", f"{hostname}:{parsed.port}"] if parsed.port else []),
        f"[{hostname}]",
        hostname,
    )
    matched = next((config_by_host[c] for c in candidates if c in config_by_host), None)
    if matched is None:
        return {}

    matched_key, matched_headers = matched
    if not isinstance(matched_headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in matched_headers.items()
    ):
        raise SansMCPError(
            f"Invalid auth config for '{matched_key}' in '{auth_path}': "
            "expected an object mapping header names to string values"
        )

    def replace_env(match: re.Match[str]) -> str:
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if not val:
            raise SansMCPError(
                f"Environment variable '{var_name}' referenced in '{auth_path}' "
                f"for '{matched_key}' is not set"
            )
        return val

    return {
        h_name: re.sub(r"\$\{([^}]+)\}", replace_env, h_val)
        for h_name, h_val in matched_headers.items()
    }


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
    return (scheme, host, port)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that blocks HTTPS downgrades and strips credentials cross-origin."""

    _SAFE_HEADERS = frozenset({"accept", "content-length", "content-type", "host", "user-agent"})

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        orig_origin = _url_origin(req.full_url)
        new_origin = _url_origin(newurl)
        if orig_origin[0] == "https" and new_origin[0] != "https":
            raise SansMCPError(
                f"Refusing redirect from HTTPS '{req.full_url}' to plaintext HTTP '{newurl}'"
            )

        if code in (307, 308) and req.get_method() == "POST":
            new_req: urllib.request.Request | None = urllib.request.Request(
                newurl.replace(" ", "%20"),
                data=req.data,
                headers=dict(req.headers),
                origin_req_host=req.origin_req_host,
                unverifiable=True,
                method="POST",
            )
        else:
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        assert new_req is not None

        if orig_origin != new_origin:
            for hdr_dict in (new_req.headers, new_req.unredirected_hdrs):
                for k in list(hdr_dict.keys()):
                    if k.lower() not in self._SAFE_HEADERS:
                        del hdr_dict[k]

        return new_req


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
        resolved_headers = _load_auth_headers(url)
        if headers:
            override_lower = {k.lower() for k in headers}
            resolved_headers = {
                k: v for k, v in resolved_headers.items() if k.lower() not in override_lower
            }
            resolved_headers.update(headers)
        self.custom_headers = resolved_headers

        if self.custom_headers:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme.lower() != "https" and not _is_loopback_host(parsed.hostname or ""):
                raise SansMCPError(
                    f"Refusing to send authentication headers over plaintext HTTP "
                    f"to non-loopback host '{parsed.hostname}'"
                )

        self.session_id: str | None = None
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())

    def _parse_sse_response(self, resp: Any, target_id: Any) -> dict[str, Any]:
        buffer: list[str] = []
        for raw_line in itertools.chain(resp, [b"\n"]):
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                buffer.append(line[5:].lstrip())
            elif not line and buffer:
                chunk = "\n".join(buffer)
                buffer.clear()
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(chunk)
                    if isinstance(data, dict) and data.get("id") == target_id:
                        return data
        raise SansMCPError(f"SSE stream ended without response for request id={target_id}")

    def _send(
        self, payload: dict[str, Any], req_id: int | None = None, label: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            body_bytes = json.dumps(payload).encode("utf-8")
            req_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **self.custom_headers,
            }
            if self.session_id:
                req_headers["mcp-session-id"] = self.session_id

            req = urllib.request.Request(
                self.url, data=body_bytes, headers=req_headers, method="POST"
            )
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    for h_name, h_val in resp.headers.items():
                        if h_name.lower() == "mcp-session-id":
                            self.session_id = h_val

                    if req_id is None:
                        return {}

                    content_type = resp.headers.get("Content-Type", "")
                    if "text/event-stream" in content_type:
                        return self._parse_sse_response(resp, req_id)

                    resp_bytes = resp.read()
                    if not resp_bytes:
                        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
                    return json.loads(resp_bytes.decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except OSError:
                    err_body = str(e.reason)
                if e.code in (404, 405) and "/sse" in self.url:
                    raise SansMCPError(
                        f"HTTP {e.code} connecting to '{self.url}': Server may require legacy MCP SSE "
                        f"transport, which is deprecated in favor of Streamable HTTP ({err_body})"
                    ) from e
                raise SansMCPError(f"HTTP {e.code} error {label}: {err_body}") from e
            except urllib.error.URLError as e:
                raise SansMCPError(f"Failed to connect to '{self.url}': {e.reason}") from e
            except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
                raise SansMCPError(f"Error communicating with '{self.url}': {e}") from e

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = next(self._id_counter)
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        return self._send(payload, req_id=req_id, label=f"calling '{method}'")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._send(payload, req_id=None, label=f"in notification '{method}'")

    def close(self) -> None:
        pass


def _resolve_schema_ref(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        parts = ref.lstrip("#/").split("/")
        if len(parts) >= 2 and parts[0] in ("$defs", "definitions"):
            target_def = defs.get(parts[1])
            if isinstance(target_def, dict):
                return {**target_def, **{k: v for k, v in schema.items() if k != "$ref"}}
    return schema


def _format_type(schema: dict[str, Any] | None, defs: dict[str, Any]) -> str:
    if not schema or not isinstance(schema, dict):
        return "Any"

    schema = _resolve_schema_ref(schema, defs)

    union_schemas = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(union_schemas, list) and union_schemas:
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

    enum_vals = schema.get("enum")
    if isinstance(enum_vals, list) and 0 < len(enum_vals) <= 5:
        return f"Literal[{', '.join(repr(v) for v in enum_vals)}]"

    t = schema.get("type")
    type_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "object": "dict",
    }
    if t in type_map:
        return type_map[t]
    if t == "array":
        item_schema = schema.get("items")
        item_type = _format_type(item_schema, defs) if isinstance(item_schema, dict) else "Any"
        return f"list[{item_type}]"

    return "Any"


def _format_object_fields_preview(schema: dict[str, Any], defs: dict[str, Any]) -> str:
    schema = _resolve_schema_ref(schema, defs)
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "dict"
    fields = [f"{k}: {_format_type(v, defs)}" for k, v in props.items()]
    return "{" + ", ".join(fields) + "}"


@dataclass
class _ToolSpec:
    py_name: str
    wire_name: str
    py_to_wire_params: dict[str, str] = field(default_factory=dict)
    required_py_params: set[str] = field(default_factory=set)
    return_type: str = "Any"
    closed_properties: bool = True
    wraps_result: bool = False
    signature: str = ""
    help_text: str = ""


def _build_tool_spec(tool_dict: dict[str, Any], py_name: str) -> _ToolSpec:
    wire_name = tool_dict.get("name", py_name)
    input_schema = tool_dict.get("inputSchema") or {}
    defs = input_schema.get("$defs") or input_schema.get("definitions") or {}
    raw_props = input_schema.get("properties") or {}
    required_raw = set(input_schema.get("required") or [])

    wire_to_py = _build_ident_map(list(raw_props.keys()))
    py_to_wire = {v: k for k, v in wire_to_py.items()}

    required_py: set[str] = set()
    req_parts: list[str] = []
    opt_parts: list[str] = []
    arg_docs: list[str] = []

    for wire_param, py_param in wire_to_py.items():
        p_schema = raw_props.get(wire_param, {})
        p_type = _format_type(p_schema, defs)
        if "default" in p_schema:
            opt_parts.append(f"{py_param}: {p_type} = {p_schema['default']!r}")
        elif wire_param in required_raw:
            required_py.add(py_param)
            req_parts.append(f"{py_param}: {p_type}")
        else:
            opt_parts.append(f"{py_param}: {p_type} = None")

        if desc := p_schema.get("description", "").strip():
            arg_docs.append(f"      {py_param}: {desc}")

    output_schema = tool_dict.get("outputSchema")
    wraps_result = False
    return_type = "Any"
    return_doc = ""

    if isinstance(output_schema, dict) and output_schema:
        out_defs = output_schema.get("$defs") or output_schema.get("definitions") or {}
        resolved_out = _resolve_schema_ref(output_schema, out_defs)
        out_props = resolved_out.get("properties") or {}
        if resolved_out.get("type") == "object" and list(out_props.keys()) == ["result"]:
            wraps_result = True
            target_schema = _resolve_schema_ref(out_props["result"], out_defs)
        else:
            target_schema = resolved_out

        return_type = _format_type(target_schema, out_defs)
        if target_schema.get("type") == "array" and isinstance(target_schema.get("items"), dict):
            item_schema = _resolve_schema_ref(target_schema["items"], out_defs)
            if item_schema.get("type") == "object" and item_schema.get("properties"):
                return_doc = f"list[{_format_object_fields_preview(item_schema, out_defs)}]"
        elif target_schema.get("type") == "object" and target_schema.get("properties"):
            return_doc = _format_object_fields_preview(target_schema, out_defs)

    params = ", ".join(req_parts + opt_parts)
    sig = f"{py_name}(*, {params})" if params else f"{py_name}()"
    if return_type != "Any":
        sig = f"{sig} -> {return_type}"

    lines = [sig]
    if raw_desc := tool_dict.get("description", "").strip():
        lines.extend(f"    {d_line.strip()}" for d_line in raw_desc.splitlines())
    if arg_docs:
        lines.append("    Args:")
        lines.extend(arg_docs)
    if return_doc:
        lines.append("    Returns:")
        lines.append(f"      {return_doc}")

    return _ToolSpec(
        py_name=py_name,
        wire_name=wire_name,
        py_to_wire_params=py_to_wire,
        required_py_params=required_py,
        return_type=return_type,
        closed_properties=input_schema.get("additionalProperties") is not True,
        wraps_result=wraps_result,
        signature=sig,
        help_text="\n".join(lines),
    )


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
        return matched[0].help_text

    count_str = f"{len(tools)} tool" if len(tools) == 1 else f"{len(tools)} tools"
    header = (
        f"Server: {name}{ver_str} — {count_str}\nUsage: m.tools.<tool_name>(param=value, ...)\n\n"
    )
    return header + "\n\n".join(t.help_text for t in sorted(tools, key=lambda x: x.py_name))


def _initialize_mcp(transport: Any, client_name: str = "sans-mcp") -> dict[str, Any]:
    init_res = transport.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        },
    )
    if "error" in init_res:
        raise SansMCPError(f"Initialization handshake failed: {init_res['error']}")
    transport.notify("notifications/initialized", {})
    return init_res.get("result", {})


def _fetch_tools(transport: Any) -> list[dict[str, Any]]:
    all_tools: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        res = transport.request("tools/list", {"cursor": cursor} if cursor else {})
        if "error" in res:
            raise SansMCPError(f"Failed to list tools: {res['error']}")
        result = res.get("result", {})
        all_tools.extend(result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return all_tools


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
                raise TypeError(
                    f"MCP tool '{py_name}' only accepts keyword arguments: m.tools.{spec.signature}"
                )

            if missing := spec.required_py_params - set(kwargs.keys()):
                missing_str = ", ".join(sorted(missing))
                raise TypeError(
                    f"MCP tool '{py_name}' missing required argument(s): {missing_str}. "
                    f"Expected signature: m.tools.{spec.signature}"
                )

            if spec.closed_properties and (
                unknown := set(kwargs.keys()) - set(spec.py_to_wire_params.keys())
            ):
                unknown_str = ", ".join(sorted(unknown))
                raise TypeError(
                    f"MCP tool '{py_name}' got unexpected keyword argument(s): {unknown_str}. "
                    f"Expected signature: m.tools.{spec.signature}"
                )

            wire_kwargs = {spec.py_to_wire_params.get(k, k): v for k, v in kwargs.items()}
            return self._client._call_tool(spec.wire_name, wire_kwargs, spec.wraps_result)

        return call_tool


class SansMCP:
    """Synchronous MCP client for tool discovery and invocation."""

    tools: _ToolsProxy

    def __init__(self, transport: _StdioTransport | _HttpTransport) -> None:
        self._transport = transport
        self._server_info: dict[str, Any] = {}
        self._tools: dict[str, _ToolSpec] = {}
        self.tools = _ToolsProxy(self, self._tools)
        try:
            self._init_session()
        except Exception:
            self._transport.close()
            raise

    def _init_session(self) -> None:
        result = _initialize_mcp(self._transport, client_name="sans-mcp")
        self._server_info = result.get("serverInfo", {})
        self._load_tools()

    def _load_tools(self) -> None:
        raw_tools = _fetch_tools(self._transport)
        ident_map = _build_ident_map([t.get("name", "") for t in raw_tools])

        self._tools.clear()
        for t in raw_tools:
            wire_name = t.get("name", "")
            py_name = ident_map.get(wire_name, wire_name)
            self._tools[py_name] = _build_tool_spec(t, py_name)

        self.tools = _ToolsProxy(self, self._tools)

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
        return SansMCP(_HttpTransport(target, timeout=timeout, headers=headers))

    cmd = shlex.split(target) if isinstance(target, str) else list(target)
    if not cmd:
        raise SansMCPError("Empty target command specified")

    return SansMCP(_StdioTransport(cmd, timeout=timeout, env=env, cwd=cwd))


def _create_proxy_server(
    host: str, port: int, transport: Any
) -> tuple[http.server.ThreadingHTTPServer, dict[str, Any]]:
    """Initialize MCP session on transport and return a configured ThreadingHTTPServer."""
    tools_lock = threading.Lock()
    cached_tools_res: dict[str, Any] | None = None

    def invalidate_tools() -> None:
        nonlocal cached_tools_res
        cached_tools_res = None

    def get_tools_res() -> dict[str, Any]:
        nonlocal cached_tools_res
        with tools_lock:
            if cached_tools_res is None:
                cached_tools_res = {"tools": _fetch_tools(transport)}
            return cached_tools_res

    transport.on_list_changed = invalidate_tools
    cached_init_result = _initialize_mcp(transport, client_name="sans-mcp-gateway")
    initial_tools = get_tools_res()
    gateway_session_id = str(uuid.uuid4())

    class GatewayHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("mcp-session-id", gateway_session_id)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._send_json(
                200,
                {
                    "status": "ok",
                    "session_id": gateway_session_id,
                    "serverInfo": cached_init_result.get("serverInfo", {}),
                    "tools_count": len(get_tools_res().get("tools", [])),
                },
            )

        def do_POST(self) -> None:
            if "Origin" in self.headers:
                self._send_json(
                    403,
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Origin not allowed"}},
                )
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length) if length > 0 else b""
                rpc_req = json.loads(raw_body.decode("utf-8"))
            except Exception:
                self._send_json(
                    400,
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                )
                return

            if not isinstance(rpc_req, dict) or not isinstance(rpc_req.get("method"), str):
                self._send_json(
                    400,
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}},
                )
                return

            method = rpc_req["method"]
            req_id = rpc_req.get("id")

            if "id" not in rpc_req:
                if method != "notifications/initialized":
                    with contextlib.suppress(Exception):
                        transport.notify(method, rpc_req.get("params"))
                self.send_response(202)
                self.send_header("mcp-session-id", gateway_session_id)
                self.end_headers()
                return

            if method == "initialize":
                resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": cached_init_result}
            elif method == "tools/list":
                resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": get_tools_res()}
            elif method == "ping":
                resp_payload = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            else:
                try:
                    resp_payload = transport.request(method, rpc_req.get("params"))
                    resp_payload["id"] = req_id
                except Exception as e:
                    resp_payload = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(e)},
                    }

            self._send_json(200, resp_payload)

    class ThreadedServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return ThreadedServer((host, port), GatewayHandler), initial_tools


def _run_proxy(host: str, port: int, command: list[str]) -> None:
    """Run persistent HTTP proxy caching initialize and tools/list in memory."""
    print(f"[sans-mcp] Spawning child stdio process: {command!r}", file=sys.stderr)
    transport = _StdioTransport(command, timeout=120.0)

    try:
        httpd, initial_tools = _create_proxy_server(host, port, transport)
    except Exception as e:
        transport.close()
        print(f"[sans-mcp] Startup handshake failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"[sans-mcp] HTTP proxy running on http://{host}:{port}/mcp "
        f"({len(initial_tools.get('tools', []))} tools cached)",
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

    help_parser = subparsers.add_parser("help", help="Discover tools and print signatures")
    help_parser.add_argument("target", help="Connection target (URL or stdio command string)")
    help_parser.add_argument(
        "tool_name", nargs="?", default=None, help="Optional specific tool name"
    )

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

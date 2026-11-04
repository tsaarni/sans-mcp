import pathlib
import shlex
import socket
import subprocess
import sys
import time
from typing import Any

import pytest

import sans_mcp
from sans_mcp import (
    SansMCP,
    SansMCPError,
    _build_ident_map,
    _build_tool_spec,
    _format_help,
    _format_type,
    _unwrap_result,
    connect,
)


def test_build_ident_map_valid_names():
    names = ["simple", "valid_ident_123", "CamelCase"]
    mapping = _build_ident_map(names)
    assert mapping == {
        "simple": "simple",
        "valid_ident_123": "valid_ident_123",
        "CamelCase": "CamelCase",
    }


def test_build_ident_map_special_chars_and_digits():
    names = ["get-user-profile", "user.name", "2fa_code", "a b c"]
    mapping = _build_ident_map(names)
    assert mapping["get-user-profile"] == "get_user_profile"
    assert mapping["user.name"] == "user_name"
    assert mapping["2fa_code"] == "_2fa_code"
    assert mapping["a b c"] == "a_b_c"


def test_build_ident_map_python_keywords():
    names = ["from", "class", "import", "in", "def", "async"]
    mapping = _build_ident_map(names)
    assert mapping["from"] == "from_"
    assert mapping["class"] == "class_"
    assert mapping["import"] == "import_"
    assert mapping["in"] == "in_"
    assert mapping["def"] == "def_"
    assert mapping["async"] == "async_"


def test_build_ident_map_collisions():
    # Pass 1: "foo_bar" claims its name
    # Pass 2: "foo-bar" normalizes to "foo_bar", collides, becomes "foo_bar_2"
    names = ["foo-bar", "foo_bar"]
    mapping = _build_ident_map(names)
    assert mapping["foo_bar"] == "foo_bar"
    assert mapping["foo-bar"] == "foo_bar_2"


def test_build_ident_map_empty_and_underscores():
    names = ["---", "___", "_private"]
    mapping = _build_ident_map(names)
    # Leading underscore is not claimed in pass 1, normalizes to valid ident
    assert mapping["_private"] == "private"
    assert "item" in mapping["---"]


def test_format_type_primitives():
    defs = {}
    assert _format_type({"type": "string"}, defs) == "str"
    assert _format_type({"type": "integer"}, defs) == "int"
    assert _format_type({"type": "number"}, defs) == "float"
    assert _format_type({"type": "boolean"}, defs) == "bool"
    assert _format_type({"type": "array", "items": {"type": "string"}}, defs) == "list[str]"
    assert _format_type({"type": "object"}, defs) == "dict"
    assert _format_type(None, defs) == "Any"


def test_format_type_enum():
    defs = {}
    enum_schema = {"type": "string", "enum": ["asc", "desc"]}
    assert _format_type(enum_schema, defs) == "Literal['asc', 'desc']"

    # Enums with > 5 values fallback to base type
    large_enum = {"type": "string", "enum": ["a", "b", "c", "d", "e", "f"]}
    assert _format_type(large_enum, defs) == "str"


def test_format_type_unions():
    defs = {}
    union_schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert _format_type(union_schema, defs) == "str | None"

    multi_union = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    assert _format_type(multi_union, defs) == "str | int"


def test_format_type_refs():
    defs = {
        "Status": {"type": "string", "enum": ["active", "inactive"]},
        "Item": {"type": "object", "properties": {"id": {"type": "integer"}}},
    }
    ref_schema = {"$ref": "#/$defs/Status"}
    assert _format_type(ref_schema, defs) == "Literal['active', 'inactive']"


def test_build_tool_spec_and_help_format():
    tool_dict = {
        "name": "decompile-function",
        "description": "Decompile a function into pseudo-C.",
        "inputSchema": {
            "type": "object",
            "required": ["binary-name", "address"],
            "properties": {
                "binary-name": {"type": "string", "description": "Binary name"},
                "address": {"type": "string", "description": "Hex address"},
                "opt-flag": {"type": "boolean", "default": False},
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
        },
    }

    spec = _build_tool_spec(tool_dict, "decompile_function")
    assert spec.py_name == "decompile_function"
    assert spec.wire_name == "decompile-function"
    assert spec.wraps_result is True
    assert spec.return_type == "str"
    assert spec.py_to_wire_params["binary_name"] == "binary-name"
    assert spec.py_to_wire_params["opt_flag"] == "opt-flag"

    help_str = _format_help({"name": "test-mcp", "version": "1.0"}, [spec])
    assert "Server: test-mcp (1.0) — 1 tool" in help_str
    assert (
        "decompile_function(*, address: str, binary_name: str, opt_flag: bool = False) -> str"
        in help_str
    )
    assert "Decompile a function into pseudo-C." in help_str
    assert "binary_name: Binary name" in help_str


def test_unwrap_result_rpc_error():
    resp = {"error": {"code": -32601, "message": "Method not found"}}
    with pytest.raises(SansMCPError, match="RPC error calling 'test': Method not found"):
        _unwrap_result("test", resp)


def test_unwrap_result_tool_is_error():
    resp = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "File not found: /tmp/none"}],
        }
    }
    with pytest.raises(SansMCPError, match="Tool 'test' failed: File not found: /tmp/none"):
        _unwrap_result("test", resp)


def test_unwrap_result_structured_content():
    # Direct dict
    resp = {"result": {"structuredContent": {"foo": "bar"}}}
    assert _unwrap_result("test", resp) == {"foo": "bar"}

    # Wrapped FastMCP result
    resp_wrapped = {"result": {"structuredContent": {"result": [1, 2, 3]}}}
    assert _unwrap_result("test", resp_wrapped, wraps_result=True) == [1, 2, 3]

    # Unwrapped dict with a "result" key when wraps_result is False
    resp_unwrapped_dict = {"result": {"structuredContent": {"result": {"status": "ok"}}}}
    assert _unwrap_result("test", resp_unwrapped_dict, wraps_result=False) == {
        "result": {"status": "ok"}
    }


def test_unwrap_result_content_fallback():
    # Empty content
    assert _unwrap_result("test", {"result": {"content": []}}) is None

    # Single text string
    resp_str = {"result": {"content": [{"type": "text", "text": "Hello world"}]}}
    assert _unwrap_result("test", resp_str) == "Hello world"

    # Single scalar numeric string preserved as string
    resp_num_str = {"result": {"content": [{"type": "text", "text": "123"}]}}
    assert _unwrap_result("test", resp_num_str) == "123"

    # Single JSON object in text parsed to dict
    resp_json = {"result": {"content": [{"type": "text", "text": '{"key": "value"}'}]}}
    assert _unwrap_result("test", resp_json) == {"key": "value"}

    # Single JSON array in text parsed to list
    resp_arr = {"result": {"content": [{"type": "text", "text": '[1, "two", 3]'}]}}
    assert _unwrap_result("test", resp_arr) == [1, "two", 3]

    # Multiple items
    resp_multi = {
        "result": {
            "content": [
                {"type": "text", "text": "line1"},
                {"type": "text", "text": '{"a": 1}'},
            ]
        }
    }
    assert _unwrap_result("test", resp_multi) == ["line1", {"a": 1}]


class MockTransport:
    def __init__(self):
        self.calls = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "initialize":
            return {"result": {"serverInfo": {"name": "mock-server", "version": "0.1"}}}
        if method == "tools/list":
            return {
                "result": {
                    "tools": [
                        {
                            "name": "calc-sum",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["a", "b"],
                                "properties": {
                                    "a": {"type": "integer"},
                                    "b": {"type": "integer"},
                                },
                            },
                        }
                    ]
                }
            }
        if method == "tools/call":
            args = params.get("arguments", {})
            return {
                "result": {
                    "content": [{"type": "text", "text": str(args.get("a", 0) + args.get("b", 0))}]
                }
            }
        return {"result": {}}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append(("notify:" + method, params))

    def close(self) -> None:
        pass


def test_sans_mcp_client_validation():
    mock = MockTransport()
    m = SansMCP(mock)

    # 1. Calling tool directly on m raises helpful AttributeError
    with pytest.raises(AttributeError, match=r"To call an MCP tool, use 'm\.tools\.calc_sum"):
        _ = m.calc_sum

    # 2. Unknown attribute raises standard AttributeError
    with pytest.raises(AttributeError, match="'SansMCP' object has no attribute 'non_existent'"):
        _ = m.non_existent

    # 3. Positional argument guard
    with pytest.raises(TypeError, match="only accepts keyword arguments"):
        m.tools.calc_sum(1, 2)

    # 4. Missing required argument guard
    with pytest.raises(TypeError, match="missing required argument"):
        m.tools.calc_sum(a=1)

    # 5. Unknown argument guard (closed schema)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        m.tools.calc_sum(a=1, b=2, unexpected=True)

    # 6. Correct call
    res = m.tools.calc_sum(a=5, b=7)
    assert res == "12"


@pytest.mark.parametrize("additional", [None, True, {"type": "integer"}])
def test_client_accepts_additional_arguments_and_nullable_types(additional):
    class FlexibleTransport(MockTransport):
        def request(self, method, params=None):
            if method == "tools/list":
                schema = {
                    "type": "object",
                    "properties": {"value": {"type": ["string", "null"]}},
                }
                if additional is not None:
                    schema["additionalProperties"] = additional
                return {"result": {"tools": [{"name": "flexible", "inputSchema": schema}]}}
            if method == "tools/call":
                return {"result": {"structuredContent": params["arguments"]}}
            return super().request(method, params)

    with SansMCP(FlexibleTransport()) as client:
        assert "value: str | None" in client.help("flexible")
        assert client.tools.flexible(value=None, extra=42) == {"value": None, "extra": 42}


def test_format_type_type_arrays():
    assert _format_type({"type": ["string", "integer"]}, {}) == "str | int"
    assert _format_type({"type": ["null", "string"]}, {}) == "str | None"
    assert _format_type({"type": ["null"]}, {}) == "None"
    assert _format_type({"type": ["array", "null"], "items": {"type": "integer"}}, {}) == (
        "list[int] | None"
    )


MOCK_SERVER_SCRIPT = str(pathlib.Path(__file__).parent / "mock_server.py")
MOCK_SERVER_CMD = [sys.executable, MOCK_SERVER_SCRIPT]


def test_stdio_transport_end_to_end():
    with connect(MOCK_SERVER_CMD) as m:
        # Check tools loaded with pagination
        assert "greet_user" in m.tools._tools
        assert "list_items" in m.tools._tools
        assert "get_item" in m.tools._tools
        assert "get_info" in m.tools._tools

        # Call greet_user
        greeting = m.tools.greet_user(user_name="Alice")
        assert greeting == "Hello, Alice!"

        # Call list_items with filter
        items = m.tools.list_items(category="hardware")
        assert len(items) == 2
        assert items[0]["id"] == "item-1"

        # Call get_item
        item = m.tools.get_item(item_id="item-1")
        assert item == {"id": "item-1", "name": "Widget", "status": "active"}

        # Call get_info (unwraps json text)
        info = m.tools.get_info()
        assert info == {"os": "mock", "version": 42}

        # Check help formatting
        help_text = m.help()
        assert "Server: dummy-stdio-server (1.2.3) — 4 tools" in help_text
        assert "greet_user(*, user_name: str) -> str" in help_text
        assert "list_items(*, category: str = None) -> list[dict]" in help_text
        assert "Returns:\n      list[{id: str, name: str, category: str}]" in help_text
        assert "get_item(*, item_id: str) -> dict" in help_text
        assert "Returns:\n      {id: str, name: str, status: str}" in help_text


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_proxy_and_http_client():
    port = find_free_port()
    gateway_cmd = [
        sys.executable,
        "-m",
        "sans_mcp",
        "proxy",
        "--listen",
        f":{port}",
        "--",
        *MOCK_SERVER_CMD,
    ]

    proc = subprocess.Popen(gateway_cmd)
    try:
        url = f"http://127.0.0.1:{port}/mcp"

        # Wait until server is up and responsive
        started = False
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    started = True
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)

        assert started, "Gateway failed to start in time"

        # Connect via HTTP client
        with connect(url) as client:
            assert "greet_user" in client.tools._tools
            assert "list_items" in client.tools._tools
            assert "get_item" in client.tools._tools
            assert "get_info" in client.tools._tools

            res = client.tools.greet_user(user_name="Bob")
            assert res == "Hello, Bob!"

            items = client.tools.list_items(category="hardware")
            assert len(items) == 2

            item = client.tools.get_item(item_id="item-1")
            assert item == {"id": "item-1", "name": "Widget", "status": "active"}

            info = client.tools.get_info()
            assert info == {"os": "mock", "version": 42}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_cli_help_subcommand(capsys):
    cmd_str = shlex.join(MOCK_SERVER_CMD)
    sans_mcp.main(["help", cmd_str])
    captured = capsys.readouterr()
    assert "Server: dummy-stdio-server (1.2.3) — 4 tools" in captured.out
    assert "greet_user(*, user_name: str)" in captured.out


def test_help_single_tool_by_wire_and_py_name():
    with connect(MOCK_SERVER_CMD) as m:
        # Lookup by normalized Python name
        h1 = m.help("greet_user")
        assert "greet_user(*, user_name: str)" in h1
        assert "Greet a user by name" in h1
        assert "Server:" not in h1

        # Lookup by raw wire name
        h2 = m.help("greet-user")
        assert h2 == h1

        # Non-existent tool
        with pytest.raises(SansMCPError, match="Tool 'unknown' not found"):
            m.help("unknown")


def test_cli_help_single_tool(capsys):
    cmd_str = shlex.join(MOCK_SERVER_CMD)
    sans_mcp.main(["help", cmd_str, "greet_user"])
    captured = capsys.readouterr()
    assert "greet_user(*, user_name: str)" in captured.out
    assert "Server:" not in captured.out


def test_http_legacy_sse_error_diagnostic(monkeypatch, tmp_path):
    import urllib.error
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    transport = sans_mcp._HttpTransport("http://127.0.0.1:9999/sse")
    mock_err = urllib.error.HTTPError(
        "http://127.0.0.1:9999/sse",
        405,
        "Method Not Allowed",
        hdrs={},
        fp=MagicMock(read=lambda: b"Cannot POST /sse"),
    )

    with (
        patch.object(transport._opener, "open", side_effect=mock_err),
        pytest.raises(SansMCPError, match="Server may require legacy MCP SSE transport"),
    ):
        transport.request("initialize", {})


def test_parse_listen_address():
    assert sans_mcp._parse_listen_address(":8001") == ("127.0.0.1", 8001)
    assert sans_mcp._parse_listen_address("127.0.0.1:8001") == ("127.0.0.1", 8001)
    assert sans_mcp._parse_listen_address("0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert sans_mcp._parse_listen_address("[::1]:8001") == ("::1", 8001)

    for invalid in ["8001", "localhost", ":abc", ":0", ":65536", ""]:
        with pytest.raises(SansMCPError):
            sans_mcp._parse_listen_address(invalid)


def test_cli_proxy_errors(capsys):
    with pytest.raises(SystemExit) as exc_info:
        sans_mcp.main(["proxy", "--listen", "invalid_addr", "--", "echo", "hi"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid listen address" in captured.err

    with pytest.raises(SystemExit) as exc_info:
        sans_mcp.main(["proxy"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "proxy requires a command" in captured.err


def test_cli_proxy_default_listen(monkeypatch):
    captured_args = {}

    def fake_run(host, port, cmd):
        captured_args["host"] = host
        captured_args["port"] = port
        captured_args["cmd"] = cmd

    monkeypatch.setattr(sans_mcp, "_run_proxy", fake_run)
    sans_mcp.main(["proxy", "--", "echo", "hi"])
    assert captured_args == {"host": "127.0.0.1", "port": 6270, "cmd": ["echo", "hi"]}


def test_unwrap_result_edge_cases():
    # Non-dict result returned directly
    assert _unwrap_result("t", {"result": 42}) == 42

    # isError with non-text content item
    with pytest.raises(SansMCPError, match=r"Tool 't' failed: \{'type': 'image'\}"):
        _unwrap_result("t", {"result": {"isError": True, "content": [{"type": "image"}]}})

    # Non-list content returned directly
    assert _unwrap_result("t", {"result": {"content": "raw-content"}}) == "raw-content"

    # Invalid JSON starting with '{' stays string; non-text item stays dict
    res = _unwrap_result(
        "t",
        {
            "result": {
                "content": [
                    {"type": "text", "text": "{invalid json"},
                    {"type": "image", "data": "abc"},
                ]
            }
        },
    )
    assert res == ["{invalid json", {"type": "image", "data": "abc"}]


def test_output_schema_object_and_array_previews_with_refs():
    assert sans_mcp._format_type({"type": "unknown_type"}, {}) == "Any"
    assert sans_mcp._format_object_fields_preview({"type": "object"}, {}) == "dict"

    # Wrapped array of $ref objects
    tool_arr = {
        "name": "list_users",
        "outputSchema": {
            "$defs": {
                "User": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                }
            },
            "type": "object",
            "properties": {"result": {"type": "array", "items": {"$ref": "#/$defs/User"}}},
        },
    }
    spec_arr = _build_tool_spec(tool_arr, "list_users")
    assert spec_arr.return_type == "list[dict]"
    assert "Returns:\n      list[{id: int, name: str}]" in spec_arr.help_text

    # Direct $ref object outputSchema
    tool_obj = {
        "name": "get_user",
        "outputSchema": {
            "$defs": {
                "User": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                }
            },
            "$ref": "#/$defs/User",
        },
    }
    spec_obj = _build_tool_spec(tool_obj, "get_user")
    assert spec_obj.return_type == "dict"
    assert "Returns:\n      {id: int}" in spec_obj.help_text


def test_stdio_transport_ping_notifications_timeout_and_errors():
    # 1. Invalid executable raises SansMCPError
    with pytest.raises(SansMCPError, match="Failed to spawn stdio MCP server"):
        sans_mcp._StdioTransport(["/nonexistent_binary_xyz_12345"])

    # 2. Child emits blank line, invalid JSON, non-dict JSON, ping, and list_changed before reply
    script = (
        "import json, sys\n"
        "req = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write('\\nnot-json\\n[1,2]\\n')\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':77,'method':'ping'}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','method':'notifications/tools/list_changed'}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "pong = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'pong_id':pong['id']}}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    changed = []
    t = sans_mcp._StdioTransport(
        [sys.executable, "-c", script], on_list_changed=lambda: changed.append(True)
    )
    try:
        res = t.request("test_method")
        assert res["result"] == {"pong_id": 77}
        assert changed == [True]
    finally:
        t.close()
        # Second close is a no-op
        t.close()

    with pytest.raises(SansMCPError, match="Transport is closed"):
        t.request("any")
    with pytest.raises(SansMCPError, match="Transport is closed"):
        t.notify("any")

    # 3. Timeout waiting for response
    t_slow = sans_mcp._StdioTransport(
        [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05
    )
    try:
        with pytest.raises(SansMCPError, match="timed out"):
            t_slow.request("slow_method")
    finally:
        t_slow.close()

    # 4. Child exits immediately without replying (EOF)
    t_eof = sans_mcp._StdioTransport([sys.executable, "-c", "import sys; sys.exit(2)"])
    try:
        t_eof._process.wait(timeout=2)
        with pytest.raises(SansMCPError, match="Subprocess terminated unexpectedly"):
            t_eof.request("eof_method")
    finally:
        t_eof.close()

    # 5. Child exits after reading stdin (EOF in queue)
    t_eof2 = sans_mcp._StdioTransport([sys.executable, "-c", "import sys; sys.stdin.readline()"])
    try:
        with pytest.raises(SansMCPError, match="Subprocess terminated \\(exit code 0\\)"):
            t_eof2.request("eof2")
    finally:
        t_eof2.close()


def test_stdio_transport_write_error_and_kill_fallback():
    from unittest.mock import patch

    t = sans_mcp._StdioTransport([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        with (
            patch.object(t._process.stdin, "write", side_effect=BrokenPipeError("broken")),
            pytest.raises(SansMCPError, match="Error writing to stdio process"),
        ):
            t.notify("test")

        # Simulate terminate() timing out so kill() fallback runs
        orig_wait = t._process.wait
        calls = []

        def flaky_wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(t.command, timeout)
            return orig_wait(timeout=timeout)

        with patch.object(t._process, "wait", side_effect=flaky_wait):
            t.close()
        assert len(calls) >= 2
    finally:
        t.close()


def test_http_transport_sse_and_network_errors(monkeypatch, tmp_path):
    import http.client
    import io
    import urllib.error
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    transport = sans_mcp._HttpTransport("http://127.0.0.1:9999/mcp")
    transport.close()

    # SSE response parsing (with invalid chunk, other id, and matching id at EOF)
    sse_stream = io.BytesIO(
        b": comment\n"
        b"data: not-json\n\n"
        b'data: {"jsonrpc": "2.0", "id": 999, "result": {}}\n\n'
        b'data: {"jsonrpc": "2.0",\n'
        b'data: "id": 1, "result": {"ok": true}}'
    )
    sse_Stream_resp = MagicMock()
    sse_Stream_resp.__enter__.return_value = sse_Stream_resp
    sse_Stream_resp.headers = {"Content-Type": "text/event-stream", "mcp-session-id": "sess-1"}
    sse_Stream_resp.__iter__.side_effect = lambda: iter(sse_stream.readlines())

    with patch.object(transport._opener, "open", return_value=sse_Stream_resp):
        res = transport.request("test_sse")
        assert res == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        assert transport.session_id == "sess-1"

    # SSE stream ending without matching ID raises SansMCPError
    empty_sse = io.BytesIO(b"data: {}\n\n")
    with (
        pytest.raises(SansMCPError, match="SSE stream ended without response"),
    ):
        transport._parse_sse_response(empty_sse.readlines(), target_id=42)

    # Empty JSON body returns empty result dict
    empty_json_resp = MagicMock()
    empty_json_resp.__enter__.return_value = empty_json_resp
    empty_json_resp.headers = {"Content-Type": "application/json"}
    empty_json_resp.read.return_value = b""
    with patch.object(transport._opener, "open", return_value=empty_json_resp):
        res2 = transport.request("empty_body")
        assert res2["result"] == {}

    # URLError and RemoteDisconnected are wrapped in SansMCPError
    with (
        patch.object(
            transport._opener, "open", side_effect=urllib.error.URLError("Connection refused")
        ),
        pytest.raises(SansMCPError, match="Failed to connect"),
    ):
        transport.request("fail")

    with (
        patch.object(
            transport._opener,
            "open",
            side_effect=http.client.RemoteDisconnected("Remote closed"),
        ),
        pytest.raises(SansMCPError, match="Error communicating with"),
    ):
        transport.request("fail2")


def test_client_dir_unknown_tool_and_failed_init():
    mock = MockTransport()
    m = SansMCP(mock)
    assert dir(m.tools) == ["calc_sum"]

    with pytest.raises(AttributeError, match="Unknown MCP tool 'missing_tool'"):
        _ = m.tools.missing_tool

    with pytest.raises(SansMCPError, match="Empty target command specified"):
        connect("")

    # Failed initialize closes transport
    class FailingInitTransport:
        def __init__(self):
            self.closed = False

        def request(self, method, params=None):
            return {"error": "init boom"}

        def close(self):
            self.closed = True

    ft = FailingInitTransport()
    with pytest.raises(SansMCPError, match="Initialization handshake failed"):
        SansMCP(ft)
    assert ft.closed is True

    # Failed tools/list closes transport
    class FailingListTransport:
        def __init__(self):
            self.closed = False

        def request(self, method, params=None):
            if method == "initialize":
                return {"result": {}}
            return {"error": "list boom"}

        def notify(self, method, params=None):
            pass

        def close(self):
            self.closed = True

    flt = FailingListTransport()
    with pytest.raises(SansMCPError, match="Failed to list tools"):
        SansMCP(flt)
    assert flt.closed is True


def test_proxy_server_in_process():
    import json
    import threading
    import urllib.error
    import urllib.request

    class ProxyMockTransport:
        def __init__(self):
            self.on_list_changed = None
            self.tool_count = 1
            self.notified = []

        def request(self, method, params=None):
            if method == "initialize":
                return {"result": {"serverInfo": {"name": "proxy-mock", "version": "1.0"}}}
            if method == "tools/list":
                return {
                    "result": {
                        "tools": [
                            {"name": f"t{i}", "inputSchema": {"type": "object"}}
                            for i in range(self.tool_count)
                        ]
                    }
                }
            if method == "explode":
                raise SansMCPError("boom from child")
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": f"called:{method}"}]},
            }

        def notify(self, method, params=None):
            self.notified.append(method)

        def close(self):
            pass

    pt = ProxyMockTransport()
    httpd, _ = sans_mcp._create_proxy_server("127.0.0.1", 0, pt)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/mcp"

    try:
        # 1. GET status
        with urllib.request.urlopen(base_url) as resp:
            status = json.loads(resp.read().decode("utf-8"))
            assert status["status"] == "ok"
            assert status["tools_count"] == 1

        # 2. Origin header rejected with 403
        req_origin = urllib.request.Request(
            base_url,
            data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
            headers={"Origin": "http://evil.example.com", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_origin)
        assert exc_info.value.code == 403

        # 3. Invalid JSON -> 400
        req_bad_json = urllib.request.Request(base_url, data=b"{bad", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_bad_json)
        assert exc_info.value.code == 400

        # 4. Non-dict or missing method -> 400
        req_no_method = urllib.request.Request(base_url, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_no_method)
        assert exc_info.value.code == 400

        # 5. Notification forwarding -> 202
        req_notif = urllib.request.Request(
            base_url,
            data=b'{"jsonrpc":"2.0","method":"notifications/cancelled"}',
            method="POST",
        )
        with urllib.request.urlopen(req_notif) as resp:
            assert resp.status == 202
        assert "notifications/cancelled" in pt.notified

        # 6. Ping and error forwarding (-32603)
        req_ping = urllib.request.Request(
            base_url, data=b'{"jsonrpc":"2.0","id":5,"method":"ping"}', method="POST"
        )
        with urllib.request.urlopen(req_ping) as resp:
            assert json.loads(resp.read().decode("utf-8")) == {
                "jsonrpc": "2.0",
                "id": 5,
                "result": {},
            }

        req_err = urllib.request.Request(
            base_url, data=b'{"jsonrpc":"2.0","id":6,"method":"explode"}', method="POST"
        )
        with urllib.request.urlopen(req_err) as resp:
            err_data = json.loads(resp.read().decode("utf-8"))
            assert err_data["error"]["code"] == -32603
            assert "boom from child" in err_data["error"]["message"]

        # 7. on_list_changed cache invalidation and client connect + tool call without deadlock
        pt.tool_count = 3
        pt.on_list_changed()
        with connect(base_url) as client:
            assert len(client.tools._tools) == 3
            assert client.tools.t0() == "called:tools/call"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_run_proxy_lifecycle_and_cli_error_paths(monkeypatch, capsys):
    import runpy

    # 1. _run_proxy startup failure exits with code 1
    with pytest.raises(SystemExit) as exc_info:
        sans_mcp._run_proxy("127.0.0.1", 0, [sys.executable, "-c", "import sys; sys.exit(1)"])
    assert exc_info.value.code == 1

    # 2. _run_proxy normal startup and signal shutdown
    class FakeHTTPD:
        def __init__(self):
            self.closed = False

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            self.closed = True

    fake_httpd = FakeHTTPD()
    registered_signals = {}
    monkeypatch.setattr(
        sans_mcp,
        "_create_proxy_server",
        lambda h, p, t: (fake_httpd, {"tools": []}),
    )
    monkeypatch.setattr(
        sans_mcp.signal,
        "signal",
        lambda sig, handler: registered_signals.setdefault(sig, handler),
    )
    sans_mcp._run_proxy("127.0.0.1", 0, MOCK_SERVER_CMD)
    assert fake_httpd.closed is True
    for handler in registered_signals.values():
        handler(sans_mcp.signal.SIGINT, None)

    # 3. CLI help failure exits with code 1, and __main__ entrypoint via runpy
    monkeypatch.setattr(sys, "argv", ["sans-mcp", "help", "/nonexistent_cmd_xyz"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(sans_mcp.__file__, run_name="__main__")
    assert exc_info.value.code == 1


@pytest.mark.parametrize(
    ("method", "params", "operation"),
    [
        ("initialize", {}, "calling 'initialize'"),
        ("tools/list", {}, "calling 'tools/list'"),
        (
            "tools/call",
            {"name": "take_snapshot", "arguments": {"pageId": 2}},
            "calling 'tools/call'",
        ),
    ],
)
def test_http_server_error_context(monkeypatch, tmp_path, method, params, operation):
    import io
    import urllib.error
    from unittest.mock import patch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    url = "http://127.0.0.1:5420/devtools-mcp"
    body = '{"jsonrpc":"2.0","error":{"code":-32603,"message":"Internal server error"},"id":null}'
    error = urllib.error.HTTPError(url, 500, "Internal Server Error", {}, io.BytesIO(body.encode()))
    transport = sans_mcp._HttpTransport(url)
    with (
        patch.object(transport._opener, "open", side_effect=error),
        pytest.raises(SansMCPError) as exc,
    ):
        transport.request(method, params)
    assert str(exc.value) == f"HTTP 500 from '{url}' {operation}: {body}"
    assert exc.value.__cause__ is error


@pytest.mark.parametrize("failure", ["connect", "disconnect", "json"])
def test_http_communication_error_context(monkeypatch, tmp_path, failure):
    import http.client
    import json
    import urllib.error
    from unittest.mock import patch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    url = "http://127.0.0.1:5420/devtools-mcp"
    errors = {
        "connect": urllib.error.URLError("Connection refused"),
        "disconnect": http.client.RemoteDisconnected("Remote closed"),
        "json": json.JSONDecodeError("Invalid JSON", "x", 0),
    }
    transport = sans_mcp._HttpTransport(url)
    with (
        patch.object(transport._opener, "open", side_effect=errors[failure]),
        pytest.raises(SansMCPError) as exc,
    ):
        transport.request("tools/call", {"name": "take_snapshot"})
    assert url in str(exc.value)
    assert "calling 'tools/call'" in str(exc.value)
    assert exc.value.__cause__ is errors[failure]


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 500 from 'http://localhost/mcp' calling 'tools/call': Internal server error",
        "Failed to connect to 'http://localhost/mcp' calling 'tools/call': Connection refused",
        "Subprocess terminated unexpectedly",
    ],
)
def test_tool_transport_error_context(message):
    from unittest.mock import patch

    transport = MockTransport()
    client = SansMCP(transport)
    error = SansMCPError(message)
    with (
        patch.object(transport, "request", side_effect=error),
        pytest.raises(SansMCPError) as exc,
    ):
        client.tools.calc_sum(a=1, b=2)
    assert str(exc.value) == f"Tool 'calc-sum' failed: {message}"
    assert exc.value.__cause__ is error

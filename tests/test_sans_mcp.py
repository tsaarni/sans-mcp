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
        assert "greet_user(*, user_name: str)" in help_text
        assert "list_items(*, category: str = None)" in help_text


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


def test_http_legacy_sse_error_diagnostic():
    import urllib.error
    from unittest.mock import MagicMock, patch

    transport = sans_mcp._HttpTransport("http://127.0.0.1:9999/sse")
    mock_err = urllib.error.HTTPError(
        "http://127.0.0.1:9999/sse",
        405,
        "Method Not Allowed",
        hdrs={},
        fp=MagicMock(read=lambda: b"Cannot POST /sse"),
    )

    with (
        patch("urllib.request.urlopen", side_effect=mock_err),
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

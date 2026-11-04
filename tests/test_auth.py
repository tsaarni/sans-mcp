import http.server
import json
import pathlib
import sys
import threading
import urllib.request
from typing import Any

import pytest

import sans_mcp
from sans_mcp import SansMCPError, connect


def _write_auth_config(tmp_path: pathlib.Path, content: str, mode: int = 0o600) -> pathlib.Path:
    auth_dir = tmp_path / "sans-mcp"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file = auth_dir / "auth.json"
    if auth_file.exists():
        auth_file.chmod(0o600)
    auth_file.write_text(content, encoding="utf-8")
    auth_file.chmod(mode)
    return auth_file


def test_auth_config_resolution_and_env_interpolation(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-token-123")
    monkeypatch.setenv("CF_ID", "client-id-abc")
    monkeypatch.setenv("CF_SECRET", "client-secret-xyz")

    config = {
        "MCP.Example.COM": {
            "Authorization": "Bearer ${TEST_MCP_TOKEN}",
            "X-Compound": "${CF_ID}:${CF_SECRET}",
            "X-Static": "literal-value",
        },
        "mcp.example.com:8443": {"Authorization": "Bearer port-specific-token"},
        "[::1]:8080": {"X-Local-Token": "ipv6-token"},
    }
    _write_auth_config(tmp_path, json.dumps(config))

    # Case-insensitive host matching, multiple headers, and multi-var interpolation
    t1 = sans_mcp._HttpTransport("https://mcp.example.com/mcp")
    assert t1.custom_headers == {
        "Authorization": "Bearer secret-token-123",
        "X-Compound": "client-id-abc:client-secret-xyz",
        "X-Static": "literal-value",
    }

    # Port-specific entry takes precedence over hostname-only entry
    t2 = sans_mcp._HttpTransport("https://mcp.example.com:8443/mcp")
    assert t2.custom_headers == {"Authorization": "Bearer port-specific-token"}

    # Explicit headers override auth.json case-insensitively while preserving other headers
    t3 = sans_mcp._HttpTransport(
        "https://mcp.example.com/mcp", headers={"authorization": "Bearer override"}
    )
    assert t3.custom_headers == {
        "authorization": "Bearer override",
        "X-Compound": "client-id-abc:client-secret-xyz",
        "X-Static": "literal-value",
    }

    # IPv6 loopback over HTTP is permitted
    t4 = sans_mcp._HttpTransport("http://[::1]:8080/mcp")
    assert t4.custom_headers == {"X-Local-Token": "ipv6-token"}

    # Unmatched or empty host gets empty headers
    t5 = sans_mcp._HttpTransport("https://other.example.com/mcp")
    assert t5.custom_headers == {}
    assert sans_mcp._load_auth_headers("https:///mcp") == {}


def test_auth_config_missing_or_empty_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("MISSING_MCP_SECRET", raising=False)

    config = {"mcp.example.com": {"Authorization": "Bearer ${MISSING_MCP_SECRET}"}}
    _write_auth_config(tmp_path, json.dumps(config))

    with pytest.raises(
        SansMCPError,
        match=r"Environment variable 'MISSING_MCP_SECRET' referenced in .* is not set",
    ):
        sans_mcp._HttpTransport("https://mcp.example.com/mcp")

    # Empty string env var is also rejected
    monkeypatch.setenv("MISSING_MCP_SECRET", "")
    with pytest.raises(
        SansMCPError,
        match=r"Environment variable 'MISSING_MCP_SECRET' referenced in .* is not set",
    ):
        sans_mcp._HttpTransport("https://mcp.example.com/mcp")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permission check")
def test_auth_config_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    payload = '{"mcp.example.com": {"Authorization": "Bearer tok"}}'

    # 0o644 (world-readable) and 0o640 (group-readable) are rejected
    for bad_mode in (0o644, 0o640, 0o620):
        _write_auth_config(tmp_path, payload, mode=bad_mode)
        with pytest.raises(SansMCPError, match=f"Insecure permissions {oct(bad_mode)}"):
            sans_mcp._HttpTransport("https://mcp.example.com/mcp")

    # 0o400 (read-only owner) is permitted
    _write_auth_config(tmp_path, payload, mode=0o400)
    t = sans_mcp._HttpTransport("https://mcp.example.com/mcp")
    assert t.custom_headers == {"Authorization": "Bearer tok"}
    _write_auth_config(tmp_path, payload, mode=0o600)


def test_auth_config_invalid_json_and_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    _write_auth_config(tmp_path, "{not-valid-json")
    with pytest.raises(SansMCPError, match="Failed to parse auth config"):
        sans_mcp._HttpTransport("https://mcp.example.com/mcp")

    _write_auth_config(tmp_path, '["not", "a", "dict"]')
    with pytest.raises(SansMCPError, match="top-level value must be a JSON object"):
        sans_mcp._HttpTransport("https://mcp.example.com/mcp")

    _write_auth_config(tmp_path, '{"mcp.example.com": "not-a-dict"}')
    with pytest.raises(
        SansMCPError, match="expected an object mapping header names to string values"
    ):
        sans_mcp._HttpTransport("https://mcp.example.com/mcp")


def test_http_transport_rejects_cleartext_auth_on_remote_host(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_auth_config(tmp_path, '{"remote.example.com": {"Authorization": "Bearer tok"}}')

    with pytest.raises(
        SansMCPError,
        match=r"Refusing to send authentication headers over plaintext HTTP to non-loopback host 'remote\.example\.com'",
    ):
        sans_mcp._HttpTransport("http://remote.example.com/mcp")

    # Loopback over plaintext HTTP is allowed
    t = sans_mcp._HttpTransport(
        "http://127.0.0.1:8000/mcp", headers={"Authorization": "Bearer local-tok"}
    )
    assert t.custom_headers == {"Authorization": "Bearer local-tok"}


def test_safe_redirect_handler():
    handler = sans_mcp._SafeRedirectHandler()

    # 1. Same-origin redirect preserves Authorization and custom headers
    req_same = urllib.request.Request(
        "https://mcp.example.com/v1/mcp",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
            "X-Custom-Auth": "custom-secret",
        },
        method="POST",
    )
    redirected_same = handler.redirect_request(
        req_same, None, 307, "Temporary Redirect", {}, "https://mcp.example.com/v2/mcp"
    )
    assert redirected_same is not None
    same_hdrs = {k.lower(): v for k, v in redirected_same.headers.items()}
    assert same_hdrs.get("authorization") == "Bearer secret"
    assert same_hdrs.get("x-custom-auth") == "custom-secret"

    # 2. Cross-origin redirect (307 and 302, plus cross-port) strips Authorization and custom headers
    for code, target_url in (
        (307, "https://other.example.com/mcp"),
        (302, "https://other.example.com/mcp"),
        (307, "https://mcp.example.com:8443/mcp"),
    ):
        redirected_cross = handler.redirect_request(
            req_same, None, code, "Redirect", {}, target_url
        )
        assert redirected_cross is not None
        cross_hdrs = {
            k.lower(): v
            for k, v in (
                *redirected_cross.headers.items(),
                *redirected_cross.unredirected_hdrs.items(),
            )
        }
        assert "authorization" not in cross_hdrs
        assert "x-custom-auth" not in cross_hdrs

    # 3. HTTPS to HTTP downgrade is blocked
    with pytest.raises(SansMCPError, match="Refusing redirect from HTTPS"):
        handler.redirect_request(
            req_same, None, 307, "Temporary Redirect", {}, "http://mcp.example.com/v2/mcp"
        )


def test_http_auth_config_end_to_end(monkeypatch, tmp_path, capsys):
    received_headers: list[tuple[str | None, str | None]] = []

    class AuthServerHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)
            auth = self.headers.get("Authorization")
            api_key = self.headers.get("X-API-Key")
            received_headers.append((auth, api_key))
            if auth != "Bearer e2e-secret-token" or api_key != "custom-key-456":
                err = b"Unauthorized"
                self.send_response(401)
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            body = json.loads(raw_body.decode("utf-8"))
            req_id = body.get("id")
            method = body.get("method")

            if "id" not in body:
                self.send_response(202)
                self.end_headers()
                return

            if method == "initialize":
                res = {"serverInfo": {"name": "auth-server", "version": "1.0"}}
            elif method == "tools/list":
                res = {"tools": [{"name": "ping_auth", "inputSchema": {"type": "object"}}]}
            else:
                res = {"content": [{"type": "text", "text": "authenticated-ok"}]}

            payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": res}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AuthServerHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        url = f"http://127.0.0.1:{port}/mcp"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        # Without auth.json -> 401 Unauthorized
        with pytest.raises(SansMCPError, match="HTTP 401"):
            connect(url)

        # With auth.json + env var -> succeeds transparently in both connect() and CLI help
        monkeypatch.setenv("E2E_TOKEN", "e2e-secret-token")
        _write_auth_config(
            tmp_path,
            json.dumps(
                {
                    f"127.0.0.1:{port}": {
                        "Authorization": "Bearer ${E2E_TOKEN}",
                        "X-API-Key": "custom-key-456",
                    }
                }
            ),
        )

        with connect(url) as m:
            assert m.tools.ping_auth() == "authenticated-ok"
        assert received_headers[-1] == ("Bearer e2e-secret-token", "custom-key-456")

        sans_mcp.main(["help", url])
        captured = capsys.readouterr()
        assert "Server: auth-server (1.0) — 1 tool" in captured.out
        assert "ping_auth()" in captured.out
    finally:
        server.shutdown()
        server.server_close()


def test_http_error_when_body_read_resets(monkeypatch, tmp_path):
    import urllib.error
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    transport = sans_mcp._HttpTransport("http://127.0.0.1:9999/mcp")

    def raise_reset():
        raise ConnectionResetError(54, "Connection reset by peer")

    mock_err = urllib.error.HTTPError(
        "http://127.0.0.1:9999/mcp",
        401,
        "Unauthorized",
        hdrs={},
        fp=MagicMock(read=raise_reset),
    )

    with (
        patch.object(transport._opener, "open", side_effect=mock_err),
        pytest.raises(SansMCPError, match=r"HTTP 401 from .* calling 'initialize': Unauthorized"),
    ):
        transport.request("initialize", {})

    with (
        patch.object(transport._opener, "open", side_effect=mock_err),
        pytest.raises(
            SansMCPError,
            match=r"HTTP 401 from .* in notification 'notifications/initialized': Unauthorized",
        ),
    ):
        transport.notify("notifications/initialized", {})

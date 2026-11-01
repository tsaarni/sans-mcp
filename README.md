![sans-mcp](assets/sans-mcp-logo-light.png#gh-light-mode-only)
![sans-mcp](assets/sans-mcp-logo-dark.png#gh-dark-mode-only)

`sans-mcp` is a Python module that lets coding agents use Model Context Protocol (MCP) servers through ad-hoc Python scripts, without configuring an MCP client.

- **On-demand discovery:** The coding agent can discover tool descriptions on demand, without polluting the context before they are needed.
- **In-memory data processing:** The coding agent can process responses and chain multiple tool calls in a single Python script all in one go, without requiring an LLM round-trip for each step.
- **No agent configuration:** Connects directly to any MCP server from Python scripts without configuring client settings or restarting the agent.
- **Stdio-to-HTTP proxy:** Converts stdio MCP servers to Streamable HTTP endpoints.
- **Single-file module with no dependencies:** Uses only the Python standard library.

![sans-mcp architecture diagram](assets/sans-mcp-diagram-light.png#gh-light-mode-only)
![sans-mcp architecture diagram](assets/sans-mcp-diagram-dark.png#gh-dark-mode-only)

## Usage

### Discover Tools

Call `m.help()` to print tool signatures and documentation formatted as Python function signatures:

```python
import sans_mcp

with sans_mcp.connect("python3 tests/mock_server.py") as m:
    print(m.help())
    # Or inspect a specific tool:
    # print(m.help("list_items"))
```

From the command line, run:

```bash
# List all tools (stdio command or HTTP URL)
python3 -m sans_mcp help "python3 tests/mock_server.py"
python3 -m sans_mcp help http://127.0.0.1:8000/mcp

# Inspect a specific tool
python3 -m sans_mcp help "python3 tests/mock_server.py" list_items
```

Output:

```text
Server: dummy-stdio-server (1.2.3) — 4 tools
Usage: m.tools.<tool_name>(param=value, ...)

get_info() -> dict
    Get system info
    Returns:
      {os: str, version: int}

get_item(*, item_id: str) -> dict
    Get item details by ID
    Args:
      item_id: Item identifier
    Returns:
      {id: str, name: str, status: str}

greet_user(*, user_name: str) -> str
    Greet a user by name
    Args:
      user_name: The user's name

list_items(*, category: str = None) -> list[dict]
    List available items with optional category filter
    Args:
      category: Category filter
    Returns:
      list[{id: str, name: str, category: str}]
```

### Call Tools from Python

Connect to an HTTP URL or `stdio` command and call tools on `m.tools` using keyword arguments. Tool calls return parsed Python objects (`dict`, `list`, `str`) directly:

```python
import sans_mcp

with sans_mcp.connect("python3 tests/mock_server.py") as m:
    items = m.tools.list_items(category="hardware")
    for item in items:
        info = m.tools.get_item(item_id=item["id"])
        print(f"{info['name']}: {info['status']}")
```

### Stdio-to-HTTP Proxy

Run `python3 -m sans_mcp proxy` to expose a `stdio` MCP server as a Streamable HTTP endpoint (at `http://127.0.0.1:6270/mcp` by default):

```bash
# Expose a stdio MCP server over HTTP (customize address with --listen :PORT)
python3 -m sans_mcp proxy -- npx -y @playwright/mcp
```

To run the proxy in the background, a process runner such as [`runagent`](https://github.com/tsaarni/runagent/) can be used, then connect to it over HTTP:

```python
import sans_mcp

with sans_mcp.connect("http://127.0.0.1:6270/mcp") as pw:
    pw.tools.browser_navigate(url="https://example.com")
    print(pw.tools.browser_snapshot())
```

### HTTP Authentication

To authenticate with remote HTTP MCP servers without placing credentials in CLI commands or Python scripts, configure per-host headers in `~/.config/sans-mcp/auth.json` (or `${XDG_CONFIG_HOME}/sans-mcp/auth.json`) with `0600` file permissions:

```json
{
  "mcp.example.com": {
    "Authorization": "Bearer ${EXAMPLE_MCP_TOKEN}"
  }
}
```

```bash
chmod 600 ~/.config/sans-mcp/auth.json
```

`sans-mcp` matches the target URL's `hostname:port` or `hostname`, expands `${VAR}` references from the environment, and attaches the headers automatically during `sans-mcp help` and `sans_mcp.connect()`. Sending authentication headers over plaintext `http://` is blocked for non-loopback hosts.

## Installation

To make `sans_mcp` available across workspaces, install it into your user environment:

```bash
pip install --user --break-system-packages git+https://github.com/tsaarni/sans-mcp
```

Or download the single file directly into the project directory:

```bash
curl -O https://raw.githubusercontent.com/tsaarni/sans-mcp/main/sans_mcp.py
```



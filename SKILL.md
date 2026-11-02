---
name: sans-mcp
description: Use when calling or inspecting MCP servers from Python.
---

```python
import sans_mcp

# 1. Discover available tools and signatures on first use
with sans_mcp.connect("http://127.0.0.1:8000/mcp") as m:
    print(m.help())
    # Or inspect a specific tool:
    # print(m.help("list_items"))

# 2. Call tools using keyword arguments
with sans_mcp.connect("http://127.0.0.1:8000/mcp") as m:
    items = m.tools.list_items(category="hardware")
    for item in items:
        info = m.tools.get_item(item_id=item["id"])
        print(f"{info['name']}: {info['status']}")
```

```bash
# Optional: expose a stdio server over Streamable HTTP at http://127.0.0.1:6270/mcp
python3 -m sans_mcp proxy -- npx -y @playwright/mcp
```

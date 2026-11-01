#!/usr/bin/env python3
"""Standalone mock MCP stdio server for testing and ad-hoc experiments."""

import json
import sys


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "serverInfo": {"name": "dummy-stdio-server", "version": "1.2.3"},
                "protocolVersion": "2024-11-05",
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        cursor = (req.get("params") or {}).get("cursor")
        if not cursor:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "greet-user",
                            "description": "Greet a user by name",
                            "inputSchema": {
                                "type": "object",
                                "required": ["user-name"],
                                "properties": {
                                    "user-name": {
                                        "type": "string",
                                        "description": "The user's name",
                                    }
                                },
                            },
                        },
                        {
                            "name": "list-items",
                            "description": "List available items with optional category filter",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "category": {
                                        "type": "string",
                                        "description": "Category filter",
                                    }
                                },
                            },
                        },
                    ],
                    "nextCursor": "page-2",
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "get-item",
                        "description": "Get item details by ID",
                        "inputSchema": {
                            "type": "object",
                            "required": ["item-id"],
                            "properties": {
                                "item-id": {
                                    "type": "string",
                                    "description": "Item identifier",
                                }
                            },
                        },
                    },
                    {
                        "name": "get-info",
                        "description": "Get system info",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        }

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if name == "greet-user":
            user = args.get("user-name", "friend")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Hello, {user}!"}]},
            }

        if name == "list-items":
            category = args.get("category")
            items = [
                {"id": "item-1", "name": "Widget", "category": "hardware"},
                {"id": "item-2", "name": "Gadget", "category": "hardware"},
                {"id": "item-3", "name": "Driver", "category": "software"},
            ]
            if category:
                items = [it for it in items if it["category"] == category]
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(items)}]},
            }

        if name == "get-item":
            item_id = args.get("item-id", "")
            items = {
                "item-1": {"id": "item-1", "name": "Widget", "status": "active"},
                "item-2": {"id": "item-2", "name": "Gadget", "status": "deprecated"},
                "item-3": {"id": "item-3", "name": "Driver", "status": "active"},
            }
            item = items.get(item_id, {"id": item_id, "name": "Unknown", "status": "not_found"})
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(item)}]},
            }

        if name == "get-info":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": '{"os": "mock", "version": 42}'}]},
            }

    return {"jsonrpc": "2.0", "id": req_id, "result": {}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

"""In-process MCP tools endpoint for the active CodeLane workspace."""

import json


class IDEMCP:
    def __init__(self, toolbox):
        self.toolbox = toolbox

    def list_tools(self):
        return [{"name": tool["name"], "description": tool["description"],
                 "inputSchema": tool["parameters"]}
                for definition in self.toolbox.definitions()
                for tool in (definition["function"],)]

    def request(self, method, params=None):
        if method == "tools/list":
            return {"tools": self.list_tools()}
        if method != "tools/call":
            raise ValueError(f"Unsupported IDE MCP method: {method}")
        params = params or {}
        name, arguments = params.get("name"), params.get("arguments", {})
        try:
            if not isinstance(arguments, dict) or not any(tool["name"] == name for tool in self.list_tools()):
                raise ValueError("Инструмент IDE недоступен или аргументы недопустимы.")
            value = self.toolbox.run(name, json.dumps(arguments, ensure_ascii=False))
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"content": [{"type": "text", "text": str(value)}]}

    def call(self, name, args):
        result = self.request("tools/call", {"name": name, "arguments": args or {}})
        text = result["content"][0]["text"]
        return f"Ошибка MCP CodeLane: {text}" if result.get("isError") else text

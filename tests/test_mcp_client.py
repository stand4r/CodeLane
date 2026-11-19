import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from services.mcp_client import MCPClient, MCPError, MCPServerConfig, MCPTool, parse_servers, run_tool


def test_parse_servers_validates_names_transports_and_duplicates():
    for payload in (
        '[{"name":"bad name","command":"node"}]',
        '[{"name":"same","command":"node"},{"name":"same","command":"node"}]',
        '[{"name":"legacy","transport":"sse","url":"http://localhost/mcp"}]',
    ):
        try:
            parse_servers(payload)
        except MCPError:
            continue
        raise AssertionError(f"invalid MCP configuration was accepted: {payload}")


def test_mcp_tools_list_follows_pagination():
    client = MCPClient(MCPServerConfig(name="docs"))
    pages = [
        {"tools": [{"name": "one", "inputSchema": {"type": "object"}}],
         "nextCursor": "page-2"},
        {"tools": [{"name": "two"}, {"name": "one"}]},
    ]
    calls = []

    def request(method, params=None, timeout=None):
        calls.append((method, params))
        return pages.pop(0)

    client.request = request
    tools = client.list_tools()
    assert [tool.name for tool in tools] == ["one", "two"]
    assert calls == [("tools/list", {}), ("tools/list", {"cursor": "page-2"})]


def test_mcp_tool_names_fit_provider_limits_and_route_back_to_original():
    original_name = "tool-" + ("x" * 90)
    tool = MCPTool(server="files", name=original_name)
    assert len(tool.full_name) <= 64
    assert tool.full_name.startswith("files__")
    assert len(tool.as_openai_tool()["function"]["name"]) <= 64

    class FakeClient:
        tools = [tool]

        def call_tool(self, name, arguments):
            return f"called:{name}:{arguments['ok']}"

    assert run_tool({"files": FakeClient()}, tool.full_name, {"ok": 1}) == (
        f"called:{original_name}:1")


def test_http_transport_keeps_session_and_custom_headers():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            seen.append((payload, dict(self.headers.items())))
            if payload.get("method") == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if payload.get("method") == "initialize":
                result = {"protocolVersion": "2024-11-05",
                          "serverInfo": {"name": "test-mcp"}, "capabilities": {"tools": {}}}
                extra_headers = {"Mcp-Session-Id": "session-42"}
            elif payload.get("method") == "tools/list":
                result = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
                extra_headers = {}
            else:
                self.send_error(400)
                return
            body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            for key, value in extra_headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = MCPClient(MCPServerConfig(
        name="remote", transport="http",
        url=f"http://127.0.0.1:{server.server_port}/mcp",
        headers={"Authorization": "Bearer test-token"},
    ), timeout=2)
    try:
        tools = client.connect()
        assert [tool.name for tool in tools] == ["ping"]
        assert seen[1][0]["method"] == "notifications/initialized"
        list_payload, list_headers = seen[2]
        list_headers = {key.lower(): value for key, value in list_headers.items()}
        assert list_payload["method"] == "tools/list"
        assert list_headers["mcp-session-id"] == "session-42"
        assert list_headers["mcp-protocol-version"] == "2024-11-05"
        assert list_headers["authorization"] == "Bearer test-token"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

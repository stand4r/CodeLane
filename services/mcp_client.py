"""Minimal Model Context Protocol (MCP) client for the CodeLane AI agent.

Supports both transports that the ecosystem actually uses:

* **stdio** — a local command line (``npx -y @modelcontextprotocol/server-filesystem .``),
  spoken to with newline-delimited JSON-RPC 2.0 over stdin/stdout.
* **http** — the "streamable HTTP" transport, where each JSON-RPC message is a
  single POST to the server URL and the reply is either ``application/json`` or
  an SSE stream.

Only the standard library is used. The client implements the subset an IDE
assistant needs: ``initialize`` → ``tools/list`` → ``tools/call``.

MCP servers are configured in *Настройки → ИИ-агент → MCP-серверы* as JSON:

```json
[
  {"name": "files", "transport": "stdio", "command": "npx",
   "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
  {"name": "docs", "transport": "http", "url": "http://localhost:3001/mcp"}
]
```
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.subprocess_utils import popen_hidden

__all__ = [
    "MCPError",
    "MCPServerConfig",
    "MCPClient",
    "MCPTool",
    "parse_servers",
    "run_tool",
]

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "CodeLane", "version": "1.0"}


class MCPError(RuntimeError):
    """Raised when an MCP server cannot be reached or returns an error."""


@dataclass
class MCPTool:
    """A tool advertised by an MCP server."""

    server: str
    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        # Namespaced so two servers can expose the same tool name, constrained
        # to the identifier grammar and 64-character limit used by model APIs.
        server = re.sub(r"[^A-Za-z0-9_-]", "_", self.server)[:40] or "mcp"
        tool = re.sub(r"[^A-Za-z0-9_-]", "_", self.name) or "tool"
        prefix = f"{server}__"
        budget = 64 - len(prefix)
        if tool != self.name or len(tool) > budget:
            digest = hashlib.sha1(self.name.encode("utf-8", errors="replace")).hexdigest()[:8]
            tool = f"{tool[:max(1, budget - 9)]}_{digest}"
        return prefix + tool

    def as_openai_tool(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.full_name,
                "description": (self.description or self.name)[:1024],
                "parameters": self.schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: List[str] = field(default_factory=list)
    url: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or (self.command or self.url or "mcp")


def parse_servers(text: str) -> List[MCPServerConfig]:
    """Parse the JSON configuration typed in Settings into server configs."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError(f"Некорректный JSON конфигурации MCP: {exc}") from None
    if isinstance(data, dict):
        data = data.get("mcpServers") or data.get("servers") or []
    # The standard MCP config form is a *map*: {"mcpServers": {"files": {...}}}.
    # Convert it to the list form, using the map key as the server name.
    if isinstance(data, dict):
        named = []
        for name, item in data.items():
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("name", name)
                named.append(entry)
        data = named
    if not isinstance(data, list):
        raise MCPError("Ожидался список MCP-серверов в формате JSON.")
    servers = []
    names = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise MCPError(f"MCP-сервер №{index + 1} должен быть объектом JSON.")
        command = str(item.get("command") or "").strip()
        url = str(item.get("url") or "").strip()
        transport = str(item.get("transport") or ("http" if url and not command else "stdio")).strip().lower()
        name = str(item.get("name") or item.get("id") or f"mcp{index + 1}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
            raise MCPError(f"Имя MCP-сервера «{name}» должно содержать 1–40 символов: латиницу, цифры, _ или -.")
        if name in names:
            raise MCPError(f"Имя MCP-сервера «{name}» указано несколько раз.")
        names.add(name)
        if transport not in ("stdio", "http"):
            raise MCPError(f"MCP «{name}»: транспорт «{transport}» не поддерживается (доступны stdio и http).")
        if transport == "stdio" and not command:
            raise MCPError(f"MCP «{name}»: для транспорта stdio укажи command.")
        if transport == "http" and not url:
            raise MCPError(f"MCP «{name}»: для транспорта http укажи url.")
        if transport == "http" and urlsplit(url).scheme not in ("http", "https"):
            raise MCPError(f"MCP «{name}»: HTTP URL должен начинаться с http:// или https://.")
        if not isinstance(item.get("args") or [], list):
            raise MCPError(f"MCP «{name}»: args должен быть списком строк.")
        if not isinstance(item.get("env") or {}, dict):
            raise MCPError(f"MCP «{name}»: env должен быть объектом ключ/значение.")
        if not isinstance(item.get("headers") or {}, dict):
            raise MCPError(f"MCP «{name}»: headers должен быть объектом ключ/значение.")
        servers.append(MCPServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=[str(a) for a in (item.get("args") or [])],
            url=url,
            env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
            headers={str(k): str(v) for k, v in (item.get("headers") or {}).items()},
            enabled=item.get("enabled", True) is not False,
        ))
    return servers


class MCPClient:
    """A single MCP server connection (stdio process or HTTP endpoint)."""

    def __init__(self, config: MCPServerConfig, timeout: float = 25.0):
        self.config = config
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stdio_request_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, Any] = {}
        self._pending_ids = set()
        self._reader: Optional[threading.Thread] = None
        self._buffer: List[str] = []
        self._stderr: List[str] = []
        self.tools: List[MCPTool] = []
        self._tools_changed = threading.Event()
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.initialized = False
        self._session_id: Optional[str] = None
        self._protocol_version = PROTOCOL_VERSION

    # ------------------------------------------------------------------
    # Transport: stdio
    # ------------------------------------------------------------------
    def _start_stdio(self):
        if self._proc is not None:
            return
        if not self.config.command:
            raise MCPError(f"MCP «{self.config.label}»: не задана команда запуска.")
        env = dict(os.environ)
        env.update(self.config.env or {})
        try:
            self._proc = popen_hidden(
                [self.config.command] + list(self.config.args or []),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            raise MCPError(
                f"MCP «{self.config.label}»: команда «{self.config.command}» не найдена. "
                "Установите её (например, Node.js для npx) и проверьте путь."
            ) from None
        except OSError as exc:
            raise MCPError(f"MCP «{self.config.label}»: не удалось запустить — {exc}") from None
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self):
        for line in self._proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(message)

    def _pump_stderr(self):
        for line in self._proc.stderr:  # type: ignore[union-attr]
            text = line.strip()
            if text:
                self._stderr.append(text)
                del self._stderr[:-40]

    def _dispatch(self, message: Dict[str, Any]):
        if "id" in message and ("result" in message or "error" in message):
            with self._lock:
                if message["id"] in self._pending_ids:
                    self._pending[message["id"]] = message
        # We do not advertise sampling or elicitation capabilities. Explicitly
        # reject unsupported server requests instead of returning a fake result.
        elif "method" in message and "id" in message:
            if message["method"] == "ping":
                self._reply(message["id"], {})
            else:
                self._reply_error(message["id"], -32601, "Method not supported")
        elif message.get("method") == "notifications/tools/list_changed":
            self._tools_changed.set()

    def _reply(self, message_id, result):
        try:
            self._write({"jsonrpc": "2.0", "id": message_id, "result": result})
        except Exception:
            pass

    def _reply_error(self, message_id, code, message):
        try:
            self._write({"jsonrpc": "2.0", "id": message_id,
                         "error": {"code": code, "message": message}})
        except Exception:
            pass

    def _write(self, payload: Dict[str, Any]):
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP-процесс не запущен.")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    # ------------------------------------------------------------------
    # Transport: HTTP (streamable)
    # ------------------------------------------------------------------
    def _post_http(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.error
        import urllib.request

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(self.config.headers or {}),
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if payload.get("method") != "initialize":
            headers["MCP-Protocol-Version"] = self._protocol_version
        req = urllib.request.Request(
            self.config.url,
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                session_id = resp.headers.get("Mcp-Session-Id")
                negotiated_version = resp.headers.get("MCP-Protocol-Version")
                if session_id:
                    self._session_id = session_id
                if negotiated_version:
                    self._protocol_version = negotiated_version
                if "text/event-stream" in resp.headers.get("Content-Type", "").lower():
                    return self._read_http_events(resp, payload.get("id"))
                raw = resp.read().decode("utf-8", errors="replace")
        except MCPError:
            raise
        except urllib.error.HTTPError as exc:
            raise MCPError(f"MCP «{self.config.label}»: HTTP {exc.code}") from None
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"MCP «{self.config.label}»: нет соединения — {exc}") from None
        if not raw.strip():
            if payload.get("id") is None:
                return {}
            raise MCPError(f"MCP «{self.config.label}»: сервер вернул пустой ответ на запрос.")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise MCPError(f"MCP «{self.config.label}»: ответ не является JSON.") from None

    def _read_http_events(self, response, message_id):
        if message_id is None:
            return {}
        data = []
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                try:
                    message = json.loads("\n".join(data))
                except json.JSONDecodeError:
                    message = None
                data.clear()
                if isinstance(message, dict):
                    if message.get("id") == message_id:
                        return message
                    if "id" not in message and "method" in message:
                        self._dispatch(message)
        raise MCPError(f"MCP «{self.config.label}»: в SSE-ответе нет ответа для запроса.")

    # ------------------------------------------------------------------
    # JSON-RPC core
    # ------------------------------------------------------------------
    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        limit = timeout or self.timeout
        with self._lock:
            message_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method,
                   "params": params or {}}

        if self.config.transport == "http":
            reply = self._post_http(payload)
        else:
            with self._stdio_request_lock:
                self._start_stdio()
                with self._lock:
                    self._pending_ids.add(message_id)
                try:
                    self._write(payload)
                    deadline = time.monotonic() + limit
                    while time.monotonic() < deadline:
                        with self._lock:
                            reply = self._pending.pop(message_id, None)
                        if reply is not None:
                            break
                        if self._proc is not None and self._proc.poll() is not None:
                            detail = " | ".join(self._stderr[-3:])
                            raise MCPError(
                                f"MCP «{self.config.label}» завершился: {detail or 'без описания'}"
                            )
                        time.sleep(0.01)
                    else:
                        raise MCPError(
                            f"MCP «{self.config.label}» не ответил за {limit:.0f} с."
                        )
                finally:
                    with self._lock:
                        self._pending_ids.discard(message_id)
                        self._pending.pop(message_id, None)
        if "error" in reply:
            error = reply["error"] or {}
            raise MCPError(f"MCP «{self.config.label}»: {error.get('message', error)}")
        return reply.get("result") or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> List[MCPTool]:
        """Handshake with the server and return its tools."""
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = result.get("serverInfo") or {}
        capabilities = result.get("capabilities", {"tools": {}})
        self.capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.initialized = True
        # The spec requires a notification after initialize (no id, no reply).
        self._protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        if self.config.transport != "http":
            try:
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            except Exception:
                pass
        else:
            self._post_http({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.tools = self.list_tools() if "tools" in self.capabilities else []
        return self.tools

    def refresh_tools(self):
        """Refresh a cached catalogue once the server reports a change."""
        if self._proc is not None and self._proc.poll() is not None:
            raise MCPError(f"MCP «{self.config.label}»: процесс завершился.")
        if self._tools_changed.is_set() and "tools" in self.capabilities:
            self._tools_changed.clear()
            try:
                self.tools = self.list_tools()
            except MCPError:
                self._tools_changed.set()
                raise
        return self.tools

    def list_resources(self, cursor="", *, templates=False, offset=0):
        method = "resources/templates/list" if templates else "resources/list"
        key = "resourceTemplates" if templates else "resources"
        result = self.request(method, {"cursor": cursor} if cursor else {})
        entries = result.get(key) or []
        fields = ("uri", "uriTemplate", "name", "description", "mimeType")
        end = offset + 50
        same_page = len(entries) > end
        return {key: [{field: str(item[field])[:4000] for field in fields if field in item}
                      for item in entries[offset:end] if isinstance(item, dict)],
                "nextOffset": end if same_page else None,
                "nextCursor": (cursor or None) if same_page else result.get("nextCursor"),
                "truncated": same_page}

    def read_resource(self, uri):
        result = self.request("resources/read", {"uri": uri})
        items = []
        remaining = 24000
        truncated = False
        for item in result.get("contents") or []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            entry = {"uri": str(item.get("uri", uri)), "mimeType": item.get("mimeType")}
            if isinstance(text, str):
                entry["text"] = text[:remaining]
                truncated |= len(text) > remaining
                remaining -= len(entry["text"])
            else:
                entry["binary"] = "blob" in item
            items.append(entry)
            if len(items) == 50:
                truncated = True
                break
        return {"contents": items, "truncated": truncated}

    def list_tools(self) -> List[MCPTool]:
        result = self.request("tools/list", {})
        tools = []
        seen = set()
        cursors = set()
        for page in range(100):
            for item in result.get("tools") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                name = str(item["name"])
                if name in seen:
                    continue
                seen.add(name)
                tools.append(MCPTool(
                    server=self.config.name,
                    name=name,
                    description=str(item.get("description") or ""),
                    schema=item.get("inputSchema") or {},
                ))
            cursor = result.get("nextCursor")
            if page == 99 or not isinstance(cursor, str) or not cursor or cursor in cursors or len(seen) >= 5000:
                break
            cursors.add(cursor)
            result = self.request("tools/list", {"cursor": cursor})
        return tools

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        chunks: List[str] = []
        for item in result.get("content") or []:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
                elif item.get("type") == "resource":
                    resource = item.get("resource") or {}
                    chunks.append(str(resource.get("text") or resource.get("uri") or ""))
                elif item.get("type") in {"image", "audio"}:
                    chunks.append(f"Двоичный результат MCP: {item.get('mimeType') or item['type']}.")
                else:
                    chunks.append(json.dumps(item, ensure_ascii=False)[:2000])
            else:
                chunks.append(str(item))
        if result.get("isError"):
            return "Ошибка инструмента: " + ("\n".join(chunks) or "без описания")
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                chunks.append("Структурированный результат:\n" +
                              json.dumps(structured, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError):
                chunks.append("Структурированный результат: " + str(structured))
        combined = "\n".join(c for c in chunks if c).strip() or "Готово."
        if len(combined) > 1_000_000:
            combined = combined[:1_000_000] + "\n… (результат MCP обрезан)"
        return combined

    def close(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        def reap():
            if os.name == "nt":
                try:
                    from core.subprocess_utils import run_hidden
                    run_hidden(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=2)
                except Exception:
                    pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
            except Exception:
                pass

        # Closing the agent must not wait several seconds on the UI thread for
        # a misbehaving MCP child to exit.
        threading.Thread(target=reap, name=f"mcp-reap-{self.config.name}",
                         daemon=True).start()


# ----------------------------------------------------------------------
# Helpers used by the agent loop
# ----------------------------------------------------------------------
def resource_tools(servers):
    names = [name for name, client in servers.items() if "resources" in getattr(client, "capabilities", {})]
    if not names:
        return []
    server = {"type": "string", "enum": names}
    return [
        {"type": "function", "function": {
            "name": "mcp_list_resources",
            "description": "Перечислить ресурсы или шаблоны MCP. Продолжай с nextCursor и nextOffset, если они заданы.",
            "parameters": {"type": "object", "required": ["server"], "properties": {
                "server": server, "cursor": {"type": "string"}, "templates": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0}}},
        }},
        {"type": "function", "function": {
            "name": "mcp_read_resource", "description": "Прочитать текст ресурса MCP по URI.",
            "parameters": {"type": "object", "required": ["server", "uri"], "properties": {
                "server": server, "uri": {"type": "string"}}},
        }},
    ]


def run_resource_tool(servers, name, payload):
    key = payload.get("server")
    client = servers.get(key) if isinstance(key, str) else None
    if client is None or "resources" not in getattr(client, "capabilities", {}):
        raise MCPError("Сервер не подключён или не объявил поддержку ресурсов.")
    if name == "mcp_list_resources":
        cursor = payload.get("cursor", "")
        templates = payload.get("templates", False)
        offset = payload.get("offset", 0)
        if not isinstance(cursor, str) or not isinstance(templates, bool) or type(offset) is not int or offset < 0:
            raise MCPError("Некорректные параметры списка ресурсов.")
        value = client.list_resources(cursor, templates=templates, offset=offset)
    else:
        uri = payload.get("uri")
        if not isinstance(uri, str) or not uri.strip() or len(uri) > 4000:
            raise MCPError("Укажи URI ресурса.")
        value = client.read_resource(uri)
    return json.dumps(value, ensure_ascii=False)


def run_tool(servers: Dict[str, MCPClient], tool_name: str,
             arguments: Dict[str, Any]) -> str:
    """Execute ``server__tool`` through the matching live client."""
    for client in servers.values():
        for tool in client.tools:
            if tool.full_name == tool_name:
                return client.call_tool(tool.name, arguments)
    return f"Ошибка: MCP-инструмент {tool_name} не объявлен сервером."

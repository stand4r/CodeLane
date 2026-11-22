"""Cross-Language Contract & OpenAPI Integration for PyScribe.

Provides:
- Detection and indexing of OpenAPI / Swagger specifications (YAML and JSON).
- Scanning client code (JS/TS, Python, C#, Go) for API client calls (fetch, axios, requests, etc.).
- Scanning server code for endpoint route handlers (FastAPI, Flask, Express, ASP.NET, Gin).
- End-to-end navigation:
    Client API call -> OpenAPI specification operation -> Server route handler.
- Distinguishes semantic OpenAPI contract links from plain text matches.
- Surfaces matching basis (path, method, operationId) and handles ambiguous candidates.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import yaml
from core.code_analysis.lsp_client import LspLocation

logger = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", "dist", "build", ".idea", ".vscode"
}


@dataclass
class OpenApiOperation:
    """An operation defined in an OpenAPI specification."""
    path: str
    method: str  # lowercase: get, post, put, delete, patch, etc.
    operation_id: str
    summary: str
    spec_file: str
    line: int
    col: int = 0


@dataclass
class ClientApiCall:
    """An outbound HTTP request in client source code."""
    path: str
    method: Optional[str]  # get, post, etc. or None if generic
    file_path: str
    line: int
    col: int
    raw_text: str


@dataclass
class ServerRouteHandler:
    """An inbound route handler in backend server source code."""
    path: str
    method: Optional[str]
    handler_name: str
    file_path: str
    line: int
    col: int
    raw_text: str


def normalize_api_path(path: str) -> str:
    """Normalize API path: strip quotes, query params, normalize {param} and ${param}."""
    p = path.strip().strip("'\"`")
    if "?" in p:
        p = p.split("?")[0]
    # Replace ${param} with {param}
    p = re.sub(r"\$\{\s*(\w+)\s*\}", r"{\1}", p)
    # Ensure leading slash
    if not p.startswith("/"):
        p = "/" + p
    # Remove trailing slash unless root
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def paths_match(p1: str, p2: str) -> bool:
    """Compare two API paths, treating {param} variables as wildcards."""
    n1 = normalize_api_path(p1)
    n2 = normalize_api_path(p2)
    if n1 == n2:
        return True

    # Pattern match replacing {param} with regex [^/]+
    regex1 = "^" + re.sub(r"\{[^/}]+\}", r"[^/]+", n1) + "$"
    if re.match(regex1, n2):
        return True
    regex2 = "^" + re.sub(r"\{[^/}]+\}", r"[^/]+", n2) + "$"
    if re.match(regex2, n1):
        return True
    return False


class OpenApiBridge:
    """Cross-language bridge connecting client calls, OpenAPI specs, and backend routes."""

    def __init__(self, root_path: Optional[str] = None):
        self.root_path: Optional[Path] = Path(root_path).resolve() if root_path else None
        self.operations: List[OpenApiOperation] = []
        self.client_calls: List[ClientApiCall] = []
        self.server_handlers: List[ServerRouteHandler] = []
        self._indexed = False
        # Index scans run in a worker thread ("openapi-indexer").  ``_generation``
        # invalidates a running scan as soon as the project root changes, so a
        # stale scan never walks a root that has already been cleared (that used
        # to raise TypeError: expected str, bytes or os.PathLike, not NoneType).
        self._generation = 0
        self._index_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        # Per-thread scan buffers: a cancelled scan collects into its own lists and
        # only the still-current scan publishes them (see ``rebuild_index``).
        self._scan_state = threading.local()

    def set_root(self, root_path: Optional[str]):
        """Retarget the bridge at ``root_path`` and cancel any running scan."""
        root = Path(root_path).resolve() if root_path else None
        with self._lock:
            self._generation += 1
            self.root_path = root
            self._reset_index_unlocked()

    def cancel_indexing(self):
        """Ask a running background scan to stop without clearing the index."""
        with self._lock:
            self._generation += 1

    def clear(self):
        with self._lock:
            self._generation += 1
            self._reset_index_unlocked()

    def _reset_index_unlocked(self):
        self.operations.clear()
        self.client_calls.clear()
        self.server_handlers.clear()
        self._indexed = False

    def _buckets(self) -> Tuple[list, list, list]:
        """Scan target lists: the calling thread's buffers or the live index."""
        buckets = getattr(self._scan_state, "buckets", None)
        if buckets is None:
            return (self.operations, self.client_calls, self.server_handlers)
        return buckets

    def start_indexing(self) -> Optional[threading.Thread]:
        """Rebuild the index in the background, replacing any previous scan."""
        with self._lock:
            generation = self._generation
            root = self.root_path
            if root is None or not root.is_dir():
                self._indexed = True
                return None
            thread = threading.Thread(
                target=self.rebuild_index, args=(generation,),
                name="openapi-indexer", daemon=True,
            )
            self._index_thread = thread
        thread.start()
        return thread

    def ensure_indexed(self):
        if not self._indexed and self.root_path and self.root_path.exists():
            self.rebuild_index()

    def rebuild_index(self, generation: Optional[int] = None):
        """Scan project workspace for OpenAPI specs, client calls, and backend handlers.

        Safe from a worker thread: the root is snapshotted once, the scan stops as
        soon as a newer root/cancellation arrives, and results are published only
        while this scan is still the current one.
        """
        with self._lock:
            if generation is None:
                generation = self._generation
            root = self.root_path
        if root is None or not root.is_dir():
            with self._lock:
                if generation == self._generation:
                    self._indexed = True
            return

        buckets: Tuple[list, list, list] = ([], [], [])
        self._scan_state.buckets = buckets
        try:
            for current, dirs, files in os.walk(root):
                if generation != self._generation:
                    return
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for file in files:
                    if generation != self._generation:
                        return
                    p = Path(current) / file
                    suffix = p.suffix.lower()

                    # 1. OpenAPI Specs
                    if suffix in (".yaml", ".yml", ".json"):
                        self._check_and_index_spec(p)

                    # 2. Source Code (Client & Server routes)
                    elif suffix in (".js", ".jsx", ".ts", ".tsx", ".py", ".cs", ".go", ".java"):
                        self._index_source_file(p)
        finally:
            self._scan_state.buckets = None

        with self._lock:
            if generation != self._generation:
                return
            self.operations[:] = buckets[0]
            self.client_calls[:] = buckets[1]
            self.server_handlers[:] = buckets[2]
            self._indexed = True
        logger.info(
            "OpenAPI index ready: %d operations, %d client calls, %d server handlers",
            len(self.operations), len(self.client_calls), len(self.server_handlers)
        )

    # ------------------------------------------------------------------
    # Indexing Specs
    # ------------------------------------------------------------------
    def _check_and_index_spec(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Fast heuristic
        if "paths:" not in content and '"paths"' not in content:
            return
        if "openapi" not in content and "swagger" not in content:
            return

        try:
            if path.suffix.lower() == ".json":
                data = json.loads(content)
            else:
                data = yaml.safe_load(content)
        except Exception:
            return

        if not isinstance(data, dict):
            return
        if "openapi" not in data and "swagger" not in data:
            return
        paths_dict = data.get("paths")
        if not isinstance(paths_dict, dict):
            return

        lines = content.splitlines()

        for route_path, methods in paths_dict.items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                m_lower = method.lower()
                if m_lower not in ("get", "post", "put", "delete", "patch", "options", "head"):
                    continue

                op_id = ""
                summary = ""
                if isinstance(details, dict):
                    op_id = str(details.get("operationId", "")).strip()
                    summary = str(details.get("summary", "")).strip()

                line_no = self._find_spec_line(lines, route_path, method)

                self._buckets()[0].append(OpenApiOperation(
                    path=route_path,
                    method=m_lower,
                    operation_id=op_id,
                    summary=summary,
                    spec_file=str(path.resolve()),
                    line=line_no,
                    col=0,
                ))

    def _find_spec_line(self, lines: List[str], route_path: str, method: str) -> int:
        """Find the approximate line where the path and method are defined in spec."""
        target_path = route_path.strip()
        in_path_block = False
        path_line = 1

        for idx, line in enumerate(lines):
            l = line.strip()
            # Match path
            if target_path in l and (l.startswith(f"'{target_path}'") or l.startswith(f'"{target_path}"') or l.startswith(f"{target_path}:") or f'"{target_path}":' in l):
                in_path_block = True
                path_line = idx + 1
                continue

            if in_path_block:
                # If we encounter next top-level path (less or equal indent), reset
                if l and not line.startswith(" ") and not line.startswith("\t"):
                    in_path_block = False
                elif method.lower() in l.lower() and (l.startswith(f"{method.lower()}:") or f'"{method.lower()}":' in l):
                    return idx + 1

        return path_line

    # ------------------------------------------------------------------
    # Indexing Source Code (Client API Calls & Server Route Handlers)
    # ------------------------------------------------------------------
    def _index_source_file(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        lines = content.splitlines()
        for idx, line in enumerate(lines):
            line_no = idx + 1
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "#", "/*", "*")):
                continue

            # Check Client API calls
            client_call = self._extract_client_call(stripped, str(path.resolve()), line_no)
            if client_call:
                self._buckets()[1].append(client_call)

            # Check Server Route Handlers
            handler = self._extract_server_handler(stripped, str(path.resolve()), line_no)
            if handler:
                self._buckets()[2].append(handler)

    def _extract_client_call(self, line: str, file_path: str, line_no: int) -> Optional[ClientApiCall]:
        # fetch('/api/...')
        m = re.search(r"\bfetch\s*\(\s*['\"`](/api/[^'\"`]+)['\"`]", line)
        if m:
            return ClientApiCall(path=m.group(1), method=None, file_path=file_path, line=line_no, col=m.start(1), raw_text=line)

        # axios.get('/api/...'), requests.post('/api/...')
        m = re.search(r"\b(?:axios|requests|http|client)\.(get|post|put|delete|patch)\s*\(\s*['\"`](/[^'\"`]+)['\"`]", line, re.I)
        if m:
            return ClientApiCall(path=m.group(2), method=m.group(1).lower(), file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        # api.get('/api/...')
        m = re.search(r"\bapi\.(get|post|put|delete|patch)\s*\(\s*['\"`](/[^'\"`]+)['\"`]", line, re.I)
        if m:
            return ClientApiCall(path=m.group(2), method=m.group(1).lower(), file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        return None

    def _extract_server_handler(self, line: str, file_path: str, line_no: int) -> Optional[ServerRouteHandler]:
        # Python FastAPI / Flask: @app.get('/api/users'), @router.post('/api/users')
        m = re.search(r"@(?:app|router|blueprint)\.(get|post|put|delete|patch|route)\s*\(\s*['\"](/[^'\"]+)['\"]", line, re.I)
        if m:
            method = m.group(1).lower() if m.group(1).lower() != "route" else None
            return ServerRouteHandler(path=m.group(2), method=method, handler_name="", file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        # Express: app.get('/api/users', ...), router.post('/api/users', ...)
        m = re.search(r"\b(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*['\"](/[^'\"]+)['\"]", line, re.I)
        if m:
            return ServerRouteHandler(path=m.group(2), method=m.group(1).lower(), handler_name="", file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        # C# ASP.NET: [HttpGet("api/users")], [Route("api/users")]
        m = re.search(r"\[Http(Get|Post|Put|Delete|Patch)\s*\(\s*\"([^\"]+)\"\s*\)\]", line, re.I)
        if m:
            return ServerRouteHandler(path=m.group(2), method=m.group(1).lower(), handler_name="", file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        # Go Gin/Echo: r.GET("/api/users", ...), e.POST("/api/users", ...)
        m = re.search(r"\b\w+\.(GET|POST|PUT|DELETE|PATCH)\s*\(\s*\"(/?[^\"]+)\"", line)
        if m:
            return ServerRouteHandler(path=m.group(2), method=m.group(1).lower(), handler_name="", file_path=file_path, line=line_no, col=m.start(2), raw_text=line)

        return None

    # ------------------------------------------------------------------
    # End-to-End Query
    # ------------------------------------------------------------------
    def find_cross_language_links(self, file_path: str, line: int, col: int, current_line_text: str = "") -> List[LspLocation]:
        """Resolve OpenAPI cross-language links from the current position.

        Returns targets linking:
        Client call -> OpenAPI Specification operation -> Server route handler.
        """
        self.ensure_indexed()
        results: List[LspLocation] = []

        norm_file = str(Path(file_path).resolve())

        # Check if cursor is on client API call
        for call in self.client_calls:
            if call.file_path == norm_file and call.line == line:
                # 1. Link to OpenAPI spec operation
                matching_ops = [op for op in self.operations if paths_match(op.path, call.path)]
                if call.method:
                    exact_ops = [op for op in matching_ops if op.method == call.method]
                    if exact_ops:
                        matching_ops = exact_ops

                for op in matching_ops:
                    basis = f"{op.method.upper()} {op.path}"
                    badge = f"[OpenAPI Спецификация: {basis}]"
                    results.append(LspLocation(
                        file_path=op.spec_file,
                        line=op.line,
                        col=op.col,
                        end_line=op.line,
                        end_col=op.col + 10,
                        preview=f"{op.method.upper()} {op.path}" + (f" ({op.summary})" if op.summary else ""),
                        kind="openapi",
                        badge=badge,
                    ))

                # 2. Link to Server handler
                matching_handlers = [h for h in self.server_handlers if paths_match(h.path, call.path)]
                if call.method:
                    exact_handlers = [h for h in matching_handlers if h.method is None or h.method == call.method]
                    if exact_handlers:
                        matching_handlers = exact_handlers

                for h in matching_handlers:
                    basis = f"{h.method.upper() + ' ' if h.method else ''}{h.path}"
                    badge = f"[OpenAPI Обработчик: {basis}]"
                    results.append(LspLocation(
                        file_path=h.file_path,
                        line=h.line,
                        col=h.col,
                        end_line=h.line,
                        end_col=h.col + len(h.path),
                        preview=h.raw_text,
                        kind="openapi",
                        badge=badge,
                    ))

                if results:
                    return results

        # Check if cursor is inside an OpenAPI specification file
        for op in self.operations:
            if op.spec_file == norm_file and abs(op.line - line) <= 2:
                # 1. Link to Client calls
                for call in self.client_calls:
                    if paths_match(call.path, op.path) and (call.method is None or call.method == op.method):
                        results.append(LspLocation(
                            file_path=call.file_path,
                            line=call.line,
                            col=call.col,
                            end_line=call.line,
                            end_col=call.col + len(call.path),
                            preview=call.raw_text,
                            kind="openapi",
                            badge=f"[OpenAPI Клиентский вызов: {op.method.upper()} {op.path}]",
                        ))

                # 2. Link to Server handlers
                for h in self.server_handlers:
                    if paths_match(h.path, op.path) and (h.method is None or h.method == op.method):
                        results.append(LspLocation(
                            file_path=h.file_path,
                            line=h.line,
                            col=h.col,
                            end_line=h.line,
                            end_col=h.col + len(h.path),
                            preview=h.raw_text,
                            kind="openapi",
                            badge=f"[OpenAPI Обработчик: {op.method.upper()} {op.path}]",
                        ))

                if results:
                    return results

        # Check if cursor is on server route handler
        for h in self.server_handlers:
            if h.file_path == norm_file and h.line == line:
                # 1. Link to OpenAPI spec
                for op in self.operations:
                    if paths_match(op.path, h.path) and (h.method is None or op.method == h.method):
                        results.append(LspLocation(
                            file_path=op.spec_file,
                            line=op.line,
                            col=op.col,
                            end_line=op.line,
                            end_col=op.col + 10,
                            preview=f"{op.method.upper()} {op.path}" + (f" ({op.summary})" if op.summary else ""),
                            kind="openapi",
                            badge=f"[OpenAPI Спецификация: {op.method.upper()} {op.path}]",
                        ))

                # 2. Link to Client calls
                for call in self.client_calls:
                    if paths_match(call.path, h.path) and (h.method is None or call.method is None or call.method == h.method):
                        results.append(LspLocation(
                            file_path=call.file_path,
                            line=call.line,
                            col=call.col,
                            end_line=call.line,
                            end_col=call.col + len(call.path),
                            preview=call.raw_text,
                            kind="openapi",
                            badge=f"[OpenAPI Клиентский вызов: {h.path}]",
                        ))

                if results:
                    return results

        # Fallback: check if line contains an explicit path string like "/api/..."
        m = re.search(r"['\"`](/api/[^'\"`]+)['\"`]", current_line_text)
        if m:
            extracted_path = m.group(1)
            for op in self.operations:
                if paths_match(op.path, extracted_path):
                    results.append(LspLocation(
                        file_path=op.spec_file,
                        line=op.line,
                        col=op.col,
                        end_line=op.line,
                        end_col=op.col + 10,
                        preview=f"{op.method.upper()} {op.path}",
                        kind="openapi",
                        badge=f"[OpenAPI Спецификация: {op.method.upper()} {op.path}]",
                    ))
            for handler in self.server_handlers:
                if paths_match(handler.path, extracted_path):
                    results.append(LspLocation(
                        file_path=handler.file_path,
                        line=handler.line,
                        col=handler.col,
                        end_line=handler.line,
                        end_col=handler.col + len(handler.path),
                        preview=handler.raw_text,
                        kind="openapi",
                        badge=f"[OpenAPI Обработчик: {handler.path}]",
                    ))

        return results

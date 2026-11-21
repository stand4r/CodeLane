"""Bridge between AI tools and the live Qt workbench."""

from dataclasses import asdict, replace
from itertools import islice
from pathlib import Path
import re

from PyQt6 import QtCore, QtGui


def background_browser_window(window):
    from PyQt6 import QtWidgets
    from widgets.WebBrowserWidget import WebBrowserWidget

    group = getattr(window, "_agent_browser_group", None)
    if group is None:
        group = QtWidgets.QTabWidget(window)
        group.hide()
        group.addTab(WebBrowserWidget(group), "Agent browser")
        window._agent_browser_group = group

    class BrowserContext:
        def __getattr__(self, name):
            return getattr(window, name)

        def _find_special_tab(self, kind):
            return (group, 0) if kind == "browser" else window._find_special_tab(kind)

        def action_open_browser(self):
            pass

    return BrowserContext()


def _limit(payload, default=100, maximum=200):
    return min(payload.get("limit", payload.get("lines", default)), maximum)


def _page(items, payload):
    start = payload.get("offset", 1) - 1
    return items[start:start + _limit(payload)]


def _browser(window):
    location = window._find_special_tab("browser")
    if location is None:
        raise RuntimeError("Браузер закрыт. Используй browser_navigate.")
    group, index = location
    return group.widget(index)


def _search_browser(window, query, complete):
    from services.browser_settings import search_url

    query = query.strip()
    if not query or len(query) > 500:
        raise RuntimeError("Укажите поисковый запрос до 500 символов.")
    group = window.active_tab_widget
    original = group.currentWidget()
    if window._find_special_tab("browser") is None:
        window.action_open_browser()
    browser = _browser(window)
    browser.new_tab()
    tab = browser.stack.currentWidget()
    url = search_url(query)
    browser.navigate(url, tab)
    if original is not None and group.indexOf(original) >= 0:
        window._set_active_group(group)
        group.setCurrentWidget(original)
    if tab.view is None:
        raise RuntimeError("Не удалось запустить веб-поиск.")

    view = tab.view
    timer = QtCore.QTimer(view)
    timer.setSingleShot(True)
    done = False

    def finish(value=None, *, error=""):
        nonlocal done
        if done:
            return
        done = True
        timer.stop()
        timer.deleteLater()
        try:
            view.loadFinished.disconnect(loaded)
            view.destroyed.disconnect(closed)
        except (RuntimeError, TypeError):
            pass
        complete(value, error=error)

    def closed(*_):
        nonlocal done
        if not done:
            done = True
            complete(error="Поисковая вкладка закрыта.")

    def results(items):
        if done:
            return
        sources = items if isinstance(items, list) else []
        finish({"query": query, "search_url": url.toString(), "sources": sources,
                "status": "ok" if sources else "no_results"})

    def loaded(ok):
        if not ok:
            finish(error="Поисковая страница не загрузилась.")
            return
        view.page().runJavaScript("""
            (() => {
                const links = document.querySelectorAll(
                    'a[data-testid="result-title-a"], .result__a, h3 a, h2 a, a:has(h3), a:has(h2)');
                const seen = new Set(), sources = [];
                for (const link of links) {
                    const title = (link.innerText || '').trim();
                    const url = new URL(link.href, location.href);
                    if (!title || !['http:', 'https:'].includes(url.protocol) ||
                        url.hostname === location.hostname || url.username || url.password ||
                        seen.has(url.href)) continue;
                    seen.add(url.href);
                    sources.push({title: title.slice(0, 250), url: url.href.slice(0, 3000)});
                    if (sources.length >= 12) break;
                }
                return sources;
            })()
        """, results)

    view.loadFinished.connect(loaded)
    view.destroyed.connect(closed)
    timer.timeout.connect(lambda: finish(error="Время ожидания веб-поиска истекло."))
    timer.start(25000)
    return PENDING


def _browser_control(browser, payload):
    action = payload["action"]
    if action == "new":
        browser.new_tab()
    elif action == "home":
        browser.show_home()
    elif action in ("select", "close"):
        index = payload.get("tab", browser.tabs.currentIndex() + 1) - 1
        if not 0 <= index < browser.tabs.count():
            raise RuntimeError("Вкладка браузера не найдена.")
        if action == "select":
            browser.tabs.setCurrentIndex(index)
        else:
            browser._close_tab(index)
    else:
        browser._web_action(action)
    return {"active_tab": browser.tabs.currentIndex() + 1, "count": browser.tabs.count()}


def _analysis_details(analysis, payload):
    section = payload["section"]
    if section == "summary":
        return {
            "files": len(analysis.files), "issues": len(analysis.issues),
            "dependencies": len(analysis.dependencies), "health": analysis.health_score,
            "truncated": analysis.scan_truncated, "backends": analysis.backend_status,
        }
    path = payload.get("path")
    if section == "dependencies":
        items = analysis.dependencies
        if path:
            module = next((item.module for item in analysis.files.values() if item.path == path), None)
            items = [pair for pair in items if path in pair or (module and module in pair)]
        return {"total": len(items), "items": _page(items, payload)}
    items = getattr(analysis, section)
    if path:
        items = [item for item in items if item.file == path]
    return {"total": len(items), "items": [asdict(item) for item in _page(items, payload)]}


def _lsp_query(service, payload, source):
    action, path = payload["action"], payload["path"]
    if action == "status":
        return service.get_language_status(path)
    if action == "outline":
        return [asdict(symbol) for symbol in service.get_file_structure(path, source)[:200]]
    line, column = payload.get("line", 1), payload.get("column", 1) - 1
    if source is None:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as handle:
            line_text = next(islice(handle, line - 1, line), "").rstrip("\r\n")
    else:
        lines = source.splitlines()
        line_text = lines[line - 1] if line <= len(lines) else ""
    symbol = payload.get("symbol") or next(
        (match[0] for match in re.finditer(r"\w+", line_text) if match.start() <= column < match.end()), ""
    )
    # LSP uses UTF-16 offsets.
    column = len(line_text[:column].encode("utf-16-le")) // 2
    query = service.goto_definition if action == "definition" else service.find_references
    return [asdict(item) for item in query(path, line, column, line_text, symbol)[:100]]


def _todo_edit(panel, payload):
    store = panel.task_store
    store.load()
    if store.error:
        raise RuntimeError(store.error)
    changes = {key: payload[key] for key in ("title", "details", "priority", "status", "line") if key in payload}
    if "path" in payload:
        changes["file"] = Path(payload["path"]).relative_to(panel.root_path).as_posix()
    action = payload["action"]
    if action == "add":
        if not changes.get("title", "").strip():
            raise RuntimeError("Укажи заголовок задачи.")
        status = changes.pop("status", "todo")
        task = store.add(**changes)
        if not store.error and status != task.status:
            task = store.set_status(task.id, status)
    elif action == "update":
        task = store.update(payload.get("id"), **changes)
    else:
        task = store.remove(payload.get("id"))
    if store.error:
        raise RuntimeError(store.error)
    if not task:
        raise RuntimeError("Задача не найдена.")
    panel._render_tasks()
    if action == "remove":
        return {"removed": True}
    if action == "update":
        return {"id": task.id, "updated": True, **changes}
    return asdict(task)


def _log_tail(base_path, payload):
    from core.logging_config import log_directory

    filename = {"application": "pyscribe.log", "activity": "user-actions.log", "crash": "crash.log"}[payload["kind"]]
    with (log_directory(base_path) / filename).open("rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - 65536)
        handle.seek(start)
        lines = handle.read(65536).decode("utf-8", errors="replace").splitlines()
    return {"lines": lines[int(start > 0):][-_limit(payload, maximum=500):]}


def _find_editor(window, path):
    for group in window.editor_groups():
        for index in range(group.count()):
            widget = group.widget(index)
            current = getattr(widget, "file_path", None)
            if current and Path(path) == Path(current).resolve():
                return group, index, widget
    return None


def _edit_explorer(window, payload, root):
    path = Path(payload["path"])
    if payload["action"] == "mkdir":
        path.mkdir()
        return {"path": str(path)}
    if "destination" not in payload:
        raise RuntimeError("Укажи destination.")
    target = Path(payload["destination"])
    if path == root or target.exists() or target.is_symlink():
        raise RuntimeError("Нельзя переместить корень проекта или заменить существующий путь.")
    for group in window.editor_groups():
        for index in range(group.count()):
            current = getattr(group.widget(index), "file_path", None)
            if current and Path(current).resolve().is_relative_to(path):
                raise RuntimeError("Сначала закрой открытые вкладки перемещаемого пути.")
    path.rename(target)
    return {"path": str(target)}


def run_workbench(window, name, payload, complete):
    """Return a result immediately or complete an asynchronous request."""
    root = None
    if "_project_root" in payload:
        payload = dict(payload)
        requested_root = payload.pop("_project_root")
        root = getattr(window.current_project, "root_path", None)
        if not root or not requested_root or Path(root).resolve() != Path(requested_root):
            raise RuntimeError("Открытый проект изменился или недоступен.")
        root = Path(requested_root)
    if name == "explorer_edit":
        return _edit_explorer(window, payload, root)
    if name in ("read_document", "preview_document"):
        from services.workbench_documents import DOCUMENT_SUFFIXES, read_document

        path = Path(payload["path"])
        if not path.is_file():
            raise RuntimeError("Документ не найден.")
        if name == "read_document":
            if path.suffix.lower() not in DOCUMENT_SUFFIXES:
                raise RuntimeError("Для текстовых файлов используй read_file.")
            def document_ready(value=None, *, error=""):
                current = getattr(window.current_project, "root_path", None)
                if not current or Path(current).resolve() != root:
                    complete(error="Открытый проект изменился.")
                    return
                if not error:
                    window.file_manager.open_file(str(path))
                complete(value, error=error)

            return _background(lambda: read_document(path, offset=payload.get("offset", 1),
                                                     limit=payload.get("limit", 5)), document_ready)
        if path.suffix.lower() not in {".html", ".htm", ".md", ".markdown"}:
            raise RuntimeError("Предпросмотр поддерживает HTML и Markdown.")
        found = _find_editor(window, path)
        source = getattr(found[2], "editor", None) if found else None
        window.file_manager.open_markdown_file(str(path), source=source)
        return {"preview": str(path)}
    if name in ("git_read", "git_change"):
        panel = window.git_panel

        def done(value=None, *, error=""):
            complete(value, error=error)
            if name == "git_change" and panel.root_path == str(root):
                if panel.isVisible():
                    panel.refresh()
                else:
                    panel._needs_refresh = True

        return _background(lambda: _git_operation(panel, root, payload), done)
    if name == "explorer_open":
        path = Path(payload["path"])
        if not path.is_file():
            raise RuntimeError("Файл не найден.")
        window.file_manager.open_file(str(path))
        return {"requested": str(path)}
    if name == "explorer_reveal":
        explorer = window.file_explorer
        index = explorer.model.index(payload["path"])
        if not index.isValid():
            raise RuntimeError("Путь не найден в Проводнике.")
        parent = index.parent()
        while parent.isValid() and parent != explorer.rootIndex():
            explorer.expand(parent)
            parent = parent.parent()
        explorer.setCurrentIndex(index)
        explorer.scrollTo(index)
        window.activity_bar.set_activity("explorer")
        return {"selected": payload["path"]}
    if name == "editor_save" or name == "editor_control":
        action = payload.get("action", "save")
        if action == "merge":
            window.unsplit_editor()
        elif action in ("split_right", "split_down"):
            orientation = QtCore.Qt.Orientation.Horizontal if action == "split_right" else QtCore.Qt.Orientation.Vertical
            window.split_editor(orientation=orientation)
        else:
            found = _find_editor(window, payload.get("path", ""))
            if found is None:
                raise RuntimeError("Вкладка файла не открыта.")
            group, index, editor = found
            if action == "select":
                window._set_active_group(group)
                group.setCurrentIndex(index)
            elif action == "close":
                document = getattr(getattr(editor, "editor", None), "document", None)
                if callable(document) and document().isModified():
                    raise RuntimeError("Во вкладке есть несохранённые изменения.")
                window.close_tab_from_group(group, index)
            else:
                window._set_active_group(group)
                group.setCurrentIndex(index)
                if not window.action_save_file():
                    raise RuntimeError("Не удалось сохранить вкладку.")
        return {"action": action}
    if name == "browser_search":
        return _search_browser(window, payload["query"], complete)
    if name == "browser_control":
        return _browser_control(_browser(window), payload)
    if name == "browser_read":
        tab = _browser(window).stack.currentWidget()
        if tab is None or not tab.showing_web():
            raise RuntimeError("Открой страницу браузера.")
        view = tab.view
        page = view.page()
        if page.isLoading():
            raise RuntimeError("Страница загружается. Повтори запрос после загрузки.")
        url = view.url()
        if url.scheme() not in ("http", "https"):
            raise RuntimeError("Можно прочитать страницу HTTP или HTTPS.")
        public_url = url.toString(QtCore.QUrl.UrlFormattingOption.RemoveUserInfo)

        def page_read(value):
            try:
                stale = view.url() != url or page.isLoading()
            except RuntimeError:
                stale = True
            if not isinstance(value, str):
                complete(error="Страница закрылась во время чтения.")
            elif stale:
                complete(error="Адрес страницы изменился во время чтения.")
            else:
                complete({"url": public_url, "text": value[:16000], "truncated": len(value) > 16000})

        page.runJavaScript(
            "(document.body?.innerText || '').slice(0, 16001)",
            page_read,
        )
        return PENDING
    if name == "analysis_details" or name == "dependency_graph":
        panel = window.intelligence_panel
        engine = panel.engine
        mode = payload.get("mode", "modules")
        if name == "dependency_graph" and payload.get("show"):
            if not window.extension_enabled("intelligence"):
                raise RuntimeError("Расширение анализа проекта отключено.")
            window.open_graph_editor(engine, mode)
        if engine is None or not window._analysis_ready(engine):
            return {"ready": False}
        if name == "analysis_details":
            return _analysis_details(engine.analysis, payload)
        nodes, edges = engine.graph_data(mode)
        return {"ready": True, "nodes_total": len(nodes), "edges_total": len(edges),
                "nodes": [asdict(node) for node in _page(nodes, payload)],
                "edges": [asdict(edge) for edge in _page(edges, payload)]}
    if name == "lsp_query":
        from core.code_analysis.service import UnifiedCodeAnalysisService

        service = UnifiedCodeAnalysisService.get_instance()
        path = Path(payload["path"])
        if not path.is_file():
            raise RuntimeError("Файл для LSP не найден.")
        found = _find_editor(window, path)
        get_code = getattr(found[2], "get_code", None) if found else None
        source = get_code() if callable(get_code) else None
        if source is not None and payload["action"] in ("definition", "references"):
            service.on_document_changed(str(path), source)
            service.flush_document_change(str(path))
        return _background(lambda: _lsp_query(service, payload, source), complete)
    if name in ("todo_list", "todo_edit"):
        panel = window.todo_panel
        if not panel.root_path:
            raise RuntimeError("Открой проект для работы с TODO.")
        if name == "todo_edit":
            return _todo_edit(panel, payload)
        panel.refresh_tasks()
        if panel.task_store.error:
            raise RuntimeError(panel.task_store.error)
        tasks = panel.task_store.tasks
        return {"total": len(tasks), "items": [asdict(task) for task in _page(tasks, payload)]}
    if name in ("project_config", "project_config_edit"):
        config = window.current_project
        if config is None:
            raise RuntimeError("Проект не открыт.")
        if name == "project_config_edit":
            from widgets.ProjectManager import project_manager

            changes = {key: value.strip() if isinstance(value, str) else value for key, value in payload.items()}
            if "name" in changes and not changes["name"]:
                raise RuntimeError("Укажи название проекта.")
            config = replace(config, **changes)
            project_manager.update_project_config(config)
            window.current_project = config
            window.project_indicator.setText(config.name)
            window.update_window_title()
            if "analyzers" in changes and window.extension_enabled("intelligence"):
                window.intelligence_panel.set_project(config.root_path, config.analyzers)
            return changes
        return config.to_dict()
    if name == "terminal_execute":
        command = payload["command"].strip()
        if not command:
            raise RuntimeError("Команда пуста.")
        terminal = window.console_widget.get_terminal()
        tab = terminal._current()
        if tab is not None and tab._busy:
            raise RuntimeError("Терминал занят. Открой новую вкладку или дождись завершения.")
        if not window.terminal_visible and not window.console_widget.isVisible():
            window.toggle_terminal()
        window.console_widget.set_current_tab("Terminal")
        wait = payload.get("wait_seconds", 30)
        if not wait:
            terminal.run_file(command, root, shell=True)
            return {"started": True, "completed": False}
        if tab is None:
            terminal.create_new_tab(root)
            tab = terminal._current()
        return _wait_for_terminal(terminal, tab, command, root, wait, complete)
    if name in ("terminal_output", "terminal_control"):
        terminal = window.console_widget.get_terminal()
        tabs = terminal.tab_widget
        if name == "terminal_output":
            tab = terminal._current()
            if tab is None:
                raise RuntimeError("В терминале нет открытых вкладок.")
            cursor = tab.screen.textCursor()
            cursor.setPosition(tab.screen.prompt_pos)
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.PreviousBlock,
                                QtGui.QTextCursor.MoveMode.KeepAnchor, _limit(payload, maximum=500))
            if tab.screen.prompt_pos - cursor.position() > 24000:
                cursor.setPosition(tab.screen.prompt_pos - 24000, QtGui.QTextCursor.MoveMode.KeepAnchor)
            return {"cwd": str(tab.working_dir), "busy": tab._busy,
                    "output": cursor.selectedText().replace("\u2029", "\n"),
                    "tabs": [tabs.tabText(index) for index in range(tabs.count())],
                    "active_tab": tabs.currentIndex() + 1}
        action = payload["action"]
        if action in ("show", "new"):
            if not window.terminal_visible:
                window.toggle_terminal()
            window.console_widget.set_current_tab("Terminal")
        if action in ("select", "close"):
            index = payload.get("tab", tabs.currentIndex() + 1) - 1
            if not 0 <= index < tabs.count():
                raise RuntimeError("Вкладка терминала не найдена.")
            if action == "select":
                tabs.setCurrentIndex(index)
            else:
                terminal.close_tab(index)
        elif action != "show":
            handler = {"new": terminal.create_new_tab, "clear": terminal.clear_current,
                       "interrupt": terminal.interrupt_current}[action]
            handler()
        return {"action": action}
    if name == "read_log":
        base_path = window.app_manager.base_path
        return _background(lambda: _log_tail(base_path, payload), complete)
    raise RuntimeError(f"Неизвестная операция CodeLane: {name}")


PENDING = object()


def _background(work, complete):
    from services.background import run_async

    run_async(work, on_done=complete, on_error=lambda error: complete(error=error))
    return PENDING


def _wait_for_terminal(terminal, tab, command, root, seconds, complete):
    anchor = QtGui.QTextCursor(tab.screen.document())
    anchor.movePosition(QtGui.QTextCursor.MoveOperation.End)
    anchor.setKeepPositionOnInsert(True)
    timer = QtCore.QTimer(terminal)
    timer.setSingleShot(True)

    def cleanup():
        try:
            timer.stop()
            timer.deleteLater()
        except RuntimeError:
            pass
        try:
            tab.command_finished.disconnect(finished)
            tab.destroyed.disconnect(closed)
        except (RuntimeError, TypeError):
            pass

    def report(completed, code=None):
        end = min(tab.screen.prompt_pos, tab.screen.document().characterCount() - 1)
        start = max(0, anchor.position(), end - 24000)
        cursor = QtGui.QTextCursor(tab.screen.document())
        cursor.setPosition(min(start, end))
        cursor.setPosition(end, QtGui.QTextCursor.MoveMode.KeepAnchor)
        output = cursor.selectedText().replace("\u2029", "\n")
        cleanup()
        complete({"started": True, "completed": completed, "exit_code": code,
                  "output": output, "timed_out": not completed})

    def finished(code):
        report(True, code)

    def closed(*_args):
        cleanup()
        complete(error="Вкладка терминала закрыта.")

    tab.command_finished.connect(finished)
    tab.destroyed.connect(closed)
    timer.timeout.connect(lambda: report(False))
    timer.start(seconds * 1000)
    try:
        terminal.run_file(command, root, shell=True)
    except Exception:
        cleanup()
        raise
    return PENDING


def _git_operation(panel, root, payload):
    def run(args):
        ok, result = panel._run(args, root=str(root))
        if not ok:
            raise RuntimeError(result[:4000] or "Git не выполнил команду.")
        return result

    def has_head():
        return panel._run(["rev-parse", "--verify", "HEAD"], root=str(root))[0]

    if Path(run(["rev-parse", "--show-toplevel"])).resolve() != root:
        raise RuntimeError("Открой корень репозитория Git как проект.")
    action = payload["action"]
    if action == "status":
        result = run(["status", "--short", "--branch"])
    elif action == "log":
        result = run(["log", "-20", "--oneline"]) if has_head() else "В репозитории пока нет коммитов."
    elif action == "branches":
        result = run(["branch", "--list", "--no-color"])
    elif action == "commit":
        message = payload.get("message", "").strip()
        if not message:
            raise RuntimeError("Укажи сообщение коммита.")
        result = run(["commit", "-m", message])
    else:
        path = payload.get("path")
        if action in {"stage", "unstage"} and not path:
            raise RuntimeError("Укажи путь к файлу.")
        if path:
            target = Path(path).relative_to(root)
            if action in {"stage", "unstage"} and (target == Path(".") or Path(path).is_dir()):
                raise RuntimeError("Укажи отдельный файл.")
            path_args = ["--", target.as_posix()]
        else:
            path_args = []
        if action == "stage":
            result = run(["--literal-pathspecs", "add", *path_args])
        elif action == "unstage":
            if has_head():
                result = run(["--literal-pathspecs", "restore", "--staged", *path_args])
            else:
                result = run(["update-index", "--force-remove", *path_args])
        else:
            args = ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv"]
            if payload.get("staged"):
                args.append("--cached")
            result = run([*args, *path_args])
    return {"output": result[:24000], "truncated": len(result) > 24000}

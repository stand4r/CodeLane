"""Process and thread usage for launches belonging to the active project."""
import time

import psutil
from PyQt6 import QtCore, QtWidgets

from services.background import run_async


def collect_snapshot(roots, previous):
    """Sample CPU/RAM per process and thread (runs on a Python worker thread)."""
    now = time.monotonic()
    rows, samples, processes = [], {}, {}
    for pid in roots:
        try:
            root = psutil.Process(pid)
            processes[pid] = root
            for child in root.children(recursive=True):
                processes[child.pid] = child
        except (psutil.Error, OSError):
            continue
    cores = psutil.cpu_count() or 1

    def usage(key, seconds):
        samples[key] = (now, seconds)
        before = previous.get(key)
        if before is None or now <= before[0]:
            return None
        return max(0.0, (seconds - before[1]) / (now - before[0]) * 100 / cores)

    for pid, process in processes.items():
        try:
            with process.oneshot():
                born = process.create_time()
                key = (pid, born)
                cpu = process.cpu_times()
                row = dict(key=key, pid=pid, parent=process.ppid(), name=process.name(),
                           status=process.status(), memory=process.memory_info().rss,
                           cpu=usage(key, cpu.user + cpu.system), threads=[])
            for thread in process.threads():
                row["threads"].append((thread.id, usage(
                    (pid, born, thread.id), thread.user_time + thread.system_time)))
            rows.append(row)
        except (psutil.Error, OSError):
            continue
    return rows, samples


class ProjectTaskManager(QtWidgets.QWidget):
    def __init__(self, terminal, parent=None):
        super().__init__(parent)
        self.terminal = terminal
        self.worker = None
        self.previous = {}
        self.session = None
        layout = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Процесс / поток", "PID / TID", "CPU", "RAM", "Потоки", "Состояние"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 270)
        self.tree.setToolTip("CPU — доля общей мощности компьютера. RAM — рабочая память процесса. "
                             "Потоки используют общую память процесса.")
        layout.addWidget(self.tree)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        session = self.terminal._session_key
        if session != self.session:
            self.session = session
            self.previous = {}
            self.tree.clear()
        if not session:
            self.summary.setText("Откройте проект, чтобы видеть его процессы.")
            self.tree.clear()
            return
        if self.worker is not None:
            return
        roots = []
        for tab in self.terminal.tabs.values():
            for process in (tab.process, getattr(tab, "_run_process", None)):
                if process is not None and process.state() != QtCore.QProcess.ProcessState.NotRunning:
                    pid = int(process.processId())
                    if pid:
                        roots.append(pid)
        self.summary.setText("Процессы терминалов и запусков проекта · " + session)
        # The snapshot is sampled on the shared Python pool, never on a Qt thread.
        previous = self.previous
        self.worker = run_async(
            lambda: collect_snapshot(roots, previous),
            parent=None,
            on_done=lambda result, sess=session: self._snapshot_ready(result, sess),
            on_error=lambda _message: self.query_finished())

    def _snapshot_ready(self, result, session):
        self.query_finished()
        if session != self.terminal._session_key:
            return
        self.accept_snapshot(result)

    def query_finished(self):
        self.worker = None

    @staticmethod
    def cpu_text(value):
        return "…" if value is None else f"{value:.1f}%"

    def accept_snapshot(self, result):
        rows, self.previous = result
        expanded = set()
        selected = self.tree.currentItem()
        selected_key = selected.data(0, QtCore.Qt.ItemDataRole.UserRole) if selected else None
        iterator = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if item.isExpanded():
                expanded.add(item.data(0, QtCore.Qt.ItemDataRole.UserRole))
            iterator += 1
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        items = {}
        for row in rows:
            item = QtWidgets.QTreeWidgetItem([
                row["name"], str(row["pid"]), self.cpu_text(row["cpu"]),
                f'{row["memory"] / 1048576:.1f} МБ', str(len(row["threads"])), row["status"]])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row["key"])
            items[row["pid"]] = item
        for row in rows:
            item = items[row["pid"]]
            parent = items.get(row["parent"])
            if parent is not None and parent is not item:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            for tid, cpu in row["threads"]:
                thread = QtWidgets.QTreeWidgetItem(
                    [f"Поток {tid}", str(tid), self.cpu_text(cpu), "Общая", "", ""])
                key = (*row["key"], tid)
                thread.setData(0, QtCore.Qt.ItemDataRole.UserRole, key)
                item.addChild(thread)
                if key == selected_key:
                    self.tree.setCurrentItem(thread)
            item.setExpanded(row["key"] in expanded)
            if row["key"] == selected_key:
                self.tree.setCurrentItem(item)
        self.tree.setUpdatesEnabled(True)
        memory = sum(row["memory"] for row in rows) / 1048576
        self.summary.setText(f"{len(rows)} процессов · {sum(len(r['threads']) for r in rows)} потоков · "
                             f"RAM {memory:.1f} МБ · {session}")

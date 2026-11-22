"""Run pytest in the project's interpreter and display machine-readable results."""

import json
from pathlib import Path

from PyQt6 import QtCore, QtWidgets


_PYTEST_DRIVER = """
import json
import sys
import pytest

class Reports:
    def write(self, report, collection=False):
        if collection and not report.failed:
            return
        if not collection and report.when != "call" and report.passed:
            return
        location = getattr(report, "location", None)
        crash = getattr(report.longrepr, "reprcrash", None)
        path = crash.path if crash else (location[0] if location else report.nodeid.split("::", 1)[0])
        line = crash.lineno if crash else ((location[1] or 0) + 1 if location else 1)
        result = {
            "id": report.nodeid,
            "outcome": report.outcome,
            "path": str(path),
            "line": line,
            "detail": report.longreprtext if not report.passed else "",
            "collection": collection,
        }
        with open(sys.argv[1], "a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\\n")

    def pytest_runtest_logreport(self, report):
        self.write(report)

    def pytest_collectreport(self, report):
        self.write(report, collection=True)

raise SystemExit(pytest.main(["-q", *sys.argv[2:]], plugins=[Reports()]))
"""


class TestResultsPanel(QtWidgets.QWidget):
    run_finished = QtCore.pyqtSignal(int)

    def __init__(self, root, executable, navigate, parent=None):
        super().__init__(parent)
        self.setProperty("pyscribeSpecial", "testResults")
        self.root = Path(root).resolve()
        self.executable = executable
        self.navigate = navigate
        self._report = None
        self._cancelled = False
        self.exit_code = None
        self.nodeid = None
        self.results = []
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        layout.addLayout(toolbar)
        self.run_button = QtWidgets.QPushButton("Запустить все", self)
        self.rerun_button = QtWidgets.QPushButton("Повторить выбранный", self)
        self.open_button = QtWidgets.QPushButton("Открыть строку", self)
        self.stop_button = QtWidgets.QPushButton("Остановить", self)
        self.failures_only = QtWidgets.QCheckBox("Только ошибки", self)
        self.status = QtWidgets.QLabel(self)
        for widget in (self.run_button, self.rerun_button, self.open_button,
                       self.stop_button, self.failures_only, self.status):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderLabels(["Тест", "Результат"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 2)
        self.output = QtWidgets.QPlainTextEdit(self)
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(2000)
        layout.addWidget(self.output, 1)
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.run_button.clicked.connect(lambda: self.start())
        self.rerun_button.clicked.connect(self._rerun)
        self.open_button.clicked.connect(self._open)
        self.stop_button.clicked.connect(self.shutdown)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        self.tree.itemDoubleClicked.connect(self._open)
        self.failures_only.toggled.connect(self._filter)
        self._update_actions()

    def start(self, nodeid=None):
        if self._report is not None:
            return
        folder = QtCore.QTemporaryDir()
        if not folder.isValid():
            self.status.setText("Не удалось создать отчёт тестов.")
            return
        self._report = folder
        self._cancelled = False
        self.exit_code = None
        self.nodeid = nodeid
        self.results = []
        self.tree.clear()
        self.output.clear()
        self.status.setText("Выполняется pytest…")
        self.process.setWorkingDirectory(str(self.root))
        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)
        args = ["-c", _PYTEST_DRIVER, self._report.filePath("results.jsonl")]
        if nodeid:
            args.extend(["--", nodeid])
        self.process.start(self.executable, args)
        self._update_actions()

    def _read_output(self):
        data = bytes(self.process.readAllStandardOutput())
        if data:
            self.output.appendPlainText(data[-200_000:].decode("utf-8", errors="replace").rstrip())

    def _finished(self, code, _exit_status=None):
        if self._report is None:
            return
        self._read_output()
        report = Path(self._report.filePath("results.jsonl"))
        rows = {}
        if report.is_file():
            with report.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    existing = rows.get(row["id"])
                    if existing is None or row["outcome"] == "failed":
                        rows[row["id"]] = row
        self._report = None
        self.exit_code = code
        self.results = sorted(rows.values(), key=lambda row: row["outcome"] != "failed")
        labels = {"passed": "Пройден", "failed": "Ошибка", "skipped": "Пропущен"}
        for row in self.results:
            item = QtWidgets.QTreeWidgetItem([row["id"], labels[row["outcome"]]])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, row)
            item.setToolTip(0, row["detail"])
            self.tree.addTopLevelItem(item)
        failed = sum(row["outcome"] == "failed" for row in self.results)
        status = "Остановлено" if self._cancelled else "Тесты не найдены" if code == 5 else (
            f"Тестов: {len(self.results)} · ошибок: {failed} · код: {code}"
        )
        self.status.setText(status)
        self._filter()
        self._update_actions()
        self.run_finished.emit(code)

    def _process_error(self, error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self.output.appendPlainText(self.process.errorString())
            self._finished(-1)

    def _filter(self):
        for index, row in enumerate(self.results):
            item = self.tree.topLevelItem(index)
            hidden = self.failures_only.isChecked() and row["outcome"] != "failed"
            if hidden and item is self.tree.currentItem():
                self.tree.setCurrentItem(None)
            item.setHidden(hidden)
        self._update_actions()

    def _selected(self):
        item = self.tree.currentItem()
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None

    def _update_actions(self):
        running = self._report is not None
        selected = self._selected()
        self.run_button.setEnabled(not running)
        self.rerun_button.setEnabled(not running and bool(selected and not selected["collection"]))
        self.open_button.setEnabled(bool(selected))
        self.stop_button.setEnabled(running)

    def _rerun(self):
        row = self._selected()
        if row and not row["collection"]:
            self.start(row["id"])

    def _open(self, *_):
        row = self._selected()
        if not row:
            return
        path = (self.root / row["path"]).resolve()
        if path.is_relative_to(self.root) and path.is_file():
            self.navigate(str(path), row["line"])
        else:
            test_file = (self.root / row["id"].split("::", 1)[0]).resolve()
            if test_file.is_relative_to(self.root) and test_file.is_file():
                self.navigate(str(test_file), 1)

    def shutdown(self):
        if self._report is not None:
            self._cancelled = True
            self.process.kill()
            self.process.waitForFinished(1500)

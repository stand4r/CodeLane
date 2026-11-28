"""PyScribe widgets/ProjectIntelligenceWidget.py: application module and reusable component."""
from __future__ import annotations

from pathlib import Path
import math
import json
import time
from datetime import datetime
from typing import Optional

from design.icons import line_icon, set_line_icon
from PyQt6 import QtCore, QtGui, QtWidgets

from core.logging_config import get_logger
from core.project_intelligence import ProjectAnalysis, ProjectIntelligenceEngine, FileInfo, Issue
from services.background import run_async
from services.code_intelligence_service import CodeIntelligenceService
from widgets.LoadingSpinner import LoadingOverlay
from widgets.Dialog import CustomDialog

logger = get_logger("intelligence")
def _ui_font(point_size: int = 9, weight=None, bold=False):
    """Neutral UI typography for analysis labels, metrics and graph painters."""
    font = QtGui.QFont("Segoe UI", point_size)
    font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
    if bold:
        font.setWeight(QtGui.QFont.Weight.DemiBold)
    elif weight is not None:
        font.setWeight(weight)
    return font


def _ui_font_family() -> str:
    return "Segoe UI"


# ----------------------------------------------------------------------
# Unified palette for the analysis view.
# The panel used to mix cyan (#00F2FE), green gradients (#10B981), purple
# (#8E74AA) and a blue-tinted navy (#16202E) that clashed with the neutral
# greys used everywhere else in the workbench. Everything here is now neutral
# grey + a single accent; colour is reserved for genuine severity meaning.
# ----------------------------------------------------------------------
class Palette:
    """Neutral surfaces + one accent, matching the workbench theme."""

    # Surfaces (same family as the rest of the app)
    SURFACE = "#1F1F1F"          # cards / panels
    BG_CARD = SURFACE
    SURFACE_ALT = "#252526"      # inputs, chips
    TRACK = "#2D2D30"            # gauge track, progress backgrounds
    BORDER = "#2B2B2B"           # hairline borders
    BORDER_STRONG = "#3A3A3C"    # emphasised borders / hover

    # Text
    TEXT = "#D4D4D4"
    TEXT_STRONG = "#EDEDED"
    TEXT_PRIMARY = TEXT_STRONG
    TEXT_MUTED = "#8B949E"

    # The only accent: VS Code blue
    ACCENT = "#0078D4"
    ACCENT_HOVER = "#1A8BE0"
    ACCENT_TEXT = "#4DA3E8"      # accent used as text on dark surfaces

    # Severity — the only place colour carries meaning
    DANGER = "#E5534B"
    WARNING = "#C9A227"
    OK = "#3FB950"

    @classmethod
    def severity(cls, level: str) -> str:
        return {"high": cls.DANGER, "medium": cls.WARNING,
                "low": cls.TEXT_MUTED}.get(str(level).lower(), cls.TEXT_MUTED)


# Two button styles only: one accent-filled primary, one quiet secondary.
def primary_button_qss(padding: str = "4px 12px", radius: int = 6) -> str:
    return (
        'QToolButton {  font-weight: 600; color: #FFFFFF;'
        f' background: {Palette.ACCENT}; border: 1px solid {Palette.ACCENT};'
        f' border-radius: {radius}px; padding: {padding}; }}'
        f'QToolButton:hover {{ background: {Palette.ACCENT_HOVER};'
        f' border-color: {Palette.ACCENT_HOVER}; }}'
    )


def secondary_button_qss(padding: str = "4px 12px", radius: int = 6) -> str:
    return (
        'QToolButton {  font-weight: 500;'
        f' color: {Palette.TEXT}; background: {Palette.SURFACE_ALT};'
        f' border: 1px solid {Palette.BORDER_STRONG};'
        f' border-radius: {radius}px; padding: {padding}; }}'
        f'QToolButton:hover {{ background: {Palette.TRACK};'
        f' border-color: {Palette.TEXT_MUTED}; }}'
    )


class AnalysisWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, str)
    finished = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, engine: ProjectIntelligenceEngine, should_stop=None):
        super().__init__()
        self.engine = engine
        self._should_stop = should_stop
        self._last_progress_at = 0.0

    @QtCore.pyqtSlot()
    def run(self):
        try:
            result = self.engine.analyze(self._progress)
            self._emit(self.finished, result)
        except Exception as exc:
            self._emit(self.failed, str(exc))

    def _emit(self, signal, payload):
        """Emit safely: the worker may already be deleted during shutdown."""
        try:
            signal.emit(payload)
        except RuntimeError:
            pass

    def _progress(self, current, total, name):
        if self._should_stop is not None and self._should_stop():
            raise InterruptedError("Project analysis cancelled")
        now = time.monotonic()
        if current < total and now - self._last_progress_at < 0.12:
            return
        self._last_progress_at = now
        try:
            self.progress.emit(current, total, name)
        except RuntimeError:
            # The dashboard (and this worker) can be gone while the analysis
            # thread is still finishing; that must not abort the thread.
            pass


class AdversaryWorker(QtCore.QObject):
    """Runs «Адвокат дьявола» off the UI thread.

    Each case spawns a subprocess with a timeout, so a full run takes a few
    seconds — far too long to block the window.
    """

    finished = QtCore.pyqtSignal(object)

    def __init__(self, path: str, timeout: int = 8):
        super().__init__()
        self.path = str(path)
        self.timeout = timeout

    @QtCore.pyqtSlot()
    def run(self):
        from core.adversary import AdversaryReport, AdversaryRunner
        runner = AdversaryRunner(timeout=self.timeout)
        try:
            report = runner.run_file(self.path)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing
            report = AdversaryReport(module_path=self.path,
                                     error=f"Ошибка испытания: {exc}")
        finally:
            runner.dispose()
        self.finished.emit(report)


class GraphNodeItem(QtWidgets.QGraphicsObject):
    toggled = QtCore.pyqtSignal(str, bool)
    activated = QtCore.pyqtSignal(str)
    moved = QtCore.pyqtSignal(str, object)

    def __init__(self, node, expanded=False, parent=None):
        super().__init__(parent)
        self.node = node
        self.expanded = expanded
        self.w = 190
        self.h = 64
        self.setAcceptHoverEvents(True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(2)
        self._hover = False

    def boundingRect(self):
        return QtCore.QRectF(0, 0, self.w, self.h)

    def paint(self, painter, option, widget=None):
        p = painter
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        base = self.scene().views()[0] if self.scene() and self.scene().views() else None
        palette = base.palette() if base is not None else QtWidgets.QApplication.palette()
        bg = palette.alternateBase().color()
        border = palette.highlight().color()
        fg = palette.text().color()
        if self._hover:
            bg = palette.mid().color()
        p.setPen(QtGui.QPen(QtGui.QColor(border), 1.4))
        p.setBrush(QtGui.QBrush(QtGui.QColor(bg)))
        p.drawRoundedRect(0, 0, self.w, self.h, 9, 9)

        kind = self.node.kind.upper()
        p.setPen(QtGui.QColor(border))
        p.setFont(_ui_font(8, weight=QtGui.QFont.Weight.Medium))
        p.drawText(12, 17, kind)
        p.setPen(QtGui.QColor(fg))
        p.setFont(_ui_font(10, weight=QtGui.QFont.Weight.Medium))
        name = self.node.name
        if len(name) > 24:
            name = name[:22] + '…'
        p.drawText(12, 37, name)
        p.setPen(QtGui.QColor(Palette.TEXT_MUTED))
        p.setFont(_ui_font(8))
        p.drawText(12, 53, f'line {self.node.line}' if self.node.line else '')

        if self.node.kind in {'module', 'class', 'function', 'method'}:
            p.setPen(QtGui.QColor(Palette.TEXT_MUTED))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.w - 26, 20, 14, 14, 4, 4)
            p.drawLine(self.w - 22, 27, self.w - 16, 27)
            if not self.expanded:
                p.drawLine(self.w - 19, 24, self.w - 19, 30)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.node.kind in {'module', 'class', 'function', 'method'}:
            if QtCore.QRectF(self.w - 34, 10, 30, 44).contains(event.pos()):
                self.expanded = not self.expanded
                self.toggled.emit(self.node.id, self.expanded)
                event.accept()
                return
        super().mousePressEvent(event)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.moved.emit(self.node.id, value)
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):
        self.activated.emit(self.node.file)
        event.accept()


class ProjectGraphView(QtWidgets.QGraphicsView):
    node_activated = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('projectGraphView')
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QtGui.QColor('#14181D'))
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._nodes = {}
        self._node_data = []
        self._edge_data = []
        self._expanded = set()
        self._mode = 'modules'
        self._search = ''
        self._layout_positions = {}
        self._did_initial_fit = False
        self._edge_items = []

    def clear_graph(self):
        self.scene().clear()
        self._nodes.clear()
        self._node_data = []
        self._edge_data = []
        self._layout_positions = {}
        self._did_initial_fit = False

    def set_graph(self, nodes, edges, mode='modules'):
        self._mode = mode
        self._node_data = list(nodes)
        self._edge_data = list(edges)
        self._did_initial_fit = False
        self._render()

    def set_search(self, text):
        self._search = (text or '').strip().lower()
        for node_id, item in self._nodes.items():
            item.setOpacity(1.0 if not self._search or self._search in item.node.name.lower() or self._search in item.node.file.lower() else 0.18)

    def _visible_nodes(self):
        nodes = {n.id: n for n in self._node_data}
        roots = [n for n in self._node_data if not n.parent_id or n.parent_id not in nodes]
        visible = []
        def walk(node):
            visible.append(node)
            if node.id in self._expanded:
                for child in self._node_data:
                    if child.parent_id == node.id:
                        walk(child)
        for root in roots:
            walk(root)
        return visible

    def _force_layout(self, visible):
        """Deterministic force layout with strong node repulsion and generous spacing."""
        import math
        n = len(visible)
        if not n:
            return {}
        ids = [x.id for x in visible]
        index = {x: i for i, x in enumerate(ids)}
        # Start on a large spiral so nodes never begin stacked in the same place.
        pos = {}
        radius_step = 250.0
        for i, node_id in enumerate(ids):
            angle = i * 2.3999632297
            radius = 180.0 + radius_step * math.sqrt(i / max(1, n - 1))
            pos[node_id] = [math.cos(angle) * radius, math.sin(angle) * radius]
        edges = [(e.source, e.target) for e in self._edge_data if e.source in index and e.target in index and e.source != e.target]
        area = max(160000.0, n * 30000.0)
        k = max(260.0, math.sqrt(area / max(1, n)))
        for _ in range(min(260, 100 + n * 5)):
            disp = {x: [0.0, 0.0] for x in ids}
            for i, a in enumerate(ids):
                ax, ay = pos[a]
                for b in ids[i+1:]:
                    bx, by = pos[b]
                    dx, dy = ax-bx, ay-by
                    d2 = max(100.0, dx*dx + dy*dy)
                    d = math.sqrt(d2)
                    force = (k*k) / d
                    fx, fy = dx/d*force, dy/d*force
                    disp[a][0] += fx; disp[a][1] += fy
                    disp[b][0] -= fx; disp[b][1] -= fy
            for a, b in edges:
                ax, ay = pos[a]; bx, by = pos[b]
                dx, dy = bx-ax, by-ay
                d = max(1.0, math.sqrt(dx*dx+dy*dy))
                force = (d*d) / k
                fx, fy = dx/d*force, dy/d*force
                disp[a][0] += fx; disp[a][1] += fy
                disp[b][0] -= fx; disp[b][1] -= fy
            for node_id in ids:
                pos[node_id][0] += max(-55.0, min(55.0, disp[node_id][0] * 0.018))
                pos[node_id][1] += max(-55.0, min(55.0, disp[node_id][1] * 0.018))
        minx = min(v[0] for v in pos.values()); miny = min(v[1] for v in pos.values())
        return {node_id: QtCore.QPointF(v[0]-minx+140, v[1]-miny+140) for node_id, v in pos.items()}

    def _render(self):
        self.scene().clear(); self._nodes.clear()
        visible = self._visible_nodes()
        visible_ids = {n.id for n in visible}
        if not visible:
            return
        positions = self._force_layout(visible)
        self._layout_positions = positions

        # All edges share one muted tone; emphasis comes from width, not hue.
        # (Previously each relation kind had its own colour: 5 hues at once.)
        edge_pen_colour = QtGui.QColor(Palette.BORDER_STRONG)
        edge_widths = {'imports': 1.8, 'calls': 1.4, 'inherits': 1.4,
                       'contains': 1.0, 'exports': 1.0}
        self._edge_items = []
        for e in self._edge_data:
            if e.source not in visible_ids or e.target not in visible_ids:
                continue
            pen = QtGui.QPen(edge_pen_colour, edge_widths.get(e.kind, 1.2))
            pen.setCosmetic(True)
            item = self.scene().addPath(QtGui.QPainterPath(), pen)
            item.setZValue(0)
            self._edge_items.append((item, e.source, e.target))
        for n in visible:
            item = GraphNodeItem(n, n.id in self._expanded)
            item.setPos(positions[n.id])
            item.toggled.connect(self._toggle_node)
            item.activated.connect(self.node_activated.emit)
            item.moved.connect(self._node_moved)
            self.scene().addItem(item)
            self._nodes[n.id] = item
        self._update_edges()
        self.set_search(self._search)
        rect = self.scene().itemsBoundingRect().adjusted(-100, -100, 100, 100)
        self.scene().setSceneRect(rect)
        if not self._did_initial_fit:
            self.fitInView(rect, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            self._did_initial_fit = True

    def _node_moved(self, node_id, position):
        self._layout_positions[node_id] = QtCore.QPointF(position)
        self._update_edges()
        self.scene().setSceneRect(self.scene().itemsBoundingRect().adjusted(-140, -140, 140, 140))

    def _update_edges(self):
        for item, source, target in self._edge_items:
            a = self._nodes.get(source)
            b = self._nodes.get(target)
            if not a or not b:
                item.setPath(QtGui.QPainterPath())
                continue
            ar = a.sceneBoundingRect()
            br = b.sceneBoundingRect()
            start = QtCore.QPointF(ar.right(), ar.center().y())
            end = QtCore.QPointF(br.left(), br.center().y())
            if end.x() < start.x():
                start, end = QtCore.QPointF(ar.left(), ar.center().y()), QtCore.QPointF(br.right(), br.center().y())
            dx = max(70.0, abs(end.x() - start.x()) * 0.42)
            path = QtGui.QPainterPath(start)
            path.cubicTo(start.x() + dx, start.y(), end.x() - dx, end.y(), end.x(), end.y())
            item.setPath(path)

    def _toggle_node(self, node_id, expanded):
        if expanded: self._expanded.add(node_id)
        else: self._expanded.discard(node_id)
        self._render()

    def expand_all(self):
        self._expanded = {n.id for n in self._node_data if n.kind in {'module','class','function','method','binary'}}
        self._render()

    def collapse_all(self):
        self._expanded.clear(); self._render()

    def zoom_in(self): self.scale(1.18, 1.18)
    def zoom_out(self): self.scale(0.847, 0.847)
    def reset_zoom(self):
        rect = self.scene().sceneRect()
        if rect.isValid(): self.fitInView(rect, QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            self.scale(1.15 if event.angleDelta().y() > 0 else 0.87, 1.15 if event.angleDelta().y() > 0 else 0.87)
            event.accept(); return
        super().wheelEvent(event)


class ProjectGraphEditorWidget(QtWidgets.QWidget):
    """Full editor-tab graph workbench.

    The graph deliberately lives in the editor area rather than the narrow
    activity/sidebar column. All graph navigation, filtering and zooming are
    available here without competing with the project tree.
    """
    open_file_requested = QtCore.pyqtSignal(str, int)

    def __init__(self, engine, mode='modules', parent=None):
        super().__init__(parent)
        self.engine = engine
        self.analysis = getattr(engine, 'analysis', None)
        self.mode = mode
        self.setObjectName('projectGraphEditor')

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        root.addWidget(self._build_header())
        root.addWidget(self._build_controls())

        self.graph = ProjectGraphView()
        self.graph.node_activated.connect(self._open_node)
        root.addWidget(self.graph, 1)
        self.loading_overlay = LoadingOverlay(self.graph)
        self._populate()

    def _build_header(self) -> QtWidgets.QFrame:
        header = QtWidgets.QFrame()
        header.setObjectName('graphEditorHeader')
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(8, 4, 8, 4)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(0)
        self.title = QtWidgets.QLabel('Граф проекта')
        self.title.setObjectName('graphEditorTitle')
        self.subtitle = QtWidgets.QLabel('Связи и зависимости проекта')
        self.subtitle.setObjectName('graphEditorSubtitle')
        title_box.addWidget(self.title)
        title_box.addWidget(self.subtitle)
        hl.addLayout(title_box)
        hl.addStretch()
        self.status = QtWidgets.QLabel('')
        self.status.setObjectName('graphEditorStatus')
        hl.addWidget(self.status)
        return header

    def _build_controls(self) -> QtWidgets.QFrame:
        controls = QtWidgets.QFrame()
        controls.setObjectName('graphEditorToolbar')
        grid = QtWidgets.QGridLayout(controls)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)

        grid.addWidget(QtWidgets.QLabel('Вид'), 0, 0)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setMinimumWidth(150)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        grid.addWidget(self.mode_combo, 0, 1)

        grid.addWidget(QtWidgets.QLabel('Файл'), 0, 2)
        self.file_combo = QtWidgets.QComboBox()
        self.file_combo.setMinimumWidth(220)
        self.file_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.file_combo.currentIndexChanged.connect(self._filters_changed)
        grid.addWidget(self.file_combo, 0, 3, 1, 2)

        grid.addWidget(QtWidgets.QLabel('Функция'), 0, 5)
        self.function_combo = QtWidgets.QComboBox()
        self.function_combo.setMinimumWidth(220)
        self.function_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.function_combo.currentIndexChanged.connect(self._filters_changed)
        grid.addWidget(self.function_combo, 0, 6, 1, 2)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText('Поиск узлов, файлов, символов…')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.graph_search)
        grid.addWidget(self.search, 1, 0, 1, 4)

        self.expand = QtWidgets.QToolButton(); self.expand.setText('Развернуть всё'); self.expand.clicked.connect(self.graph_expand)
        self.collapse = QtWidgets.QToolButton(); self.collapse.setText('Свернуть всё'); self.collapse.clicked.connect(self.graph_collapse)
        self.zoom_out_button = QtWidgets.QToolButton(); set_line_icon(self.zoom_out_button, 'minus'); self.zoom_out_button.setToolTip('Уменьшить'); self.zoom_out_button.clicked.connect(lambda: self.graph.zoom_out())
        self.zoom_in_button = QtWidgets.QToolButton(); set_line_icon(self.zoom_in_button, 'add'); self.zoom_in_button.setToolTip('Увеличить'); self.zoom_in_button.clicked.connect(lambda: self.graph.zoom_in())
        self.fit_button = QtWidgets.QToolButton(); self.fit_button.setText('По размеру'); self.fit_button.clicked.connect(lambda: self.graph.reset_zoom())
        self.open_button = QtWidgets.QToolButton(); self.open_button.setText('Открыть исходник'); self.open_button.clicked.connect(self._open_selected)
        for col, widget in enumerate((self.expand, self.collapse, self.zoom_out_button, self.zoom_in_button, self.fit_button, self.open_button), start=4):
            grid.addWidget(widget, 1, col)

        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(6, 2)
        return controls

    def _available_modes(self):
        a = self.analysis
        if not a:
            return []
        available = [('Модули', 'modules')] if a.files else []
        kinds = {n.kind for n in a.structure_nodes}
        if any(k in kinds for k in {'class', 'struct', 'interface', 'enum'}):
            available.append(('Классы', 'classes'))
        if any(k in kinds for k in {'function', 'method', 'constructor'}):
            available.append(('Функции', 'functions'))
        if any(k == 'variable' for k in kinds):
            available.append(('Переменные', 'variables'))
        if a.binaries:
            available.append(('Бинарные объекты', 'binaries'))
        if any(e.kind == 'imports' for e in a.structure_edges) or any(f.imports for f in a.files.values()):
            available.append(('Импорты', 'imports'))
        if any(e.kind == 'exports' for e in a.structure_edges) or any(b.exports for b in a.binaries.values()):
            available.append(('Экспорты', 'exports'))
        return available

    def _populate(self):
        if not self.analysis:
            self.status.setText('Нет данных анализа')
            return
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for label, value in self._available_modes():
            self.mode_combo.addItem(label, value)
        idx = self.mode_combo.findData(self.mode)
        self.mode_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.mode_combo.blockSignals(False)
        self._populate_files()
        self._refresh_graph()

    def _populate_files(self):
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.addItem('Все файлы проекта', '')
        if self.analysis:
            for info in sorted(self.analysis.files.values(), key=lambda x: x.module.lower()):
                self.file_combo.addItem(info.module, info.path)
        self.file_combo.blockSignals(False)
        self._populate_functions()

    def _populate_functions(self):
        self.function_combo.blockSignals(True)
        self.function_combo.clear()
        self.function_combo.addItem('Все функции', '')
        path = self.file_combo.currentData()
        if self.analysis and path:
            for n in sorted(self.analysis.structure_nodes, key=lambda x: (x.line, x.name.lower())):
                if n.file == path and n.kind in {'function', 'method'}:
                    self.function_combo.addItem(n.name, n.id)
        self.function_combo.blockSignals(False)

    def _mode_changed(self):
        self.mode = self.mode_combo.currentData() or 'modules'
        self._refresh_graph()

    def _filters_changed(self):
        self._populate_functions()
        self._refresh_graph()

    def _refresh_graph(self):
        if not self.engine or not self.analysis:
            return
        mode = self.mode_combo.currentData() or self.mode
        file_path = self.file_combo.currentData() or None
        function_id = self.function_combo.currentData() or None
        if hasattr(self, 'loading_overlay'):
            self.loading_overlay.start('Построение графа зависимостей...', 'Расчёт связей и расположения узлов...')
            QtWidgets.QApplication.processEvents()
        nodes, edges = self.engine.graph_data(mode, file_path, function_id)
        self.graph.set_graph(nodes, edges, mode)
        if hasattr(self, 'loading_overlay'):
            self.loading_overlay.stop()
        self.file_combo.setEnabled(mode in {'classes', 'functions', 'variables'})
        self.function_combo.setEnabled(mode == 'variables')
        mode_names = {'modules': 'Модули', 'classes': 'Классы', 'functions': 'Функции', 'variables': 'Переменные', 'binaries': 'Бинарные объекты', 'imports': 'Импорты', 'exports': 'Экспорты'}
        mode_label = mode_names.get(mode, mode.title())
        self.status.setText(f'{len(nodes)} узлов · {len(edges)} связей')
        self.subtitle.setText(f'Вид: {mode_label} · перетаскивайте узлы, колесико мыши для масштабирования')

    def graph_search(self, text):
        self.graph.set_search(text)

    def graph_expand(self):
        self.graph.expand_all()

    def graph_collapse(self):
        self.graph.collapse_all()

    def _open_node(self, file_path):
        if file_path:
            self.open_file_requested.emit(file_path, 1)

    def _open_selected(self):
        items = self.graph.scene().selectedItems()
        if items and hasattr(items[0], 'node'):
            self._open_node(items[0].node.file)

    def set_mode(self, mode):
        idx = self.mode_combo.findData(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)

    def refresh(self):
        self.analysis = getattr(self.engine, 'analysis', None)
        self._populate()

class QualityBadgeWidget(QtWidgets.QWidget):
    """Pristine circular quality gauge displaying exact numeric percentage and radial score arc."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(62, 62)
        self._score = 0.0
        self._color = QtGui.QColor(Palette.TEXT_MUTED)
        self._bg_color = QtGui.QColor(Palette.SURFACE_ALT)

    def setText(self, text: str):
        try:
            self._score = float(str(text).replace('%', '').strip())
        except Exception:
            self._score = 0.0
        self.update()

    def setStyleSheet(self, style: str):
        pass  # Painted via antialiased QPainter

    def set_grade(self, letter: str, color_hex: str, bg_hex: str, score: float = 0):
        self._color = QtGui.QColor(color_hex)
        self._bg_color = QtGui.QColor(bg_hex)
        self._score = float(score) if score is not None else 0.0
        self.update()

    def set_score(self, score: float, color_hex: str = None, bg_hex: str = None):
        self._color = QtGui.QColor(color_hex or Palette.ACCENT)
        self._bg_color = QtGui.QColor(bg_hex or Palette.SURFACE_ALT)
        self._score = float(score) if score is not None else 0.0
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)

        size = min(self.width(), self.height()) - 8
        rect = QtCore.QRectF(4, 4, size, size)

        # Background circular track
        track_pen = QtGui.QPen(QtGui.QColor(Palette.TRACK), 3.5)
        painter.setPen(track_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

        # Active circular progress arc
        if self._score > 0:
            arc_pen = QtGui.QPen(self._color, 3.5, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)
            start_angle = 90 * 16  # 12 o'clock
            span_angle = -int((min(100.0, max(0.0, self._score)) / 100.0) * 360 * 16)
            painter.drawArc(rect, start_angle, span_angle)

        # Exact numeric metric
        if self._score > 0:
            val_font = _ui_font(14, bold=True)
            val_font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
            painter.setFont(val_font)
            painter.setPen(QtGui.QColor('#FFFFFF'))
            score_text = f"{int(round(self._score))}"
            val_rect = QtCore.QRectF(rect.x(), rect.y() + 6, rect.width(), rect.height() * 0.45)
            painter.drawText(val_rect, QtCore.Qt.AlignmentFlag.AlignCenter, score_text)

            sub_font = _ui_font(8, weight=QtGui.QFont.Weight.DemiBold)
            sub_font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
            painter.setFont(sub_font)
            painter.setPen(self._color)
            sub_rect = QtCore.QRectF(rect.x(), rect.y() + rect.height() * 0.50, rect.width(), rect.height() * 0.35)
            painter.drawText(sub_rect, QtCore.Qt.AlignmentFlag.AlignCenter, "%")
        else:
            font = _ui_font(16, bold=True)
            font.setStyleStrategy(QtGui.QFont.StyleStrategy.PreferAntialias)
            painter.setFont(font)
            painter.setPen(QtGui.QColor('#8B949E'))
            painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, "—")


class ProjectIntelligenceDashboard(QtWidgets.QWidget):
    analysis_ready = QtCore.pyqtSignal(object)
    analysis_failed = QtCore.pyqtSignal(str)
    open_file_requested = QtCore.pyqtSignal(str, int)
    open_graph_editor_requested = QtCore.pyqtSignal(object, str)
    open_defense_guide_requested = QtCore.pyqtSignal()
    open_analyzers_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('projectIntelligence')
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty('pyscribeSpecial', 'projectAnalysis')
        self.editor_path_hint = None
        self._wide_layout = None
        self.engine: Optional[ProjectIntelligenceEngine] = None
        self.analysis: Optional[ProjectAnalysis] = None
        self._thread = None
        self._worker_engine = None
        self._rendered_analysis = None
        self._worker = None
        self._analysis_generation = 0
        self._refresh_pending = False
        self._last_progress_at = 0.0
        self.enabled_analyzers = {'structure', 'dependencies', 'health'}
        self.student_tips = []
        self._build()

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        # Header
        header = QtWidgets.QFrame()
        header.setObjectName('intelligenceHeader')
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(4, 0, 4, 0)
        hl.setSpacing(6)

        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(1)
        title = QtWidgets.QLabel('Анализ проекта')
        title.setObjectName('intelligenceTitle')
        title.setStyleSheet(
            ' font-size: 16px; font-weight: 600;'
            f' color: {Palette.TEXT_STRONG};')
        self.status = QtWidgets.QLabel('Проект не открыт')
        self.status.setObjectName('intelligenceStatus')
        self.status.setStyleSheet(
            ' font-size: 11px;'
            f' color: {Palette.TEXT_MUTED};')
        title_box.addWidget(title)
        title_box.addWidget(self.status)
        hl.addLayout(title_box, 1)

        # Status lines are informative, not width-defining: without this the
        # long "Файлов: N · M мс" text forced the panel to ~800 px.
        for label in (title, self.status):
            label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                QtWidgets.QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)

        self.refresh_button = QtWidgets.QToolButton()
        set_line_icon(self.refresh_button, 'reload')
        self.refresh_button.setToolTip('Повторно проанализировать проект')
        self.refresh_button.setStyleSheet(secondary_button_qss("4px 6px", 6))
        self.refresh_button.clicked.connect(self.refresh)

        self.open_graph_button = QtWidgets.QToolButton()
        self.open_graph_button.setText('Граф')
        self.open_graph_button.setToolTip('Открыть интерактивную карту/граф связей проекта')
        self.open_graph_button.setStyleSheet(secondary_button_qss("4px 7px", 6))
        self.open_graph_button.clicked.connect(self._open_graph_editor)

        self.defense_guide_button = QtWidgets.QToolButton()
        self.defense_guide_button.setText('Паспорт')
        self.defense_guide_button.setToolTip('Сформировать архитектурный паспорт и сводный отчёт проекта')
        self.defense_guide_button.setStyleSheet(primary_button_qss("4px 7px", 6))
        self.defense_guide_button.clicked.connect(self._open_defense_guide)
        self.analyzers_button = QtWidgets.QToolButton()
        self.analyzers_button.setText('Анализаторы')
        self.analyzers_button.setStyleSheet(secondary_button_qss("4px 7px", 6))
        self.analyzers_button.clicked.connect(self.open_analyzers_requested)
        for button in (self.analyzers_button, self.open_graph_button,
                       self.defense_guide_button, self.refresh_button):
            hl.addWidget(button)
        root.addWidget(header)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        root.addWidget(self.progress)

        self.analysis_scroll = QtWidgets.QScrollArea()
        self.analysis_scroll.setObjectName('projectAnalysisScroll')
        self.analysis_scroll.setWidgetResizable(True)
        self.analysis_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.analysis_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._analysis_body = QtWidgets.QWidget()
        self._analysis_body.setObjectName('projectAnalysisBody')
        body = QtWidgets.QVBoxLayout(self._analysis_body)
        body.setContentsMargins(0, 0, 3, 0)
        body.setSpacing(12)
        self.analysis_scroll.setWidget(self._analysis_body)
        root.addWidget(self.analysis_scroll, 1)

        self.overview_tab = QtWidgets.QWidget()
        self._build_overview_tab()
        self.tips_tab = QtWidgets.QWidget()
        self._build_tips_tab()
        self.structure_tab = QtWidgets.QWidget()
        self._build_structure_tab()
        self.impact_page = QtWidgets.QWidget()
        self._build_impact_tab()

        self._adversary_path: Optional[str] = None
        self._adversary_explicit_path: Optional[str] = None
        self._adversary_report = None
        self._adversary_thread: Optional[object] = None
        self._adversary_worker = None
        self.adversary_page = QtWidgets.QWidget()
        self._build_adversary_tab()

        self._analysis_sections = {'overview': self.overview_tab}
        body.addWidget(self.overview_tab)
        self.section_grid = QtWidgets.QGridLayout()
        self.section_grid.setSpacing(12)
        body.addLayout(self.section_grid)
        for key, title, page in (
            ('tips', 'Рекомендации', self.tips_tab),
            ('structure', 'Файлы и символы', self.structure_tab),
            ('impact', 'Влияние изменений', self.impact_page),
            ('tests', 'Тесты', self.adversary_page),
        ):
            page.setObjectName('analysisSectionContent')
            section = QtWidgets.QFrame()
            section.setObjectName('analysisSection')
            section_layout = QtWidgets.QVBoxLayout(section)
            section_layout.setContentsMargins(10, 10, 10, 10)
            section_layout.setSpacing(8)
            heading = QtWidgets.QLabel(title)
            heading.setObjectName('analysisSectionTitle')
            section_layout.addWidget(heading)
            section_layout.addWidget(page)
            self._analysis_sections[key] = section
        body.addStretch()
        self._arrange_sections()

        self.tasks_page = None
        self._task_store = None

        self.loading_overlay = LoadingOverlay(self)
        self.apply_interface_font()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'section_grid'):
            self._arrange_sections()

    def _arrange_sections(self):
        wide = self.width() >= 850
        if wide == self._wide_layout:
            return
        self._wide_layout = wide
        columns = 2 if wide else 1
        sections = list(self._analysis_sections.values())[1:]
        for section in sections:
            self.section_grid.removeWidget(section)
        self.section_grid.setColumnStretch(0, 1)
        self.section_grid.setColumnStretch(1, int(wide))
        for index, section in enumerate(sections):
            self.section_grid.addWidget(section, index // columns, index % columns)
        direction = QtWidgets.QBoxLayout.Direction.LeftToRight if wide else QtWidgets.QBoxLayout.Direction.TopToBottom
        self.overview_content.setDirection(direction)
        self.tips_inputs.setDirection(direction)
        metric_columns = 4 if wide else 2
        for column in range(4):
            self.metrics_grid.setColumnStretch(column, int(column < metric_columns))
        for index, metric in enumerate(self.metric_cards):
            self.metrics_grid.removeWidget(metric)
            if index == 4:
                self.metrics_grid.addWidget(metric, index // metric_columns, 0, 1, metric_columns)
            else:
                self.metrics_grid.addWidget(metric, index // metric_columns, index % metric_columns)

    def _scroll_to_section(self, key):
        section = self._analysis_sections.get(key)
        if section is not None:
            position = section.mapTo(self._analysis_body, QtCore.QPoint()).y()
            self.analysis_scroll.verticalScrollBar().setValue(position)

    def _build_overview_tab(self):
        layout = QtWidgets.QVBoxLayout(self.overview_tab)
        layout.setContentsMargins(0, 0, 0, 0)

        self.grade_card = QtWidgets.QFrame()
        self.grade_card.setObjectName('analysisSection')
        self.overview_content = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom, self.grade_card)
        self.overview_content.setContentsMargins(14, 12, 14, 12)
        self.overview_content.setSpacing(14)
        quality = QtWidgets.QWidget()
        quality.setObjectName('analysisSectionContent')
        gcl = QtWidgets.QHBoxLayout(quality)
        gcl.setContentsMargins(0, 0, 0, 0)
        gcl.setSpacing(10)
        self.overview_content.addWidget(quality, 2)

        self.grade_badge = QualityBadgeWidget()
        gcl.addWidget(self.grade_badge)

        quality_info = QtWidgets.QWidget()
        quality_info.setObjectName('analysisSectionContent')
        ginfo_layout = QtWidgets.QVBoxLayout(quality_info)
        ginfo_layout.setContentsMargins(0, 0, 0, 0)
        ginfo_layout.setSpacing(3)
        self.grade_title = QtWidgets.QLabel('Качество кода: —')
        self.grade_title.setWordWrap(True)
        self.grade_title.setStyleSheet(
            ' font-size: 13px; font-weight: 600;'
            f' color: {Palette.TEXT_STRONG};')
        self.grade_score = QtWidgets.QLabel('Оценка: —')
        self.grade_score.setStyleSheet(
            ' font-size: 12px; font-weight: 600;'
            f' color: {Palette.ACCENT_TEXT};')
        self.grade_desc = QtWidgets.QLabel('Откройте проект')
        self.grade_desc.setWordWrap(True)
        self.grade_desc.setStyleSheet(
            ' font-size: 11px;'
            f' color: {Palette.TEXT_MUTED};')
        ginfo_layout.addWidget(self.grade_title)
        ginfo_layout.addWidget(self.grade_score)
        ginfo_layout.addWidget(self.grade_desc)

        gcl.addWidget(quality_info, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.grade_card)

        metrics = QtWidgets.QWidget()
        metrics.setObjectName('analysisSectionContent')
        self.metrics_grid = QtWidgets.QGridLayout(metrics)
        self.metrics_grid.setContentsMargins(0, 0, 0, 0)
        self.metrics_grid.setSpacing(5)
        self.overview_content.addWidget(metrics, 3)

        self.metric_widgets = {}
        self.metric_cards = []
        cards = [
            ('files', 'Файлов', 'Файлы исходного кода'),
            ('lines', 'Строк кода', 'Всего строк'),
            ('functions', 'Функций', 'Функции и методы'),
            ('classes', 'Классов', 'Классы и структуры'),
            ('complexity', 'Сложность', 'Средняя сложность функций'),
        ]
        for key, label, desc in cards:
            box = QtWidgets.QFrame()
            box.setObjectName('analysisMetric')
            box.setToolTip(desc)
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(8, 6, 8, 6)
            bl.setSpacing(2)
            val = QtWidgets.QLabel('—')
            val.setStyleSheet(
                ' font-size: 15px; font-weight: 600;'
                f' color: {Palette.TEXT_STRONG};')
            lbl = QtWidgets.QLabel(label)
            lbl.setStyleSheet(' color: #8B949E; font-size: 11px; font-weight: 500;')
            bl.addWidget(val)
            bl.addWidget(lbl)
            self.metric_cards.append(box)
            self.metric_widgets[key] = val

    def _build_tips_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tips_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.tips_inputs = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.TopToBottom)
        layout.addLayout(self.tips_inputs)
        self.tips_filter = QtWidgets.QComboBox()
        self.tips_filter.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.tips_filter.setMinimumContentsLength(12)
        self.tips_filter.addItems(['Все рекомендации', 'Высокий приоритет', 'Слишком длинные функции', 'Глубокая вложенность'])
        self.tips_filter.currentIndexChanged.connect(self._filter_tips)
        self.tips_inputs.addWidget(self.tips_filter)

        self.tips_search = QtWidgets.QLineEdit()
        self.tips_search.setPlaceholderText('Поиск по файлу или описанию…')
        self.tips_search.setClearButtonEnabled(True)
        self.tips_search.textChanged.connect(self._filter_tips)
        self.tips_inputs.addWidget(self.tips_search, 1)

        actions = QtWidgets.QHBoxLayout()
        self.copy_tips_btn = QtWidgets.QPushButton('Скопировать')
        self.copy_tips_btn.setToolTip('Скопировать все текущие рекомендации в буфер обмена')
        self.copy_tips_btn.setStyleSheet(secondary_button_qss("4px 10px", 6))
        self.copy_tips_btn.clicked.connect(self._copy_tips)
        actions.addWidget(self.copy_tips_btn)

        self.save_tips_btn = QtWidgets.QPushButton('Экспорт…')
        self.save_tips_btn.setToolTip('Сохранить рекомендации и дефекты в файл (.md, .txt, .json)')
        self.save_tips_btn.setStyleSheet(secondary_button_qss("4px 10px", 6))
        self.save_tips_btn.clicked.connect(self._save_tips_to_file)
        actions.addWidget(self.save_tips_btn)

        self.tips_to_tasks_btn = QtWidgets.QPushButton('Все в задачи')
        self.tips_to_tasks_btn.setToolTip(
            'Превратить показанные рекомендации анализатора в задачи проекта; '
            'уже созданные задачи пропускаются')
        self.tips_to_tasks_btn.setStyleSheet(secondary_button_qss("4px 10px", 6))
        self.tips_to_tasks_btn.setEnabled(False)
        self.tips_to_tasks_btn.clicked.connect(self._tasks_from_recommendations)
        actions.addWidget(self.tips_to_tasks_btn)

        actions.addStretch()
        layout.addLayout(actions)

        self.tips_tree = QtWidgets.QTreeWidget()
        self.tips_tree.setFixedHeight(100)
        self.tips_tree.setHeaderLabels(['Замечание', 'Приоритет', 'Файл', 'Строка', 'Рекомендация'])
        self.tips_tree.setAlternatingRowColors(True)
        self.tips_tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tips_tree.customContextMenuRequested.connect(self._on_tips_context_menu)
        self.tips_tree.itemDoubleClicked.connect(self._open_tip_item)
        self.tips_tree.itemExpanded.connect(self._expand_tip_group)
        self.tips_tree.itemCollapsed.connect(self._resize_tips_tree)
        self.tips_tree.itemClicked.connect(self._on_tip_clicked)
        layout.addWidget(self.tips_tree)
        layout.addStretch()

    def _collect_tips_data(self, only_selected: bool = False) -> list:
        tips = getattr(self, "_filtered_tips", [])
        if only_selected:
            item = self.tips_tree.currentItem()
            if item is None:
                return []
            index = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
            if isinstance(index, int):
                tips = [tips[index]]
            elif item.parent() is None:
                tips = [tip for tip in tips if tip.get("priority", "low") == item.data(0, QtCore.Qt.ItemDataRole.UserRole)]
            else:
                return []
        return [{"title": t.get("title", ""), "priority": t.get("priority", "low"),
                 "file": self._relative(t.get("file", "")), "full_path": t.get("file", ""),
                 "line": t.get("line", 1), "action": t.get("action", ""),
                 "explanation": t.get("explanation", "")} for t in tips]

    def _copy_tips(self):
        tips = self._collect_tips_data(only_selected=False)
        if not tips:
            QtWidgets.QMessageBox.information(self, "Копирование", "Список рекомендаций пуст.")
            return

        lines = [
            f"# Замечания и рекомендации по коду проекта ({len(tips)})",
            f"Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]
        for idx, t in enumerate(tips, 1):
            lines.append(f"### {idx}. [{t['priority']}] {t['title']}")
            lines.append(f"- **Файл:** `{t['file']}` (строка {t['line']})")
            if t['action']:
                lines.append(f"- **Рекомендация:** {t['action']}")
            if t['explanation'] and t['explanation'] != t['action']:
                lines.append(f"- **Пояснение:** {t['explanation']}")
            lines.append("")

        text = "\n".join(lines)
        QtWidgets.QApplication.clipboard().setText(text)

        orig_text = self.copy_tips_btn.text()
        self.copy_tips_btn.setText(f"✓ Скопировано ({len(tips)})")
        QtCore.QTimer.singleShot(2000, lambda: self.copy_tips_btn.setText(orig_text))

    def _save_tips_to_file(self):
        tips = self._collect_tips_data(only_selected=False)
        if not tips:
            QtWidgets.QMessageBox.information(self, "Сохранение", "Список рекомендаций пуст.")
            return

        suggested = "project_code_issues.md"
        file_path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Сохранить замечания и дефекты в файл",
            suggested,
            "Markdown (*.md);;Текстовый файл (*.txt);;JSON файл (*.json);;Все файлы (*.*)"
        )
        if not file_path:
            return

        p = Path(file_path)
        ext = p.suffix.lower()

        if ext == ".json" or "JSON" in selected_filter:
            content = json.dumps({
                "generated_at": datetime.now().isoformat(),
                "total_issues": len(tips),
                "issues": tips
            }, indent=2, ensure_ascii=False)
        elif ext == ".txt" or "Текстовый" in selected_filter:
            lines = [
                f"=== ЗАМЕЧАНИЯ И ДЕФЕКТЫ ПРОЕКТА ({len(tips)}) ===",
                f"Создано: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 50,
                ""
            ]
            for idx, t in enumerate(tips, 1):
                lines.append(f"[{idx}] [{t['priority']}] {t['title']}")
                lines.append(f"    Файл: {t['file']}:{t['line']}")
                lines.append(f"    Рекомендация: {t['action']}")
                if t['explanation'] and t['explanation'] != t['action']:
                    lines.append(f"    Детали: {t['explanation']}")
                lines.append("-" * 40)
            content = "\n".join(lines)
        else:
            lines = [
                f"# Замечания и рекомендации по коду проекта ({len(tips)})",
                f"Экспортировано: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "",
                "| № | Приоритет | Замечание | Файл | Строка | Рекомендация |",
                "|---|-----------|-----------|------|--------|--------------|"
            ]
            for idx, t in enumerate(tips, 1):
                lines.append(f"| {idx} | {t['priority']} | {t['title']} | `{t['file']}` | {t['line']} | {t['action']} |")
            lines.append("")
            lines.append("## Подробный список")
            lines.append("")
            for idx, t in enumerate(tips, 1):
                lines.append(f"### {idx}. {t['title']}")
                lines.append(f"- **Приоритет:** {t['priority']}")
                lines.append(f"- **Файл:** `{t['full_path']}`:{t['line']}")
                lines.append(f"- **Рекомендация:** {t['action']}")
                if t['explanation'] and t['explanation'] != t['action']:
                    lines.append(f"- **Описание:** {t['explanation']}")
                lines.append("")
            content = "\n".join(lines)

        try:
            p.write_text(content, encoding='utf-8')
            QtWidgets.QMessageBox.information(
                self,
                "Файл сохранён",
                f"Список замечаний ({len(tips)}) успешно сохранён в файл:\n{file_path}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Ошибка сохранения",
                f"Не удалось записать файл:\n{e}"
            )

    def _on_tips_context_menu(self, pos):
        item = self.tips_tree.itemAt(pos)
        menu = QtWidgets.QMenu(self)

        if item and isinstance(item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1), int):
            act_copy_one = menu.addAction(line_icon("copy"), "Скопировать выбранное замечание")
            act_copy_one.triggered.connect(lambda: self._copy_single_tip(item))
            act_task_one = menu.addAction(line_icon("add"), "Выбранное замечание → задача")
            act_task_one.triggered.connect(self._task_from_recommendation)
            act_open = menu.addAction(line_icon("search"), "Перейти к строке в файле")
            act_open.triggered.connect(lambda: self._open_tip_item(item, 0))
            menu.addSeparator()

        total = len(getattr(self, "_filtered_tips", []))
        act_copy_all = menu.addAction(line_icon("copy"), f"Скопировать все замечания ({total})")
        act_copy_all.triggered.connect(self._copy_tips)

        act_save = menu.addAction(line_icon("save"), "Сохранить список в файл…")
        act_save.triggered.connect(self._save_tips_to_file)

        act_tasks_all = menu.addAction(
            line_icon("tasklist"), f"Все рекомендации → задачи ({total})")
        act_tasks_all.setEnabled(bool(total))
        act_tasks_all.triggered.connect(self._tasks_from_recommendations)

        menu.exec(self.tips_tree.viewport().mapToGlobal(pos))

    def _copy_single_tip(self, item):
        title = item.text(0)
        prio = item.text(1)
        f = item.text(2)
        line = item.text(3)
        action = item.text(4)
        expl = item.toolTip(0) or action
        text = f"[{prio}] {title}\nФайл: {f}:{line}\nРекомендация: {action}\nПояснение: {expl}"
        QtWidgets.QApplication.clipboard().setText(text)

    def _build_structure_tab(self):
        layout = QtWidgets.QVBoxLayout(self.structure_tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.structure_summary = QtWidgets.QLabel('Откройте проект')
        self.structure_summary.setWordWrap(True)
        self.structure_summary.setStyleSheet('color: #D4D4D4; font-size: 12px; line-height: 1.4;')
        layout.addWidget(self.structure_summary)

        self.structure_search = QtWidgets.QLineEdit()
        self.structure_search.setPlaceholderText('Поиск по классам, функциям и файлам…')
        self.structure_search.setClearButtonEnabled(True)
        self.structure_search.textChanged.connect(self._filter_structure)
        layout.addWidget(self.structure_search)

        self.structure_tree = QtWidgets.QTreeWidget()
        self.structure_tree.setMinimumHeight(170)
        self.structure_tree.setHeaderLabels(['Элемент', 'Тип', 'Файл', 'Строка'])
        self.structure_tree.itemDoubleClicked.connect(self._open_symbol_item)
        self.structure_tree.itemExpanded.connect(self._on_structure_item_expanded)
        layout.addWidget(self.structure_tree, 1)

        actions = QtWidgets.QHBoxLayout()
        self.structure_impact_button = QtWidgets.QPushButton('Влияние')
        self.structure_impact_button.clicked.connect(lambda: self.set_impact_file(self._selected_analysis_file()))
        self.structure_test_button = QtWidgets.QPushButton('Тесты файла')
        self.structure_test_button.clicked.connect(lambda: self.set_adversary_file(self._selected_analysis_file()))
        for button in (self.structure_impact_button, self.structure_test_button):
            button.setStyleSheet(secondary_button_qss("4px 8px", 6))
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.structure_tree.currentItemChanged.connect(self._sync_structure_actions)
        self._sync_structure_actions()

    def _selected_analysis_file(self):
        item = self.structure_tree.currentItem()
        return item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync_structure_actions(self):
        path = self._selected_analysis_file()
        self.structure_impact_button.setEnabled(bool(path))
        self.structure_test_button.setEnabled(bool(path and Path(path).suffix.lower() == '.py'))

    def apply_interface_font(self):
        """Keep analysis UI independent of the editor's monospace font."""
        font = _ui_font(9)
        self.setFont(font)
        for child in self.findChildren(QtWidgets.QWidget):
            child.setFont(font)

    def _todo_panel(self):
        """Locate the project task board regardless of how the window is wired."""
        panel = getattr(self, "todo_panel", None)
        if panel is None:
            window = getattr(self, "main_window", None) or self.window()
            panel = getattr(window, "todo_panel", None)
        return panel

    @staticmethod
    def _task_from_tip(tip):
        """Build a project task from one analyzer recommendation dict."""
        from core.project_tasks import Task

        title = " ".join(str(tip.get("title") or "Замечание анализатора").split())
        explanation = str(tip.get("explanation") or "").strip()
        action = str(tip.get("action") or "").strip()
        details = explanation
        if action:
            details = f"{details}\n\nЧто сделать: {action}".strip()
        priority = str(tip.get("priority") or "medium").lower()
        if priority not in ("urgent", "high", "medium", "low"):
            priority = "medium"
        try:
            line = max(0, int(tip.get("line") or 0))
        except (TypeError, ValueError):
            line = 0
        return Task(
            title=title,
            details=details,
            priority=priority,
            file=str(tip.get("file") or ""),
            line=line,
            tag="ANALYSIS",
            source="analysis",
        )

    def _create_tasks_from_tips(self, tips):
        from widgets.Dialog import CustomDialog

        panel = self._todo_panel()
        store = getattr(panel, "task_store", None) if panel is not None else None
        if store is None:
            CustomDialog("Диспетчер задач недоступен. Откройте проект и повторите.").exec()
            return
        if store.root is None:
            CustomDialog("Сначала откройте проект: задачи сохраняются в .codelane/tasks.json.").exec()
            return

        created, skipped = store.add_many(self._task_from_tip(tip) for tip in tips)
        panel.refresh_tasks()
        if created:
            window = getattr(self, "main_window", None) or self.window()
            if hasattr(window, "activity_bar"):
                window.activity_bar.set_activity("todo")

        message = f"Создано задач: {len(created)}."
        if skipped:
            message += f"\nПропущено уже существующих: {skipped}."
        if not created:
            message = "Все выбранные рекомендации уже есть в задачах проекта." if skipped else message
        CustomDialog(message).exec()

    def _task_from_recommendation(self):
        item = getattr(self, "tips_tree", None) and self.tips_tree.currentItem()
        index = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) if item is not None else None
        tips = getattr(self, "_filtered_tips", [])
        if not isinstance(index, int) or not (0 <= index < len(tips)):
            from widgets.Dialog import CustomDialog
            CustomDialog("Выберите замечание в разделе «Рекомендации».").exec()
            return
        self._create_tasks_from_tips([tips[index]])

    def _tasks_from_recommendations(self):
        tips = list(getattr(self, "_filtered_tips", []))
        if not tips:
            from widgets.Dialog import CustomDialog
            CustomDialog("Список рекомендаций пуст.").exec()
            return
        self._create_tasks_from_tips(tips)

    def _open_task_item(self, item, _column):
        task = self._selected_task()
        if task is None or not task.file:
            return
        self.open_file_requested.emit(str(task.file), int(task.line or 1))

    def _build_adversary_tab(self):
        """«Адвокат дьявола»: run boundary inputs and prove the crashes."""
        layout = QtWidgets.QVBoxLayout(self.adversary_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        self.adversary_file_label = QtWidgets.QLabel('Файл не выбран')
        self.adversary_file_label.setStyleSheet(
            f'color: {Palette.TEXT}; font-size: 12px;')
        self.adversary_file_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        layout.addWidget(self.adversary_file_label)
        self.adversary_btn = QtWidgets.QPushButton('Испытать')
        self.adversary_btn.setStyleSheet(primary_button_qss("5px 12px", 6))
        self.adversary_btn.clicked.connect(self._run_adversary)
        self.adversary_editor_btn = QtWidgets.QToolButton()
        self.adversary_editor_btn.setText('Текущий файл')
        self.adversary_editor_btn.setToolTip('Использовать файл из активного редактора')
        self.adversary_editor_btn.setStyleSheet(secondary_button_qss("3px 5px", 6))
        self.adversary_editor_btn.clicked.connect(self._use_editor_for_adversary)
        row.addWidget(self.adversary_btn)
        row.addWidget(self.adversary_editor_btn)
        row.addStretch()
        layout.addLayout(row)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(4)
        self.adversary_test_btn = QtWidgets.QPushButton('Тест')
        self.adversary_test_btn.setStyleSheet(secondary_button_qss("3px 5px", 6))
        self.adversary_test_btn.setToolTip(
            '«Создать тест»: сохранить найденные падения как pytest-тесты')
        self.adversary_test_btn.clicked.connect(self._save_adversary_tests)
        actions.addWidget(self.adversary_test_btn)
        self.offline_tests_btn = QtWidgets.QPushButton('Авто тест')
        self.offline_tests_btn.setToolTip('Python: проверки синтаксиса и примеров doctest, без ИИ и сети')
        self.offline_tests_btn.clicked.connect(self._generate_offline_tests)
        actions.addWidget(self.offline_tests_btn)


        self.adversary_export_btn = QtWidgets.QPushButton('Отчёт')
        self.adversary_export_btn.setStyleSheet(secondary_button_qss("3px 5px", 6))
        self.adversary_export_btn.setToolTip('Экспортировать отчёт в Markdown')
        self.adversary_export_btn.clicked.connect(self._export_adversary)
        actions.addWidget(self.adversary_export_btn)

        self.adversary_ai_btn = QtWidgets.QPushButton('ИИ')
        self.adversary_ai_btn.setStyleSheet(secondary_button_qss("3px 5px", 6))
        self.adversary_ai_btn.setToolTip(
            '«Спросить ИИ»: отправить находку ИИ-агенту на разбор')
        self.adversary_ai_btn.clicked.connect(self._ask_ai_about_adversary)
        actions.addWidget(self.adversary_ai_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.adversary_summary = QtWidgets.QLabel(
            'Откройте файл и нажмите «Испытать».')
        self.adversary_summary.setStyleSheet(
            f'color: {Palette.TEXT_MUTED}; font-size: 12px;')
        self.adversary_summary.setWordWrap(True)
        layout.addWidget(self.adversary_summary)

        self.adversary_progress = QtWidgets.QProgressBar()
        self.adversary_progress.setRange(0, 0)
        self.adversary_progress.setFixedHeight(4)
        self.adversary_progress.setTextVisible(False)
        self.adversary_progress.hide()
        layout.addWidget(self.adversary_progress)

        self.adversary_tree = QtWidgets.QTreeWidget()
        self.adversary_tree.setMinimumHeight(150)
        self.adversary_tree.setHeaderLabels(['Функция (вход)', 'Результат'])
        self.adversary_tree.setColumnWidth(0, 120)
        self.adversary_tree.header().setStretchLastSection(True)
        self.adversary_tree.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.adversary_tree.setWordWrap(True)
        self.adversary_tree.itemDoubleClicked.connect(self._open_adversary_item)
        layout.addWidget(self.adversary_tree, 1)

        self._set_adversary_buttons(False)

    def _set_adversary_buttons(self, enabled: bool):
        has_report = self._adversary_report is not None and bool(
            getattr(self._adversary_report, "failures", []))
        self.adversary_test_btn.setEnabled(bool(enabled and has_report))
        self.adversary_export_btn.setEnabled(enabled and self._adversary_report is not None)
        self.adversary_ai_btn.setEnabled(bool(enabled and has_report))

    def set_adversary_file(self, path: Optional[str]):
        """Choose a file directly from project analysis or the editor."""
        if self._adversary_thread is not None:
            return
        self._adversary_explicit_path = str(path) if path else None
        self._set_adversary_path(self._adversary_explicit_path)
        if path:
            self._scroll_to_section('tests')

    def _use_editor_for_adversary(self):
        if self._adversary_thread is None:
            self._adversary_explicit_path = None
            self._sync_adversary_file()

    def _current_editor_path(self) -> Optional[str]:
        """Path of the file open in the editor, if any."""
        win = self.window()
        getter = getattr(win, "get_current_editor", None)
        if not callable(getter):
            return None
        try:
            editor = getter()
        except Exception:
            return None
        path = getattr(editor, "file_path", None) if editor else None
        return str(path) if path else self.editor_path_hint

    def _sync_adversary_file(self) -> Optional[str]:
        """Use the chosen file, or follow the editor when no file is pinned."""
        if self._adversary_thread is None:
            self._set_adversary_path(self._adversary_explicit_path or self._current_editor_path())
        return self._adversary_path

    def _set_adversary_path(self, path):
        if path != self._adversary_path:
            self._adversary_path = path
            # Results belong to the previous file: drop them instead of
            # showing them next to a different file name.
            self._adversary_report = None
            self.adversary_tree.clear()
            self._set_adversary_buttons(False)
            self.adversary_summary.setText(
                'Нажмите «Испытать», чтобы проверить этот файл.' if path else '')
            self.adversary_summary.setStyleSheet(
                f'color: {Palette.TEXT_MUTED}; font-size: 12px;')
        self.adversary_file_label.setText(Path(path).name if path else 'Файл не выбран')
        self.adversary_file_label.setToolTip(path or '')

    def _run_adversary(self):
        if self._adversary_thread is not None:
            return
        if not self._sync_adversary_file():
            CustomDialog("Сначала откройте файл, который нужно испытать.").exec()
            return
        path = Path(self._adversary_path)
        if path.suffix.lower() != ".py":
            CustomDialog(
                f"«Адвокат дьявола» работает с Python-файлами, "
                f"а открыт «{path.name}».").exec()
            return
        self.adversary_tree.clear()
        self._adversary_report = None
        self._set_adversary_buttons(False)
        self.adversary_summary.setText('Испытание выполняется…')
        self.adversary_progress.show()
        self.adversary_btn.setEnabled(False)
        self.adversary_editor_btn.setEnabled(False)

        worker = AdversaryWorker(self._adversary_path)
        worker.finished.connect(self._on_adversary_finished)
        self._adversary_worker = worker
        # The run itself goes to the shared Python pool; the returned handle is
        # kept so the panel knows a test is in flight and can be polled by callers.
        self._adversary_thread = run_async(
            worker.run,
            parent=self,
            on_done=lambda _result: self._on_adversary_thread_finished(),
            on_error=lambda _message: self._on_adversary_thread_finished())

    def _on_adversary_thread_finished(self):
        self._adversary_thread = None
        self._adversary_worker = None
        self.adversary_btn.setEnabled(True)
        self.adversary_editor_btn.setEnabled(True)

    @QtCore.pyqtSlot(object)
    def _on_adversary_finished(self, report):
        self.adversary_progress.hide()
        if self._adversary_path is None:
            return
        self._adversary_report = report
        if report is None:
            self.adversary_summary.setText('Испытание не удалось.')
            return
        if getattr(report, "error", ""):
            self.adversary_summary.setText(report.error)
            self._set_adversary_buttons(True)
            return
        groups = report.groups() if hasattr(report, "groups") else []
        failures = len(groups)
        if not failures:
            self.adversary_summary.setText(
                f'Проблем не найдено: {report.functions_checked} функций, '
                f'{report.cases_run} случаев.')
            self.adversary_summary.setStyleSheet(
                f'color: {Palette.OK}; font-size: 12px;')
            self._set_adversary_buttons(True)
            return
        self.adversary_summary.setText(
            f'Найдено проблем: {failures} · проверено функций: '
            f'{report.functions_checked} · запущено случаев: {report.cases_run}')
        self.adversary_summary.setStyleSheet(
            f'color: {Palette.DANGER}; font-size: 12px;')
        for finding, count in groups:
            suffix = f'  (+{count - 1})' if count > 1 else ''
            item = QtWidgets.QTreeWidgetItem([
                f"{finding.function}({finding.args_literal})",
                finding.summary + suffix,
            ])
            item.setToolTip(0, f"{finding.function}({finding.args_literal})")
            item.setToolTip(1, (finding.traceback or finding.summary)[:1500])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, finding)
            self.adversary_tree.addTopLevelItem(item)
        self._set_adversary_buttons(True)

    def _open_adversary_item(self, item, _column):
        finding = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None
        if finding is None or not self._adversary_path:
            return
        line = finding.line or 1
        self.open_file_requested.emit(str(self._adversary_path), int(line))

    def _generate_offline_tests(self):
        from services.offline_tests import generate_project_tests
        from services.background import run_async
        root = self._resolve_root_path()
        if not root:
            self.adversary_summary.setText('Сначала откройте проект.')
            return
        self.offline_tests_btn.setEnabled(False)
        self.adversary_summary.setText('Создание тестов на ПК…')

        def completed(result):
            path, count = result
            self.offline_tests_btn.setEnabled(True)
            self.adversary_summary.setText(
                f'Создан {path.name}: {count} Python-файлов. Проверки синтаксиса и doctest; '
                'для проверки требований нужны дополнительные тесты. Запуск — через «Запустить тесты».')
            self.open_file_requested.emit(str(path), 1)

        def failed(message):
            self.offline_tests_btn.setEnabled(True)
            self.adversary_summary.setText(str(message))

        self._offline_tests_job = run_async(lambda: generate_project_tests(root), parent=self,
                                            on_done=completed, on_error=failed)

    def _save_adversary_tests(self):
        from core.adversary import failing_cases_pytest
        if not self._adversary_report or not self._adversary_path:
            return
        target = Path(self._adversary_path)
        suggested = target.with_name(f"test_{target.stem}_adversary.py")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Сохранить тесты', str(suggested), 'Python (*.py)')
        if not path:
            return
        try:
            content = failing_cases_pytest(self._adversary_path,
                                           self._adversary_report.failures)
            Path(path).write_text(content, encoding="utf-8")
            self.adversary_summary.setText(f'Тесты сохранены: {Path(path).name}')
        except Exception as exc:  # noqa: BLE001
            CustomDialog(f"Не удалось сохранить тесты: {exc}").exec()

    def _export_adversary(self):
        if not self._adversary_report:
            return
        report = self._adversary_report
        suggested = Path(report.module_path or "report").with_suffix(".md")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Экспорт отчёта', str(suggested), 'Markdown (*.md)')
        if not path:
            return
        try:
            Path(path).write_text(report.to_markdown(), encoding="utf-8")
            self.adversary_summary.setText(f'Отчёт сохранён: {Path(path).name}')
        except Exception as exc:  # noqa: BLE001
            CustomDialog(f"Не удалось сохранить отчёт: {exc}").exec()

    def _ask_ai_about_adversary(self):
        """Send the selected finding to the AI panel for an explanation."""
        if not self._adversary_report:
            return
        item = self.adversary_tree.currentItem()
        finding = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else None
        if finding is None:
            groups = self._adversary_report.groups()
            finding = groups[0][0] if groups else None
        if finding is None:
            return
        prompt = (
            "«Адвокат дьявола» нашёл падение:\n"
            f"Файл: {self._adversary_report.module_path}:{finding.line or 1}\n"
            f"Функция: {finding.function}({finding.args_literal})\n"
            f"Ошибка: {finding.summary}\n\n"
            "Объясни причину и предложи минимальное исправление.")
        win = self.window()
        panel = getattr(win, "ai_panel", None)
        if panel is None:
            return
        if win.extension_enabled("ai"):
            panel.send_quick_action(prompt)

    def _build_impact_tab(self):
        layout = QtWidgets.QVBoxLayout(self.impact_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QtWidgets.QHBoxLayout()
        self.impact_combo = QtWidgets.QComboBox()
        self.impact_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.impact_combo.setMinimumContentsLength(4)
        self.impact_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        row.addWidget(self.impact_combo, 1)
        self.impact_btn = QtWidgets.QPushButton('Проверить')
        self.impact_btn.setStyleSheet(primary_button_qss("5px 12px", 6))
        self.impact_btn.clicked.connect(self._run_impact_analysis)
        row.addWidget(self.impact_btn)
        layout.addLayout(row)

        self.impact_status_label = QtWidgets.QLabel('Выберите файл, чтобы увидеть зависимости.')
        self.impact_status_label.setWordWrap(True)
        self.impact_status_label.setStyleSheet(
            f'color: {Palette.TEXT_MUTED}; font-size: 12px;')
        layout.addWidget(self.impact_status_label)

        self.impact_tree = QtWidgets.QTreeWidget()
        self.impact_tree.setMinimumHeight(150)
        self.impact_tree.setHeaderLabels(['Модуль', 'Файл', 'Сложность'])
        self.impact_tree.itemDoubleClicked.connect(self._open_impact_item)
        layout.addWidget(self.impact_tree, 1)

    def _resolve_root_path(self) -> Optional[str]:
        """Auto-detect active project/workspace directory from surrounding workbench state."""
        if self.engine and self.engine.root and Path(str(self.engine.root)).is_dir():
            return str(Path(self.engine.root).resolve())

        win = self.window()
        if not hasattr(win, 'current_project') and hasattr(self, 'parent') and self.parent():
            p = self.parent()
            if hasattr(p, 'current_project'):
                win = p
            elif hasattr(p, 'window'):
                win = p.window()

        # 1. Project config
        if hasattr(win, 'current_project') and win.current_project:
            cp = win.current_project
            p_root = cp.get('root_path') if isinstance(cp, dict) else getattr(cp, 'root_path', None)
            if p_root and Path(str(p_root)).is_dir():
                return str(Path(str(p_root)).resolve())

        return None

    def choose_and_analyze_folder(self):
        start_dir = self._resolve_root_path() or str(Path.cwd())
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Выберите папку для анализа", start_dir)
        if folder and Path(folder).is_dir():
            self.set_project(str(Path(folder).resolve()))

    def set_project(self, root: Optional[str], analyzers=None):
        if str(root) != str(getattr(self.engine, 'root', None)):
            self.editor_path_hint = None
        self.enabled_analyzers = set({'structure', 'dependencies', 'health'} if analyzers is None else analyzers)
        # Invalidate any analysis still running for the previous project: its
        # result must never be painted for the newly opened one.
        self._analysis_generation += 1
        self._refresh_pending = False
        self._relative_root = None
        self.engine = ProjectIntelligenceEngine(root, self.enabled_analyzers) if root else None
        self._worker_engine = None
        self._rendered_analysis = None
        # A running analysis notices the raised generation on its next progress
        # tick and stops; its stale result is dropped by _is_stale().
        self.analysis = None
        if self._task_store:
            try:
                self._task_store.set_root(root)
                self._refresh_tasks()
            except Exception:
                logger.exception("Не удалось загрузить задачи проекта")
        self._clear_views()
        if not root:
            self.status.setText('Проект не открыт')
            self._refresh_pending = False
            self.loading_overlay.stop()
            self.progress.hide()
        elif not self.enabled_analyzers:
            self.status.setText('Анализ отключён')
            # Nothing will finish the animation if no analysis starts.
            try:
                self.loading_overlay.stop()
                self.progress.hide()
                self.refresh_button.setEnabled(True)
            except RuntimeError:
                pass
        else:
            self.status.setText(f'Анализ: {Path(root).name}…')
            if self._thread is not None:
                self._refresh_pending = True
            else:
                self.refresh()

    def _clear_views(self):
        for label in self.metric_widgets.values():
            label.setText('—')
        self.grade_badge.set_grade('—', Palette.TEXT_MUTED, Palette.SURFACE_ALT, 0)
        if hasattr(self, 'grade_title'):
            self.grade_title.setText('Качество кода: —')
        if hasattr(self, 'grade_score'):
            self.grade_score.setText('Оценка: —')
        if hasattr(self, 'grade_desc'):
            self.grade_desc.setText('Откройте проект')
        self.student_tips = []
        self.tips_tree.clear()
        self.tips_tree.setFixedHeight(100)
        self.structure_summary.setText('Нет данных о структуре проекта.')
        self.structure_tree.clear()
        self.impact_tree.clear()
        self.impact_combo.clear()
        self.impact_status_label.setText('Выберите файл, чтобы увидеть зависимости.')
        self.impact_status_label.setStyleSheet(f'color: {Palette.TEXT_MUTED}; font-size: 12px;')
        self._adversary_explicit_path = None
        self._set_adversary_path(None)
        self.analysis_scroll.verticalScrollBar().setValue(0)
        self.open_graph_button.setEnabled(True)

    def refresh(self):
        window = self.window()
        if hasattr(window, "extension_enabled") and not window.extension_enabled("intelligence"):
            return
        if not self.engine or not self.engine.root:
            discovered = self._resolve_root_path()
            if discovered:
                self.set_project(discovered)
                return
            else:
                self.set_project(None)
                return

        if self._thread is not None or not self.enabled_analyzers:
            return

        root = str(self.engine.root)
        self._analysis_generation += 1
        generation = self._analysis_generation
        if self._worker_engine is None:
            self._worker_engine = ProjectIntelligenceEngine(root, self.enabled_analyzers)

        self.status.setText('Анализируем проект…')
        self.loading_overlay.start('Анализ проекта…', 'Готовим анализатор…', show_progress=True)
        self.progress.setValue(0)
        self.progress.show()
        self.refresh_button.setEnabled(False)

        worker = AnalysisWorker(
            self._worker_engine,
            should_stop=lambda gen=generation: self._is_stale(gen))
        self._worker = worker

        worker.progress.connect(
            lambda current, total, name, gen=generation:
            self._on_progress(current, total, name, gen))
        worker.finished.connect(
            lambda result, gen=generation: self._on_finished(result, gen))
        worker.failed.connect(
            lambda message, gen=generation: self._on_failed(message, gen))
        self._thread = run_async(
            worker.run,
            parent=self,
            on_done=lambda _result, gen=generation: self._analysis_job_finished(gen),
            on_error=lambda _message, gen=generation: self._analysis_job_finished(gen))

    def _is_stale(self, generation) -> bool:
        """True for results that belong to a project the user already left."""
        return generation is not None and generation != self._analysis_generation

    def _on_progress(self, current, total, name, generation=None):
        if self._is_stale(generation):
            return
        # The engine reports every file; repainting the panel (overlay + status
        # text + progress bar) 600+ times made the window stutter. Two updates
        # per second are enough for a progress indicator.
        now = time.monotonic()
        if current < total and (now - self._last_progress_at) < 0.12:
            return
        self._last_progress_at = now
        self.progress.setValue(int(current * 100 / max(1, total)))
        self.loading_overlay.update_progress(current, total, f"Файл: {name}")
        self.status.setText(f'Анализ: {name}…')

    def _analysis_job_finished(self, generation=None):
        if self._is_stale(generation):
            return
        self._thread = None
        self._worker = None
        if getattr(self, "_refresh_pending", False) and self.engine:
            # A newer project was requested while this analysis was running.
            self._refresh_pending = False
            pending = self._analysis_generation
            QtCore.QTimer.singleShot(0, lambda: None if self._is_stale(pending) else self.refresh())

    def shutdown(self):
        """Detach the panel from any background job before it is destroyed.

        The analysis and adversary runs live on the shared Python pool, so they
        cannot be waited for here (that would block the GUI); instead the
        generation counter makes their results stale and their signals are
        dropped once this widget is gone.
        """
        self._refresh_pending = False
        self._analysis_generation += 1
        self._thread = None
        self._worker = None
        self._adversary_thread = None
        self._adversary_worker = None

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def _on_finished(self, analysis, generation=None):
        if self._is_stale(generation):
            return
        self.analysis = analysis
        if self.engine:
            self.engine.analysis = analysis
        self.loading_overlay.stop()
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        status = f'Файлов: {len(analysis.files)} · {analysis.duration_ms} мс'
        if getattr(analysis, 'scan_truncated', False):
            status += ' · часть файлов пропущена (лимит)'
        self.status.setText(status)
        skipped_dirs = int(getattr(analysis, 'skipped_dirs', 0) or 0)
        skipped_files = int(getattr(analysis, 'skipped_files', 0) or 0)
        skipped_names = list(getattr(analysis, 'skipped_dir_names', []) or [])
        hint = (f'Пропущено служебных каталогов: {skipped_dirs}, файлов: '
                f'{skipped_files}.\nНе анализируются node_modules, .git, '
                'виртуальные окружения, каталоги сборки и файлы больше 2 МБ.')
        if skipped_names:
            hint += '\nПропущены: ' + ', '.join(skipped_names[:10])
            if len(skipped_names) > 10:
                hint += f' и ещё {len(skipped_names) - 10}'
        self.status.setToolTip(hint)
        if self.isVisible():
            self._render_analysis()
        self.analysis_ready.emit(analysis)

    def showEvent(self, event):
        super().showEvent(event)
        if self.analysis is not None and self._rendered_analysis is not self.analysis:
            QtCore.QTimer.singleShot(0, self._render_analysis)

    def _render_analysis(self):
        if self.isVisible() and self.analysis is not None and self._rendered_analysis is not self.analysis:
            self._populate(self.analysis)
            self._rendered_analysis = self.analysis

    def _on_failed(self, message, generation=None):
        if self._is_stale(generation):
            return
        self.loading_overlay.stop()
        self.progress.hide()
        self.refresh_button.setEnabled(True)
        self.status.setText('Ошибка анализа')
        self.grade_desc.setText(message or 'Не удалось проанализировать проект.')
        # Actions that wait for the analysis must not wait forever.
        try:
            self.analysis_failed.emit(str(message or ''))
        except RuntimeError:
            pass

    def _populate(self, a: ProjectAnalysis):
        metrics = self.engine.metrics() if self.engine else {}
        
        # 1. Update Project Quality Score (Safe parsing)
        health_score = 85.0
        if hasattr(a, 'health_score') and isinstance(a.health_score, (int, float)):
            health_score = float(a.health_score)
        else:
            raw_val = metrics.get('health_score', getattr(a, 'health_score', 85))
            import re
            m = re.search(r'(\d+(?:\.\d+)?)', str(raw_val))
            if m:
                health_score = float(m.group(1))

        grade_info = CodeIntelligenceService.calculate_health_grade(health_score)
        
        self.grade_badge.set_grade(grade_info['letter'], grade_info['color'], grade_info['badge_bg'], health_score)
        if hasattr(self, 'grade_title'):
            self.grade_title.setText(f"Качество кода: {grade_info['label'].lower()}")
        if hasattr(self, 'grade_score'):
            self.grade_score.setText(f"Оценка {int(round(health_score))} из 100")
        if hasattr(self, 'grade_desc'):
            self.grade_desc.setText(grade_info['summary'])

        # 2. Update Metric cards
        self.metric_widgets['files'].setText(str(len(a.files)))
        self.metric_widgets['lines'].setText(str(metrics.get('lines', sum(f.lines for f in a.files.values()))))
        
        funcs_count = len([s for s in a.symbols if s.kind in {'function', 'method'}])
        classes_count = len([s for s in a.symbols if s.kind in {'class', 'struct', 'interface'}])
        self.metric_widgets['functions'].setText(str(funcs_count))
        self.metric_widgets['classes'].setText(str(classes_count))

        avg_comp = float(metrics.get('average_complexity', 3.0))
        comp_str = "Низкая (легко)" if avg_comp <= 5 else "Средняя (норма)" if avg_comp <= 10 else "Высокая (сложно)"
        self.metric_widgets['complexity'].setText(comp_str)

        if 'todo' in self.metric_widgets:
            health_debt = getattr(a, 'health_debt_items', []) or []
            todo_count = len([d for d in health_debt if d.get('kind') == 'Debt']) or len(getattr(a, 'debt', []))
            self.metric_widgets['todo'].setText(str(todo_count))
            if todo_count > 0:
                self.metric_widgets['todo'].setStyleSheet(
            f'font-size: 16px; font-weight: 600; color: {Palette.WARNING};')

        # 3. Populate Recommendations
        self.student_tips = CodeIntelligenceService.extract_code_recommendations(a)
        self._filter_tips()

        # 4. Populate Structure & Files
        langs = ', '.join(sorted({f.language for f in a.files.values()})) or 'Python'
        self.structure_summary.setText(
            f"В проекте {len(a.files)} файлов ({langs}): {classes_count} классов и {funcs_count} функций.\n"
            f"Двойной щелчок по символу открывает его в редакторе."
        )
        self._populate_structure_tree(a)

        # 5. Populate Impact files dropdown
        self.impact_combo.blockSignals(True)
        self.impact_combo.clear()
        for fpath in sorted(a.files.keys()):
            self.impact_combo.addItem(self._relative(fpath), fpath)
        self.impact_combo.blockSignals(False)

        self.open_graph_button.setEnabled(True)
        self.defense_guide_button.setEnabled(True)

    def _filter_tips(self):
        filter_mode = self.tips_filter.currentText()
        query = self.tips_search.text().strip().lower()

        self.tips_tree.clear()
        sev_colors = {
            'high': Palette.severity('high'),
            'medium': Palette.severity('medium'),
            'low': Palette.severity('low'),
        }

        filtered = []
        for tip in self.student_tips:
            cat = tip.get('category', '')
            prio = tip.get('priority', 'low')
            file_path = tip.get('file', '')
            line = tip.get('line', 1)
            title = tip.get('title', '')
            explanation = tip.get('explanation', '')
            action = tip.get('action', '')

            if filter_mode == 'Высокий приоритет' and prio != 'high':
                continue
            elif filter_mode == 'Технический долг (TODO/FIXME)' and 'TODO' not in cat and 'FIXME' not in cat and 'BUG' not in cat:
                continue
            elif filter_mode == 'Слишком длинные функции' and 'длинная' not in cat.lower():
                continue
            elif filter_mode == 'Глубокая вложенность' and 'вложенность' not in cat.lower():
                continue

            if query:
                full_text = f"{cat} {file_path} {title} {explanation}".lower()
                if query not in full_text:
                    continue

            filtered.append(tip)
        self._filtered_tips = filtered
        if hasattr(self, "tips_to_tasks_btn"):
            self.tips_to_tasks_btn.setEnabled(bool(filtered))
            self.tips_to_tasks_btn.setText(
                f"Все в задачи ({len(filtered)})" if filtered else "Все в задачи")
        self._tip_groups = {}
        for priority, label in (("high", "Высокий"), ("medium", "Средний"), ("low", "Низкий")):
            indices = [i for i, tip in enumerate(filtered) if tip.get("priority", "low") == priority]
            if not indices:
                continue
            group = QtWidgets.QTreeWidgetItem([f"{label} приоритет · {len(indices)}", "", "", "", ""])
            group.setData(0, QtCore.Qt.ItemDataRole.UserRole, priority)
            group.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            group.setForeground(0, QtGui.QBrush(QtGui.QColor(sev_colors.get(priority, Palette.TEXT_MUTED))))
            self._tip_groups[priority] = (group, indices, 0)
            self.tips_tree.addTopLevelItem(group)

        # Resizing every column for hundreds of rows is itself several hundred
        # milliseconds; fixed widths are cheaper and stable while scrolling.
        self.tips_tree.setColumnWidth(0, 156)
        self.tips_tree.setColumnWidth(1, 80)
        self.tips_tree.setColumnWidth(2, 180)
        self.tips_tree.setColumnWidth(3, 60)
        self._resize_tips_tree()

    def _resize_tips_tree(self, _item=None):
        visible_rows = self.tips_tree.topLevelItemCount()
        for index in range(self.tips_tree.topLevelItemCount()):
            group = self.tips_tree.topLevelItem(index)
            if group.isExpanded():
                visible_rows += group.childCount()
        visible_rows = min(max(visible_rows, 2), 12)
        row_height = max(self.tips_tree.sizeHintForRow(0), self.tips_tree.fontMetrics().height() + 8)
        header_height = self.tips_tree.header().sizeHint().height()
        self.tips_tree.setFixedHeight(max(100, header_height + visible_rows * row_height + 14))

    def _on_tip_clicked(self, item, _column):
        parent = item.parent()
        if parent is not None and item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) is None:
            self._expand_tip_group(parent, force=True)

    def _expand_tip_group(self, item, force=False):
        if item.parent() is not None:
            return
        priority = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        state = getattr(self, "_tip_groups", {}).get(priority)
        if state is None:
            return
        group, indices, offset = state
        if offset and not force:
            self._resize_tips_tree()
            return
        if item.childCount() and item.child(item.childCount() - 1).data(0, QtCore.Qt.ItemDataRole.UserRole + 1) is None:
            item.removeChild(item.child(item.childCount() - 1))
        for index in indices[offset:offset + 50]:
            tip = self._filtered_tips[index]
            child = QtWidgets.QTreeWidgetItem([
                tip.get("title", ""), {"high": "Высокая", "medium": "Средняя"}.get(priority, "Низкая"),
                self._relative(tip.get("file", "")), str(tip.get("line", 1)), tip.get("action", "")])
            child.setData(0, QtCore.Qt.ItemDataRole.UserRole, tip.get("file", ""))
            child.setData(1, QtCore.Qt.ItemDataRole.UserRole, tip.get("line", 1))
            child.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, index)
            child.setToolTip(0, tip.get("explanation", ""))
            item.addChild(child)
        offset = min(offset + 50, len(indices))
        self._tip_groups[priority] = (group, indices, offset)
        if offset < len(indices):
            more = QtWidgets.QTreeWidgetItem([f"Показать ещё 50 · осталось {len(indices) - offset}"])
            item.addChild(more)
        self._resize_tips_tree()

    def _populate_structure_tree(self, a: ProjectAnalysis):
        # Creating a tree item for every symbol froze the panel for seconds on a
        # large project. Only file nodes are built now; a file's symbols are
        # added the first time the node is expanded.
        self.structure_tree.setUpdatesEnabled(False)
        try:
            self.structure_tree.clear()
            symbols_by_file = {}
            for s in a.symbols:
                symbols_by_file.setdefault(s.file, []).append(s)
            self._structure_symbols = symbols_by_file
            self._structure_populated = set()

            top_items = []
            for fpath, finfo in sorted(a.files.items(), key=lambda x: self._relative(x[0])):
                rel_path = self._relative(fpath)
                file_item = QtWidgets.QTreeWidgetItem([rel_path, f"{finfo.language}", rel_path, f"{finfo.lines} стр."])
                file_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, fpath)
                file_item.setData(1, QtCore.Qt.ItemDataRole.UserRole, 1)
                count = len(symbols_by_file.get(fpath, []))
                if count:
                    # A disabled item still shows the expander, and gives the
                    # user a placeholder until the children are built.
                    file_item.setChildIndicatorPolicy(
                        QtWidgets.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                top_items.append(file_item)
            self.structure_tree.addTopLevelItems(top_items)
        finally:
            self.structure_tree.setUpdatesEnabled(True)

    def _populate_file_symbols(self, file_item, max_children: int = 200):
        """Build the symbol children of one file node on demand."""
        if file_item is None:
            return
        fpath = file_item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not fpath or fpath in self._structure_populated:
            return
        self._structure_populated.add(fpath)
        rel_path = self._relative(fpath)
        symbols = sorted(self._structure_symbols.get(fpath, []),
                         key=lambda x: (x.line, x.name))
        children = []
        for s in symbols[:max_children]:
            sym_item = QtWidgets.QTreeWidgetItem([s.name, s.kind, rel_path, str(s.line)])
            sym_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, s.file)
            sym_item.setData(1, QtCore.Qt.ItemDataRole.UserRole, s.line)
            children.append(sym_item)
        if symbols:
            file_item.addChildren(children)
        if len(symbols) > max_children:
            file_item.addChild(QtWidgets.QTreeWidgetItem(
                [f"…ещё {len(symbols) - max_children}", "", rel_path, ""]))

    def _on_structure_item_expanded(self, item):
        if item is not None and not item.parent():
            self._populate_file_symbols(item)

    def _filter_structure(self, text):
        query = text.strip().lower()
        for i in range(self.structure_tree.topLevelItemCount()):
            file_item = self.structure_tree.topLevelItem(i)
            match_file = query in file_item.text(0).lower()
            any_child_match = False
            for j in range(file_item.childCount()):
                child = file_item.child(j)
                match_child = query in child.text(0).lower() or query in child.text(1).lower()
                child.setHidden(not (match_child or not query))
                if match_child:
                    any_child_match = True
            file_item.setHidden(not (match_file or any_child_match or not query))
            if any_child_match and query:
                file_item.setExpanded(True)

    def _run_impact_analysis(self):
        if not self.engine:
            return
        path = self.impact_combo.currentData()
        if not path:
            return

        impacted = self.engine.impacted_by(path)
        self.impact_tree.clear()
        file_name = Path(path).name
        if impacted:
            self.impact_status_label.setText(f"При изменении '{file_name}' затронуто {len(impacted)} модуль(ей):")
            self.impact_status_label.setStyleSheet(
                f"color: {Palette.WARNING}; font-weight: 600;")
        else:
            self.impact_status_label.setText(f"Модуль '{file_name}' независим: другие файлы от него не зависят.")
            self.impact_status_label.setStyleSheet(
                f"color: {Palette.OK}; font-weight: 600;")

        for info in impacted:
            item = QtWidgets.QTreeWidgetItem([info.module, self._relative(info.path), f"Сложность: {info.complexity}"])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, info.path)
            item.setData(1, QtCore.Qt.ItemDataRole.UserRole, 1)
            self.impact_tree.addTopLevelItem(item)

    def _open_graph_editor(self):
        self.open_graph_editor_requested.emit(self.engine, 'modules')

    def _open_defense_guide(self):
        self.open_defense_guide_requested.emit()

    def _relative(self, path):
        # ``Path.resolve()`` on every call is a filesystem round-trip; on a
        # OneDrive project it cost ~2 s while filling the tables. The root is
        # resolved once, and paths coming from the scan are already absolute.
        try:
            root = getattr(self, "_relative_root", None)
            if root is None:
                if self.analysis and self.analysis.root:
                    root = str(Path(self.analysis.root).resolve())
                else:
                    root = ""
                self._relative_root = root
            if root:
                candidate = Path(path)
                try:
                    return str(candidate.relative_to(root))
                except ValueError:
                    return str(Path(path).resolve().relative_to(Path(root)))
        except Exception:
            pass
        return Path(path).name

    def _open_tip_item(self, item, _column):
        if item is None or not isinstance(item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1), int):
            return
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        line = item.data(1, QtCore.Qt.ItemDataRole.UserRole)
        if path:
            self.open_file_requested.emit(str(path), int(line or 1))

    def _open_symbol_item(self, item, _column):
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        line = item.data(1, QtCore.Qt.ItemDataRole.UserRole)
        if path:
            self.open_file_requested.emit(str(path), int(line or 1))

    def _open_impact_item(self, item, _column):
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if path:
            self.open_file_requested.emit(str(path), 1)

    def set_impact_file(self, path: Optional[str]):
        if path:
            self._scroll_to_section('impact')
            idx = self.impact_combo.findData(str(path))
            if idx < 0:
                self.impact_combo.setCurrentIndex(-1)
                self.impact_tree.clear()
                self.impact_status_label.setText('Файл ещё не попал в результаты анализа.')
                self.impact_status_label.setStyleSheet(f'color: {Palette.TEXT_MUTED}; font-size: 12px;')
                return
            self.impact_combo.setCurrentIndex(idx)
            self._run_impact_analysis()


ProjectIntelligenceWidget = ProjectIntelligenceDashboard


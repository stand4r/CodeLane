"""Compact node graph for the supervisor and its assignments."""

from PyQt6 import QtCore, QtGui, QtWidgets


class AgentNode(QtWidgets.QGraphicsObject):
    def __init__(self, title, palette, activate):
        super().__init__()
        self.title = title
        self.status = ""
        self.report = ""
        self.palette = palette
        self.activate = activate
        self.width = 200
        self.pulse_phase = True
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)

    def set_width(self, width):
        if self.width != width:
            self.prepareGeometryChange()
            self.width = width
            self.update()

    def boundingRect(self):
        return QtCore.QRectF(0, 0, self.width, 62)

    def paint(self, painter, option, widget=None):
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        surface = self.palette.color(QtGui.QPalette.ColorRole.AlternateBase)
        ink = self.palette.color(QtGui.QPalette.ColorRole.Text)
        border = QtGui.QColor(ink)
        border.setAlpha(54 if self.isUnderMouse() else 24)
        painter.setBrush(surface)
        painter.setPen(border)
        painter.drawRoundedRect(rect, 16, 16)
        active = self.status in {"Работает", "Координирует", "Запуск"}
        color = {"Работает": "#409CE6", "Координирует": "#409CE6", "Запуск": "#409CE6", "Ошибка": "#DE6969",
                 "Готово": "#58B789", "Частичный итог": "#D3A552"}.get(self.status, "#929AA4")
        dot = QtGui.QColor(color)
        if active and not self.pulse_phase:
            dot.setAlpha(55)
        painter.setBrush(dot)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        radius = 4.5 if active else 3.5
        painter.drawEllipse(QtCore.QPointF(16, 43), radius, radius)
        painter.setPen(ink)
        title_font = painter.font()
        title_font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(title_font)
        metrics = painter.fontMetrics()
        painter.drawText(13, 24, metrics.elidedText(self.title, QtCore.Qt.TextElideMode.ElideRight,
                                                   max(0, int(self.width - 26))))
        title_font.setWeight(QtGui.QFont.Weight.Normal)
        painter.setFont(title_font)
        painter.drawText(27, 47, metrics.elidedText(self.status, QtCore.Qt.TextElideMode.ElideRight,
                                                   max(0, int(self.width - 39))))

    def mouseDoubleClickEvent(self, event):
        self.activate(self)
        event.accept()


class AgentGraph(QtWidgets.QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes = {}
        self.edges = []
        self.relationships = {}
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setObjectName("agentGraph")
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush))
        self.empty_label = QtWidgets.QWidget(self.viewport())
        self.empty_label.setObjectName("agentStageGuide")
        guide = QtWidgets.QVBoxLayout(self.empty_label)
        guide.setContentsMargins(8, 12, 8, 12)
        guide.setSpacing(10)
        for role, detail in (("Специалист", "Находит файлы и критерии"),
                             ("Исполнитель", "Вносит изменения"),
                             ("Проверяющий", "Проверяет результат")):
            row = QtWidgets.QHBoxLayout()
            dot = QtWidgets.QFrame()
            dot.setObjectName("agentStageDot")
            dot.setFixedSize(8, 8)
            row.addWidget(dot)
            labels = QtWidgets.QVBoxLayout()
            labels.setSpacing(2)
            heading = QtWidgets.QLabel(role)
            heading.setObjectName("agentStageHeading")
            labels.addWidget(heading)
            hint = QtWidgets.QLabel(detail)
            hint.setObjectName("agentStageHint")
            labels.addWidget(hint)
            row.addLayout(labels, 1)
            guide.addLayout(row)
        guide.addStretch()
        self.empty_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._pulse_timer = QtCore.QTimer(self)
        self._pulse_timer.setInterval(480)
        self._pulse_timer.timeout.connect(self._pulse_active_agents)
        self._pulse_timer.start()

    def _pulse_active_agents(self):
        for node in self.nodes.values():
            if node.status in {"Работает", "Координирует", "Запуск"}:
                node.pulse_phase = not node.pulse_phase
                node.update()

    def add_agent(self, number, title, activate):
        node = AgentNode(title, self.palette(), activate)
        self.nodes[number] = node
        self.empty_label.hide()
        self.scene().addItem(node)
        self._layout_graph()
        enter = QtCore.QPropertyAnimation(node, b"opacity", node)
        enter.setDuration(240)
        enter.setStartValue(0.0)
        enter.setEndValue(1.0)
        enter.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        enter.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        return node

    def clear(self):
        self.nodes.clear()
        self.relationships.clear()
        self.edges.clear()
        self.scene().clear()
        self.empty_label.show()
        self._layout_graph()

    def set_relationship(self, number, parent, dependencies):
        self.relationships[number] = (parent, dependencies)
        self._layout_graph()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())
        self._layout_graph()

    def _layout_graph(self):
        for edge in self.edges:
            self.scene().removeItem(edge)
        self.edges.clear()
        width = max(120, self.viewport().width())
        root = self.nodes.get(0)
        if root is not None:
            root.setPos(12, 8)
            root.set_width(width - 24)
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.Mid), 1.5)
        y = 89
        path = QtGui.QPainterPath()
        def ancestry(number):
            parent = self.relationships.get(number, (0, []))[0]
            return ancestry(parent) + [number] if parent in self.nodes and parent != number else [number]

        for number in sorted(self.nodes, key=ancestry):
            node = self.nodes[number]
            if number == 0:
                continue
            depth = max(1, len(ancestry(number)) - 1)
            x = 24 + min(depth, 4) * 17
            node.setPos(x, y)
            node.set_width(max(60, width - x - 12))
            y += 74
        for number, node in self.nodes.items():
            if number == 0:
                continue
            parent, dependencies = self.relationships.get(number, (0, []))
            origin = self.nodes.get(parent)
            if origin is not None:
                x = node.x() - 12
                path.moveTo(origin.x() + 12, origin.y() + 62)
                path.lineTo(x, origin.y() + 70)
                path.lineTo(x, node.y() + 31)
                path.lineTo(node.x(), node.y() + 31)
            for dependency in dependencies:
                source = self.nodes.get(dependency)
                if source:
                    line = QtGui.QPainterPath(QtCore.QPointF(width - 6, source.y() + 31))
                    line.lineTo(width - 3, source.y() + 31)
                    line.lineTo(width - 3, node.y() + 31)
                    line.lineTo(width - 6, node.y() + 31)
                    dashed = QtGui.QPen(QtGui.QColor("#409CE6"), 1, QtCore.Qt.PenStyle.DashLine)
                    self.edges.append(self.scene().addPath(line, dashed))
        if not path.isEmpty():
            self.edges.append(self.scene().addPath(path, pen))
        for edge in self.edges:
            edge.setZValue(-1)
        self.setSceneRect(0, 0, width, y if self.nodes else 0)

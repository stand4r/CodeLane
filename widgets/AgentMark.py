"""CodeLane's three flowing lanes, painted at native display resolution."""

from PyQt6 import QtCore, QtGui, QtWidgets


class AgentMark(QtWidgets.QWidget):
    def __init__(self, *, small=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(32 if small else 210, 24 if small else 82)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.scale(self.width() / 210, self.height() / 82)
        dark = self.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 138
        gradient = QtGui.QLinearGradient(10, 58, 202, 20)
        gradient.setColorAt(0, QtGui.QColor("#7098eb" if dark else "#4762af"))
        gradient.setColorAt(.55, QtGui.QColor("#a297ed" if dark else "#7756a6"))
        gradient.setColorAt(1, QtGui.QColor("#79d3c5" if dark else "#2b827a"))
        pen = QtGui.QPen(QtGui.QBrush(gradient), 4 if self.width() > 40 else 8)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for lane in range(3):
            y = lane * 9
            path = QtGui.QPainterPath(QtCore.QPointF(9, 55 + y))
            path.cubicTo(48, 55 + y, 52, 4 + y, 84, 11 + y)
            path.cubicTo(110, 17 + y, 107, 57 + y, 135, 47 + y)
            path.cubicTo(159, 39 + y, 170, 6 + y, 200, 8 + y)
            p.drawPath(path)

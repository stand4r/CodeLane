"""Editor group with compact VS Code-like tabs and cross-group drag/drop."""
from uuid import uuid4
from PyQt6 import QtCore, QtGui, QtWidgets


class EditorTabBar(QtWidgets.QTabBar):
    _drag_sources = {}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMovable(False)
        self.setTabsClosable(True)
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.setUsesScrollButtons(True)
        self._press_pos = None
        self._press_index = -1
        self.setAcceptDrops(True)
        self.setIconSize(QtCore.QSize(16, 16))
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

    def mouseMoveEvent(self, event):
        if (self._press_pos is not None and self._press_index >= 0 and
                event.buttons() & QtCore.Qt.MouseButton.LeftButton and
                (event.pos() - self._press_pos).manhattanLength() >= QtWidgets.QApplication.startDragDistance()):
            group = self.parentWidget()
            if isinstance(group, TabWidget):
                token = str(uuid4())
                self._drag_sources[token] = (group, self._press_index)
                mime = QtCore.QMimeData()
                mime.setData('application/x-pyscribe-editor-tab', token.encode('ascii'))
                drag = QtGui.QDrag(self)
                drag.setMimeData(mime)
                drag.exec(QtCore.Qt.DropAction.MoveAction)
                self._press_pos = None
                self._press_index = -1
                return
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self._press_pos = event.pos()
        self._press_index = self.tabAt(event.pos())
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-pyscribe-editor-tab'):
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        raw = bytes(event.mimeData().data('application/x-pyscribe-editor-tab')).decode('ascii', 'ignore')
        source_info = self._drag_sources.pop(raw, None)
        target = self.parentWidget()
        if source_info and isinstance(target, TabWidget):
            source, index = source_info
            source.transfer_tab_to(index, target, self.tabAt(event.position().toPoint()))
            event.setDropAction(QtCore.Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()


class TabWidget(QtWidgets.QTabWidget):
    groupActivated = QtCore.pyqtSignal(object)
    tabMovedOut = QtCore.pyqtSignal()

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.setIconSize(QtCore.QSize(20, 20))
        self.setMovable(False)
        self.setTabsClosable(True)
        self.setAcceptDrops(True)
        self.setTabBar(EditorTabBar(self))
        self.tabBar().setObjectName("editorTabs")
        self.setObjectName("editorGroup")
        self.currentChanged.connect(self._sync_active_surface)

        self.tabBar().installEventFilter(self)

    def _sync_active_surface(self, _index=None):
        page = self.currentWidget()
        kind = page.property("pyscribeSpecial") if page is not None else None
        surface = ("agent" if kind in {"aiAgent", "agentTeam"} else
                   "journal" if kind == "agentJournal" else
                   "analysis" if kind in {"projectAnalysis", "projectAnalyzers"} else "editor")
        bar = self.tabBar()
        if bar.property("activeSurface") != surface:
            bar.setProperty("activeSurface", surface)
            bar.style().unpolish(bar)
            bar.style().polish(bar)
            bar.update()

    # Compatibility attributes for older callers; toolbar actions were intentionally removed.
    button_run = None
    button_settings = None
    toggle_button = None

    def _watch_page(self, widget):
        if widget is None:
            return
        widget.installEventFilter(self)
        for child in widget.findChildren(QtCore.QObject):
            child.installEventFilter(self)

    def addTab(self, *args):
        index = super().addTab(*args)
        if args:
            self._watch_page(args[0])
        return index

    def insertTab(self, *args):
        index = super().insertTab(*args)
        if args:
            self._watch_page(args[1] if len(args) > 1 and isinstance(args[0], int) else args[0])
        return index

    def removeTab(self, index):
        page = self.widget(index)
        if page is not None:
            page.removeEventFilter(self)
            for child in page.findChildren(QtCore.QObject):
                child.removeEventFilter(self)
        super().removeTab(index)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.FocusIn):
            self.groupActivated.emit(self)
        if event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove):
            if event.mimeData().hasFormat('application/x-pyscribe-editor-tab'):
                event.acceptProposedAction()
                return True
        if event.type() == QtCore.QEvent.Type.Drop and event.mimeData().hasFormat('application/x-pyscribe-editor-tab'):
            raw = bytes(event.mimeData().data('application/x-pyscribe-editor-tab')).decode('ascii', 'ignore')
            source_info = EditorTabBar._drag_sources.pop(raw, None)
            if source_info:
                source, index = source_info
                source.transfer_tab_to(index, self, self.count())
                event.acceptProposedAction()
                return True
        return super().eventFilter(watched, event)

    def focusInEvent(self, event):
        self.groupActivated.emit(self)
        super().focusInEvent(event)

    def mousePressEvent(self, event):
        self.groupActivated.emit(self)
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat('application/x-pyscribe-editor-tab'):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        raw = bytes(event.mimeData().data('application/x-pyscribe-editor-tab')).decode('ascii', 'ignore')
        source_info = EditorTabBar._drag_sources.pop(raw, None)
        if source_info:
            source, index = source_info
            source.transfer_tab_to(index, self, self.count())
            event.acceptProposedAction()
        else:
            event.ignore()

    def transfer_tab_to(self, index, target, target_index=-1, *, notify=True):
        if not (0 <= index < self.count()):
            return
        if target is self:
            if 0 <= target_index < self.count() and target_index != index:
                self.tabBar().moveTab(index, target_index)
            return
        widget = self.widget(index)
        title = self.tabText(index)
        icon = self.tabIcon(index)
        tip = self.tabToolTip(index)
        self.removeTab(index)
        if target_index < 0 or target_index > target.count():
            target_index = target.count()
        new_index = target.insertTab(target_index, widget, icon, title)
        target.setTabToolTip(new_index, tip)
        target.setCurrentIndex(new_index)
        target.groupActivated.emit(target)
        if notify:
            self.tabMovedOut.emit()

    def _request_close(self, index):
        if 0 <= index < self.count():
            self.tabCloseRequested.emit(index)

    def set_project_name(self, project_name):
        return

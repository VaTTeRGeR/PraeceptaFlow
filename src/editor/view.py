from PySide6.QtCore import QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGraphicsView


class EditorView(QGraphicsView):
    zoom_changed = Signal(float)
    canvas_context_menu = Signal(QPoint, QPointF)  # global position, scene position

    MIN_ZOOM = 0.1
    MAX_ZOOM = 5.0
    ZOOM_STEP = 1.2

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._pan_from: QPoint | None = None
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.centerOn(0, 0)

    def zoom(self) -> float:
        return self.transform().m11()

    def _scale_by(self, factor: float, anchor: QGraphicsView.ViewportAnchor) -> None:
        current = self.zoom()
        target = min(max(current * factor, self.MIN_ZOOM), self.MAX_ZOOM)
        if abs(target - current) < 1e-9:
            return
        self.setTransformationAnchor(anchor)
        self.scale(target / current, target / current)
        self.zoom_changed.emit(self.zoom())

    def zoom_in(self) -> None:
        self._scale_by(self.ZOOM_STEP, QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def zoom_out(self) -> None:
        self._scale_by(1 / self.ZOOM_STEP, QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def reset_zoom(self) -> None:
        self._scale_by(1 / self.zoom(), QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def fit_all(self) -> None:
        """Show every item, without magnifying beyond 100%."""
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            self.reset_zoom()
            self.centerOn(0, 0)
            return
        self.fitInView(rect.adjusted(-40, -40, 40, 40), Qt.AspectRatioMode.KeepAspectRatio)
        zoom = min(max(self.zoom(), self.MIN_ZOOM), 1.0)
        self.resetTransform()
        self.scale(zoom, zoom)
        self.centerOn(rect.center())
        self.zoom_changed.emit(self.zoom())

    def contextMenuEvent(self, event) -> None:
        if self.itemAt(event.pos()) is None:
            self.canvas_context_menu.emit(event.globalPos(), self.mapToScene(event.pos()))
            event.accept()
            return
        super().contextMenuEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            self._scale_by(self.ZOOM_STEP**steps, QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_from = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._pan_from is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan_from
            self._pan_from = pos
            for bar, distance in ((self.horizontalScrollBar(), delta.x()), (self.verticalScrollBar(), delta.y())):
                bar.setValue(bar.value() - distance)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._pan_from is not None:
            self._pan_from = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

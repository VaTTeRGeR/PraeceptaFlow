import math

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QUndoStack
from PySide6.QtWidgets import QGraphicsItem, QGraphicsLineItem, QGraphicsScene

from editor.commands import AddItemCommand, MoveAnchorCommand, MoveItemsCommand
from editor.items import EDGE_COLOR, EdgeHandle, EdgeItem, NodeItem, connection_error

FORMAT_VERSION = 1

BACKGROUND_COLOR = QColor("#ffffff")
GRID_MINOR_COLOR = QColor("#eef1f4")
GRID_MAJOR_COLOR = QColor("#d9dee4")


class EditorScene(QGraphicsScene):
    text_edited = Signal()

    GRID_SIZE = 20
    GRID_MAJOR_EVERY = 5

    def __init__(self, undo_stack: QUndoStack, parent=None):
        super().__init__(QRectF(-5000, -5000, 10000, 10000), parent)
        self._undo_stack = undo_stack
        self._drag_source: NodeItem | None = None
        self._drag_anchor: QPointF | None = None
        self._drag_line: QGraphicsLineItem | None = None
        self._press_positions: list[tuple[QGraphicsItem, QPointF]] = []

    # --- items -----------------------------------------------------------

    def add_item(self, item: QGraphicsItem) -> None:
        self.addItem(item)
        if isinstance(item, EdgeItem):
            item.source.edges.append(item)
            item.target.edges.append(item)
            for handle in item.handles:
                self.addItem(handle)
            item.update_path()

    def remove_item(self, item: QGraphicsItem) -> None:
        item.setSelected(False)
        if isinstance(item, EdgeItem):
            item.source.edges.remove(item)
            item.target.edges.remove(item)
            for handle in item.handles:
                self.removeItem(handle)
        self.removeItem(item)

    def move_edge_anchor(self, edge: EdgeItem, is_source: bool, old: QPointF, new: QPointF) -> None:
        self._undo_stack.push(MoveAnchorCommand(edge, is_source, old, new))

    def has_edge(self, source: NodeItem, target: NodeItem) -> bool:
        """Whether the two are already connected that way round; either way round for an edge without a head."""
        return any(
            (edge.source is source and edge.target is target)
            or (not edge.has_head and edge.source is target and edge.target is source)
            for edge in source.edges
        )

    def is_editing_text(self) -> bool:
        item = self.focusItem()
        return item is not None and hasattr(item, "is_editing") and item.is_editing()

    def reset(self) -> None:
        self._undo_stack.clear()  # commands hold items that clear() is about to delete
        self._drag_source = None
        self._drag_anchor = None
        self._drag_line = None
        self._press_positions = []
        self.clear()

    # --- serialisation ---------------------------------------------------

    def to_dict(self) -> dict:
        nodes, edges = [], []
        for item in self.items(Qt.SortOrder.AscendingOrder):
            if isinstance(item, NodeItem):
                nodes.append(item.to_dict())
            elif isinstance(item, EdgeItem):
                edges.append(item.to_dict())
        return {"version": FORMAT_VERSION, "nodes": nodes, "edges": edges}

    def load_dict(self, data: dict) -> None:
        """Replace the scene content; raises ValueError and leaves the scene untouched on bad data."""
        if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
            raise ValueError("not a flow graph file, or an unsupported version")
        try:
            nodes = {entry["id"]: NodeItem.from_dict(entry) for entry in data.get("nodes", [])}
            edges = [EdgeItem.from_dict(entry, nodes) for entry in data.get("edges", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed flow graph: {exc!r}") from exc
        self.reset()
        for item in [*nodes.values(), *edges]:
            self.add_item(item)

    # --- interaction -----------------------------------------------------

    def _node_at(self, pos: QPointF) -> NodeItem | None:
        for item in self.items(pos):
            if isinstance(item, NodeItem):
                return item
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self.items(event.scenePos())
            on_handle = bool(hit) and isinstance(hit[0], EdgeHandle)  # it overlaps the node's border
            node = self._node_at(event.scenePos())
            # The border of a kind without outgoing edges moves the node like the rest of it.
            starts_edge = node is not None and node.kind.outgoing and node.border_contains(event.scenePos())
            if not on_handle and starts_edge:
                self.setFocusItem(None)
                self._drag_source = node
                self._drag_anchor = node.anchor_at(event.scenePos())
                self._drag_line = QGraphicsLineItem(QLineF(node.anchor_pos(self._drag_anchor), event.scenePos()))
                self._drag_line.setPen(QPen(EDGE_COLOR, 2, Qt.PenStyle.DashLine))
                self._drag_line.setZValue(10)
                self.addItem(self._drag_line)
                event.accept()
                return
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_positions = [
                (item, item.pos()) for item in self.selectedItems() if isinstance(item, NodeItem)
            ]

    def mouseMoveEvent(self, event) -> None:
        if self._drag_line is not None:
            line = self._drag_line.line()
            target = self._node_at(event.scenePos())
            if target is not None and connection_error(self._drag_source, target) is None:
                # Preview where the edge would attach.
                line.setP2(target.anchor_pos(target.anchor_for_drop(event.scenePos(), line.p1())))
            else:
                line.setP2(event.scenePos())
            self._drag_line.setLine(line)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_line is not None:
            if event.button() == Qt.MouseButton.LeftButton:
                source, source_anchor = self._drag_source, self._drag_anchor
                start = self._drag_line.line().p1()
                self.removeItem(self._drag_line)
                self._drag_line = None
                self._drag_source = None
                self._drag_anchor = None
                target = self._node_at(event.scenePos())
                if target is source:
                    # The border starts edges instead of moves, but a click on it still selects.
                    self.clearSelection()
                    source.setSelected(True)
                elif (
                    target is not None
                    and connection_error(source, target) is None
                    and not self.has_edge(source, target)
                ):
                    target_anchor = target.anchor_for_drop(event.scenePos(), start)
                    edge = EdgeItem(source, target, source_anchor, target_anchor)
                    self._undo_stack.push(AddItemCommand(self, edge, "Add Connection"))
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            moves = [(item, old, item.pos()) for item, old in self._press_positions if item.pos() != old]
            self._press_positions = []
            if moves:
                self._undo_stack.push(MoveItemsCommand(moves))

    # --- painting --------------------------------------------------------

    def drawBackground(self, painter, rect: QRectF) -> None:
        painter.fillRect(rect, BACKGROUND_COLOR)
        major = self.GRID_SIZE * self.GRID_MAJOR_EVERY
        # Minor lines turn into noise when zoomed far out.
        if painter.worldTransform().m11() >= 0.4:
            self._draw_grid(painter, rect, self.GRID_SIZE, GRID_MINOR_COLOR)
        self._draw_grid(painter, rect, major, GRID_MAJOR_COLOR)

    @staticmethod
    def _draw_grid(painter, rect: QRectF, step: int, color: QColor) -> None:
        lines = []
        x = math.floor(rect.left() / step) * step
        while x <= rect.right():
            lines.append(QLineF(x, rect.top(), x, rect.bottom()))
            x += step
        y = math.floor(rect.top() / step) * step
        while y <= rect.bottom():
            lines.append(QLineF(rect.left(), y, rect.right(), y))
            y += step
        painter.setPen(QPen(color, 0))
        painter.drawLines(lines)

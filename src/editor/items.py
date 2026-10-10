import math
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
    QTextCursor,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsTextItem

TEXT_COLOR = QColor("#1f2933")
NODE_FILL = QColor("#f4f8fd")  # edge handles sit outside the boxes on this
EDGE_COLOR = QColor("#52606d")
SELECTION_COLOR = QColor("#1c7ed6")


@dataclass(frozen=True)
class NodeKind:
    """What a box stands for; decides its colour, which connections it takes and how they are drawn."""

    label: str
    color: str  # border and label strip; the body is a light tint of it
    outgoing: bool = True  # edges may start here
    incoming: bool = True  # edges may end here
    arrow_head: bool = True  # False: its connections are plain lines


# Keyed by the name stored in the file's "type" field. Data is cool (input teal, structure violet, output
# green), the algorithm is the one warm step, and notes take yellow, grey and rose.
NODE_KINDS = {
    "project_note": NodeKind("Project Note", "#fab005", outgoing=False, incoming=False),
    "data_input": NodeKind("Data Input", "#0c8599", incoming=False),
    "data_output": NodeKind("Data Output", "#2b8a3e", outgoing=False),
    "data_structure": NodeKind("Data Structure", "#7048e8"),
    "algorithm": NodeKind("Algorithm", "#d9480f"),
    "tooling_note": NodeKind("Tooling Note", "#6c757d", arrow_head=False),
    "implementation_note": NodeKind("Implementation Note", "#a68f96", arrow_head=False),
}
DEFAULT_KIND = "algorithm"  # for files written before boxes had a type

# The verdicts of a parity check (PARITY_SCHEMA in claude_runner.py): the badge's mark and colour.
PARITY_MARKS = {
    "match": ("✓", "#2f9e44"),
    "mismatch": ("✗", "#e03131"),
    "unimplemented": ("○", "#868e96"),
}


def _tint(color: QColor, amount: float) -> QColor:
    """Mix a colour with white: amount 0 is white, 1 is the colour itself."""
    return QColor(*(round(255 + (channel - 255) * amount) for channel in (color.red(), color.green(), color.blue())))


def _label_color(color: QColor) -> QColor:
    """White on dark colours, dark text on light ones such as the project note yellow."""
    luminance = 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()
    return TEXT_COLOR if luminance > 0.6 else QColor("#ffffff")


class EditableText(QGraphicsTextItem):
    """Text that is inert until start_editing() and leaves edit mode on focus loss."""

    def __init__(self, text: str, parent: QGraphicsItem | None = None, single_line: bool = False):
        super().__init__(text, parent)
        self.setDefaultTextColor(TEXT_COLOR)
        self._single_line = single_line
        self._text_before_edit = text

    def is_editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def start_editing(self) -> None:
        if self.is_editing():
            return
        self._text_before_edit = self.toPlainText()
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = self.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        self.setTextCursor(cursor)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return  # the text context menu opened; keep editing
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._edit_finished()

    def _edit_finished(self) -> None:
        scene = self.scene()
        if scene is not None and self.toPlainText() != self._text_before_edit:
            scene.text_edited.emit()

    def keyPressEvent(self, event) -> None:
        finish_keys = [Qt.Key.Key_Escape]
        if self._single_line or event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            finish_keys += [Qt.Key.Key_Return, Qt.Key.Key_Enter]
        if event.key() in finish_keys:
            self.clearFocus()
            return
        super().keyPressEvent(event)


class _TitleText(EditableText):
    """Node text, any number of centred lines; ignores the mouse outside edit mode so the node receives the clicks.

    Enter starts a new line; Ctrl+Enter, Escape or a click elsewhere ends the edit.
    """

    def __init__(self, text: str, node: "NodeItem"):
        super().__init__(text, node)
        option = self.document().defaultTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.document().setDefaultTextOption(option)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def start_editing(self) -> None:
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        super().start_editing()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if not self.is_editing():
            self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)


class NodeItem(QGraphicsObject):
    """Flow-chart box, centred on its position; dragging from its wide border starts an edge."""

    MIN_WIDTH = 140.0
    MIN_HEIGHT = 56.0
    PADDING = 16.0
    BORDER_WIDTH = 10.0
    CORNER_RADIUS = 10.0
    KIND_LABEL_PIXEL_SIZE = 8
    SELECTION_GAP = 3.0  # the selection ring sits this far outside the border
    PARITY_RADIUS = 9.0  # the parity badge is a circle centred on the top right corner

    def __init__(self, title: str = "Box", node_id: str | None = None, kind: str = DEFAULT_KIND):
        super().__init__()
        self.id = node_id or uuid.uuid4().hex
        self.parity: str | None = None  # a key of PARITY_MARKS after a parity check; not saved
        self.kind_key = kind
        self.kind = NODE_KINDS[kind]
        self._border = QColor(self.kind.color)
        self._fill = _tint(self._border, 0.12)
        self._label_color = _label_color(self._border)
        self.edges: list[EdgeItem] = []
        self._rect = QRectF()
        self._laying_out = False
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self._title = _TitleText(title, self)
        # Not documentSizeChanged: with a fixed text width the size does not shrink when the text does.
        self._title.document().contentsChanged.connect(self._layout)
        self._layout()

    @property
    def title(self) -> str:
        return self._title.toPlainText()

    def is_editing(self) -> bool:
        return self._title.is_editing()

    def set_parity(self, status: str | None, reason: str = "") -> None:
        """Show (or with None remove) the badge of a parity check; the reason is the tooltip."""
        self.parity = status if status in PARITY_MARKS else None
        self.setToolTip(reason if self.parity else "")
        self.update()

    def _layout(self, *_) -> None:
        if self._laying_out:
            return  # changing the text width below resizes the document again
        self._laying_out = True
        try:
            # Qt aligns lines only within a fixed text width, so fit it to the widest line. That line is
            # measured unbounded, otherwise the old width would wrap the text before it could grow.
            self._title.setTextWidth(-1)
            self._title.setTextWidth(self._title.document().idealWidth())
            text_size = self._title.document().size()
        finally:
            self._laying_out = False
        label_width = QFontMetricsF(self._kind_label_font()).horizontalAdvance(self.kind.label)
        width = max(
            self.MIN_WIDTH,
            text_size.width() + 2 * (self.PADDING + self.BORDER_WIDTH),
            math.ceil(label_width) + 2 * self.CORNER_RADIUS,
        )
        height = max(self.MIN_HEIGHT, text_size.height() + self.PADDING + 2 * self.BORDER_WIDTH)
        self.prepareGeometryChange()
        self._rect = QRectF(-width / 2, -height / 2, width, height)
        self._title.setPos(-text_size.width() / 2, -text_size.height() / 2)
        for edge in self.edges:
            edge.update_path()

    def _kind_label_font(self) -> QFont:
        font = QFont()
        font.setPixelSize(self.KIND_LABEL_PIXEL_SIZE)
        font.setBold(True)
        return font

    def _kind_label_rect(self) -> QRectF:
        """Strip of the top border between the rounded corners."""
        rect = self._rect
        return QRectF(
            rect.left() + self.CORNER_RADIUS, rect.top(), rect.width() - 2 * self.CORNER_RADIUS, self.BORDER_WIDTH
        )

    def _inner_rect(self) -> QRectF:
        inset = self.BORDER_WIDTH
        return self._rect.adjusted(inset, inset, -inset, -inset)

    def border_contains(self, scene_pos: QPointF) -> bool:
        pos = self.mapFromScene(scene_pos)
        return self._rect.contains(pos) and not self._inner_rect().contains(pos)

    def anchor_at(self, scene_pos: QPointF) -> QPointF:
        """Outline point nearest to scene_pos, as fractions of the half size so it survives a resize."""
        pos = self.mapFromScene(scene_pos)
        half_width = self._rect.width() / 2
        half_height = self._rect.height() / 2
        x = min(max(pos.x(), -half_width), half_width)
        y = min(max(pos.y(), -half_height), half_height)
        if half_width - abs(x) < half_height - abs(y):
            x = math.copysign(half_width, x)
        else:
            y = math.copysign(half_height, y)
        return QPointF(x / half_width, y / half_height)

    def _outline_point(self, anchor: QPointF) -> tuple[QPointF, QPointF]:
        """Local outline point and outward unit normal of an anchor.

        An anchor lies on the bounding rectangle; the part of a side that falls within the corner radius is
        bent onto the rounded corner, half of the arc per side, so the normal turns smoothly from straight
        out to 45 degrees at the corner's middle.
        """
        half_width = self._rect.width() / 2
        half_height = self._rect.height() / 2
        x, y = anchor.x() * half_width, anchor.y() * half_height
        sign_x, sign_y = math.copysign(1.0, x), math.copysign(1.0, y)
        radius = min(self.CORNER_RADIUS, half_width, half_height)
        on_vertical_side = abs(anchor.x()) >= abs(anchor.y())
        along = abs(y) - (half_height - radius) if on_vertical_side else abs(x) - (half_width - radius)
        if along <= 0:
            normal = QPointF(sign_x, 0.0) if on_vertical_side else QPointF(0.0, sign_y)
            return QPointF(x, y), normal
        turn = min(along / radius, 1.0) * math.pi / 4
        if on_vertical_side:
            normal = QPointF(sign_x * math.cos(turn), sign_y * math.sin(turn))
        else:
            normal = QPointF(sign_x * math.sin(turn), sign_y * math.cos(turn))
        centre = QPointF(sign_x * (half_width - radius), sign_y * (half_height - radius))
        return centre + normal * radius, normal

    def anchor_normal(self, anchor: QPointF) -> QPointF:
        """Unit vector pointing out of the box at an anchor."""
        return self._outline_point(anchor)[1]

    def anchor_for_drop(self, scene_pos: QPointF, from_scene_pos: QPointF) -> QPointF:
        """Anchor for an edge end dropped at scene_pos: there if on the border, else where the edge enters."""
        if self.border_contains(scene_pos):
            return self.anchor_at(scene_pos)
        start = self.mapFromScene(from_scene_pos)
        delta = self.mapFromScene(scene_pos) - start
        enter = 0.0
        for origin, step, half in (
            (start.x(), delta.x(), self._rect.width() / 2),
            (start.y(), delta.y(), self._rect.height() / 2),
        ):
            if abs(step) > 1e-9:
                enter = max(enter, min((-half - origin) / step, (half - origin) / step))
        return self.anchor_at(self.mapToScene(start + delta * enter))

    def anchor_pos(self, anchor: QPointF) -> QPointF:
        """Scene position of an anchor returned by anchor_at()."""
        return self.mapToScene(self._outline_point(anchor)[0])

    def border_point_towards(self, scene_pos: QPointF) -> QPointF:
        """Point on the box outline along the line from its centre to scene_pos."""
        centre = self.scenePos()
        delta = scene_pos - centre
        if abs(delta.x()) < 1e-6 and abs(delta.y()) < 1e-6:
            return centre
        scale_x = self._rect.width() / 2 / abs(delta.x()) if delta.x() else math.inf
        scale_y = self._rect.height() / 2 / abs(delta.y()) if delta.y() else math.inf
        return centre + delta * min(scale_x, scale_y)

    def boundingRect(self) -> QRectF:
        # The ring is 2 wide, plus a pixel for antialiasing; the parity badge reaches further out.
        margin = max(self.SELECTION_GAP + 2, self.PARITY_RADIUS + 1)
        return self._rect.adjusted(-margin, -margin, margin, margin)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self._rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
        return path

    def paint(self, painter, option, widget=None) -> None:
        inner_radius = max(self.CORNER_RADIUS - self.BORDER_WIDTH, 2.0)
        if self.isSelected():
            # Outside the box, so the type colour stays visible while the box is selected.
            ring = self._rect.adjusted(-self.SELECTION_GAP, -self.SELECTION_GAP, self.SELECTION_GAP, self.SELECTION_GAP)
            radius = self.CORNER_RADIUS + self.SELECTION_GAP
            painter.setPen(QPen(SELECTION_COLOR, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(ring, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._border)
        painter.drawRoundedRect(self._rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
        painter.setBrush(self._fill)
        painter.drawRoundedRect(self._inner_rect(), inner_radius, inner_radius)
        painter.setPen(self._label_color)
        painter.setFont(self._kind_label_font())
        painter.drawText(
            self._kind_label_rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.kind.label
        )
        if self.parity:
            mark, color = PARITY_MARKS[self.parity]
            radius = self.PARITY_RADIUS
            badge = QRectF(self._rect.right() - radius, self._rect.top() - radius, 2 * radius, 2 * radius)
            painter.setPen(QPen(QColor("#ffffff"), 1.5))
            painter.setBrush(QColor(color))
            painter.drawEllipse(badge)
            font = QFont(painter.font())
            font.setPixelSize(int(radius * 1.3))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, mark)

    def hoverMoveEvent(self, event) -> None:
        if self.kind.outgoing and self.border_contains(event.scenePos()):
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges:
                edge.update_path()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        self._title.start_editing()
        event.accept()

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.kind_key, "x": self.x(), "y": self.y(), "title": self.title}

    @classmethod
    def from_dict(cls, data: dict) -> "NodeItem":
        item = cls(str(data["title"]), str(data["id"]), data.get("type", DEFAULT_KIND))
        item.setPos(float(data["x"]), float(data["y"]))
        return item


def _anchor_from(value) -> QPointF | None:
    """Anchor from its [x, y] file form, pushed out onto the outline; None if absent or central."""
    if value is None:
        return None
    x, y = value
    x, y = float(x), float(y)
    scale = max(abs(x), abs(y))
    return QPointF(x / scale, y / scale) if scale > 1e-6 else None


def connection_error(source: NodeItem, target: NodeItem) -> str | None:
    """Why no edge may run from source to target, or None if one may."""
    if source is target:
        return "a box cannot connect to itself"
    if not source.kind.outgoing:
        return f"a {source.kind.label} has no outgoing connections"
    if not target.kind.incoming:
        return f"a {target.kind.label} has no incoming connections"
    return None


class EdgeHandle(QGraphicsObject):
    """Half-circle where an edge meets a node; drag it to slide the connection along the outline.

    It is a full circle for hit-testing and painting, but the node's body is cut out of the painted circle. It
    sits above the nodes because half of it overlaps the node's border. Its +x axis points out of the box.
    """

    RADIUS = 7.0

    def __init__(self, edge: "EdgeItem", is_source: bool):
        super().__init__()
        self.edge = edge
        self.is_source = is_source
        self._press_anchor: QPointF | None = None
        self.setZValue(2)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def boundingRect(self) -> QRectF:
        margin = self.RADIUS + 2
        return QRectF(-margin, -margin, 2 * margin, 2 * margin)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), self.RADIUS + 1, self.RADIUS + 1)
        return path

    def paint(self, painter, option, widget=None) -> None:
        # A full circle with the node's body cut out of it: it reads as half-hidden behind the box on a straight
        # side, and shows more of itself where the rounded corner curves away.
        node = self.edge.node(self.is_source)
        visible = QPainterPath()
        visible.addRect(self.boundingRect())
        painter.setClipPath(visible.subtracted(self.mapFromItem(node, node.shape())))
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setPen(QPen(SELECTION_COLOR if self.edge.isSelected() else EDGE_COLOR, 2))
        painter.setBrush(NODE_FILL)
        painter.drawEllipse(QPointF(0, 0), self.RADIUS, self.RADIUS)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        scene = self.scene()
        scene.setFocusItem(None)
        scene.clearSelection()
        self.edge.setSelected(True)
        self._press_anchor = self.edge.anchor(self.is_source)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press_anchor is None:
            return
        node = self.edge.node(self.is_source)
        self.edge.set_anchor(self.is_source, node.anchor_at(event.scenePos()))

    def mouseReleaseEvent(self, event) -> None:
        if self._press_anchor is None or event.button() != Qt.MouseButton.LeftButton:
            return
        old, self._press_anchor = self._press_anchor, None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        new = self.edge.anchor(self.is_source)
        if new != old:
            self.scene().move_edge_anchor(self.edge, self.is_source, old, new)


class EdgeItem(QGraphicsPathItem):
    """Curved arrow between an anchor on one node's outline and an anchor on another's.

    The curve leaves and enters each node at a right angle, starting from a half-circle handle on the outline.
    It is a plain line without the arrow head if either node's kind says so.
    """

    ARROW_LENGTH = 12.0
    ARROW_HALF_ANGLE = 0.45

    def __init__(
        self,
        source: NodeItem,
        target: NodeItem,
        source_anchor: QPointF | None = None,
        target_anchor: QPointF | None = None,
    ):
        super().__init__()
        self.source = source
        self.target = target
        # An anchor left out (files written before edges had them) faces the other node.
        if source_anchor is None:
            source_anchor = source.anchor_at(source.border_point_towards(target.scenePos()))
        if target_anchor is None:
            target_anchor = target.anchor_at(target.border_point_towards(source.scenePos()))
        self.source_anchor = source_anchor
        self.target_anchor = target_anchor
        self.has_head = source.kind.arrow_head and target.kind.arrow_head
        self._shaft = QPainterPath()
        self._head = QPainterPath()
        # The scene adds these next to the edge; they sit above the nodes, which the edge itself cannot.
        self.handles = (EdgeHandle(self, True), EdgeHandle(self, False))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(-1)
        self.update_path()

    def node(self, is_source: bool) -> NodeItem:
        return self.source if is_source else self.target

    def anchor(self, is_source: bool) -> QPointF:
        return self.source_anchor if is_source else self.target_anchor

    def set_anchor(self, is_source: bool, anchor: QPointF) -> None:
        if is_source:
            self.source_anchor = anchor
        else:
            self.target_anchor = anchor
        self.update_path()

    def update_path(self) -> None:
        radius = EdgeHandle.RADIUS
        ends = []
        for is_source, handle in zip((True, False), self.handles):
            node, anchor = self.node(is_source), self.anchor(is_source)
            normal = node.anchor_normal(anchor)
            point = node.anchor_pos(anchor)
            handle.setPos(point)
            handle.setRotation(math.degrees(math.atan2(normal.y(), normal.x())))
            ends.append((point + normal * radius, normal))
        (start, start_normal), (end, end_normal) = ends

        shaft, head = QPainterPath(), QPainterPath()
        distance = QLineF(start, end).length()
        if distance > 1.0:
            reach = max(30.0, distance * 0.4)
            # The arrow arrives against the target's outward normal.
            angle = math.atan2(-end_normal.y(), -end_normal.x())
            wings = [
                end - QPointF(math.cos(angle + side) * self.ARROW_LENGTH, math.sin(angle + side) * self.ARROW_LENGTH)
                for side in (self.ARROW_HALF_ANGLE, -self.ARROW_HALF_ANGLE)
            ]
            shaft.moveTo(start)
            shaft_end = (wings[0] + wings[1]) / 2 if self.has_head else end
            shaft.cubicTo(start + start_normal * reach, end + end_normal * reach, shaft_end)
            if self.has_head:
                head.addPolygon(QPolygonF([end, wings[0], wings[1], end]))
        self._shaft, self._head = shaft, head
        combined = QPainterPath(shaft)
        combined.addPath(head)
        self.setPath(combined)
        for handle in self.handles:
            handle.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            for handle in self.handles:
                handle.update()
        return super().itemChange(change, value)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self.path())

    def paint(self, painter, option, widget=None) -> None:
        color = SELECTION_COLOR if self.isSelected() else EDGE_COLOR
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._shaft)
        painter.setBrush(color)
        painter.drawPath(self._head)

    def to_dict(self) -> dict:
        return {
            "source": self.source.id,
            "target": self.target.id,
            "source_anchor": [self.source_anchor.x(), self.source_anchor.y()],
            "target_anchor": [self.target_anchor.x(), self.target_anchor.y()],
        }

    @classmethod
    def from_dict(cls, data: dict, nodes: dict[str, NodeItem]) -> "EdgeItem":
        source, target = nodes[data["source"]], nodes[data["target"]]
        error = connection_error(source, target)
        if error is not None:
            raise ValueError(f"connection not allowed: {error}")
        return cls(
            source,
            target,
            _anchor_from(data.get("source_anchor")),
            _anchor_from(data.get("target_anchor")),
        )

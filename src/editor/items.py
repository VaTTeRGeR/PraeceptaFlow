import math
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRectF, QSizeF, Qt, QTimer, Signal
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
    links: tuple[str, ...] = ()  # kinds it may connect to whatever outgoing/incoming say; "*" is any kind
    insert: bool = True  # False: not in the Insert menu (the cases of a decision)

    @property
    def starts_edges(self) -> bool:
        """Whether dragging from the border of such a box starts an edge."""
        return self.outgoing or bool(self.links)


# Keyed by the name stored in the file's "type" field. Data is cool (input teal, structure violet, output
# green), the algorithm is the one warm step, and notes take yellow, grey and rose.
NODE_KINDS = {
    "project_note": NodeKind("Project Note", "#fab005", outgoing=False, incoming=False),
    "data_input": NodeKind("Data Input", "#0c8599", incoming=False),
    "data_output": NodeKind("Data Output", "#2b8a3e", outgoing=False, links=("data_input",)),
    "data_structure": NodeKind("Data Structure", "#7048e8"),
    "algorithm": NodeKind("Algorithm", "#d9480f"),
    "tooling_note": NodeKind("Tooling Note", "#6c757d", arrow_head=False, links=("*",)),
    "implementation_note": NodeKind("Implementation Note", "#a68f96", arrow_head=False),
    # A decision is a header box (DecisionItem) with one CaseItem per branch; edges leave from the cases.
    "decision": NodeKind("Decision", "#c2255c", outgoing=False),
    "case": NodeKind("Case", "#c2255c", incoming=False, insert=False),
    "group": NodeKind("Group", "#1864ab"),  # holds a sub-canvas: the nodes whose group it is
}
DEFAULT_KIND = "algorithm"  # for files written before boxes had a type
GROUP_KIND = "group"

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
    """Centred text of any number of lines, inert until start_editing(); leaves edit mode on focus loss.

    Enter starts a new line; Ctrl+Enter, Escape or a click elsewhere ends the edit. Outside edit mode it
    ignores the mouse, so its parent receives the clicks.
    """

    def __init__(self, text: str, parent: QGraphicsItem | None = None):
        super().__init__(text, parent)
        self.setDefaultTextColor(TEXT_COLOR)
        self._text_before_edit = text
        option = self.document().defaultTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.document().setDefaultTextOption(option)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def is_editing(self) -> bool:
        return self.textInteractionFlags() != Qt.TextInteractionFlag.NoTextInteraction

    def start_editing(self) -> None:
        if self.is_editing():
            return
        self._text_before_edit = self.toPlainText()
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
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
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._edit_finished()

    def _edit_finished(self) -> None:
        scene = self.scene()
        if scene is not None and self.toPlainText() != self._text_before_edit:
            scene.text_edited.emit()

    def keyPressEvent(self, event) -> None:
        finish_keys = [Qt.Key.Key_Escape]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            finish_keys += [Qt.Key.Key_Return, Qt.Key.Key_Enter]
        if event.key() in finish_keys:
            self.clearFocus()
            return
        super().keyPressEvent(event)


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

    cases = ()  # only a DecisionItem has any
    # Only a CaseItem has one. Not parentItem(): on an item without parent that call makes PySide forget that
    # the scene owns the item, which is then deleted with its last Python reference.
    decision = None

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
        self.group: NodeItem | None = None  # the group node whose sub-canvas this is on; None on the main one
        self._rect = QRectF()
        self.natural_size = QSizeF()  # what the title needs
        self._min_size = QSizeF(0, 0)  # a decision stretches itself and its cases beyond that
        self._laying_out = False
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self._title = EditableText(title, self)
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
        self.natural_size = QSizeF(
            max(
                self.MIN_WIDTH,
                text_size.width() + 2 * (self.PADDING + self.BORDER_WIDTH),
                math.ceil(label_width) + 2 * self.CORNER_RADIUS,
            ),
            max(self.MIN_HEIGHT, text_size.height() + self.PADDING + 2 * self.BORDER_WIDTH),
        )
        width, height = self.natural_size.expandedTo(self._min_size).toTuple()
        self.prepareGeometryChange()
        self._rect = QRectF(-width / 2, -height / 2, width, height)
        self._title.setPos(-text_size.width() / 2, -text_size.height() / 2)
        self._resized()
        for edge in self.edges:
            edge.update_path()

    def set_min_size(self, size: QSizeF) -> None:
        self._min_size = size
        self._layout()

    def _resized(self) -> None:
        """Called when the box got a new size; for the subclasses that place something by it."""

    def _outer_rect(self) -> QRectF:
        """Everything that is selected with the box; the selection ring goes around it."""
        return self._rect

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
        return self._outer_rect().adjusted(-margin, -margin, margin, margin)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(self._rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
        return path

    def paint(self, painter, option, widget=None) -> None:
        inner_radius = max(self.CORNER_RADIUS - self.BORDER_WIDTH, 2.0)
        if self.isSelected():
            # Outside the box, so the type colour stays visible while the box is selected.
            ring = self._outer_rect().adjusted(-self.SELECTION_GAP, -self.SELECTION_GAP, self.SELECTION_GAP, self.SELECTION_GAP)
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
        if self.kind.starts_edges and self.border_contains(event.scenePos()):
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def itemChange(self, change, value):
        if change in (
            QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged,
            QGraphicsItem.GraphicsItemChange.ItemScenePositionHasChanged,  # a case, when its decision moves
        ):
            for edge in self.edges:
                edge.update_path()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event) -> None:
        # A group is entered; only a double-click on its title renames it.
        if self.kind_key == GROUP_KIND and not self._title.contains(self._title.mapFromItem(self, event.pos())):
            self.scene().enter_group(self)
        else:
            self._title.start_editing()
        event.accept()

    def to_dict(self) -> dict:
        data = {"id": self.id, "type": self.kind_key, "x": self.x(), "y": self.y(), "title": self.title}
        if self.group is not None:
            data["group"] = self.group.id
        return data

    @staticmethod
    def from_dict(data: dict) -> "NodeItem":
        """The node of a file entry; its group is resolved by EditorScene.load_dict."""
        kind = data.get("type", DEFAULT_KIND)
        if not NODE_KINDS[kind].insert:
            raise ValueError(f"a {kind} is not a node of its own")
        item = make_node(kind, str(data["title"]), str(data["id"]))
        item.setPos(float(data["x"]), float(data["y"]))
        if "cases" in data and isinstance(item, DecisionItem):
            item.set_cases([CaseItem(str(case["title"]), str(case["id"])) for case in data["cases"]])
        return item


class CircleButton(QGraphicsObject):
    """Small round button with a symbol, a child of the box it belongs to."""

    clicked = Signal()
    RADIUS = 7.0

    def __init__(self, symbol: str, parent: QGraphicsItem):
        super().__init__(parent)
        self._symbol = symbol
        self.setZValue(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def boundingRect(self) -> QRectF:
        margin = self.RADIUS + 1
        return QRectF(-margin, -margin, 2 * margin, 2 * margin)

    def paint(self, painter, option, widget=None) -> None:
        painter.setPen(QPen(EDGE_COLOR, 1.5))
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QPointF(0, 0), self.RADIUS, self.RADIUS)
        font = QFont(painter.font())
        font.setPixelSize(int(self.RADIUS * 1.6))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.boundingRect(), Qt.AlignmentFlag.AlignCenter, self._symbol)

    def mousePressEvent(self, event) -> None:
        event.setAccepted(event.button() == Qt.MouseButton.LeftButton)

    def mouseReleaseEvent(self, event) -> None:
        if self.contains(event.pos()):
            # Not from inside the event: the click may take this button's box out of the scene.
            QTimer.singleShot(0, self.clicked.emit)


class CaseItem(NodeItem):
    """One branch of a decision: a box in the row beneath the header, a child item of its DecisionItem.

    Edges leave from it like from any box. It is neither selected nor moved on its own: a press selects the
    decision, and dragging it left or right changes its place in the row.
    """

    _remove: CircleButton | None = None  # _resized runs before __init__ is through

    def __init__(self, title: str = "Case", node_id: str | None = None):
        super().__init__(title, node_id, "case")
        self._order_before: list[CaseItem] | None = None
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
            | QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges
        )
        self._remove = CircleButton("−", self)
        self._remove.clicked.connect(self._remove_clicked)
        self._resized()

    def _resized(self) -> None:
        if self._remove is not None:
            inset = CircleButton.RADIUS + 3
            self._remove.setPos(self._rect.left() + inset, self._rect.bottom() - inset)
        decision = self.decision
        if decision is not None:
            decision._arrange()

    def _remove_clicked(self) -> None:
        decision = self.decision
        if decision is not None and decision.scene() is not None:
            cases = [case for case in decision.cases if case is not self]
            decision.scene().set_cases(decision, list(decision.cases), cases, "Remove Case")

    def mousePressEvent(self, event) -> None:
        decision = self.decision
        if event.button() != Qt.MouseButton.LeftButton or decision is None:
            event.ignore()
            return
        if not decision.isSelected():
            self.scene().clearSelection()
            decision.setSelected(True)
        self._order_before = list(decision.cases)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._order_before is None:
            return
        decision = self.decision
        x = decision.mapFromScene(event.scenePos()).x()
        others = [case for case in decision.cases if case is not self]
        index = sum(1 for case in others if case.x() < x)
        order = [*others[:index], self, *others[index:]]
        if order != decision.cases:
            decision.set_cases(order)

    def mouseReleaseEvent(self, event) -> None:
        if self._order_before is None or event.button() != Qt.MouseButton.LeftButton:
            return
        old, self._order_before = self._order_before, None
        decision = self.decision
        if decision.cases != old:
            self.scene().set_cases(decision, old, list(decision.cases), "Reorder Cases")


class DecisionItem(NodeItem):
    """if/else or switch: a header box with a row of joined case boxes beneath it, one per branch.

    The list of cases is the whole representation: its order is the order of evaluation, and an edge out
    of a branch simply has that CaseItem as its source.
    """

    _arranging = False
    _add: CircleButton | None = None

    def __init__(self, title: str = "Decision", node_id: str | None = None):
        super().__init__(title, node_id, "decision")
        self.cases: list[CaseItem] = []
        self._add = CircleButton("+", self)
        self._add.clicked.connect(self._add_case)
        self.set_cases([CaseItem("Yes"), CaseItem("No")])

    def set_cases(self, cases: list[CaseItem]) -> None:
        """Make these the cases, in this order; one left out leaves the scene (its edges must be gone)."""
        for case in self.cases:
            if case not in cases:
                case.setParentItem(None)
                case.decision = None
                if case.scene() is not None:
                    case.scene().removeItem(case)
        for case in cases:
            case.setParentItem(self)
            case.decision = self
        self.cases = list(cases)
        self._arrange()

    def _add_case(self) -> None:
        if self.scene() is not None:
            self.scene().set_cases(self, list(self.cases), [*self.cases, CaseItem()], "Add Case")

    def _resized(self) -> None:
        self._arrange()

    def _arrange(self) -> None:
        """Give header and row the same width and the cases the same height, and put the row in place."""
        if self._arranging or not self.cases:
            return
        self._arranging = True
        try:
            self.prepareGeometryChange()
            natural = sum(case.natural_size.width() for case in self.cases)
            width = max(self.natural_size.width(), natural)
            height = max(case.natural_size.height() for case in self.cases)
            self.set_min_size(QSizeF(width, 0))
            spare = (width - natural) / len(self.cases)
            x = -width / 2
            for case in self.cases:
                case_width = case.natural_size.width() + spare
                case.set_min_size(QSizeF(case_width, height))
                case.setPos(x + case_width / 2, self._rect.bottom() + height / 2)
                case._remove.setVisible(len(self.cases) > 2)  # a decision keeps two branches
                x += case_width
            inset = CircleButton.RADIUS + 3
            self._add.setPos(width / 2 - inset, self._rect.bottom() + height - inset)
        finally:
            self._arranging = False

    def _outer_rect(self) -> QRectF:
        rect = QRectF(self._rect)
        if self.cases:
            rect.setBottom(rect.bottom() + self.cases[0]._rect.height())
        return rect

    def to_dict(self) -> dict:
        return {**super().to_dict(), "cases": [{"id": case.id, "title": case.title} for case in self.cases]}


def make_node(kind: str, title: str | None = None, node_id: str | None = None) -> NodeItem:
    """A new box of a kind; the class follows from the kind."""
    if kind == "decision":
        return DecisionItem("Decision" if title is None else title, node_id)
    return NodeItem("Box" if title is None else title, node_id, kind)


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
    if source.decision is target:
        return "a case cannot connect to its own decision"
    if "*" in source.kind.links + target.kind.links or target.kind_key in source.kind.links:
        return None
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


class RegionItem(QGraphicsObject):
    """Logical group: a coloured background with a title; the nodes lying on it belong to it.

    Its position is its top left corner. Only the header strip and the border take clicks, so the canvas
    under it still works. Dragging the header moves it and the nodes on it, dragging the border resizes it.
    """

    HEADER = 26.0
    MARGIN = 6.0  # width of the border that resizes
    MIN_SIZE = QSizeF(120.0, 80.0)
    COLOR = QColor("#4263eb")

    def __init__(self, title: str = "Logical Group", region_id: str | None = None, size: QSizeF | None = None):
        super().__init__()
        self.id = region_id or uuid.uuid4().hex
        self.group: NodeItem | None = None
        self.size = QSizeF(size or QSizeF(320.0, 220.0)).expandedTo(self.MIN_SIZE)
        self._resize: tuple[tuple[int, int], tuple[QPointF, QSizeF]] | None = None  # sides, geometry at press
        self._carried: list[tuple[QGraphicsItem, QPointF]] = []
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(-2)  # under the edges
        self._title = EditableText(title, self)
        self._title.setPos(self.MARGIN, 0)

    @property
    def title(self) -> str:
        return self._title.toPlainText()

    def geometry(self) -> tuple[QPointF, QSizeF]:
        return self.pos(), QSizeF(self.size)

    def set_geometry(self, pos: QPointF, size: QSizeF) -> None:
        self.prepareGeometryChange()
        self.size = QSizeF(size)
        self.setPos(pos)

    def members(self) -> list[QGraphicsItem]:
        """The boxes and logical groups on the same canvas whose centre lies on this one."""
        if self.scene() is None:
            return []
        rect = QRectF(self.pos(), self.size)
        return [
            item
            for item in self.scene().items()
            if item is not self
            and isinstance(item, (NodeItem, RegionItem))
            and not isinstance(item, CaseItem)
            and item.group is self.group
            and rect.contains(item.pos() if isinstance(item, NodeItem) else item.sceneBoundingRect().center())
        ]

    @property
    def resizing(self) -> bool:
        return self._resize is not None

    def carry(self, carrying: bool) -> None:
        """While carrying, the members as they are now follow every move."""
        self._carried = [(item, item.pos() - self.pos()) for item in self.members()] if carrying else []

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            for item, offset in self._carried:
                item.setPos(self.pos() + offset)
        return super().itemChange(change, value)

    def _box(self) -> QRectF:
        return QRectF(QPointF(0, 0), self.size)

    def boundingRect(self) -> QRectF:
        return self._box().adjusted(-2, -2, 2, 2)

    def shape(self) -> QPainterPath:
        path, inside = QPainterPath(), QPainterPath()
        path.addRect(self._box())
        inside.addRect(self._box().adjusted(self.MARGIN, self.HEADER, -self.MARGIN, -self.MARGIN))
        return path.subtracted(inside)

    def paint(self, painter, option, widget=None) -> None:
        box = self._box()
        painter.setPen(QPen(SELECTION_COLOR if self.isSelected() else self.COLOR, 2))
        painter.setBrush(_tint(self.COLOR, 0.08))
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(self.COLOR, 0.25))
        painter.drawRoundedRect(QRectF(1, 1, box.width() - 2, self.HEADER - 1), 5, 5)

    def _sides_at(self, pos: QPointF) -> tuple[int, int]:
        """Which sides a point of the border resizes: -1 left or top, 1 right or bottom, 0 neither."""
        margin = self.MARGIN
        return (
            (pos.x() > self.size.width() - margin) - (pos.x() < margin),
            (pos.y() > self.size.height() - margin) - (pos.y() < margin),
        )

    def hoverMoveEvent(self, event) -> None:
        across, down = self._sides_at(event.pos())
        if across and down:
            shape = Qt.CursorShape.SizeFDiagCursor if across == down else Qt.CursorShape.SizeBDiagCursor
        elif across or down:
            shape = Qt.CursorShape.SizeHorCursor if across else Qt.CursorShape.SizeVerCursor
        else:
            shape = Qt.CursorShape.SizeAllCursor
        self.setCursor(shape)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        sides = self._sides_at(event.pos())
        if event.button() == Qt.MouseButton.LeftButton and sides != (0, 0):
            self.scene().clearSelection()
            self.setSelected(True)
            self._resize = (sides, self.geometry())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resize is None:
            super().mouseMoveEvent(event)
            return
        (across, down), (pos, size) = self._resize
        left, top = pos.x(), pos.y()
        right, bottom = left + size.width(), top + size.height()
        pointer = event.scenePos()
        if across < 0:
            left = min(pointer.x(), right - self.MIN_SIZE.width())
        elif across > 0:
            right = max(pointer.x(), left + self.MIN_SIZE.width())
        if down < 0:
            top = min(pointer.y(), bottom - self.MIN_SIZE.height())
        elif down > 0:
            bottom = max(pointer.y(), top + self.MIN_SIZE.height())
        self.set_geometry(QPointF(left, top), QSizeF(right - left, bottom - top))

    def mouseReleaseEvent(self, event) -> None:
        if self._resize is None:
            super().mouseReleaseEvent(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        old, self._resize = self._resize[1], None
        if self.geometry() != old:
            self.scene().resize_region(self, old, self.geometry())

    def mouseDoubleClickEvent(self, event) -> None:
        self._title.start_editing()
        event.accept()

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "x": self.x(),
            "y": self.y(),
            "w": self.size.width(),
            "h": self.size.height(),
            "title": self.title,
        }
        if self.group is not None:
            data["group"] = self.group.id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RegionItem":
        item = cls(str(data["title"]), str(data["id"]), QSizeF(float(data["w"]), float(data["h"])))
        item.setPos(float(data["x"]), float(data["y"]))
        return item


def group_of(item: QGraphicsItem) -> NodeItem | None:
    """The group whose sub-canvas a box, logical group, edge or edge handle is on."""
    if isinstance(item, EdgeHandle):
        item = item.edge
    if isinstance(item, EdgeItem):
        item = item.source
    if isinstance(item, CaseItem):
        item = item.decision
    return item.group


def group_chain(group: NodeItem | None) -> list[NodeItem]:
    """A group and the groups around it, outermost first; empty for the main canvas."""
    chain = []
    while group is not None:
        chain.insert(0, group)
        group = group.group
    return chain

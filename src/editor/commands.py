from PySide6.QtCore import QPointF, QSizeF
from PySide6.QtGui import QUndoCommand
from PySide6.QtWidgets import QGraphicsItem

from editor.items import GROUP_KIND, CaseItem, DecisionItem, EdgeItem, NodeItem, RegionItem


class AddItemCommand(QUndoCommand):
    def __init__(self, scene, item: QGraphicsItem, text: str):
        super().__init__(text)
        self._scene = scene
        self._item = item

    def redo(self) -> None:
        self._scene.add_item(self._item)

    def undo(self) -> None:
        self._scene.remove_item(self._item)


class DeleteItemsCommand(QUndoCommand):
    """Removes the given items plus every edge attached to a removed node and all that is inside a removed group."""

    def __init__(self, scene, items: list[QGraphicsItem]):
        super().__init__("Delete")
        self._scene = scene
        self._edges: list[EdgeItem] = []
        self._others: list[QGraphicsItem] = []
        for item in items:
            if isinstance(item, EdgeItem):
                self._add_edge(item)
            else:
                self._add_other(item)

    def _add_edge(self, edge: EdgeItem) -> None:
        if edge not in self._edges:
            self._edges.append(edge)

    def _add_other(self, item: QGraphicsItem) -> None:
        if item in self._others:
            return
        self._others.append(item)
        if not isinstance(item, NodeItem):
            return
        for node in (item, *item.cases):
            for edge in node.edges:
                self._add_edge(edge)
        if item.kind_key == GROUP_KIND:
            for member in self._scene.items():
                if isinstance(member, (NodeItem, RegionItem)) and member.group is item:
                    self._add_other(member)

    def redo(self) -> None:
        for item in self._edges + self._others:
            self._scene.remove_item(item)

    def undo(self) -> None:
        for item in self._others + self._edges:
            self._scene.add_item(item)


class MoveAnchorCommand(QUndoCommand):
    """Slides one end of an edge to another place on its node's outline."""

    def __init__(self, edge: EdgeItem, is_source: bool, old: QPointF, new: QPointF):
        super().__init__("Move Connection")
        self._edge = edge
        self._is_source = is_source
        self._old = old
        self._new = new

    def redo(self) -> None:
        self._edge.set_anchor(self._is_source, self._new)

    def undo(self) -> None:
        self._edge.set_anchor(self._is_source, self._old)


class MoveItemsCommand(QUndoCommand):
    def __init__(self, moves: list[tuple[QGraphicsItem, QPointF, QPointF]]):
        super().__init__("Move")
        self._moves = moves

    def redo(self) -> None:
        for item, _, new_pos in self._moves:
            item.setPos(new_pos)

    def undo(self) -> None:
        for item, old_pos, _ in self._moves:
            item.setPos(old_pos)


class SetCasesCommand(QUndoCommand):
    """Adds, removes or reorders the cases of a decision; the edges of a removed case must be gone before."""

    def __init__(self, decision: DecisionItem, old: list[CaseItem], new: list[CaseItem], text: str):
        super().__init__(text)
        self._decision = decision
        self._old = old
        self._new = new

    def redo(self) -> None:
        self._decision.set_cases(self._new)

    def undo(self) -> None:
        self._decision.set_cases(self._old)


class SetGeometryCommand(QUndoCommand):
    """Resizes a logical group; a geometry is its (position, size)."""

    def __init__(self, region: RegionItem, old: tuple[QPointF, QSizeF], new: tuple[QPointF, QSizeF]):
        super().__init__("Resize Logical Group")
        self._region = region
        self._old = old
        self._new = new

    def redo(self) -> None:
        self._region.set_geometry(*self._new)

    def undo(self) -> None:
        self._region.set_geometry(*self._old)

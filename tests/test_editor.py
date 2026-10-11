"""Checks of the editor's items, scene and commands, without a display.

Run with: .venv\\Scripts\\python.exe tests\\test_editor.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtCore import QPointF, QSettings, QSizeF
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import QApplication

from editor.commands import AddItemCommand, DeleteItemsCommand, MoveItemsCommand
from editor.export import issues_section, project_description
from editor.items import CaseItem, EdgeItem, NodeItem, RegionItem, connection_error, make_node
from editor.main_window import MainWindow
from editor.scene import EditorScene


def new_scene() -> tuple[EditorScene, QUndoStack]:
    stack = QUndoStack()
    return EditorScene(stack), stack


def assert_round_trip(scene: EditorScene) -> dict:
    """The file form of a scene, checked to load back the same."""
    data = scene.to_dict()
    other, _ = new_scene()
    other.load_dict(data)
    assert other.to_dict() == data
    return data


def test_connections() -> None:
    kinds = ("project_note", "data_input", "data_output", "algorithm", "tooling_note", "implementation_note")
    node = {kind: NodeItem(kind=kind) for kind in kinds}
    assert connection_error(node["data_output"], node["data_input"]) is None
    assert connection_error(node["data_output"], node["algorithm"]) is not None
    assert connection_error(node["algorithm"], node["data_input"]) is not None
    assert node["data_output"].kind.starts_edges
    for kind in kinds:
        if kind != "tooling_note":
            assert connection_error(node["tooling_note"], node[kind]) is None, kind
            assert connection_error(node[kind], node["tooling_note"]) is None, kind
    assert connection_error(node["implementation_note"], node["project_note"]) is not None


def test_decision() -> None:
    scene, stack = new_scene()
    decision, target = make_node("decision"), NodeItem("Target")
    target.setPos(0, 300)
    scene.add_item(decision)
    scene.add_item(target)
    yes, no = decision.cases
    assert yes.x() < no.x()
    assert connection_error(yes, target) is None and connection_error(decision, target) is not None
    assert connection_error(yes, decision) is not None  # also what a drop on a sibling case resolves to
    edge = EdgeItem(yes, target)
    scene.add_item(edge)

    # The file names the case as the edge's source and loads back the same.
    data = assert_round_trip(scene)
    assert [case["id"] for case in data["nodes"][0]["cases"]] == [yes.id, no.id]
    assert data["edges"][0]["source"] == yes.id

    # Reordering moves the case and its edge's start with it.
    start = edge.path().elementAt(0).x
    scene.set_cases(decision, [yes, no], [no, yes], "Reorder Cases")
    assert no.x() < yes.x() and edge.path().elementAt(0).x > start
    stack.undo()
    assert decision.cases == [yes, no] and edge.path().elementAt(0).x == start

    # Removing a case takes its edges along, in one undo step.
    extra = CaseItem("Maybe")
    scene.set_cases(decision, [yes, no], [yes, no, extra], "Add Case")
    assert extra.scene() is scene
    scene.set_cases(decision, [yes, no, extra], [no, extra], "Remove Case")
    assert yes.scene() is None and edge.scene() is None and target.edges == []
    stack.undo()
    assert decision.cases == [yes, no, extra] and edge.scene() is scene and target.edges == [edge]

    # Deleting the decision removes the edges of its cases.
    stack.push(DeleteItemsCommand(scene, [decision]))
    assert edge.scene() is None and yes.scene() is None
    stack.undo()
    assert edge.scene() is scene and yes.scene() is scene


def test_groups() -> None:
    scene, stack = new_scene()
    group, outside = make_node("group"), NodeItem("Outside")
    for item in (group, outside):
        stack.push(AddItemCommand(scene, item, "Add"))
    scene.enter_group(group)
    assert not outside.isVisible() and not group.isVisible()
    inner, inner2 = NodeItem("Inner"), NodeItem("Inner 2")
    for item in (inner, inner2):
        item.group = scene.current_group
        stack.push(AddItemCommand(scene, item, "Add"))
    edge = EdgeItem(inner, inner2)
    stack.push(AddItemCommand(scene, edge, "Add Connection"))
    assert inner.isVisible() and edge.isVisible() and edge.handles[0].isVisible()
    assert scene.visible_rect().contains(inner.sceneBoundingRect())
    scene.enter_group(None)
    assert outside.isVisible() and not inner.isVisible() and not edge.handles[0].isVisible()

    data = assert_round_trip(scene)
    assert {node["title"]: node.get("group") for node in data["nodes"]}["Inner"] == group.id

    # Deleting a group deletes what is inside, and leaves its canvas if that is the one shown.
    scene.enter_group(group)
    stack.push(DeleteItemsCommand(scene, [group]))
    assert scene.current_group is None and inner.scene() is None and edge.scene() is None
    stack.undo()
    assert inner.scene() is scene and edge.scene() is scene and not inner.isVisible()


def test_region() -> None:
    scene, stack = new_scene()
    region, on_it, beside = RegionItem(size=QSizeF(300, 200)), NodeItem("On it"), NodeItem("Beside")
    on_it.setPos(100, 100)
    beside.setPos(500, 100)
    for item in (region, on_it, beside):
        scene.add_item(item)
    assert region.members() == [on_it]

    # What the scene does for a drag of the selected region: the node on it follows, one command undoes both.
    region.carry(True)
    region.setPos(40, 0)
    region.carry(False)
    assert on_it.pos() == QPointF(140, 100) and beside.pos() == QPointF(500, 100)
    stack.push(MoveItemsCommand([(region, QPointF(0, 0), region.pos()), (on_it, QPointF(100, 100), on_it.pos())]))
    stack.undo()
    assert region.pos() == QPointF(0, 0) and on_it.pos() == QPointF(100, 100)

    scene.resize_region(region, region.geometry(), (QPointF(-20, 0), QSizeF(320, 200)))
    assert region.size == QSizeF(320, 200)
    stack.undo()
    assert region.geometry() == (QPointF(0, 0), QSizeF(300, 200))

    assert_round_trip(scene)


def test_issues() -> None:
    with tempfile.TemporaryDirectory() as folder:
        window = MainWindow(QSettings(str(Path(folder, "settings.ini")), QSettings.Format.IniFormat))
        scene = window._scene
        first, second = NodeItem("First"), NodeItem("Second")
        second.setPos(0, 300)
        scene.add_item(first)
        scene.add_item(second)
        path = Path(folder, "graph.pfg")
        assert window.save_file(path)

        # What _run_claude records and a parity check answers.
        window._run_path, window._run_text = path, project_description(scene.to_dict())
        window._show_result({
            "steps": [
                {"tag": first.id[:8], "status": "mismatch", "reason": "Differs."},
                {"tag": second.id[:8], "status": "unimplemented", "reason": ""},
            ],
            "other": ["stray code"],
        })
        report = json.loads(path.with_suffix(".issues").read_text(encoding="utf-8"))
        assert report["other"] == ["stray code"] and report["steps"][first.id[:8]]["reason"] == "Differs."
        assert (first.parity, second.parity) == ("mismatch", "unimplemented")

        # Changing a step hides its issue only; undoing the change shows it again.
        window._undo_stack.push(AddItemCommand(scene, EdgeItem(first, second), "Connect"))
        assert (first.parity, second.parity) == (None, "unimplemented")
        window._undo_stack.undo()
        assert first.parity == "mismatch"
        second._title.setPlainText("Second, renamed")
        scene.text_edited.emit()
        assert (first.parity, second.parity) == ("mismatch", None)
        # A generation is told the issues that still hold.
        assert issues_section(*window._issues()) == f"\nParity Issues:\n- [{first.id[:8]}] mismatch: Differs.\n- stray code\n"

        # A file edited by hand into nonsense shows nothing and breaks nothing.
        issues = path.with_suffix(".issues")
        good = issues.read_text(encoding="utf-8")
        for broken in ("[]", '{"steps": 1, "other": 2}', json.dumps({"steps": {first.id[:8]: {"status": ["x"]}, "zz": 3}})):
            issues.write_text(broken, encoding="utf-8")
            window._show_issues()
            assert first.parity is None and window._issues() == ({}, [])
        issues.write_text(good, encoding="utf-8")

        # The issues come back with the file, and a generation ends them.
        assert window.save_file(path) and window.open_file(path)
        reopened = {node.id: node.parity for node in window._nodes()}
        assert reopened == {first.id: "mismatch", second.id: None}
        window._show_result({"notes": []})
        assert not path.with_suffix(".issues").exists()
        window._update_code_view()  # what the end of the run does
        assert not any(node.parity for node in window._nodes())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    test_issues()
    test_connections()
    test_decision()
    test_groups()
    test_region()
    print("ok")

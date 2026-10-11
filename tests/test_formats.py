"""Checks of the two text formats shared with INSTRUCTIONS: the *.pd export and the tag comments.

Run with: .venv\\Scripts\\python.exe tests\\test_formats.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from editor.export import issues_section, project_description, tagged_items
from editor.source_map import tag_sections


def node(node_id: str, kind: str, y: float, title: str, **more) -> dict:
    return {"id": node_id, "type": kind, "x": 0, "y": y, "title": title, **more}


def test_project_description() -> None:
    graph = {
        "nodes": [
            node("b" * 8, "algorithm", 100, "Ping\ntwice"),
            node("a" * 8, "data_input", 0, "Read IPs"),
            node("n" * 8, "implementation_note", 100, "No retries"),
            node("t" * 8, "tooling_note", 0, "Python"),
            node("p" * 8, "project_note", 0, "Ping tool"),
        ],
        "edges": [
            {"source": "a" * 8, "target": "b" * 8},
            {"source": "n" * 8, "target": "b" * 8},
        ],
    }
    assert project_description(graph) == (
        "Project Notes:\n"
        "- [pppppppp] Ping tool\n"
        "\n"
        "Tooling notes:\n"
        "- [tttttttt] Python\n"
        "\n"
        "Program Flow:\n"
        "- [aaaaaaaa] Data Input: Read IPs\n"
        "  Next: [bbbbbbbb]\n"
        "- [bbbbbbbb] Algorithm: Ping\n"
        "  twice\n"
        "  Next: end\n"
        "  Implementation notes:\n"
        "    - [nnnnnnnn] No retries\n"
    )
    # A step's item runs to the next item or section; the note under it is part of it, not an item itself.
    items = tagged_items(project_description(graph))
    assert sorted(items) == ["aaaaaaaa", "bbbbbbbb", "pppppppp", "tttttttt"]
    assert items["aaaaaaaa"] == "- [aaaaaaaa] Data Input: Read IPs\n  Next: [bbbbbbbb]"
    assert items["bbbbbbbb"].endswith("    - [nnnnnnnn] No retries")
    assert items["tttttttt"] == "- [tttttttt] Python"

    # What a generation gets after the description; matches are no issue.
    verdicts = {
        "aaaaaaaa": {"status": "match", "reason": "Match"},
        "bbbbbbbb": {"status": "mismatch", "reason": "Pings once."},
    }
    assert issues_section(verdicts, ["stray code"]) == "\nParity Issues:\n- [bbbbbbbb] mismatch: Pings once.\n- stray code\n"
    assert issues_section({"aaaaaaaa": verdicts["aaaaaaaa"]}, []) == ""


def test_decision_group_region() -> None:
    graph = {
        "nodes": [
            node("a" * 8, "data_input", 0, "Read temperature"),
            node("d" * 8, "decision", 100, "temperature", cases=[
                {"id": "c1", "title": "< 0"},
                {"id": "c2", "title": "else"},
            ]),
            node("g" * 8, "group", 200, "Alarm\nhandling"),
            # Inside the group: e leads to f, f is the group's last step.
            node("e" * 8, "algorithm", 0, "Log", group="g" * 8),
            node("f" * 8, "algorithm", 100, "Beep", group="g" * 8),
            node("o" * 8, "data_output", 300, "Print"),
            node("t" * 8, "tooling_note", 200, "Use syslog"),
        ],
        "edges": [
            {"source": "a" * 8, "target": "d" * 8},
            {"source": "c1", "target": "g" * 8},
            {"source": "e" * 8, "target": "f" * 8},
            {"source": "g" * 8, "target": "o" * 8},
            {"source": "t" * 8, "target": "g" * 8},
        ],
        "regions": [
            {"id": "1" * 8, "x": -50, "y": -50, "w": 100, "h": 200, "title": "Sensors"},
            {"id": "2" * 8, "x": -50, "y": -50, "w": 100, "h": 100, "title": "Inner\nloop", "group": "g" * 8},
            # No step lies on it, so it is not listed.
            {"id": "3" * 8, "x": 500, "y": 500, "w": 100, "h": 100, "title": "Empty"},
        ],
    }
    assert project_description(graph) == (
        "Logical Groups:\n"
        "- [11111111] Sensors\n"
        "- [22222222] Inner\n"
        "  loop\n"
        "\n"
        "Program Flow:\n"
        "- [aaaaaaaa] Data Input: Read temperature\n"
        "  Logical group: [11111111]\n"
        "  Next: [dddddddd]\n"
        "- [dddddddd] Decision: temperature\n"
        "  Logical group: [11111111]\n"
        "  Cases:\n"
        "    - < 0\n"
        "      Next: [eeeeeeee]\n"
        "    - else\n"
        "      Next: end\n"
        "- [eeeeeeee] Algorithm: Log\n"
        "  Group: Alarm\n"
        "  Logical group: [22222222]\n"
        "  Next: [ffffffff]\n"
        "  Tooling notes:\n"
        "    - [tttttttt] Use syslog\n"
        "- [ffffffff] Algorithm: Beep\n"
        "  Group: Alarm\n"
        "  Next: [oooooooo]\n"
        "  Tooling notes:\n"
        "    - [tttttttt] Use syslog\n"
        "- [oooooooo] Data Output: Print\n"
        "  Next: end\n"
    )


def test_tag_sections() -> None:
    lines = [
        "import x",
        "# [aaaaaaaa] Algorithm: one",
        "a()",
        "# [aaaaaaaa] end",
        "// [bbbbbbbb] Algorithm: two",
        "// [cccccccc] Algorithm: three",
        "b()",
        "// [bbbbbbbb] end",
        "// [cccccccc] end",
        "    # [dddddddd] Data Output: no end comment",
        "    d()",
        "    [eeeeeeee]",  # no comment sign: not a tag comment
    ]
    assert sorted(tag_sections(lines)) == [
        ("aaaaaaaa", 1, 3),
        ("bbbbbbbb", 4, 7),
        ("cccccccc", 5, 8),
        ("dddddddd", 9, 11),
    ]
    # A logical group's section holds the sections of its steps.
    nested = ["# [11111111] Logical group: g", "def f():", "    # [aaaaaaaa] Algorithm: one", "    a()", "    # [aaaaaaaa] end", "# [11111111] end"]
    assert sorted(tag_sections(nested)) == [("11111111", 0, 5), ("aaaaaaaa", 2, 4)]


if __name__ == "__main__":
    test_project_description()
    test_decision_group_region()
    test_tag_sections()
    print("ok")

import heapq
import re

from editor.items import DEFAULT_KIND, GROUP_KIND, NODE_KINDS

# The kinds that are steps of the program flow; each is listed under the hash other steps refer to it by.
STEP_KINDS = ("data_input", "data_output", "data_structure", "algorithm", "decision")
# Note kinds and the heading of their list, under the step they are connected to or at the end of the file.
NOTE_HEADINGS = {"implementation_note": "Implementation notes:", "tooling_note": "Tooling notes:"}
PROJECT_NOTE_KIND = "project_note"
MIN_HASH_LENGTH = 8
# A list item at the left margin and the lines under it, up to the next line that starts at the margin.
_TAGGED_ITEM = re.compile(r"^- \[([^\]]+)\].*(?:\n(?!\S).*)*", re.MULTILINE)


def _kind(node: dict) -> str:
    return node.get("type", DEFAULT_KIND)


def _position(node: dict) -> tuple[float, float, str]:
    """Sort key for reading order: top to bottom, then left to right."""
    return (float(node["y"]), float(node["x"]), node["id"])


def first_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[0]


def _hash_length(ids) -> int:
    """Shortest id prefix, at least MIN_HASH_LENGTH long, that still tells all nodes apart."""
    length = MIN_HASH_LENGTH
    while len({node_id[:length] for node_id in ids}) < len(ids):
        length += 1
    return length


def _flow_order(steps: dict[str, dict], successors: dict[str, set[str]]) -> list[dict]:
    """Steps in the order the flow reaches them; a step on a cycle follows the ones that are not."""
    waiting = {node_id: 0 for node_id in steps}
    for targets in successors.values():
        for target in targets:
            waiting[target] += 1
    ready = [_position(step) for node_id, step in steps.items() if waiting[node_id] == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        node_id = heapq.heappop(ready)[2]
        ordered.append(steps[node_id])
        del waiting[node_id]
        for target in successors.get(node_id, ()):
            waiting[target] -= 1
            if waiting[target] == 0:
                heapq.heappush(ready, _position(steps[target]))
    return ordered + sorted((steps[node_id] for node_id in waiting), key=_position)


def _list_item(text: str, level: int = 0) -> list[str]:
    """Lines of a list item; the further lines of a multi-line text continue it."""
    pad = "  " * level
    first, *rest = text.strip().splitlines() or [""]
    return [f"{pad}- {first}".rstrip(), *(f"{pad}  {line}".rstrip() for line in rest)]


def tagged_items(text: str) -> dict[str, str]:
    """The top-level list items of a project description by their hash, each with its indented lines."""
    return {match[1]: match[0].rstrip() for match in _TAGGED_ITEM.finditer(text)}


def issues_section(steps: dict[str, dict], other: list[str]) -> str:
    """The `Parity Issues:` section a generation gets after the description: what the last check found wrong."""
    lines = []
    for tag, entry in steps.items():
        if entry["status"] != "match":
            lines += _list_item(f"[{tag}] {entry['status']}: {entry['reason']}")
    for line in other:
        lines += _list_item(line)
    return "\n".join(["", "Parity Issues:", *lines, ""]) if lines else ""


def project_description(data: dict) -> str:
    """Text of a project description (*.pd) for a graph in the form of EditorScene.to_dict().

    Groups are not steps: the flow runs through them, from an edge into a group to the first steps inside
    and from the last steps inside to what the group leads to.
    """
    nodes = {node["id"]: node for node in data.get("nodes", [])}
    steps = {node_id: node for node_id, node in nodes.items() if _kind(node) in STEP_KINDS}
    groups = {node_id: node for node_id, node in nodes.items() if _kind(node) == GROUP_KIND}
    regions = sorted(data.get("regions", []), key=_position)
    # Logical groups are tagged in the source code too, so their tags must not collide with a node's.
    length = _hash_length(nodes.keys() | {region["id"] for region in regions})
    # An edge out of a branch of a decision has the case as its source.
    owner = {case["id"]: node_id for node_id, node in steps.items() for case in node.get("cases", [])}
    inside: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        if node.get("group") in groups:
            inside.setdefault(node["group"], []).append(node_id)

    def ref(node_id: str) -> str:
        return f"[{node_id[:length]}]"

    def steps_in(node_id: str) -> list[str]:
        """A step itself, or every step inside a group."""
        if node_id in steps:
            return [node_id]
        return [step_id for member in inside.get(node_id, ()) for step_id in steps_in(member)]

    flow: dict[str, set[str]] = {}  # step, case or group -> the steps and groups its edges lead to
    entered = set()  # steps and groups with an edge leading to them
    step_notes: dict[str, set[str]] = {}
    attached = set()
    for edge in data.get("edges", []):
        if edge["source"] == edge["target"]:
            continue
        source, target = owner.get(edge["source"], edge["source"]), owner.get(edge["target"], edge["target"])
        if source not in nodes or target not in nodes:
            continue
        if (source in steps or source in groups) and (target in steps or target in groups):
            flow.setdefault(edge["source"], set()).add(target)
            entered.add(target)
            continue
        # A line to a note has no head, so it does not matter which end the note is on. A note on a
        # group is a note on every step inside.
        for other_id, note_id in ((source, target), (target, source)):
            if _kind(nodes[note_id]) in NOTE_HEADINGS:
                for step_id in steps_in(other_id):
                    step_notes.setdefault(step_id, set()).add(note_id)
                    attached.add(note_id)

    def entries(node_id: str, seen: frozenset) -> set[str]:
        """The steps the flow reaches first when it arrives at a step or a group."""
        if node_id in steps:
            return {node_id}
        if node_id in seen:  # groups without steps that lead to each other
            return set()
        seen = seen | {node_id}
        members = [member for member in inside.get(node_id, ()) if member in steps or member in groups]
        if not members:
            return after(node_id, seen)  # nothing to do inside: the flow passes through
        # Normally the members nothing leads to; if all are on a cycle, the topmost one.
        first = [m for m in members if m not in entered] or [min(members, key=lambda m: _position(nodes[m]))]
        return set().union(*(entries(member, seen) for member in first))

    def after(source: str, seen: frozenset = frozenset()) -> set[str]:
        """The steps that follow a step, case or group; the end of a group goes on with what follows the group."""
        leads_to = flow.get(source)
        if leads_to:
            return set().union(*(entries(target, seen) for target in leads_to))
        group = nodes[owner.get(source, source)].get("group")
        return after(group, seen) if group in groups else set()

    def targets(source: str) -> str:
        following = sorted((steps[i] for i in after(source)), key=_position)
        return ", ".join(ref(i["id"]) for i in following) or "end"

    def notes(kind: str, ids) -> list[dict]:
        return sorted((nodes[i] for i in ids if _kind(nodes[i]) == kind), key=_position)

    def note_item(note: dict, level: int = 0) -> list[str]:
        """A note's list item, led by its tag so the generation can report on it."""
        return _list_item(f"{ref(note['id'])} {note['title'].strip()}", level)

    def group_path(node: dict) -> str:
        """Titles of the groups a node is in, outermost first."""
        names = []
        while node.get("group") in groups:
            node = groups[node["group"]]
            names.insert(0, first_line(node["title"]))
        return " / ".join(names)

    def logical_groups(node: dict) -> list[dict]:
        """The logical groups (on the same canvas) the node lies on."""
        return [
            region
            for region in regions
            if region.get("group") == node.get("group")
            and region["x"] <= node["x"] <= region["x"] + region["w"]
            and region["y"] <= node["y"] <= region["y"] + region["h"]
        ]

    sections = []

    project_notes = notes(PROJECT_NOTE_KIND, nodes)
    if project_notes:
        lines = ["Project Notes:"]
        for note in project_notes:
            lines += note_item(note)
        sections.append(lines)

    def add_loose_notes(kind: str) -> None:
        """List the notes of a kind that are not connected to any step."""
        loose = notes(kind, nodes.keys() - attached)
        if loose:
            lines = [NOTE_HEADINGS[kind]]
            for note in loose:
                lines += note_item(note)
            sections.append(lines)

    # General tooling notes concern the whole project, so they follow the project notes.
    add_loose_notes("tooling_note")

    # A logical group is named once here; its steps only refer to its tag. One without steps has no code to tag.
    used = [region for region in regions if any(region in logical_groups(step) for step in steps.values())]
    if used:
        lines = ["Logical Groups:"]
        for region in used:
            lines += _list_item(f"{ref(region['id'])} {region['title'].strip()}")
        sections.append(lines)

    if steps:
        lines = ["Program Flow:"]
        # A step that only leads back to itself (a case to its own decision) is not waiting for itself.
        successors = {
            step_id: set().union(*(after(i) for i in (step_id, *(c["id"] for c in step.get("cases", []))))) - {step_id}
            for step_id, step in steps.items()
        }
        for step in _flow_order(steps, successors):
            step_id = step["id"]
            label = NODE_KINDS[_kind(step)].label
            lines += _list_item(f"{ref(step_id)} {label}: {step['title'].strip()}")
            # The keywords below are plain lines indented under the step; only text items carry a dash.
            tags = ", ".join(ref(region["id"]) for region in logical_groups(step))
            for heading, names in (("Group", group_path(step)), ("Logical group", tags)):
                if names:
                    lines.append(f"  {heading}: {names}")
            if "cases" in step:
                # Each case of a decision has its own continuation, in the order they are evaluated.
                lines.append("  Cases:")
                for case in step["cases"]:
                    lines += _list_item(case["title"], 2)
                    lines.append(f"      Next: {targets(case['id'])}")
            else:
                lines.append(f"  Next: {targets(step_id)}")
            for kind, heading in NOTE_HEADINGS.items():
                own = notes(kind, step_notes.get(step_id, ()))
                if own:
                    lines.append(f"  {heading}")
                    for note in own:
                        lines += note_item(note, 2)
        sections.append(lines)

    add_loose_notes("implementation_note")

    return "\n".join("\n".join(lines) + "\n" for lines in sections)

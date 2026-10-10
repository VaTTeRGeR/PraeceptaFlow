import heapq

from editor.items import DEFAULT_KIND, NODE_KINDS

# The kinds that are steps of the program flow; each is listed under the hash other steps refer to it by.
STEP_KINDS = ("data_input", "data_output", "data_structure", "algorithm")
# Note kinds and the heading of their list, under the step they are connected to or at the end of the file.
NOTE_HEADINGS = {"implementation_note": "Implementation notes:", "tooling_note": "Tooling notes:"}
PROJECT_NOTE_KIND = "project_note"
MIN_HASH_LENGTH = 8


def _kind(node: dict) -> str:
    return node.get("type", DEFAULT_KIND)


def _position(node: dict) -> tuple[float, float, str]:
    """Sort key for reading order: top to bottom, then left to right."""
    return (float(node["y"]), float(node["x"]), node["id"])


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


def project_description(data: dict) -> str:
    """Text of a project description (*.pd) for a graph in the form of EditorScene.to_dict()."""
    nodes = {node["id"]: node for node in data.get("nodes", [])}
    steps = {node_id: node for node_id, node in nodes.items() if _kind(node) in STEP_KINDS}
    length = _hash_length(nodes.keys())

    def ref(node_id: str) -> str:
        return f"[{node_id[:length]}]"

    successors: dict[str, set[str]] = {}
    step_notes: dict[str, set[str]] = {}
    attached = set()
    for edge in data.get("edges", []):
        source, target = edge["source"], edge["target"]
        if source == target or source not in nodes or target not in nodes:
            continue
        if source in steps and target in steps:
            successors.setdefault(source, set()).add(target)
            continue
        # A line to a note has no head, so it does not matter which end the note is on.
        for step_id, note_id in ((source, target), (target, source)):
            if step_id in steps and _kind(nodes[note_id]) in NOTE_HEADINGS:
                step_notes.setdefault(step_id, set()).add(note_id)
                attached.add(note_id)

    def notes(kind: str, ids) -> list[dict]:
        return sorted((nodes[i] for i in ids if _kind(nodes[i]) == kind), key=_position)

    def note_item(note: dict, level: int = 0) -> list[str]:
        """A note's list item, led by its tag so the generation can report on it."""
        return _list_item(f"{ref(note['id'])} {note['title'].strip()}", level)

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

    if steps:
        lines = ["Program Flow:"]
        for step in _flow_order(steps, successors):
            step_id = step["id"]
            label = NODE_KINDS[_kind(step)].label
            lines += _list_item(f"{ref(step_id)} {label}: {step['title'].strip()}")
            # The keywords below are plain lines indented under the step; only text items carry a dash.
            following = sorted((steps[i] for i in successors.get(step_id, ())), key=_position)
            targets = ", ".join(ref(i["id"]) for i in following) or "end"
            lines.append(f"  Next: {targets}")
            for kind, heading in NOTE_HEADINGS.items():
                own = notes(kind, step_notes.get(step_id, ()))
                if own:
                    lines.append(f"  {heading}")
                    for note in own:
                        lines += note_item(note, 2)
        sections.append(lines)

    add_loose_notes("implementation_note")

    return "\n".join("\n".join(lines) + "\n" for lines in sections)

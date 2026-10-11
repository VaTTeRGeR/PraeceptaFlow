import json
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QStandardPaths, QTimer, Signal

from editor.instructions import INSTRUCTIONS

GENERATE_PROMPT = (
    "Build the program described by the project description on stdin. When you are done, report one "
    "entry per note tag of the description (every note, also one whose code you did not change in "
    "this run), with the tag without its brackets. Its realization is one or two sentences saying "
    "what in the program realizes the note, naming the file and the step tag where that applies, or "
    "saying that it had no effect on the code and why."
)
# How each note was realized, so the editor can show it for the selected note.
NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tag": {"type": "string"}, "realization": {"type": "string"}},
                "required": ["tag", "realization"],
            },
        },
    },
    "required": ["notes"],
}
# Checking must not change files, so the tools that could are denied on top of not being allowed.
PARITY_PROMPT = (
    "Check that the source code in the current directory matches the project description on stdin. "
    "Do not change any file. Report one entry per step tag of the description, with the tag without "
    "its brackets; the tags of notes and of logical groups are not steps and get no entry. Its status is `unimplemented` if no code carries the tag; `match` if the code is "
    "delimited by its tag and end comments, does what the step and its notes say, and continues as "
    "its Next: links say; otherwise `mismatch`. The reason is one sentence saying what differs, "
    "empty for a match. Under `other` list code that implements no step and tags that are not in "
    "the description."
)
# The verdicts come back as the `structured_output` of the result event, so the editor can mark the boxes.
PARITY_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "status": {"type": "string", "enum": ["match", "mismatch", "unimplemented"]},
                    "reason": {"type": "string"},
                },
                "required": ["tag", "status", "reason"],
            },
        },
        "other": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["steps"],
}
# The instructions forbid running commands; denying the shell tools holds whatever the user's settings allow.
GENERATE_ARGS = [
    "--permission-mode", "acceptEdits",
    "--disallowedTools", "Bash,PowerShell",
    "--json-schema", json.dumps(NOTES_SCHEMA),
]
PARITY_ARGS = [
    "--allowedTools", "Read,Glob,Grep",
    "--disallowedTools", "Edit,Write,NotebookEdit,Bash,PowerShell",
    "--json-schema", json.dumps(PARITY_SCHEMA),
]


SETUP_TIMEOUT_MS = 30_000
LOGIN_TIMEOUT_MS = 5 * 60_000
# claude has no command that lists its models. The stream-json protocol's initialize request
# answers with them (and the effort levels each supports) without calling a model.
MODELS_REQUEST = {"type": "control_request", "request_id": "models", "request": {"subtype": "initialize"}}


class ClaudeSetup(QObject):
    """Checks that `claude` is installed and logged in, then asks it which models it offers."""

    ready = Signal(list)  # the model dicts claude reports: value, displayName, supportedEffortLevels, ...
    problem = Signal(str, bool)  # what is wrong, and whether logging in would fix it

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    def check(self) -> None:
        self.stop()
        if not QStandardPaths.findExecutable("claude"):
            self.problem.emit("Claude Code is not installed: the claude command was not found on PATH.", False)
            return
        self._start(["auth", "status", "--json"], self._on_auth_status, SETUP_TIMEOUT_MS)

    def login(self) -> None:
        """Run the interactive login (it opens the browser), then check again."""
        self.stop()
        self._start(["auth", "login"], self.check, LOGIN_TIMEOUT_MS)

    def stop(self) -> None:
        self._timer.stop()
        if self._process is not None:
            self._process.blockSignals(True)  # a killed process must not report back
            self._process.kill()
            self._process.waitForFinished(1000)
            self._process.deleteLater()
            self._process = None

    def _start(self, args: list[str], on_finished, timeout_ms: int, request: dict | None = None) -> None:
        process = self._process = QProcess(self)
        process.setWorkingDirectory(str(Path.home()))  # not a project folder, so no project settings
        process.finished.connect(lambda *_: on_finished())
        process.errorOccurred.connect(self._on_error)
        if request is not None:
            process.readyReadStandardOutput.connect(self._read_models)
        process.start("claude", args)
        if request is not None:
            process.write((json.dumps(request) + "\n").encode("utf-8"))
            process.closeWriteChannel()
        self._timer.start(timeout_ms)

    def _on_auth_status(self) -> None:
        # `claude auth status` exits non-zero when logged out, so the JSON decides, not the exit code.
        try:
            status = json.loads(bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace"))
        except ValueError:
            status = {}
        self.stop()
        if not status.get("loggedIn"):
            self.problem.emit("Claude Code is installed but you are not logged in.", True)
            return
        self._start(
            ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
            self._on_probe_finished, SETUP_TIMEOUT_MS, MODELS_REQUEST,
        )

    def _read_models(self) -> None:
        while self._process is not None and self._process.canReadLine():
            line = bytes(self._process.readLine()).decode("utf-8", "replace")
            try:
                event = json.loads(line)
            except ValueError:
                continue  # hook and plugin output share the stream
            reply = event.get("response", {}) if event.get("type") == "control_response" else {}
            if reply.get("request_id") != MODELS_REQUEST["request_id"]:
                continue
            models = reply.get("response", {}).get("models")
            self.stop()
            if isinstance(models, list) and models:
                self.ready.emit(models)
            else:
                self._no_models()
            return

    def _on_probe_finished(self) -> None:
        self._read_models()  # output that arrived together with the exit
        if self._process is not None:  # no reply was found
            self.stop()
            self._no_models()

    def _no_models(self) -> None:
        self.problem.emit("Could not read the list of models from claude; only its default can be used.", False)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self.stop()
            self.problem.emit("Could not start the claude command.", False)

    def _on_timeout(self) -> None:
        self.stop()
        self.problem.emit("Claude did not answer in time.", False)


class ClaudeRunner(QObject):
    """Runs `claude -p` in a directory and reports what it says and does as lines of text."""

    message = Signal(str)
    result = Signal(dict)  # the structured output of a run that was given a --json-schema
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stopped = False
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.errorOccurred.connect(self._on_error)
        self._process.finished.connect(self._on_finished)

    def is_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def start(self, prompt: str, stdin: str, cwd: Path, extra_args: list[str]) -> None:
        self._stopped = False
        self._process.setWorkingDirectory(str(cwd))
        args = [prompt, "-p", "--output-format", "stream-json", "--verbose",
                "--append-system-prompt", INSTRUCTIONS, *extra_args]
        self._process.start("claude", args)
        self._process.write(stdin.encode("utf-8"))
        self._process.closeWriteChannel()

    def stop(self) -> None:
        self._stopped = True
        self._process.kill()
        self._process.waitForFinished(3000)

    def _read(self) -> None:
        while self._process.canReadLine():
            self._handle(bytes(self._process.readLine()).decode("utf-8", "replace").strip())

    def _handle(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            if line:
                self.message.emit(line)  # not an event: plain output such as an error message
            return
        if event.get("type") == "system" and event.get("subtype") == "init":
            self.message.emit(f"Model: {event.get('model', 'unknown')}")
        elif event.get("type") == "assistant":
            for block in event["message"]["content"]:
                if block["type"] == "text":
                    self.message.emit(block["text"])
                elif block["type"] == "tool_use":
                    args = block["input"]
                    detail = args.get("file_path") or args.get("command") or args.get("pattern") or ""
                    self.message.emit(f"> {block['name']} {detail}")
        elif event.get("type") == "result" and event.get("is_error"):
            self.message.emit(f"Error: {event.get('result', '')}")
        elif event.get("type") == "result" and isinstance(event.get("structured_output"), dict):
            self.result.emit(event["structured_output"])

    def _on_error(self, error: QProcess.ProcessError) -> None:
        # `finished` is not emitted when the process never started.
        if error == QProcess.ProcessError.FailedToStart:
            self.message.emit("Could not start the claude command. Is it installed and on PATH?")
            self.finished.emit()

    def _on_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read()
        if self._stopped:
            self.message.emit("Stopped.")
        elif code or status != QProcess.ExitStatus.NormalExit:
            self.message.emit(f"Failed (exit code {code}).")
        else:
            self.message.emit("Finished.")
        self.finished.emit()

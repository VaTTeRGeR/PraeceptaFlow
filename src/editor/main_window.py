import html
import json
import os
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QSettings, Qt, QTimer, QUrl, qVersion
from PySide6.QtGui import QAction, QActionGroup, QCursor, QDesktopServices, QKeySequence, QUndoStack
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from editor.claude_runner import (
    GENERATE_ARGS,
    GENERATE_PROMPT,
    PARITY_ARGS,
    PARITY_PROMPT,
    ClaudeRunner,
    ClaudeSetup,
)
from editor.commands import AddItemCommand, DeleteItemsCommand
from editor.code_view import CodeView
from editor.export import (
    NOTE_HEADINGS,
    PROJECT_NOTE_KIND,
    first_line,
    issues_section,
    project_description,
    tagged_items,
)
from editor.items import NODE_KINDS, PARITY_MARKS, NodeItem, RegionItem, group_chain, make_node
from editor.scene import EditorScene
from editor.source_map import scan
from editor.view import EditorView

APP_NAME = "PraeceptaFlow"
ORG_NAME = "PraeceptaFlow"
FILE_SUFFIX = ".pfg"
FILE_FILTER = f"Project flow graphs (*{FILE_SUFFIX});;All files (*)"
EXPORT_SUFFIX = ".pd"
# How the last generation realized each note, as JSON {tag: text} beside the graph (source_map skips it).
NOTES_SUFFIX = ".notes"
# The report of the last parity check, as JSON beside the graph: {"steps": {tag: {status, reason, step}},
# "other": [...]}. `step` is the step's item of the description that was checked; a verdict is only shown
# while the graph still yields the same item, which is how a later change to the node hides it.
ISSUES_SUFFIX = ".issues"
NOTE_KINDS = (PROJECT_NOTE_KIND, *NOTE_HEADINGS)
RECENT_KEY = "recentFiles"
MAX_RECENT = 10


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        # Per-user settings (the registry on Windows); tests can pass a throwaway QSettings.
        self._settings = settings or QSettings(ORG_NAME, APP_NAME)
        self._path: Path | None = None
        self._run_path: Path | None = None  # the graph the current claude run was started on
        self._run_text = ""  # the description that run was given
        self._has_code = False  # whether the last scan found tagged source code
        self._has_notes = False  # whether the graph had a notes file at the last scan
        self._text_dirty = False
        self._insert_count = 0
        self._insert_pos: QPointF | None = None  # set while the canvas context menu is open
        self._insert_menu_open = False

        self._undo_stack = QUndoStack(self)
        self._scene = EditorScene(self._undo_stack, self)
        self._view = EditorView(self._scene, self)
        # The way back out of a group: the groups around the canvas shown, as links, over its top left corner.
        self._crumbs = QLabel(self._view)
        self._crumbs.setStyleSheet("background: #ffffff; border: 1px solid #d9dee4; padding: 3px 6px;")
        self._crumbs.move(8, 8)
        self._crumbs.linkActivated.connect(self._enter_group_id)
        self._crumbs.hide()
        self._code_view = CodeView(self)
        self._code_view_sized = False
        # What the last generation did with the selected note, above the code.
        self._note_label = QLabel(self)
        self._note_label.setWordWrap(True)
        self._note_label.setMargin(6)
        self._note_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._note_label.setTextFormat(Qt.TextFormat.PlainText)
        self._note_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._note_label.hide()
        self._code_pane = QWidget(self)
        layout = QVBoxLayout(self._code_pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._note_label)
        layout.addWidget(self._code_view, 1)
        self._code_pane.hide()  # shown beside the canvas while there is code or note feedback to show
        self._splitter = QSplitter(self)
        self._splitter.addWidget(self._view)
        self._splitter.addWidget(self._code_pane)
        self._splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self._splitter)
        self.resize(1200, 800)

        self._claude = ClaudeRunner(self)
        self._claude_log = QPlainTextEdit(readOnly=True)
        self._claude_dock = QDockWidget("Claude", self)
        self._claude_dock.setWidget(self._claude_log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._claude_dock)
        self._claude_dock.hide()
        self._claude.message.connect(self._claude_log.appendPlainText)
        self._claude.finished.connect(lambda: self._set_claude_busy(False))
        self._claude.finished.connect(self._update_code_view)
        self._claude.result.connect(self._show_result)
        # A verdict is about a step as it was checked, so an edit can hide (and an undo bring back) its badge.
        self._undo_stack.indexChanged.connect(self._show_issues)
        self._scene.text_edited.connect(self._show_issues)

        # The model and effort passed to claude; None means "Default": no flag, so claude's own
        # settings decide. The choices come from claude itself (ClaudeSetup) once it has answered.
        self._model: str | None = None
        self._effort: str | None = None
        self._models: dict[str, dict] = {}
        self._claude_setup = ClaudeSetup(self)
        self._claude_setup.ready.connect(self._show_models)
        self._claude_setup.problem.connect(self._show_claude_problem)

        self._create_menus()

        hint = "Drag from a box's border to connect  |  Double-click to edit text (Enter: new line, Ctrl+Enter: done) or enter a group  |  Ctrl+wheel zooms  |  Middle-drag pans"
        self.statusBar().addWidget(QLabel(hint))
        self._zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self._zoom_label)
        self._view.zoom_changed.connect(self._show_zoom)
        self._view.canvas_context_menu.connect(self._show_canvas_menu)
        self._show_zoom(self._view.zoom())

        self._undo_stack.cleanChanged.connect(self._update_modified)
        self._scene.text_edited.connect(self._on_text_edited)
        self._scene.selectionChanged.connect(self._show_selection)
        self._scene.text_edited.connect(self._update_crumbs)
        self._scene.group_changed.connect(self._update_crumbs)
        self._scene.group_changed.connect(self._view.fit_all)
        self._update_title()

    # --- menus -----------------------------------------------------------

    def _add_action(self, menu, text: str, slot, *shortcuts) -> QAction:
        action = QAction(text, self)
        if shortcuts:
            action.setShortcuts([QKeySequence(shortcut) for shortcut in shortcuts])
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self._add_action(file_menu, "&New", self._new, QKeySequence.StandardKey.New)
        self._add_action(file_menu, "&Open...", self._open, QKeySequence.StandardKey.Open)
        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._update_recent_menu()
        file_menu.addSeparator()
        self._add_action(file_menu, "&Save", self._save, QKeySequence.StandardKey.Save)
        self._add_action(file_menu, "Save &As...", self._save_as, "Ctrl+Shift+S")
        file_menu.addSeparator()
        self._add_action(file_menu, "E&xit", self.close, "Ctrl+Q")

        edit_menu = self.menuBar().addMenu("&Edit")
        undo_action = self._undo_stack.createUndoAction(self, "&Undo")
        undo_action.setShortcuts(QKeySequence.StandardKey.Undo)
        redo_action = self._undo_stack.createRedoAction(self, "&Redo")
        redo_action.setShortcuts(QKeySequence.StandardKey.Redo)
        edit_menu.addActions([undo_action, redo_action])
        edit_menu.addSeparator()
        self._add_action(edit_menu, "&Delete", self._delete_selected, QKeySequence.StandardKey.Delete)
        self._add_action(edit_menu, "Select &All", self._select_all, QKeySequence.StandardKey.SelectAll)

        insert_menu = self.menuBar().addMenu("&Insert")
        self._insert_menu = insert_menu
        insertable = [(key, kind) for key, kind in NODE_KINDS.items() if kind.insert]
        for number, (key, kind) in enumerate(insertable, start=1):
            insert = lambda _checked=False, key=key: self._insert_box(key)
            self._add_action(insert_menu, f"&{number} {kind.label}", insert, f"Ctrl+{number}")
        insert = lambda _checked=False: self._insert(RegionItem(), "Add Logical Group")
        self._add_action(insert_menu, "&Logical Group", insert)
        # A pick from the menu bar is not a pointer-on-canvas insert. The menu hides before
        # the action fires, so the flag is only cleared once that event has been handled.
        insert_menu.aboutToShow.connect(lambda: setattr(self, "_insert_menu_open", True))
        insert_menu.aboutToHide.connect(
            lambda: QTimer.singleShot(0, lambda: setattr(self, "_insert_menu_open", False))
        )

        view_menu = self.menuBar().addMenu("&View")
        self._add_action(view_menu, "Zoom &In", self._view.zoom_in, "Ctrl++", "Ctrl+=")
        self._add_action(view_menu, "Zoom &Out", self._view.zoom_out, "Ctrl+-")
        self._add_action(view_menu, "&Reset Zoom", self._view.reset_zoom, "Ctrl+0")
        self._add_action(view_menu, "&Fit All", self._view.fit_all, "Ctrl+Shift+F")
        self._add_action(view_menu, "&Leave Group", self._leave_group, "Alt+Up")
        view_menu.addSeparator()
        self._code_action = self._add_action(view_menu, "&Source Code", self._update_code_view)
        self._code_action.setCheckable(True)
        self._code_action.setChecked(True)
        view_menu.addAction(self._claude_dock.toggleViewAction())

        source_menu = self.menuBar().addMenu("&Source Generation")
        self._generate_action = self._add_action(
            source_menu, "&Generate Source Code", self._generate, "Ctrl+G"
        )
        self._parity_action = self._add_action(source_menu, "Check &Parity", self._check_parity, "Ctrl+Shift+G")
        self._stop_action = self._add_action(source_menu, "&Stop", self._claude.stop)
        self._stop_action.setEnabled(False)
        source_menu.addSeparator()
        self._model_menu = source_menu.addMenu("&Model")
        self._model_group = QActionGroup(self)
        self._model_group.triggered.connect(self._select_model)
        self._effort_menu = source_menu.addMenu("&Effort")
        self._effort_group = QActionGroup(self)
        self._effort_group.triggered.connect(self._select_effort)
        self._show_models([])

        help_menu = self.menuBar().addMenu("&Help")
        self._add_action(help_menu, "&About", self._about)
        self._add_action(help_menu, "About &Qt", QApplication.aboutQt)

    # --- recent files ----------------------------------------------------

    def _recent_files(self) -> list[str]:
        value = self._settings.value(RECENT_KEY, [])
        if isinstance(value, str):  # some QSettings backends return a one-element list as a string
            value = [value]
        return [path for path in (value or []) if path]

    def _set_recent_files(self, files: list[str]) -> None:
        self._settings.setValue(RECENT_KEY, files[:MAX_RECENT])
        self._update_recent_menu()

    def _add_recent(self, path: Path) -> None:
        new = str(path.resolve())
        others = [f for f in self._recent_files() if os.path.normcase(f) != os.path.normcase(new)]
        self._set_recent_files([new] + others)

    def _remove_recent(self, path: Path) -> None:
        gone = os.path.normcase(str(path.resolve()))
        self._set_recent_files([f for f in self._recent_files() if os.path.normcase(f) != gone])

    def _update_recent_menu(self) -> None:
        menu = self._recent_menu
        menu.clear()
        files = self._recent_files()[:MAX_RECENT]
        for number, file in enumerate(files, start=1):
            label = Path(file).name.replace("&", "&&")
            action = menu.addAction(f"&{number % 10} {label}")
            action.setStatusTip(file)
            action.triggered.connect(lambda _checked=False, file=file: self._open_recent(file))
        if files:
            menu.addSeparator()
            menu.addAction("&Clear List").triggered.connect(lambda: self._set_recent_files([]))
        menu.setEnabled(bool(files))

    def _open_recent(self, path: str) -> None:
        if self._maybe_save():
            self.open_file(path)

    # --- document state --------------------------------------------------

    def _update_title(self) -> None:
        name = self._path.name if self._path else "Untitled"
        self.setWindowTitle(f"{name}[*] - {APP_NAME}")

    def _update_modified(self, *_) -> None:
        self.setWindowModified(self._text_dirty or not self._undo_stack.isClean())

    def _on_text_edited(self) -> None:
        # Text edits bypass the undo stack, so they are tracked separately.
        self._text_dirty = True
        self._update_modified()

    def _mark_clean(self, path: Path | None) -> None:
        self._path = path
        self._text_dirty = False
        self._undo_stack.setClean()
        self._update_modified()
        self._update_title()
        self._update_code_view()

    def _maybe_save(self) -> bool:
        """Offer to save unsaved changes; False means the user cancelled."""
        self._scene.setFocusItem(None)  # commit a text edit in progress
        if not self.isWindowModified():
            return True
        name = self._path.name if self._path else "Untitled"
        buttons = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Save changes to {name}?",
            buttons.Save | buttons.Discard | buttons.Cancel,
            buttons.Save,
        )
        if answer == buttons.Save:
            return self._save()
        return answer == buttons.Discard

    def closeEvent(self, event) -> None:
        # Save first: cancelling here must not have stopped claude already.
        if not self._maybe_save():
            event.ignore()
            return
        if self._claude.is_running():
            answer = QMessageBox.question(self, "Claude is running", "Stop Claude and close the editor?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._claude.stop()
        self._claude_setup.stop()
        # Still modified means the changes were discarded, so the description already matches the file.
        if self._path and not self.isWindowModified():
            self._export_beside(self._path)
        event.accept()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # The source files may have been edited in another program meanwhile.
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._update_code_view()

    # --- file actions ----------------------------------------------------

    def _new(self) -> None:
        if not self._maybe_save():
            return
        self._scene.reset()
        self._mark_clean(None)
        self._view.fit_all()

    def _open(self) -> None:
        if not self._maybe_save():
            return
        directory = str(self._path.parent) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open flow graph", directory, FILE_FILTER)
        if path:
            self.open_file(path)

    def open_file(self, path: str | Path) -> bool:
        path = Path(path)
        try:
            self._scene.load_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            self._show_missing_file(path)
            self._remove_recent(path)
            return False
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Open failed", f"Could not open {path}:\n{exc}")
            self._remove_recent(path)
            return False
        self._mark_clean(path)
        self._add_recent(path)
        self._view.fit_all()
        self._export_beside(path)
        return True

    def _show_missing_file(self, path: Path) -> None:
        message = f"{path.as_posix()} does not exist, maybe it was moved, renamed or deleted."
        box = QMessageBox(QMessageBox.Icon.Critical, "Open failed", message, QMessageBox.StandardButton.Close, self)
        folder = next((parent for parent in path.absolute().parents if parent.is_dir()), None)
        open_folder = box.addButton("Show in File-Explorer", QMessageBox.ButtonRole.ActionRole) if folder else None
        box.exec()
        if open_folder is not None and box.clickedButton() is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _save(self) -> bool:
        if self._path is None:
            return self._save_as()
        return self.save_file(self._path)

    def _save_as(self) -> bool:
        directory = str(self._path) if self._path else f"untitled{FILE_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self, "Save flow graph", directory, FILE_FILTER)
        if not path:
            return False
        if not Path(path).suffix:
            path += FILE_SUFFIX
        return self.save_file(path)

    def save_file(self, path: str | Path) -> bool:
        path = Path(path)
        self._scene.setFocusItem(None)  # commit a text edit in progress
        try:
            # Written beside the graph and swapped in, so a failed write leaves the old file intact.
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(self._scene.to_dict(), indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", f"Could not save {path}:\n{exc}")
            return False
        self._mark_clean(path)
        self._add_recent(path)
        self._export_beside(path)
        return True

    def _export_beside(self, path: Path) -> None:
        """Rewrite the description next to the graph; a failure is reported but never fails the caller."""
        if path.suffix.lower() == EXPORT_SUFFIX:
            return
        target = path.with_suffix(EXPORT_SUFFIX)
        try:
            target.write_text(project_description(self._scene.to_dict()), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export {target}:\n{exc}")

    # --- source code generation ------------------------------------------

    def start_claude_check(self) -> None:
        """Check that claude is installed and logged in and fetch its models (called once at startup)."""
        self._claude_setup.check()

    def _show_claude_problem(self, text: str, can_log_in: bool) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, "Claude Code", text, QMessageBox.StandardButton.Close, self)
        login = box.addButton("Log in", QMessageBox.ButtonRole.AcceptRole) if can_log_in else None
        box.exec()
        if login is not None and box.clickedButton() is login:
            self._claude_setup.login()

    def _add_choice(self, menu, group, text: str, value: str | None, tip: str = "") -> QAction:
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setData(value)
        action.setStatusTip(tip)
        group.addAction(action)
        return action

    def _show_models(self, models: list) -> None:
        """Fill the Model menu from claude's list, starting on its default (which passes no --model)."""
        self._models = {m["value"]: m for m in models if isinstance(m, dict) and m.get("value")}
        default = self._models.get("default", {})
        resolved = default.get("resolvedModel")
        self._model_menu.clear()
        first = self._add_choice(
            self._model_menu, self._model_group,
            f"Default ({resolved})" if resolved else "Default", None, default.get("description", ""),
        )
        first.setChecked(True)
        for value, model in self._models.items():
            if value != "default":
                self._add_choice(
                    self._model_menu, self._model_group,
                    model.get("displayName", value), value, model.get("description", ""),
                )
        self._model = None
        self._show_efforts()

    def _show_efforts(self) -> None:
        """Fill the Effort menu with the levels the selected model supports."""
        model = self._models.get(self._model or "default", {})
        levels = model.get("supportedEffortLevels", []) if model.get("supportsEffort") else []
        if self._effort not in levels:
            self._effort = None
        self._effort_menu.clear()
        self._add_choice(self._effort_menu, self._effort_group, "Default", None)
        for level in levels:
            self._add_choice(self._effort_menu, self._effort_group, level.capitalize(), level)
        for action in self._effort_group.actions():
            action.setChecked(action.data() == self._effort)
        self._effort_menu.setEnabled(bool(levels))

    def _select_model(self, action: QAction) -> None:
        self._model = action.data()
        self._show_efforts()

    def _select_effort(self, action: QAction) -> None:
        self._effort = action.data()

    def _model_args(self) -> list[str]:
        args = ["--model", self._model] if self._model else []
        return args + (["--effort", self._effort] if self._effort else [])

    def _generate(self) -> None:
        self._run_claude("Generate source code", GENERATE_PROMPT, GENERATE_ARGS, generate=True)

    def _check_parity(self) -> None:
        self._run_claude("Check parity", PARITY_PROMPT, PARITY_ARGS)

    def _run_claude(self, title: str, prompt: str, args: list[str], generate: bool = False) -> None:
        """Run claude on the project description of the saved graph, in the graph's folder."""
        self._scene.setFocusItem(None)  # commit a text edit in progress
        # Saved without asking: the run must be about the graph on the canvas, so there is nothing to discard.
        if (self._path is None or self.isWindowModified()) and not self._save():
            return
        description = self._path.with_suffix(EXPORT_SUFFIX)
        others = [
            p.name for p in description.parent.glob(f"*{FILE_SUFFIX}")
            if os.path.normcase(p.name) != os.path.normcase(self._path.name)
        ]
        if generate and others:
            # A generation removes the code of tags it does not know, which is the other graphs' code.
            question = (
                f"This folder also holds {', '.join(others)}. Generating can remove the source code "
                "of other graphs in the same folder. Continue?"
            )
            if QMessageBox.question(self, title, question) != QMessageBox.StandardButton.Yes:
                return
        text = project_description(self._scene.to_dict())
        self._run_path = self._path
        self._run_text = text
        self._claude_log.setPlainText(f"{title}: {description}")
        self._claude_dock.show()
        self._set_claude_busy(True)
        # A generation is also told what the last check found wrong, so it can fix that.
        issues = issues_section(*self._issues()) if generate else ""
        self._claude.start(prompt, text + issues, description.parent, [*args, *self._model_args()])

    def _nodes(self) -> list[NodeItem]:
        """The boxes of all canvases; not the cases, which are part of their decision."""
        return [item for item in self._scene.items() if isinstance(item, NodeItem) and item.decision is None]

    def _issues(self) -> tuple[dict[str, dict], list[str]]:
        """The verdicts of the last parity check for the steps that did not change since, and its other findings."""
        if self._path is None:
            return {}, []
        try:
            report = json.loads(self._path.with_suffix(ISSUES_SUFFIX).read_text(encoding="utf-8"))
        except (OSError, ValueError):  # no file, or not JSON
            return {}, []
        if not isinstance(report, dict):
            return {}, []
        steps, other = report.get("steps"), report.get("other")
        current = tagged_items(project_description(self._scene.to_dict()))
        # The file may have been edited by hand: only an entry with a known status counts.
        shown = {
            tag: {"status": entry["status"], "reason": str(entry.get("reason", ""))}
            for tag, entry in (steps.items() if isinstance(steps, dict) else ())
            if isinstance(entry, dict)
            and isinstance(entry.get("status"), str)
            and entry["status"] in PARITY_MARKS
            and tag in current
            and entry.get("step") == current[tag]
        }
        return shown, [str(line) for line in other] if isinstance(other, list) else []

    def _show_issues(self, *_) -> None:
        """Badge each box with its verdict of the last parity check, unless its step changed since then."""
        shown, _ = self._issues()
        for node in self._nodes():
            # The same rule that maps tag comments to nodes.
            entry = next((e for tag, e in shown.items() if tag and node.id.startswith(tag)), {})
            node.set_parity(entry.get("status"), entry.get("reason", ""))

    def _show_result(self, result: dict) -> None:
        """The structured output of a run: note feedback from a generation, verdicts from a parity check."""
        if "notes" in result:
            self._save_notes(result)
        if "steps" in result:
            self._show_parity(result)

    def _save_notes(self, result: dict) -> None:
        """Keep how each note was realized beside the graph, replacing the last generation's."""
        notes = {
            str(entry.get("tag", "")).strip("[]"): str(entry.get("realization", ""))
            for entry in result["notes"] if isinstance(entry, dict) and entry.get("tag")
        }
        target = self._run_path.with_suffix(NOTES_SUFFIX)
        try:
            target.write_text(json.dumps(notes, indent=2), encoding="utf-8")
            # The code was rewritten, so the last parity check no longer describes it.
            self._run_path.with_suffix(ISSUES_SUFFIX).unlink(missing_ok=True)
        except OSError as exc:
            # Either file may have failed: the notes, or the issues file that was to go.
            self._claude_log.appendPlainText(f"Could not write {exc.filename or target}: {exc.strerror}")
            return
        self._claude_log.appendPlainText(f"{len(notes)} notes reported.")

    def _note_feedback(self, node: NodeItem) -> str:
        """What the last generation reported for a note node."""
        try:
            notes = json.loads(self._path.with_suffix(NOTES_SUFFIX).read_text(encoding="utf-8"))
            # The same rule that maps tag comments to nodes.
            return next(text for tag, text in notes.items() if tag and node.id.startswith(tag))
        except (AttributeError, OSError, ValueError, StopIteration):  # unsaved graph, no or broken file, no entry
            return "No feedback from the last generation."

    def _show_parity(self, result: dict) -> None:
        """List the verdicts in the log and keep them beside the graph that was checked, then mark the boxes."""
        counts: dict[str, int] = {}
        checked = tagged_items(self._run_text)
        steps = {}
        for entry in result.get("steps", []):
            tag, status, reason = str(entry.get("tag", "")).strip("[]"), entry.get("status"), entry.get("reason", "")
            if not tag or status not in PARITY_MARKS:
                continue
            counts[status] = counts.get(status, 0) + 1
            if tag in checked:
                steps[tag] = {"status": status, "reason": reason or status.capitalize(), "step": checked[tag]}
            self._claude_log.appendPlainText(f"{PARITY_MARKS[status][0]} [{tag}] {reason}".rstrip())
        other = [str(line) for line in result.get("other", [])]
        for line in other:
            self._claude_log.appendPlainText(f"! {line}")
        self._claude_log.appendPlainText(", ".join(f"{n} {status}" for status, n in counts.items()) or "No steps reported.")
        target = self._run_path.with_suffix(ISSUES_SUFFIX)
        try:
            target.write_text(json.dumps({"steps": steps, "other": other}, indent=2), encoding="utf-8")
        except OSError as exc:
            self._claude_log.appendPlainText(f"Could not write {target}: {exc}")
        self._show_issues()

    def _set_claude_busy(self, busy: bool) -> None:
        self._generate_action.setEnabled(not busy)
        self._parity_action.setEnabled(not busy)
        self._stop_action.setEnabled(busy)

    def _update_code_view(self, *_) -> None:
        """Read the folder's source code again and show it; the pane stays open while there is any."""
        folder = self._path.parent if self._path else None
        files = scan(folder) if folder else []
        self._code_view.set_files(folder, files)
        self._has_code = bool(files)
        self._has_notes = bool(self._path) and self._path.with_suffix(NOTES_SUFFIX).is_file()
        self._show_selection()
        self._show_issues()

    def _show_selection(self) -> None:
        """Mark and scroll to the selected node's code, or show a note's feedback; reads no source files."""
        items = self._scene.selectedItems()
        item = items[0] if len(items) == 1 and isinstance(items[0], (NodeItem, RegionItem)) else None
        self._code_view.highlight(item.id if item else None)
        node = item if isinstance(item, NodeItem) else None
        # Open or closed by what the last generation left, never by the selection: that would shift the canvas.
        shown = self._has_code or self._has_notes
        note = self._note_feedback(node) if shown and node and node.kind_key in NOTE_KINDS else ""
        self._note_label.setText(note)
        self._note_label.setVisible(bool(note))
        self._code_view.setVisible(self._has_code)
        if not shown or not self._code_action.isChecked():
            self._code_pane.hide()
        elif self._code_pane.isHidden():
            self._code_pane.show()
            if not self._code_view_sized:  # afterwards the splitter keeps the width the user chose
                half = self._splitter.width() // 2
                self._splitter.setSizes([half, half])
                self._code_view_sized = True

    # --- edit / insert actions -------------------------------------------

    def _delete_selected(self) -> None:
        if self._scene.is_editing_text():
            return
        items = self._scene.selectedItems()
        if items:
            self._undo_stack.push(DeleteItemsCommand(self._scene, items))

    def _select_all(self) -> None:
        for item in self._scene.items():
            if item.isVisible():  # the other canvases are in the scene too
                item.setSelected(True)

    # --- groups ----------------------------------------------------------

    def _leave_group(self) -> None:
        if self._scene.current_group is not None:
            self._scene.enter_group(self._scene.current_group.group)

    def _enter_group_id(self, group_id: str) -> None:
        """A link of the breadcrumb: a group's id, or anything else for the main canvas."""
        self._scene.enter_group(next((node for node in self._nodes() if node.id == group_id), None))

    def _update_crumbs(self) -> None:
        chain = group_chain(self._scene.current_group)
        names = [html.escape(first_line(node.title) or "Group") for node in chain]
        links = ['<a href="main">Main</a>', *(f'<a href="{node.id}">{name}</a>' for node, name in zip(chain, names))]
        self._crumbs.setText(" › ".join([*links[:-1], f"<b>{names[-1]}</b>"]) if chain else "")
        self._crumbs.adjustSize()
        self._crumbs.setVisible(bool(chain))

    def _show_canvas_menu(self, global_pos, scene_pos: QPointF) -> None:
        # The context menu is the Insert menu; its actions read the click position from here.
        self._insert_pos = scene_pos
        try:
            self._insert_menu.exec(global_pos)
        finally:
            self._insert_pos = None

    def _pointer_scene_pos(self) -> QPointF | None:
        """Scene position of the mouse pointer while it is over the canvas, else None."""
        if self._insert_menu_open:
            return None  # the pointer is on the menu, not on the canvas it happens to cover
        global_pos = QCursor.pos()
        if QApplication.widgetAt(global_pos) is not self._view.viewport():
            return None
        return self._view.mapToScene(self._view.viewport().mapFromGlobal(global_pos))

    def _insert(self, item, text: str) -> None:
        grid = self._scene.GRID_SIZE
        pos = self._insert_pos if self._insert_pos is not None else self._pointer_scene_pos()
        if pos is None:
            # No pointer on the canvas: use the view centre and stagger successive inserts
            # so they do not land exactly on top of each other.
            offset = grid * (self._insert_count % 10)
            self._insert_count += 1
            centre = self._view.mapToScene(self._view.viewport().rect().center())
            pos = QPointF(centre.x() + offset, centre.y() + offset)
        item.setPos(QPointF(round(pos.x() / grid) * grid, round(pos.y() / grid) * grid))
        item.group = self._scene.current_group
        self._undo_stack.push(AddItemCommand(self._scene, item, text))
        self._scene.clearSelection()
        item.setSelected(True)

    def _insert_box(self, kind: str) -> None:
        self._insert(make_node(kind), f"Add {NODE_KINDS[kind].label}")

    # --- misc ------------------------------------------------------------

    def _show_zoom(self, zoom: float) -> None:
        self._zoom_label.setText(f"{zoom:.0%}")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b><p>Prototype flow-graph editor.</p>"
            f"<p>Built with PySide6 (Qt {qVersion()}), used under the LGPLv3.</p>",
        )

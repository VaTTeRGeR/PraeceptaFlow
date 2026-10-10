import json
import os
from pathlib import Path

from PySide6.QtCore import QPointF, QSettings, Qt, QTimer, QUrl, qVersion
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
from editor.export import NOTE_HEADINGS, PROJECT_NOTE_KIND, project_description
from editor.items import NODE_KINDS, PARITY_MARKS, NodeItem
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
NOTE_KINDS = (PROJECT_NOTE_KIND, *NOTE_HEADINGS)
RECENT_KEY = "recentFiles"
MAX_RECENT = 10


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None):
        super().__init__()
        # Per-user settings (the registry on Windows); tests can pass a throwaway QSettings.
        self._settings = settings or QSettings(ORG_NAME, APP_NAME)
        self._path: Path | None = None
        self._text_dirty = False
        self._insert_count = 0
        self._insert_pos: QPointF | None = None  # set while the canvas context menu is open
        self._insert_menu_open = False

        self._undo_stack = QUndoStack(self)
        self._scene = EditorScene(self._undo_stack, self)
        self._view = EditorView(self._scene, self)
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
        # A verdict is about the graph and code as they were checked, so any edit removes the badges.
        self._undo_stack.indexChanged.connect(self._clear_parity)
        self._scene.text_edited.connect(self._clear_parity)

        # The model and effort passed to claude; None means "Default": no flag, so claude's own
        # settings decide. The choices come from claude itself (ClaudeSetup) once it has answered.
        self._model: str | None = None
        self._effort: str | None = None
        self._models: dict[str, dict] = {}
        self._claude_setup = ClaudeSetup(self)
        self._claude_setup.ready.connect(self._show_models)
        self._claude_setup.problem.connect(self._show_claude_problem)

        self._create_menus()

        hint = "Drag from a box's border to connect  |  Double-click to edit text (Enter: new line, Ctrl+Enter: done)  |  Ctrl+wheel zooms  |  Middle-drag pans"
        self.statusBar().addWidget(QLabel(hint))
        self._zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self._zoom_label)
        self._view.zoom_changed.connect(self._show_zoom)
        self._view.canvas_context_menu.connect(self._show_canvas_menu)
        self._show_zoom(self._view.zoom())

        self._undo_stack.cleanChanged.connect(self._update_modified)
        self._scene.text_edited.connect(self._on_text_edited)
        self._scene.selectionChanged.connect(self._update_code_view)
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
        for number, (key, kind) in enumerate(NODE_KINDS.items(), start=1):
            insert = lambda _checked=False, key=key: self._insert_box(key)
            self._add_action(insert_menu, f"&{number} {kind.label}", insert, f"Ctrl+{number}")
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
        if self._claude.is_running():
            answer = QMessageBox.question(self, "Claude is running", "Stop Claude and close the editor?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._claude.stop()
        self._claude_setup.stop()
        if self._maybe_save():
            # Still modified means the changes were discarded, so the description already matches the file.
            if self._path and not self.isWindowModified():
                self._export_beside(self._path)
            event.accept()
        else:
            event.ignore()

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
            path.write_text(json.dumps(self._scene.to_dict(), indent=2), encoding="utf-8")
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
        self._run_claude("Generate source code", GENERATE_PROMPT, GENERATE_ARGS)

    def _check_parity(self) -> None:
        self._run_claude("Check parity", PARITY_PROMPT, PARITY_ARGS)

    def _run_claude(self, title: str, prompt: str, args: list[str]) -> None:
        """Run claude on the project description beside the saved graph, in that folder."""
        if not self._maybe_save():  # also rewrites the description when there were changes
            return
        if self._path is None:
            QMessageBox.information(self, title, "Save the flow graph first.")
            return
        description = self._path.with_suffix(EXPORT_SUFFIX)
        try:
            text = description.read_text(encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, title, f"Could not read {description}:\n{exc}")
            return
        self._clear_parity()
        self._claude_log.setPlainText(f"{title}: {description}")
        self._claude_dock.show()
        self._set_claude_busy(True)
        self._claude.start(prompt, text, description.parent, [*args, *self._model_args()])

    def _nodes(self) -> list[NodeItem]:
        return [item for item in self._scene.items() if isinstance(item, NodeItem)]

    def _clear_parity(self, *_) -> None:
        for node in self._nodes():
            node.set_parity(None)

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
        target = self._path.with_suffix(NOTES_SUFFIX)
        try:
            target.write_text(json.dumps(notes, indent=2), encoding="utf-8")
        except OSError as exc:
            self._claude_log.appendPlainText(f"Could not write {target}: {exc}")
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
        """Mark each step's box with its verdict and list the verdicts in the log."""
        counts: dict[str, int] = {}
        for entry in result.get("steps", []):
            tag, status, reason = str(entry.get("tag", "")).strip("[]"), entry.get("status"), entry.get("reason", "")
            if not tag or status not in PARITY_MARKS:
                continue
            counts[status] = counts.get(status, 0) + 1
            for node in self._nodes():
                if node.id.startswith(tag):  # the same rule that maps tag comments to nodes
                    node.set_parity(status, reason or status.capitalize())
            self._claude_log.appendPlainText(f"{PARITY_MARKS[status][0]} [{tag}] {reason}".rstrip())
        for line in result.get("other", []):
            self._claude_log.appendPlainText(f"! {line}")
        self._claude_log.appendPlainText(", ".join(f"{n} {status}" for status, n in counts.items()) or "No steps reported.")

    def _set_claude_busy(self, busy: bool) -> None:
        self._generate_action.setEnabled(not busy)
        self._parity_action.setEnabled(not busy)
        self._stop_action.setEnabled(busy)

    def _update_code_view(self, *_) -> None:
        """Keep the code pane in step with the folder and the selected node.

        The pane stays open while there is tagged source code; selecting a node only marks and scrolls to its code.
        """
        folder = self._path.parent if self._path else None
        files = scan(folder) if folder else []
        self._code_view.set_files(folder, files)
        items = self._scene.selectedItems()
        node = items[0] if len(items) == 1 and isinstance(items[0], NodeItem) else None
        self._code_view.highlight(node.id if node else None)
        note = self._note_feedback(node) if node and node.kind_key in NOTE_KINDS else ""
        self._note_label.setText(note)
        self._note_label.setVisible(bool(note))
        self._code_view.setVisible(bool(files))
        if not (files or note) or not self._code_action.isChecked():
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
            item.setSelected(True)

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
        self._undo_stack.push(AddItemCommand(self._scene, item, text))
        self._scene.clearSelection()
        item.setSelected(True)

    def _insert_box(self, kind: str) -> None:
        self._insert(NodeItem("Box", kind=kind), f"Add {NODE_KINDS[kind].label}")

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

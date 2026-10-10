from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QTextEdit
from pygments.lexers import get_lexer_for_filename
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound

CONTEXT_LINES = 3  # lines kept above a section when the view scrolls to it
_STYLE = get_style_by_name("default")


def _token_rows(path: Path, lines: list[str]) -> list[list[tuple]] | None:
    """(token type, text) pieces for each line, lexed as one file so multi-line strings stay correct."""
    try:
        lexer = get_lexer_for_filename(path.name, stripnl=False)
    except ClassNotFound:
        return None
    rows: list[list[tuple]] = [[]]
    for token, value in lexer.get_tokens("\n".join(lines)):
        for number, part in enumerate(value.split("\n")):
            if number:
                rows.append([])
            if part:
                rows[-1].append((token, part))
    return rows


class CodeView(QPlainTextEdit):
    """Read-only listing of the source files; the code of the selected node is tinted, the rest greyed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        palette = self.palette()
        self._grey = palette.color(QPalette.ColorRole.PlaceholderText)
        self._tint = QColor(palette.color(QPalette.ColorRole.Highlight))
        self._tint.setAlpha(40)
        self._token_formats: dict = {}
        self._shown: list = []  # (path, lines) of what the document was built from
        self._file_blocks: list[tuple[int, int]] = []  # first and last block of each file's code
        self._file_names: list[str] = []  # shown path of each file, parallel to _file_blocks
        # Path of the file at the top of the view, pinned above the text so it stays while scrolling.
        self._header = QLabel(self)
        font = self.font()
        font.setBold(True)
        self._header.setFont(font)
        self._header.setIndent(int(self.document().documentMargin()))
        self._header.setFixedHeight(self._header.fontMetrics().lineSpacing() + 4)
        self.setViewportMargins(0, self._header.height(), 0, 0)
        self.verticalScrollBar().valueChanged.connect(self._update_header)
        self._sections: list[tuple[int, int, str]] = []  # first and last block, tag; in document order

    def set_files(self, root: Path, files: list[tuple[Path, list[str], list[tuple[str, int, int]]]]) -> None:
        """Show whole files as returned by source_map.scan; paths are shown relative to root.

        Nothing happens when the text is the same as before, so the view does not jump.
        """
        shown = [(path, lines) for path, lines, _ in files]
        if shown == self._shown:
            return
        self._shown = shown
        scroll = self.verticalScrollBar().value()
        self.clear()
        self._file_blocks, self._file_names, self._sections = [], [], []
        cursor = QTextCursor(self.document())

        def add(pieces: list[tuple[str, QTextCharFormat]]) -> int:
            """Append a line made of pieces; returns its block number."""
            if cursor.blockNumber() or cursor.position():
                cursor.insertBlock()
            for text, fmt in pieces:
                cursor.insertText(text, fmt)
            return cursor.blockNumber()

        grey = QTextCharFormat()
        grey.setForeground(self._grey)
        header = QTextCharFormat()
        header.setFontWeight(QFont.Weight.Bold)

        for index, (path, lines, sections) in enumerate(files):
            if index:
                add([("", grey)])
            try:
                shown_path = path.relative_to(root)
            except ValueError:
                shown_path = path
            add([(str(shown_path), header)])
            rows = _token_rows(path, lines)
            width = len(str(len(lines)))
            base = None
            for number, line in enumerate(lines):
                pieces = [(f"{number + 1:>{width}}  ", grey)]
                if rows is None:
                    pieces.append((line, QTextCharFormat()))
                else:
                    pieces += [(text, self._format(token)) for token, text in rows[number]]
                block = add(pieces)
                base = block if base is None else base
            self._file_blocks.append((base, base + len(lines) - 1))
            self._file_names.append(str(shown_path))
            self._sections += [(base + first, base + last, tag) for tag, first, last in sections]
        self.setExtraSelections([])
        self.verticalScrollBar().setValue(scroll)
        self._update_header()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        rect = self.contentsRect()
        self._header.setGeometry(rect.left(), rect.top(), rect.width(), self._header.height())

    def _update_header(self) -> None:
        """Show the path of the file the first visible line belongs to."""
        top = self.firstVisibleBlock().blockNumber()
        name = ""
        for (first, last), path in zip(self._file_blocks, self._file_names):
            # Empty while the file's own path line (first - 1) is the top line, or the name shows twice.
            if first <= top <= last + 1:  # last + 1 is the blank line before the next file
                name = path
        self._header.setText(name)

    def highlight(self, node_id: str | None) -> None:
        """Tint the code tagged for the node and grey the rest, and scroll to it unless it is in view.

        Without a node, or without code for it, the files are shown plainly.
        """
        own = [(first, last) for first, last, tag in self._sections if node_id and node_id.startswith(tag)]
        if not own:
            self.setExtraSelections([])
            return
        tint = QTextCharFormat()
        tint.setBackground(self._tint)
        tint.setProperty(QTextFormat.Property.FullWidthSelection, True)
        grey = QTextCharFormat()
        grey.setForeground(self._grey)
        selections = []
        for first, last in self._file_blocks:
            next_block = first
            for start, end in (r for r in own if first <= r[0] <= last):
                if start > next_block:
                    selections.append(self._selection(next_block, start - 1, grey))
                selections.append(self._selection(start, end, tint))
                next_block = max(next_block, end + 1)
            if next_block <= last:
                selections.append(self._selection(next_block, last, grey))
        self.setExtraSelections(selections)
        start, end = own[0]
        bar = self.verticalScrollBar()
        if not (bar.value() <= start and end < bar.value() + bar.pageStep()):
            bar.setValue(max(start - CONTEXT_LINES, 0))

    def _selection(self, first: int, last: int, fmt: QTextCharFormat) -> QTextEdit.ExtraSelection:
        """Selection covering blocks first..last."""
        cursor = QTextCursor(self.document().findBlockByNumber(first))
        cursor.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor, last - first)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format = fmt
        return selection

    def _format(self, token) -> QTextCharFormat:
        if token not in self._token_formats:
            style = _STYLE.style_for_token(token)
            fmt = QTextCharFormat()
            if style["color"]:
                fmt.setForeground(QColor(f"#{style['color']}"))
            if style["bold"]:
                fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setFontItalic(bool(style["italic"]))
            self._token_formats[token] = fmt
        return self._token_formats[token]

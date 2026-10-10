import sys

from PySide6.QtWidgets import QApplication

from editor.main_window import APP_NAME, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()  # before opening, so the view has its real size when the graph is fitted
    arguments = app.arguments()
    if len(arguments) > 1:
        window.open_file(arguments[1])
    window.start_claude_check()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

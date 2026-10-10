# PraeceptaFlow

A prototype desktop editor for flow graphs that describe the structure of a program. You draw the program as boxes and arrows; [Claude Code](https://claude.com/claude-code) turns the graph into source code and can check afterwards whether code and graph still agree.

![A flow graph for a small ping program on the left, the Java code generated from it on the right, with the selected box's code highlighted](demo-screenshot.png)

Built with PySide6 (Qt's Graphics View framework). This is a prototype: Windows only, no test suite, and the file format may change.

## Requirements

- Windows
- Python 3.14
- PySide6 6.10 or later (installed by the setup below)
- Optional: the `claude` command on `PATH`, logged in. Only the Source Generation menu needs it.

## Setup

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Usage

| Task | Command |
|---|---|
| Run the app | `run.bat [file.pfg]` |
| Run the app with a console | `run_console.bat [file.pfg]` |
| Build a PyInstaller bundle | `build.bat [--onefile]` |
| Remove `build\` and `dist\` | `clean.bat` |

## How it works

A graph is made of typed boxes connected by edges. Right-click the canvas (or use the Insert menu) to add a box, and drag from a box's border to another box to connect them.

| Box | Purpose |
|---|---|
| Data Input, Data Output, Data Structure, Algorithm | The steps of the program. Edges between them are the program flow. |
| Implementation Note, Tooling Note | Remarks attached to the steps they are connected to (or to the whole project if unconnected). |
| Project Note | A remark about the whole project. |

The graph is saved as `name.pfg` (JSON). Two more files appear beside it:

- `name.pd`: the project description, plain text derived from the graph and rewritten on every save. This is what Claude Code reads.
- `name.notes`: how the last generation realized each note.

The Source Generation menu runs Claude Code in the folder of the graph:

- **Generate Source Code** (Ctrl+G) writes source code for the description. Each step's code is marked with a tag comment, so selecting a box highlights its code in the pane on the right.
- **Check Parity** (Ctrl+Shift+G) compares the source code with the description without changing anything, and marks each box as matching, mismatching or unimplemented.

The model and effort level for both commands are chosen in the same menu.

## License

Copyright (C) 2026 VaTTeRGeR

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed without any warranty. See [LICENSE](LICENSE) for the full text.

PraeceptaFlow uses [PySide6](https://doc.qt.io/qtforpython-6/) (LGPLv3) and [Pygments](https://pygments.org/) (BSD 2-Clause).

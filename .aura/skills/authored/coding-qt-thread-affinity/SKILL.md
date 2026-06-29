---
task_kinds: ["coding", "gui"]
path_globs: ["aura/gui/**", "aura/**/*.py"]
model: null
triggers: ["QtWidgets", "QtCore", "pyqtSignal", "QThread", "QObject", "PySide", "signal", "slot", "thread"]
---

Never create, read, or update a Qt widget from any thread but the one that owns it. Marshal cross-thread updates through signals and slots. Off-thread widget access crashes without a clean traceback.

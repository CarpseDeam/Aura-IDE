# Codebase Structure

## Directory Layout

```
aura/
  __init__.py
  __main__.py              # entry point — wires app, theme, MainWindow
  config.py                # settings, paths, model registry, pricing constants
  bridge/
    __init__.py
    qt_bridge.py           # ConversationBridge — QThread + blocking approval
  client/
    __init__.py
    events.py              # streaming event dataclasses
    deepseek.py            # DeepSeekClient.stream(...) -> Iterator[Event]
  conversation/
    __init__.py
    history.py             # History with for_api() — the replay-rule trap
    manager.py             # ConversationManager — model->tool->model loop
    persistence.py         # Save/load conversation history
    tools/
      __init__.py
      registry.py          # ToolRegistry — workspace jail, tool defs, dispatch
      fs_read.py           # read_file, list_directory, glob
      fs_write.py          # propose_write, propose_edit (no FS mutation here)
      backup.py            # timestamped pre-write backups
  gui/
    __init__.py
    chat_view.py           # transcript with all card types
    diff_dialog.py         # modal diff approval dialog
    input_panel.py         # composer (text/drag/paste/picker/send/stop)
    main_window.py         # QMainWindow, three-pane splitter, toolbar
    settings_dialog.py     # Application settings UI
    theme.py               # dark palette + global stylesheet
    workspace_tree.py      # File tree for the active workspace
```

## Documentation

- `docs/API.md`: Public interface documentation.
- `docs/ARCHITECTURE.md`: High-level system design.
- `docs/CHANGELOG.md`: History of changes.
- `SPEC.md`: Original project specification and Phase 2 plan.

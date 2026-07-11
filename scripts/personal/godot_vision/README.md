# Personal Godot vision tool

This optional tool uses a locally installed Ollama vision model to critique an Aura Godot preview.
It is intentionally outside the `aura` package, is excluded by setuptools, and is not included by
Aura's Nuitka installer build.

## Setup for V_Ruins

1. Install Ollama and pull a vision-capable model of your choice.
2. Optionally set `AURA_GODOT_VISION_MODEL` in the environment used to launch Aura. The personal
   tool defaults to `gemma3:12b`, which is already installed and was verified with local image input
   on this workstation. The installed `llama3.2-vision` artifact is incompatible with the current
   local Ollama runtime.
3. Copy `critique_godot_preview.py` into `C:\Projects\V_Ruins\.aura\tools\`.
4. Restart Aura with `C:\Projects\V_Ruins` as the workspace.

Aura discovers `.aura/tools/*.py` automatically in Worker and single-agent modes. The resulting tool
is named `critique_godot_preview_local`.

Example PowerShell setup:

```powershell
$env:AURA_GODOT_VISION_MODEL = "your-local-vision-model"
New-Item -ItemType Directory -Force C:\Projects\V_Ruins\.aura\tools | Out-Null
Copy-Item scripts\personal\godot_vision\critique_godot_preview.py `
  C:\Projects\V_Ruins\.aura\tools\critique_godot_preview.py
python -m aura
```

The tool accepts only workspace-relative PNGs beneath `.aura/tmp/godot_previews`, sends image bytes
only to `127.0.0.1:11434`, and returns bounded text/JSON. It does not mutate Godot or save scenes.

If Aura uses Docker sandbox mode, this personal tool will not reach the host Ollama service. Use host
sandbox mode for this local workflow.

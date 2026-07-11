# Personal Godot vision tool

This optional personal tool uses a locally installed Ollama vision model to produce a concise
factual visual description of an Aura Godot preview screenshot.  The description is consumed by
DeepSeek (the builder) so it can continue constructing the scene in the same request — it acts as
DeepSeek's local eyes, not as a critic or planner.

It is intentionally outside the `aura` package, is excluded by setuptools, and is not included by
Aura's Nuitka installer build.

## Setup for V_Ruins

1. Install Ollama and pull a vision-capable model of your choice.
2. Optionally set `AURA_GODOT_VISION_MODEL` in the environment used to launch Aura. The personal
   tool defaults to `gemma3:12b`, which is already installed and was verified with local image input
   on this workstation.
3. Copy `describe_godot_preview.py` into `C:\Projects\V_Ruins\.aura\tools\`.
4. Restart Aura with `C:\Projects\V_Ruins` as the workspace.

Aura discovers `.aura/tools/*.py` automatically in Worker and single-agent modes. The resulting tool
is named `describe_godot_preview_local`.

Example PowerShell setup:

```powershell
$env:AURA_GODOT_VISION_MODEL = "your-local-vision-model"
New-Item -ItemType Directory -Force C:\Projects\V_Ruins\.aura\tools | Out-Null
Copy-Item scripts\personal\godot_vision\describe_godot_preview.py `
  C:\Projects\V_Ruins\.aura\tools\describe_godot_preview.py
python -m aura
```

The tool accepts only workspace-relative PNGs beneath `.aura/tmp/godot_previews`, sends image bytes
only to `127.0.0.1:11434`, and returns a small bounded dictionary containing:

- `ok`
- `local_only`
- `model`
- `capture_path`
- `width`
- `height`
- `description` — a plain factual visual description covering footprint, wall runs, corners,
  openings, rooms, towers, gaps, overlaps, rubble and damage, silhouette, relative scale, unfinished
  edges, camera limitations, and uncertain or obscured areas

The descriptor is explicitly forbidden from judging quality, issuing a verdict, scoring coherence,
recommending changes, or selecting the next action. It does not mutate Godot or save scenes.

This script requests a 45-second Aura dynamic-tool timeout for cold local-model startup. Other dynamic
tools retain Aura's 30-second default; timeout hints are bounded to 120 seconds.

If Aura uses Docker sandbox mode, this personal tool will not reach the host Ollama service. Use host
sandbox mode for this local workflow.

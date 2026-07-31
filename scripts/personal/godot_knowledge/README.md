# Personal Godot expertise pack — moved

The Godot skills that used to live here now ship with Aura as packaged skills in
`aura/skills/bundled/godot_*/SKILL.md`. There is one authoritative copy; nothing needs to be copied
into a project any more.

Aura loads them automatically when the open workspace is a Godot project (`workspace_markers:
["project.godot"]`), and selects only the skills relevant to the current request, task kind, target
files, and model.

The pack complements `inspect_godot_api`: skills provide engineering judgment and workflow;
ClassDB provides exact version-specific signatures.

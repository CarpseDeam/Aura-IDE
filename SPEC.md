Refactor Aura Settings UI into a clean, organized settings dialog.

Context:
The current Settings menu is a cluttered single-page form. It mixes unrelated concerns:
- API provider keys
- Tavily key
- Planner/Worker provider/model selection
- local vision settings
- temperature settings
- automation toggles
- sandbox mode
- CLI backend auth
- system prompts
- workspace/backups info
- model discovery/background threads

This is code smell and bad UX. The goal is to reorganize Settings without changing core behavior.

Scope:
UI organization and code structure only.

Do NOT:
- Change tool-call limits.
- Change planner/worker dispatch behavior.
- Change provider registry behavior.
- Change model defaults.
- Change Google Cloud / Vertex backend behavior.
- Touch Gemini CLI behavior.
- Add/remove providers.
- Make live API calls just from opening Settings.
- Rewrite unrelated GUI components.

Goal:
Turn SettingsDialog into a clean shell with grouped pages/sections.

Recommended layout:
Use tabs or a left-side category list.

Preferred sections:
1. Models
   - Planner provider
   - Planner model
   - Planner thinking
   - Worker provider
   - Worker model
   - Worker thinking
   - Temperature
   - Worker temperature

2. API Keys
   - Provider API key management
   - Tavily key management
   - Clear/save/status indicators

3. Agent Backends
   - Gemini CLI auth/status
   - Claude Code auth/status
   - Codex auth/status
   - Recheck/login buttons

4. Automation
   - Planner/Worker mode
   - Auto-dispatch
   - Auto-approve
   - Auto-commit
   - Restore recent conversation

5. Vision
   - Local vision enabled
   - Vision model
   - Vision endpoint

6. Sandbox / Workspace
   - Sandbox mode
   - Workspace root
   - Backups location

7. Prompts
   - Single-mode prompt
   - Planner prompt
   - Worker prompt
   - Reset buttons

Implementation:
Split the giant SettingsDialog into smaller widgets.

Suggested files:
- aura/gui/settings_dialog.py
- aura/gui/settings_pages/__init__.py
- aura/gui/settings_pages/models_page.py
- aura/gui/settings_pages/api_keys_page.py
- aura/gui/settings_pages/agent_backends_page.py
- aura/gui/settings_pages/automation_page.py
- aura/gui/settings_pages/vision_page.py
- aura/gui/settings_pages/sandbox_page.py
- aura/gui/settings_pages/prompts_page.py

SettingsDialog should:
- own OK/Cancel
- create the pages
- pass the current AppSettings into each page
- collect values from each page into one AppSettings object
- save only when OK is clicked
- not contain all individual widget logic directly anymore

Important:
Keep existing behavior intact. This is a cleanup/refactor, not a behavior redesign.

Model page requirements:
- Show Planner and Worker controls clearly.
- No top-level confusing default provider/model selector.
- Models come from provider_registry.
- Provider changes should update that page’s model dropdowns.
- Do not hardcode DeepSeek globally.
- Provider-specific defaults only apply to that provider.

API Keys page requirements:
- Make it clear which provider key is being edited.
- Key status should say whether it came from env var or saved key.
- Tavily should be separate from model provider keys.

Agent Backends page requirements:
- Gemini CLI stays.
- Claude Code stays.
- Codex stays.
- Auth checks stay asynchronous.
- Do not mix CLI backend auth with API provider auth.

Prompt page requirements:
- Make prompt text boxes larger and easier to read.
- Keep reset buttons.

Validation:
Run:
python -m compileall aura tests scripts
python -m pytest tests -q

Manual validation:
- Open Settings.
- Confirm it is organized into clear sections/tabs.
- Confirm Planner/Worker model settings still save and reload.
- Confirm API keys still save/clear.
- Confirm Tavily key still saves/clears.
- Confirm Gemini CLI / Claude Code / Codex auth status still works.
- Confirm automation toggles still save.
- Confirm prompts still save.
- Confirm closing with Cancel does not save changes.
- Confirm no provider/backend behavior changed.

Report:
- files created
- files modified
- what sections were created
- compileall result
- pytest result
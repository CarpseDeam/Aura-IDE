# Spec: Rebuild Web Research Around One Browser Owner

## Goal

Rebuild Aura web research around one explicit owner: a Research Browser Controller that opens and controls the user browser session for research.

Aura research should feel like a real assistant using a real browser, not a pile of silent modes, hidden fallbacks, subprocess seams, and special-case source patches.

## Current problem

The current implementation spreads browser behavior across too many places:

- `aura/drones/bundled/web-research/drone.json` declares browser profile, visibility, and live browser flags.
- `aura/research/request.py` declares route, UI mode, and headless state.
- `aura/research/ui_contract.py` derives silent/headless/visible/no-work-surface flags and env overrides.
- `aura/conversation/tools/_planner_mixin.py` wraps web-research calls with silent answer-only upstream payloads.
- `aura/drones/folder_runner.py` chooses in-process vs subprocess execution based on silent intent and applies env overrides.
- `aura/drones/bundled/web-research/browser_search.py` reads manifest/env state and creates `BrowserRuntime`.
- `aura/browser/runtime.py` chooses browser routes and creates Playwright launch/persistent contexts.
- `aura/drones/bundled/web-research/fetching.py` can disable browser discovery, use DDG HTML fallback, and add special schedule targets.

This makes browser behavior hard to reason about. A user-facing research run can be silently changed by manifest fields, upstream payload fields, environment variables, fallback branches, or runtime route selection.

## Product invariant

Aura has one web research mode:

Aura opens and controls the user browser session, navigates it, observes the page, and uses that session for research.

The browser session is visible, intentional, and credential-capable. The research path reports what browser/session/page it is using.

## Architecture target

Create a single Research Browser Controller and route all web research browser work through it.

Suggested module shape:

- `aura/browser/research_controller.py`
  - Owns browser executable selection.
  - Owns Aura research profile directory selection.
  - Owns remote-debugging port allocation.
  - Owns process launch.
  - Owns CDP readiness probing.
  - Owns page/tab acquisition.
  - Owns navigation.
  - Owns route/session/page receipts.

- `aura/browser/browser_config.py` or equivalent
  - Stores the user-selected browser policy.
  - Default should prefer Chrome on Windows when available.
  - Keep future browser choice explicit and user-configurable.

- `aura/drones/bundled/web-research/browser_search.py`
  - Becomes a caller of the controller.
  - Stops deciding visibility/headless/profile/runtime route.

- `aura/drones/bundled/web-research/research_pipeline.py`
  - Uses the controller-backed browser session for discovery and source reads.
  - Keeps query parsing, evidence selection, and synthesis separate from browser ownership.

## Implementation pattern

Use a native Aura implementation of this common browser-control pattern:

1. Find the configured browser executable.
2. Allocate a local free port.
3. Launch the browser process with Aura's research profile directory and `--remote-debugging-port=<port>`.
4. Poll `http://127.0.0.1:<port>/json/version` until CDP is ready.
5. Connect to the browser over CDP.
6. Acquire or create a page/tab.
7. Navigate the page immediately to the requested search or target URL.
8. Return a receipt containing browser executable, profile directory, PID when available, CDP URL, active URL, page title, and navigation status.

Use this as an architecture pattern only. Write Aura-native code. Copy no third-party source code, class bodies, function bodies, constant lists, watchdog structures, or retry logic.

## Ownership rules

The Research Browser Controller owns every browser-facing decision for web research.

Other code may request research. Other code may pass a query or target URL. Other code may consume evidence. Other code does not choose browser mode, headless mode, visibility, profile, runtime route, or fallback route.

## Cleanup requirements

### Collapse UI/headless contract sprawl

Retire the web-research meaning of:

- `ui_mode`
- `headless`
- `silent`
- `visible`
- `no_work_surface`
- `AURA_RESEARCH_UI_MODE`
- `AURA_WEB_RESEARCH_HEADLESS`
- `AURA_WEB_RESEARCH_VISIBLE`

Keep compatibility shims only where needed to avoid immediate import breakage, and make them inert for web research behavior.

### Simplify planner/tool web research calls

`launch_read_only_drone` and `run_read_only_drone` should pass the research query/goal to the web-research path without constructing a competing browser/UI contract.

### Simplify folder runner

`run_folder_drone_sync` should stop treating web-research as a special silent/in-process execution mode.

The web-research implementation itself should own browser behavior through the Research Browser Controller.

### Replace BrowserRuntime usage for research

Keep `aura/browser/runtime.py` only if another feature still uses it.

Web research should not use `BrowserRuntime` as its browser owner once the Research Browser Controller exists.

### Remove automatic web-research route substitution

Remove automatic DDG HTML fallback from the normal research route.

Remove special-case source targeting such as world-cup/schedule hardcoded targets from generic source discovery.

Generic research should discover through the controlled browser session. Domain-specific source preferences can be designed later as explicit user-visible policy, not hidden pipeline behavior.

## User profile and credentials

Aura should use an Aura-owned research profile directory by default so the session can persist credentials safely across runs without fighting the user's currently-open primary Chrome profile lock.

The profile should be user-visible and intentional. First-time credential setup can open the same research browser profile so the user can sign in once, then Aura can reuse that profile on later research runs.

Future UI can expose:

- selected browser executable
- research profile path
- open research browser
- reset research browser profile

This spec focuses on backend ownership and research behavior first.

## Browser launch details

For Windows, support at least Chrome detection first:

- `%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe`
- `%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe`
- `%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe`

Use a small, Aura-owned launch argument set:

- `--remote-debugging-port=<port>`
- `--user-data-dir=<aura research profile dir>`
- `--profile-directory=Default`
- `--no-first-run`
- `--no-default-browser-check`

Add more launch args only when they solve a verified Aura problem.

## Receipts and diagnostics

Every research run should include route diagnostics in the returned artifact/receipt:

- controller name/version
- browser executable path
- browser profile directory
- browser PID if available
- CDP URL
- requested query or target URL
- first navigated URL
- final active URL
- page title
- navigation status
- errors with exact phase names: detect, launch, cdp_ready, connect, page, navigate, extract

These diagnostics should make a blank-window issue obvious from a single receipt.

## Acceptance checks

### Static checks

- There is one browser owner for web research.
- Web research no longer reads behavior from `ui_mode/headless/silent/visible` fields.
- Web research no longer depends on `AURA_RESEARCH_UI_MODE`, `AURA_WEB_RESEARCH_HEADLESS`, or `AURA_WEB_RESEARCH_VISIBLE`.
- Web research no longer uses automatic DDG fallback in the normal route.
- Generic source discovery no longer contains hardcoded world-cup/schedule source targets.

### Runtime checks

Create focused tests around the controller boundary:

1. Controller builds a Chrome launch command with an Aura research profile dir and remote debugging port.
2. Controller waits for `/json/version` and records CDP readiness.
3. Controller reports phase-specific errors when browser detection, launch, CDP readiness, or navigation fails.
4. Web-research pipeline calls the controller for browser discovery and source reads.
5. Planner/tool web-research calls no longer construct silent/headless upstream browser policy.
6. Search/source discovery tests use fixtures at the controller boundary rather than hardcoded public web domains.

### Manual Windows smoke check

Run Aura on Windows and ask a current-info question such as:

`Are there any World Cup matches today?`

Expected behavior:

- Aura opens the intentional research browser window/profile.
- The browser navigates immediately to the requested search or target.
- No blank orphan Python/browser slab appears.
- The Worker/Planner receives sourced research output or a phase-specific browser receipt.
- The receipt identifies the browser executable, profile dir, CDP URL, active URL, and page title.

## Suggested work order

1. Add the Research Browser Controller with mocked tests for command construction, CDP readiness, and navigation receipt shape.
2. Wire web-research browser discovery to the controller.
3. Remove web-research behavior ownership from `ui_contract.py`, `request.py`, `_planner_mixin.py`, and `folder_runner.py`.
4. Remove normal-route DDG fallback and hardcoded source targets from `fetching.py`.
5. Update tests that currently assert silent/headless plumbing so they assert controller ownership instead.
6. Run focused tests, then run the Windows smoke check.

## Completion report format

When finished, report:

- Files changed
- Ownership paths removed
- New controller API
- Browser launch command shape, with secrets/user paths redacted if needed
- Tests run
- Windows smoke result
- Remaining follow-up work, if any

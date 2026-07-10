# Aura Ruin Assembly and Visual Iteration Roadmap

## Purpose

Build a safe creative loop in which a user can describe a ruin, Aura can assemble it from modular
pieces in Godot, and Aura can inspect, validate, preview, critique, and revise the result.

The target experience is:

> "Make a compact courtyard fortress with one turret."

Aura should translate that intent into a structured brief, assemble compatible pieces, validate the
geometry, capture consistent previews, judge the composition, and make bounded revisions. This is the
spatial equivalent of Aura's coding loop:

```text
Intent -> Inspect -> Assemble -> Validate -> Preview -> Critique -> Revise
```

This roadmap deliberately favors small, reversible capabilities over a large autonomous system. Aura's
existing coding behavior must continue working even when Godot is closed, the bridge is missing, or a
Godot project does not use modular ruins.

## Current baseline

The following foundation exists today:

- A project-local **Aura Editor Bridge** Godot addon.
- A token-authenticated, localhost-only protocol.
- Live inspection of the open scene tree, selection, scripts, transforms, and Inspector properties.
- Approval-gated node creation and property edits through Godot's `EditorUndoRedoManager`.
- Explicit scene saving and semantic reinspection.
- Separate transport, protocol, perception, and action modules; no monolithic plugin script.

The bridge is connected successfully in `V_Ruins` and can inspect
`res://scenes/ruin_preview.tscn`.

## Safety invariants

These requirements apply to every milestone and are not optional.

1. **Godot integration remains optional.** Aura starts and performs normal coding tasks without Godot,
   the addon, or a live bridge.
2. **New capabilities are feature-gated.** Experimental assembly and vision tools remain disabled from
   autonomous use until their milestone acceptance checks pass.
3. **Reads and writes stay separate.** Inspection and preview capture are observational. Scene mutation
   remains approval-gated and unavailable to the Planner.
4. **Godot owns scene mutation.** Live edits use `EditorUndoRedoManager`; Aura does not secretly rewrite
   an open `.tscn` behind Godot's back.
5. **Saving is explicit.** A successful edit does not automatically save unless the approved operation
   requests it.
6. **Production scenes are protected.** Early assembly work targets a dedicated preview scene or a new
   generated scene, never an important authored scene by default.
7. **Every loop is bounded.** Limit operations per pass, preview count, total iterations, response size,
   and wall-clock time. Cancellation must always return control to the user.
8. **No arbitrary execution.** The bridge accepts a versioned whitelist of commands. It never evaluates
   model-written GDScript or arbitrary editor methods.
9. **Paths stay inside the project.** Resource instancing and output paths must resolve beneath `res://`.
10. **Failure is local.** A bridge timeout, malformed module, unavailable viewport, or vision failure
    produces a clear tool error; it must not poison Aura's normal conversation or Worker lifecycle.
11. **Restore and audit evidence are retained.** Each approved pass records its brief, operations,
    validation, previews, and resulting scene path.
12. **Generic and project-specific logic stay separate.** The reusable editor bridge must not absorb
    `V_Ruins` generation rules.

## Architecture boundaries

Keep the system divided into narrow components:

```text
Aura core
  conversation tools
    Godot inspection adapter       read-only
    Godot mutation adapter         approval-gated
    visual evidence adapter        read-only

Aura Godot integration
  bridge client                    protocol, timeout, authentication
  bridge installer                 project-local addon installation/update
  iteration coordinator            bounded inspect/validate/preview loop

Godot addon
  transport/                       localhost message transport
  protocol/                        versioned request routing
  perception/                      scene snapshots and preview capture
  actions/                         undoable generic editor operations

V_Ruins
  module catalog                   asset metadata, tags, bounds, sockets
  ruin assembler                   layout graph -> scene instances
  ruin validators                  overlap, grounding, connection, access
  ruin brief adapter               user intent -> constrained parameters
```

The iteration coordinator may orchestrate these components, but it must not implement their internal
logic. No single "ruin agent" or bridge file should become the owner of transport, layout, validation,
vision, and mutation.

## Milestone plan

### Phase 0 — Live semantic bridge

**Status:** Foundation implemented and connected.

**Acceptance gate:**

- The addon activates normally in Godot.
- Aura can ping it and inspect the open scene.
- Godot-offline behavior is a normal recoverable tool error.
- Existing Aura selfcheck and focused tool tests pass.

### Phase 1 — Read-only module catalog

Teach Aura what pieces exist before allowing assembly.

**Scope:**

- Define a `RuinModule` record containing resource path, category, tags, local bounds, allowed
  rotations, sockets, entrance/exit roles, and optional style metadata.
- Scan or explicitly register modular ruin scenes without instantiating them into the active scene.
- Expose a read-only `inspect_ruin_modules` tool.
- Report missing scenes, duplicate identifiers, invalid sockets, and unusable metadata.

**Acceptance gate:**

- Catalog output is deterministic for the same project state.
- Every returned resource exists and loads in Godot.
- Invalid modules are excluded with actionable diagnostics.
- No scene files are changed.

**Rollback:** Disable the catalog tool. The generic editor bridge remains unaffected.

### Phase 2 — Safe module instancing

Add the smallest reliable assembly vocabulary.

**Scope:**

- Instantiate a `PackedScene` beneath an approved parent.
- Set name and `Transform3D` during creation.
- Move, rotate, duplicate, reparent, and remove bridge-created instances.
- Group a batch into one named Godot undo action.
- Track which nodes were created by the current assembly pass.
- Work in `ruin_preview.tscn` or a new output scene first.

**Acceptance gate:**

- Create, move, undo, redo, and remove work in the normal editor.
- Failed batches make no partial scene changes.
- Instancing rejects paths outside `res://` and non-`PackedScene` resources.
- Existing hand-authored nodes are not removable unless explicitly addressed and approved.
- The scene can be reinspected immediately after each operation.

**Rollback:** One Godot Undo action restores the pre-pass scene state.

### Phase 3 — Structural validation

Give geometry the final say on correctness.

**Scope:**

- Socket compatibility and alignment tolerance.
- World-space overlap checks using declared or calculated bounds.
- Ground contact and excessive floating/burial checks.
- Required entrance and courtyard connectivity.
- Minimum passage width and navigability proxies.
- Footprint, height, piece-count, and density constraints from the ruin brief.

**Acceptance gate:**

- Validators return structured facts with node paths and measured values.
- Known-good fixtures pass and intentionally broken fixtures fail.
- Validation is deterministic and does not mutate the scene.
- Aesthetic criticism cannot override a structural failure.

**Rollback:** Assembly remains manually reviewable without automated validation.

### Phase 4 — Controlled preview capture

Provide consistent visual evidence instead of arbitrary screenshots.

**Scope:**

- Capture a top-down image.
- Capture a three-quarter overview.
- Capture a ground-level entrance view.
- Optionally capture a slow orbit as a bounded set of still frames.
- Store captures beneath `.aura/tmp/` with scene revision and camera metadata.
- Prefer a controlled preview camera/scene when the editor viewport is unreliable.

**Acceptance gate:**

- Repeated captures of an unchanged scene use equivalent camera framing.
- Images contain the assembled ruin and are not blank, stale, or editor UI chrome.
- Capture failure is recoverable and never blocks semantic inspection.
- Image dimensions and total payload are bounded.

**Rollback:** Disable preview capture while semantic assembly continues to work.

### Phase 5 — Visual critic

Use vision for aesthetic judgment, not structural authority.

**Scope:**

- Attach preview images to the model as genuine multimodal input, never base64 inside plain tool text.
- Evaluate the ruin against the original brief using a small rubric:
  silhouette, hierarchy, courtyard readability, turret dominance, entrance clarity, repetition, and
  visual balance.
- Require observations to reference a particular camera and visible region.
- Produce proposed adjustments rather than direct mutations.

**Acceptance gate:**

- The critic distinguishes structural facts from visual opinions.
- Critiques remain tied to the brief and captured evidence.
- The same unchanged preview does not cause endless contradictory edits.
- The user can inspect the images and critique before approving revisions.

**Rollback:** Ignore critic proposals; retain previews as user-facing evidence.

### Phase 6 — Bounded assemble/look/revise loop

Connect the proven pieces without granting open-ended autonomy.

**Scope:**

1. Parse a user request into a `RuinBrief`.
2. Read the module catalog and current preview scene.
3. Propose one assembly pass.
4. Request approval and apply one undoable batch.
5. Run structural validation.
6. Capture controlled previews.
7. Produce a visual critique.
8. Propose a bounded revision or stop.

Initial limits:

- Maximum 3 revision passes.
- Maximum 25 scene operations per pass.
- Maximum 4 preview images per pass.
- Stop immediately on repeated validation failure, unchanged scene state, bridge disconnect, user
  cancellation, or exhausted limits.

**Acceptance gate:**

- The loop completes a small fixture brief without manual file editing.
- Every mutation remains individually inspectable and collectively undoable.
- Stop reasons are explicit and truthful.
- Coding-only Aura regression tests remain unchanged and passing.

### Phase 7 — Product polish

Only after the creative loop is reliable:

- Aura project panel status: **Not installed**, **Installed/disabled**, **Connected**, **Update
  available**, or **Error**.
- One-click **Install / Update Godot Bridge**.
- Connection health and protocol-version display.
- Preview gallery and pass comparison.
- "Accept variation", "Revise", and "Undo last pass" actions.

## Ruin brief contract

Aura should preserve the user's words and derive a constrained brief rather than replacing the request
with an opaque prompt.

Example:

```yaml
intent: compact courtyard fortress with one turret
scale: small
footprint: compact
enclosure: high
courtyard:
  required: true
  openness: medium
turrets:
  count: 1
  prominence: medium
entrances:
  primary: 1
navigation: traversable
damage: moderate
piece_budget: 18
```

The brief becomes the shared acceptance contract for layout validation and visual critique. Any inferred
field should remain visible and revisable by the user.

## Iteration artifact

Each pass should produce a compact record:

```yaml
pass: 2
scene: res://scenes/generated/courtyard_fortress_01.tscn
brief_revision: 1
operations:
  - instantiated west_wall_02
  - moved turret_a inward by 1.5m
structural_validation:
  status: passed
  warnings:
    - south passage width is near minimum
previews:
  - top_down
  - overview_northeast
  - entrance_ground
visual_critique:
  strengths:
    - courtyard reads clearly
  issues:
    - turret dominates the entrance silhouette
next_proposal:
  - lower turret by one module tier
stop_reason: awaiting_user_approval
```

This record is evidence and recovery context, not a replacement for the Godot scene or undo history.

## Regression and verification matrix

Before advancing any phase, run checks at four levels:

1. **Pure unit tests:** protocol parsing, catalogs, briefs, transforms, bounds, and validator logic.
2. **Bridge tests:** authentication, timeouts, version mismatch, response limits, and offline recovery.
3. **Godot integration fixtures:** actual Godot 4.6 parsing plus create/undo/reinspect behavior in a
   disposable project.
4. **Aura regression checks:** focused conversation/tool tests, Worker approval behavior, selfcheck, and
   a coding-only smoke task with Godot closed.

No phase is complete merely because the happy path works once in `V_Ruins`.

## Immediate next task

Begin Phase 1 only: define the `RuinModule` metadata contract and build a read-only catalog for the
existing `V_Ruins` pieces. Do not add autonomous assembly, preview capture, or vision in the same change.

The first review question is:

> Can Aura accurately explain which modular pieces are available, how they connect, and which pieces are
> unsuitable—without changing the open scene?

Once that is trustworthy, proceed to safe instancing as a separate milestone.

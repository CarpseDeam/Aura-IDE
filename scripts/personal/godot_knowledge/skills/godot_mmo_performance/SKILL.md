---
task_kinds: ["performance", "mmo"]
path_globs: []
triggers: ["mmo", "performance", "multimesh", "lod", "hlod", "occlusion", "streaming", "navigation", "multiplayer", "authority", "draw calls"]
---
### Godot MMO and Large-World Judgment

Measure before optimizing and state whether a cost is CPU, rendering, physics, navigation, memory,
network, or editor-time. Do not recommend a system-wide rewrite from intuition alone.

- Use MultiMesh for large groups of visually repeated, mostly uniform instances when per-instance
  node behavior is unnecessary. Partition MultiMeshes spatially because visibility is culled for the
  whole MultiMesh, not each instance.
- Consider mesh LOD, visibility ranges/HLOD, occlusion culling, shadow distance/quality, and spatial
  streaming as separate tools with different tradeoffs. Verify the renderer and target hardware.
- Keep decorative geometry out of physics and navigation unless gameplay requires it. Prefer simple
  collision proxies and bounded navigation regions; avoid rebuilding large navigation maps casually.
- Separate server-authoritative gameplay state from client presentation. Synchronize intentional
  state/events, not entire scene trees by default, and verify multiplayer authority at the node that
  owns the decision.
- Pool only when profiling demonstrates churn and when reset/lifetime semantics are explicit. Godot's
  normal node lifecycle is often simpler and safer at moderate scale.
- Large-world generation should be deterministic from explicit seeds/inputs, divided into bounded
  cells or chunks, and resumable without one monolithic generator script.

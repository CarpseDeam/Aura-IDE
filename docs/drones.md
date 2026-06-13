# Drones

Drones are reusable folder-backed workers. A Drone is registered from a folder
that contains:

- `drone.json`
- the Python entrypoint, usually `main.py`
- the smoke check, usually `smoke.py`
- optional support files such as `requirements.txt` and `README.md`

The manifest must identify a Python runtime:

```json
{
  "id": "source-scout",
  "name": "Source Scout",
  "description": "Collects source candidates.",
  "runtime": "python",
  "entrypoint": "main:run",
  "smoke": "smoke:run",
  "instructions": "Collect candidates and return cargo.",
  "write_policy": "read_only",
  "allowed_tools": [],
  "output_contract": "Return candidate cargo."
}
```

`allowed_tools` is a compatibility field for older UI surfaces. New Drones run
through their folder entrypoint, not through an LLM tool menu.

Use `/drone make <brief>` to enter Drone Architect mode. Aura may use the
Planner/Worker harness to author the folder, but registration goes through
`register_drone_folder`, which validates the manifest and runs the smoke check
before installing the Drone globally.

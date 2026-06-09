# Safety & Control

## Diff Approval

Every write operation (`write_file`, `edit_file`, `edit_symbol`) generates a unified diff preview before touching disk. The diff dialog shows:

- The file path relative to workspace root
- Added lines (green), removed lines (red), context lines (dim)
- A summary of changes (X insertions, Y deletions)

Actions per diff:

- **Approve** — Apply this one change
- **Reject** — Skip this one change
- **Approve All** — Apply all remaining changes in this batch
- **Reject All** — Skip all remaining changes in this batch

Auto-Approve (Settings → General) skips the diff dialog entirely. Not recommended unless you trust the model.

## Automatic Backups

Before every write operation, the existing file is backed up to `.aura/backups/<ISO-timestamp>/<relative-path>`. Backups are never automatically deleted. You can restore manually from the file system.

## Git Integration

- **Auto-commit** — After each Worker cycle, changed files are committed with an AI-generated message (enabled by default)
- **`/undo`** — Soft-resets the last commit and restores the pre-worker snapshot. Type `/undo` in the input panel.
- **`git_init`** — Initialize a git repo in the workspace if one doesn't exist
- **Snapshot/Restore** — Pre-worker snapshots are created automatically. Use `/undo` to restore.
- **`.gitignore`** — Aura adds `.aura/` to `.gitignore` automatically on workspace open

## Read-Only Mode

When enabled (Settings → General or via a read-only drone policy), all write tools are stripped from the AI's tool list. The AI can read files, search, and discuss but cannot modify anything. This is enforced at the tool registry level — writes are not just blocked by the UI, they are invisible to the model.

## API Key Encryption

Keys are stored in `~/.config/Aura/keys.json` encrypted with Fernet (symmetric encryption from the `cryptography` library). The encryption key is machine-derived using:

- Machine SID (Windows) or UUID (macOS/Linux)
- Combined with a static application salt
- Hashed with SHA-256 to produce a 32-byte Fernet key

Environment variables take precedence over encrypted storage. If both exist, the env var is used. Keys stored in the old plaintext format are automatically migrated to encrypted format on first access. File permissions are set to `0o600` (owner read/write only).

## Philosophy

Aura treats AI-generated code changes like a teammate's pull request. Every change is visible, reversible, and understandable. The guardrails (diff approval, backups, encryption, read-only mode) exist to protect your work without getting in the way. If you trust the model, you can reduce friction (auto-approve, auto-dispatch). If you're exploring, you can lock everything down (read-only mode, manual approval).

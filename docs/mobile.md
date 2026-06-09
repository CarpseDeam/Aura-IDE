# Mobile Companion

## Aura Companion

Aura Companion is a web-based mobile surface that connects to your running Aura desktop instance. It lets you interact with your Planner from your phone — browse projects, send messages, and dispatch specs — while the desktop streams responses back in real-time.

## How It Works

The companion uses a WebSocket relay. Your desktop Aura connects to the relay and registers itself as online. Your phone browser connects to the same relay and sees the desktop. Communication goes through the relay — the phone never connects directly to your desktop.

The relay is optional. You can run a local relay for LAN-only access, or use a hosted relay for remote access.

## What You Can Do

- **Browse projects** — See all recent Aura projects on your desktop
- **Browse conversations** — View threads within a project
- **Chat with the Planner** — Send messages and get real-time streaming responses
- **Dispatch specs** — The Planner can dispatch specs that the desktop Worker executes
- **View receipts** — Browse completed run receipts
- **Check drone status** — See active drone runs

## Connection Model

The desktop appears in the companion's "Desktops" view with:

- Display name (configurable in Settings → Companion)
- Online/offline status indicator (green dot when connected)
- Machine identifier

Tap a desktop to begin chatting. The companion shows "Online" or "Offline" in the header. If the desktop disconnects, a banner appears with a reconnect option.

## Setup

1. Open Aura Desktop
2. Go to Settings → Companion
3. Enable Companion
4. Set the relay URL (default: `ws://localhost:8765` for local relay)
5. Set a display name
6. Open `http://localhost:5173` (or the companion web URL configured in settings) on your phone
7. Pair using the displayed code

## Current State

- Chat with Planner is functional
- Project and conversation browsing works
- Spec dispatch from phone works
- Desktop streams responses in real-time
- More features landing soon

## Important Notes

- This is a **companion surface**, not a standalone app. Your desktop must be running Aura.
- The mobile companion is not a mobile IDE — it's a remote control and chat interface.
- `companion_enabled` is session-only and never persists. You must re-enable it each session.
- The companion web UI is served locally by default. For remote access, use a relay service or tunnel.

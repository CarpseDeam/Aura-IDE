"""Companion default URLs for hosted Cloudflare deployment.

These are the production defaults used by Aura Companion out of the box.
Developers can override them with the AURA_COMPANION_DEV_LOCAL env var.
"""

DEFAULT_HOSTED_COMPANION_WEB_URL = "https://aura-companion.pages.dev"
DEFAULT_HOSTED_COMPANION_RELAY_URL = "wss://aura-companion-relay.carpsedema.workers.dev/ws"
DEFAULT_LOCAL_COMPANION_WEB_URL = "http://localhost:5173"
DEFAULT_LOCAL_COMPANION_RELAY_URL = "ws://localhost:8765"

"""Aura Relay — FastAPI application."""
from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from relay.sessions import SessionManager
from relay.websocket import handle_websocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Aura Relay", version="0.1.0", description="Message router between Aura Desktop and Companion")
sessions = SessionManager()


@app.get("/health")
async def health():
    return {
        "service": "aura-relay",
        "status": "ok",
        "online_desktops": len(sessions.list_online("desktop")),
        "online_phones": len(sessions.list_online("phone")),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await handle_websocket(ws, sessions)


# Phase 4 endpoints (stubs)
@app.post("/api/pair")
async def pair_device():
    return {"error": "Not implemented in Phase 0"}


@app.post("/api/revoke")
async def revoke_device():
    return {"error": "Not implemented in Phase 0"}

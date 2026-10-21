"""Health, metrics and the live log stream."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from app import logging_setup, metrics
from app.auth.entra import UserClaims, authenticate, get_current_user
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", tags=["ops"])
async def health(settings: Settings = Depends(get_settings)):
    # web_search_available lets the UI show the opt-in only when Bing grounding is configured
    return {
        "status": "healthy",
        "service": "rfp-draft-agent",
        "web_search_available": bool(settings.bing_connection_id),
    }


@router.get("/metrics", tags=["ops"])
async def get_metrics(user: UserClaims = Depends(get_current_user)):
    return metrics.counters


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket, settings: Settings = Depends(get_settings)):
    """Stream live log entries to the browser over WebSocket (authenticated like the HTTP API)."""
    try:
        await authenticate(websocket.headers, settings)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    logging_setup.ws_clients.add(websocket)
    logger.info("WebSocket log client connected (%d total)", len(logging_setup.ws_clients))

    # Send recent log buffer to the new client
    for entry in logging_setup.log_buffer:
        try:
            await websocket.send_json(entry)
        except Exception:
            break

    try:
        while True:
            # Keep connection alive; client doesn't send data
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        logging_setup.ws_clients.discard(websocket)
        logger.info("WebSocket log client disconnected (%d remaining)", len(logging_setup.ws_clients))

"""FastAPI application for the GuidanceWidgets integration."""

import os
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .backend import FeedbackConflict, GuidanceWidgetBackend
from .backend import PlaygroundConfigError, PlaygroundSessionExpired
from .contracts import (
    FeedbackRequest,
    GuidanceResult,
    PlaygroundPreset,
    PlaygroundRunRequest,
    PlaygroundRunResult,
    SessionState,
    StateSnapshot,
)


router = APIRouter(prefix="/sessions")
playground_router = APIRouter(prefix="/playground")


@router.put("/{session_id}/state", response_model=SessionState)
def replace_state(session_id: str, snapshot: StateSnapshot, request: Request):
    return request.app.state.guidance_backend.replace_state(
        session_id,
        snapshot,
    )


@router.get("/{session_id}/guidance", response_model=GuidanceResult)
def get_guidance(
    session_id: str,
    request: Request,
    level: Optional[str] = None,
):
    return request.app.state.guidance_backend.get_guidance(
        session_id,
        _parse_level(level),
    )


@router.post(
    "/{session_id}/guidance/feedback",
    response_model=GuidanceResult,
)
def submit_feedback(
    session_id: str,
    feedback: FeedbackRequest,
    request: Request,
):
    try:
        return request.app.state.guidance_backend.submit_feedback(
            session_id,
            feedback,
        )
    except FeedbackConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@playground_router.get(
    "/presets/{widget_id}",
    response_model=PlaygroundPreset,
)
def get_playground_preset(widget_id: str, request: Request):
    _require_playground(request)
    try:
        return request.app.state.guidance_backend.get_playground_preset(widget_id)
    except PlaygroundConfigError as error:
        raise HTTPException(status_code=404, detail=error.detail()) from error


@playground_router.post(
    "/runs",
    response_model=PlaygroundRunResult,
)
def create_playground_run(payload: PlaygroundRunRequest, request: Request):
    _require_playground(request)
    try:
        session_id = request.app.state.guidance_backend.create_playground_session(
            payload.widget_id,
            payload.entrypoint,
            payload.documents,
        )
    except PlaygroundConfigError as error:
        raise HTTPException(status_code=422, detail=error.detail()) from error
    return {
        "widgetId": payload.widget_id,
        "sessionId": session_id,
    }


def create_app(
    backend: Optional[GuidanceWidgetBackend] = None,
    allowed_origins=None,
    playground_enabled: Optional[bool] = None,
) -> FastAPI:
    instance = FastAPI(title="Lotse GuidanceWidgets Backend")
    instance.state.guidance_backend = (
        backend or GuidanceWidgetBackend.from_environment()
    )
    instance.state.playground_enabled = (
        _playground_enabled()
        if playground_enabled is None
        else playground_enabled
    )
    instance.add_middleware(
        CORSMiddleware,
        allow_origins=(
            _allowed_origins()
            if allowed_origins is None
            else allowed_origins
        ),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type"],
    )
    instance.include_router(router)
    instance.include_router(playground_router)

    @instance.exception_handler(PlaygroundSessionExpired)
    async def expired_playground_session(request, error):
        return JSONResponse(status_code=410, content={"detail": str(error)})

    @instance.get("/health")
    def health():
        return {"status": "ok"}

    return instance


def _require_playground(request: Request):
    if not request.app.state.playground_enabled:
        raise HTTPException(status_code=404, detail="Playground is disabled")


def _playground_enabled():
    return os.getenv("LOTSE_PLAYGROUND_ENABLED") == "1"


def _parse_level(value: Optional[str]):
    if value is None or value == "adapt":
        return value
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = -1
    if level not in {0, 1, 2, 3}:
        raise HTTPException(
            status_code=422,
            detail="level must be adapt, 0, 1, 2, or 3",
        )
    return level


def _allowed_origins():
    configured = os.getenv("LOTSE_ALLOWED_ORIGINS")
    if not configured:
        return ["http://127.0.0.1:5174", "http://localhost:5174"]
    return [
        origin.strip()
        for origin in configured.split(",")
        if origin.strip()
    ]

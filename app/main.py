"""FastAPI application and HTTP routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import service
from .database import init_db
from .models import (
    AssemblyOut,
    FragmentIn,
    FragmentOut,
    PublicationOut,
    WithdrawalIn,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Seismic Waveform Assembly API",
    version="1.0.0",
    description=(
        "Ingest out-of-order waveform fragments, assemble them into whole "
        "UTC-minute records and publish immutable minutes for phase pickers."
    ),
    lifespan=lifespan,
)


@app.exception_handler(service.ServiceError)
async def _service_error_handler(_: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


class UploadOut(FragmentOut):
    status: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/fragments", response_model=UploadOut, status_code=201)
def upload_fragment(frag: FragmentIn) -> dict[str, Any]:
    return service.upload_fragment(frag.model_dump())


@app.get("/fragments", response_model=list[FragmentOut])
def list_fragments(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> list[dict[str, Any]]:
    return service.list_fragments(station, minute_utc)


@app.get("/assembly", response_model=AssemblyOut)
def get_assembly(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> dict[str, Any]:
    return service.get_assembly(station, minute_utc)


@app.post("/publications", response_model=PublicationOut, status_code=201)
def publish_minute(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(description="UTC minute, e.g. 2026-11-17T05:06:00Z"),
) -> dict[str, Any]:
    return service.publish_minute(station, minute_utc)


@app.get("/publications", response_model=list[PublicationOut])
def list_publications(
    station: str | None = Query(default=None),
    minute_utc: datetime | None = Query(default=None),
) -> list[dict[str, Any]]:
    return service.list_publications(station, minute_utc)


@app.get("/supply", response_model=list[PublicationOut])
def current_supply(
    station: str | None = Query(default=None),
    minute_utc: datetime | None = Query(default=None),
) -> list[dict[str, Any]]:
    """What is currently supplied: effective publications only.

    Withdrawn or stale publications never appear here, though they remain
    fully readable through the publication endpoints.
    """
    return service.list_supply(station, minute_utc)


@app.post(
    "/publications/{pub_id}/withdrawal",
    response_model=PublicationOut,
)
def withdraw_publication(pub_id: int, body: WithdrawalIn, http_response: Response) -> dict[str, Any]:
    """Stop supplying one effective publication (idempotent per id + reason).

    First withdrawal of an id returns 201; repeating the same id with the same
    reason is an idempotent retry and returns 200 with the original withdrawal.
    """
    status_code, result = service.withdraw_publication(pub_id, body.reason)
    http_response.status_code = status_code
    return result


@app.get("/publications/latest", response_model=PublicationOut)
def get_latest_publication(
    station: str = Query(min_length=1),
    minute_utc: datetime = Query(...),
) -> JSONResponse | dict[str, Any]:
    result = service.get_latest_publication(station, minute_utc)
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "publication not found"})
    return result


@app.get("/publications/{pub_id}", response_model=PublicationOut)
def get_publication(pub_id: int) -> JSONResponse | dict[str, Any]:
    result = service.get_publication(pub_id)
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "publication not found"})
    return result

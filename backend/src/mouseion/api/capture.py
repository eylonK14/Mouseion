"""Phone capture helpers: share-sheet normalization and one-time pairing."""

from __future__ import annotations

import base64
import io
import sqlite3

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from mouseion.config import Settings, get_settings
from mouseion.db import get_db
from mouseion.services.pairing import PairingTokenError, consume_pairing_token, create_pairing_token
from mouseion.services.sharing import extract_shared_url

router = APIRouter(prefix="/api", tags=["capture"])


class SharedContentIn(BaseModel):
    url: str = Field(default="", max_length=20_000)
    text: str = Field(default="", max_length=100_000)
    title: str = Field(default="", max_length=10_000)


class SharedContentOut(BaseModel):
    url: str


class PairingCreatedOut(BaseModel):
    pair_url: str
    qr_data_url: str
    expires_at: str


class PairingConsumeIn(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class PairingConsumedOut(BaseModel):
    api_token: str


@router.post("/share/extract", response_model=SharedContentOut)
def extract_share(body: SharedContentIn) -> SharedContentOut:
    try:
        url = extract_shared_url(shared_url=body.url, text=body.text, title=body.title)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return SharedContentOut(url=url)


@router.post("/pairing", response_model=PairingCreatedOut, status_code=status.HTTP_201_CREATED)
def create_pairing(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PairingCreatedOut:
    pairing = create_pairing_token(conn, ttl_minutes=settings.pairing_token_ttl_minutes)
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    pair_url = f"{base}/pair?token={pairing.token}"
    image = qrcode.make(pair_url, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return PairingCreatedOut(
        pair_url=pair_url,
        qr_data_url=qr_data_url,
        expires_at=pairing.expires_at,
    )


@router.post("/pairing/consume", response_model=PairingConsumedOut)
def consume_pairing(
    body: PairingConsumeIn,
    conn: sqlite3.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PairingConsumedOut:
    try:
        consume_pairing_token(conn, body.token)
    except PairingTokenError as exc:
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc
    if not settings.api_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_TOKEN is not configured")
    return PairingConsumedOut(api_token=settings.api_token)

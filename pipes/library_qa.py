"""
title: 📚 Paper Library
description: Grounded questions over the whole Mouseion library or a topic subtree.
version: 1.0.0
requirements: httpx
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from pydantic import BaseModel, Field

MOUNT = Path("/app/backend/data/mouseion-pipes")
if MOUNT.is_dir() and str(MOUNT) not in sys.path:
    sys.path.insert(0, str(MOUNT))


class Pipe:
    class Valves(BaseModel):
        MOUSEION_API_BASE_URL: str = Field(
            default="http://api:8000", description="Mouseion API URL on the compose network"
        )
        MOUSEION_API_TOKEN: str = Field(
            default="", description="Bearer token matching Mouseion API_TOKEN"
        )

    def __init__(self) -> None:
        self.name = "📚 Paper Library"
        self.valves = self.Valves()

    async def pipe(self, body: dict):
        import common

        runtime = importlib.reload(common)
        async for token in runtime.collection_response(
            body,
            api_base_url=self.valves.MOUSEION_API_BASE_URL,
            token=self.valves.MOUSEION_API_TOKEN,
        ):
            yield token

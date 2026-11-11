from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class OpenRequest(BaseModel):
    side: Optional[str] = Field(default=None)
    stop: Optional[str | float | int] = Field(default=None)
    symbol: Optional[str] = Field(default=None)


class WatchlistAddRequest(BaseModel):
    symbol: str


class ErrorBody(BaseModel):
    status: str = "REJECT"
    reason_code: str
    reason_zh: str


class IndicatorRequest(BaseModel):
    symbol: Optional[str] = None
    interval: Optional[str] = None
    source: str
    side: Optional[str] = None

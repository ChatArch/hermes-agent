"""Media proxy errors must not reveal signed URLs or hide programming errors."""

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from hermes_cli.web_routers import files


class _Client:
    def __init__(self, *args, error=None, **kwargs):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        raise self.error


def test_remote_image_request_error_is_generic_and_does_not_expose_query(monkeypatch):
    signed = "https://v3.fal.media/image.png?token=private-sample"
    monkeypatch.setattr(files, "_require_token", lambda request: None)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Client(
        error=httpx.ConnectError(f"GET {signed} failed")))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(files.proxy_remote_media(signed, object()))
    assert raised.value.status_code == 502
    assert "private-sample" not in raised.value.detail


def test_remote_image_programming_error_propagates(monkeypatch):
    monkeypatch.setattr(files, "_require_token", lambda request: None)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _Client(
        error=RuntimeError("client bug")))
    with pytest.raises(RuntimeError, match="client bug"):
        asyncio.run(files.proxy_remote_media("https://v3.fal.media/image.png", object()))

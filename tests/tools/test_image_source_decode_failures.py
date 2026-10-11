"""Data URL input errors must not hide an unexpected decoder failure."""

import asyncio
import base64

import pytest

from tools import image_source


@pytest.mark.parametrize("payload", ["%%%", "☃"])
def test_invalid_base64_remains_an_image_source_error(payload):
    with pytest.raises(image_source.NotAnImage, match="invalid base64"):
        asyncio.run(image_source.resolve_image_source(
            f"data:image/png;base64,{payload}", image_source.ResolveContext()))


def test_unexpected_decoder_failure_is_not_misreported_as_invalid_input(monkeypatch):
    fault = RuntimeError("decoder subsystem unavailable")
    payload = base64.b64encode(b"sample").decode("ascii")

    def unavailable(*args, **kwargs):
        raise fault

    monkeypatch.setattr(image_source.base64, "b64decode", unavailable)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(image_source.resolve_image_source(
            f"data:image/png;base64,{payload}", image_source.ResolveContext()))
    assert error.value is fault

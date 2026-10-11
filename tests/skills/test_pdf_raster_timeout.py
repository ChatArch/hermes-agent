"""Execute the pdftoppm fallback contract without launching an external converter."""
import importlib.util
from pathlib import Path
import subprocess
import traceback
from types import SimpleNamespace

import pytest
from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[2] / "skills/productivity/pdf/scripts"
_PASSWORD = "fake-pdf-password-canary"


@pytest.fixture
def raster():
    spec = importlib.util.spec_from_file_location("pdf_raster_timeout_test", SCRIPTS / "_raster.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_success_keeps_rgb_image_and_uses_finite_single_page_budget(raster, monkeypatch):
    captured = {}

    def converted(cmd, **kwargs):
        captured.update(kwargs)
        captured["directory"] = Path(cmd[-1]).parent
        Image.new("L", (2, 3), 240).save(cmd[-1] + "-1.png")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(raster.subprocess, "run", converted)
    image = raster._via_pdftoppm("document.pdf", 1, 150, None)
    assert image.mode == "RGB" and image.size == (2, 3)
    assert image.getpixel((0, 0)) == (240, 240, 240)
    assert captured.get("timeout") == 60
    assert not captured["directory"].exists()


def test_timeout_is_a_clean_value_error_without_password_chain_or_partial_image(raster, monkeypatch):
    directories = []

    def timed_out(cmd, **kwargs):
        directories.append(Path(cmd[-1]).parent)
        Image.new("RGB", (1, 1)).save(cmd[-1] + "-1.png")
        raise subprocess.TimeoutExpired(cmd, 60, output=_PASSWORD, stderr=_PASSWORD)

    monkeypatch.setattr(raster.subprocess, "run", timed_out)
    with pytest.raises(ValueError, match="pdftoppm timed out after 60 seconds") as caught:
        raster._via_pdftoppm("document.pdf", 1, 150, _PASSWORD)
    exc = caught.value
    assert exc.__cause__ is None and exc.__context__ is None
    assert _PASSWORD not in str(exc)
    assert _PASSWORD not in "".join(traceback.format_exception(exc))
    assert directories and all(not directory.exists() for directory in directories)


def test_existing_nonzero_exit_remains_a_value_error(raster, monkeypatch):
    monkeypatch.setattr(raster.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=2, stderr="invalid PDF"))
    with pytest.raises(ValueError, match="pdftoppm failed: invalid PDF"):
        raster._via_pdftoppm("document.pdf", 1, 150, None)


def test_existing_missing_page_image_remains_a_value_error(raster, monkeypatch):
    monkeypatch.setattr(raster.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stderr=""))
    with pytest.raises(ValueError, match="out of range"):
        raster._via_pdftoppm("document.pdf", 1, 150, None)

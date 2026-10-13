"""Stream timing for the native handoff families, not per-poll diagnostics."""

import pytest

from tests.scripts.desktop_update.windows_handoff_support import instrument_handoff_phases


@pytest.fixture(autouse=True)
def _handoff_phase_diagnostics(request, monkeypatch):
    with instrument_handoff_phases(request, monkeypatch):
        yield

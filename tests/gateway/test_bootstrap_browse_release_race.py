"""A browse holder can release between the boot reserve and its ownership probe."""
import pytest


@pytest.mark.platforms("posix")
def test_boot_retries_when_the_conflicting_holder_has_already_released(tmp_path, monkeypatch):
    from gateway import run_bootstrap as bootstrap
    from gateway.runtime_ownership import ProfileOwnership

    home = tmp_path / "home"
    holder = ProfileOwnership()
    holder.reserve([home], purpose="browse_schema_reconcile")
    booting = ProfileOwnership()
    real_reserve = booting.reserve
    attempts = []

    def release_after_refused_reserve(homes):
        attempts.append(tuple(homes))
        if len(attempts) == 1:
            try:
                real_reserve(homes)
            finally:
                holder.close()
        return real_reserve(homes)

    monkeypatch.setattr(booting, "reserve", release_after_refused_reserve)
    try:
        bootstrap._reserve_waiting_out_browse_reconcile(booting, [home])
        assert booting.owns(home)
        assert len(attempts) == 2
    finally:
        holder.close()
        booting.close()

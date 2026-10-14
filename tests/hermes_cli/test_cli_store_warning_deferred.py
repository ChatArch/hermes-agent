"""A deferred store warning must retain the cause after exception cleanup."""

from types import SimpleNamespace


def test_deferred_store_warning_keeps_failure_details(monkeypatch):
    import cli
    import hermes_state_registry
    import hermes_cli.cli_init_mixin as init
    import gateway.warning_notifications as notifications
    from hermes_cli.cli_init_mixin import CLIInitMixin

    rendered = []
    deferred = []

    def fail_open():
        raise RuntimeError("bounded store failure")

    monkeypatch.setattr(hermes_state_registry, "acquire", fail_open)
    monkeypatch.setattr(cli, "_run_state_db_auto_maintenance", lambda *_: None)
    monkeypatch.setattr(cli, "_run_checkpoint_auto_maintenance", lambda: None)
    monkeypatch.setattr(notifications, "render_notification", lambda callback, **kw: deferred.append(callback))
    monkeypatch.setattr(init, "Console", lambda **kw: SimpleNamespace(print=lambda value: rendered.append(value)))
    monkeypatch.setattr("agent.i18n.t", lambda key, **kw: (key, kw))

    instance = SimpleNamespace()
    CLIInitMixin._init_session_store(instance)
    assert instance._session_db is None and instance._session_db_unavailable
    assert len(deferred) == 1
    deferred[0]()  # Invoke after `except` has exited and its `e` binding is gone.
    assert rendered and rendered[0][0] == "cli.session_store.unavailable_rich"
    assert "bounded store failure" in rendered[0][1]["details"]

"""launchd bootstrap readiness and quiet unloaded-job handling."""

import os
import subprocess
from types import SimpleNamespace

import pytest

pwd = pytest.importorskip("pwd")
grp = pytest.importorskip("grp")

import hermes_cli.gateway as gateway_cli


class TestLaunchdUnloadedJobStderrStaysOffTerminal:
    """#106273: against an UNLOADED job, ``launchctl bootout`` / ``kickstart -k`` exit 3 and print
    ``Could not find service ...`` / ``Boot-out failed: 3`` on fd 2. Those exits are the *handled*
    case, so a real launchctl child (fake binary on PATH, real fd inheritance) must leave the
    terminal's stderr empty while the CLI's own status lines print. ``capfd`` reads fd 2, so an
    inherited-stderr regression fires even though ``subprocess.run`` never touches ``sys.stderr``."""

    @pytest.fixture
    def fake_launchctl(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "launchctl"
        # bootout exits $FAKE_BOOTOUT_RC (default 3 = unloaded); `kickstart -k` is the unloaded 3;
        # bootstrap / plain kickstart / print succeed. Every invocation is logged for the caller.
        script.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$FAKE_LAUNCHCTL_LOG"\n'
            'case "$1" in\n'
            '  bootout) echo "Boot-out failed: ${FAKE_BOOTOUT_RC:-3}: No such process" >&2; exit "${FAKE_BOOTOUT_RC:-3}" ;;\n'
            '  kickstart) if [ "$2" = "-k" ]; then echo "Could not find service in domain for user gui: 501" >&2; exit 3; fi ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        log = tmp_path / "calls.log"
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_LAUNCHCTL_LOG", str(log))
        monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
        monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: tmp_path / "ai.hermes.gateway.plist")
        monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)
        monkeypatch.setattr(gateway_cli, "_mark_planned_stop", lambda *a, **k: None)
        monkeypatch.setattr(gateway_cli, "_wait_for_gateway_exit", lambda *a, **k: True)
        monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: None)
        return log

    def test_restart_of_unloaded_job_reloads_without_launchctl_noise(self, fake_launchctl, capfd):
        gateway_cli.launchd_restart()

        out, err = capfd.readouterr()
        assert "↻ launchd job was unloaded; reloading" in out
        assert "✓ Service restarted" in out
        assert err == ""
        verbs = [line.split()[0] for line in fake_launchctl.read_text(encoding="utf-8").splitlines()]
        assert verbs == ["kickstart", "bootout", "bootstrap", "kickstart"]

    def test_stop_of_unloaded_job_is_quiet_but_a_real_bootout_failure_keeps_stderr(
        self, fake_launchctl, capfd, monkeypatch
    ):
        gateway_cli.launchd_stop()
        out, err = capfd.readouterr()
        assert "✓ Service stopped" in out
        assert err == ""

        # Unexpected exit code: not swallowed, and the captured stderr rides on the exception for
        # the caller's diagnostic instead of having been printed raw by launchctl.
        monkeypatch.setenv("FAKE_BOOTOUT_RC", "1")
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            gateway_cli.launchd_stop()
        assert excinfo.value.returncode == 1
        assert "Boot-out failed: 1" in excinfo.value.stderr
        assert capfd.readouterr().err == ""


class TestRetryLaunchctlBootstrapUntilRegistered:
    """`_retry_launchctl_bootstrap_until_registered` — salvage of #53277.

    Covers the three review findings the salvage hardens: retry until the
    label is actually LISTED (not just a zero bootstrap exit), TimeoutExpired
    is retried (not escaped leaving the service unloaded), and the retry is
    bounded by a wall-clock deadline rather than a fixed short window.
    """

    DOMAIN = "gui/501"
    PLIST = "/tmp/ai.hermes.gateway.plist"
    LABEL = "ai.hermes.gateway"

    # `launchctl list <label>` output for a job launchd is actively running.
    # Success requires a PID here, not just exit 0 — exit 0 alone also covers a
    # registered-but-not-running definition (macOS 26+ `state = not running`).
    RUNNING_LIST_OUTPUT = '{\n\t"PID" = 4242;\n\t"Label" = "ai.hermes.gateway";\n};'

    def test_returns_true_once_label_is_registered(self, monkeypatch):
        """Success requires launchctl list to confirm a supervised process, not
        just a zero bootstrap exit."""
        list_results = iter([1, 0])  # first check: not registered, second: registered

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "list"]:
                rc = next(list_results)
                return SimpleNamespace(
                    returncode=rc,
                    stdout=self.RUNNING_LIST_OUTPUT if rc == 0 else "",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli.time, "sleep", lambda *_a, **_k: None)

        ok = gateway_cli._retry_launchctl_bootstrap_until_registered(
            self.DOMAIN, self.PLIST, self.LABEL,
            deadline=gateway_cli.time.monotonic() + 60,
        )
        assert ok is True

    def test_timeout_expired_is_retried_not_escaped(self, monkeypatch):
        """A bootstrap that times out must be retried — it leaves the service
        unloaded, so it must not escape the retry/log path (finding #2)."""
        attempts = {"bootstrap": 0}

        def fake_run(cmd, check=False, **kwargs):
            if cmd[1] == "bootstrap":
                attempts["bootstrap"] += 1
                if attempts["bootstrap"] == 1:
                    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["launchctl", "list"]:
                # registered only after the second (successful) bootstrap
                ok = attempts["bootstrap"] >= 2
                return SimpleNamespace(
                    returncode=0 if ok else 1,
                    stdout=self.RUNNING_LIST_OUTPUT if ok else "",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli.time, "sleep", lambda *_a, **_k: None)

        ok = gateway_cli._retry_launchctl_bootstrap_until_registered(
            self.DOMAIN, self.PLIST, self.LABEL,
            deadline=gateway_cli.time.monotonic() + 60,
        )
        assert ok is True
        assert attempts["bootstrap"] >= 2  # the timeout was retried, not raised
    def test_registered_but_not_running_is_not_success(self, monkeypatch):
        """A definition with no PID must not end the loop.

        `launchctl list` exits 0 for a registered-but-not-running job (macOS
        26+ `state = not running`), so exit-0 alone would report success for a
        gateway launchd is not actually running. Verified against live launchd
        on 2026-08-05.
        """
        list_calls = {"n": 0}

        def fake_run(cmd, check=False, **kwargs):
            if cmd[:2] == ["launchctl", "list"]:
                list_calls["n"] += 1
                # Registered (exit 0) but no PID line — never running.
                return SimpleNamespace(
                    returncode=0,
                    stdout='{\n\t"Label" = "ai.hermes.gateway";\n};',
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(gateway_cli.time, "sleep", lambda *_a, **_k: None)

        ok = gateway_cli._retry_launchctl_bootstrap_until_registered(
            self.DOMAIN, self.PLIST, self.LABEL,
            deadline=gateway_cli.time.monotonic() - 1,  # already expired
        )
        assert ok is False
        assert list_calls["n"] >= 1

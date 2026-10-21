"""Isolated lifecycle checks; never contact the installed gateway or database."""
import contextlib
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import pytest
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.platforms("macos")


class CycleLifecycle(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_root(self, tmp_path):
        self.temp_root = tmp_path

    def scenario(self, *, busy=False, apply_failure=False, restart_failure=False, drain=True, prior=False):
        spec = importlib.util.spec_from_file_location("cycle_under_test", ROOT / "scripts/maintenance/run_retention_cycle.py")
        cycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cycle)
        with tempfile.TemporaryDirectory(dir=self.temp_root) as temporary:
            home = Path(temporary)
            state_path = home / "backups/session-retention/cycle-state.json"
            state_path.parent.mkdir(parents=True)
            manifest = home / "manifest.json"
            manifest.write_text(json.dumps({"cutoff": (datetime.now(UTC) - timedelta(days=85)).isoformat()}))
            if prior:
                state_path.write_text(json.dumps({"manifest": str(manifest), "attempt_month": datetime.now().strftime("%Y-%m")}))
            marker = home / ".drain_request.json"
            control = types.ModuleType("gateway.drain_control")
            control.drain_request_path = lambda home: marker
            control.write_drain_request = lambda **kwargs: marker.write_text(json.dumps({"principal": cycle.PRINCIPAL}))
            control.read_drain_request = lambda **kwargs: json.loads(marker.read_text()) if marker.exists() else None
            control.clear_drain_request = lambda **kwargs: marker.unlink(missing_ok=True)
            calls = []
            def invoke(arguments):
                calls.append(arguments)
                if arguments[0] == "apply" and apply_failure:
                    raise RuntimeError("fixture apply failed")
            status = {"pid": 123, "answering_pid": 123, "gateway_state": "running", "active_agents": int(busy)}
            restart = RuntimeError("fixture restart failed") if restart_failure else 456
            with contextlib.ExitStack() as stack:
                for name, value in (("HOME", home), ("STATE", state_path)):
                    stack.enter_context(patch.object(cycle, name, value))
                stack.enter_context(patch.dict(sys.modules, {"gateway.drain_control": control}))
                stack.enter_context(patch.object(sys, "argv", ["cycle", "--manifest", str(manifest)]))
                stack.enter_context(patch.object(cycle.os, "nice"))
                stack.enter_context(patch.object(cycle.signal, "signal"))
                stack.enter_context(patch.object(cycle.signal, "alarm"))
                kill = stack.enter_context(patch.object(cycle, "stop_gateway"))
                stack.enter_context(patch.object(cycle, "gateway_status", return_value=status))
                stack.enter_context(patch.object(cycle, "validate_service"))
                stack.enter_context(patch.object(cycle, "wait_for_drain", return_value={**status, "gateway_state": "draining"} if drain else None))
                stack.enter_context(patch.object(cycle, "live", return_value=False))
                restarted = stack.enter_context(patch.object(cycle, "restart_gateway", side_effect=restart if isinstance(restart, Exception) else None, return_value=restart))
                stack.enter_context(patch.object(cycle.runpy, "run_path", return_value={"main": invoke, "offline": lambda db: None}))
                stack.enter_context(patch.object(cycle, "cleanup_routes", return_value=2))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                raised = None
                try:
                    cycle.main()
                except RuntimeError as error:
                    raised = str(error)
            return json.loads(state_path.read_text()), calls, kill.call_count, restarted.call_count, raised, marker.exists()

    def test_busy_skips_and_keeps_manifest_for_retry(self):
        state, calls, kills, restarts, error, marker = self.scenario(busy=True, prior=True)
        self.assertEqual(state["phase"], "skipped_gateway_busy")
        self.assertIn("manifest", state)
        self.assertEqual((calls, kills, restarts, error, marker), ([], 0, 0, None, False))

    def test_verified_manifest_reused_and_fixed_ninety_day_cutoff(self):
        state, calls, kills, restarts, error, marker = self.scenario()
        self.assertEqual([call[0] for call in calls], ["verify", "apply"])
        cutoff = datetime.fromisoformat(calls[1][calls[1].index("--cutoff") + 1])
        self.assertLessEqual(cutoff, datetime.now(UTC) - timedelta(days=90))
        self.assertEqual((state["phase"], kills, restarts, error, marker), ("complete", 1, 1, None, False))

    def test_apply_failure_always_restarts_without_marking_month_complete(self):
        state, calls, kills, restarts, error, marker = self.scenario(apply_failure=True)
        self.assertEqual(state["phase"], "failed")
        self.assertNotIn("completed_month", state)
        self.assertEqual([call[0] for call in calls], ["verify", "apply"])
        self.assertEqual((kills, restarts, error, marker), (1, 1, "fixture apply failed", False))

    def test_restart_failure_is_not_reported_complete(self):
        state, calls, kills, restarts, error, marker = self.scenario(restart_failure=True)
        self.assertEqual(state["phase"], "failed_gateway_restart")
        self.assertNotIn("completed_month", state)
        self.assertEqual(error, "fixture restart failed")
        self.assertEqual(([call[0] for call in calls], kills, restarts, marker), (["verify", "apply"], 1, 1, False))

    def test_unacknowledged_drain_never_stops_gateway(self):
        state, calls, kills, restarts, error, marker = self.scenario(drain=False)
        self.assertEqual(state["phase"], "cleanup_deferred_drain_not_ready")
        self.assertEqual([call[0] for call in calls], ["verify"])
        self.assertEqual((kills, restarts, error, marker), (0, 0, None, False))

    def test_restart_retries_transient_launchctl_errors(self):
        spec = importlib.util.spec_from_file_location("cycle_restart_test", ROOT / "scripts/maintenance/run_retention_cycle.py")
        cycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cycle)
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        results = [
            subprocess.TimeoutExpired("launchctl", 10),
            subprocess.CompletedProcess([], 1),
            subprocess.CompletedProcess([], 0, stderr=""),
            OSError("fixture transient print failure"),
            subprocess.CompletedProcess([], 0, stdout="pid = 999\n"),
            subprocess.CompletedProcess([], 0, stdout="pid = 456\n"),
        ]
        status = {"pid": 456, "gateway_state": "running", "platforms": {
            "feishu": {"state": "connected", "writer_pid": 456}}}
        with patch.object(cycle.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(cycle.time, "sleep", side_effect=sleep), \
                patch.object(cycle, "gateway_status", return_value=status), \
                patch.object(cycle.subprocess, "run", side_effect=results) as commands:
            self.assertEqual(cycle.restart_gateway(previous_pid=123), 456)
        self.assertEqual(commands.call_count, 6)
        self.assertIn("bootstrap", commands.call_args_list[2].args[0])


if __name__ == "__main__":
    unittest.main()

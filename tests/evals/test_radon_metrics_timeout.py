"""Behavioral coverage for bounded Radon subprocesses; no benchmark import."""
import json
import os
import subprocess
import sys

import pytest

from evals.codebase_navigability import radon_metrics


CC = {"pkg/mod.py": [
    {"name": "a", "complexity": 1},
    {"name": "b", "complexity": 20},
    {"name": "c", "complexity": 40},
    {"name": "d", "complexity": 60},
    {"name": "ignored"}, "ignored"], "error.py": {"error": "ignored"}}
MI = {"a.py": {"mi": 5}, "b.py": {"mi": 15}, "c.py": {"mi": 80},
      "error.py": {"error": "ignored"}, "ignored.py": []}
CC_METRICS = {"cc_blocks": 4, "cc_avg": 30.25, "cc_p95": 40, "cc_max": 60,
              "cc_gt_15": 3, "cc_gt_30": 2, "cc_gt_50": 1,
              "cc_worst": [(60, "pkg/mod.py", "d"), (40, "pkg/mod.py", "c"),
                           (20, "pkg/mod.py", "b"), (1, "pkg/mod.py", "a")]}
MI_METRICS = {"mi_files": 3, "mi_avg": 33.33, "mi_lt_20": 2, "mi_lt_10": 1}


def fake_radon(monkeypatch, timed_out=(), payloads=None, returncode=0):
    calls = []
    payloads = payloads or {"cc": json.dumps(CC), "mi": json.dumps(MI)}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        phase = command[1]
        if phase in timed_out:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 120),
                                            output=payloads[phase], stderr="partial-secret")
        return subprocess.CompletedProcess(command, returncode,
                                           stdout=payloads[phase], stderr="ignored")

    monkeypatch.setattr(radon_metrics.subprocess, "run", run)
    monkeypatch.setattr(radon_metrics.shutil, "which", lambda name: "/fake/radon")
    return calls


def test_json_aggregation_and_roots_are_preserved(monkeypatch, tmp_path):
    calls = fake_radon(monkeypatch, returncode=9)
    files = [tmp_path / "pkg" / "mod.py", tmp_path / "tests" / "test_a.py",
             tmp_path / "z.py", tmp_path / "pkg" / "other.py"]
    assert radon_metrics.radon(files, str(tmp_path)) == CC_METRICS | MI_METRICS
    assert [c[0] for c in calls] == [
        ["/fake/radon", "cc", "-j", "-s", "-e", "tests/*,tests/**", "pkg", "z.py"],
        ["/fake/radon", "mi", "-j", "-e", "tests/*,tests/**", "pkg", "z.py"]]
    for _, kwargs in calls:
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False


@pytest.mark.parametrize("timed_out", [("cc",), ("mi",), ("cc", "mi")],
                         ids=["cc-timeout-mi-continues", "mi-timeout-cc-retained", "both-timeout"])
def test_timeout_is_isolated_and_discards_partial_output(monkeypatch, tmp_path, timed_out):
    calls = fake_radon(monkeypatch, timed_out=timed_out)
    actual = radon_metrics.radon([tmp_path / "pkg" / "mod.py"], str(tmp_path))
    expected = {}
    for phase, metrics in [("cc", CC_METRICS), ("mi", MI_METRICS)]:
        if phase in timed_out:
            expected[phase + "_error"] = "timeout after 120s"
        else:
            expected.update(metrics)
    assert actual == expected
    assert [command[1] for command, _ in calls] == ["cc", "mi"]
    assert [kwargs["timeout"] for _, kwargs in calls] == [120, 120]
    assert "partial-secret" not in repr(actual)


def test_each_phase_gets_its_own_finite_budget(monkeypatch, tmp_path):
    calls = fake_radon(monkeypatch)
    assert radon_metrics.radon([], str(tmp_path)) == CC_METRICS | MI_METRICS
    # No shared/decremented budget, including when the first phase succeeds.
    assert [kwargs.get("timeout") for _, kwargs in calls] == [120, 120]


@pytest.mark.parametrize("phase", ["cc", "mi"])
@pytest.mark.parametrize("bad_json", ["not-json", "[]", "null"])
def test_parse_errors_keep_original_repr_and_other_phase(monkeypatch, tmp_path, phase, bad_json):
    payloads = {"cc": json.dumps(CC), "mi": json.dumps(MI), phase: bad_json}
    fake_radon(monkeypatch, payloads=payloads)
    result = radon_metrics.radon([], str(tmp_path))
    try:
        json.loads(bad_json).values()
    except (json.JSONDecodeError, AttributeError) as error:
        assert result[phase + "_error"] == repr(error)[:200]
    other_metrics = MI_METRICS if phase == "cc" else CC_METRICS
    assert result == other_metrics | {phase + "_error": result[phase + "_error"]}


def test_empty_json_preserves_zero_aggregates(monkeypatch, tmp_path):
    fake_radon(monkeypatch, payloads={"cc": "{}", "mi": "{}"})
    assert radon_metrics.radon([], str(tmp_path)) == {
        "cc_blocks": 0, "cc_avg": 0.0, "cc_p95": 0, "cc_max": 0,
        "cc_gt_15": 0, "cc_gt_30": 0, "cc_gt_50": 0, "cc_worst": [],
        "mi_files": 0, "mi_avg": 0.0, "mi_lt_20": 0, "mi_lt_10": 0}


def test_executable_fallback_and_launch_failure_are_preserved(monkeypatch, tmp_path):
    calls = fake_radon(monkeypatch)
    monkeypatch.setattr(radon_metrics.shutil, "which", lambda name: None)
    radon_metrics.radon([], str(tmp_path))
    assert calls[0][0][0] == os.path.join(os.path.dirname(sys.executable), "radon")

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("missing radon")

    monkeypatch.setattr(radon_metrics.subprocess, "run", unavailable)
    with pytest.raises(FileNotFoundError, match="missing radon"):
        radon_metrics.radon([], str(tmp_path))


@pytest.mark.parametrize("entrypoint", ["script", "module"])
def test_static_cli_entrypoints_preserve_radon_prefix(tmp_path, entrypoint):
    tree = tmp_path / "tiny-tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "native.py").write_text("def native():\n    return 1\n", encoding="utf-8")
    (tree / "tests" / "test_native.py").write_text("def test_native():\n    assert True\n", encoding="utf-8")
    script = os.path.join(os.path.dirname(radon_metrics.__file__), "static_metrics.py")
    harness = r'''import json, os, runpy, subprocess, sys
real_run = subprocess.run

def native_stub(command, **kwargs):
    payload = {"native.py": [{"name": "native", "complexity": 12}]} if command[1] == "cc" else {"native.py": {"mi": 70}}
    return real_run([sys.executable, "-c", "print(" + repr(json.dumps(payload)) + ")"], **kwargs)

subprocess.run = native_stub
mode, script, tree, label = sys.argv[1:]
sys.argv = [script, tree, label]
if mode == "script":
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")
else:
    runpy.run_module("evals.codebase_navigability.static_metrics", run_name="__main__")
'''
    label = "native-" + entrypoint
    env = os.environ | {"NAV_OUT": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", harness, entrypoint, script, str(tree), label],
                          cwd=os.path.dirname(os.path.dirname(os.path.dirname(script))),
                          env=env, capture_output=True, text=True, check=False, timeout=20)
    assert proc.returncode == 0, proc.stderr
    result = json.loads((tmp_path / (label + ".static.json")).read_text(encoding="utf-8"))
    assert result["source"]["radon_cc_avg"] == 12
    assert result["source"]["radon_mi_avg"] == 70
    assert "cc_avg" not in result["source"]
    assert result["source"]["files"] == 1
    assert result["tests"]["files"] == 1


@pytest.mark.parametrize("phase", ["cc", "mi"])
def test_json_numeric_overflow_keeps_original_error(monkeypatch, tmp_path, phase):
    huge = 10 ** 1000
    data = {"cc": json.dumps(CC), "mi": json.dumps(MI)}
    data[phase] = json.dumps({"huge.py": [{"name": "huge", "complexity": huge}]} if phase == "cc" else {"huge.py": {"mi": huge}})
    fake_radon(monkeypatch, payloads=data)
    actual = radon_metrics.radon([], str(tmp_path))
    try:
        huge / 1
    except OverflowError as error:
        expected_error = repr(error)[:200]
    other = MI_METRICS if phase == "cc" else CC_METRICS
    assert actual == other | {phase + "_error": expected_error}

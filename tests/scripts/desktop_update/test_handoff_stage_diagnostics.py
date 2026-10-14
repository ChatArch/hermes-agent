"""Stage timings from already captured handoff output reveal cumulative Windows cost."""

import json

from tests.scripts.desktop_update import windows_handoff_support as support


def test_stage_timings_use_only_fixed_labels_and_elapsed_seconds():
    output = "\n".join([
        "2026-10-14T10:00:00Z claimed update marker (private path)",
        "2026-10-14T10:00:03Z hand-off start: root=private-path branch=private-branch",
        "2026-10-14T10:00:08Z update marker names its custodian pid 123 (hand-off pid 456)",
        "2026-10-14T10:00:13Z process cwd set to install root: private-path",
        "2026-10-14T10:00:16Z running: python private-arguments",
        "2026-10-14T10:00:24Z hermes update exit code: 0",
        "2026-10-14T10:00:29Z removed update marker (owned)",
    ])
    assert support._handoff_stage_spans("commit", output) == [
        {"handoff_stage": "claim-to-start", "family": "commit", "elapsed_s": 3},
        {"handoff_stage": "start-to-custodian", "family": "commit", "elapsed_s": 5},
        {"handoff_stage": "custodian-to-cwd", "family": "commit", "elapsed_s": 5},
        {"handoff_stage": "cwd-to-update", "family": "commit", "elapsed_s": 3},
        {"handoff_stage": "update-to-exit", "family": "commit", "elapsed_s": 8},
        {"handoff_stage": "exit-to-release", "family": "commit", "elapsed_s": 5},
    ]
    assert "private" not in json.dumps(support._handoff_stage_spans("commit", output))


def test_stage_timings_ignore_partial_reordered_and_untrusted_output():
    output = "\n".join([
        "2026-10-14T10:00:00Z claimed update marker",
        "PRIVATE 2026-10-14T10:00:01Z hand-off start: root=private-path",
        "2026-10-14T10:00:05Z hand-off start: root=private-path",
        "2026-10-14T10:00:04Z update marker names its custodian pid 123",
        "2026-10-14T10:00:12Z removed update marker (owned)",
        "2026-10-14T10:00:20Z malicious hand-off start: root=private-path",
    ])
    assert support._handoff_stage_spans("marker", output) == [
        {"handoff_stage": "claim-to-start", "family": "marker", "elapsed_s": 5},
        {"handoff_stage": "start-to-release", "family": "marker", "elapsed_s": 7},
    ]
    assert support._handoff_stage_spans("lineage", output) == []


def test_handoff_wrapper_retains_result_and_records_sanitized_stages(capsys):
    result = (object(), "2026-10-14T10:00:00Z hand-off start: private\n"
             "2026-10-14T10:00:04Z update marker names its custodian pid 123\n")
    wrapped = support.trace_handoff_phase(lambda: result, family="commit", step="handoff")
    assert wrapped() is result
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row.get("handoff_phase", row.get("handoff_stage")) for row in rows] == [
        "handoff", "start-to-custodian", "handoff",
    ]
    assert rows[1] == {"handoff_stage": "start-to-custodian", "family": "commit", "elapsed_s": 4}
    assert "private" not in json.dumps(rows)


def test_closed_stage_stream_keeps_original_result(monkeypatch):
    class Broken:
        def write(self, _value):
            raise BrokenPipeError("closed")

        def flush(self):
            raise BrokenPipeError("closed")

    result = (object(), "2026-10-14T10:00:00Z hand-off start: private\n"
             "2026-10-14T10:00:04Z update marker names its custodian pid 123\n")
    wrapped = support.trace_handoff_phase(lambda: result, family="commit", step="handoff")
    with monkeypatch.context() as scoped:
        scoped.setattr(support.sys, "stdout", Broken())
        assert wrapped() is result

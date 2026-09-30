"""A job reads another job's result only through a `needs` entry it declares.

GitHub rejects a job graph that references an unknown job or needs itself, so
cycles are the platform's own validation. What it does not fail closed on is a
result read from a job that was never declared: `needs.build-win32-release.result`
resolves to nothing when the result job's `needs` list only names itself, and the
dispatch burns its whole fan-out before anything reports a problem.
"""
import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
JOB_REFERENCE = re.compile(r"\bneeds\.([A-Za-z0-9_-]+)")


def _loaded():
    yaml = YAML(typ="base")
    return {
        path.name: yaml.load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    }


def _referenced_jobs(node, key=None):
    """Job names this job reads through `needs.` — the `if:` and every expression."""
    if isinstance(node, dict):
        for sub_key, value in node.items():
            yield from _referenced_jobs(value, str(sub_key))
    elif isinstance(node, list):
        for value in node:
            yield from _referenced_jobs(value, key)
    elif isinstance(node, str):
        # `run:` scripts may contain their own `needs.items()` — expressions only.
        yield from JOB_REFERENCE.findall(node) if key == "if" else ()
        for expression in EXPRESSION.finditer(node):
            yield from JOB_REFERENCE.findall(expression.group(1))


def test_jobs_only_read_declared_dependencies():
    violations = []
    for filename, workflow in _loaded().items():
        for name, job in ((workflow or {}).get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            declared = job.get("needs") or []
            if isinstance(declared, str):
                declared = [declared]
            for read in sorted(set(_referenced_jobs(job))):
                if read not in declared:
                    violations.append(f"{filename}: {name} reads '{read}' without needing it")
    assert not violations, "workflow job graph:\n" + "\n".join(violations)


def test_no_workflow_runs_from_a_tag_push():
    violations = []
    for filename, workflow in _loaded().items():
        triggers = (workflow or {}).get("on") or {}
        if not isinstance(triggers, dict) or "push" not in triggers:
            continue
        push = triggers["push"]
        # This nightly canary also gates releases with dedicated spend-capped keys.
        if filename == "live-providers.yml" and push == {"tags": ["v*"]}:
            continue
        if filename == "desktop-release.yml" and push == {"tags": ["v[0-9][0-9][0-9][0-9].*"]}:
            # ChatArch's legacy EXE/MSI release is an explicit CalVer-tag pipeline,
            # separate from upstream PM's workflow_dispatch stable release.
            assert "github.event_name == 'push'" in workflow["jobs"]["publish"]["if"]
            assert "github.repository == 'ChatArch/hermes-agent'" in workflow["jobs"]["publish"]["if"]
            continue
        if not isinstance(push, dict) or not ({"branches", "branches-ignore"} & set(push)):
            violations.append(filename)
        elif {"tags", "tags-ignore"} & set(push):
            violations.append(filename)
    assert not violations, "tag-triggered workflows: " + ", ".join(violations)


def test_automatic_canary_release_writes_only_in_official_repository():
    workflow = _loaded()["canary-release.yml"]
    assert "schedule" in workflow["on"]
    for name in ("tag", "prune"):
        job = workflow["jobs"][name]
        assert "github.repository == 'NousResearch/hermes-agent'" in job["if"]


def test_scheduled_skills_deploy_is_explicitly_upstream_only():
    workflow = _loaded()["skills-index.yml"]
    assert "schedule" in workflow["on"]
    assert "github.repository == 'NousResearch/hermes-agent'" in workflow["jobs"]["trigger-deploy"]["if"]


def test_upstream_autofix_does_not_create_or_auto_merge_fork_prs():
    workflow = _loaded()["js-autofix.yml"]
    assert "main" in workflow["on"]["push"]["branches"]
    for name in ("generate-patch", "apply-patch"):
        assert "github.repository == 'NousResearch/hermes-agent'" in workflow["jobs"][name]["if"]


def test_change_detection_has_headroom_for_large_upstream_sync_prs():
    workflow = _loaded()["ci.yaml"]
    # A completed classifier still gets cancelled if checkout + teardown hits
    # the one-minute job deadline; downstream matrix lanes then never run.
    assert int(workflow["jobs"]["detect"]["timeout-minutes"]) >= 5


def test_required_pr_workflows_use_available_standard_runners():
    for name in (
        "tests.yml", "tests-os.yml", "e2e-desktop-core.yml", "e2e-desktop-update.yml",
        "windows-install-update-e2e.yml", "install-e2e-windows-run.yml",
        "windows-bundle-sdk.yml", "pm-bundle.yml",
    ):
        workflow = _loaded()[name]
        assert "latest-32-core" not in str(workflow), name
        assert "latest-32-arm-core" not in str(workflow), name


def test_standard_windows_runner_bounds_native_process_tree_parallelism():
    workflow = _loaded()["tests-os.yml"]
    windows = next(row for row in workflow["jobs"]["os-tests"]["strategy"]["matrix"]["include"] if row["marker"] == "windows")
    assert windows["runner"] == "windows-latest"
    assert int(windows["timeout"]) >= 60
    workers = workflow["jobs"]["os-tests"]["steps"][-1]["env"]["HERMES_TEST_WORKERS"]
    assert "'8'" not in workers
    assert "'2'" in workers
    e2e = workflow["jobs"]["e2e-windows"]
    assert int(e2e["timeout-minutes"]) >= 60
    run = next(step for step in e2e["steps"] if step.get("name") == "Run Windows E2E suite")
    assert int(run["env"]["HERMES_TEST_WORKERS"]) <= 2


def test_standard_windows_bundle_runners_keep_both_native_architectures():
    sdk = _loaded()["windows-bundle-sdk.yml"]
    runners = sdk["jobs"]["windows-bundle-tools"]["strategy"]["matrix"]["runner"]
    assert set(runners) == {"windows-latest", "windows-11-arm"}
    assert len(runners) == 2
    pm = _loaded()["pm-bundle.yml"]
    targets = {row["label"]: row["runner"] for row in pm["jobs"]["bundle"]["strategy"]["matrix"]["target"]}
    assert targets["win32-x64"] == "windows-latest"
    assert targets["win32-arm64"] == "windows-11-arm"


def test_standard_e2e_runners_bound_process_tree_workers():
    python = _loaded()["tests.yml"]["jobs"]
    assert int(python["e2e"]["steps"][-1]["env"]["HERMES_TEST_WORKERS"]) <= 2
    assert int(python["e2e"]["timeout-minutes"]) >= 60
    assert int(python["e2e-upgrade"]["timeout-minutes"]) >= 90
    install = _loaded()["windows-install-update-e2e.yml"]["jobs"]["install-update"]
    assert int(install["timeout-minutes"]) >= 90
    run = next(step for step in install["steps"] if step.get("name") == "Run Windows install + update E2E")
    assert int(run["env"]["HERMES_TEST_WORKERS"]) <= 2

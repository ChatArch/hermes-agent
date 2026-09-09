"""SSH execution plane for browser-use; never launches on the gateway."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile

from hermes_constants import get_hermes_home
from tools.registry import tool_error, tool_result

# Executed by target Python, with target environment only. No uvx auto-install,
# Chrome discovery, personal-profile copying, or credential forwarding.
_REMOTE_RUNNER = r'''
import json, os, pathlib, re, shutil, subprocess, sys, time
request = json.load(sys.stdin)
def fail(kind, message):
    print(json.dumps({"success": False, "error_type": kind, "error": message}))
    raise SystemExit(0)
home = pathlib.Path.home()
cli = next((str(p) for p in (home / ".hermes/bin/browser-use", home / ".local/bin/browser-use") if p.is_file() and os.access(p, os.X_OK)), None) or shutil.which("browser-use")
if not cli:
    fail("remote_cli_missing", "browser-use CLI is not installed on the SSH target; install and verify it there. No local fallback was attempted.")
env = dict(os.environ)
for key in ("PYTHONPATH", "PYTHONHOME"):
    env.pop(key, None)
if not (env.get("BU_CDP_URL") or env.get("BU_CDP_WS")):
    fail("remote_endpoint_missing", "A private browser CDP endpoint must be provisioned on the SSH target (BU_CDP_URL/BU_CDP_WS). Automatic Chrome discovery and gateway launch are disabled.")
env["BU_AUTOSPAWN"] = "0"
env["ANONYMIZED_TELEMETRY"] = "false"
env["BU_NAME"] = request["daemon"]
workspace = pathlib.Path(env.get("BH_AGENT_WORKSPACE") or str(home / ".cache/hermes/browser-use/workspace" / request["scope"]))
workspace = workspace.expanduser().resolve()
workspace.mkdir(parents=True, exist_ok=True)
env["BH_AGENT_WORKSPACE"] = str(workspace)
# The CLI otherwise saves screenshots below ~/.config, which is intentionally
# excluded from SSH artifact export. Keep screenshots task-owned and IPC short.
env.setdefault("BH_TMP_DIR", str(workspace / "screenshots" / request["daemon"]))
env.setdefault("BH_RUNTIME_DIR", str(home / ".cache/hbu" / request["daemon"]))
for directory in (env["BH_TMP_DIR"], env["BH_RUNTIME_DIR"]):
    pathlib.Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
started = time.time()
try:
    proc = subprocess.run([cli], input=request["code"], text=True, capture_output=True, env=env, cwd=workspace, timeout=request["timeout"])
except subprocess.TimeoutExpired:
    fail("remote_browser_timeout", "Remote browser-use timed out; its daemon may still be working. Workspace is preserved.")
except OSError as exc:
    fail("remote_cli_launch_failed", str(exc))
result = {"success": proc.returncode == 0, "exit_code": proc.returncode, "output": proc.stdout[:50000], "workspace": str(workspace), "backend": "ssh"}
if proc.returncode and "hermes_browser_isolation_failed" in proc.stdout + proc.stderr:
    result.update(error_type="remote_browser_isolation_failed", error="Named-tab isolation failed; user code was not executed.")
if proc.stderr:
    result["stderr"] = proc.stderr[:4000]
for path in reversed(re.findall(r"(/[^\s\"']+?\.(?:png|jpe?g|webp))", proc.stdout, re.I)):
    try:
        p = pathlib.Path(path)
        if p.is_file() and p.stat().st_mtime >= started - 1:
            result["screenshot_path"] = path
            break
    except OSError:
        pass
print(json.dumps(result))
'''


def effective_browser_backend(task_id):
    from tools.terminal_tool import _get_env_config, apply_task_env_overrides, resolve_task_overrides
    return apply_task_env_overrides(_get_env_config(), resolve_task_overrides(task_id))["env_type"]


def browser_exec_ssh(code, session, timeout, task_id, local=False):
    from tools import browser_use_cli as browser
    from tools.file_tools import _get_file_ops
    from tools.terminal_tool import _resolve_container_task_id

    cfg = browser._read_browser_cfg()
    if local or cfg.get("use_real_profile"):
        return tool_error("Real-profile browser access cannot be moved to SSH implicitly. No profile was copied or local browser launched.", error_type="remote_real_profile_unsupported")
    provider = str(cfg.get("cloud_provider") or "").strip().lower()
    if (provider not in ("", "local") or cfg.get("use_gateway") or cfg.get("cdp_url")
            or any(os.getenv(k) for k in ("BU_CDP_URL", "BU_CDP_WS", "BROWSER_CDP_URL", "BROWSER_USE_API_KEY"))):
        return tool_error("Gateway cloud/CDP configuration cannot be reinterpreted on SSH. Provision an explicit target-side endpoint; no credentials were forwarded and no gateway browser was launched.", error_type="remote_endpoint_configuration_required")
    if session and not browser._SESSION_RE.fullmatch(session):
        return tool_error("Invalid session name: use 1-64 letters, digits, dashes or underscores.")
    scope = hashlib.sha256(str(_resolve_container_task_id(task_id)).encode()).hexdigest()[:20]
    daemon = "hermes-" + hashlib.sha256((scope + ":" + session).encode()).hexdigest()[:24]
    payload = {"code": browser._OWN_TAB_PREAMBLE + code, "timeout": timeout, "scope": scope, "daemon": daemon}
    runner = base64.b64encode(_REMOTE_RUNNER.encode()).decode()
    command = "python3 -c " + shlex.quote("import base64; exec(base64.b64decode(" + repr(runner) + "))")
    try:
        env = _get_file_ops(task_id or "default").env
        executed = env.execute(command, timeout=timeout + 10, stdin_data=json.dumps(payload))
        if executed.get("returncode") != 0:
            return tool_error("Remote browser wrapper failed: " + str(executed.get("output", ""))[:4000], error_type="remote_browser_execution_failed")
        result = json.loads(executed.get("output", ""))
        if not isinstance(result, dict):
            raise ValueError("Invalid remote browser response")
    except Exception as exc:
        return tool_error(str(exc), error_type="remote_browser_execution_failed")
    if session:
        result["session"] = session
    screenshot = result.get("screenshot_path")
    if screenshot:
        cache = Path(get_hermes_home()) / "cache/browser-use/materialized"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="ssh-", dir=cache) as directory:
                destination = Path(directory) / "screenshot.png"
                env.materialize_file(screenshot, destination, max_bytes=20 * 1024 * 1024, timeout=30, require_recent_seconds=timeout + 30)
                native = browser._native_screenshot_result(result, str(destination))
                if native is not None:
                    native.setdefault("meta", {})["screenshot_path"] = screenshot
                    return native
        except Exception as exc:
            result["screenshot_error"] = str(exc)
    return tool_result(result)

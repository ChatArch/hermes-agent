"""Final output, secret redaction and native-image projection for browser_exec."""


def render_exec_result(proc, *, workspace, session, route_key, started):
    from agent.redact import redact_sensitive_text
    from tools import browser_use_cli as browser
    from tools.registry import tool_result

    # browser_vault fill registers injected values with this forced model-egress
    # boundary. Preserve raw stdout only for screenshot-path detection below.
    result = {
        "success": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": redact_sensitive_text(proc.stdout, force=True),
    }
    if workspace:
        result["workspace"] = workspace
    if session:
        result["session"] = session
    result.update(browser._take_real_profile_note(route_key))
    stderr = redact_sensitive_text((proc.stderr or "").strip(), force=True)
    if len(stderr) > browser._STDERR_CAP_CHARS:
        stderr = stderr[:browser._STDERR_CAP_CHARS] + "\n… (stderr truncated)"
    if stderr:
        result["stderr"] = stderr
    screenshot = browser._find_screenshot(proc.stdout, started)
    if screenshot:
        result["screenshot_path"] = screenshot
        native = browser._native_screenshot_result(result, screenshot)
        if native is not None:
            return native
    return tool_result(result)

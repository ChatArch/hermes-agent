"""Registry-owned slash command execution (thin slice).

Invariant: an executor's output depends only on ``ctx.args`` / ``ctx.options`` — never on
``ctx.surface`` — so the core text is identical across surfaces for a fixed context (enforced by
tests/hermes_cli/test_commands_execute.py).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import shlex
from typing import Any

__all__ = ["CommandContext", "CommandReply", "EXECUTORS", "execute_command", "resolve_executor",
           "run_execute"]


@dataclass(frozen=True)
class CommandContext:
    """Surface-provided inputs for a shared command executor."""
    surface: str = "cli"                # "cli" | "gateway" | "tui" — decoration only
    args: str = ""                      # raw argument string after the command word
    options: Mapping[str, Any] = field(default_factory=dict)  # surface params (page_size, ...)
    config_get: Callable[[str, Any], Any] | None = None       # optional config accessor


@dataclass(frozen=True)
class CommandReply:
    """Canonical result of a shared executor: surface-independent ``text`` plus the structured
    ``data`` it derived so a surface can re-render with its own decoration."""
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)
    format: str = "plain"               # "plain" | "markdown" (hint, not a contract)


# Executors — pure formatters, no agent/session mutation.
def _exec_version(ctx: CommandContext) -> CommandReply:
    """Core /version text — the banner version label."""
    from hermes_cli.banner import format_banner_version_label
    return CommandReply(format_banner_version_label())


def _exec_egress(ctx: CommandContext) -> CommandReply:
    """Core /egress text — Docker egress proxy status."""
    from hermes_cli.proxy_cli import format_status_text
    return CommandReply(format_status_text())


def _exec_profile(ctx: CommandContext) -> CommandReply:
    """Core /profile data — active profile name + home directory.

    A multiplexed gateway may pre-resolve the per-source profile/home via ``options``
    (``profile_name`` / ``home_display``); otherwise process-level values are used.
    """
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import display_hermes_home
    profile_name = str(ctx.options.get("profile_name") or "").strip() or get_active_profile_name()
    home_display = str(ctx.options.get("home_display") or "").strip() or display_hermes_home()
    # Presentation-only display name (profile.yaml); `data.profile` stays the canonical id.
    label = profile_name
    try:
        from hermes_cli.profiles import format_profile_label, get_profile_dir, read_profile_meta
        display = read_profile_meta(get_profile_dir(profile_name)).get("display_name", "")
        label = format_profile_label(profile_name, display)
    except Exception:
        pass
    return CommandReply(f"Profile: {label}\nHome: {home_display}",
                        data={"profile": profile_name, "home": home_display})


def _exec_bundles(ctx: CommandContext) -> CommandReply:
    """Core /bundles data — installed skill bundles listing."""
    try:
        from agent.skill_bundles import _bundles_dir, list_bundles
    except Exception as exc:  # pragma: no cover - env-specific
        return CommandReply(f"Bundles subsystem unavailable: {exc}", data={"error": str(exc)})
    bundles = list_bundles()
    bundles_dir = str(_bundles_dir())
    if not bundles:
        return CommandReply(
            "No skill bundles installed.\n"
            "Create one with: hermes bundles create <name> --skill <s1> --skill <s2>\n"
            f"Directory: {bundles_dir}",
            data={"bundles": [], "dir": bundles_dir})
    lines = [f"Skill Bundles ({len(bundles)} installed):"]
    for info in bundles:
        skills = info.get("skills", [])
        desc = info.get("description") or f"Load {len(skills)} skills"
        lines.append(f"/{info['slug']} — {desc} ({len(skills)} skills)")
        lines.extend(f"    · {s}" for s in skills)
    lines.append("Invoke a bundle with /<slug> to load all its skills.")
    return CommandReply("\n".join(lines), data={"bundles": bundles, "dir": bundles_dir})


def _skill_commands() -> dict:
    """Registered skill commands, or ``{}`` when the skill subsystem is unavailable."""
    try:
        from agent.skill_commands import get_skill_commands
        return get_skill_commands() or {}
    except Exception:
        return {}


def _exec_help(ctx: CommandContext) -> CommandReply:
    """Core gateway /help body (pre platform mention decoration)."""
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines
    lines = [t("gateway.help.header"), *gateway_help_lines()]
    skill_cmds = _skill_commands()
    try:
        if skill_cmds:
            lines.append(t("gateway.help.skill_header", count=len(skill_cmds)))
            sorted_cmds = sorted(skill_cmds)  # first 10, then point to /commands for the rest
            lines.extend(f"`{cmd}` — {skill_cmds[cmd]['description']}"
                         for cmd in sorted_cmds[:10])
            if len(sorted_cmds) > 10:
                lines.append(t("gateway.help.more_use_commands", count=len(sorted_cmds) - 10))
    except Exception:
        pass
    return CommandReply("\n".join(lines), format="markdown")


def _exec_commands(ctx: CommandContext) -> CommandReply:
    """Core gateway /commands body — paginated command + skill listing.

    ``ctx.options["page_size"]`` is a surface parameter (Telegram uses 15, everything else 20).
    """
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines
    try:
        requested_page = int((ctx.args or "").strip() or 1)
    except ValueError:
        return CommandReply(t("gateway.commands.usage"), format="markdown")

    entries = list(gateway_help_lines())
    skill_cmds = _skill_commands()
    try:
        if skill_cmds:
            entries.extend(["", t("gateway.commands.skill_header")])
            for cmd in sorted(skill_cmds):
                desc = skill_cmds[cmd].get("description", "").strip() or t("gateway.commands.default_desc")
                entries.append(f"`{cmd}` — {desc}")
    except Exception:
        pass

    if not entries:
        return CommandReply(t("gateway.commands.none"), format="markdown")

    try:
        page_size = max(1, int(ctx.options.get("page_size", 20)))
    except (TypeError, ValueError):
        page_size = 20
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(1, min(requested_page, total_pages))
    start = (page - 1) * page_size
    lines = [t("gateway.commands.header", total=len(entries), page=page, total_pages=total_pages),
             "", *entries[start:start + page_size]]
    if total_pages > 1:
        nav_parts = ([t("gateway.commands.nav_prev", page=page - 1)] if page > 1 else []) + (
            [t("gateway.commands.nav_next", page=page + 1)] if page < total_pages else [])
        lines.extend(["", " | ".join(nav_parts)])
    if page != requested_page:
        lines.append(t("gateway.commands.out_of_range", requested=requested_page, page=page))
    return CommandReply("\n".join(lines), format="markdown")


def _short_text(value: Any, limit: int = 140) -> str:
    """Return a single-line display value bounded for gateway messages."""
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _schedule_display(job: Mapping[str, Any]) -> str:
    schedule = job.get("schedule") or {}
    if isinstance(schedule, Mapping):
        return _short_text(
            job.get("schedule_display")
            or schedule.get("display")
            or schedule.get("expr")
            or schedule.get("value")
        )
    return _short_text(job.get("schedule_display") or schedule)


def _job_mode(job: Mapping[str, Any]) -> str:
    if job.get("no_agent"):
        return "script-only"
    if job.get("script"):
        return "agent+script"
    return "agent"


def _format_cron_job(job: Mapping[str, Any]) -> list[str]:
    job_id = _short_text(job.get("id") or job.get("job_id") or "?", 18)
    name = _short_text(job.get("name") or "(unnamed)", 80)
    state = _short_text(
        job.get("state") or ("scheduled" if job.get("enabled", True) else "paused"), 24
    )
    enabled = "enabled" if job.get("enabled", True) else "disabled"
    lines = [f"- {name} ({job_id}) [{state}, {enabled}, {_job_mode(job)}]"]
    lines.append(f"  schedule: {_schedule_display(job)}")
    lines.append(f"  next: {_short_text(job.get('next_run_at'), 48)}")
    last_status = job.get("last_status")
    last_run = job.get("last_run_at")
    if last_status or last_run:
        last = _short_text(last_run, 48)
        status = _short_text(last_status or "unknown", 32)
        last_error = job.get("last_error")
        if last_error:
            status = f"{status}: {_short_text(last_error, 120)}"
        lines.append(f"  last: {last} {status}")
    else:
        lines.append("  last: never")
    delivery_error = job.get("last_delivery_error")
    if delivery_error:
        lines.append(f"  delivery_error: {_short_text(delivery_error, 180)}")
    script = job.get("script")
    if script:
        lines.append(f"  script: {_short_text(script, 80)}")
    workdir = job.get("workdir")
    if workdir:
        lines.append(f"  workdir: {_short_text(workdir, 120)}")
    latest_execution = job.get("latest_execution")
    if isinstance(latest_execution, Mapping):
        latest_status = latest_execution.get("status")
        latest_id = latest_execution.get("id")
        if latest_status or latest_id:
            lines.append(
                "  execution: "
                f"{_short_text(latest_status or '?', 24)} {_short_text(latest_id or '?', 28)}"
            )
    return lines


def _exec_cron(ctx: CommandContext) -> CommandReply:
    """Gateway-safe read-only cron listing."""
    raw_args = (ctx.args or "").strip()
    try:
        tokens = shlex.split(raw_args) if raw_args else []
    except ValueError:
        return CommandReply(
            "Usage: /cron list [--all]\n"
            "Gateway /cron is read-only; use the CLI for create/edit/pause/run/remove.",
            format="markdown",
        )

    if not tokens:
        tokens = ["list"]
    subcommand = tokens[0].lower()
    show_all = False
    if subcommand in {"list", "ls", "jobs"}:
        rest = tokens[1:]
    elif subcommand in {"--all", "-a", "all"}:
        rest = tokens
    else:
        return CommandReply(
            "Gateway /cron is read-only.\n"
            "Use /cron list [--all] here, or use the CLI for create/edit/pause/run/remove.",
            data={"blocked_subcommand": subcommand},
            format="markdown",
        )
    for token in rest:
        if token in {"--all", "-a", "all"}:
            show_all = True
        else:
            return CommandReply("Usage: /cron list [--all]", format="markdown")

    try:
        from cron.jobs import list_jobs

        jobs = list_jobs(include_disabled=show_all)
    except Exception as exc:  # pragma: no cover - environment-specific failure
        return CommandReply(
            f"Cron jobs unavailable: {_short_text(exc, 200)}",
            data={"error": str(exc)},
        )

    if not jobs:
        return CommandReply(
            "No scheduled cron jobs found."
            if show_all
            else "No active cron jobs found. Use /cron list --all to include disabled jobs.",
            data={"count": 0, "include_disabled": show_all},
        )

    lines = [
        f"Cron jobs ({len(jobs)} {'total' if show_all else 'active'}):",
        "",
    ]
    any_no_agent = False
    for job in jobs:
        any_no_agent = any_no_agent or bool(job.get("no_agent"))
        lines.extend(_format_cron_job(job))
    if any_no_agent:
        lines.extend([
            "",
            "Note: script-only jobs can be intentionally silent when stdout is empty.",
        ])
    text = "\n".join(lines)
    try:
        max_chars = int(ctx.options.get("max_chars", 3800))
    except (TypeError, ValueError):
        max_chars = 3800
    max_chars = max(500, max_chars)
    if len(text) > max_chars:
        text = text[: max_chars - 80].rstrip() + "\n... truncated; use CLI `hermes cron list --all` for full details."
    return CommandReply(
        text,
        data={"count": len(jobs), "include_disabled": show_all},
        format="markdown",
    )


# ---------------------------------------------------------------------------
# Registry + resolution
# ---------------------------------------------------------------------------

EXECUTORS: dict[str, Callable[[CommandContext], CommandReply]] = {
    "version": _exec_version,
    "egress": _exec_egress,
    "profile": _exec_profile,
    "bundles": _exec_bundles,
    "gateway_help": _exec_help,
    "gateway_commands": _exec_commands,
    "gateway_cron": _exec_cron,
}


def resolve_executor(cmd_def: Any) -> Callable[[CommandContext], CommandReply] | None:
    """Return the shared executor for ``cmd_def`` (or None when not migrated)."""
    return EXECUTORS.get(getattr(cmd_def, "execute", None) or "")


def run_execute(cmd_def: Any, ctx: CommandContext) -> CommandReply | None:
    """Run ``cmd_def``'s registry-owned executor, if any."""
    fn = resolve_executor(cmd_def)
    return None if fn is None else fn(ctx)


def execute_command(name: str, ctx: CommandContext) -> CommandReply:
    """Run the shared executor for ``name``; ``LookupError`` when unknown or not migrated."""
    from hermes_cli.commands import resolve_command
    cmd_def = resolve_command(name)
    reply = run_execute(cmd_def, ctx) if cmd_def is not None else None
    if reply is None:
        raise LookupError(f"no registry-owned executor for /{name}")
    return reply

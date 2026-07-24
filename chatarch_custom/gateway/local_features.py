"""ChatArch-local gateway feature registry.

The goal of this module is to keep fork-maintained command metadata and core
seam markers in one owned place. Upstream-heavy files should import these
narrow helpers instead of scattering ChatArch-only command lists throughout the
main gateway implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

CommandDefT = TypeVar("CommandDefT")
CommandDefFactory = Callable[..., CommandDefT]


@dataclass(frozen=True)
class LocalGatewayCommand:
    """Metadata for a ChatArch-maintained gateway slash command."""

    name: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    args_hint: str = ""
    subcommands: tuple[str, ...] = ()
    handler_name: str = ""

    def to_command_def(self, factory: CommandDefFactory) -> CommandDefT:
        """Build the host project's CommandDef without importing it here."""
        return factory(
            self.name,
            self.description,
            self.category,
            aliases=self.aliases,
            gateway_only=True,
            args_hint=self.args_hint,
            subcommands=self.subcommands,
        )


LOCAL_GATEWAY_COMMANDS: tuple[LocalGatewayCommand, ...] = (
    LocalGatewayCommand(
        "thread",
        "Start or reset a Feishu thread session",
        "Session",
        aliases=("t",),
        args_hint="<prompt>",
        handler_name="_handle_thread_command",
    ),
    LocalGatewayCommand(
        "ssh",
        "Manage SSH backend targets and section bindings",
        "Session",
        args_hint="[list|status|test <alias>|use <alias>|off]",
        subcommands=("list", "status", "test", "use", "off", "local", "help"),
        handler_name="_handle_ssh_command",
    ),
    LocalGatewayCommand(
        "template",
        "Use, create, update, or list thread templates",
        "Tools & Skills",
        aliases=("tpl",),
        args_hint="<name|list|create|update|use> [instruction...]",
        subcommands=("list", "create", "update", "use"),
        handler_name="_handle_template_command",
    ),
)

LOCAL_GATEWAY_COMMAND_NAMES: frozenset[str] = frozenset(
    command.name for command in LOCAL_GATEWAY_COMMANDS
)

LOCAL_GATEWAY_COMMAND_ALIASES: frozenset[str] = frozenset(
    alias for command in LOCAL_GATEWAY_COMMANDS for alias in command.aliases
)

LOCAL_ACTIVE_SESSION_BYPASS_COMMANDS: frozenset[str] = frozenset(
    set(LOCAL_GATEWAY_COMMAND_NAMES) | set(LOCAL_GATEWAY_COMMAND_ALIASES)
)

LOCAL_GATEWAY_HANDLER_BY_COMMAND: dict[str, str] = {
    command.name: command.handler_name
    for command in LOCAL_GATEWAY_COMMANDS
    if command.handler_name
}

# Core seams that still live in upstream-heavy files. If a future merge
# conflicts near one of these symbols, resolve the hunk manually and run the
# matching real-entrypoint tests before accepting it.
CORE_SEAMS: dict[str, str] = {
    "gateway.run.GatewayRunner._handle_thread_command": "Feishu /thread and /t typed entrypoints",
    "gateway.run.GatewayRunner._handle_template_command": "Feishu /template and /tpl thread launcher",
    "gateway.run.GatewayRunner._handle_ssh_command": "Gateway /ssh section binding UX",
    "gateway.run.GatewayRunner._handle_message_with_agent._ssh_grant_notify_sync": "gateway-to-tool ssh_mode.request_use authorization card bridge",
    "tools.ssh_mode_tool._request_use": "model-side ssh_mode.request_use decision path",
    "plugins.platforms.feishu.adapter.send_ssh_grant_approval": "Feishu SSH allow-current/all/deny card",
    "gateway.platforms.base.CardReply": "Structured gateway card reply surface",
    "gateway.platforms.base.SendResult.thread_id": "Platform thread metadata preservation",
}


def command_defs(factory: CommandDefFactory) -> tuple[CommandDefT, ...]:
    """Return ChatArch-local slash command definitions for the host registry."""
    return tuple(command.to_command_def(factory) for command in LOCAL_GATEWAY_COMMANDS)


def active_session_bypass_commands() -> frozenset[str]:
    """Return local command names/aliases that must bypass active agents."""
    return LOCAL_ACTIVE_SESSION_BYPASS_COMMANDS


def local_gateway_handler_name(canonical_command: str | None) -> str | None:
    """Return the GatewayRunner handler name for a local command, if any."""
    if not canonical_command:
        return None
    return LOCAL_GATEWAY_HANDLER_BY_COMMAND.get(canonical_command)


def iter_core_seams() -> Iterable[tuple[str, str]]:
    """Yield documented non-extracted core seams for audit tooling/docs."""
    return CORE_SEAMS.items()

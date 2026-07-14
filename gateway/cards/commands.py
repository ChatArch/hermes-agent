"""Reusable cards for gateway slash-command UX."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from gateway.cards.actions import CardActionContext, CardActionResponse, register_card_action
from gateway.cards.model import Actions, Button, Card, CardHeader, Divider, Markdown, Note

COMMAND_CARD_ACTION_HOME = "gateway.command.home"
COMMAND_CARD_ACTION_OPEN_GROUP = "gateway.command.open_group"
COMMAND_CARD_ACTION_TEXT_HELP = "gateway.command.text_help"
COMMAND_CARD_ACTION_RUN = "gateway.command.run"


@dataclass(frozen=True, slots=True)
class CommandCardGroup:
    """A top-level command category exposed by the command-center card."""

    key: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class CommandCardEntry:
    """A command shown inside a command-card group."""

    command: str
    description: str
    action: str = COMMAND_CARD_ACTION_RUN
    args: str = ""


COMMAND_CENTER_GROUPS: tuple[CommandCardGroup, ...] = (
    CommandCardGroup("session", "Session", "Start, stop, reset, and steer runs."),
    CommandCardGroup("model", "Model", "Choose model, provider, and reasoning."),
    CommandCardGroup("config", "Config", "Profile, personality, footer, voice."),
    CommandCardGroup("tools", "Tools", "Skills, MCP, SSH, files, and terminal."),
    CommandCardGroup("info", "Info", "Status, usage, version, and debug info."),
    CommandCardGroup("safety", "Safety", "Approvals and destructive confirmations."),
)

COMMAND_GROUP_ENTRIES: dict[str, tuple[CommandCardEntry, ...]] = {
    "session": (
        CommandCardEntry("/new", "Start a fresh session."),
        CommandCardEntry("/stop", "Stop the active run."),
        CommandCardEntry("/queue", "Queue a free-form follow-up prompt."),
        CommandCardEntry("/steer", "Steer a running task with text."),
        CommandCardEntry("/thread", "Create or continue a thread."),
    ),
    "model": (
        CommandCardEntry("/model", "Open model/provider switching."),
        CommandCardEntry("/reasoning", "Set reasoning effort."),
        CommandCardEntry("/fast", "Toggle priority processing."),
        CommandCardEntry("/usage", "Review provider/account usage."),
    ),
    "config": (
        CommandCardEntry("/profile", "Show active profile."),
        CommandCardEntry("/personality", "Pick a personality preset."),
        CommandCardEntry("/voice", "Configure voice replies."),
        CommandCardEntry("/footer", "Toggle final reply footer."),
    ),
    "tools": (
        CommandCardEntry("/commands", "Browse all commands."),
        CommandCardEntry("/skills", "List skills."),
        CommandCardEntry("/ssh", "Inspect or switch SSH target."),
        CommandCardEntry("/mcp", "Inspect MCP server state."),
    ),
    "info": (
        CommandCardEntry("/status", "Show gateway/session status."),
        CommandCardEntry("/agents", "List running agents."),
        CommandCardEntry("/whoami", "Show access identity."),
        CommandCardEntry("/version", "Show Hermes version."),
    ),
    "safety": (
        CommandCardEntry("/yolo", "Review approval mode."),
        CommandCardEntry("/undo", "Undo recent turns with confirmation."),
        CommandCardEntry("/reload-mcp", "Reload MCP servers."),
        CommandCardEntry("/debug", "Collect debug info."),
    ),
}


def command_run_payload(command: str, *, args: str = "", scope: str = "session") -> dict[str, str]:
    """Build a stable payload for command-card actions.

    The payload names a gateway command intent; callers must execute it through
    command handlers, not by injecting synthetic slash text into agent history.
    """

    normalized = command.strip().lstrip("/")
    if not normalized:
        raise ValueError("command cannot be empty")
    payload = {"command": normalized, "scope": scope.strip() or "session"}
    if args.strip():
        payload["args"] = args.strip()
    return payload


def _status_line(*, profile: str = "", provider: str = "", model: str = "", busy: bool = False) -> str:
    parts: list[str] = []
    if profile:
        parts.append(f"Profile: `{profile}`")
    if provider or model:
        current = "/".join(part for part in (provider, model) if part)
        parts.append(f"Model: `{current}`")
    parts.append("Agent: `running`" if busy else "Agent: `idle`")
    return "  |  ".join(parts)


def _group_by_key(group_key: str) -> CommandCardGroup:
    normalized = (group_key or "").strip().lower()
    for group in COMMAND_CENTER_GROUPS:
        if group.key == normalized:
            return group
    return COMMAND_CENTER_GROUPS[0]


def _safe_markdown_lines(lines: Iterable[str], *, limit: int = 5500) -> str:
    text = "\n".join(str(line) for line in lines).strip()
    if len(text) <= limit:
        return text or "No content."
    return text[:limit].rstrip() + "\n\n...truncated. Type the command for the full text output."


def _home_buttons() -> Actions:
    return Actions(
        buttons=[
            Button("Command Center", COMMAND_CARD_ACTION_HOME, style="primary"),
            Button(
                "Text help",
                COMMAND_CARD_ACTION_TEXT_HELP,
                payload=command_run_payload("help"),
            ),
        ],
        layout="row",
    )


def build_command_center_card(
    *,
    profile: str = "",
    provider: str = "",
    model: str = "",
    busy: bool = False,
) -> Card:
    """Build the top-level Hermes command-center card."""

    group_buttons = [
        Button(
            text=group.title,
            action=COMMAND_CARD_ACTION_OPEN_GROUP,
            payload={"group": group.key},
        )
        for group in COMMAND_CENTER_GROUPS
    ]
    return Card(
        header=CardHeader(title="Hermes Command Center", color="turquoise"),
        elements=[
            Markdown(_status_line(profile=profile, provider=provider, model=model, busy=busy)),
            Divider(),
            Markdown("Choose a command group. Typed slash commands still work exactly as before."),
            Actions(buttons=group_buttons[:3], layout="equal"),
            Actions(buttons=group_buttons[3:], layout="equal"),
            Divider(),
            Actions(
                buttons=[
                    Button(
                        text="Text help",
                        action=COMMAND_CARD_ACTION_TEXT_HELP,
                        payload=command_run_payload("help"),
                    ),
                    Button(
                        text="Browse commands",
                        action=COMMAND_CARD_ACTION_RUN,
                        payload=command_run_payload("commands"),
                    ),
                ],
                layout="row",
            ),
            Note("Card clicks are gateway actions, not user messages sent to the agent."),
        ],
    )


def build_command_group_card(group_key: str) -> Card:
    """Build a command group page for the command-center card."""

    group = _group_by_key(group_key)
    entries = COMMAND_GROUP_ENTRIES.get(group.key, ())
    lines = [f"**{entry.command}** - {entry.description}" for entry in entries]
    buttons = [
        Button(
            text=entry.command,
            action=entry.action,
            payload=command_run_payload(entry.command, args=entry.args),
        )
        for entry in entries[:4]
    ]
    elements = [
        Markdown(group.description),
        Divider(),
        Markdown(_safe_markdown_lines(lines)),
    ]
    if buttons:
        elements.append(Actions(buttons=buttons[:2], layout="equal"))
        if len(buttons) > 2:
            elements.append(Actions(buttons=buttons[2:4], layout="equal"))
    elements.extend([
        Divider(),
        _home_buttons(),
        Note("Buttons are gateway actions. Free-form commands remain typed commands."),
    ])
    return Card(header=CardHeader(title=f"Hermes Commands - {group.title}", color="blue"), elements=elements)


def build_text_help_card(lines: Iterable[str]) -> Card:
    """Build a card containing the textual help fallback."""

    return Card(
        header=CardHeader(title="Hermes Text Help", color="blue"),
        elements=[
            Markdown(_safe_markdown_lines(lines)),
            Divider(),
            _home_buttons(),
        ],
    )


def build_commands_browse_card(lines: Iterable[str], *, page: int = 1, page_size: int = 12) -> Card:
    """Build a simple command browser card from gateway help lines."""

    entries = [line for line in lines if str(line).strip()]
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    shown = entries[start:start + page_size]
    return Card(
        header=CardHeader(title=f"Hermes Commands ({page}/{total_pages})", color="wathet"),
        elements=[
            Markdown(_safe_markdown_lines(shown)),
            Divider(),
            _home_buttons(),
            Note("Type /commands for the full paginated text output."),
        ],
    )


def register_command_card_actions() -> None:
    """Register built-in command-card button handlers.

    Registration is idempotent: the registry replaces existing handlers for the
    same action names, which is safe across tests and gateway reloads.
    """

    def _help_lines() -> list[str]:
        from agent.i18n import t
        from hermes_cli.commands import gateway_help_lines

        return [t("gateway.help.header"), *gateway_help_lines()]

    async def _home(_ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(build_command_center_card())

    async def _open_group(ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(build_command_group_card(ctx.payload.get("group", "session")))

    async def _text_help(_ctx: CardActionContext) -> CardActionResponse:
        return CardActionResponse.replace_card(build_text_help_card(_help_lines()))

    async def _run(ctx: CardActionContext) -> CardActionResponse:
        command = str(ctx.payload.get("command", "")).strip().lstrip("/")
        if command == "help":
            return CardActionResponse.replace_card(build_text_help_card(_help_lines()))
        if command == "commands":
            return CardActionResponse.replace_card(build_commands_browse_card(_help_lines()))
        return CardActionResponse.replace_card(
            Card(
                header=CardHeader(title=f"/{command}" if command else "Command", color="grey"),
                elements=[
                    Markdown("This command still uses typed input. Type it in chat to run it."),
                    Divider(),
                    _home_buttons(),
                ],
            )
        )

    register_card_action(COMMAND_CARD_ACTION_HOME, _home)
    register_card_action(COMMAND_CARD_ACTION_OPEN_GROUP, _open_group)
    register_card_action(COMMAND_CARD_ACTION_TEXT_HELP, _text_help)
    register_card_action(COMMAND_CARD_ACTION_RUN, _run)

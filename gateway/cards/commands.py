"""Reusable cards for gateway slash-command UX."""

from __future__ import annotations

from dataclasses import dataclass

from gateway.cards.model import Actions, Button, Card, CardHeader, Divider, Markdown, Note

COMMAND_CARD_ACTION_OPEN_GROUP = "gateway.command.open_group"
COMMAND_CARD_ACTION_TEXT_HELP = "gateway.command.text_help"
COMMAND_CARD_ACTION_RUN = "gateway.command.run"


@dataclass(frozen=True, slots=True)
class CommandCardGroup:
    """A top-level command category exposed by the command-center card."""

    key: str
    title: str
    description: str


COMMAND_CENTER_GROUPS: tuple[CommandCardGroup, ...] = (
    CommandCardGroup("session", "Session", "Start, stop, reset, and steer runs."),
    CommandCardGroup("model", "Model", "Choose model, provider, and reasoning."),
    CommandCardGroup("config", "Config", "Profile, personality, footer, voice."),
    CommandCardGroup("tools", "Tools", "Skills, MCP, SSH, files, and terminal."),
    CommandCardGroup("info", "Info", "Status, usage, version, and debug info."),
    CommandCardGroup("safety", "Safety", "Approvals and destructive confirmations."),
)


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


def build_command_center_card(
    *,
    profile: str = "",
    provider: str = "",
    model: str = "",
    busy: bool = False,
) -> Card:
    """Build the top-level Hermes command-center card.

    This card is intentionally only navigation and status. Concrete actions such
    as switching models or resetting a session stay in their existing command
    handlers and are wired by action handlers in follow-up PRs.
    """

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

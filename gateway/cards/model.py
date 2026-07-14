"""Platform-neutral card model for Hermes gateway cards.

This module intentionally defines a small composable DSL rather than one
hardcoded template per Feishu card use case. Platform renderers can translate
these elements to native message formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


CardColor = Literal[
    "blue",
    "green",
    "red",
    "orange",
    "purple",
    "grey",
    "turquoise",
    "violet",
    "indigo",
    "wathet",
    "yellow",
    "carmine",
]
ButtonStyle = Literal["default", "primary", "danger"]
ActionsLayout = Literal["row", "equal"]


@dataclass(slots=True)
class CardHeader:
    title: str
    color: CardColor | str = "blue"


@dataclass(slots=True)
class Markdown:
    content: str


@dataclass(slots=True)
class Divider:
    pass


@dataclass(slots=True)
class Button:
    text: str
    action: str
    style: ButtonStyle | str = "default"
    url: str | None = None
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Actions:
    buttons: list[Button]
    layout: ActionsLayout | str = "row"


@dataclass(slots=True)
class ListItem:
    """A compact row with descriptive text and one primary action."""

    text: str
    button: Button


@dataclass(slots=True)
class SelectOption:
    text: str
    value: str = ""
    action: str = ""
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Select:
    """A dropdown-style chooser for structured command workflows."""

    placeholder: str
    options: list[SelectOption]
    initial_value: str = ""


@dataclass(slots=True)
class Note:
    content: str


@dataclass(slots=True)
class Image:
    image_key: str
    alt: str = "image"


CardElement = Markdown | Divider | Actions | ListItem | Select | Note | Image


@dataclass(slots=True)
class Card:
    elements: list[CardElement]
    header: CardHeader | None = None


@dataclass(slots=True)
class RawFeishuCard:
    """Escape hatch for native Feishu card JSON when the generic DSL is insufficient."""

    data: dict[str, Any]


def build_feishu_authorization_card(
    *,
    verification_url: str,
    flow_id: str,
    title: str = "飞书授权请求",
    body: str = "需要你完成飞书授权后，我才能继续。",
) -> Card:
    """Build the first acceptance card as a generic Card composition.

    This is a convenience builder over the flexible Card DSL, not a dedicated
    renderer/template path. Callers can build the same shape manually.
    """

    return Card(
        header=CardHeader(title=title, color="blue"),
        elements=[
            Markdown(body),
            Actions(
                layout="row",
                buttons=[
                    Button(
                        text="授权",
                        style="primary",
                        action="auth.authorize",
                        url=verification_url,
                        payload={"flow_id": flow_id},
                    ),
                    Button(
                        text="取消",
                        style="danger",
                        action="auth.cancel",
                        payload={"flow_id": flow_id},
                    ),
                ],
            ),
            Note("Hermes 将在授权完成后继续。"),
        ],
    )

"""Registry for Hermes card action callbacks."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from gateway.cards.model import Card


@dataclass(slots=True)
class CardActionContext:
    action: str
    payload: dict[str, str]
    user_id: str
    chat_id: str
    message_id: str
    session_key: str | None = None


@dataclass(slots=True)
class CardActionResponse:
    kind: Literal["replace_card", "toast", "dispatch_message", "noop"]
    card: Card | None = None
    text: str = ""

    @classmethod
    def replace_card(cls, card: Card) -> "CardActionResponse":
        return cls(kind="replace_card", card=card)

    @classmethod
    def toast(cls, text: str) -> "CardActionResponse":
        return cls(kind="toast", text=text)

    @classmethod
    def dispatch_message(cls, text: str) -> "CardActionResponse":
        return cls(kind="dispatch_message", text=text)

    @classmethod
    def noop(cls) -> "CardActionResponse":
        return cls(kind="noop")


CardActionHandler = Callable[[CardActionContext], CardActionResponse | Awaitable[CardActionResponse]]


class CardActionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, CardActionHandler] = {}

    def register(self, action: str, handler: CardActionHandler) -> None:
        action = action.strip()
        if not action:
            raise ValueError("card action name cannot be empty")
        if not callable(handler):
            raise TypeError("card action handler must be callable")
        self._handlers[action] = handler

    def get(self, action: str) -> CardActionHandler | None:
        return self._handlers.get(action)

    def actions(self) -> list[str]:
        return sorted(self._handlers)

    async def dispatch(self, context: CardActionContext) -> CardActionResponse:
        handler = self.get(context.action)
        if handler is None:
            raise KeyError(f"No card action handler registered for {context.action!r}")
        result = handler(context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, CardActionResponse):
            raise TypeError("card action handler must return CardActionResponse")
        return result


_global_registry = CardActionRegistry()


def register_card_action(action: str, handler: CardActionHandler) -> None:
    _global_registry.register(action, handler)


def get_card_action_registry() -> CardActionRegistry:
    return _global_registry

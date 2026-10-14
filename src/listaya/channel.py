"""Messaging channel boundary.

Business logic talks to the merchant only through the Channel interface, so it
never depends on the Telegram library and tests can use an in-memory channel.
Inbound messages arrive as IncomingMessage values passed to a MessageHandler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence, runtime_checkable

# Bot API limit for downloading files (https://core.telegram.org/bots/api#getfile).
MAX_DOWNLOAD_BYTES = 20_000_000


class FileTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"file of {size} bytes exceeds the {MAX_DOWNLOAD_BYTES} byte download limit")
        self.size = size


@dataclass(frozen=True)
class Attachment:
    file_id: str
    file_name: str | None
    mime_type: str | None
    size: int | None


@dataclass(frozen=True)
class IncomingMessage:
    """One thing the merchant did: wrote text, sent a file (with optional caption) or pressed a button."""

    chat_id: str
    text: str | None = None
    attachment: Attachment | None = None
    button_data: str | None = None


@dataclass(frozen=True)
class Button:
    label: str
    data: str


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


@runtime_checkable
class Channel(Protocol):
    async def send_text(self, chat_id: str, text: str) -> None: ...

    async def send_buttons(self, chat_id: str, text: str, buttons: Sequence[Button]) -> None: ...

    async def download_file(self, attachment: Attachment) -> bytes: ...

"""In-memory test doubles that never touch the network."""

from __future__ import annotations

from dataclasses import dataclass, field

from listaya.channel import Attachment, Button, FileTooLargeError, MAX_DOWNLOAD_BYTES


@dataclass
class SentMessage:
    chat_id: str
    text: str
    buttons: list[Button] = field(default_factory=list)


class FakeChannel:
    """Channel double: records what was sent and serves files from memory."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.sent: list[SentMessage] = []
        self.files = files or {}

    async def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append(SentMessage(chat_id, text))

    async def send_buttons(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        self.sent.append(SentMessage(chat_id, text, list(buttons)))

    async def download_file(self, attachment: Attachment) -> bytes:
        if attachment.size is not None and attachment.size > MAX_DOWNLOAD_BYTES:
            raise FileTooLargeError(attachment.size)
        return self.files[attachment.file_id]

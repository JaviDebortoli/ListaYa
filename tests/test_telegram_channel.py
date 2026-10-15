import asyncio
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup, Update

from listaya.channel import MAX_DOWNLOAD_BYTES, Attachment, Button, Channel, FileTooLargeError, IncomingMessage
from listaya.telegram_channel import TelegramChannel, to_incoming
from tests.fakes import FakeChannel

CHAT = {"id": 42, "type": "private"}
USER = {"id": 7, "is_bot": False, "first_name": "Marta"}


def message_update(**message: Any) -> Update:
    data = {"update_id": 1, "message": {"message_id": 10, "date": 0, "chat": CHAT, "from": USER, **message}}
    return Update.de_json(data, None)


class TestToIncoming:
    def test_text_message(self):
        incoming = to_incoming(message_update(text="Lista de Proveedor X"))
        assert incoming == IncomingMessage(chat_id="42", text="Lista de Proveedor X")

    def test_document_with_caption(self):
        update = message_update(
            document={
                "file_id": "doc-1",
                "file_unique_id": "u1",
                "file_name": "lista.pdf",
                "mime_type": "application/pdf",
                "file_size": 1234,
            },
            caption="Proveedor X",
        )
        incoming = to_incoming(update)
        assert incoming.chat_id == "42"
        assert incoming.text == "Proveedor X"
        assert incoming.attachment == Attachment(
            file_id="doc-1", file_name="lista.pdf", mime_type="application/pdf", size=1234
        )

    def test_photo_uses_largest_size(self):
        update = message_update(
            photo=[
                {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 1000},
                {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 960, "file_size": 90000},
            ]
        )
        attachment = to_incoming(update).attachment
        assert attachment.file_id == "large"
        assert attachment.mime_type == "image/jpeg"
        assert attachment.size == 90000

    def test_button_press(self):
        data = {
            "update_id": 2,
            "callback_query": {
                "id": "cb-1",
                "from": USER,
                "chat_instance": "ci",
                "data": "approve:run-1",
                "message": {"message_id": 11, "date": 0, "chat": CHAT, "text": "Resumen"},
            },
        }
        incoming = to_incoming(Update.de_json(data, None))
        assert incoming == IncomingMessage(chat_id="42", button_data="approve:run-1")

    def test_unsupported_update_is_ignored(self):
        assert to_incoming(message_update(sticker=None)) is None


class FakeTelegramFile:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.content)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        self.calls.append({"get_file": file_id})
        return FakeTelegramFile(b"contents")


def run(coro):
    return asyncio.run(coro)


class TestTelegramChannel:
    def test_send_text(self):
        bot = FakeBot()
        run(TelegramChannel(bot).send_text("42", "Recibí tu lista"))
        assert bot.calls == [{"chat_id": 42, "text": "Recibí tu lista"}]

    def test_send_buttons_builds_inline_keyboard(self):
        bot = FakeBot()
        buttons = [Button("Aprobar", "approve:run-1"), Button("Cancelar", "cancel:run-1")]
        run(TelegramChannel(bot).send_buttons("42", "Resumen", buttons))
        markup = bot.calls[0]["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        row = markup.inline_keyboard[0]
        assert [(b.text, b.callback_data) for b in row] == [("Aprobar", "approve:run-1"), ("Cancelar", "cancel:run-1")]

    def test_download_file(self):
        bot = FakeBot()
        attachment = Attachment(file_id="doc-1", file_name="lista.pdf", mime_type="application/pdf", size=10)
        assert run(TelegramChannel(bot).download_file(attachment)) == b"contents"

    def test_rejects_files_over_the_bot_api_limit_without_calling_telegram(self):
        bot = FakeBot()
        attachment = Attachment(file_id="big", file_name="x.pdf", mime_type=None, size=MAX_DOWNLOAD_BYTES + 1)
        with pytest.raises(FileTooLargeError):
            run(TelegramChannel(bot).download_file(attachment))
        assert bot.calls == []


def test_adapters_satisfy_the_channel_interface():
    assert isinstance(TelegramChannel(FakeBot()), Channel)
    assert isinstance(FakeChannel(), Channel)


class TestDispatch:
    def test_button_press_reaches_the_handler_even_if_answering_fails(self):
        from types import SimpleNamespace

        from telegram.error import BadRequest

        from listaya.telegram_channel import dispatch_update

        async def failing_answer(*args, **kwargs):
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")

        query = SimpleNamespace(
            data="approve:run-1", message=SimpleNamespace(chat=SimpleNamespace(id=42)), answer=failing_answer
        )
        update = SimpleNamespace(callback_query=query, message=None)
        received: list[IncomingMessage] = []

        async def handler(message: IncomingMessage) -> None:
            received.append(message)

        run(dispatch_update(update, handler))
        assert received == [IncomingMessage(chat_id="42", button_data="approve:run-1")]

    def test_unsupported_update_is_not_dispatched(self):
        from listaya.telegram_channel import dispatch_update

        received = []

        async def handler(message):
            received.append(message)

        run(dispatch_update(message_update(sticker=None), handler))
        assert received == []

import asyncio

import pytest

from listaya.app import ACK_FILE, ACK_TEXT, FILE_TOO_LARGE, ConfigError, acknowledge, load_settings
from listaya.channel import MAX_DOWNLOAD_BYTES, Attachment, IncomingMessage
from tests.fakes import FakeChannel


class TestLoadSettings:
    def test_reads_token(self):
        assert load_settings({"TELEGRAM_BOT_TOKEN": "123:abc"}).telegram_token == "123:abc"

    @pytest.mark.parametrize("env", [{}, {"TELEGRAM_BOT_TOKEN": ""}, {"TELEGRAM_BOT_TOKEN": "replace-with-botfather-token"}])
    def test_missing_or_placeholder_token_fails_clearly(self, env):
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            load_settings(env)


class TestAcknowledge:
    def test_file_gets_processing_ack(self):
        channel = FakeChannel()
        attachment = Attachment(file_id="f", file_name="lista.pdf", mime_type="application/pdf", size=100)
        asyncio.run(acknowledge(channel, IncomingMessage(chat_id="42", attachment=attachment)))
        assert [(m.chat_id, m.text) for m in channel.sent] == [("42", ACK_FILE)]

    def test_oversized_file_is_rejected_with_explanation(self):
        channel = FakeChannel()
        attachment = Attachment(file_id="f", file_name="x.pdf", mime_type=None, size=MAX_DOWNLOAD_BYTES + 1)
        asyncio.run(acknowledge(channel, IncomingMessage(chat_id="42", attachment=attachment)))
        assert [m.text for m in channel.sent] == [FILE_TOO_LARGE]

    def test_text_gets_text_ack(self):
        channel = FakeChannel()
        asyncio.run(acknowledge(channel, IncomingMessage(chat_id="42", text="hola")))
        assert [m.text for m in channel.sent] == [ACK_TEXT]

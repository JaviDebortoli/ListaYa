"""Application wiring: settings, the message handler and the bot entry point.

For now the handler only acknowledges what the merchant sends; list processing
is wired in once extraction, matching and the summary exist.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping

from listaya.channel import MAX_DOWNLOAD_BYTES, Channel, IncomingMessage

PLACEHOLDER_TOKEN = "replace-with-botfather-token"

ACK_FILE = "Recibí tu lista, la estoy procesando. Te aviso en cuanto tenga el resumen."
ACK_TEXT = "Mandame la lista de tu proveedor: PDF, Excel/CSV, una foto o el texto pegado."
ACK_BUTTON = "Recibido."
FILE_TOO_LARGE = "El archivo pesa más de 20 MB y Telegram no me deja descargarlo. ¿Podés mandarlo más liviano?"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    telegram_token: str


def load_settings(env: Mapping[str, str]) -> Settings:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == PLACEHOLDER_TOKEN:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set: create a bot with @BotFather and put its token in .env")
    return Settings(telegram_token=token)


async def acknowledge(channel: Channel, message: IncomingMessage) -> None:
    if message.attachment is not None:
        size = message.attachment.size
        if size is not None and size > MAX_DOWNLOAD_BYTES:
            await channel.send_text(message.chat_id, FILE_TOO_LARGE)
        else:
            await channel.send_text(message.chat_id, ACK_FILE)
    elif message.button_data is not None:
        await channel.send_text(message.chat_id, ACK_BUTTON)
    else:
        await channel.send_text(message.chat_id, ACK_TEXT)


def main() -> None:
    from dotenv import load_dotenv

    from listaya.telegram_channel import TelegramChannel, build_application

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Keep the bot token out of the logs: httpx logs every request URL, which contains it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    settings = load_settings(os.environ)

    channel: TelegramChannel | None = None

    async def handle(message: IncomingMessage) -> None:
        assert channel is not None
        await acknowledge(channel, message)

    application = build_application(settings.telegram_token, handle)
    channel = TelegramChannel(application.bot)
    application.run_polling()

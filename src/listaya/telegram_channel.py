"""Telegram implementation of the Channel interface (python-telegram-bot)."""

from __future__ import annotations

import logging
from typing import Any, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from listaya.channel import MAX_DOWNLOAD_BYTES, Attachment, Button, FileTooLargeError, IncomingMessage
from listaya.channel import MessageHandler as IncomingHandler

logger = logging.getLogger(__name__)


def to_incoming(update: Update) -> IncomingMessage | None:
    """Translate a Telegram update into a channel-neutral message, or None if it is not supported."""
    query = update.callback_query
    if query is not None:
        if query.message is None or query.data is None:
            return None
        return IncomingMessage(chat_id=str(query.message.chat.id), button_data=query.data)

    message = update.message
    if message is None:
        return None
    chat_id = str(message.chat.id)

    if message.document is not None:
        document = message.document
        attachment = Attachment(
            file_id=document.file_id,
            file_name=document.file_name,
            mime_type=document.mime_type,
            size=document.file_size,
        )
        return IncomingMessage(chat_id=chat_id, text=message.caption, attachment=attachment)

    if message.photo:
        largest = message.photo[-1]  # Telegram lists photo sizes from smallest to largest
        attachment = Attachment(
            file_id=largest.file_id, file_name=None, mime_type="image/jpeg", size=largest.file_size
        )
        return IncomingMessage(chat_id=chat_id, text=message.caption, attachment=attachment)

    if message.text:
        return IncomingMessage(chat_id=chat_id, text=message.text)
    return None


class TelegramChannel:
    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def send_text(self, chat_id: str, text: str) -> None:
        await self._bot.send_message(chat_id=int(chat_id), text=text)

    async def send_buttons(self, chat_id: str, text: str, buttons: Sequence[Button]) -> None:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(b.label, callback_data=b.data) for b in buttons]])
        await self._bot.send_message(chat_id=int(chat_id), text=text, reply_markup=markup)

    async def download_file(self, attachment: Attachment) -> bytes:
        # Check the size Telegram reported before asking for the file: getFile fails above the limit anyway.
        if attachment.size is not None and attachment.size > MAX_DOWNLOAD_BYTES:
            raise FileTooLargeError(attachment.size)
        telegram_file = await self._bot.get_file(attachment.file_id)
        return bytes(await telegram_file.download_as_bytearray())


async def dispatch_update(update: Update, handler: IncomingHandler) -> None:
    if update.callback_query is not None:
        try:
            await update.callback_query.answer()  # stop the button's loading spinner
        except TelegramError as error:
            # Best effort: a press delivered late (e.g. after a restart) can no longer be answered,
            # but it must still reach the handler, or an approval would be lost.
            logger.warning("could not answer callback query: %s", error)
    incoming = to_incoming(update)
    if incoming is None:
        return
    await handler(incoming)


def build_application(token: str, handler: IncomingHandler) -> Application:
    """Create the bot application wired to a channel-neutral message handler."""
    # concurrent_updates: a long-running list must not block other merchants' messages.
    application = Application.builder().token(token).concurrent_updates(True).build()

    async def on_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await dispatch_update(update, handler)

    application.add_handler(
        MessageHandler(filters.TEXT | filters.Document.ALL | filters.PHOTO, on_update)
    )
    application.add_handler(CallbackQueryHandler(on_update))
    return application

"""Application wiring: settings from the environment and the bot entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from listaya.extraction.rows import ListFormat
from listaya.pricing import MarginRule, PricingConfig
from listaya.token_factory import DEFAULT_BASE_URL

PLACEHOLDER_TOKEN = "replace-with-botfather-token"
DEFAULT_TEXT_MODEL = "DeepSeek-V4-Flash"
DEFAULT_VISION_MODEL = "DeepSeek-V4.1-Flash"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    radeon_api_key: str
    allowed_chat_ids: frozenset[str]
    catalog_path: Path
    db_path: Path
    rule: MarginRule
    pricing: PricingConfig
    list_format: ListFormat
    text_model: str
    vision_model: str
    base_url: str


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = _text(env, name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not value.is_finite():
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    return value


def _bool(env: Mapping[str, str], name: str, default: str) -> bool:
    raw = _text(env, name, default).lower()
    if raw in {"true", "1", "yes", "si", "sí"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def load_settings(env: Mapping[str, str]) -> Settings:
    token = _text(env, "TELEGRAM_BOT_TOKEN")
    if not token or token == PLACEHOLDER_TOKEN:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set: create a bot with @BotFather and put its token in .env")
    api_key = _text(env, "RADEON_API_KEY")
    if not api_key:
        raise ConfigError("RADEON_API_KEY is not set: put your AMD Token Factory key in .env")

    catalog_path = Path(_text(env, "LISTAYA_CATALOG_PATH", "data/catalogo.csv"))
    if not catalog_path.is_file():
        raise ConfigError(
            f"catalog not found at {catalog_path} (LISTAYA_CATALOG_PATH); to try the demo, copy "
            "samples/ferreteria/catalogo.csv there"
        )

    target = _decimal(env, "LISTAYA_TARGET_MARGIN", "0.30")
    minimum = _decimal(env, "LISTAYA_MIN_MARGIN", "0.20")
    try:
        rule = MarginRule(target_margin=target, min_margin=minimum)
    except ValueError as error:
        raise ConfigError(f"LISTAYA_TARGET_MARGIN / LISTAYA_MIN_MARGIN: {error}") from None

    currency = _text(env, "LISTAYA_CURRENCY", "ARS").upper()
    try:
        pricing = PricingConfig(
            rounding_step=_decimal(env, "LISTAYA_ROUNDING_STEP", "10"),
            vat_rate=_decimal(env, "LISTAYA_VAT_RATE", "0.21"),
            prices_include_vat=_bool(env, "LISTAYA_PRICES_INCLUDE_VAT", "false"),
            currency=currency,
        )
    except ValueError as error:
        raise ConfigError(f"LISTAYA_ROUNDING_STEP / LISTAYA_VAT_RATE: {error}") from None
    try:
        list_format = ListFormat(
            decimal_separator=_text(env, "LISTAYA_DECIMAL_SEPARATOR", ","), local_currency=currency
        )
    except ValueError as error:
        raise ConfigError(f"LISTAYA_DECIMAL_SEPARATOR / LISTAYA_CURRENCY: {error}") from None

    allowed = frozenset(part.strip() for part in _text(env, "TELEGRAM_ALLOWED_CHAT_IDS").split(",") if part.strip())
    return Settings(
        telegram_token=token,
        radeon_api_key=api_key,
        allowed_chat_ids=allowed,
        catalog_path=catalog_path,
        db_path=Path(_text(env, "LISTAYA_DB_PATH", "data/listaya.db")),
        rule=rule,
        pricing=pricing,
        list_format=list_format,
        text_model=_text(env, "LISTAYA_TEXT_MODEL", DEFAULT_TEXT_MODEL),
        vision_model=_text(env, "LISTAYA_VISION_MODEL", DEFAULT_VISION_MODEL),
        base_url=_text(env, "TOKEN_FACTORY_BASE_URL", DEFAULT_BASE_URL),
    )


async def _check_models(settings: Settings) -> None:
    from listaya.token_factory import TokenFactoryClient

    client = TokenFactoryClient(settings.radeon_api_key, base_url=settings.base_url)
    try:
        # PRD 9.1: the model catalog changes; fail at startup with a clear message.
        await client.validate_models([settings.text_model, settings.vision_model])
    finally:
        await client.aclose()


def main() -> None:
    from dotenv import load_dotenv

    from listaya.assistant import Assistant, current_run_id
    from listaya.catalog import CsvCatalog
    from listaya.extraction.model import ModelExtractor
    from listaya.extraction.router import ListExtractor
    from listaya.matching import Matcher
    from listaya.store import SqliteStore
    from listaya.telegram_channel import TelegramChannel, build_application
    from listaya.token_factory import TokenFactoryClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Keep the bot token out of the logs: httpx logs every request URL, which contains it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    try:
        settings = load_settings(os.environ)
    except ConfigError as error:
        raise SystemExit(f"Configuration error: {error}") from None
    if not settings.allowed_chat_ids:
        logging.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty: the bot will only reply with each chat's id")
    elif "*" in settings.allowed_chat_ids:
        logging.warning("TELEGRAM_ALLOWED_CHAT_IDS is *: any chat can use the bot and update the shared catalog")
    asyncio.run(_check_models(settings))

    store = SqliteStore(settings.db_path)
    client = TokenFactoryClient(
        settings.radeon_api_key,
        base_url=settings.base_url,
        on_usage=lambda record: store.log_usage(current_run_id.get(), record),
    )
    extractor = ListExtractor(
        ModelExtractor(
            client,
            text_model=settings.text_model,
            vision_model=settings.vision_model,
            list_format=settings.list_format,
        ),
        settings.list_format,
    )

    assistant: Assistant | None = None

    async def handle(message) -> None:
        assert assistant is not None
        await assistant.handle(message)

    application = build_application(settings.telegram_token, handle)
    assistant = Assistant(
        channel=TelegramChannel(application.bot),
        extractor=extractor,
        matcher_factory=lambda catalog: Matcher(catalog, store, client, model=settings.text_model),
        catalog=CsvCatalog(settings.catalog_path),
        store=store,
        rule=settings.rule,
        pricing=settings.pricing,
        allowed_chats=settings.allowed_chat_ids,
    )
    application.run_polling()

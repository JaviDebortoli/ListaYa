"""The extraction contract: one validated row per product line of a supplier list.

Rows come from code (spreadsheets) or from a model (images, PDFs, text). Either
way they pass through validate_rows, which parses amounts with code and marks
anything missing or unreadable instead of filling it in.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

CURRENCIES = frozenset({"ARS", "USD"})

_CURRENCY_TOKENS = re.compile(r"(?i)(u\$s|us\$|ar\$|ars|usd|\$)")
_DIGITS_AND_SEPARATORS = re.compile(r"\d[\d.,]*")

# How price lists say whether VAT is included (Spanish and English), matched on lowercase text without accents.
# Whole words only, so ordinary words such as "masiva" never read as "mas iva".
_VAT_INCLUDED = re.compile(
    r"(\bc/|\bcon\b)\s*iva\b|\biva\s*incl|\bincl\.?\s*(vat|tax)\b|\b(vat|tax)\s*incl"
)
_VAT_EXCLUDED = re.compile(
    r"(\bs/|\bsin\b|\+|\bmas\b)\s*iva\b|(\bex|\bexcl?)\.?\s*(vat|tax)\b|(\+|\bplus\b)\s*(vat|tax)\b|\bnet\b"
)
# Lines that talk about the list's prices; a note such as "Flete + IVA" does not.
_PRICE_STATEMENT_LINE = re.compile(r"\b(precio|precios|lista|valores|importes|price|prices)\b")
# Short standalone lines ("IVA incluido", "+ IVA") also state it, unless they are about another charge.
_STANDALONE_MAX_WORDS = 4
_OTHER_CHARGE = re.compile(r"\b(flete|fletes|envio|envios|shipping|freight|delivery)\b")
_EXCEPTION = re.compile(r"\b(excepto|salvo|menos|except|excluding)\b")


def vat_statement(text: str) -> bool | None:
    """True if the text says prices include VAT, False if they exclude it, None if it says neither or both."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    # Drop the rate so "IVA 21% incluido" reads like "IVA incluido".
    plain = re.sub(r"\(?\d+(?:[.,]\d+)?\s*%\)?", " ", plain)
    included, excluded = bool(_VAT_INCLUDED.search(plain)), bool(_VAT_EXCLUDED.search(plain))
    if included == excluded:
        return None
    return included


def list_vat_statement(text: str) -> bool | None:
    """VAT convention stated by a whole list, or None when it does not say or contradicts itself.

    Lines about the list's prices ("Precios + IVA", even "Precios con IVA, flete incluido")
    decide first. Only when none of them states VAT do short standalone lines count
    ("IVA incluido", or "+ IVA" under a "LISTA DE PRECIOS" title), and never short lines
    with an amount, an exception ("Excepto herramientas: + IVA") or another charge ("Flete + IVA").
    """
    price_lines: set[bool] = set()
    short_lines: set[bool] = set()
    for line in text.splitlines():
        statement = vat_statement(line)
        if statement is None:
            continue
        plain = unicodedata.normalize("NFKD", line).encode("ascii", "ignore").decode("ascii").lower()
        charge = _OTHER_CHARGE.search(plain)
        vat_word = re.search(r"\b(iva|vat|tax)\b", plain)
        if _PRICE_STATEMENT_LINE.search(plain):
            # "Precios + IVA - Envío sin cargo" is about the list; "Precio del flete + IVA" is about freight.
            if not (charge and vat_word and charge.start() < vat_word.start()):
                price_lines.add(statement)
        elif (
            len(re.findall(r"[a-z]+", plain)) <= _STANDALONE_MAX_WORDS
            # A rate such as "21%" is fine; any other number means a product row with an amount.
            and not re.search(r"\d", re.sub(r"\d+(?:[.,]\d+)?\s*%", "", plain))
            and not _OTHER_CHARGE.search(plain)
            and not _EXCEPTION.search(plain)
        ):
            short_lines.add(statement)
    statements = price_lines or short_lines
    return statements.pop() if len(statements) == 1 else None


@dataclass(frozen=True)
class ExtractedRow:
    supplier_code: str | None
    description: str
    pack_size: Decimal | None
    unit: str | None
    unit_cost: Decimal | None
    currency: str | None
    includes_vat: bool | None
    source_row: int
    confidence: float
    raw_cost: str | None  # the cost exactly as it appeared, for showing doubtful rows to the merchant
    issues: tuple[str, ...] = ()


def _grouped(integer_part: str, separator: str) -> bool:
    """True if integer_part is digits with optional thousands groups using separator (1.234.567)."""
    if separator not in integer_part:
        return integer_part.isdigit()
    return re.fullmatch(rf"\d{{1,3}}(?:{re.escape(separator)}\d{{3}})+", integer_part) is not None


@dataclass(frozen=True)
class ListFormat:
    """How the merchant's suppliers write prices: Argentina uses '1.234,56' and '$' for pesos;
    a US-style list uses '1,234.56' and '$' for dollars."""

    decimal_separator: str = ","
    local_currency: str = "ARS"  # what a bare "$" means

    def __post_init__(self) -> None:
        if self.decimal_separator not in {",", "."}:
            raise ValueError("decimal_separator must be ',' or '.'")
        if self.local_currency not in CURRENCIES:
            raise ValueError(f"local_currency must be one of {sorted(CURRENCIES)}")


def parse_amount(text: str, decimal_separator: str = ",") -> Decimal | None:
    """Parse a price as written in a supplier list: '$ 1.234,56' -> 1234.56. None if unreadable.

    With decimal_separator ',' (Argentina) the dot groups thousands; with '.' the comma
    does. When both appear, the last one is the decimal separator either way. A lone
    thousands separator followed by exactly three digits ('1.234' or '1,250') groups thousands.
    """
    cleaned = _CURRENCY_TOKENS.sub("", text).replace(" ", "").replace("\u00a0", "")
    if not _DIGITS_AND_SEPARATORS.fullmatch(cleaned):
        return None
    decimal_sep = decimal_separator
    thousands_sep = "." if decimal_sep == "," else ","

    if "." in cleaned and "," in cleaned:
        decimal_sep = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        integer_part, _, fraction = cleaned.rpartition(decimal_sep)
        if decimal_sep in integer_part or not fraction.isdigit() or not _grouped(integer_part, thousands_sep):
            return None
        normalized = integer_part.replace(thousands_sep, "") + "." + fraction
    elif decimal_sep in cleaned:
        if cleaned.count(decimal_sep) == 1:
            integer_part, _, fraction = cleaned.partition(decimal_sep)
            if not fraction.isdigit():
                return None
            normalized = integer_part + "." + fraction
        elif _grouped(cleaned, decimal_sep):
            normalized = cleaned.replace(decimal_sep, "")
        else:
            return None
    elif thousands_sep in cleaned:
        lookalike = rf"[1-9]\d{{0,2}}{re.escape(thousands_sep)}\d{{3}}"
        if cleaned.count(thousands_sep) > 1 or re.fullmatch(lookalike, cleaned):
            if not _grouped(cleaned, thousands_sep):
                return None
            normalized = cleaned.replace(thousands_sep, "")
        else:
            integer_part, _, fraction = cleaned.partition(thousands_sep)
            if not fraction.isdigit():
                return None
            normalized = integer_part + "." + fraction
    else:
        normalized = cleaned

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_decimal(value: int | float | Decimal) -> Decimal | None:
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _positive_number(value: Any, decimal_separator: str) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = _finite_decimal(value)
        if number is None:
            return None
    elif isinstance(value, str):
        number = parse_amount(value, decimal_separator)
        if number is None:
            return None
    else:
        return None
    return number if number.is_finite() and number > 0 else None


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else 0.0


def validate_rows(items: Iterable[Any], list_format: ListFormat | None = None) -> list[ExtractedRow]:
    """Validate raw row dicts (from a model or a spreadsheet) into ExtractedRow values."""
    fmt = list_format or ListFormat()
    rows: list[ExtractedRow] = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        issues: list[str] = []

        description = _text(item.get("description"))
        if description is None:
            issues.append("missing_description")

        cost_value = item.get("unit_cost_text")
        raw_cost = _text(cost_value)
        unit_cost = None
        if raw_cost is None:
            issues.append("missing_cost")
        else:
            # Real numbers (spreadsheet cells) are already numbers; only text goes through the Argentine parser.
            is_number = isinstance(cost_value, (int, float, Decimal)) and not isinstance(cost_value, bool)
            unit_cost = (
                _finite_decimal(cost_value) if is_number else parse_amount(raw_cost, fmt.decimal_separator)
            )
            if unit_cost is None:
                issues.append("unparseable_cost")
            elif unit_cost <= 0:
                issues.append("non_positive_cost")

        pack_size = None
        if item.get("pack_size") is not None:
            pack_size = _positive_number(item.get("pack_size"), fmt.decimal_separator)
            if pack_size is None:
                issues.append("invalid_pack_size")

        currency = (_text(item.get("currency")) or "").upper()
        includes_vat = item.get("includes_vat")
        source_row = item.get("source_row")

        rows.append(
            ExtractedRow(
                supplier_code=_text(item.get("supplier_code")),
                description=description or "",
                pack_size=pack_size,
                unit=_text(item.get("unit")),
                unit_cost=unit_cost,
                currency=currency if currency in CURRENCIES else None,
                includes_vat=includes_vat if isinstance(includes_vat, bool) else None,
                source_row=source_row if isinstance(source_row, int) and not isinstance(source_row, bool) else position,
                confidence=_confidence(item.get("confidence")),
                raw_cost=raw_cost,
                issues=tuple(issues),
            )
        )
    return rows

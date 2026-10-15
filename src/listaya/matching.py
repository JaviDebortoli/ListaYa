"""Matching supplier rows to the merchant's catalog, in stages (PRD section 8.4).

1. Supplier code already confirmed for a product (alias).
2. Description already confirmed by the merchant (alias).
3. Approximate text similarity in code, plus an attribute check: measures in the
   names (6x1, 2.25 L) and pack sizes must agree.
4. Only rows in the doubtful band go to the model, in batches, with their closest
   candidates. The model narrows the choice to one suggestion or rejects all of
   them; it never matches on its own.
5. Everything else is unmatched and reported.

Nothing becomes a match through the model: doubtful rows become questions for
the merchant, and each answer is saved as an alias so it is not asked again.
"""

from __future__ import annotations

import enum
import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, Sequence

from rapidfuzz import fuzz, process

from listaya.catalog import CatalogProduct
from listaya.extraction.rows import ExtractedRow
from listaya.token_factory import TokenFactoryError

MAX_CANDIDATES = 5
MODEL_BATCH_SIZE = 30

SYSTEM_PROMPT = """You match lines from a supplier price list to products in a store's catalog.
For each item, choose the catalog product that is the same product: same kind, measure, size and
presentation. If none of the candidates is the same product, answer null. Do not guess.
Reply with a JSON object: {"decisions": [{"row": <row number>, "sku": "<chosen sku>" or null}]}."""


class MatchStatus(str, enum.Enum):
    MATCHED = "matched"
    NEEDS_CONFIRMATION = "needs_confirmation"
    UNMATCHED = "unmatched"


@dataclass(frozen=True)
class MatchResult:
    row: ExtractedRow
    status: MatchStatus
    sku: str | None  # set only when matched
    suggestion: str | None  # product to propose when the merchant must confirm
    method: str  # code, alias, fuzzy, model or none
    score: float
    candidates: tuple[tuple[str, float], ...] = ()


class AliasStore(Protocol):
    def get(self, supplier_id: str, key: str) -> str | None: ...

    def save(self, supplier_id: str, key: str, sku: str) -> None: ...


class InMemoryAliasStore:
    def __init__(self) -> None:
        self._aliases: dict[tuple[str, str], str] = {}

    def get(self, supplier_id: str, key: str) -> str | None:
        return self._aliases.get((supplier_id, key))

    def save(self, supplier_id: str, key: str, sku: str) -> None:
        self._aliases[(supplier_id, key)] = sku


class ChatClient(Protocol):
    async def chat(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...


def normalize_description(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    plain = re.sub(r"(?<=\d),(?=\d)", ".", plain)  # 2,25 -> 2.25
    plain = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", plain)  # keep dots only inside numbers
    plain = re.sub(r"[^a-z0-9.]+", " ", plain)
    return " ".join(plain.split())


def measure_numbers(text: str) -> set[Decimal]:
    """Numbers that describe the product (6x1, 2.25 L), without pack counts such as 'x6' or 'x 100'."""
    normalized = normalize_description(text)
    without_packs = re.sub(r"(?<![\w.])x\s*\d+(?:\.\d+)?", " ", normalized)
    return {Decimal(number) for number in re.findall(r"\d+(?:\.\d+)?", without_packs)}


def attributes_compatible(row: ExtractedRow, product: CatalogProduct) -> bool:
    row_numbers, product_numbers = measure_numbers(row.description), measure_numbers(product.name)
    if row_numbers and product_numbers and row_numbers != product_numbers:
        return False
    if row.pack_size is not None and product.pack_size is not None and row.pack_size != product.pack_size:
        return False
    return True


def code_key(code: str) -> str:
    return "code:" + code.strip().lower()


def description_key(description: str) -> str:
    return "desc:" + normalize_description(description)


class Matcher:
    def __init__(
        self,
        catalog: Sequence[CatalogProduct],
        aliases: AliasStore,
        client: ChatClient,
        *,
        model: str,
        auto_accept: float = 0.90,
        ask_from: float = 0.60,
    ) -> None:
        if not 0.0 <= ask_from <= auto_accept <= 1.0:
            raise ValueError("thresholds must satisfy 0 <= ask_from <= auto_accept <= 1")
        self._products = {product.sku: product for product in catalog}
        self._names = {product.sku: normalize_description(product.name) for product in catalog}
        self._aliases = aliases
        self._client = client
        self._model = model
        self._auto_accept = auto_accept
        self._ask_from = ask_from

    async def match(self, supplier_id: str, rows: Sequence[ExtractedRow]) -> list[MatchResult]:
        results: list[MatchResult | None] = []
        doubtful: list[tuple[int, ExtractedRow, tuple[tuple[str, float], ...]]] = []
        for index, row in enumerate(rows):
            result, candidates = self._match_in_code(supplier_id, row)
            results.append(result)
            if result is None:
                doubtful.append((index, row, candidates))

        for start in range(0, len(doubtful), MODEL_BATCH_SIZE):
            batch = doubtful[start : start + MODEL_BATCH_SIZE]
            for (index, _, _), result in zip(batch, await self._adjudicate(batch)):
                results[index] = result
        return [result for result in results if result is not None]

    def confirm(self, supplier_id: str, row: ExtractedRow, sku: str) -> None:
        """Remember the merchant's answer so the same supplier line is matched automatically next time."""
        if sku not in self._products:
            raise ValueError(f"unknown sku {sku!r}")
        if row.supplier_code:
            self._aliases.save(supplier_id, code_key(row.supplier_code), sku)
        if row.description.strip():
            self._aliases.save(supplier_id, description_key(row.description), sku)

    def _alias(self, supplier_id: str, key: str) -> str | None:
        sku = self._aliases.get(supplier_id, key)
        return sku if sku in self._products else None

    def _match_in_code(
        self, supplier_id: str, row: ExtractedRow
    ) -> tuple[MatchResult | None, tuple[tuple[str, float], ...]]:
        if row.supplier_code:
            sku = self._alias(supplier_id, code_key(row.supplier_code))
            if sku:
                return MatchResult(row, MatchStatus.MATCHED, sku, None, "code", 1.0), ()
        if not row.description.strip():
            return MatchResult(row, MatchStatus.UNMATCHED, None, None, "none", 0.0), ()
        sku = self._alias(supplier_id, description_key(row.description))
        if sku:
            return MatchResult(row, MatchStatus.MATCHED, sku, None, "alias", 1.0), ()
        if not self._names:
            return MatchResult(row, MatchStatus.UNMATCHED, None, None, "none", 0.0), ()

        found = process.extract(
            normalize_description(row.description), self._names, scorer=fuzz.WRatio, limit=MAX_CANDIDATES
        )
        candidates = tuple((sku, score / 100) for _, score, sku in found)
        best_sku, best_score = candidates[0]
        if best_score >= self._auto_accept and attributes_compatible(row, self._products[best_sku]):
            return MatchResult(row, MatchStatus.MATCHED, best_sku, None, "fuzzy", best_score, candidates), candidates
        if best_score < self._ask_from:
            return MatchResult(row, MatchStatus.UNMATCHED, None, None, "none", best_score, candidates), candidates
        return None, candidates

    async def _adjudicate(
        self, batch: list[tuple[int, ExtractedRow, tuple[tuple[str, float], ...]]]
    ) -> list[MatchResult]:
        items = []
        for number, (_, row, candidates) in enumerate(batch, start=1):
            line = row.description
            if row.supplier_code:
                line = f"[{row.supplier_code}] {line}"
            if row.pack_size is not None:
                line += f" (pack of {row.pack_size})"
            items.append(
                {
                    "row": number,
                    "supplier_line": line,
                    "candidates": [{"sku": sku, "name": self._products[sku].name} for sku, _ in candidates],
                }
            )
        decisions = await self._ask_model(items)

        results = []
        for number, (_, row, candidates) in enumerate(batch, start=1):
            best_sku, best_score = candidates[0]
            allowed = {sku for sku, _ in candidates}
            if number in decisions and decisions[number] is None:
                results.append(MatchResult(row, MatchStatus.UNMATCHED, None, None, "model", best_score, candidates))
            elif decisions.get(number) in allowed:
                chosen = decisions[number]
                score = dict(candidates)[chosen]
                results.append(
                    MatchResult(row, MatchStatus.NEEDS_CONFIRMATION, None, chosen, "model", score, candidates)
                )
            else:
                # No usable answer from the model: ask the merchant about the closest candidate.
                results.append(
                    MatchResult(row, MatchStatus.NEEDS_CONFIRMATION, None, best_sku, "fuzzy", best_score, candidates)
                )
        return results

    async def _ask_model(self, items: list[dict[str, Any]]) -> dict[int, str | None]:
        try:
            result = await self._client.chat(
                self._model,
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
                ],
                max_tokens=4000,
                response_format={"type": "json_object"},
                temperature=0,
            )
            body = json.loads(result.content)
        except (TokenFactoryError, ValueError):
            return {}
        decisions: dict[int, str | None] = {}
        entries = body.get("decisions") if isinstance(body, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("row"), int):
                sku = entry.get("sku")
                decisions[entry["row"]] = str(sku) if sku is not None else None
        return decisions

"""Structuring price lists with models served on AMD Token Factory.

The model only transcribes: it returns each price exactly as printed and the
code parses it (see rows.parse_amount), so the model never computes amounts.
Text is numbered by line and sent in batches; images go one per request to a
vision model. A batch or page that fails is reported, never silently dropped.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from listaya.extraction.rows import ExtractedRow, validate_rows
from listaya.token_factory import TokenFactoryError

SYSTEM_PROMPT = """You extract product rows from a supplier price list for a store in Argentina.
Reply with a JSON object: {"rows": [ ... ]}, one entry per product line, in the order they appear.
Each entry has exactly these fields:
- "source_row": for text input, the number after "L" at the start of the line; for images, 1, 2, 3...
- "supplier_code": the supplier's product code, or null if there is none.
- "description": the product name as printed.
- "pack_size": units per pack or box if the line states it (e.g. "x 100", "caja 12"), else null.
- "unit": the unit or presentation as printed (e.g. "unidad", "caja", "kg", "lt"), else null.
- "unit_cost_text": the price exactly as printed, keeping its separators (e.g. "1.234,56"); null if missing or unreadable.
- "currency": "USD" if the list marks the price in dollars (US$, U$S, USD), "ARS" for pesos ($ or ARS), else null.
- "includes_vat": true if the list says prices include VAT (IVA incluido), false if it says VAT is extra (+ IVA), else null.
- "confidence": your confidence from 0 to 1 that this entry is read correctly.
Never invent, complete or calculate values. Skip titles, headers, totals and lines that are not products.
If a line is illegible, still include it with null fields and low confidence."""


class ChatClient(Protocol):
    async def chat(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ExtractionResult:
    rows: list[ExtractedRow]
    problems: list[str] = field(default_factory=list)  # human-readable, one per failed batch or page


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def _parse_rows(content: str) -> list[Any] | None:
    """Return the "rows" list from the model reply, or None if it is not valid JSON of that shape."""
    try:
        body = json.loads(_FENCE.sub("", content.strip()))
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    rows = body.get("rows", [])
    return rows if isinstance(rows, list) else None


class ModelExtractor:
    def __init__(
        self,
        client: ChatClient,
        *,
        text_model: str,
        vision_model: str,
        batch_lines: int = 60,
        max_tokens: int = 8000,
        reasoning_effort: str | None = None,
    ) -> None:
        if batch_lines < 1:
            raise ValueError("batch_lines must be >= 1")
        self._client = client
        self._text_model = text_model
        self._vision_model = vision_model
        self._batch_lines = batch_lines
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort

    async def extract_text(self, text: str) -> ExtractionResult:
        lines = [line.strip() for line in text.splitlines()]
        numbered = [(n, line) for n, line in enumerate(lines, start=1) if line]
        batches = [numbered[i : i + self._batch_lines] for i in range(0, len(numbered), self._batch_lines)]
        outcomes = await asyncio.gather(*(self._extract_batch(batch) for batch in batches))

        rows: list[ExtractedRow] = []
        problems: list[str] = []
        for batch_rows, problem in outcomes:
            rows.extend(batch_rows)
            if problem:
                problems.append(problem)
        return ExtractionResult(rows, problems)

    async def extract_images(self, images: Sequence[tuple[bytes, str]]) -> ExtractionResult:
        """images: (content, mime type) per page or photo, in reading order."""
        outcomes = await asyncio.gather(*(self._extract_image(content, mime) for content, mime in images))

        rows: list[ExtractedRow] = []
        problems: list[str] = []
        for page, (page_rows, problem) in enumerate(outcomes, start=1):
            if problem:
                problems.append(f"page {page}: {problem}")
            for page_row in page_rows:
                rows.append(dataclasses.replace(page_row, source_row=len(rows) + 1))
        return ExtractionResult(rows, problems)

    async def _extract_batch(self, batch: list[tuple[int, str]]) -> tuple[list[ExtractedRow], str | None]:
        span = f"lines {batch[0][0]}-{batch[-1][0]}"
        user_text = "\n".join(f"L{n}: {line}" for n, line in batch)
        rows, problem = await self._ask(self._text_model, user_text)
        return rows, f"{span}: {problem}" if problem else None

    async def _extract_image(self, content: bytes, mime: str) -> tuple[list[ExtractedRow], str | None]:
        data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        user_content = [
            {"type": "text", "text": "Extract the product rows from this price list image."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return await self._ask(self._vision_model, user_content)

    async def _ask(self, model: str, user_content: Any) -> tuple[list[ExtractedRow], str | None]:
        try:
            result = await self._client.chat(
                model,
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
                reasoning_effort=self._reasoning_effort,
                temperature=0,
            )
        except TokenFactoryError as error:
            return [], str(error)
        raw_rows = _parse_rows(result.content)
        if raw_rows is None:
            return [], "the model did not return valid JSON"
        return validate_rows(raw_rows), None

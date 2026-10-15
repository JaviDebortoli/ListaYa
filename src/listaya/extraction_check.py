"""Manual extraction run against the real Token Factory API, to compare models (decision D-01).

    python -m listaya.extraction_check lista.pdf
    python -m listaya.extraction_check foto.jpg --vision-model MiMo-V2.6-Flash
    python -m listaya.extraction_check --text "Tornillo 6x1 $1.234,56"

Prints every extracted row with its issues, the problems found, and the
tokens, cost-relevant usage and time spent. Reads RADEON_API_KEY from .env.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from listaya.extraction.model import ModelExtractor
from listaya.extraction.router import ListExtractor
from listaya.token_factory import DEFAULT_BASE_URL, TokenFactoryClient, UsageRecord

DEFAULT_TEXT_MODEL = "DeepSeek-V4-Flash"
DEFAULT_VISION_MODEL = "DeepSeek-V4.1-Flash"


async def run(args: argparse.Namespace) -> None:
    api_key = os.environ.get("RADEON_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("RADEON_API_KEY is not set")
    usage: list[UsageRecord] = []
    client = TokenFactoryClient(
        api_key,
        base_url=os.environ.get("TOKEN_FACTORY_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        on_usage=usage.append,
    )
    extractor = ListExtractor(ModelExtractor(client, text_model=args.text_model, vision_model=args.vision_model))
    started = time.monotonic()
    try:
        if args.text is not None:
            result = await extractor.extract_pasted_text(args.text)
        else:
            path = Path(args.file)
            result = await extractor.extract_file(path.read_bytes(), path.name, None)
    finally:
        await client.aclose()
    elapsed = time.monotonic() - started

    for row in result.rows:
        issues = f"  issues={','.join(row.issues)}" if row.issues else ""
        print(
            f"[{row.source_row}] {row.supplier_code or '-'} | {row.description} | pack={row.pack_size} "
            f"unit={row.unit} | cost={row.unit_cost} (raw {row.raw_cost!r}) {row.currency or '?'} "
            f"vat={row.includes_vat} conf={row.confidence:.2f}{issues}"
        )
    for problem in result.problems:
        print(f"PROBLEM: {problem}")
    prompt = sum(record.prompt_tokens for record in usage)
    completion = sum(record.completion_tokens for record in usage)
    print(
        f"\n{len(result.rows)} rows, {sum(1 for r in result.rows if r.issues)} with issues, "
        f"{len(usage)} model calls, {prompt} prompt + {completion} completion tokens, {elapsed:.1f} s"
    )


def main() -> None:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("file", nargs="?", help="supplier list: PDF, Excel/CSV, image or .txt")
    source.add_argument("--text", help="pasted list text instead of a file")
    parser.add_argument("--text-model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--vision-model", default=DEFAULT_VISION_MODEL)
    args = parser.parse_args()
    load_dotenv()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

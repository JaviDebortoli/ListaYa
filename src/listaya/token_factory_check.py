"""Manual check against the real Token Factory API.

    python -m listaya.token_factory_check            # list models and show usage (no model call)
    python -m listaya.token_factory_check --chat     # also send one tiny chat request

Reads RADEON_API_KEY from the environment or .env. Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from listaya.token_factory import DEFAULT_BASE_URL, TokenFactoryClient

CHECK_MODEL = "DeepSeek-V4-Flash"


async def run(chat: bool) -> None:
    api_key = os.environ.get("RADEON_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("RADEON_API_KEY is not set")
    base_url = os.environ.get("TOKEN_FACTORY_BASE_URL", "").strip() or DEFAULT_BASE_URL
    client = TokenFactoryClient(api_key, base_url=base_url, on_usage=lambda record: print(f"usage: {record}"))
    try:
        models = await client.list_models()
        print(f"models ({len(models)}): {', '.join(models)}")
        print("usage:", json.dumps(await client.usage(), indent=2, ensure_ascii=False))
        if chat:
            result = await client.chat(
                CHECK_MODEL,
                [{"role": "user", "content": "Say hello in one sentence."}],
                max_tokens=50,
            )
            print(f"chat ({result.latency_seconds:.1f}s): {result.content}")
            print(f"rate limit headers: {result.rate_limit}")
    finally:
        await client.aclose()


def main() -> None:
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chat", action="store_true", help="also send one tiny chat request")
    args = parser.parse_args()
    load_dotenv()
    asyncio.run(run(args.chat))


if __name__ == "__main__":
    main()

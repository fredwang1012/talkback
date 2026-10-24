"""Probe (c): mint a client secret."""
import asyncio

from _common import BosonClient, flush_notes, note


async def main() -> None:
    s = await BosonClient().mint_client_secret(300)
    note(f"client secret minted: prefix ok={s['value'].startswith('bai-eph-')}, expires_at={s['expires_at']}, keys={sorted(s)}")
    flush_notes("client secret (c)")


asyncio.run(main())

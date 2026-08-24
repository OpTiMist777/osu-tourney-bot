"""Populate cached osu! metadata for pools migrated from SQLite.

Run this once after the Mongo migration. It is safe to run again: every map is
refreshed from osu! API and the resulting snapshot replaces the old cache.
"""

import asyncio

from cogs.pool_commands import PoolCommands
from database import _db, init_db


async def backfill() -> None:
    await init_db()
    parser = PoolCommands(None)
    updated = failed = 0

    async for pool in _db().pools.find({}):
        maps = pool.get("maps", [])
        print(f"📦 Пул {pool['pool_id']}: {pool['name']} · карт: {len(maps)}")
        changed = False
        for map_document in maps:
            try:
                map_document["snapshot"] = await parser._parse_map_snapshot(
                    map_document["slot"], map_document["beatmap_id"], pool["mode"]
                )
                map_document["beatmapset_id"] = map_document["snapshot"]["beatmapset_id"]
                map_document["difficulty_name"] = map_document["snapshot"]["difficulty_name"]
                map_document["mods"] = map_document["snapshot"]["mods"]
                changed = True
                updated += 1
            except Exception as error:
                failed += 1
                print(f"Could not parse pool {pool['pool_id']} / {map_document['slot']}: {error}")
        if changed:
            await _db().pools.update_one({"_id": pool["_id"]}, {"$set": {"maps": maps}})
            print(f"💾 Пул {pool['pool_id']} сохранён в MongoDB")

    print(f"Snapshot backfill complete: updated {updated}, failed {failed}.")


if __name__ == "__main__":
    asyncio.run(backfill())

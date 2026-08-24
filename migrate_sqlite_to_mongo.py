"""One-time, idempotent migration of local SQLite pools to MongoDB."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from database import _db, init_db

SQLITE_PATH = Path(__file__).with_name("tourney.db")


def parse_timestamp(value: str | None):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None


async def migrate() -> None:
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"SQLite backup not found: {SQLITE_PATH}")

    await init_db()
    connection = sqlite3.connect(SQLITE_PATH)
    connection.row_factory = sqlite3.Row
    migrated = skipped = 0
    try:
        for row in connection.execute("SELECT * FROM pools ORDER BY id"):
            pool_id = row["id"]
            if await _db().pools.find_one({"pool_id": pool_id}, {"_id": 1}):
                skipped += 1
                continue
            maps = [
                {"slot": item["slot"].upper(), "beatmap_id": item["beatmap_id"],
                 "beatmapset_id": item["beatmapset_id"], "difficulty_name": item["difficulty_name"],
                 "mods": item["mods"]}
                for item in connection.execute("SELECT * FROM pool_maps WHERE pool_id = ? ORDER BY slot", (pool_id,))
            ]
            logs = [
                {"action": item["action"], "moderator_id": item["moderator_id"], "reason": item["reason"],
                 "created_at": parse_timestamp(item["created_at"]) or datetime.now(timezone.utc)}
                for item in connection.execute("SELECT * FROM pool_logs WHERE pool_id = ? ORDER BY id", (pool_id,))
            ]
            await _db().pools.insert_one({
                "pool_id": pool_id, "name": row["name"], "mode": row["mode"], "status": row["status"],
                "allow_converts": bool(row["allow_converts"]), "created_by": row["created_by"],
                "approved_by": row["approved_by"], "created_at": parse_timestamp(row["created_at"]),
                "approved_at": parse_timestamp(row["approved_at"]), "maps": maps, "logs": logs,
            })
            migrated += 1
        next_id = max((await _db().pools.distinct("pool_id")), default=0) + 1
        await _db().counters.update_one({"_id": "pools"}, {"$set": {"next_id": next_id}}, upsert=True)
    finally:
        connection.close()
    print(f"Migration complete: migrated {migrated}, skipped {skipped} existing pools.")


if __name__ == "__main__":
    asyncio.run(migrate())

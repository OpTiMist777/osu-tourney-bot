"""MongoDB persistence for tournament map pools."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import PyMongoError

load_dotenv()
MONGODB_URI = os.getenv("MONGODB_URI")
DATABASE_NAME = "osu_tourney_dev"
_client: Optional[AsyncMongoClient] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slot(slot: str) -> str:
    return slot.strip().upper()


def _name_key(name: str) -> str:
    """Return the canonical key used for case-insensitive pool-name lookups.

    Pool names are user-facing, so their original spelling is kept in ``name``.
    The key deliberately only trims surrounding whitespace: names which differ in
    the middle are still distinct names.
    """
    return name.strip().casefold()


def _map_document(slot: str, beatmap_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a stored map record; snapshots make pool reads independent of osu! API."""
    document = {
        "slot": _slot(slot), "beatmap_id": beatmap_id, "beatmapset_id": 0,
        "difficulty_name": "Unknown", "mods": None, "snapshot": None,
    }
    if snapshot:
        document["beatmapset_id"] = snapshot.get("beatmapset_id", 0)
        document["difficulty_name"] = snapshot.get("difficulty_name", "Unknown")
        document["mods"] = snapshot.get("mods", [])
        document["snapshot"] = snapshot
    return document


def _pool(document: Optional[Dict[str, Any]]) -> Optional[Dict]:
    if document is None:
        return None
    document = dict(document)
    document.pop("_id", None)
    return document


def _db():
    if _client is None:
        raise RuntimeError("MongoDB client is not initialized")
    return _client[DATABASE_NAME]


async def init_db() -> None:
    global _client
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is not set. Add it to your private .env file.")
    if _client is None:
        _client = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    db = _db()
    await db.command("ping")
    await db.pools.create_index([("pool_id", ASCENDING)], unique=True)
    await db.pools.create_index([("status", ASCENDING)])
    await db.pools.create_index([("mode", ASCENDING)])
    await db.pools.create_index([("name_key", ASCENDING), ("status", ASCENDING)])
    # One-time backwards-compatible migration for pools created before the
    # public names changed from approved/rejected to ranked/unranked.
    await db.pools.update_many({"status": "approved"}, {"$set": {"status": "ranked"}})
    await db.pools.update_many({"status": "rejected"}, {"$set": {"status": "unranked"}})
    await db.pools.update_many(
        {"ranked_by": {"$exists": False}, "approved_by": {"$exists": True}},
        [{"$set": {"ranked_by": "$approved_by", "ranked_at": "$approved_at"}}],
    )
    # ``name_key`` was added after pools already existed.  Do this in Python
    # rather than with MongoDB's $toLower so Python's casefold behaviour is used
    # consistently for both old and newly created names.
    async for pool in db.pools.find({"name_key": {"$exists": False}}, {"_id": 1, "name": 1}):
        await db.pools.update_one(
            {"_id": pool["_id"]},
            {"$set": {"name_key": _name_key(str(pool.get("name", "")))}},
        )
    await db.counters.update_one({"_id": "pools"}, {"$setOnInsert": {"next_id": 1}}, upsert=True)
    print("✅ MongoDB инициализирована")


async def _next_pool_id() -> int:
    counter = await _db().counters.find_one_and_update(
        {"_id": "pools"}, {"$inc": {"next_id": 1}}, return_document=True
    )
    if counter is None:
        raise RuntimeError("Could not allocate pool ID")
    return int(counter["next_id"] - 1)


async def create_pool_with_maps(name: str, mode: str, created_by: int, maps: List[Tuple]) -> Tuple[int, str]:
    try:
        pool_id = await _next_pool_id()
        await _db().pools.insert_one({
            "pool_id": pool_id, "name": name.strip(), "name_key": _name_key(name),
            "mode": mode.lower().strip(), "status": "draft",
            "allow_converts": True, "created_by": created_by, "ranked_by": None,
            "created_at": _now(), "ranked_at": None,
            "maps": [
                _map_document(item[0], item[1], item[2] if len(item) > 2 else None)
                for item in maps
            ],
            "logs": [],
        })
        return pool_id, ""
    except PyMongoError as error:
        return -1, str(error)


async def get_pool(pool_id: int) -> Optional[Dict]:
    return _pool(await _db().pools.find_one({"pool_id": pool_id}))


async def get_pool_by_name(name: str) -> List[Dict]:
    """Get all pools with this exact name, ignoring case.

    Draft names are allowed to repeat, hence this intentionally returns a list
    instead of silently selecting an arbitrary pool.
    """
    cursor = _db().pools.find({"name_key": _name_key(name)}, {"_id": 0})
    return [item async for item in cursor.sort("pool_id", DESCENDING)]


async def list_pools(mode: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
    """List pool metadata and map counts; moderation history stays private."""
    query: Dict[str, Any] = {}
    if mode:
        query["mode"] = mode.lower().strip()
    if status:
        query["status"] = status.lower().strip()
    cursor = _db().pools.find(query, {"_id": 0, "logs": 0}).sort("pool_id", DESCENDING)
    return [item async for item in cursor]


async def pool_name_in_use_for_review(name: str, exclude_pool_id: Optional[int] = None) -> bool:
    """Whether a non-draft pool already owns ``name`` for review/public use."""
    query: Dict[str, Any] = {"name_key": _name_key(name), "status": {"$ne": "draft"}}
    if exclude_pool_id is not None:
        query["pool_id"] = {"$ne": exclude_pool_id}
    return await _db().pools.count_documents(query, limit=1) > 0


async def get_pool_maps(pool_id: int) -> List[Dict]:
    pool = await get_pool(pool_id)
    return list(pool.get("maps", [])) if pool else []


async def update_pool_status(pool_id: int, new_status: str, moderator_id: int = None) -> Tuple[bool, str]:
    try:
        updates: Dict[str, Any] = {"status": new_status}
        if new_status == "ranked":
            updates.update({"ranked_by": moderator_id, "ranked_at": _now()})
        result = await _db().pools.update_one({"pool_id": pool_id}, {"$set": updates})
        return result.matched_count == 1, "" if result.matched_count else "Пул не найден"
    except PyMongoError as error:
        return False, str(error)


async def set_moderation_message(pool_id: int, channel_id: int, message_id: int) -> bool:
    """Persist the moderator post so its persistent buttons can be restored."""
    result = await _db().pools.update_one(
        {"pool_id": pool_id},
        {"$set": {"moderation_channel_id": channel_id, "moderation_message_id": message_id}},
    )
    return result.matched_count == 1


async def get_pending_moderation_pools() -> List[Dict]:
    query = {
        "status": "pending",
        "moderation_channel_id": {"$exists": True},
        "moderation_message_id": {"$exists": True},
    }
    cursor = _db().pools.find(query, {"_id": 0, "maps": 0, "logs": 0})
    return [item async for item in cursor]


async def get_pools_by_status(status: str) -> List[Dict]:
    """Return all pools in a status, including maps needed for moderator posts."""
    cursor = _db().pools.find({"status": status}, {"_id": 0})
    return [item async for item in cursor.sort("pool_id", ASCENDING)]


async def log_moderation_action(pool_id: int, action: str, moderator_id: int, reason: str = None) -> bool:
    try:
        entry = {"action": action, "moderator_id": moderator_id, "reason": reason, "created_at": _now()}
        return (await _db().pools.update_one({"pool_id": pool_id}, {"$push": {"logs": entry}})).matched_count == 1
    except PyMongoError:
        return False


async def get_pool_logs(pool_id: int) -> List[Dict]:
    pool = await get_pool(pool_id)
    return sorted(pool.get("logs", []), key=lambda item: item["created_at"], reverse=True) if pool else []


async def get_pool_count() -> int:
    return await _db().pools.count_documents({})


async def get_recent_pools(limit: int = 10) -> List[Dict]:
    cursor = _db().pools.find({}, {"_id": 0, "maps": 0, "logs": 0}).sort("pool_id", DESCENDING).limit(limit)
    return [item async for item in cursor]


async def delete_pool(pool_id: int) -> Tuple[bool, str]:
    try:
        result = await _db().pools.delete_one({"pool_id": pool_id})
        return result.deleted_count == 1, "" if result.deleted_count else "Пул не найден"
    except PyMongoError as error:
        return False, str(error)


async def edit_pool_maps(pool_id: int, new_maps: List[Tuple[str, int]]) -> Tuple[bool, str]:
    pool = await get_pool(pool_id)
    if not pool:
        return False, "Пул не найден"
    categories = {"TB" if _slot(slot) == "TB" else "".join(filter(str.isalpha, _slot(slot))) for slot, _ in new_maps}
    retained = [item for item in pool.get("maps", []) if ("TB" if item["slot"] == "TB" else "".join(filter(str.isalpha, item["slot"]))) not in categories]
    retained.extend({"slot": _slot(slot), "beatmap_id": beatmap_id, "beatmapset_id": 0,
                     "difficulty_name": "Unknown", "mods": None} for slot, beatmap_id in new_maps)
    await _db().pools.update_one({"pool_id": pool_id}, {"$set": {"maps": retained}})
    return True, ""


async def update_pool_map(pool_id: int, slot: str, beatmap_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    slot = _slot(slot)
    result = await _db().pools.update_one(
        {"pool_id": pool_id, "maps.slot": slot},
        {"$set": {f"maps.$.{key}": value for key, value in _map_document(slot, beatmap_id, snapshot).items() if key != "slot"}},
    )
    return result.matched_count == 1, "" if result.matched_count else f"Слот `{slot}` не найден"


async def add_pool_map(pool_id: int, slot: str, beatmap_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    slot = _slot(slot)
    item = _map_document(slot, beatmap_id, snapshot)
    result = await _db().pools.update_one(
        {"pool_id": pool_id, "maps.slot": {"$ne": slot}}, {"$push": {"maps": item}}
    )
    return result.matched_count == 1, "" if result.matched_count else f"Слот `{slot}` уже существует или пул не найден"


async def close_database() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None

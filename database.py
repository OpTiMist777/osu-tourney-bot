"""MongoDB persistence for tournament map pools."""
from __future__ import annotations

import asyncio
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
_osu_link_lock = asyncio.Lock()


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
    await db.counters.update_one({"_id": "matches"}, {"$setOnInsert": {"next_id": 1}}, upsert=True)
    await db.matches.create_index([("match_id", ASCENDING)], unique=True)
    await db.matches.create_index([("bancho_channel", ASCENDING), ("status", ASCENDING)])
    await db.matches.create_index([("discord_channel_id", ASCENDING), ("status", ASCENDING)])
    await db.matches.create_index([("player_osu_ids", ASCENDING), ("status", ASCENDING)])
    await db.osu_accounts.create_index([("discord_user_id", ASCENDING)], unique=True)
    await db.osu_accounts.create_index([("osu_user_id", ASCENDING)], unique=True)
    await db.osu_link_challenges.create_index([("code_hash", ASCENDING)], unique=True)
    await db.osu_link_challenges.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    print("✅ MongoDB initialized")


async def _next_pool_id() -> int:
    counter = await _db().counters.find_one_and_update(
        {"_id": "pools"}, {"$inc": {"next_id": 1}}, return_document=True
    )
    if counter is None:
        raise RuntimeError("Could not allocate pool ID")
    return int(counter["next_id"] - 1)

async def _next_match_id() -> int:
    counter = await _db().counters.find_one_and_update(
        {"_id": "matches"}, {"$inc": {"next_id": 1}}, return_document=True
    )
    if counter is None:
        raise RuntimeError("Could not allocate match ID")
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


async def update_pool_status(
    pool_id: int, new_status: str, moderator_id: int = None,
    *, expected_status: str | None = None,
) -> Tuple[bool, str]:
    """Change a pool status, optionally only from one expected prior state."""
    try:
        updates: Dict[str, Any] = {"status": new_status}
        if new_status == "ranked":
            updates.update({"ranked_by": moderator_id, "ranked_at": _now()})
        query: Dict[str, Any] = {"pool_id": pool_id}
        if expected_status is not None:
            query["status"] = expected_status
        result = await _db().pools.update_one(query, {"$set": updates})
        if result.matched_count == 1:
            return True, ""
        return False, "Pool status has already changed or the pool was not found"
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
        return result.deleted_count == 1, "" if result.deleted_count else "Pool not found"
    except PyMongoError as error:
        return False, str(error)


async def update_pool_map(pool_id: int, slot: str, beatmap_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    slot = _slot(slot)
    result = await _db().pools.update_one(
        {"pool_id": pool_id, "maps.slot": slot},
        {"$set": {f"maps.$.{key}": value for key, value in _map_document(slot, beatmap_id, snapshot).items() if key != "slot"}},
    )
    return result.matched_count == 1, "" if result.matched_count else f"Slot `{slot}` not found"


async def add_pool_map(pool_id: int, slot: str, beatmap_id: int, snapshot: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    slot = _slot(slot)
    item = _map_document(slot, beatmap_id, snapshot)
    result = await _db().pools.update_one(
        {"pool_id": pool_id, "maps.slot": {"$ne": slot}}, {"$push": {"maps": item}}
    )
    return result.matched_count == 1, "" if result.matched_count else f"Slot `{slot}` already exists or the pool was not found"

async def get_match_by_bancho_channel(channel: str) -> Optional[Dict]:
    """Find the latest live match in a multiplayer chat, including its lobby phase."""
    return _pool(await _db().matches.find_one(
        {
            "bancho_channel": channel,
            "status": {"$in": ["waiting_players", "pickban", "waiting_ready", "checking_settings", "game_running"]},
        },
        sort=[("match_id", DESCENDING)],
    ))


async def get_active_match_by_bancho_channel(channel: str) -> Optional[Dict]:
    """Return the latest match currently waiting for a pick/ban action."""
    return _pool(await _db().matches.find_one(
        {"bancho_channel": channel, "status": "pickban"},
        sort=[("match_id", DESCENDING)],
    ))


async def get_live_matches() -> List[Dict]:
    """Return every match that still requires Bancho IRC management."""
    cursor = _db().matches.find(
        {
            "status": {
                "$in": [
                    "waiting_players", "pickban", "waiting_ready",
                    "checking_settings", "game_running",
                ],
            },
        },
        {"_id": 0},
    ).sort("match_id", ASCENDING)
    return [item async for item in cursor]

async def create_match(document: Dict[str, Any]) -> int:
    """Persist a Discord-managed test match and return its public ID."""
    match_id = await _next_match_id()
    payload = dict(document)
    payload.update({"match_id": match_id, "created_at": _now(), "updated_at": _now()})
    await _db().matches.insert_one(payload)
    return match_id


async def get_match(match_id: int) -> Optional[Dict]:
    return _pool(await _db().matches.find_one({"match_id": match_id}))


async def get_active_match_for_osu_users(osu_user_ids: List[int]) -> Optional[Dict]:
    """Return a live match containing any supplied stable osu! account ID."""
    ids = [int(user_id) for user_id in osu_user_ids]
    if not ids:
        return None
    return _pool(await _db().matches.find_one(
        {
            "player_osu_ids": {"$in": ids},
            "status": {
                "$in": [
                    "waiting_players", "pickban", "waiting_ready",
                    "checking_settings", "game_running",
                ],
            },
        },
        sort=[("match_id", DESCENDING)],
    ))


async def update_match(match_id: int, updates: Dict[str, Any]) -> bool:
    updates = dict(updates)
    updates["updated_at"] = _now()
    result = await _db().matches.update_one({"match_id": match_id}, {"$set": updates})
    return result.matched_count == 1


async def create_osu_login_challenge(
    discord_user_id: int, code_hash: str, expires_at: datetime
) -> bool:
    """Replace the user's pending osu! connection code."""
    try:
        await _db().osu_link_challenges.delete_many({"discord_user_id": discord_user_id})
        await _db().osu_link_challenges.insert_one({
            "discord_user_id": discord_user_id,
            "code_hash": code_hash,
            "expires_at": expires_at,
            "created_at": _now(),
        })
        return True
    except PyMongoError:
        return False


async def delete_osu_login_challenge(discord_user_id: int) -> None:
    try:
        await _db().osu_link_challenges.delete_many({"discord_user_id": discord_user_id})
    except PyMongoError:
        pass


async def get_osu_login_challenge(code_hash: str) -> Optional[Dict[str, Any]]:
    document = await _db().osu_link_challenges.find_one({
        "code_hash": code_hash,
        "expires_at": {"$gt": _now()},
    }, {"_id": 0})
    return dict(document) if document else None


async def get_osu_account_by_discord(discord_user_id: int) -> Optional[Dict[str, Any]]:
    document = await _db().osu_accounts.find_one({"discord_user_id": discord_user_id}, {"_id": 0})
    return dict(document) if document else None


async def get_osu_account_by_osu_id(osu_user_id: int) -> Optional[Dict[str, Any]]:
    document = await _db().osu_accounts.find_one({"osu_user_id": osu_user_id}, {"_id": 0})
    return dict(document) if document else None


async def update_osu_account_username(osu_user_id: int, osu_username: str) -> bool:
    """Refresh the cached username while keeping the stable osu! ID unchanged."""
    try:
        result = await _db().osu_accounts.update_one(
            {"osu_user_id": int(osu_user_id)},
            {"$set": {"osu_username": osu_username.strip(), "last_verified_at": _now()}},
        )
    except (PyMongoError, TypeError, ValueError):
        return False
    return result.matched_count > 0


async def complete_osu_login(
    code_hash: str, osu_user_id: int, osu_username: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Consume a code and atomically create the Discord <-> osu! link.

    The process-local lock prevents two IRC events from consuming the same
    challenge in this bot process. Unique MongoDB indexes protect the mapping
    if another worker ever attempts the same operation concurrently.
    """
    async with _osu_link_lock:
        challenge = await _db().osu_link_challenges.find_one({
            "code_hash": code_hash,
            "expires_at": {"$gt": _now()},
        })
        if not challenge:
            return "invalid_or_expired", None

        discord_user_id = int(challenge["discord_user_id"])
        existing_osu = await get_osu_account_by_osu_id(osu_user_id)
        if existing_osu and int(existing_osu["discord_user_id"]) != discord_user_id:
            return "osu_already_linked", existing_osu

        existing_discord = await get_osu_account_by_discord(discord_user_id)
        if existing_discord and int(existing_discord["osu_user_id"]) != osu_user_id:
            return "discord_already_linked", existing_discord

        account = {
            "discord_user_id": discord_user_id,
            "osu_user_id": int(osu_user_id),
            "osu_username": osu_username.strip(),
            "linked_at": existing_discord.get("linked_at", _now()) if existing_discord else _now(),
            "last_verified_at": _now(),
        }
        try:
            await _db().osu_accounts.update_one(
                {"discord_user_id": discord_user_id}, {"$set": account}, upsert=True
            )
            await _db().osu_link_challenges.delete_one({"_id": challenge["_id"]})
        except PyMongoError:
            return "storage_error", None
        return "linked", account


async def close_database() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None

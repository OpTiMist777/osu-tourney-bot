# osu_api.py
import aiohttp
import asyncio
from dotenv import load_dotenv
import os
from urllib.parse import quote

load_dotenv()

OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET")
OSU_API_URL = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"

class OsuClientManager:
    """Direct osu! API v2 client using aiohttp."""
    
    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0
        self._lock = asyncio.Lock()
    
    async def _get_token(self):
        """Obtain an OAuth2 token from the osu! API."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OSU_TOKEN_URL,
                json={
                    "client_id": OSU_CLIENT_ID,
                    "client_secret": OSU_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                    "scope": "public"
                },
                headers={"Accept": "application/json"}
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"❌ Could not obtain the osu! API token (HTTP {resp.status}): {error_text}")
                
                data = await resp.json()
                self.access_token = data["access_token"]
                self.token_expires_at = asyncio.get_event_loop().time() + data.get("expires_in", 86400) - 3600
                print("✅ osu! API token received")
    
    async def _ensure_token(self):
        """Ensure that the token is current."""
        current_time = asyncio.get_event_loop().time()
        if not self.access_token or current_time >= self.token_expires_at:
            async with self._lock:
                current_time = asyncio.get_event_loop().time()
                if not self.access_token or current_time >= self.token_expires_at:
                    await self._get_token()

    async def warm_up(self) -> None:
        """Preload an API token during startup without waiting for a map request."""
        await self._ensure_token()

    def seconds_until_refresh(self) -> float:
        """Seconds until the cached token reaches its early-refresh threshold."""
        return max(0.0, self.token_expires_at - asyncio.get_running_loop().time())
    
    async def get_beatmap(self, beatmap_id: int) -> dict:
        """Fetch full beatmap data by ID."""
        await self._ensure_token()
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{OSU_API_URL}/beatmaps/{beatmap_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json"
                }
            ) as resp:
                if resp.status == 404:
                    raise ValueError(f"❌ Beatmap {beatmap_id} was not found in osu!")
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"❌ osu! API error (HTTP {resp.status}): {error_text}")
                
                data = await resp.json()
                
                # Map osu! API modes to our internal format
                mode_map = {
                    "osu": "osu",
                    "taiko": "taiko",
                    "fruits": "ctb",
                    "mania": "mania"
                }
                
                # Detect converted beatmaps
                is_convert = False
                beatmapset = data.get("beatmapset", {})
                
                # Check 1: the convert field in the beatmapset
                if beatmapset.get("convert") is True:
                    is_convert = True
                # Check 2: compare modes
                elif "mode" in data and "mode" in beatmapset:
                    if data["mode"] != beatmapset["mode"]:
                        is_convert = True
                
                # Extract all beatmap attributes
                return {
                    "id": data["id"],
                    "set_id": data["beatmapset_id"],
                    "title": data["beatmapset"]["title"],
                    "artist": data["beatmapset"]["artist"],
                    "difficulty": data["version"],
                    "stars": round(data.get("difficulty_rating", 0.0), 2),
                    "mode": mode_map.get(data["mode"], data["mode"]),
                    "bpm": round(data.get("bpm", 0.0), 1),
                    "length": data.get("total_length", 0),
                    "cs": round(data.get("cs", 0.0), 1),      # Circle Size
                    "ar": round(data.get("ar", 0.0), 1),      # Approach Rate
                    # The v2 API exposes OD as `accuracy`; keep `od` as a
                    # fallback for compatibility with older responses.
                    "od": round(data.get("accuracy", data.get("od", 0.0)), 1),
                    "hp": round(data.get("drain", 0.0), 1),   # HP Drain
                    "url": f"https://osu.ppy.sh/b/{data['id']}",
                    "convert": is_convert,
                    "mods": []
                }

    async def get_user(self, username: str) -> dict:
        """Resolve an osu! username to its stable public account ID."""
        await self._ensure_token()
        raw_username = username.strip().strip("[]")
        if not raw_username:
            raise ValueError("osu! username is empty")
        candidates = list(dict.fromkeys((raw_username, raw_username.replace("_", " "))))

        async with aiohttp.ClientSession() as session:
            for candidate in candidates:
                encoded_username = quote(candidate, safe="")
                async with session.get(
                    f"{OSU_API_URL}/users/{encoded_username}",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Accept": "application/json",
                    },
                ) as resp:
                    if resp.status == 404:
                        continue
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"osu! API user lookup failed (HTTP {resp.status}): {error_text}")
                    data = await resp.json()
                    return {
                        "id": int(data["id"]),
                        "username": str(data.get("username", raw_username)),
                    }
        raise ValueError(f"osu! account `{raw_username}` was not found")

    async def get_user_by_id(self, user_id: int) -> dict:
        """Resolve the current public username for a stable osu! account ID."""
        await self._ensure_token()
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError) as error:
            raise ValueError("osu! user ID must be an integer") from error
        if numeric_id <= 0:
            raise ValueError("osu! user ID must be positive")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{OSU_API_URL}/users/{numeric_id}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status == 404:
                    raise ValueError(f"osu! account with ID `{numeric_id}` was not found")
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(
                        f"osu! API user lookup by ID failed (HTTP {resp.status}): {error_text}"
                    )
                data = await resp.json()
                return {
                    "id": int(data["id"]),
                    "username": str(data.get("username", numeric_id)),
                }

    async def get_beatmap_star_rating(
        self, beatmap_id: int, mods: list[str], *, ruleset: str = "osu",
    ) -> float:
        """Return the official star rating for a beatmap/mod/ruleset combination."""
        await self._ensure_token()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OSU_API_URL}/beatmaps/{beatmap_id}/attributes",
                json={"mods": mods, "ruleset": ruleset},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise RuntimeError(
                        f"Could not get modded difficulty (HTTP {resp.status}): {error_text}"
                    )

                attributes = (await resp.json()).get("attributes", {})
                star_rating = attributes.get("star_rating")
                if star_rating is None:
                    raise RuntimeError("osu! API did not return a star rating")
                return round(float(star_rating), 2)
    
# Global manager instance
osu_manager = OsuClientManager()

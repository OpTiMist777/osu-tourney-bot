# osu_api.py
import aiohttp
import asyncio
from dotenv import load_dotenv
import os

load_dotenv()

OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET")
OSU_API_URL = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"

class OsuClientManager:
    """Прямой клиент osu! API v2 через aiohttp"""
    
    def __init__(self):
        self.access_token = None
        self.token_expires_at = 0
        self._lock = asyncio.Lock()
    
    async def _get_token(self):
        """Получает OAuth2 токен от osu! API"""
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
                    raise Exception(f"❌ Не удалось получить токен osu! API (HTTP {resp.status}): {error_text}")
                
                data = await resp.json()
                self.access_token = data["access_token"]
                self.token_expires_at = asyncio.get_event_loop().time() + data.get("expires_in", 86400) - 3600
                print("✅ osu! API токен получен")
    
    async def _ensure_token(self):
        """Гарантирует, что токен актуален"""
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
        """Получает полные данные о битмапе по ID"""
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
                    raise ValueError(f"❌ Битмап {beatmap_id} не найден в osu! базе")
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"❌ osu! API ошибка (HTTP {resp.status}): {error_text}")
                
                data = await resp.json()
                
                # Маппинг режимов osu! API → наш формат
                mode_map = {
                    "osu": "osu",
                    "taiko": "taiko",
                    "fruits": "ctb",
                    "mania": "mania"
                }
                
                # ОПРЕДЕЛЕНИЕ КОНВЕРТА
                is_convert = False
                beatmapset = data.get("beatmapset", {})
                
                # Проверка 1: Поле convert в beatmapset
                if beatmapset.get("convert") is True:
                    is_convert = True
                # Проверка 2: Сравнение режимов
                elif "mode" in data and "mode" in beatmapset:
                    if data["mode"] != beatmapset["mode"]:
                        is_convert = True
                
                # Извлечение всех параметров карты
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

    async def get_beatmap_star_rating(self, beatmap_id: int, mods: list[str]) -> float:
        """Return the official star rating for a beatmap/mod combination."""
        await self._ensure_token()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OSU_API_URL}/beatmaps/{beatmap_id}/attributes",
                json={"mods": mods, "ruleset": "osu"},
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
    
    async def validate_beatmap_for_slot(self, beatmap_id: int, slot: str, mode: str) -> tuple[bool, str]:
        """Валидирует битмап для слота (конверты ВСЕГДА разрешены)"""
        try:
            beatmap_data = await self.get_beatmap(beatmap_id)
            
            mode_map = {
                'std': 'osu',
                'taiko': 'taiko',
                'ctb': 'ctb',
                'mania': 'mania'
            }
            
            expected_mode = mode_map.get(mode, 'osu')
            if beatmap_data['mode'] != expected_mode:
                return False, f"❌ Режим карты: `{beatmap_data['mode']}`, требуется: `{expected_mode}`"
            
            return True, f"✅ {beatmap_data['artist']} - {beatmap_data['title']} [{beatmap_data['difficulty']}] ({beatmap_data['stars']}★)"
        
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, str(e)

# Глобальный экземпляр менеджера
osu_manager = OsuClientManager()

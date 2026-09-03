"""Small asyncio client for osu!Bancho IRC used by in-game match flow."""
from __future__ import annotations
import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable

MessageHandler = Callable[[str, str, str], Awaitable[None]]


def parse_match_settings(lines: list[str]) -> dict:
    """Parse the multiline BanchoBot response to `!mp settings`.

    The response looks like the captured lobby log: one line for beatmap,
    team/win mode, active mods, player count, then one line per occupied slot.
    """
    settings: dict = {"players": [], "player_count": 0}
    for line in lines:
        text = line.strip()
        if text.startswith("Beatmap:"):
            match = re.search(r"/b/(\d+)", text)
            if match:
                settings["beatmap_id"] = int(match.group(1))
        elif text.startswith("Team mode:"):
            match = re.match(r"Team mode:\s*([^,]+),\s*Win condition:\s*(.+)$", text, re.I)
            if match:
                settings["team_mode"] = match.group(1).strip()
                settings["win_condition"] = match.group(2).strip()
        elif text.startswith("Active mods:"):
            settings["active_mods"] = text.split(":", 1)[1].strip().casefold()
        elif text.startswith("Players:"):
            match = re.search(r"(\d+)", text)
            if match:
                settings["player_count"] = int(match.group(1))
        else:
            slot = re.match(r"Slot\s+(\d+)\s+(.+?)\s+https?://osu\.ppy\.sh/u/(\d+)\s+(.+?)\s+\[Team\s+([^/\]]+)\s*/\s*(.*?)\]$", text, re.I)
            if slot:
                settings["players"].append({
                    "slot": int(slot.group(1)),
                    "status": slot.group(2).strip(),
                    "user_id": int(slot.group(3)),
                    "username": slot.group(4).strip(),
                    "team": slot.group(5).strip(),
                    "mods": [item.strip().casefold() for item in slot.group(6).split(",") if item.strip()],
                })
    return settings
JoinHandler = Callable[[str, str], Awaitable[None]]
logger = logging.getLogger("osu_tourney.bancho")

class BanchoIRC:
    HOST, PORT = "irc.ppy.sh", 6667
    # BanchoBot currently announces rooms as /mp/<id>; older replies may use
    # /community/matches/<id>, so accept both formats.
    MATCH_URL = re.compile(
        r"https?://osu\.ppy\.sh/(?:mp|community/matches)/(\d+)", re.I
    )
    def __init__(self) -> None:
        self.username = os.getenv("BANCHO_USERNAME", "").strip()
        self.password = os.getenv("BANCHO_IRC_PASSWORD", "").strip()
        self.reader = self.writer = None
        self._connected = asyncio.Event()
        self._match_waiter = None
        self._settings_waiters: dict[str, asyncio.Future[None]] = {}
        self._settings_buffers: dict[str, list[str]] = {}
        self.message_handler: MessageHandler | None = None
        self.join_handler: JoinHandler | None = None
        self._reader_task = None
    @property
    def configured(self) -> bool: return bool(self.username and self.password)
    async def _send(self, line: str) -> None:
        if not self.writer: raise RuntimeError("Bancho IRC не подключён")
        self.writer.write((line + "\r\n").encode()); await self.writer.drain()
    async def connect(self) -> None:
        if self.writer and not self.writer.is_closing(): return
        if not self.configured: raise RuntimeError("BANCHO_USERNAME или BANCHO_IRC_PASSWORD не заданы в .env")
        logger.info("Подключение к Bancho IRC как %s", self.username)
        self.reader, self.writer = await asyncio.open_connection(self.HOST, self.PORT)
        nick = self.username.replace(" ", "_")
        await self._send(f"PASS {self.password}"); await self._send(f"NICK {nick}"); await self._send(f"USER {nick} 0 * :{nick}")
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._connected.wait(), 15)
            logger.info("Bancho IRC: вход подтверждён")
        except TimeoutError as error: raise RuntimeError("Bancho IRC не подтвердил вход за 15 секунд") from error
    async def close(self) -> None:
        if self.writer and not self.writer.is_closing():
            self.writer.close(); await self.writer.wait_closed()
        self.writer = self.reader = None
        if self._reader_task: self._reader_task.cancel()
        logger.info("Bancho IRC: соединение закрыто")
    async def send_channel(self, channel: str, text: str) -> None:
        await self._send(f"PRIVMSG {channel} :{text}")
        logger.info("IRC → %s: %s", channel, text)

    async def verify_room(self, channel: str) -> None:
        """Confirm that BanchoBot answers from the MP channel before setup."""
        waiter = asyncio.get_running_loop().create_future()
        self._settings_waiters[channel.casefold()] = waiter
        await self.send_channel(channel, "!mp settings")
        try:
            await asyncio.wait_for(waiter, 10)
            logger.info("BanchoBot подтвердил доступ к настройкам %s", channel)
        except TimeoutError as error:
            self._settings_waiters.pop(channel.casefold(), None)
            logger.error("Нет ответа BanchoBot на !mp settings в %s", channel)
            raise RuntimeError("BanchoBot не ответил на !mp settings в созданной комнате") from error

    async def get_match_settings(self, channel: str, wait_seconds: float = 2.5) -> dict:
        """Request and collect the multiline BanchoBot ``!mp settings`` reply."""
        key = channel.casefold()
        self._settings_buffers[key] = []
        await self.send_channel(channel, "!mp settings")
        await asyncio.sleep(wait_seconds)
        lines = self._settings_buffers.pop(key, [])
        return parse_match_settings(lines)
    async def make_match(self, name: str) -> str:
        await self.connect()
        logger.info("Запрос создания Bancho-комнаты: %s", name)
        self._match_waiter = asyncio.get_running_loop().create_future()
        await self.send_channel("BanchoBot", f"!mp make {name}")
        try: match_id = await asyncio.wait_for(self._match_waiter, 20)
        except TimeoutError as error: raise RuntimeError("BanchoBot не подтвердил создание комнаты") from error
        channel = f"#mp_{match_id}"
        logger.info("BanchoBot создал комнату %s", channel)
        await self._send(f"JOIN {channel}")
        logger.info("IRC → JOIN %s", channel)
        # `/join #mp_<id>` has no guaranteed standalone IRC acknowledgement
        # from Bancho.  TCP preserves command order, so verify the join by the
        # subsequent BanchoBot response in the room instead of timing out on a
        # non-portable JOIN event.
        await asyncio.sleep(1)
        await self.verify_room(channel)
        await self.send_channel(channel, "!mp set 0 3 3")
        logger.info("Комната %s настроена командой !mp set 0 3 3", channel)
        return channel
    async def _read_loop(self) -> None:
        try:
            while line := await self.reader.readline():
                raw = line.decode("utf-8", "replace").rstrip()
                if raw.startswith("PING "):
                    await self._send("PONG " + raw[5:])
                    continue
                if " 001 " in raw:
                    self._connected.set()
                    # Bancho auto-joins #osu for IRC clients.  This bot never
                    # uses that global channel and must not receive its noisy
                    # join/quit stream.
                    await self._send("PART #osu")
                    logger.info("IRC → PART #osu (общий канал не используется)")
                    continue
                # Only room/BanchoBot traffic is useful to operators.  Do not
                # print unrelated global-channel join, quit, and timeout noise.
                if "#mp_" in raw.casefold() or "banchobot" in raw.casefold():
                    logger.info("IRC ← %s", raw)
                joined = re.match(r"^:([^ !]+)(?:!.*)? JOIN :?(#[^ ]+)$", raw, re.I)
                if joined and joined.group(2).casefold().startswith("#mp_") and self.join_handler:
                    await self.join_handler(joined.group(1), joined.group(2))
                    continue
                message = re.match(r"^:([^!]+)!.* PRIVMSG ([^ ]+) :(.*)$", raw)
                if not message: continue
                sender, target, text = message.groups()
                url = self.MATCH_URL.search(text)
                if sender.casefold() == "banchobot" and url and self._match_waiter and not self._match_waiter.done(): self._match_waiter.set_result(url.group(1))
                if sender.casefold() == "banchobot":
                    waiter = self._settings_waiters.pop(target.casefold(), None)
                    if waiter and not waiter.done(): waiter.set_result(None)
                    buffer = self._settings_buffers.get(target.casefold())
                    if buffer is not None:
                        buffer.append(text)
                if target.startswith("#") and self.message_handler: await self.message_handler(sender, target, text)
        finally: self._connected.clear()

"""Small asyncio client for osu!Bancho IRC used by in-game match flow."""
from __future__ import annotations
import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable

MessageHandler = Callable[[str, str, str], Awaitable[None]]
PrivateMessageHandler = Callable[[str, str], Awaitable[None]]


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
ReconnectHandler = Callable[[str], Awaitable[None]]
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
        self.private_message_handler: PrivateMessageHandler | None = None
        self.join_handler: JoinHandler | None = None
        self.reconnect_handler: ReconnectHandler | None = None
        self._reader_task = None
        self._reconnect_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._channels: dict[str, str] = {}
        self._keep_connected = False
        self._closing = False
    @property
    def configured(self) -> bool: return bool(self.username and self.password)
    async def _send(self, line: str) -> None:
        writer = self.writer
        if not writer or writer.is_closing() or not self._connected.is_set():
            logger.warning("Bancho IRC: команда не отправлена, соединение неактивно: %s", line.split(' ', 1)[0])
            self._schedule_reconnect()
            raise RuntimeError("Bancho IRC не подключён")
        try:
            writer.write((line + "\r\n").encode())
            await writer.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as error:
            logger.warning("Bancho IRC: ошибка отправки, соединение потеряно: %s", error)
            self._handle_connection_lost(writer)
            raise RuntimeError("Соединение с Bancho IRC потеряно") from error
    async def connect(self) -> None:
        if self.writer and not self.writer.is_closing() and self._connected.is_set(): return
        if not self.configured:
            logger.error("Bancho IRC: подключение невозможно, учётные данные не настроены")
            raise RuntimeError("BANCHO_USERNAME или BANCHO_IRC_PASSWORD не заданы в .env")
        async with self._connect_lock:
            if self.writer and not self.writer.is_closing() and self._connected.is_set(): return
            self._closing = False
            self._connected.clear()
            logger.info("Подключение к Bancho IRC как %s", self.username)
            try:
                reader, writer = await asyncio.open_connection(self.HOST, self.PORT)
            except (OSError, ConnectionError, TimeoutError) as error:
                logger.error("Bancho IRC: TCP-подключение к %s:%s не удалось: %s", self.HOST, self.PORT, error)
                raise RuntimeError("Не удалось подключиться к Bancho IRC") from error
            self.reader, self.writer = reader, writer
            logger.info("Bancho IRC: TCP-соединение установлено, отправляю IRC-аутентификацию")
            nick = self.username.replace(" ", "_")
            try:
                writer.write((f"PASS {self.password}\r\nNICK {nick}\r\nUSER {nick} 0 * :{nick}\r\n").encode())
                await writer.drain()
            except (ConnectionError, OSError) as error:
                logger.error("Bancho IRC: не удалось отправить IRC-аутентификацию: %s", error)
                writer.close()
                await writer.wait_closed()
                if self.writer is writer:
                    self.reader = self.writer = None
                raise RuntimeError("Не удалось отправить данные входа в Bancho IRC") from error
            self._reader_task = asyncio.create_task(self._read_loop(reader, writer))
            try:
                await asyncio.wait_for(self._connected.wait(), 15)
                logger.info("Bancho IRC: вход подтверждён")
            except TimeoutError as error:
                logger.error("Bancho IRC: сервер не подтвердил вход за 15 секунд")
                self._handle_connection_lost(writer)
                raise RuntimeError("Bancho IRC не подтвердил вход за 15 секунд") from error
    async def close(self) -> None:
        self._closing = True
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if reconnect_task and reconnect_task is not asyncio.current_task() and not reconnect_task.done():
            reconnect_task.cancel()
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task and reader_task is not asyncio.current_task() and not reader_task.done():
            reader_task.cancel()
        writer = self.writer
        self.reader = self.writer = None
        self._connected.clear()
        self._channels.clear()
        self._keep_connected = False
        if writer and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        logger.info("Bancho IRC: соединение закрыто")

    def forget_channel(self, channel: str) -> None:
        """Stop rejoining a room that has been closed or cancelled."""
        removed = self._channels.pop(channel.casefold(), None)
        if removed:
            logger.info("Bancho IRC: MP-комната исключена из reconnect: %s", removed)

    def set_keep_connected(self, enabled: bool) -> None:
        """Keep the IRC session alive even when no MP room is active."""
        self._keep_connected = enabled
        logger.info("Bancho IRC: постоянное соединение %s", "включено" if enabled else "выключено")

    def _handle_connection_lost(self, writer) -> None:
        is_current = self.writer is writer
        if is_current:
            self.reader = self.writer = None
            self._connected.clear()
        if writer and not writer.is_closing():
            writer.close()
        if is_current:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closing or not (self._channels or self._keep_connected):
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        logger.info(
            "Bancho IRC: планирую reconnect (комнат: %s, постоянное соединение: %s)",
            len(self._channels),
            "да" if self._keep_connected else "нет",
        )
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 2
        try:
            while not self._closing and (self._channels or self._keep_connected):
                try:
                    logger.info("Bancho IRC: попытка reconnect")
                    await self.connect()
                    channels = list(self._channels.values())
                    for channel in channels:
                        await self._send(f"JOIN {channel}")
                        logger.info("IRC → повторный JOIN %s", channel)
                    await asyncio.sleep(0.5)
                    if self.reconnect_handler:
                        for channel in channels:
                            try:
                                await self.reconnect_handler(channel)
                            except Exception:
                                logger.exception("Bancho IRC: не удалось восстановить состояние %s", channel)
                    if not self.writer or self.writer.is_closing() or not self._connected.is_set():
                        raise RuntimeError("соединение снова потеряно во время восстановления")
                    logger.info("Bancho IRC: соединение восстановлено, комнат: %s", len(channels))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning("Bancho IRC: reconnect не удался (%s), следующая попытка через %s с", error, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            self._reconnect_task = None
            # A connection can disappear in the tiny window between the
            # health check above and this task's return. Do not leave the
            # active MP room without another reconnect attempt in that case.
            if (
                not self._closing
                and (self._channels or self._keep_connected)
                and (not self.writer or self.writer.is_closing() or not self._connected.is_set())
            ):
                self._schedule_reconnect()
    async def send_channel(self, channel: str, text: str) -> None:
        if channel.casefold().startswith("#mp_"):
            self._channels.setdefault(channel.casefold(), channel)
        await self.connect()
        await self._send(f"PRIVMSG {channel} :{text}")
        logger.info("IRC → %s: %s", channel, text)

    async def send_private_message(self, username: str, text: str) -> None:
        """Send a direct osu! chat message without joining any channel."""
        await self.connect()
        await self._send(f"PRIVMSG {username} :{text}")
        logger.info("IRC → PM %s: сообщение отправлено", username)

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
        logger.info("Bancho IRC: запрашиваю !mp settings в %s", channel)
        await self.send_channel(channel, "!mp settings")
        await asyncio.sleep(wait_seconds)
        lines = self._settings_buffers.pop(key, [])
        if not lines:
            logger.warning("Bancho IRC: !mp settings в %s не вернула ни одной строки", channel)
        settings = parse_match_settings(lines)
        logger.info(
            "Bancho IRC: !mp settings в %s собраны (%s строк, карта=%s, игроков=%s)",
            channel,
            len(lines),
            settings.get("beatmap_id", "?"),
            settings.get("player_count", 0),
        )
        return settings
    async def make_match(self, name: str) -> str:
        await self.connect()
        logger.info("Запрос создания Bancho-комнаты: %s", name)
        self._match_waiter = asyncio.get_running_loop().create_future()
        await self.send_channel("BanchoBot", f"!mp make {name}")
        try:
            match_id = await asyncio.wait_for(self._match_waiter, 20)
        except TimeoutError as error:
            logger.error("Bancho IRC: BanchoBot не подтвердил создание комнаты за 20 секунд")
            raise RuntimeError("BanchoBot не подтвердил создание комнаты") from error
        channel = f"#mp_{match_id}"
        self._channels[channel.casefold()] = channel
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
    async def _read_loop(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                raw = line.decode("utf-8", "replace").rstrip()
                if raw.startswith("PING "):
                    try:
                        writer.write(("PONG " + raw[5:] + "\r\n").encode())
                        await writer.drain()
                        logger.debug("IRC ← PING, → PONG")
                    except (ConnectionError, OSError):
                        break
                    continue
                if " 001 " in raw:
                    self._connected.set()
                    logger.info("Bancho IRC: получено IRC welcome-сообщение (001)")
                    # Bancho auto-joins #osu for IRC clients.  This bot never
                    # uses that global channel and must not receive its noisy
                    # join/quit stream.
                    try:
                        writer.write(b"PART #osu\r\n")
                        await writer.drain()
                    except (ConnectionError, OSError):
                        break
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
                if sender.casefold() == "banchobot" and url:
                    logger.info("BanchoBot подтвердил создание MP-комнаты: #%s", url.group(1))
                if sender.casefold() == "banchobot":
                    waiter = self._settings_waiters.pop(target.casefold(), None)
                    if waiter and not waiter.done():
                        waiter.set_result(None)
                        logger.info("BanchoBot ответил на проверку !mp settings в %s", target)
                    buffer = self._settings_buffers.get(target.casefold())
                    if buffer is not None:
                        buffer.append(text)
                if target.startswith("#") and self.message_handler: await self.message_handler(sender, target, text)
                elif (
                    target.casefold() == self.username.replace(" ", "_").casefold()
                    and sender.casefold() != "banchobot"
                    and self.private_message_handler
                ):
                    logger.info("IRC ← PM от %s: сообщение получено", sender)
                    await self.private_message_handler(sender, text)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as error:
            logger.warning("Bancho IRC: соединение разорвано: %s", error)
        except Exception:
            logger.exception("Bancho IRC: ошибка обработки входящего IRC-сообщения")
        else:
            logger.warning("Bancho IRC: сервер закрыл соединение (EOF)")
        finally:
            if self.writer is writer:
                self.reader = self.writer = None
                self._connected.clear()
                if not self._closing:
                    logger.warning("Bancho IRC: потеряно соединение, запускаю reconnect")
                    self._schedule_reconnect()

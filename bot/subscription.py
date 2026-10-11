"""Проверка обязательной подписки на каналы и запуска верификационного бота."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.contacts import GetRequestedEntityRequest  # type: ignore[attr-defined]
from telethon.tl.types import Channel, ChatInvite, InputPeerChannel, PeerChannel

log = logging.getLogger("bot.sub")


@dataclass(slots=True)
class Missing:
    """Список того, что пользователь ещё не выполнил."""

    channels: list[str]
    need_bot_start: str | None = None
    bot_token_url: str | None = None  # deep-link для запуска верификационного бота

    @property
    def ok(self) -> bool:
        return not self.channels and not self.need_bot_start


class SubscriptionGate:
    def __init__(
        self,
        client: TelegramClient,
        channels: list[str],
        required_bot: str = "",
        required_bot_token: str = "",
    ) -> None:
        self.client = client
        self.channels = channels
        self.required_bot = required_bot
        # Токен OTHER-бота (нужен только для проверки факта /start).
        self.required_bot_token = required_bot_token.strip()

    async def _resolve_channel(self, ref: str):
        """ref — username или числовой ID (в т.ч. формат -100xxxxxxxxxx)."""
        if ref.lstrip("-").isdigit():
            cid = int(ref)
            if cid <= -1000000000000:  # -100xxxxxxxxxx -> внутренний channel id
                cid = -(cid + 1000000000000)
            try:
                return await self.client.get_entity(PeerChannel(int(cid)))
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось разрешить канал по ID %s: %s", ref, exc)
                return None
        for attempt in (ref, f"t.me/{ref}"):
            try:
                return await self.client.get_entity(attempt)
            except Exception:  # noqa: BLE001
                continue
        try:  # последний шанс — поиск по invite-ссылке
            res = await self.client(
                GetRequestedEntityRequest(ChatInvite(link=f"https://t.me/{ref}"), join=False)
            )
            return res.entity
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось разрешить канал %s: %s", ref, exc)
            return None

    async def check_channels(self, user_id: int) -> list[str]:
        """Возвращает список username'ов каналов, на которые НЕ подписан пользователь."""
        missing: list[str] = []
        me = await self.client.get_me()
        for ref in self.channels:
            entity = await self._resolve_channel(ref)
            if entity is None:
                # Не смогли проверить — не блокируем пользователя.
                log.warning("Канал %s недоступен для проверки, пропускаем.", ref)
                continue
            if isinstance(entity, Channel) and entity.id == me.id:
                continue  # собственный аккаунт/канал бота
            peer = entity
            if isinstance(entity, Channel):
                peer = InputPeerChannel(entity.id, entity.access_hash or 0)
            try:
                await self.client(GetParticipantRequest(channel=entity, participant=peer))
            except (UserNotParticipantError, ChannelPrivateError):
                missing.append(getattr(entity, "username", None) or ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("Ошибка проверки %s для %s: %s", ref, user_id, exc)
        return missing

    async def channel_title(self, ref: str) -> str:
        entity = await self._resolve_channel(ref)
        return getattr(entity, "title", None) or getattr(entity, "username", ref)

    # ------------------------------------------------------------------ bot start
    async def check_bot_start(self, user_id: int) -> bool:
        """Проверяет, запускал ли пользователь REQUIRED_BOT_USERNAME (/start).

        Требует токен того бота в REQUIRED_BOT_TOKEN (Bot API getChatMember),
        т.к. узнать чужие диалоги с ботом через MTProto невозможно.
        Если токен не задан — проверка пропускается (возвращаем True).
        """
        if not self.required_bot:
            return True
        if not self.required_bot_token:
            log.warning(
                "REQUIRED_BOT_USERNAME=%s задан, но REQUIRED_BOT_TOKEN пуст — "
                "проверку запуска бота выполнить нельзя, пропускаем.",
                self.required_bot,
            )
            return True
        try:
            import aiohttp

            # getChat от имени бота успешен только если пользователь уже
            # взаимодействовал с этим ботом (у него открыт чат/диалог).
            url = f"https://api.telegram.org/bot{self.required_bot_token}/getChat"
            async with aiohttp.ClientSession() as s, s.post(
                url,
                json={"chat_id": user_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json(content_type=None)
            if data.get("ok"):
                return True
            log.debug("bot-start check: %s", data)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка проверки запуска бота %s: %s", self.required_bot, exc)
            return True

    # ------------------------------------------------------------------ combined
    async def check(self, user_id: int) -> Missing:
        missing_channels = await self.check_channels(user_id)
        need_bot = None
        deep_link = None
        if self.required_bot and not await self.check_bot_start(user_id):
            need_bot = self.required_bot
            deep_link = f"https://t.me/{self.required_bot}?start=verify_{user_id}"
        return Missing(channels=missing_channels, need_bot_start=need_bot, bot_token_url=deep_link)


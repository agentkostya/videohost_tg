"""Telegram-бот: доступ по подписке, выбор качества, скачивание и отдача видео."""
from __future__ import annotations

import html
import logging
import time

from telethon import Button, TelegramClient, events
from telethon.tl.types import MessageMediaWebPage, WebPage
from telethon.utils import get_peer_id

from . import files, media
from .config import Config
from .pipeline import Pipeline
from .storage import Job, Storage
from .subscription import SubscriptionGate

log = logging.getLogger("bot.handlers")

HELP = (
    "🎬 <b>Видео-бот</b>\n\n"
    "1. Отправь <b>ссылку</b> на видео (YouTube, VK, Rutube, OK, Twitch, прямой mp4…).\n"
    "2. Выбери <b>качество</b> или <b>только звук</b>.\n"
    "3. Получи файл. Если ролик больше лимита Telegram — он будет разрезан на части.\n\n"
    "Команды:\n"
    "/start — проверка доступа / меню\n"
    "/check — я подписался (повторная проверка)\n"
    "/jobs — активные задания\n"
    "/cancel &lt;id&gt; — отменить задание\n"
    "/limits — текущие лимиты аккаунта\n"
)


class _CheckProxy:
    """Адаптер: позволяет handle_check работать как с событиями сообщений,
    так и с нажатиями inline-кнопок (callback)."""

    def __init__(self, ev, message) -> None:
        self.ev = ev
        self.message = message

    @property
    def sender_id(self) -> int:
        return self.ev.sender_id or self.message.sender_id

    async def reply(self, text, buttons=None):
        try:
            await self.ev.edit_message(text, buttons=buttons)
        except Exception:  # noqa: BLE001
            return await self.ev.respond(text, buttons=buttons)
        return None


class VideoBot:
    def __init__(self, client: TelegramClient, cfg: Config, storage: Storage, gate: SubscriptionGate) -> None:
        self.client = client
        self.cfg = cfg
        self.storage = storage
        self.gate = gate
        self.pipeline = Pipeline(client, cfg, storage)
        self._last_check: dict[int, float] = {}
        self._fmt_cache: dict[str, list[media.FormatOption]] = {}

    # --------------------------------------------------------------- wiring
    def register(self) -> None:

        @self.client.on(events.NewMessage(pattern=r"^/start(?:\s+.*)?$", func=lambda e: e.is_private))
        async def cmd_start(ev):
            await self.handle_start(ev)

        @self.client.on(events.NewMessage(pattern=r"^/help(?:\s+.*)?$", func=lambda e: e.is_private))
        async def cmd_help(ev):
            await ev.reply(HELP)

        @self.client.on(events.NewMessage(pattern=r"^/check(?:\s+.*)?$", func=lambda e: e.is_private))
        async def cmd_check(ev):
            await self.handle_check(ev, force=True)

        @self.client.on(events.NewMessage(pattern=r"^/jobs(?:\s+.*)?$", func=lambda e: e.is_private))
        async def cmd_jobs(ev):
            await self.handle_jobs(ev)

        @self.client.on(events.NewMessage(pattern=r"^/cancel(?:\s+\S+)?$", func=lambda e: e.is_private))
        async def cmd_cancel(ev):
            await self.handle_cancel(ev)

        @self.client.on(events.NewMessage(pattern=r"^/limits(?:\s+.*)?$", func=lambda e: e.is_private))
        async def cmd_limits(ev):
            lim = await self.pipeline.limits()
            await ev.reply(
                f"Лимит одного файла: <b>{files.human(lim.hard_limit)}</b>\n"
                f"Размер части при нарезке: <b>{files.human(lim.chunk_target)}</b>\n"
                f"Premium аккаунт: <b>{'да' if lim.is_premium else 'нет'}</b>\n"
                f"ffmpeg: <b>{'найден' if files.has_ffmpeg() else 'НЕ найден (нарезка недоступна)'}</b>"
            )

        @self.client.on(events.CallbackQuery)
        async def callback(ev):
            await self.handle_callback(ev)

        @self.client.on(events.NewMessage(func=lambda e: e.is_private and not e.text))
        async def non_text(ev):
            url = self.extract_url_from_message(ev.message)
            if url:
                await self.handle_link(ev, url)
            else:
                await ev.reply("Пришли ссылку на видео 🙂  /help")

        @self.client.on(events.NewMessage(func=lambda e: e.is_private and bool(e.text)))
        async def text_msg(ev):
            text = ev.raw_text or ""
            if text.startswith("/"):
                return  # команды обработаны выше
            url = media.find_url(text)
            if url:
                await self.handle_link(ev, url)
            else:
                await ev.reply(HELP)

    # --------------------------------------------------------------- start / access
    async def handle_start(self, ev):
        user_id = ev.sender_id
        self.storage.touch_user(user_id)
        payload = (ev.pattern_match.group(0) or "").split()
        # /start verify_<uid> — приходит из верификационного бота (deeplink)
        if len(payload) > 1 and payload[1].startswith("verify"):
            self.storage.grant_access(user_id)
        if self.storage.has_access(user_id):
            await ev.reply(HELP)
            return
        await self.handle_check(ev, force=True)

    async def handle_check(self, ev, force: bool = False):
        user_id = ev.sender_id
        now = time.time()
        if not force and now - self._last_check.get(user_id, 0) < 5:
            await ev.reply("Проверяем чуть позже, подожди пару секунд…")
            return
        self._last_check[user_id] = now
        missing = await self.gate.check(user_id)
        if missing.ok:
            self.storage.grant_access(user_id)
            await ev.reply("✅ Доступ разрешён!\n\n" + HELP)
            return

        lines = ["⛔️ Чтобы пользоваться ботом, выполни условия:"]
        buttons = []
        for ref in missing.channels:
            title = await self.gate.channel_title(ref)
            lines.append(f"• подпишись на <b>@{html.escape(ref)}</b> ({html.escape(str(title))})")
            buttons.append(Button.url(f"Подписаться на @{ref}", f"https://t.me/{ref}"))
        if missing.need_bot_start:
            lines.append(f"• нажми /start у бота <b>@{html.escape(missing.need_bot_start)}</b>")
            buttons.append(
                Button.url(
                    f"Открыть @{missing.need_bot_start}",
                    missing.bot_token_url or f"https://t.me/{missing.need_bot_start}",
                )
            )
        lines.append("\nПосле выполнения нажми «Проверить подписку».")
        buttons.append([Button.inline("🔄 Проверить подписку", b"recheck")])
        await ev.reply("\n".join(lines), buttons=buttons)

    # --------------------------------------------------------------- links
    @staticmethod
    def extract_url_from_message(message) -> str | None:
        """Достаёт ссылку из сообщения (текст или превью веб-страницы)."""
        media_obj = getattr(message, "media", None)
        if isinstance(media_obj, MessageMediaWebPage):
            page = getattr(media_obj, "webpage", None)
            url = getattr(page, "url", None)
            if url:
                return url
        return media.find_url(getattr(message, "text", "") or "")

    async def handle_link(self, ev, url: str):
        user_id = ev.sender_id
        if not self.storage.has_access(user_id):
            await self.handle_check(ev, force=False)
            return
        msg = await ev.reply(f"🔎 Ищу варианты качества для\n<code>{html.escape(url[:120])}</code> …")
        info = await media.probe(url, self.cfg.cookies_file, self.cfg.cookies_from_browser)
        if info.error or not info.formats:
            await msg.edit(
                "❌ Не удалось получить данные по ссылке.\n"
                f"<code>{html.escape(info.error or 'нет доступных форматов')}</code>\n\n"
                "Для приватных/региональных роликов нужны cookies (COOKIES_FILE)."
            )
            return

        job = self.storage.create_job(user_id, get_peer_id(ev.chat_id), url)
        job.title = info.title
        job.duration = info.duration
        self.storage.save_job(job)
        self._fmt_cache[job.id] = info.formats

        rows: list[list] = []
        per_row: list = []
        for i, f in enumerate(info.formats[:24]):
            size = f"{f.approx_mb:.0f} MiB" if f.approx_mb else "?"
            label = f"{f.label} · {size}"
            per_row.append(Button.inline(label[:60], f"fmt:{job.id}:{i}".encode()))
            if len(per_row) == 2:
                rows.append(per_row)
                per_row = []
        if per_row:
            rows.append(per_row)
        rows.append([Button.inline("❌ Отмена", f"cancel:{job.id}".encode())])

        await msg.edit(
            f"🎞 <b>{html.escape(info.title[:120])}</b>\n"
            f"⏱ длительность ~{info.duration // 60}:{info.duration % 60:02d}\n"
            f"🆔 задание <code>{job.id}</code>\n\n"
            "Выбери качество (размер указан примерно):",
            buttons=rows,
        )

    # --------------------------------------------------------------- callbacks
    async def handle_callback(self, ev):
        data = (ev.data.decode() if isinstance(ev.data, bytes) else str(ev.data)) or ""
        user_id = ev.sender_id
        parts = data.split(":")
        kind = parts[0]

        if kind == "recheck":
            await ev.answer()
            message = await ev.get_message()
            fake = _CheckProxy(ev, message)
            await self.handle_check(fake, force=True)
            return
        if kind == "cancel":
            job_id = parts[1]
            ok = self.pipeline.cancel(job_id)
            job = self.storage.get_job(job_id)
            if job:
                job.status = "canceled"
                self.storage.save_job(job)
            await ev.answer("Отменено" if ok else "Задание уже не активно")
            try:
                await ev.edit_message("Задание отменено.")
            except Exception:  # noqa: BLE001
                pass
            return
        if kind == "fmt":
            job_id, idx = parts[1], int(parts[2])
            fmts = self._fmt_cache.get(job_id) or []
            job = self.storage.get_job(job_id)
            if not job or not fmts or idx >= len(fmts):
                await ev.answer("Задание устарело — отправь ссылку заново", alert=True)
                return
            if job.user_id != user_id:
                await ev.answer("Это не твоё задание", alert=True)
                return
            chosen = fmts[idx]
            job.fmt_id = chosen.fid
            job.status = "downloading"
            self.storage.save_job(job)
            await ev.answer(f"Выбрано: {chosen.label}")
            try:
                await ev.edit_message(
                    f"🎬 <b>{html.escape(job.title[:120])}</b>\n"
                    f"Качество: <b>{html.escape(chosen.label)}</b> (~{chosen.approx_mb:.0f} MiB)\n"
                    "Начинаю…"
                )
            except Exception:  # noqa: BLE001
                pass
            await self.pipeline.start(job)
            return
        await ev.answer()

    # --------------------------------------------------------------- jobs / cancel
    async def handle_jobs(self, ev):
        jobs = self.storage.active_jobs_for_user(ev.sender_id)
        if not jobs:
            await ev.reply("Активных заданий нет.")
            return
        lines = ["<b>Задания:</b>"]
        status_map = {
            "await_choice": "⏳ ждёт выбора качества",
            "downloading": "⬇️ скачивается",
            "uploading": "⤴️ отправляется",
            "splitting": "✂️ режется",
            "done": "✅ готово",
            "error": "❌ ошибка",
            "canceled": "🚫 отменено",
        }
        buttons = []
        for j in jobs:
            pct = int(max(j.progress, j.phase_progress) * 100)
            lines.append(
                f"<code>{j.id}</code> · {html.escape(j.title[:50])} · "
                f"{status_map.get(j.status, j.status)} {pct}%"
            )
            if j.status in ("downloading", "uploading", "splitting"):
                buttons.append([Button.inline(f"Отменить {j.id}", f"cancel:{j.id}".encode())])
        await ev.reply("\n".join(lines), buttons=buttons or None)

    async def handle_cancel(self, ev):
        args = (ev.pattern_match.group(0) or "").split()
        jobs = self.storage.active_jobs_for_user(ev.sender_id)
        target = None
        if len(args) > 1:
            target = next((j for j in jobs if j.id == args[1]), None)
            if target is None:
                target = self.storage.get_job(args[1])
        elif jobs:
            target = jobs[0]
        if target is None:
            await ev.reply("Нечего отменять. Использование: /cancel &lt;id&gt;")
            return
        self.pipeline.cancel(target.id)
        target.status = "canceled"
        self.storage.save_job(target)
        await ev.reply(f"Задание <code>{target.id}</code> отменено.")

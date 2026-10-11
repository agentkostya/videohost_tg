"""Оркестрация: выбор качества -> скачивание -> (нарезка) -> загрузка в Telegram."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import (
    FileIdInvalidError,
    FilePartTooBigError,
    FileReferenceExpiredError,
    TimedOutError,
)
from telethon.tl.types import DocumentAttributeAudio

from . import files, media
from .config import Config
from .storage import Job, Storage

log = logging.getLogger("bot.pipeline")

MIB = 1024 * 1024


@dataclass(slots=True)
class Task:
    job_id: str
    cancel: threading.Event
    future: "asyncio.Future | None" = None


class Pipeline:
    def __init__(self, client: TelegramClient, cfg: Config, storage: Storage) -> None:
        self.client = client
        self.cfg = cfg
        self.storage = storage
        self.tasks: dict[str, Task] = {}
        self._limits: files.Limits | None = None

    # ------------------------------------------------------------- public API
    async def limits(self) -> files.Limits:
        if self._limits is None:
            self._limits = await files.detect_limits(
                self.client, chat_id=0, chunk_target=self.cfg.chunk_target_size
            )
            log.info(
                "Лимит размера файла: %s (premium=%s), куски по %s",
                files.human(self._limits.hard_limit),
                self._limits.is_premium,
                files.human(self._limits.chunk_target),
            )
        return self._limits

    def cancel(self, job_id: str) -> bool:
        t = self.tasks.get(job_id)
        if not t:
            return False
        t.cancel.set()
        return True

    def running(self, user_id: int) -> list[Job]:
        return [j for j in self.storage.active_jobs_for_user(user_id) if j.id in self.tasks]

    async def start(self, job: Job) -> None:
        if job.id in self.tasks:
            return
        task = Task(job_id=job.id, cancel=threading.Event())
        self.tasks[job.id] = task
        # Весь тяжёлый код (yt-dlp / ffmpeg) — блокирующий, поэтому он
        # выполняется в рабочих потоках через asyncio.to_thread.
        task.future = asyncio.get_running_loop().create_task(self._run(job, task))

    async def resume_unfinished(self) -> None:
        """После рестарта поднимаем зависшие задания как ошибки/ждущие выбора."""
        for job in self.storage.unfinished_jobs():
            if job.status == "await_choice":
                continue  # пользователь сможет выбрать качество заново через /jobs
            job.status = "error"
            job.error = "Бот был перезапущен во время обработки. Отправьте ссылку заново."
            self.storage.save_job(job)

    async def _run(self, job: Job, task: Task) -> None:
        try:
            await self._download_and_send(job, task)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            job.status = "error"
            job.error = str(exc)[:500]
            self.storage.save_job(job)
            with contextlib.suppress(Exception):
                await self.client.send_message(
                    job.chat_id, f"❌ Ошибка: {str(exc)[:400]}\nПопробуйте другое качество или /cancel."
                )
        finally:
            self.tasks.pop(job.id, None)
            if not self.cfg.keep_files and job.file_path and os.path.isfile(job.file_path):
                with contextlib.suppress(Exception):
                    os.remove(job.file_path)
                d = os.path.dirname(job.file_path)
                with contextlib.suppress(Exception):
                    for n in os.listdir(d):
                        if n.startswith(os.path.basename(job.file_path).split(".part")[0]):
                            p = os.path.join(d, n)
                            if os.path.isfile(p):
                                os.remove(p)

    async def _download_and_send(self, job: Job, task: Task) -> None:
        out_dir = os.path.join(self.cfg.download_dir, job.id)
        os.makedirs(out_dir, exist_ok=True)

        job.status = "downloading"
        self.storage.save_job(job)
        await self.client.send_message(job.chat_id, "⬇️ Скачиваю выбранное качество…")

        path, size = await asyncio.to_thread(
            _download_blocking,
            job.url,
            job.fmt_id,
            out_dir,
            self.cfg,
            task.cancel,
            lambda done, total: self._set_dl_progress(job, done, total),
        )
        if task.cancel.is_set():
            raise RuntimeError("Отменено пользователем")
        job.file_path = path
        job.total_size = size
        self.storage.save_job(job)
        await self.client.send_message(
            job.chat_id, f"✅ Скачано: {files.human(size)}. Готовлю к отправке…"
        )

        lim = await self.limits()
        if size <= lim.hard_limit:
            await self._send_one(job, path, lim)
            job.status = "done"
            self.storage.save_job(job)
            return

        # --- превышен лимит: режим нарезки ---
        job.status = "splitting"
        self.storage.save_job(job)
        await self.client.send_message(
            job.chat_id,
            f"⚠️ Файл ({files.human(size)}) больше лимита Telegram ({files.human(lim.hard_limit)}).\n"
            f"Режу на части по ~{files.human(lim.chunk_target)} и отправляю частями…",
        )
        parts = await files.split_file(path, lim.chunk_target, out_dir)
        if task.cancel.is_set():
            raise RuntimeError("Отменено пользователем")
        total = len(parts)
        for i, part in enumerate(parts, 1):
            await self._send_one(
                job,
                part,
                lim,
                caption=f"{job.title[:60]} · часть {i}/{total}",
            )
            job.progress = i / total
            self.storage.save_job(job)
            with contextlib.suppress(Exception):
                os.remove(part)
        job.status = "done"
        job.progress = 1.0
        self.storage.save_job(job)
        await self.client.send_message(job.chat_id, "🏁 Все части отправлены. Приятного просмотра!")

    def _set_dl_progress(self, job: Job, done: float, total: float) -> None:
        frac = (done / total) if total else 0.0
        job.phase_progress = min(1.0, max(0.0, frac))

    async def _send_one(self, job: Job, path: str, lim: files.Limits, caption: str | None = None) -> None:
        size = os.path.getsize(path)
        is_audio = _looks_audio(path)
        cap = caption or f"{job.title[:80]}"
        job.status = "uploading"
        self.storage.save_job(job)

        upload_cb = self._upload_progress(job)
        try:
            await self._do_send(job, path, cap, is_audio, upload_cb, size)
        except (FileReferenceExpiredError, FileIdInvalidError):
            log.warning("file reference expired, retry once")
            await asyncio.sleep(2)
            await self._do_send(job, path, cap, is_audio, upload_cb, size)
        except TimedOutError:
            await asyncio.sleep(3)
            await self._do_send(job, path, cap, is_audio, upload_cb, size)

    async def _do_send(self, job, path, cap, is_audio, upload_cb, size) -> None:
        kwargs: dict = dict(
            file=path,
            caption=cap,
            progress_callback=upload_cb,
            supports_streaming=not is_audio,
        )
        if is_audio:
            kwargs["attributes"] = [DocumentAttributeAudio(duration=int(job.duration or 0), voice=False)]
        try:
            await self.client.send_file(job.chat_id, **kwargs)
        except FilePartTooBigError:
            # серверный лимит оказался меньше ожидаемого — режем ещё сильнее
            new_chunk = max(8 * MIB, int(lim.hard_limit * 0.5))
            lim.hard_limit = new_chunk
            lim.chunk_target = new_chunk
            log.warning("FilePartTooBig: уменьшаем лимит до %s", files.human(new_chunk))
            raise RuntimeError(
                f"Сервер Telegram ограничил размер загрузки {files.human(new_chunk)}. Повторите отправку."
            )

    def _upload_progress(self, job: Job):
        state = {"last": 0.0}

        async def cb(sent: int, total: int) -> None:
            import time

            frac = (sent / total) if total else 0.0
            job.phase_progress = min(1.0, max(0.0, frac))
            if time.time() - state["last"] > 3:
                state["last"] = time.time()
                self.storage.save_job(job)

        return cb


def _looks_audio(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in (".mp3", ".m4a", ".aac", ".opus", ".ogg", ".flac", ".wav")


def _download_blocking(url, fmt_id, out_dir, cfg: Config, cancel: threading.Event, progress_cb):
    """Синхронная обёртка для вызова из рабочего потока (asyncio.to_thread)."""
    return media.download_sync(
        url,
        fmt_id,
        out_dir,
        cookies_file=cfg.cookies_file,
        cookies_browser=cfg.cookies_from_browser,
        progress_cb=progress_cb,
        cancel_flag=cancel,
    )

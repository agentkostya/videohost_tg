"""Извлечение информации о видео, выбор качества и скачивание через yt-dlp."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

import yt_dlp

log = logging.getLogger("bot.media")

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(slots=True)
class FormatOption:
    fid: str            # yt-dlp format selector
    label: str          # "1080p + звук"
    approx_mb: float    # примерный размер
    kind: str = "video"  # video | audio


@dataclass(slots=True)
class MediaInfo:
    url: str
    title: str
    duration: int
    webpage: str
    formats: list[FormatOption] = field(default_factory=list)
    error: str = ""


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _ydl_opts(download_dir: str, cookies_file: str = "", cookies_browser: str = "") -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "format_sort": ["res", "brs", "fps"],
    }
    if download_dir:
        opts["paths"] = {"home": download_dir}
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


def _h(ms: int | None) -> str:
    s = int((ms or 0) // 1000)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


async def probe(url: str, cookies_file: str = "", cookies_browser: str = "") -> MediaInfo:
    """Получает список доступных вариантов качества."""
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, lambda: _probe_sync(url, cookies_file, cookies_browser))
    except Exception as exc:  # noqa: BLE001
        log.exception("probe failed")
        return MediaInfo(url=url, title="", duration=0, webpage="", error=str(exc)[:400])
    return info


def _probe_sync(url: str, cookies_file: str, cookies_browser: str) -> MediaInfo:
    opts = _ydl_opts("", cookies_file, cookies_browser)
    opts["simulate"] = True
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(url, download=False)
    data = data or {}
    if data.get("_type") == "playlist":
        entries = [e for e in (data.get("entries") or []) if e]
        data = entries[0] if entries else {}

    duration = int(data.get("duration") or 0)
    title = data.get("title") or data.get("id") or "video"
    info = MediaInfo(url=url, title=title, duration=duration, webpage=data.get("webpage_url", url))

    vids: dict[int, dict] = {}
    best_audio: dict | None = None
    for f in data.get("formats") or []:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        height = f.get("height") or 0
        tbr = f.get("tbr") or 0
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if vcodec != "none" and height:
            cur = vids.get(height)
            if cur is None or (cur.get("tbr") or 0) < tbr:
                vids[height] = f
        if acodec != "none" and vcodec == "none":
            if best_audio is None or (best_audio.get("tbr") or 0) < tbr:
                best_audio = f

    has_muxed = any(
        (f.get("vcodec") not in (None, "none")) and (f.get("acodec") not in (None, "none"))
        for f in (data.get("formats") or [])
    )

    for height in sorted(vids, reverse=True):
        f = vids[height]
        fid = f.get("format_id", "")
        if has_muxed:
            selector = fid
        elif best_audio is not None:
            selector = f"{fid}+{best_audio.get('format_id', 'bestaudio')}"
        else:
            selector = f"{fid}+bestaudio/best"
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if not size and duration and f.get("tbr"):
            size = int(f["tbr"] * 1000 / 8 * duration * 1000)
        info.formats.append(
            FormatOption(
                fid=selector,
                label=f"{height}p · {_h(duration)}" + ("" if has_muxed else " + звук"),
                approx_mb=round(size / 1024 / 1024, 1) if size else 0.0,
            )
        )

    # отдельная аудиодорожка
    a_fids = [f for f in (data.get("formats") or []) if (f.get("acodec") not in (None, "none")) and (f.get("vcodec") in (None, "none"))]
    if a_fids:
        ab = max(a_fids, key=lambda x: x.get("abr") or x.get("tbr") or 0)
        sel = ab.get("format_id", "bestaudio")
        size = ab.get("filesize") or ab.get("filesize_approx") or 0
        info.formats.append(
            FormatOption(fid=f"{sel}/bestaudio/best", label="Только аудио (mp3/m4a)", approx_mb=round(size / 1024 / 1024, 1) if size else 0.0, kind="audio")
        )

    if not info.formats:
        size = data.get("filesize") or data.get("filesize_approx") or 0
        info.formats.append(FormatOption(fid="best", label="Лучшее доступное", approx_mb=round(size / 1024 / 1024, 1) if size else 0.0))
    return info


def download_sync(
    url: str,
    fmt_id: str,
    out_dir: str,
    *,
    cookies_file: str = "",
    cookies_browser: str = "",
    progress_cb=None,
    cancel_flag=None,
) -> tuple[str, int]:
    """Синхронное скачивание (вызывать из рабочего потока).

    Возвращает (путь, размер). progress_cb(done_bytes, total_bytes).
    """
    return _download_sync(url, fmt_id, out_dir, cookies_file, cookies_browser, progress_cb, cancel_flag)


async def download(
    url: str,
    fmt_id: str,
    out_dir: str,
    *,
    cookies_file: str = "",
    cookies_browser: str = "",
    progress_cb=None,
    cancel_flag=None,
) -> tuple[str, int]:
    """Асинхронная обёртка над download_sync."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _download_sync(url, fmt_id, out_dir, cookies_file, cookies_browser, progress_cb, cancel_flag),
    )


def _download_sync(url, fmt_id, out_dir, cookies_file, cookies_browser, progress_cb, cancel_flag) -> tuple[str, int]:
    os.makedirs(out_dir, exist_ok=True)
    opts = _ydl_opts(out_dir, cookies_file, cookies_browser)
    opts.update(
        {
            "format": fmt_id,
            "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noprogress": True,
            "progress_hooks": [],
        }
    )

    state = {"path": "", "size": 0}

    def hook(d: dict) -> None:
        if cancel_flag is not None and cancel_flag.is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled by user")
        if d.get("status") == "finished":
            state["path"] = d.get("filename") or state["path"]
        if progress_cb:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            try:
                progress_cb(done, total)
            except Exception:  # noqa: BLE001
                log.exception("progress_cb error")

    opts["progress_hooks"].append(hook)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    path = state["path"]
    if not path or not os.path.isfile(path):
        # ищем самый свежий файл в папке
        cands = [
            os.path.join(out_dir, n)
            for n in os.listdir(out_dir)
            if os.path.isfile(os.path.join(out_dir, n))
        ]
        if not cands:
            raise RuntimeError("Файл не найден после скачивания")
        path = max(cands, key=os.path.getmtime)
    return path, os.path.getsize(path)

"""Работа с файлами: определение лимитов Telegram и нарезка больших видео."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger("bot.files")

MIB = 1024 * 1024
GIB = 1024**3


@dataclass(slots=True)
class Limits:
    """Фактические лимиты аккаунта, определённые экспериментально."""

    hard_limit: int          # максимальный размер одного файла, байт
    chunk_target: int        # целевой размер куска при нарезке
    is_premium: bool
    verified: bool = False   # True, если лимит подтверждён пробной отправкой


def human(nbytes: float) -> str:
    units = ["Б", "КиБ", "МиБ", "GiB"]
    v = float(nbytes)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "Б" else f"{int(v)} Б"
        v /= 1024
    return f"{v:.1f} GiB"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


PREMIUM_HARD_LIMIT = 4 * GIB   # абсолютный потолок Telegram для одного файла
FREE_HARD_LIMIT = 2 * GIB


async def detect_limits(
    client,
    chat_id: int,
    chunk_target: int,
    *,
    force_premium: bool = False,
    hard_cap: int = PREMIUM_HARD_LIMIT,
) -> Limits:
    """Определяет МАКСИМАЛЬНО возможный лимит размера файла для аккаунта.

    Лимит выдаёт сам Telegram в зависимости от типа аккаунта — отдельно
    «для бота» его оплатить нельзя, но можно поднять до потолка, разрешённого
    аккаунту-загрузчику:
      - Bot API (@BotFather)       — 50 MiB (увеличить нельзя никак);
      - MTProto-аккаунт            — 2 GiB;
      - MTProto + Telegram Premium — 4 GiB («продление лимита»: подписка
        оформляется на аккаунт через @PremiumBot либо подарком);
      - self-hosted дата-центр     — до 4 GiB.

    Алгоритм: читаем флаг premium, затем переспрашиваем конфиг сервера
    (help.getConfig, свойство upload_max_filesize_mb) и берём максимум из
    известных значений с ограничением сверху hard_cap. Значение сервера
    перечитывается при каждом старте, поэтому сразу после активации Premium
    лимит поднимется сам. Если файл больше полученного лимита — он режется.
    """
    is_premium = force_premium
    try:
        me = await client.get_me()
        is_premium = is_premium or bool(getattr(me, "premium", False))
    except Exception as exc:  # noqa: BLE001
        log.debug("premium check failed: %s", exc)

    hard = PREMIUM_HARD_LIMIT if is_premium else FREE_HARD_LIMIT

    # Спрашиваем у сервера актуальный лимит загрузки для ЭТОГО аккаунта.
    try:
        from telethon import functions

        cfg = await client(functions.help.GetConfigRequest())
        props = {p.name: p.value for p in (cfg.properties or [])}
        for key in ("upload_max_file_size_mb", "upload_max_filesize_mb"):
            raw = props.get(key)
            if not raw:
                continue
            try:
                val = int(float(raw)) * MIB
            except (TypeError, ValueError):
                continue
            if val > 0:
                new_hard = min(max(hard, val), hard_cap)
                if new_hard > hard:
                    log.info(
                        "Сервер разрешил больший лимит: %s -> %s",
                        human(hard), human(new_hard),
                    )
                hard = new_hard
    except Exception as exc:  # noqa: BLE001
        log.debug("getConfig limits unavailable: %s", exc)

    hard = min(hard, hard_cap)
    chunk = min(max(chunk_target, 8 * MIB), hard)
    return Limits(hard_limit=hard, chunk_target=chunk, is_premium=is_premium)



# ---------------------------------------------------------------- ffprobe helpers
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_video(path: str) -> dict:
    """Возвращает {'duration': float, 'has_audio': bool}."""
    res = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path,
        ]
    )
    duration, has_audio = 0.0, False
    try:
        data = json.loads(res.stdout or "{}")
        duration = float(data.get("format", {}).get("duration") or 0)
        for st in data.get("streams", []):
            if st.get("codec_type") == "audio":
                has_audio = True
                break
    except Exception:  # noqa: BLE001
        log.warning("ffprobe parse failed for %s: %s", path, res.stderr[:200])
    return {"duration": duration, "has_audio": has_audio}


def estimate_bitrate(path: str) -> int:
    """Общий битрейт в битах/сек."""
    res = _run(["ffprobe", "-v", "error", "-show_entries", "format=bit_rate,size,duration", "-of", "json", path])
    try:
        fmt = json.loads(res.stdout or "{}").get("format", {})
        br = int(fmt.get("bit_rate") or 0)
        if br <= 0:
            size = int(fmt.get("size") or 0)
            dur = float(fmt.get("duration") or 0)
            if size and dur:
                br = int(size * 8 / dur)
        return max(br, 200_000)
    except Exception:  # noqa: BLE001
        return 1_500_000


def _ffmpeg_copy_segment(src: str, start: float, length: float, dst: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", dst,
    ]
    res = _run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg copy error: {res.stderr[-400:]}")


def _ffmpeg_reencode_segment(src: str, start: float, length: float, dst: str, max_width: int = 1280, crf: int = 26) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-vf", f"scale='min({max_width},iw)':-2",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", dst,
    ]
    res = _run(cmd)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg reencode error: {res.stderr[-400:]}")


def split_file_sync(path: str, max_bytes: int, out_dir: str, *, progress_cb=None) -> list[str]:
    """Режет видео на части <= max_bytes. Сначала — без перекодирования;
    если кусок всё равно больше лимита — перекодируем его со сжатием.
    Возвращает список путей частей по порядку.
    """
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg не установлен — нарезка больших файлов невозможна")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0].replace(" ", "_")
    meta = probe_video(path)
    total_dur = meta["duration"]
    if total_dur <= 0:
        raise RuntimeError("Не удалось определить длительность файла")
    bitrate = estimate_bitrate(path)
    seg_dur = max(5.0, (max_bytes * 0.90) * 8 / bitrate)

    parts: list[str] = []
    idx = 0
    start = 0.0
    while start < total_dur - 0.05:
        length = min(seg_dur, total_dur - start)
        dst = os.path.join(out_dir, f"{base}.part{idx + 1:03d}.mp4")
        _ffmpeg_copy_segment(path, start, length, dst)
        size = os.path.getsize(dst) if os.path.exists(dst) else 0
        if size > max_bytes:
            # плотный фрагмент — перекодируем сильнее
            log.info("Часть %s весит %s (> %s), перекодируем…", idx + 1, human(size), human(max_bytes))
            _ffmpeg_reencode_segment(path, start, length, dst, max_width=1280, crf=28)
            size = os.path.getsize(dst)
            guard = 0
            while size > max_bytes and guard < 4:
                crf = 28 + 3 * (guard + 1)
                width = max(426, 1280 - 240 * (guard + 1))
                _ffmpeg_reencode_segment(path, start, length, dst, max_width=width, crf=crf)
                size = os.path.getsize(dst)
                guard += 1
            if size > max_bytes:
                # последний шанс — режем этот отрезок пополам рекурсивно
                half = length / 2
                tmp = dst
                _ffmpeg_copy_segment(path, start, half, tmp)
                parts.append(tmp)
                dst2 = os.path.join(out_dir, f"{base}.part{idx + 1:03d}b.mp4")
                _ffmpeg_copy_segment(path, start + half, length - half, dst2)
                parts.append(dst2)
                idx += 1
                start += length
                continue
        parts.append(dst)
        if progress_cb:
            progress_cb(min(1.0, (start + length) / total_dur), len(parts))
        idx += 1
        start += length
    return parts


async def split_file(path: str, max_bytes: int, out_dir: str, progress_cb=None) -> list[str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: split_file_sync(path, max_bytes, out_dir, progress_cb))

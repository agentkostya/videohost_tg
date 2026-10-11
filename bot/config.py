"""Загрузка конфигурации из переменных окружения / .env файла."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv опционален
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _split_list(raw: str) -> list[str]:
    items: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        part = part.strip().lstrip("@")
        if part:
            items.append(part)
    return items


@dataclass(slots=True)
class Config:
    # --- Telegram (my.telegram.org) ---
    api_id: int = int(os.getenv("API_ID", "0") or 0)
    api_hash: str = os.getenv("API_HASH", "").strip()
    bot_token: str = os.getenv("BOT_TOKEN", "").strip()

    # --- Обязательная подписка ---
    required_channels: list[str] = field(
        default_factory=lambda: _split_list(os.getenv("REQUIRED_CHANNELS", ""))
    )
    required_bot_username: str = os.getenv("REQUIRED_BOT_USERNAME", "").strip().lstrip("@")

    # --- Хранение состояния ---
    session_path: str = os.getenv("SESSION_PATH", "./data/bot_session.session")
    db_path: str = os.getenv("DB_PATH", "./data/bot_state.db")

    # --- Загрузка файлов ---
    # Лимит на один файл зависит от аккаунта, а НЕ от настроек бота:
    #   Bot API (@BotFather)      — 50 MiB (увеличить нельзя ничем)
    #   MTProto-аккаунт           — 2 GiB
    #   MTProto + Telegram Premium— 4 GiB
    #   self-hosted DC            — до 4 GiB
    # MAX_FILE_SIZE — жёсткий потолок (защита от дисковой ошибки), по умолчанию 4 GiB.
    max_file_size: int = int(os.getenv("MAX_FILE_SIZE", str(4 * 1024**3)))
    # Целевой размер одного "куска" при нарезке больших видео.
    # По умолчанию максимум для Premium-аккаунта: целые файлы до 4 GiB
    # уходят одним сообщением, режутся только файлы больше лимита.
    chunk_target_size: int = int(os.getenv("CHUNK_TARGET_SIZE", str(4 * 1024**3)))
    # Принудительно считать аккаунт premium, даже если флаг не подтянулся
    # (например, куплен недавно и кэш get_me ещё старый).
    force_premium: bool = os.getenv("FORCE_PREMIUM", "false").lower() in ("1", "true", "yes")
    download_dir: str = os.getenv("DOWNLOAD_DIR", "./downloads")
    keep_files: bool = os.getenv("KEEP_FILES", "false").lower() in ("1", "true", "yes")

    # --- Приватные источники ---
    cookies_file: str = os.getenv("COOKIES_FILE", "").strip()
    cookies_from_browser: str = os.getenv("COOKIES_FROM_BROWSER", "").strip()

    # --- Прочее ---
    admin_ids: list[int] = field(
        default_factory=lambda: [
            int(x) for x in _split_list(os.getenv("ADMIN_IDS", "")) if x.lstrip("-").isdigit()
        ]
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self) -> None:
        missing = []
        if not self.api_id:
            missing.append("API_ID")
        if not self.api_hash:
            missing.append("API_HASH")
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if missing:
            raise RuntimeError(
                "Не заданы обязательные переменные: "
                + ", ".join(missing)
                + ". Скопируйте config_example.env в .env и заполните значения."
            )
        Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        Path(self.session_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

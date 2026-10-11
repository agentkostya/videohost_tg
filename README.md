# VideoHost TG Bot — код живёт на GitHub, но исполняться должен на сервере

## Короткий ответ
GitHub сам по себе бота запускать **не умеет**. Он подходит только для:
- хранения кода и истории изменений;
- сборки через GitHub Actions (но Actions — это CI/CD, а не хостинг: там нельзя
  держать постоянный процесс с long-polling, и нет доступа к файловой системе между запусками);
- публикации релизов.

Сам бот (Telethon-клиент + yt-dlp + ffmpeg) должен работать на **постоянном сервере**:
VPS (от ~3–5 $/мес), выделенная машина или контейнер-хостинг (Fly.io, Railway, Docker).

## Почему именно Telethon, а не Bot API
Обычный токен от @BotFather (Bot API) жёстко ограничен: **50 MiB на файл**,
4 GiB лимит не поднять никакими уловками. Поэтому в этом проекте используется
Telethon + `API_ID`/`API_HASH` из my.telegram.org:
- обычный аккаунт — до 2 GiB на файл;
- Telegram Premium — до 4 GiB;
- файлы больше лимита автоматически режутся ffmpeg на части (`bot/files.py`,
  функция `split_file_sync`) и отправляются частями с подписями «часть N/M».

То есть «обход» ограничения 50 МБ — это не трюк вокруг Bot API, а легитимная
работа через MTProto-аккаунт + нарезка больших файлов.

## Что уже реализовано в пакете `bot/`
| Модуль | Назначение |
|---|---|
| `config.py` | Загрузка настроек из `.env` (токены, каналы, директории) |
| `storage.py` | SQLite: пользователи, задачи, состояние прогресса |
| `subscription.py` | Проверка подписки на обязательные каналы / запуска верификационного бота |
| `media.py` | yt-dlp: разбор ссылки, список доступных форматов (видео/аудио, качество), скачивание |
| `files.py` | Определение лимитов аккаунта (2/4 GiB), probe через ffprobe, нарезка файлов ffmpeg |
| `pipeline.py` | Очередь задач: скачал → порезал → загрузил → отправил, прогресс, отмена, возобновление после рестарта |
| `handlers.py` | Команды `/start`, `/check`, `/jobs`, `/cancel`, приём ссылок, inline-кнопки выбора качества |

## Деплой: GitHub → сервер
1. Пуш кода в приватный репозиторий GitHub.
2. На сервере: `git clone` + systemd-юнит (или Docker Compose). Секреты — в `.env`,
   который **никогда** не коммитится (`.gitignore` уже настроен).
3. Обновления: `git pull && systemctl restart videohost-bot`.

### requirements.txt (пример)
```
telethon
yt-dlp
python-dotenv
```
Плюс системные зависимости: `ffmpeg`, `python3.10+`.

### systemd-юнит (пример)
```ini
[Unit]
Description=VideoHost TG Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/videohost-bot
EnvironmentFile=/opt/videohost-bot/.env
ExecStart=/opt/videohost-bot/venv/bin/python -m bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Docker Compose (альтернатива)
```yaml
services:
  bot:
    build: .
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./downloads:/app/downloads
    restart: unless-stopped
```

## Важные оговорки
- **Авторизация аккаунта**: Telethon-клиент нужно один раз залогинить (`phone login`)
  на сервере; session-файл хранится в `data/` и не должен попадать в Git.
- **Риск для аккаунта**: автоматизация скачивания чужого контента может нарушать
  ToS Telegram и авторские права — используйте свой аккаунт осознанно.
- **Приватные источники**: для закрытых каналов/сайтов задайте `COOKIES_FILE`.
- **Нагрузка**: одновременные закачки ограничены очередью в `pipeline.py`;
  при росте аудитории добавляйте воркеров/второй сервер.

## Итог
Код — на GitHub ✅. Работающий бот — на вашем сервере/VPS ✅.
GitHub Actions можно использовать только для тестов и автодеплоя (ssh на сервер после push),
но не как место постоянной работы процесса.

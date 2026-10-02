import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: frozenset[int]
    db_path: Path
    timezone: str = "Europe/Moscow"

    @classmethod
    def from_env(cls):
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Заполните BOT_TOKEN в .env токеном от @BotFather.")
        try:
            admins = frozenset(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
            if any(x <= 0 for x in admins):
                raise ValueError
        except ValueError as exc:
            raise ValueError("ADMIN_IDS: положительные числовые ID через запятую.") from exc
        tz = os.getenv("TIMEZONE", "Europe/Moscow").strip()
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Неизвестный TIMEZONE. Например: Europe/Moscow.") from exc
        return cls(token, admins, Path(os.getenv("DB_PATH", "data/parabot.sqlite3")), tz)

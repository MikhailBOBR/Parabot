import logging
import time

from telegram.error import NetworkError, TelegramError

from .bot import build_app
from .config import Settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client INFO logs contain token-bearing URLs. Never print those.
    for name in ("httpx", "httpcore", "apscheduler", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Настройка: {exc}")
        raise SystemExit(2) from None

    retry_delay = 5
    while True:
        try:
            build_app(settings).run_polling(
                timeout=30,
                bootstrap_retries=0,
                allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
                drop_pending_updates=True,
            )
            return
        except NetworkError as exc:
            logging.getLogger(__name__).warning(
                "Telegram network error (%s); reconnecting in %s seconds.",
                type(exc).__name__,
                retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except TelegramError as exc:
            print(
                f"Telegram: {type(exc).__name__}. Проверьте токен, интернет "
                "и отсутствие второго экземпляра."
            )
            raise SystemExit(2) from None


if __name__ == "__main__":
    main()

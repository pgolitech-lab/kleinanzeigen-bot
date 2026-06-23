# Точка входа: инициализация БД → старт scheduler-а → запуск FastAPI.
# По Ctrl+C uvicorn возвращает управление, мы останавливаем scheduler и выходим.

import logging

import uvicorn

import config
import log_buffer
import scheduler
from modules import telegram_bot
from web.app import app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Параллельно с stdout-handler-ом пишем в кольцевой буфер для веб-морды
    log_buffer.install()
    log = logging.getLogger("main")

    # 1. БД: создать таблицы + засеять дефолтные настройки
    log.info("Инициализация БД...")
    config.bootstrap()

    # 2. Поднять scheduler в фоне (отдельный поток)
    log.info("Запуск scheduler...")
    sched = scheduler.start()

    # 2b. Бот теперь чисто исходящий (без polling). Ставим нативную
    #     Telegram Menu Button у поля ввода → открывает Mini App.
    log.info("Установка Telegram Menu Button...")
    try:
        telegram_bot.set_menu_button()
    except Exception:
        log.exception("set_menu_button failed")

    # 3. Запустить FastAPI. uvicorn сам ловит SIGINT/SIGTERM и
    #    корректно завершает worker-ы — после этого управление возвращается сюда.
    host = config.web_host()
    port = config.web_port()
    log.info("Запуск FastAPI на http://%s:%d", host, port)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        # 4. Останавливаем scheduler (jobs допишутся, новых не будет)
        log.info("Остановка scheduler...")
        sched.shutdown(wait=False)
        log.info("Завершено.")


if __name__ == "__main__":
    main()

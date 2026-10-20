# Точка входа: инициализация БД → старт scheduler-а → запуск FastAPI.
# По Ctrl+C uvicorn возвращает управление, мы останавливаем scheduler и выходим.

import logging

import uvicorn

import config
import log_buffer
import scheduler
from modules import telegram_bot
from web.app import app


# Шумные логгеры: APScheduler пишет INFO на каждый запуск/завершение джоба
# (poll каждые 15 с → ~75 % журнала), httpx — каждый HTTP-запрос. Оставляем
# только WARNING+: пропуски джобов («skipped: maximum number of running
# instances») и ошибки по-прежнему видны.
_QUIET_LOGGERS = ("apscheduler.executors.default", "apscheduler.scheduler", "httpx")

# Пути, успешные запросы к которым не пишем в access-лог: health-пробы
# (watchdog/внешний мониторинг) и автообновление списка Mini App.
# Ошибки (status >= 400) по этим путям логируются как обычно.
_ACCESS_LOG_SKIP_PATHS = ("/health", "/api/ma/pipeline")


class _AccessLogFilter(logging.Filter):
    """Фильтр uvicorn.access: args = (client, method, path, http_version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path = str(args[2]).split("?", 1)[0]
        try:
            status = int(args[4])
        except (TypeError, ValueError):
            return True
        return not (status < 400 and path in _ACCESS_LOG_SKIP_PATHS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Параллельно с stdout-handler-ом пишем в кольцевой буфер для веб-морды
    log_buffer.install()
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
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

    log.info("Очистка legacy reply-keyboard...")
    try:
        telegram_bot.clear_reply_keyboards()
    except Exception:
        log.exception("clear_reply_keyboards failed")

    # 2c. Регистрация команд бота + callback-поллер (команды + legacy-кнопки)
    log.info("Регистрация команд бота...")
    try:
        telegram_bot.set_bot_commands()
    except Exception:
        log.exception("set_bot_commands failed")

    # Callback/message поллер: /tasks, close_THREAD, legacy-кнопки
    log.info("Запуск callback-query poller...")
    try:
        telegram_bot.start_callback_poller()
    except Exception:
        log.exception("start_callback_poller failed")

    # 3. Запустить FastAPI. uvicorn сам ловит SIGINT/SIGTERM и
    #    корректно завершает worker-ы — после этого управление возвращается сюда.
    host = config.web_host()
    port = config.web_port()
    log.info("Запуск FastAPI на http://%s:%d", host, port)
    # Config/Server вместо uvicorn.run: uvicorn настраивает свои логгеры в
    # Config.__init__, и фильтр access-лога вешаем уже после этого.
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())
    try:
        uvicorn.Server(uv_config).run()
    finally:
        # 4. Останавливаем scheduler (jobs допишутся, новых не будет)
        log.info("Остановка scheduler...")
        sched.shutdown(wait=False)
        log.info("Завершено.")


if __name__ == "__main__":
    main()

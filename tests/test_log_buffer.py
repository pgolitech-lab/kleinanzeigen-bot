"""Регресс-тесты для log_buffer — конкурентный доступ к кольцевому буферу.

До фикса `get_since` итерировал `deque` без блокировки, пока `emit` писал из
других потоков → "RuntimeError: deque mutated during iteration" на /api/logs.
"""

import logging
import threading

import log_buffer


def _make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


def test_concurrent_emit_and_read_no_crash():
    """Читатель и писатель одновременно — не должно быть RuntimeError."""
    handler = log_buffer._RingBufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        i = 0
        while not stop.is_set():
            handler.emit(_make_record(f"msg-{i}"))
            i += 1

    def reader():
        try:
            for _ in range(2000):
                log_buffer.get_since(0)
                log_buffer.get_since(None)
        except BaseException as e:  # noqa: BLE001 — фиксируем любой сбой итерации
            errors.append(e)

    writers = [threading.Thread(target=writer) for _ in range(3)]
    for w in writers:
        w.start()
    r = threading.Thread(target=reader)
    r.start()
    r.join()
    stop.set()
    for w in writers:
        w.join()

    assert not errors, f"Конкурентное чтение упало: {errors[0]!r}"


def test_ids_are_unique_under_concurrency():
    """Инкремент id атомарен — конкурентные emit не выдают дублей id."""
    handler = log_buffer._RingBufferHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    # маленький буфер не переполнить: 4 потока * 200 < maxlen 2000
    log_buffer._BUFFER.clear()
    barrier = threading.Barrier(4)

    def spam():
        barrier.wait()
        for i in range(200):
            handler.emit(_make_record(f"x-{i}"))

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = [r["id"] for r in log_buffer.get_since(None, limit=10000)]
    assert len(ids) == len(set(ids)), "Обнаружены дублирующиеся id логов"

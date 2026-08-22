#!/usr/bin/env python3
"""
Content Bot — автоматический постинг в каналы MAX.

Команды:
  python main.py fetch      — собрать новый контент из всех источников
  python main.py post       — опубликовать один батч прямо сейчас (тест)
  python main.py run        — запустить legacy-планировщик (+ news realtime при enabled)
  python main.py news-run   — запустить только realtime-движок новостной сетки
  python main.py status     — показать статистику очереди
"""
import sys
import logging
import yaml
import os
import fcntl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx на INFO логирует полные URL — для Telegram Bot API это сливает токен в bot.log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# APScheduler executors на INFO раз в минуту бьют bot.log сотнями строк "job executed successfully".
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)  # 2026-06-30: spam "Language detector not installed"
logger = logging.getLogger(__name__)


from utils import load_config, load_env

# Eagerly load .env so TELEGRAM_BOT_TOKEN and friends are available
# in os.environ even for code paths that don't go through load_config().
load_env()


def cmd_fetch(config):
    from fetcher import fetch_all
    logger.info("=== Сбор контента ===")
    total = fetch_all(config)
    print(f"✅ Добавлено в очередь: {total} постов")


def cmd_post(config):
    from scheduler import make_grid_post_batch
    import db
    db.init_db()
    logger.info("=== Тестовая публикация ===")
    grids = config.get("grids", {})
    if not grids:
        print("❌ Нет сеток в конфиге")
        return
    # Если передана конкретная сетка: python main.py post "Города России"
    target_grid = sys.argv[2] if len(sys.argv) > 2 else None
    if target_grid:
        if target_grid not in grids:
            print(f"❌ Сетка '{target_grid}' не найдена. Доступные: {list(grids.keys())}")
            return
        make_grid_post_batch(target_grid)()
        print(f"✅ Батч опубликован для сетки '{target_grid}'")
    else:
        for grid_name in grids:
            make_grid_post_batch(grid_name)()
        print("✅ Батч опубликован для всех сеток")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE_DIR, "contentbot.pid")
LOCK_FILE = os.path.join(BASE_DIR, "contentbot.lock")
_LOCK_FH = None


def _acquire_single_instance_lock() -> bool:
    """Гарантирует один экземпляр процесса через file lock (Linux, fcntl)."""
    global _LOCK_FH
    _LOCK_FH = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    _LOCK_FH.seek(0)
    _LOCK_FH.truncate()
    _LOCK_FH.write(str(os.getpid()))
    _LOCK_FH.flush()
    return True


def _kill_previous():
    """Убивает предыдущий экземпляр content-bot если он запущен."""
    if not os.path.exists(PID_FILE):
        return
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
        if old_pid == os.getpid():
            return
        # Проверяем что процесс существует
        os.kill(old_pid, 0)
        # Убиваем
        import signal
        os.kill(old_pid, signal.SIGTERM)
        import time
        time.sleep(2)
        # Если не умер — SIGKILL
        try:
            os.kill(old_pid, 0)
            os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        logger.info(f"Завершён предыдущий экземпляр (PID {old_pid})")
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    finally:
        try:
            os.remove(PID_FILE)
        except Exception:
            pass


def _write_pid():
    """Записывает PID текущего процесса."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _cleanup_pid():
    """Удаляет PID-файл при завершении."""
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


def _install_signal_handlers(label: str = "bot"):
    """Регистрирует SIGTERM/SIGINT handler с graceful shutdown логированием.

    Без этого systemctl restart обрывает процесс жёстко — pending задачи
    в asyncio/APScheduler остаются в состоянии 'processing' до следующего
    requeue_stale_processing(older_than_minutes=30).
    """
    import signal
    def _handler(signum, frame):
        try:
            logger.warning(
                f"[{label}] Получен сигнал {signal.Signals(signum).name} — "
                f"начинаем graceful shutdown"
            )
        except Exception:
            pass
        # Стандартный путь: поднять KeyboardInterrupt чтобы выйти из start()
        # или asyncio.run() через стандартную обработку.
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError) as e:
        # signal.signal работает только в главном потоке — запасной путь
        logger.warning(f"[{label}] Не удалось установить signal handlers: {e}")


def cmd_run(config):
    import atexit
    from scheduler import start

    # Жёсткая защита от второго экземпляра (и под systemd, и вручную)
    if not _acquire_single_instance_lock():
        logger.error("Другой экземпляр content-bot уже запущен. Завершаем запуск.")
        print("❌ content-bot уже запущен")
        return

    # Дополнительный PID-файл для удобной диагностики
    _write_pid()
    atexit.register(_cleanup_pid)

    # FIX (2026-04-30): graceful shutdown на SIGTERM/SIGINT.
    _install_signal_handlers("scheduler")

    logger.info("=== Запуск планировщика ===")
    try:
        import db as _db
        from regex_store import init_regex_store, migrate_from_config
        _db.init_db()
        init_regex_store()
        migrated = migrate_from_config(config)
        if migrated:
            logger.info(f"Regex rules migrated from config: +{migrated}")
        restored = _db.requeue_stale_processing(older_than_minutes=30)
        ads_restored = _db.requeue_stale_ads_processing(older_than_minutes=30)
        if restored:
            logger.info(f"Восстановлено зависших processing -> pending: {restored}")
        if ads_restored:
            logger.info(f"Восстановлено зависших ads processing -> pending: {ads_restored}")
    except Exception as e:
        logger.warning(f"Не удалось восстановить processing-посты: {e}")

    _fetch_if_empty(config)
    _notify_start(config)

    # Slot Monitor (Task 11) — стартует в daemon-потоке только если
    # config['slot_monitor']['enabled'] = true. По умолчанию false → no-op.
    try:
        from slot_monitor import start_in_thread as _sm_start_in_thread
        _sm_start_in_thread(config)
    except Exception as e:
        logger.warning(f"slot_monitor failed to start: {e}")

    start(config)


def _fetch_if_empty(config: dict):
    """При старте: если очередь пуста для сетки — сразу делаем fetch."""
    try:
        import sqlite3
        from fetcher import fetch_grid
        import db as _db
        _db.init_db()
        conn = sqlite3.connect(_db.DB_PATH)
        grids = config.get("grids", {})
        for grid_name in grids:
            # Проверяем есть ли pending посты для каналов этой сетки
            channels = grids[grid_name]
            placeholders = ','.join('?' * len(channels))
            cur = conn.execute(
                f"SELECT COUNT(*) FROM posts WHERE status='pending' AND channel IN ({placeholders})",
                channels
            )
            count = cur.fetchone()[0]
            # Живой режим не нуждается в pre-fetch (берёт только свежие)
            live = config.get("grid_settings", {}).get(grid_name, {}).get("live_mode", False)
            if count == 0 and not live:
                logger.info(f"[{grid_name}] Очередь пуста при старте — запускаем fetch")
                try:
                    added = fetch_grid(config, grid_name)
                    logger.info(f"[{grid_name}] Стартовый fetch: +{added} постов")
                except Exception as e:
                    logger.warning(f"[{grid_name}] Стартовый fetch ошибка: {e}")
        conn.close()
    except Exception as e:
        logger.warning(f"_fetch_if_empty ошибка: {e}")


def _notify_start(config: dict):
    """Отправляет уведомление суперадмину о запуске. Не чаще 1 раза в 2 минуты."""
    try:
        import json, httpx, time as _time
        # Защита от спама при быстрых перезапусках
        flag_file = "/tmp/contentbot_start_notified"
        if os.path.exists(flag_file):
            if _time.time() - os.path.getmtime(flag_file) < 120:
                return
        open(flag_file, 'w').close()
        admins_path = os.path.join(os.path.dirname(__file__), "admins.json")
        if not os.path.exists(admins_path):
            return
        with open(admins_path, encoding="utf-8") as f:
            admins = json.load(f)
        superadmins = [uid for uid, role in admins.items() if role == "superadmin"]
        if not superadmins:
            return

        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not tg_token:
            return
        grids = config.get("grids", {})
        grid_settings = config.get("grid_settings", {})
        lines = ["🚀 *Content-bot запущен*\n"]
        for gname, gchs in grids.items():
            gs = grid_settings.get(gname, {})
            enabled = gs.get("enabled", True)
            status = "✅" if enabled else "⏸"
            sources_cfg = config.get("sources", {})
            active = sum(1 for ch in gchs if any(len(v) > 0 for v in sources_cfg.get(ch, {}).values()))
            lines.append(f"{status} *{gname}*: {active}/{len(gchs)} каналов с источниками")

        text = "\n".join(lines)
        for uid in superadmins:
            try:
                httpx.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": int(uid), "text": text, "parse_mode": "Markdown"},
                    timeout=10,
                )
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о старте: {e}")


def cmd_news_run(config):
    from news_realtime_engine import run_engine_from_config, init_news_schema
    logger.info("=== Запуск news realtime engine ===")
    # FIX (2026-04-30): graceful shutdown на SIGTERM/SIGINT.
    _install_signal_handlers("news-engine")
    init_news_schema()

    # Slot Monitor (Task 11) — стартует в daemon-потоке только если
    # config['slot_monitor']['enabled'] = true. По умолчанию false → no-op.
    try:
        from slot_monitor import start_in_thread as _sm_start_in_thread
        _sm_start_in_thread(config)
    except Exception as e:
        logger.warning(f"slot_monitor failed to start: {e}")

    run_engine_from_config()


def cmd_status():
    import db
    db.init_db()
    stats = db.get_stats()
    if not stats:
        print("База данных пуста")
        return
    print(f"\n{'Канал':<12} {'Статус':<10} {'Кол-во':>6}")
    print("-" * 30)
    for row in sorted(stats, key=lambda x: (x["channel"], x["status"])):
        print(f"{row['channel']:<12} {row['status']:<10} {row['cnt']:>6}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        cmd_status()
        return

    config = load_config()

    if cmd == "fetch":
        cmd_fetch(config)
    elif cmd == "post":
        cmd_post(config)
    elif cmd == "run":
        cmd_run(config)
    elif cmd == "news-run":
        cmd_news_run(config)
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

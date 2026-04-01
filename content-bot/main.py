#!/usr/bin/env python3
"""
Content Bot — автоматический постинг в каналы MAX.

Команды:
  python main.py fetch    — собрать новый контент из всех источников
  python main.py post     — опубликовать один батч прямо сейчас (тест)
  python main.py run      — запустить планировщик (бесконечно)
  python main.py status   — показать статистику очереди
"""
import sys
import logging
import yaml
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "bot.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    cfg_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cmd_fetch(config):
    from fetcher import fetch_all
    logger.info("=== Сбор контента ===")
    total = fetch_all(config)
    print(f"✅ Добавлено в очередь: {total} постов")


def cmd_post(config):
    from scheduler import post_batch
    import db
    db.init_db()
    logger.info("=== Тестовая публикация ===")
    post_batch()
    print("✅ Батч опубликован")


def cmd_run(config):
    from scheduler import start
    logger.info("=== Запуск планировщика ===")
    start(config)


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
    else:
        print(f"Неизвестная команда: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

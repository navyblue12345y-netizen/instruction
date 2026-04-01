"""
Планировщик публикаций: 8:00–22:00 МСК, каждые 2 часа (1 пост на канал).
Ночной fetch в 23:00 МСК — чистит очередь и набирает 8 свежих постов на канал.
"""
import json
import logging
import os
import sqlite3

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from publisher.max_api import MaxPublisher

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Выбор поста из очереди ─────────────────────────────────────────────────

def _pick_post(niche: str) -> dict | None:
    """
    Выбирает следующий пост из очереди:
    - каждый 3-й по счёту — видео (если есть)
    - приоритет фото > видео > текст
    - пропускает дубли по тексту
    """
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Мода и Дом — только видео
    video_only = niche in ("moda", "dom")
    media_filter = "AND media_type='video'" if video_only else ""

    rows = conn.execute(
        f"SELECT * FROM posts WHERE channel=? AND status='pending' {media_filter}"
        " ORDER BY RANDOM() LIMIT 20",
        (niche,),
    ).fetchall()

    result = None
    for row in rows:
        p = dict(row)
        snippet = (p.get("rewritten_text") or "")[:80]
        if snippet:
            dup = conn.execute(
                "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                " AND rewritten_text LIKE ? LIMIT 1",
                (niche, f"{snippet}%"),
            ).fetchone()
            if dup:
                continue
        result = p
        break

    conn.close()
    return result


# ── Публикация батча ────────────────────────────────────────────────────────

def post_batch():
    """Публикует 1 пост на каждый канал из очереди."""
    config = load_config()
    publisher = MaxPublisher(config["max"]["token"])

    for niche, channel_id in config["max"]["channels"].items():
        published = False
        for _ in range(5):
            post = _pick_post(niche)
            if not post:
                logger.info(f"[{niche}] Очередь пуста — пропускаем слот")
                break

            media_url = post.get("media_url")
            media_type = post.get("media_type")
            media_files_raw = post.get("media_files")
            media_files = json.loads(media_files_raw) if media_files_raw else None
            text = (post.get("rewritten_text") or "").strip()
            video_only = niche in ("moda", "dom")

            # Если медиафайл пропал с диска — пропускаем пост для всех каналов
            if media_url and not os.path.exists(media_url):
                logger.warning(f"[{niche}] Медиафайл не найден: {media_url}")
                db.mark_skipped(post["id"])
                continue

            # Пост без медиа не публикуем ни в один канал
            if not media_url:
                db.mark_skipped(post["id"])
                continue

            # Сохраняем оригинальные пути ДО публикации для последующей очистки
            orig_media_url = media_url
            orig_media_files = media_files

            success = publisher.post(
                channel_id=int(channel_id),
                text=text,
                media_url=media_url,
                media_type=media_type,
                media_files=media_files,
            )

            if success:
                db.mark_posted(post["id"])
                # Удаляем файлы используя оригинальные пути (до возможного обнуления)
                _cleanup_media(orig_media_url, orig_media_files)
                logger.info(f"[{niche}] ✅ Пост #{post['id']} (@{post['source_id']}) опубликован")
                published = True
                break
            else:
                db.mark_skipped(post["id"])
                logger.warning(f"[{niche}] ❌ Пост #{post['id']} не прошёл, пробуем следующий")

        if not published:
            logger.warning(f"[{niche}] Все попытки исчерпаны, слот пропущен")


# ── Очистка медиа ──────────────────────────────────────────────────────────

def _cleanup_media(media_url: str | None, media_files: list | None):
    """Удаляет локальные медиафайлы после публикации."""
    files_to_delete = set()
    if media_url and os.path.exists(media_url):
        files_to_delete.add(media_url)
    if media_files:
        for f in media_files:
            if f and os.path.exists(f):
                files_to_delete.add(f)
    for f in files_to_delete:
        try:
            os.remove(f)
        except Exception as e:
            logger.debug(f"Не удалось удалить {f}: {e}")


# ── Ночной fetch ────────────────────────────────────────────────────────────

def nightly_fetch():
    """Чистит очередь и набирает 8 свежих постов на каждый канал."""
    logger.info("Ночной fetch запущен...")
    config = load_config()
    from fetcher import fetch_all
    total = fetch_all(config)
    logger.info(f"Ночной fetch завершён: добавлено {total} постов")


# ── Запуск ──────────────────────────────────────────────────────────────────

def start(config: dict):
    db.init_db()
    scheduler = BlockingScheduler(timezone="Europe/Moscow")

    sched = config.get("schedule", {})
    tz = sched.get("timezone", "Europe/Moscow")
    start_h = sched.get("start_hour", 8)
    end_h = sched.get("end_hour", 22)
    interval = sched.get("interval_hours", 2)

    hours_str = ",".join(str(h) for h in range(start_h, end_h + 1, interval))

    scheduler.add_job(
        post_batch,
        CronTrigger(hour=hours_str, minute=0, timezone=tz),
        id="post_batch",
        name="Публикация постов",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        nightly_fetch,
        CronTrigger(hour=23, minute=0, timezone=tz),
        id="nightly_fetch",
        name="Ночной сбор контента",
        misfire_grace_time=600,
    )

    logger.info(f"Планировщик запущен. Публикации в {hours_str}:00 МСК")
    scheduler.start()

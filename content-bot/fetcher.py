"""
Сбор контента из Telegram → БД.
Медиа скачивается в telegram.py сразу при парсинге.
"""
import logging
import os
import re
import sqlite3

import db
from processor.filter import should_skip
from sources import telegram

logger = logging.getLogger(__name__)

POSTS_PER_NICHE = 8  # постов на канал в день

def _get_groq_client(config: dict):
    """Создаёт Groq клиент из конфига. Каждый раз свежий — чтобы не кэшировать сбои."""
    key = config.get("groq", {}).get("api_key", "")
    if not key:
        return None
    try:
        from groq import Groq
        return Groq(api_key=key)
    except Exception as e:
        logger.warning(f"Не удалось инициализировать Groq: {e}")
        return None


def _clean(text: str) -> str:
    """Базовая очистка без AI."""
    if not text:
        return ""
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[•·]\s*Подписат[а-я]+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'Подписат[а-я]+\s+в\s+\S+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'Подпиш[а-я]+\s+на\s+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'^\s*Смотрет[ьь]\s*$', '', text, flags=re.MULTILINE)
    lines = [l for l in text.split('\n') if sum(c.isalpha() for c in l) >= 3 or not l.strip()]
    text = '\n'.join(lines)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch_all(config: dict) -> int:
    db.init_db()
    groq_client = _get_groq_client(config)
    sources_cfg = config.get("sources", {})
    added_total = 0

    for niche in ["moda", "vyazanie", "dom"]:
        # Чистим старую очередь
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DELETE FROM posts WHERE channel=? AND status='pending'", (niche,))
        conn.commit()
        conn.close()

        added = 0
        channels = sources_cfg.get(niche, {}).get("telegram", [])
        video_only = niche in ("moda", "dom")
        i = 0

        while added < POSTS_PER_NICHE and i < len(channels) * 5:
            channel = channels[i % len(channels)]
            pages = 1 + (i // len(channels))  # глубже при нехватке
            i += 1
            try:
                posts = telegram.fetch_channel(channel, max_posts=20, max_pages=pages)
                for p in posts:
                    if added >= POSTS_PER_NICHE:
                        break
                    if video_only and p.get("media_type") != "video":
                        continue
                    added += _save(p, channel, niche, groq_client)
            except Exception as e:
                logger.error(f"[{niche}] @{channel}: {e}")

        logger.info(f"[{niche}] +{added}/{POSTS_PER_NICHE}")
        added_total += added

    return added_total


def _save(post: dict, source_id: str, channel: str, groq_client=None) -> int:
    url = post.get("source_url", "")
    orig = post.get("text", "") or ""
    media_url = post.get("media_url")
    media_type = post.get("media_type")

    if url and db.is_seen(url):
        return 0

    # Фильтр по оригиналу — чтобы не пропустить скрытую рекламу
    if should_skip(orig, media_url):
        if url:
            db.add_seen(url)
        return 0

    clean = _clean(orig)

    if not clean.strip() and not media_url:
        if url:
            db.add_seen(url)
        return 0

    # Рерайт через Groq или fallback
    rewritten = _rewrite(clean, channel, media_type, groq_client)

    media_files = post.get("media_files", [])
    db.add_post("telegram", source_id, orig, rewritten, media_url, media_type, channel,
                media_files=media_files)
    if url:
        db.add_seen(url)
    return 1


def _rewrite(text: str, channel: str, media_type: str = None, groq_client=None) -> str:
    """Рерайт через Groq. При недоступности — fallback."""
    if groq_client is None:
        from processor.rewriter import _fallback_caption
        return _fallback_caption(text, channel)
    try:
        from processor.rewriter import rewrite
        return rewrite(text, channel, groq_client, media_type=media_type)
    except Exception as e:
        logger.warning(f"Groq недоступен: {e}")
        from processor.rewriter import _fallback_caption
        return _fallback_caption(text, channel)

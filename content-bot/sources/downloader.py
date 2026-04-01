"""
Скачивание медиафайлов сразу при fetch — до того как ссылки истекут.
"""
import os
import hashlib
import httpx
import logging

logger = logging.getLogger(__name__)

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media", "cache")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def download_media(url: str, media_type: str = "photo") -> str | None:
    """
    Скачивает медиафайл и сохраняет в media/cache/.
    Возвращает локальный путь или None при ошибке.
    """
    if not url or not url.startswith("http"):
        return None

    os.makedirs(MEDIA_DIR, exist_ok=True)

    # Имя файла по хэшу URL
    url_hash = hashlib.md5(url.encode()).hexdigest()
    ext = ".mp4" if media_type == "video" else ".jpg"
    local_path = os.path.join(MEDIA_DIR, f"{url_hash}{ext}")

    # Уже скачан
    if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
        return local_path

    try:
        r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            logger.debug(f"Медиа недоступно HTTP {r.status_code}: {url[:50]}")
            return None
        if len(r.content) < 1000:
            logger.debug(f"Медиа слишком мало ({len(r.content)} байт): {url[:50]}")
            return None

        with open(local_path, "wb") as f:
            f.write(r.content)

        logger.debug(f"Скачано {len(r.content)//1024}KB: {local_path}")
        return local_path

    except Exception as e:
        logger.debug(f"Ошибка скачивания {url[:50]}: {e}")
        return None


def cleanup_old_files(max_age_hours: int = 72):
    """Удаляет файлы старше max_age_hours часов."""
    import time
    if not os.path.exists(MEDIA_DIR):
        return
    now = time.time()
    removed = 0
    for fname in os.listdir(MEDIA_DIR):
        fpath = os.path.join(MEDIA_DIR, fname)
        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_hours * 3600:
            os.remove(fpath)
            removed += 1
    if removed:
        logger.info(f"Очищено {removed} устаревших медиафайлов")

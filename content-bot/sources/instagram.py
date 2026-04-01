"""
Парсинг Instagram через yt-dlp (публичные профили, без авторизации).
"""
import os
import logging
import yt_dlp

logger = logging.getLogger(__name__)

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media")


def fetch_profile(username: str, max_posts: int = 5) -> list[dict]:
    """
    Скачивает последние посты/reels с публичного Instagram-профиля.
    Возвращает список: [{source_url, text, media_url, media_type}]
    """
    os.makedirs(MEDIA_DIR, exist_ok=True)
    results = []
    profile_url = f"https://www.instagram.com/{username}/"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "playlistend": max_posts,
        "outtmpl": os.path.join(MEDIA_DIR, "%(uploader)s_%(id)s.%(ext)s"),
        "format": "mp4/best[ext=mp4]/best",
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "ignoreerrors": True,
        "geo_bypass": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(profile_url, download=True)
            if not info:
                logger.warning(f"Instagram: нет данных для @{username}")
                return []

            entries = info.get("entries", [info])
            for entry in entries:
                if not entry:
                    continue
                post_url = entry.get("webpage_url") or entry.get("url", "")
                text = entry.get("description") or entry.get("title") or ""
                # Путь к скачанному файлу
                local_path = ydl.prepare_filename(entry)
                if not os.path.exists(local_path):
                    # Попробуем найти файл с другим расширением
                    base = os.path.splitext(local_path)[0]
                    for ext in [".mp4", ".jpg", ".jpeg", ".png", ".webm"]:
                        if os.path.exists(base + ext):
                            local_path = base + ext
                            break

                media_type = "video" if local_path.endswith((".mp4", ".webm", ".mov")) else "photo"

                results.append({
                    "source_url": post_url,
                    "text": text,
                    "media_url": local_path if os.path.exists(local_path) else None,
                    "media_type": media_type,
                })

        logger.info(f"Instagram @{username}: получено {len(results)} постов")
    except Exception as e:
        logger.error(f"Instagram @{username} ошибка: {e}")

    return results

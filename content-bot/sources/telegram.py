"""
Парсинг публичных Telegram-каналов через t.me/s/.
Медиа скачивается СРАЗУ при парсинге — ссылки живут минуты.
"""
import hashlib
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "media", "cache")

# Паттерны хвостов для очистки текста
_TAIL_PATTERNS = [
    r'[Яя]бнадела.*',
    r'Подписат[а-я]+\s+[👉→▶️»]+.*',
    r'📍\s*Если не грузится.*',
    r'Если не грузится фото.*',
    r'[Нн]аш (резервный )?канал.*',
    r'смотрите в\s*MAX.*',
]


def _download_now(url: str, ext: str) -> str | None:
    """Скачивает медиа немедленно, возвращает локальный путь или None."""
    if not url:
        return None
    os.makedirs(MEDIA_DIR, exist_ok=True)
    local = os.path.join(MEDIA_DIR, hashlib.md5(url.encode()).hexdigest() + ext)
    if os.path.exists(local) and os.path.getsize(local) > 1000:
        return local
    try:
        r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 1000:
            with open(local, "wb") as f:
                f.write(r.content)
            return local
    except Exception as e:
        logger.debug(f"Ошибка скачивания {url[:50]}: {e}")
    return None


def _clean_text(text_el) -> str:
    """Извлекает и чистит текст из элемента сообщения."""
    for a in text_el.select("a"):
        href = a.get("href", "")
        if "t.me/+" in href or "t.me/joinchat" in href:
            a.decompose()
        else:
            a.replace_with(a.get_text().strip())

    text = text_el.get_text(separator="\n", strip=True)
    text = re.sub(r"[᛫ᛈᛊᚹᛚᛟƃᚣᛠ]+", " ", text)
    for pattern in _TAIL_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _parse_msg(msg) -> dict | None:
    """Парсит одно сообщение → dict или None если пустое."""
    url_el = msg.select_one("a.tgme_widget_message_date")
    post_url = url_el.get("href", "") if url_el else ""

    text_el = msg.select_one("div.tgme_widget_message_text")
    text = _clean_text(text_el) if text_el else ""

    # Фото (альбом)
    media_files = []
    media_type = None
    for photo_el in msg.select("a.tgme_widget_message_photo_wrap"):
        m = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", photo_el.get("style", ""))
        if m:
            local = _download_now(m.group(1), ".jpg")
            if local:
                media_files.append(local)
    if media_files:
        media_type = "photo"

    # Видео (если нет фото)
    if not media_files:
        seen: set = set()
        for video_el in msg.select("video[src]"):
            src = video_el.get("src")
            if src and src not in seen:
                seen.add(src)
                local = _download_now(src, ".mp4")
                if local:
                    media_files.append(local)
                    media_type = "video"
                    break

    if not text and not media_files:
        return None

    return {
        "source_url": post_url,
        "text": text,
        "media_url": media_files[0] if media_files else None,
        "media_files": media_files,
        "media_type": media_type,
    }


def fetch_channel(channel: str, max_posts: int = 10, max_pages: int = 1) -> list[dict]:
    """
    Парсит канал, при необходимости листает историю назад.
    max_pages — сколько страниц (каждая ~20 постов).
    """
    from bs4 import BeautifulSoup

    base_url = f"https://t.me/s/{channel}"
    results: list[dict] = []
    before_id: int | None = None

    try:
        for _ in range(max_pages):
            url = f"{base_url}?before={before_id}" if before_id else base_url
            resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            messages = soup.select("div.tgme_widget_message")
            if not messages:
                break

            # ID для следующей страницы
            ids = []
            for m in messages:
                dp = m.get("data-post", "")
                try:
                    ids.append(int(dp.split("/")[-1]))
                except (ValueError, IndexError):
                    pass
            if ids:
                before_id = min(ids)

            for msg in messages:
                post = _parse_msg(msg)
                if post:
                    results.append(post)

        logger.info(f"TG @{channel}: {len(results)} постов ({max_pages} стр.)")

    except Exception as e:
        logger.error(f"TG @{channel}: {e}")

    return results

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
    r'оперативно\s+о\s+чп.*',
    r'в\s+наших\s+профильных\s+каналах.*',
    r'подпис(ывай|аться|ка).*',
    r'написать\s+нам.*',
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


_AD_LINK_RE = re.compile(r"[?&]erid=[A-Za-z0-9]{6,}", re.IGNORECASE)


_ARTICLE_LINK_SKIP = (
    "t.me/", "telegram.me", "tg://", "max.ru/", "vk.com/", "ok.ru/",
    "instagram.com", "youtube.com", "youtu.be", "dzen.ru/id", "wa.me/",
    "viber", "whatsapp", "zen.yandex",
)


def extract_article_link(html):
    """Ссылка на полную статью в теле telegram-поста (cases-2026-08-19).

    Зачем: _clean_text вырезает ссылки как рекламные хвосты. 19.08 сызранский
    пост оказался тизером на 105 символов, хотя в нём была ссылка на полный
    материал ktv-ray.ru. Соцсети и мессенджеры не считаем статьёй.
    """
    if not html:
        return None
    import re as _re
    for m in _re.finditer(r'href="(https?://[^"]+)"', str(html)):
        url = m.group(1).strip()
        low = url.lower()
        if any(s in low for s in _ARTICLE_LINK_SKIP):
            continue
        return url
    return None


def _clean_text(text_el) -> str:
    """Извлекает и чистит текст из элемента сообщения."""
    # 1) Удаляем хвостовые скрытые/кнопочные ссылки (text_link-аналог в HTML)
    # Если ссылка короткая/брендовая и стоит в конце сообщения — удаляем вместе с текстом.
    links = text_el.select("a[href]")
    for a in links:
        href = (a.get("href") or "").strip().lower()
        label = (a.get_text(" ", strip=True) or "").strip()
        is_invite = ("t.me/+" in href) or ("t.me/joinchat" in href)
        is_hidden_tail = bool(href) and (len(label) <= 40) and (
            re.search(r"подпис|написать|чп|срочно|канал|boost|чат", label, flags=re.IGNORECASE)
            or href.startswith("http")
            or href.startswith("tg://")
            or "t.me/" in href
        )
        if is_invite or is_hidden_tail:
            a.decompose()
        else:
            a.replace_with(label)

    text = text_el.get_text(separator="\n", strip=True)
    text = re.sub(r"[᛫ᛈᛊᚹᛚᛟƃᚣᛠ]+", " ", text)

    # 2) Удаляем CTA/бренд-хвосты по строкам с конца
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    while lines:
        tail = lines[-1]
        # Удаляем только явные CTA/ссылочные хвосты, не срезаем короткие содержательные строки
        if re.search(r"подпис|написать\s+нам|канал|t\.me|http|joinchat|boost", tail, flags=re.IGNORECASE):
            lines.pop()
            continue
        break
    text = "\n".join(lines)

    for pattern in _TAIL_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _parse_msg(msg, download_media: bool = False) -> dict | None:
    """Парсит одно сообщение → dict или None если пустое.

    download_media=False (по умолчанию, для realtime engine Лайв-сетки):
        media_files содержит cdn-telegram URL'ы — publisher.upload_file качает сам.
        Минус: URL живут 1-2 часа.

    download_media=True (для legacy fetcher Дача/Контент сетки):
        Скачиваем медиа сразу через _download_now → сохраняем локальные пути.
        Минус: блокирует fetch (30s timeout на медиа), но локальные файлы живут вечно
        и pending в БД не протухает.
    """
    url_el = msg.select_one("a.tgme_widget_message_date")
    post_url = url_el.get("href", "") if url_el else ""

    # Время публикации поста
    pub_time = None
    time_el = msg.select_one("time[datetime]")
    if time_el:
        try:
            from datetime import datetime, timezone
            pub_time = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
        except Exception:
            pub_time = None

    text_el = msg.select_one("div.tgme_widget_message_text")
    _raw_hrefs = [(a.get("href") or "") for a in text_el.select("a[href]")] if text_el else []
    text = _clean_text(text_el) if text_el else ""

    # Inline кнопки с URL (сигнал рекламы)
    inline_url_buttons = []
    for a in msg.select("div.tgme_widget_message_inline_keyboard a[href]"):
        href = (a.get("href") or "").strip()
        if href.startswith("http") or href.startswith("tg://") or href.startswith("t.me/"):
            inline_url_buttons.append(href)

    # Ad-гейт: erid в ссылке = реклама (по 38-ФЗ erid бывает ТОЛЬКО в рекламе) -> отбрасываем пост
    if _AD_LINK_RE.search(" ".join(_raw_hrefs + inline_url_buttons)):
        return None

    # Фото (альбом)
    media_files = []
    media_type = None
    for photo_el in msg.select("a.tgme_widget_message_photo_wrap"):
        m = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", photo_el.get("style", ""))
        if m:
            url = m.group(1)
            if download_media:
                local = _download_now(url, ".jpg")
                if local:
                    media_files.append(local)
            else:
                media_files.append(url)
    if media_files:
        media_type = "photo"

    # Видео (если нет фото). 2026-08-07: альбом собираем ЦЕЛИКОМ (кап 3),
    # раньше стоял break после первого — «Дачники» вышли с текстом «на втором
    # видео поподробнее» и одним видео. Кап: видео тяжёлые (10-50 МБ, okcdn
    # медленный) — гигабайтные подборки не тянем.
    VIDEO_ALBUM_MAX = 3
    if not media_files:
        seen: set = set()
        for video_el in msg.select("video[src]"):
            if len(media_files) >= VIDEO_ALBUM_MAX:
                break
            src = video_el.get("src")
            if src and src not in seen:
                seen.add(src)
                if download_media:
                    local = _download_now(src, ".mp4")
                    if local:
                        media_files.append(local)
                        media_type = "video"
                else:
                    media_files.append(src)
                    media_type = "video"

    if not text and not media_files:
        return None

    return {
        "source_url": post_url,
        "text": text,
        "media_url": media_files[0] if media_files else None,
        "media_files": media_files,
        "media_type": media_type,
        "pub_time": pub_time,
        "has_inline_url_buttons": bool(inline_url_buttons),
        "inline_url_buttons": inline_url_buttons,
    }


# P0 fix (2026-05-04): общий httpx.Client с явными limits.
# Раньше каждый httpx.get создавал implicit client с pool_size=1 per host.
# При параллельных запросах из engine (36 workers, gather 8+ источников)
# это давало connection pool exhaustion → TimeoutError.
# Один Client расшарен между engine (через asyncio.to_thread) и legacy fetcher
# (sync). Используется ТОЛЬКО для t.me — web идёт через свой стек (trafilatura).
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_HTTP_CLIENT = httpx.Client(
    limits=_HTTP_LIMITS,
    timeout=_HTTP_TIMEOUT,
    headers=HEADERS,
    follow_redirects=True,
)


def fetch_channel(channel: str, max_posts: int = 10, max_pages: int = 1,
                  download_media: bool = False) -> list[dict]:
    """
    Парсит канал, при необходимости листает историю назад.
    max_pages — сколько страниц (каждая ~20 постов).

    download_media:
      False (default) — медиа возвращается как cdn-telegram URL (lazy, для Лайв-engine).
      True — медиа скачивается локально (для legacy fetcher Дача/Вязание/Контент,
              где между fetch и публикацией могут пройти часы).
    """
    from bs4 import BeautifulSoup

    base_url = f"https://telegram.me/s/{channel}"
    results: list[dict] = []
    seen_urls: set[str] = set()  # in-run dedup: t.me/s/?before= иногда отдаёт overlap страниц
    dupes_skipped = 0
    before_id: int | None = None

    try:
        for _ in range(max_pages):
            url = f"{base_url}?before={before_id}" if before_id else base_url
            resp = _HTTP_CLIENT.get(url)
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
                post = _parse_msg(msg, download_media=download_media)
                if not post:
                    continue
                # Защита от overlap пагинации: один и тот же source_url не должен
                # вернуться дважды из одного fetch_channel — иначе fetcher._save
                # пройдёт is_seen дважды (ещё не успел add_seen) и создаст дубль.
                src_url = post.get("source_url") or ""
                if src_url and src_url in seen_urls:
                    dupes_skipped += 1
                    continue
                if src_url:
                    seen_urls.add(src_url)
                results.append(post)

        if dupes_skipped:
            logger.info(f"TG @{channel}: {len(results)} постов ({max_pages} стр.), пропущено {dupes_skipped} overlap-дублей")
        else:
            logger.info(f"TG @{channel}: {len(results)} постов ({max_pages} стр.)")

    except Exception as e:
        logger.error(f"TG @{channel}: {e}")

    return results

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
    # 16.09: гиперссылки из текста РАНЬШЕ выбрасывались (использовались только
    # для ad-гейта) — тизеры теряли ссылку на полную статью (Рязань ya62),
    # подборки — ссылки на первоисточники (Чита). Сохраняем пары label+url.
    text_links = []
    if text_el:
        for _a in text_el.select("a[href]"):
            _h = (_a.get("href") or "").strip()
            if _h.startswith("http"):
                text_links.append({"label": _a.get_text(" ", strip=True)[:80], "url": _h})
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

    # 16.09 (Ростов etorostov/135426): крупное видео web-превью отдаёт как
    # «толстый плеер» БЕЗ <video src> — раньше пост выходил голым текстом.
    # Помечаем: юзербот-дозаборщик скачает файл через MTProto.
    has_video_player = bool(
        msg.select(".tgme_widget_message_video_player, i.tgme_widget_message_video_thumb")
    ) and not msg.select("video[src]")

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
        "text_links": text_links,
        "has_video_player": has_video_player and not media_files,
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


# ── 16.09: юзербот-дозаборщик вложений (общая tg-сессия с chef-форком) ──────
_UB_SESSION_DEFAULT = "/home/openclaw/.openclaw/workspace/tg_userbot/ikigai_userbot"
_UB_CHEF_ENV = "/home/openclaw/.openclaw/workspace/content-bot-client-chef/.env"
_UB_FLOCK_TIMEOUT_SEC = 120
_UB_MAX_FILE_BYTES = 48 * 1024 * 1024        # аудио: лимит MAX на вложение
_UB_MAX_VIDEO_BYTES = 80 * 1024 * 1024       # видео: качаем крупнее — при заливке
                                             # публикатор сожмёт ffmpeg'ом (max_api._shrink)
_AUDIO_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
              "audio/x-m4a": ".m4a", "audio/mp4": ".m4a", "audio/flac": ".flac",
              "audio/x-wav": ".wav"}


def _ub_session_path() -> str:
    return os.environ.get("TG_USERBOT_SESSION", _UB_SESSION_DEFAULT)


def _ub_env(name: str) -> str:
    """env процесса, а если пусто — .env chef-форка (общий юзербот)."""
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        for line in open(_UB_CHEF_ENV, encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _flock_or_timeout(fh, timeout_sec: int):
    import fcntl, time as _t
    deadline = _t.monotonic() + timeout_sec
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if _t.monotonic() >= deadline:
                raise TimeoutError("userbot session flock busy")
            _t.sleep(1.0)


def userbot_enrich_media(channel: str, posts: list, want_audio: bool = False,
                         max_downloads: int = 3, max_age_hours: float = 48.0,
                         client_factory=None) -> int:
    """Батч-дозабор вложений через tg-юзербот (ОДИН get_messages на канал).

    Кому: посты с has_video_player=True (крупное видео, web-превью отдало
    плеер без src — Ростов 16.09) и, при want_audio, посты вовсе без медиа
    (аудио web-превью не отдаёт никак — Благовещенск/песни 16.09).
    Скачивает файл ≤48 МБ в кэш, проставляет media_url/media_files/media_type.
    Пост с плеером, который дозабрать не вышло, помечается media_fetch_failed
    (вызывающий не должен постить его голым). Fail-open: без юзербота — 0."""
    from datetime import datetime, timezone, timedelta

    todo = {}
    now = datetime.now(timezone.utc)
    for p in posts or []:
        try:
            mid = int(str(p.get("source_url", "")).rstrip("/").rsplit("/", 1)[-1])
        except Exception:
            continue
        pt = p.get("pub_time")
        if pt is not None and (now - pt) > timedelta(hours=max_age_hours):
            continue
        if p.get("has_video_player"):
            todo[mid] = (p, "video")
        elif want_audio and not p.get("media_files") and (p.get("text") or "").strip():
            todo[mid] = (p, "audio?")
    if not todo:
        return 0

    api_id = _ub_env("TG_USERBOT_API_ID")
    api_hash = _ub_env("TG_USERBOT_API_HASH")
    if not api_id or not api_hash:
        logger.debug("[ub-enrich] нет TG_USERBOT_API_ID/HASH — пропуск")
        return 0

    os.makedirs(MEDIA_DIR, exist_ok=True)
    enriched = 0
    try:
        import fcntl  # noqa: F401 — гарантируем POSIX
        _lk = open(_ub_session_path() + ".flock", "w")
        try:
            _flock_or_timeout(_lk, _UB_FLOCK_TIMEOUT_SEC)
        except TimeoutError:
            _lk.close()
            logger.warning("[ub-enrich] @%s: сессия юзербота занята — пропуск", channel)
            return 0
        try:
            if client_factory is not None:
                client = client_factory()
            else:
                from telethon.sync import TelegramClient
                client = TelegramClient(_ub_session_path(), int(api_id), api_hash,
                                        receive_updates=False)
            with client:
                msgs = client.get_messages(channel, ids=sorted(todo.keys()))
                for m in msgs or []:
                    if m is None or getattr(m, "id", None) not in todo:
                        continue
                    p, kind = todo[m.id]
                    doc = getattr(m, "document", None)
                    video = getattr(m, "video", None)
                    audio = getattr(m, "audio", None) or getattr(m, "voice", None)
                    target, ext, mtype = None, None, None
                    _mime = ((getattr(doc, "mime_type", "") or "")).lower()
                    _is_vid = video is not None or _mime.startswith("video/")
                    if kind == "video" and (_is_vid or doc is not None):
                        size = getattr(video or doc, "size", 0) or 0
                        if size and size > _UB_MAX_VIDEO_BYTES:
                            logger.info("[ub-enrich] @%s/%s: видео %d МБ > лимита — пропуск",
                                        channel, m.id, size // 1048576)
                            p["media_fetch_failed"] = True
                            continue
                        target, ext, mtype = m, ".mp4", "video"
                    elif kind == "audio?" and audio is not None:
                        size = getattr(audio, "size", 0) or 0
                        if size and size > _UB_MAX_FILE_BYTES:
                            continue
                        mime = (getattr(audio, "mime_type", "") or "").lower()
                        target, ext, mtype = m, _AUDIO_EXT.get(mime, ".mp3"), "audio"
                    elif kind == "audio?" and _is_vid:
                        # Благовещенск 16.09: «песни» оказались альбомом ВИДЕО
                        # (video/mp4 61 МБ) — web-превью его вовсе не показало.
                        size = getattr(video or doc, "size", 0) or 0
                        if size and size > _UB_MAX_VIDEO_BYTES:
                            continue
                        target, ext, mtype = m, ".mp4", "video"
                    if target is None:
                        if kind == "video":
                            p["media_fetch_failed"] = True
                        continue
                    if enriched >= max_downloads:
                        break
                    path = os.path.join(MEDIA_DIR, "ubfix_%s_%s%s" % (channel, m.id, ext))
                    if not (os.path.exists(path) and os.path.getsize(path) > 1000):
                        try:
                            client.download_media(target, file=path)
                        except Exception as _de:
                            logger.warning("[ub-enrich] @%s/%s: скачивание не удалось: %s",
                                           channel, m.id, _de)
                            if kind == "video":
                                p["media_fetch_failed"] = True
                            continue
                    if not (os.path.exists(path) and os.path.getsize(path) > 1000):
                        if kind == "video":
                            p["media_fetch_failed"] = True
                        continue
                    p["media_files"] = [path]
                    p["media_url"] = path
                    p["media_type"] = mtype
                    p.pop("media_fetch_failed", None)
                    enriched += 1
                    logger.info("[ub-enrich] @%s/%s: дозабрал %s (%d КБ)",
                                channel, m.id, mtype, os.path.getsize(path) // 1024)
        finally:
            try:
                import fcntl as _f
                _f.flock(_lk, _f.LOCK_UN)
            except Exception:
                pass
            _lk.close()
    except Exception as e:
        logger.warning("[ub-enrich] @%s: fail-open: %s", channel, e)
    return enriched

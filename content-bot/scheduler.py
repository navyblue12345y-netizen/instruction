"""
Планировщик публикаций — мультисеточная архитектура.

Каждая сетка имеет своё расписание, количество постов в день и настройки.
Настройки сетки читаются из config.yaml → grid_settings.<Название сетки>:
  - start_hour: int (default: 8)
  - end_hour: int (default: 22)
  - interval_hours: int (default: 2)
  - posts_per_day: int (default: 8)
  - fetch_hour: int (default: 23) — когда собирать контент
  - timezone: str (default: "Europe/Moscow")
  - media_type: "any" | "photo" | "video" | "text" (default: "any")

Глобальное расписание из config.yaml → schedule используется как fallback
если у сетки нет своих настроек.
"""
import json
import logging
import os
import random
import re
import sqlite3
import time
import threading
from urllib.parse import quote

import httpx

from utils import load_config, grid_settings as _grid_settings_util
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from publisher.max_api import MaxPublisher

logger = logging.getLogger(__name__)





# ── Настройки сетки ────────────────────────────────────────────────────────



def _hours_str(start_h: int, end_h: int, interval: int) -> str:
    return ",".join(str(h) for h in range(start_h, end_h + 1, interval))




# ── Выбор поста из очереди ─────────────────────────────────────────────────

def _pick_post(niche: str, media_type_filter: str = "any") -> dict | None:
    """
    Выбирает следующий пост из очереди.
    media_type_filter: "any" | "photo" | "video" | "text"
    """
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row

    if media_type_filter == "video":
        media_filter = "AND media_type='video'"
    elif media_type_filter == "photo":
        media_filter = "AND media_type='photo'"
    elif media_type_filter == "text":
        media_filter = "AND (media_type IS NULL OR media_type='')"
    else:
        media_filter = ""

    rows = conn.execute(
        f"SELECT * FROM posts WHERE channel=? AND status='pending' {media_filter}"
        " ORDER BY RANDOM() LIMIT 20",
        (niche,),
    ).fetchall()

    result = None
    for row in rows:
        p = dict(row)

        # 2026-04-29: усиленный дедуп. Раньше была только проверка по LIKE первых
        # 80 chars rewritten_text — пропускала случай, когда fetcher плодил пары
        # записей в posts с одинаковым text_hash за 1-3 миллисекунды (race),
        # обе попадали в pending, обе публиковались как "разные" id. Теперь:
        # 1) сравниваем по text_hash в posts (точное совпадение контента)
        # 2) и в published_history за последние 7 дней (фоллбек)
        text_hash = p.get("text_hash") or ""
        url_hash = p.get("url_hash") or ""
        is_dup = False

        if text_hash:
            dup = conn.execute(
                "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                " AND text_hash=? AND id != ? LIMIT 1",
                (niche, text_hash, p.get("id")),
            ).fetchone()
            if dup:
                is_dup = True

        if not is_dup and url_hash:
            dup = conn.execute(
                "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                " AND url_hash=? AND id != ? LIMIT 1",
                (niche, url_hash, p.get("id")),
            ).fetchone()
            if dup:
                is_dup = True

        # Фоллбек на published_history (на случай если posts.status был обновлён)
        if not is_dup and text_hash:
            dup = conn.execute(
                "SELECT 1 FROM published_history WHERE channel=?"
                " AND text_hash=? AND seen_at > datetime('now', '-7 days')"
                " LIMIT 1",
                (niche, text_hash),
            ).fetchone()
            if dup:
                is_dup = True

        # Старая LIKE-проверка (на случай постов без text_hash)
        if not is_dup:
            snippet = (p.get("rewritten_text") or "")[:80]
            if snippet:
                dup = conn.execute(
                    "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                    " AND rewritten_text LIKE ? AND id != ? LIMIT 1",
                    (niche, f"{snippet}%", p.get("id")),
                ).fetchone()
                if dup:
                    is_dup = True

        if is_dup:
            # Помечаем как duplicate чтобы планировщик не возвращался к нему
            try:
                conn.execute(
                    "UPDATE posts SET status='duplicate', skip_reason='text_hash dup at publish time'"
                    " WHERE id=?",
                    (p.get("id"),),
                )
                conn.commit()
            except Exception:
                pass
            logger.info(
                f"[{niche}] Пост #{p.get('id')} помечен как duplicate "
                f"(text_hash совпал с уже опубликованным)"
            )
            continue

        result = p
        break

    conn.close()
    return result


# ── Очистка медиа ──────────────────────────────────────────────────────────

def _cleanup_media(media_url: str | None, media_files: list | None, post_id: int | None = None):
    """Удаляет файлы только если на них не ссылается другой pending/processing пост.

    Один и тот же source-пост может попасть в несколько строк posts (одинаковый
    источник в разных каналах сетки). Раньше публикация первого удаляла файл,
    а второй потом падал в media_file_missing.
    """
    candidates = set()
    if media_url and os.path.exists(media_url):
        candidates.add(media_url)
    if media_files:
        for f in media_files:
            if f and os.path.exists(f):
                candidates.add(f)
    if not candidates:
        return

    import sqlite3 as _sqlite3
    import json as _json
    conn = _sqlite3.connect(db.DB_PATH)
    in_use = set()
    try:
        if post_id is not None:
            rows = conn.execute(
                "SELECT media_url, media_files FROM posts "
                "WHERE status IN ('pending','processing') AND id != ?",
                (post_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT media_url, media_files FROM posts "
                "WHERE status IN ('pending','processing')"
            ).fetchall()
        for mu, mf in rows:
            if mu:
                in_use.add(mu)
            if mf:
                try:
                    for f in _json.loads(mf):
                        if f:
                            in_use.add(f)
                except Exception:
                    pass
    finally:
        conn.close()

    for f in candidates:
        if f in in_use:
            logger.info(f"Cleanup пропущен: {f} ещё нужен другому pending/processing посту")
            continue
        try:
            os.remove(f)
        except Exception as e:
            logger.debug(f"Не удалось удалить {f}: {e}")


_NBSP = " "


def protect_numbered_lists(text: str) -> str:
    """«1. текст» -> «1.<nbsp>текст» в начале строк (21.09).

    MAX переразбивает тело по «. » и вырывает номер в отдельный абзац —
    в «Дачном уголке» пост про базилик выехал ступенькой. Неразрывный
    пробел визуально идентичен и не меняет длину (markup не съедет).
    Трогаем ТОЛЬКО начало строки: «ст. л.» и «1 ст.» внутри текста целы."""
    import re as _re
    if not text:
        return text
    return _re.sub(r"(?m)^(\s{0,3}\d{1,2}[.)])[ 	]+(?=\S)",
                   lambda m: m.group(1) + _NBSP, text)


def _format_dacha_body(text: str) -> str:
    """Заголовок поста -> **жирный** + пустая строка перед телом (2026-07-26).

    Лайв/в MAX прогоняют текст через _to_markdown_with_bold_title, а legacy-путь
    Дача/Вязание публиковал как есть: 241 из 369 постов nash_dom за 30 дней шли
    с одинарным переносом — заголовок слипался с телом. Переиспользуем ту же
    функцию движка (единый вид сети). Fail-open: ошибка -> исходный текст."""
    if not text:
        return text
    try:
        from news_realtime_engine import _to_markdown_with_bold_title
        return protect_numbered_lists(_to_markdown_with_bold_title(text))
    except Exception:
        return protect_numbered_lists(text)


def _apply_channel_signature(niche: str, channel_id: int, text: str) -> tuple[str, list | None, str | None]:
    """Добавляет подпись канала в конце поста. Возвращает (final_text, markup, text_format).

    Режимы (config channel_settings.<niche>.signature.style):
      markdown  — КЛИКАБЕЛЬНЫЙ текст-ссылка [emoji label](url) + text_format=markdown.
                  MAX сам парсит markdown-ссылку в link-markup (offset в UTF-16 —
                  эмодзи в теле не ломают). Проверено на новостниках и ретро-Даче.
      url_line  — видимая подпись + URL отдельной строкой (голая ссылка, legacy default).
      markup    — экспериментальный ручной link-markup по label (offset = Python len,
                  ломается на эмодзи, т.к. MAX считает UTF-16). НЕ использовать.
    """
    try:
        cfg = load_config()
        chs = (cfg.get("channel_settings") or {}).get(niche, {}) or {}
        sig = (chs.get("signature") or {}) if isinstance(chs, dict) else {}
        if not sig or not sig.get("enabled"):
            return text, None, None

        emoji = (sig.get("emoji") or "").strip()
        label = (sig.get("label") or "").strip()
        if not label:
            return text, None, None

        # По умолчанию ведём на сам канал в MAX
        url = (sig.get("url") or "").strip()
        if not url:
            url = f"https://max.ru/c/{quote(str(channel_id), safe='-')}"

        suffix = f"{emoji} {label}".strip()
        base = (text or "").rstrip()
        style = (sig.get("style") or "url_line").strip().lower()

        # Кликабельная подпись: markdown-ссылка, MAX парсит её в link-markup сам
        # (offset в UTF-16 — эмодзи в теле не смещают ссылку, в отличие от ручного markup).
        if style == "markdown":
            final_text = f"{base}\n\n[{suffix}]({url})" if base else f"[{suffix}]({url})"
            return final_text, None, "markdown"

        # Legacy default: видимая подпись + URL на новой строке (голая ссылка, кликабельна как URL).
        if style == "url_line":
            final_text = f"{base}\n\n{suffix}\n{url}" if base else f"{suffix}\n{url}"
            return final_text, None, None

        # Экспериментальный ручной link-markup (баговый для эмодзи-текстов) — оставлен для совместимости.
        final_text = f"{base}\n\n{suffix}" if base else suffix
        start = len(final_text) - len(label)
        markup = [{"type": "link", "from": start, "length": len(label), "url": url}]
        return final_text, markup, None
    except Exception as e:
        logger.debug(f"[{niche}] signature apply skipped: {e}")
        return text, None, None


# ── Публикация одного канала ───────────────────────────────────────────────

def _current_slot_key() -> str:
    """Ключ текущего слота: YYYY-MM-DD HH:MM (UTC, округлено до 5 минут)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rounded = now.minute - (now.minute % 5)
    return f"{now.strftime('%Y-%m-%d %H:')}{ rounded:02d}"


def _load_telegram_bot_token() -> str:
    from utils import load_env
    load_env()
    return os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TG_ADMIN_BOT_TOKEN") or ""


def _telegram_file_url(file_id: str) -> str | None:
    token = _load_telegram_bot_token()
    if not token or not file_id:
        return None
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        if not data.get("ok"):
            return None
        path = ((data.get("result") or {}).get("file_path") or "").strip()
        if not path:
            return None
        return f"https://api.telegram.org/file/bot{token}/{path}"
    except Exception:
        return None


def _normalize_ad_text_for_max(text: str) -> str:
    """Prepare ad text for MAX, preserving markdown links when present."""
    if not text:
        return ""
    return text.strip()


def _publish_ad_if_due(channel_id: int, ad_row: dict, publisher: MaxPublisher) -> bool:
    """Публикует рекламный пост из ads_schedule с retry/backoff и atomic-claim."""
    ad_id = ad_row.get("id")
    if not ad_id:
        return False

    # Защита от двойной публикации (worker + slot checker)
    if not db.claim_ad(ad_id):
        return False

    text = _normalize_ad_text_for_max((ad_row.get("ad_text") or "").strip())

    # Parse optional markup (кнопки/ссылки)
    mk = (ad_row.get("markup") or "")
    markup_obj = None
    if mk:
        try:
            markup_obj = json.loads(mk) if isinstance(mk, str) else mk
        except Exception:
            markup_obj = None

    # Fallback: если в тексте нет URL, но в markup есть — добавим первый URL в текст,
    # чтобы ссылка не потерялась даже если MAX не отрендерит кнопки.
    has_text_url = bool(re.search(r"https?://\S+", text))
    if (not has_text_url) and mk:
        try:
            m = re.search(r"https?://[^\s\"']+", mk)
            if m:
                text = (text + "\n\n" + m.group(0)).strip() if text else m.group(0)
        except Exception:
            pass

    media_file_id = ad_row.get("media_file_id")

    # Валидация креатива: должен быть хотя бы текст или медиа
    if not text and not media_file_id:
        db.mark_ad_error(ad_id, "empty_ad_creative")
        return False

    # Try resolve Telegram file_id (AgAC...) -> downloadable URL
    media_ref = media_file_id
    if media_file_id and (not str(media_file_id).startswith("http")) and (not os.path.exists(str(media_file_id))):
        tg_url = _telegram_file_url(str(media_file_id))
        if tg_url:
            media_ref = tg_url
        else:
            db.mark_ad_error(ad_id, "unsupported_media_id_format")
            return False

    for attempt in range(1, 4):
        try:
            result = publisher.post_result(
                channel_id=channel_id,
                text=text,
                media_url=media_ref,
                media_type=None,
                media_files=[media_ref] if media_ref else None,
                markup=markup_obj,
                # FIX (2026-04-30): реклама приходит из ad-таблицы с фиксированным
                # форматом и часто без bold-заголовка. Auto-edit чтобы её не
                # портил — отключаем.
                auto_edit_check=False,
            )
            if result.get("ok"):
                db.mark_ad_published(ad_id, published_mid=result.get("mid"))
                return True
        except Exception as e:
            if attempt == 3:
                db.mark_ad_error(ad_id, str(e))
                return False

        # backoff + jitter
        time.sleep((0.8 * attempt) + random.random() * 0.6)

    db.mark_ad_error(ad_id, "publish_failed")
    return False


_RESCUE_LAST = {}                  # niche -> монотонное время последнего добора
_RESCUE_COOLDOWN_SEC = 600         # пустой донорский день не должен превращать
                                   # каждый слот в многоминутный фетч


def _upload_tokens_for(publisher, media_url, media_files, media_type):
    """Пред-заливка медиа в MAX ДО ad-скана. → список токенов.

    [] — текстовый пост (заливать нечего); None — залить не вышло или тип
    файла не совпал с media_type → отправлять старым путём (заливка внутри
    post_result). Альбом токенов в post_result идёт одним типом (image или
    video по media_type), поэтому смешанное/непонятное не рискуем."""
    files = [f for f in (media_files or []) if f] or ([media_url] if media_url else [])
    if not files:
        return []
    want = "video" if media_type == "video" else "image"
    toks = []
    for f in files:
        name = str(f).split("?")[0].lower()
        is_vid = name.endswith((".mp4", ".mov", ".m4v", ".webm"))
        if (want == "video") != is_vid:
            return None
        try:
            t = publisher.upload_file(str(f), want)
        except Exception as _ue:
            logger.warning(f"pre-upload {str(f)[:80]}: {_ue}")
            return None
        if not t:
            return None
        toks.append(t)
    return toks


def _race_safe_post(publisher, niche, channel_id, final_text, media_url,
                    media_type, media_files, markup, text_format,
                    slot_key, post_id, defer_cb):
    """Заливка → живой ad-скан → POST. Возвращает 'posted'|'held'|'failed'.

    14.09 (Дачный уголок 15:00): скан до заливки оставлял окно ~9с (альбом),
    реклама, легшая в него, перекрывалась. Теперь окно скан→POST < 1с.
    При HOLD: пост назад в pending, slot_lock СНИМАЕТСЯ (иначе отложенная
    джоба билась в «Слот уже занят» и посты терялись — та же дыра ела
    1-2 слота Нашего Дома ежедневно с 18.08), defer_cb(age) пере-откладывает.
    Fail-open по скану и заливке: сбой не блокирует публикацию."""
    try:
        toks = _upload_tokens_for(publisher, media_url, media_files, media_type)
    except Exception as _pe:
        logger.warning(f"[{niche}] pre-upload fail-open: {_pe}")
        toks = None
    try:
        from ad_race_guard import top_feed_foreign
        _tf = top_feed_foreign(publisher, niche, channel_id, 60)
    except Exception as _tfe:
        logger.debug(f"[{niche}] live ad-check fail-open: {_tfe}")
        _tf = None
    if _tf:
        try:
            _c = db.get_conn()
            _c.execute("UPDATE posts SET status='pending' "
                       "WHERE id=? AND status='processing'", (post_id,))
            _c.commit()
            _c.close()
        except Exception as _re:
            logger.warning(f"[{niche}] release post #{post_id}: {_re}")
        try:
            db.release_slot(niche, slot_key)
        except Exception as _rl:
            logger.warning(f"[{niche}] release slot {slot_key}: {_rl}")
        logger.info(f"[{niche}] AD-RACE HOLD перед отправкой: {_tf[0]} — "
                    f"пост #{post_id} возвращён в очередь (слот освобождён)")
        if defer_cb:
            try:
                defer_cb(_tf[1])
            except Exception as _de:
                logger.warning(f"[{niche}] defer_cb: {_de}")
        return "held"
    if toks:
        res = publisher.post_result(
            channel_id=channel_id, text=final_text, media_type=media_type,
            markup=markup, text_format=text_format, auto_edit_check=False,
            attachments_tokens=toks)
        return "posted" if res.get("ok") else "failed"
    ok = publisher.post(
        channel_id=channel_id, text=final_text, media_url=media_url,
        media_type=media_type, media_files=media_files, markup=markup,
        text_format=text_format, auto_edit_check=False)
    return "posted" if ok else "failed"


def _publish_channel(niche: str, channel_id: int, publisher: MaxPublisher,
                     media_type_filter: str = "any", defer_cb=None,
                     rescue_fetch=None):
    """Публикует 1 пост для канала. Возвращает True если опубликовано.

    rescue_fetch (27.08): спасательный добор канала. Слот 21:00 dachniki_2
    сгорел при «полной» очереди — добор принёс одни text_hash-дубли, счётчик
    _pending_count считал их живыми (sync-ветка молчала), а claim молча
    выбраковал. Теперь провал перебора сам зовёт добор и пробует ещё раз.
    """
    from fetcher import _matches_stopwords, _matches_regex_block, _matches_profanity

    # Атомарная защита: 1 канал = 1 пост в слот (через SQL INSERT OR IGNORE)
    slot_key = _current_slot_key()
    if not db.try_acquire_slot(niche, slot_key):
        logger.info(f"[{niche}] Слот {slot_key} уже занят — пропускаем")
        return False

    published = False
    for _ in range(15):
        # 26.08: вязанию видео важнее фото — из очереди сначала видео
        _vf = bool((load_config().get("channel_settings", {}) or {})
                   .get(niche, {}).get("video_first"))
        post = db.claim_pending_post(niche, media_type_filter, video_first=_vf)
        if not post:
            logger.info(f"[{niche}] Очередь пуста — пропускаем слот")
            break

        media_url = post.get("media_url")
        media_type = post.get("media_type")
        media_files_raw = post.get("media_files")
        media_files = json.loads(media_files_raw) if media_files_raw else None
        text = (post.get("rewritten_text") or "").strip()
        orig_text = (post.get("original_text") or "").strip()

        # HTTP(S) URL допустим — publisher.upload_file умеет качать сам.
        # Только локальные пути требуют физической проверки.
        is_remote = bool(media_url) and (str(media_url).startswith("http://") or str(media_url).startswith("https://"))
        if media_url and not is_remote and not os.path.exists(media_url):
            logger.warning(f"[{niche}] Медиафайл не найден: {media_url}")
            db.mark_skipped(post["id"], reason="media_file_missing")
            continue

        if not media_url and media_type_filter in ("photo", "video", "require_media"):
            db.mark_skipped(post["id"], reason=f"no_media_filter={media_type_filter}")
            continue

        if not media_url and not text:
            db.mark_skipped(post["id"], reason="no_content")
            continue

        # Проверяем стоп-слова в момент публикации (актуальные фильтры из конфига)
        if _matches_stopwords(orig_text or text, niche):
            logger.info(f"[{niche}] Пост #{post['id']} — стоп-слово при публикации, пропускаем")
            db.mark_skipped(post["id"], reason="stopword_at_publish")
            continue

        # Проверяем scoped regex (global + grid + channel) в момент публикации
        if _matches_regex_block(orig_text or text, niche) or _matches_regex_block(text or orig_text, niche):
            logger.info(f"[{niche}] Пост #{post['id']} — regex_block_at_publish")
            db.mark_skipped(post["id"], reason="regex_block_at_publish")
            continue

        # Regex-фильтр приветствий — проверяем И оригинал И рерайт (Claude мог написать приветствие)
        from processor.filter import is_greeting
        if is_greeting(orig_text) or is_greeting(text):
            logger.info(f"[{niche}] Пост #{post['id']} — приветствие в оригинале или рерайте, пропускаем")
            db.mark_skipped(post["id"], reason="greeting_filtered")
            continue

        # Глобальный анти-мат на этапе публикации (последний safety-барьер)
        if _matches_profanity(orig_text) or _matches_profanity(text):
            logger.info(f"[{niche}] Пост #{post['id']} — profanity filtered")
            db.mark_skipped(post["id"], reason="profanity_filtered")
            continue

        orig_media_url = media_url
        orig_media_files = media_files

        # Формат заголовка (2026-07-26): **bold** + пустая строка, как в Лайв/в MAX.
        # Только для каналов с markdown-подписью — иначе ** отрендерятся звёздочками.
        try:
            _sig = ((load_config().get("channel_settings", {}) or {}).get(niche, {}) or {}).get("signature", {}) or {}
            if _sig.get("style") == "markdown":
                text = _format_dacha_body(text)
        except Exception as _fe:
            logger.debug(f"[{niche}] title format skipped: {_fe}")
        final_text, final_markup, final_format = _apply_channel_signature(niche, channel_id, text)

        # DL 2026-08-04 + 14.09: заливка медиа → ЖИВОЙ скан ленты → отправка
        # готовыми токенами (_race_safe_post). Раньше скан шёл ДО заливки:
        # альбом из 5 фото ≈ 9с, реклама успевала лечь в эти секунды и
        # перекрывалась (Дачный уголок 14.09 15:00). auto-edit выключен для
        # сетки по запросу пользователя (2026-04-30).
        _st = _race_safe_post(publisher, niche, channel_id, final_text,
                              media_url, media_type, media_files,
                              final_markup, final_format,
                              slot_key, post["id"], defer_cb)
        if _st == "held":
            return False
        success = (_st == "posted")

        if success:
            db.mark_posted(post["id"])
            _cleanup_media(orig_media_url, orig_media_files, post["id"])
            logger.info(f"[{niche}] ✅ Пост #{post['id']} (@{post['source_id']}) опубликован")
            published = True
            break
        else:
            db.mark_skipped(post["id"], reason="max_api_error")
            logger.warning(f"[{niche}] ❌ Пост #{post['id']} не прошёл, пробуем следующий")

    if not published and rescue_fetch is not None:
        now_mono = time.monotonic()
        last = _RESCUE_LAST.get(niche)
        if last is not None and (now_mono - last) < _RESCUE_COOLDOWN_SEC:
            logger.info(f"[{niche}] спасательный добор пропущен (кулдаун)")
        else:
            _RESCUE_LAST[niche] = now_mono
            logger.info(f"[{niche}] кандидаты кончились — спасательный добор в слоте")
            try:
                rescue_fetch()
            except Exception as _rf_e:
                logger.warning(f"[{niche}] спасательный добор упал: {_rf_e}")
            db.release_slot(niche, slot_key)   # вернуть слот перед второй попыткой
            return _publish_channel(niche, channel_id, publisher,
                                    media_type_filter, defer_cb=defer_cb,
                                    rescue_fetch=None)
    if not published:
        logger.warning(f"[{niche}] Все попытки исчерпаны, слот пропущен")
        db.release_slot(niche, slot_key)  # освобождаем слот если не опубликовали
    return published


# ── Публикация батча для сетки ─────────────────────────────────────────────

def _collect_active_media(conn) -> set:
    """Файлы, которые чистилке трогать НЕЛЬЗЯ.

    15.09: раньше учитывалась только legacy-таблица posts — файлы кандидатов
    и готовящихся постов Лайв/вМАКС (candidate_pool, prepared_posts) были для
    чистилки «сиротами» и выносились до публикации: 25 постов за полдня 15.09
    ушли текстом без фото (Тобольск 07:34 — редактор заменяла руками)."""
    import json as _json
    cur = conn.cursor()
    active = set()
    cur.execute("SELECT media_url, media_files FROM posts WHERE status IN ('pending','processing')")
    for row in cur.fetchall():
        if row[0]: active.add(row[0])
        if row[1]:
            try:
                for f in _json.loads(row[1]):
                    if f: active.add(f)
            except Exception:
                pass
    try:
        cur.execute("SELECT media_url FROM candidate_pool WHERE used_at IS NULL "
                    "AND fetched_at >= datetime('now', '-3 days')")
        for row in cur.fetchall():
            if row[0]: active.add(row[0])
    except Exception as _e:
        logger.debug(f"cleanup: candidate_pool пропущен: {_e}")
    try:
        cur.execute("SELECT media_url, media_files_json FROM prepared_posts "
                    "WHERE status IN ('pending','preparing','ready')")
        for mu, mfj in cur.fetchall():
            if mu: active.add(mu)
            if mfj:
                try:
                    for f in _json.loads(mfj):
                        if f: active.add(f)
                except Exception:
                    pass
    except Exception as _e:
        logger.debug(f"cleanup: prepared_posts пропущен: {_e}")
    return active


def cleanup_orphan_media():
    """Удаляет медиафайлы не привязанные к pending постам."""
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(db.DB_PATH)
    active = _collect_active_media(conn)
    conn.close()

    media_dir = os.path.join(os.path.dirname(__file__), "media")
    if not os.path.exists(media_dir):
        return

    deleted, freed = 0, 0
    for root, dirs, files in os.walk(media_dir):
        for fname in files:
            path = os.path.join(root, fname)
            if path not in active:
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    deleted += 1
                except Exception as e:
                    logger.debug(f"Не удалось удалить {path}: {e}")

    if deleted:
        logger.info(f"🧹 Очистка медиа: удалено {deleted} файлов, освобождено {freed//1024//1024} МБ")


def calculate_next_available_slot(local_now, shift_to, grid_slots: list[str]):
    """
    Вычисляет ближайшее доступное время для переноса:
    - базово: shift_to (реклама + 60 минут)
    - если следующий регулярный слот <= shift_to, перенос не нужен (вернёт None)
    """
    from datetime import datetime

    if not shift_to:
        return None

    # найти ближайший регулярный слот после local_now
    next_regular = None
    for s in sorted(grid_slots or []):
        try:
            h, m = map(int, s.split(":"))
            candidate = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate <= local_now:
                continue
            if next_regular is None or candidate < next_regular:
                next_regular = candidate
        except Exception:
            continue

    if next_regular and next_regular <= shift_to:
        return None
    return shift_to


def _schedule_shifted_publish(scheduler, grid_name: str, niche: str, channel_id: int,
                             media_type_filter: str, run_at_local, tz_name: str):
    """Создаёт one-shot job на отложенную публикацию (реклама+60м)."""
    if run_at_local is None:
        return

    job_id = f"shift_{grid_name}_{niche}_{run_at_local.strftime('%Y%m%d_%H%M')}"

    def _run_once():
        config = load_config()
        publisher = MaxPublisher(config["max"]["token"])

        def _redefer(age_min):
            from datetime import datetime as _dt, timedelta as _td
            from zoneinfo import ZoneInfo as _zi
            _now = _dt.now(_zi(tz_name))
            _ra = (_now + _td(minutes=max(1.0, 60.0 - float(age_min)) + 1)
                   ).replace(second=0, microsecond=0)
            _schedule_shifted_publish(
                scheduler=scheduler, grid_name=grid_name, niche=niche,
                channel_id=int(channel_id), media_type_filter=media_type_filter,
                run_at_local=_ra, tz_name=tz_name)

        def _rescue_shifted():
            _fetch_on_demand(config, grid_name, niche)
        _publish_channel(niche, int(channel_id), publisher, media_type_filter,
                         defer_cb=_redefer, rescue_fetch=_rescue_shifted)

    from apscheduler.triggers.date import DateTrigger
    try:
        scheduler.add_job(
            _run_once,
            trigger=DateTrigger(run_date=run_at_local, timezone=tz_name),
            id=job_id,
            name=f"Shifted publish [{niche}]",
            replace_existing=True,
            misfire_grace_time=180,
        )
        logger.info(f"[AD CHECK] Канал {channel_id}: создан deferred-job на {run_at_local.strftime('%H:%M')}")
    except Exception as e:
        logger.warning(f"[AD CHECK] Канал {channel_id}: не удалось создать deferred-job: {e}")


def _unplanned_external_shift(niche: str, local_now, quiet_min: int = 60):
    """run_at_local | None: свежий ВНЕплановый внешний пост канала (<quiet_min)
    → время T_ext+quiet_min для deferred-публикации (_schedule_shifted_publish).

    Дача 2026-07-20: реклама в 10:00 (вне плановых 09/12/15/19) перекрыта
    слот-постами 10:01/10:31. Watcher теперь видит Дача-каналы (external_posts_seen);
    этот guard даёт час тишины после любой чужой публикации. Fail-open."""
    try:
        ts = db.last_external_seen_within(niche, quiet_min)
    except Exception as e:
        logger.debug(f"_unplanned_external_shift {niche}: {e}")
        return None
    if not ts:
        return None
    from datetime import datetime as _dt, timedelta as _td
    try:
        seen = _dt.fromisoformat(ts)
        run_at = (seen + _td(minutes=quiet_min)).astimezone(local_now.tzinfo)
        # 14.09: обрезка секунд ВНИЗ давала run_at раньше конца часа тишины
        # (реклама 15:00:05 → сдвиг «на 16:00», гард в 16:00:01 видел age=59.9м
        # и пере-откладывал). Теперь округляем ВВЕРХ до минуты + 1 мин запаса.
        floored = run_at.replace(second=0, microsecond=0)
        if run_at != floored:
            floored += _td(minutes=1)
        return floored + _td(minutes=1)
    except Exception as e:
        logger.debug(f"_unplanned_external_shift parse {niche}: {e}")
        return None


def _pending_count(niche: str) -> int:
    """Количество pending постов для канала."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db.DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM posts WHERE channel=? AND status='pending'", (niche,))
    count = cur.fetchone()[0]
    conn.close()
    return count


def _fetch_on_demand(config: dict, grid_name: str, niche: str):
    """On-demand fetch только для одного канала (без прохода по всей сетке)."""
    from fetcher import fetch_channel
    logger.info(f"[{grid_name}][{niche}] On-demand fetch (очередь мала)...")
    try:
        added = fetch_channel(config, niche, grid_name=grid_name)
        logger.info(f"[{grid_name}][{niche}] On-demand fetch: +{added}")
    except Exception as e:
        logger.error(f"[{grid_name}][{niche}] On-demand fetch ошибка: {e}")


def _topup_decision(pending: int, low_water: int) -> str:
    """Что делать с очередью канала перед публикацией:
      'sync'       — очередь ПУСТА: фетчим прямо сейчас, иначе слот потерян;
      'background' — тонкая (0 < pending < low_water): публикуем сразу,
                     добираем фоном (слот не ждёт 11-13 мин фетча);
      'none'       — хватает.

    2026-08-10: замер показал, что слот 18:00 выходил в 18:11-18:13, потому
    что тик упирался в on-demand fetch голодного «Дачного уголка», а ждали
    его ВСЕ шесть каналов сетки."""
    if pending <= 0:
        return "sync"
    if pending < low_water:
        return "background"
    return "none"


def make_grid_topup(grid_name: str, low_water: int = 5):
    """Фоновый добор очередей сетки — работает МЕЖДУ слотами."""
    def topup():
        config = load_config()
        settings = _grid_settings_util(config, grid_name)
        if not settings.get("enabled"):
            return
        disabled = config.get("disabled_channels", []) or []
        sources_cfg = config.get("sources", {}) or {}
        channels_cfg = (config.get("max", {}) or {}).get("channels", {}) or {}
        hungry = []
        for niche in (config.get("grids", {}) or {}).get(grid_name, []) or []:
            if niche in disabled or not channels_cfg.get(niche):
                continue
            srcs = sources_cfg.get(niche, {}) or {}
            if not any(len(v) > 0 for v in srcs.values()):
                continue
            if _pending_count(niche) < low_water:
                hungry.append(niche)
        if not hungry:
            return
        logger.info(f"[{grid_name}] фоновый добор: {len(hungry)} канал(ов) "
                    f"голодны — {', '.join(hungry)}")
        for niche in hungry:
            try:
                _fetch_on_demand(config, grid_name, niche)
            except Exception as e:
                logger.error(f"[{grid_name}][{niche}] фоновый добор: {e}")
    return topup


def make_grid_post_batch(grid_name: str, scheduler=None):
    """Фабрика: создаёт функцию публикации для конкретной сетки.
    
    Логика:
    1. Для каждого канала проверяет очередь
    2. Если pending < LOW_WATER (5) — запускает on-demand fetch
    3. Публикует 1 пост, защищая от дублей через threading.Lock + SQL-проверку
    """
    LOW_WATER = 5  # порог для on-demand fetch

    def post_batch():
        config = load_config()
        publisher = MaxPublisher(config["max"]["token"])
        settings = _grid_settings_util(config, grid_name)

        if not settings["enabled"]:
            logger.info(f"[{grid_name}] Сетка на паузе — пропускаем")
            return

        disabled = config.get("disabled_channels", [])
        channels_cfg = config.get("max", {}).get("channels", {})
        sources_cfg = config.get("sources", {})
        grid_channels = config.get("grids", {}).get(grid_name, [])
        media_type_filter = settings["media_type"]
        channel_settings = config.get("channel_settings", {})

        published_count = 0
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo(settings["timezone"])
        local_now = datetime.now(tzinfo)

        for niche in grid_channels:
            if niche in disabled:
                continue
            srcs = sources_cfg.get(niche, {})
            if not any(len(v) > 0 for v in srcs.values()):
                continue
            channel_id = channels_cfg.get(niche)
            if not channel_id:
                continue

            # Рекламная проверка/приоритет
            ad_check = db.check_ad_slot(str(channel_id), local_now)
            if ad_check.get("has_ad") and ad_check.get("ad"):
                ad_hm = local_now.strftime('%H:%M')
                logger.info(f"[AD CHECK] Канал {channel_id}: найдена реклама на {ad_hm}, контентный слот смещен")
                _publish_ad_if_due(int(channel_id), ad_check["ad"], publisher)
                continue
            if ad_check.get("block_content"):
                shift_to = ad_check.get("shift_to")
                logger.info(
                    f"[AD CHECK] Канал {channel_id}: {ad_check.get('reason')}, "
                    f"контентный слот смещен до {shift_to.strftime('%H:%M') if shift_to else 'N/A'}"
                )

                # Реальное смещение: создаём one-shot job на ad+60м, только если это нужно
                if scheduler is not None:
                    grid_slots = config.get("grid_settings", {}).get(grid_name, {}).get("schedule_slots", [])
                    run_at = calculate_next_available_slot(local_now, shift_to, grid_slots)
                    ch_media = channel_settings.get(niche, {}).get("media_mode")
                    effective_filter = ch_media if ch_media and ch_media != "any" else media_type_filter
                    _schedule_shifted_publish(
                        scheduler=scheduler,
                        grid_name=grid_name,
                        niche=niche,
                        channel_id=int(channel_id),
                        media_type_filter=effective_filter,
                        run_at_local=run_at,
                        tz_name=settings["timezone"],
                    )
                continue

            # Час тишины после ВНЕплановой рекламы (Дача 2026-07-20):
            # watcher детектит чужие посты (external_posts_seen); свежий (<60мин)
            # → слот сдвигается готовым deferred-механизмом (реклама+60м).
            _ext_run_at = _unplanned_external_shift(niche, local_now)
            if _ext_run_at is not None and _ext_run_at > local_now:
                logger.info(
                    f"[{grid_name}][{niche}] внеплановая реклама <60мин — "
                    f"слот сдвинут на {_ext_run_at.strftime('%H:%M')}")
                if scheduler is not None:
                    _ch_m = channel_settings.get(niche, {}).get("media_mode")
                    _eff = _ch_m if _ch_m and _ch_m != "any" else media_type_filter
                    _schedule_shifted_publish(
                        scheduler=scheduler, grid_name=grid_name, niche=niche,
                        channel_id=int(channel_id), media_type_filter=_eff,
                        run_at_local=_ext_run_at, tz_name=settings["timezone"])
                continue

            # Очередь: пустую добираем ПРЯМО СЕЙЧАС (иначе слот потерян),
            # тонкую оставляем фоновому top-up — слот не ждёт фетч.
            pending = _pending_count(niche)
            _decision = _topup_decision(pending, LOW_WATER)
            if _decision == "sync":
                logger.info(f"[{grid_name}][{niche}] Очередь пуста — фетч в слоте")
                _fetch_on_demand(config, grid_name, niche)
            elif _decision == "background":
                logger.info(f"[{grid_name}][{niche}] Очередь {pending} < {LOW_WATER} "
                            f"— добор уйдёт в фон, слот не ждём")

            ch_media = channel_settings.get(niche, {}).get("media_mode")
            effective_filter = ch_media if ch_media and ch_media != "any" else media_type_filter

            # DL 2026-08-04: live-hold перед отправкой -> пере-отложить слот
            # на рекламу+60м штатным deferred-механизмом (дефолт-аргументы
            # фиксируют значения итерации цикла).
            def _defer_race(age_min, _n=niche, _cid=channel_id, _f=effective_filter):
                from datetime import datetime as _dt, timedelta as _td
                from zoneinfo import ZoneInfo as _zi
                _now = _dt.now(_zi(settings["timezone"]))
                _ra = (_now + _td(minutes=max(1.0, 60.0 - float(age_min)) + 1)
                       ).replace(second=0, microsecond=0)
                _schedule_shifted_publish(
                    scheduler=scheduler, grid_name=grid_name, niche=_n,
                    channel_id=int(_cid), media_type_filter=_f,
                    run_at_local=_ra, tz_name=settings["timezone"])

            def _rescue(_n=niche):
                _fetch_on_demand(config, grid_name, _n)
            if _publish_channel(niche, int(channel_id), publisher, effective_filter,
                                defer_cb=_defer_race, rescue_fetch=_rescue):
                published_count += 1

        logger.info(f"[{grid_name}] Слот: опубликовано {published_count} постов")

    post_batch.__name__ = f"post_batch_{grid_name}"
    return post_batch


# ── Fetch для сетки ────────────────────────────────────────────────────────

def make_grid_fetch(grid_name: str):
    """Фабрика: создаёт функцию ночного fetch для конкретной сетки."""
    def nightly_fetch():
        logger.info(f"[{grid_name}] Fetch запущен...")
        config = load_config()
        from fetcher import fetch_grid
        total = fetch_grid(config, grid_name)
        logger.info(f"[{grid_name}] Fetch завершён: добавлено {total} постов")
        db.cleanup_seen_posts()

    nightly_fetch.__name__ = f"nightly_fetch_{grid_name}"
    return nightly_fetch


def _channel_timezone_map(config: dict) -> dict[str, str]:
    """channel_id(str) -> timezone"""
    mapping = {}
    channels_cfg = config.get("max", {}).get("channels", {})  # key->id
    grids = config.get("grids", {})
    gset = config.get("grid_settings", {})
    default_tz = config.get("schedule", {}).get("timezone", "Europe/Moscow")

    key_to_grid = {}
    for g, arr in grids.items():
        for k in arr:
            key_to_grid[k] = g

    for key, cid in channels_cfg.items():
        grid = key_to_grid.get(key)
        tz = gset.get(grid, {}).get("timezone", default_tz) if grid else default_tz
        mapping[str(cid)] = tz
    return mapping


def _run_ads_worker(config: dict | None = None):
    """Rate-limited ad worker: публикует due-рекламу малыми батчами."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # ВАЖНО: всегда подгружаем свежий конфиг, чтобы видеть новые слоты/настройки без рестарта
    config = load_config()

    token = config.get("max", {}).get("token")
    if not token:
        return

    tz_map = _channel_timezone_map(config)
    pending_ads = db.list_pending_ads(limit=800)
    if not pending_ads:
        return

    publisher = MaxPublisher(token)
    published = 0

    for ad in pending_ads:
        ch_id = str(ad.get("channel_id"))
        tz_name = tz_map.get(ch_id, config.get("schedule", {}).get("timezone", "Europe/Moscow"))
        now_local = datetime.now(ZoneInfo(tz_name)).replace(second=0, microsecond=0)
        try:
            target_local = datetime.fromisoformat(ad.get("target_datetime")).replace(second=0, microsecond=0)
            if target_local.tzinfo is None:
                target_local = target_local.replace(tzinfo=now_local.tzinfo)
            else:
                target_local = target_local.astimezone(now_local.tzinfo)
        except Exception:
            db.mark_ad_error(ad["id"], "bad_target_datetime")
            continue

        # Публикуем если время уже наступило в локальной TZ канала
        if target_local > now_local:
            continue

        logger.info(f"[AD CHECK] Канал {ch_id}: найдена реклама на {target_local.strftime('%H:%M')}, контентный слот смещен")
        ok = _publish_ad_if_due(int(ch_id), ad, publisher)
        if ok:
            published += 1

        # Rate limit + jitter (защита API)
        time.sleep(0.35 + random.random() * 0.45)

    if published:
        logger.info(f"[AD WORKER] Опубликовано рекламных постов: {published}")


def _collect_external_ads_by_slots(config: dict, publisher: MaxPublisher) -> int:
    """Детектит внешние рекламные посты (не из TG-бота) по слотам/окнам времени."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    grids_cfg = config.get("grids", {}) or {}
    gset = config.get("grid_settings", {}) or {}
    channels_cfg = (config.get("max", {}) or {}).get("channels", {}) or {}

    # Bug #7 fix (2026-05-20): default ad_slots for Live/MAX grids whose
    # grid_settings doesn't explicitly list ad_slots. Without this, the
    # detector silently skipped both Города лайв and Города в MAX (only
    # Дача и Вязание had ad_slots configured), so external_ad_posts stayed
    # empty and the 60-min cooldown never auto-fired.
    # Per CLAUDE.md note: «реклама в Лайв определяется по часу публикации
    # МСК: 09, 12, 15, 19». Adding 18:00 because today's ads landed at
    # 18:01/18:10 МСК — the network-side rotation evidently uses both
    # 18:00 and 19:00 slots.
    # 2026-06-17: убран 19:00 — его НЕТ в подтверждённом news_ad_windows (MSK+0:
    # 09/12/15/18). 19:00 — КОНТЕНТНЫЙ слот, не рекламный; из-за него collector
    # ловил наш пост слота 19:00 как рекламу → ложный slot_shift → дубль.
    _DEFAULT_AD_SLOTS_BY_GRID = {
        "Города лайв":   ["09:00", "12:00", "15:00", "18:00"],
        "Города в MAX":  ["09:00", "12:00", "15:00", "18:00"],
    }

    detected = 0

    for grid_name, channels in grids_cfg.items():
        gs = gset.get(grid_name, {}) or {}
        ad_slots = gs.get("ad_slots") or _DEFAULT_AD_SLOTS_BY_GRID.get(grid_name) or []
        if not ad_slots:
            continue

        tz_name = gs.get("timezone") or config.get("schedule", {}).get("timezone", "Europe/Moscow")
        tz = ZoneInfo(tz_name)
        now_local = datetime.now(tz)
        content_slots = gs.get("schedule_slots") or []

        # окрестность рекламного слота (в минутах).
        # Bug #7 fix: bumped after_min default 10 → 15 because the 18:10 МСК
        # car ad sat exactly at the old window edge (10 min past 18:00 slot)
        # and a 30-second jitter could push it outside. Дача grid overrides
        # this via grid_settings (after_min=13), so no regression there.
        before_min = int(gs.get("ad_detect_before_min", 7) or 7)
        after_min = int(gs.get("ad_detect_after_min", 15) or 15)

        for niche in (channels or []):
            ch_id = channels_cfg.get(niche)
            if not ch_id:
                continue

            msgs = publisher.list_messages(int(ch_id), limit=60)
            if not msgs:
                continue

            parsed = []
            for m in msgs:
                ts_ms = m.get("timestamp")
                body = m.get("body") or {}
                mid = body.get("mid")
                if not ts_ms or not mid:
                    continue
                try:
                    dt_local = datetime.fromtimestamp(int(ts_ms) / 1000, tz)
                except Exception:
                    continue
                # Берём только актуальное окно ~36ч
                if dt_local < now_local - timedelta(hours=36):
                    continue
                parsed.append((dt_local, str(mid)))

            if not parsed:
                continue

            for days_back in (0, 1):
                day = (now_local - timedelta(days=days_back)).date()
                for slot in ad_slots:
                    try:
                        hh, mm = [int(x) for x in str(slot).split(":", 1)]
                    except Exception:
                        continue

                    slot_dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                    win_l = slot_dt - timedelta(minutes=before_min)
                    win_r = slot_dt + timedelta(minutes=after_min)
                    # если окно ещё не наступило — пропускаем
                    if now_local < win_l:
                        continue

                    candidates = [(dt, mid) for (dt, mid) in parsed if win_l <= dt <= win_r]
                    if not candidates:
                        continue

                    # Берём ближайший к слоту
                    dt_best, mid_best = min(candidates, key=lambda x: abs((x[0] - slot_dt).total_seconds()))

                    # Защита от путаницы с контентными слотами:
                    # если пост заметно ближе к контентному слоту, этот кандидат отбрасываем.
                    if content_slots:
                        try:
                            content_deltas = []
                            for cs in content_slots:
                                ch, cm = [int(x) for x in str(cs).split(":", 1)]
                                cdt = datetime(day.year, day.month, day.day, ch, cm, tzinfo=tz)
                                content_deltas.append(abs((dt_best - cdt).total_seconds()))
                            nearest_content = min(content_deltas) if content_deltas else 10**9
                            nearest_ad = abs((dt_best - slot_dt).total_seconds())
                            if nearest_content + 120 < nearest_ad:
                                continue
                        except Exception:
                            pass

                    ad_id = db.upsert_external_ad_post(
                        channel_id=str(ch_id),
                        mid=mid_best,
                        published_at=dt_best.astimezone(ZoneInfo("UTC")).replace(tzinfo=None).isoformat(),
                        slot_local=slot_dt.isoformat(timespec="minutes"),
                    )
                    if ad_id:
                        detected += 1

    return detected


def _collect_views_for_rows(rows: list[dict], publisher: MaxPublisher) -> int:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    collected = 0
    for ad in rows:
        ad_id = ad.get("ad_id") or ad.get("id")
        ch_id = ad.get("channel_id")
        mid = ad.get("published_mid") or ad.get("mid")
        pub_at_raw = ad.get("published_at")
        ttl_min = int((ad.get("auto_delete_after_min") or 0))
        if not ad_id or not ch_id or not mid or not pub_at_raw:
            continue
        try:
            pub_at = datetime.fromisoformat(pub_at_raw)
        except Exception:
            continue

        # checkpoints every 24h up to TTL (24/48/72...)
        max_hours = max(24, (ttl_min // 60)) if ttl_min else 72
        checkpoints = [h for h in range(24, max_hours + 1, 24)]

        for h in checkpoints:
            due_at = pub_at + timedelta(hours=h)
            if now < due_at:
                continue
            if db.has_ad_view_stat(int(ad_id), int(h)):
                continue
            views = publisher.get_message_views(int(ch_id), str(mid))
            db.add_ad_view_stat(int(ad_id), str(ch_id), str(mid), int(h), views, payload="")
            collected += 1
            time.sleep(0.15 + random.random() * 0.2)

    return collected


def _run_ads_stats_worker(config: dict | None = None):
    """Collect ad views snapshots each 24h: +24 / +48 / +72 (by campaign TTL)."""
    config = load_config()
    token = config.get("max", {}).get("token")
    if not token:
        return

    publisher = MaxPublisher(token)

    # 1) Классические ads_schedule
    rows = db.list_ads_for_view_stats(limit=600)
    collected = _collect_views_for_rows(rows, publisher)

    # 2) Внешние рекламные посты. Детекция ПЕРЕНЕСЕНА в ad_detection_loop (17.06):
    # news_ad_windows (правильное расписание) + tz-aware + bot_posted_mids guard.
    # Legacy _collect_external_ads_by_slots ОТКЛЮЧЁН (был источником FP-дублей слота 19:00).
    # _collect_external_ads_by_slots(config, publisher)  # OFF — replaced by ad_detection_loop
    ext_rows = db.list_external_ads_for_view_stats(limit=1200)
    collected += _collect_views_for_rows(ext_rows, publisher)

    if collected:
        logger.info(f"[AD STATS] Собрано срезов охватов: {collected}")


def _run_ads_delete_worker(config: dict | None = None):
    """Auto-delete published ads whose TTL expired."""
    config = load_config()
    token = config.get("max", {}).get("token")
    if not token:
        return

    due = db.list_due_ad_deletions(limit=100)
    if not due:
        return

    publisher = MaxPublisher(token)
    deleted = 0
    for ad in due:
        ad_id = ad.get("id")
        ch_id = ad.get("channel_id")
        mid = ad.get("published_mid")
        if not mid or not ch_id:
            db.mark_ad_delete_error(ad_id, "missing_mid_or_channel")
            continue
        ok = publisher.delete_message(int(ch_id), str(mid))
        if ok:
            db.mark_ad_deleted(ad_id)
            deleted += 1
        else:
            db.mark_ad_delete_error(ad_id, "delete_failed")
        time.sleep(0.15 + random.random() * 0.25)

    if deleted:
        logger.info(f"[AD DELETE] Удалено рекламных постов: {deleted}")


def _start_news_realtime_engine_if_enabled(config: dict):
    rt = config.get("news_realtime", {})
    if not rt.get("enabled", False):
        return

    def _runner():
        try:
            from news_realtime_engine import run_engine_from_config
            run_engine_from_config()
        except Exception as e:
            logger.exception(f"[NEWS RT] engine crashed: {e}")

    t = threading.Thread(target=_runner, name="news-realtime-engine", daemon=True)
    t.start()
    logger.info("[NEWS RT] Реалтайм-движок новостной сетки запущен")


def make_grid_live_fetch(grid_name: str):
    """
    Фабрика: живой режим — fetch + немедленная публикация новых постов.
    Не накапливает очередь, публикует сразу после сбора.
    """
    def live_fetch():
        config = load_config()
        settings = _grid_settings_util(config, grid_name)
        if not settings["enabled"]:
            return

        from fetcher import fetch_grid
        added = fetch_grid(config, grid_name)
        if added == 0:
            return

        logger.info(f"[{grid_name}] Живой fetch: +{added} постов → публикуем...")
        publisher = MaxPublisher(config["max"]["token"])
        disabled = config.get("disabled_channels", [])
        channels_cfg = config.get("max", {}).get("channels", {})
        sources_cfg = config.get("sources", {})
        grid_channels = config.get("grids", {}).get(grid_name, [])
        channel_settings = config.get("channel_settings", {})
        media_type_filter = settings["media_type"]
        published_count = 0

        from datetime import datetime
        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo(settings["timezone"])
        local_now = datetime.now(tzinfo)

        for niche in grid_channels:
            if niche in disabled:
                continue
            srcs = sources_cfg.get(niche, {})
            if not any(len(v) > 0 for v in srcs.values()):
                continue
            channel_id = channels_cfg.get(niche)
            if not channel_id:
                continue

            # AD guard и в live-режиме
            ad_check = db.check_ad_slot(str(channel_id), local_now)
            if ad_check.get("has_ad") and ad_check.get("ad"):
                _publish_ad_if_due(int(channel_id), ad_check["ad"], publisher)
                continue
            if ad_check.get("block_content"):
                continue

            ch_media = channel_settings.get(niche, {}).get("media_mode")
            effective_filter = ch_media if ch_media and ch_media != "any" else media_type_filter
            # В живом режиме публикуем не более 1 поста на канал за цикл
            def _rescue_live(_n=niche):
                _fetch_on_demand(config, grid_name, _n)
            if _publish_channel(niche, int(channel_id), publisher, effective_filter,
                                rescue_fetch=_rescue_live):
                published_count += 1

        if published_count:
            logger.info(f"[{grid_name}] Живой режим: опубликовано {published_count} постов")

    live_fetch.__name__ = f"live_fetch_{grid_name}"
    return live_fetch


# ── Запуск ──────────────────────────────────────────────────────────────────


# ─── Prepare-then-fire workers (2026-05-21) ──────────────────────────
import threading as _pp_threading


async def _pp_periodic(coro_fn, interval_sec: int, cfg):
    """Run async coro every interval_sec. Catches all errors so loop stays alive."""
    import asyncio as _aio
    while True:
        try:
            await coro_fn(cfg)
        except Exception:
            logger.exception(f"_pp_periodic: {coro_fn.__name__} raised")
        await _aio.sleep(interval_sec)


def _start_prepared_pipeline_if_enabled(config: dict):
    """Start preparer/publisher background loop in dedicated thread.

    No-op if config.prepared_pipeline.enabled is false.
    Cleanup cron registered in main apscheduler (separate).
    """
    pp = config.get("prepared_pipeline", {}) or {}
    if not pp.get("enabled"):
        logger.info("[PREPARED_PIPELINE] disabled in config — workers NOT started")
        return

    def _runner():
        import asyncio as _aio
        from prepared_pipeline.preparer import preparer_tick
        from prepared_pipeline.publisher import publisher_tick
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)

        async def main():
            preparer_int = pp.get("preparer_interval_seconds", 60)
            publisher_int = pp.get("publisher_interval_seconds", 5)
            tasks = [
                _aio.create_task(_pp_periodic(preparer_tick, preparer_int, config)),
                _aio.create_task(_pp_periodic(publisher_tick, publisher_int, config)),
            ]
            await _aio.gather(*tasks)

        try:
            loop.run_until_complete(main())
        except Exception:
            logger.exception("[PREPARED_PIPELINE] runner crashed")

    t = _pp_threading.Thread(target=_runner, name="prepared-pipeline", daemon=True)
    t.start()
    logger.info("[PREPARED_PIPELINE] worker'ы запущены "
                f"(preparer={pp.get('preparer_interval_seconds', 60)}s, "
                f"publisher={pp.get('publisher_interval_seconds', 5)}s)")


def _start_max_ad_cover_if_enabled(config: dict):
    """Start MAX ad-cover: watcher (long-poll /updates) + ad_cover_tick periodic.

    No-op if config.max_ad_cover.enabled is false (dark launch — default).
    Watcher ловит внеплановую рекламу в external_posts_seen; tick перекрывает
    её cover-постом через delay_minutes (обход ad_guard). См.
    prepared_pipeline/ad_cover.py и max_channel_watcher.py.
    """
    ac = config.get("max_ad_cover", {}) or {}
    if not ac.get("enabled"):
        logger.info("[MAX_AD_COVER] max_ad_cover disabled in config — watcher/cover NOT started")
        return

    token = (config.get("max", {}) or {}).get("token", "")
    if not token:
        logger.warning("[MAX_AD_COVER] no max.token — NOT started")
        return

    def _runner():
        import asyncio as _aio
        import os as _os
        from max_channel_watcher import watcher_loop
        from prepared_pipeline.ad_cover import ad_cover_tick
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)

        poll_timeout = int(ac.get("watcher_poll_timeout_seconds", 30))
        tick_int = int(ac.get("tick_interval_seconds", 60))
        marker_path = ac.get("watcher_marker_path") or _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), ".max_watcher_marker")

        async def main():
            tasks = [
                _aio.create_task(watcher_loop(token, poll_timeout, marker_path)),
                _aio.create_task(_pp_periodic(ad_cover_tick, tick_int, config)),
            ]
            await _aio.gather(*tasks)

        try:
            loop.run_until_complete(main())
        except Exception:
            logger.exception("[MAX_AD_COVER] runner crashed")

    t = _pp_threading.Thread(target=_runner, name="max-ad-cover", daemon=True)
    t.start()
    logger.info(
        f"[MAX_AD_COVER] watcher + cover запущены "
        f"(delay={ac.get('delay_minutes', 60)}min, tick={ac.get('tick_interval_seconds', 60)}s)")


def _start_ad_detection_if_enabled(config: dict):
    """Start ad_detection_loop in a dedicated thread (mirror prepared_pipeline pattern).

    No-op if config.ad_detection.enabled is false (dark launch — default).
    """
    ad_cfg = config.get("ad_detection", {}) or {}
    if not ad_cfg.get("enabled"):
        logger.info("[AD_DETECTION] ad_detection disabled in config — loop NOT started")
        return

    def _runner():
        import asyncio as _aio
        from ad_detection.loop import ad_detection_loop
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
        try:
            loop.run_until_complete(ad_detection_loop(config))
        except Exception:
            logger.exception("[AD_DETECTION] runner crashed")

    import threading
    t = threading.Thread(target=_runner, name="ad-detection-loop", daemon=True)
    t.start()
    logger.info(f"[AD_DETECTION] loop started (tick_seconds={ad_cfg.get('tick_seconds', 30)})")


def start(config: dict):
    db.init_db()
    tz_default = config.get("schedule", {}).get("timezone", "Europe/Moscow")
    scheduler = BlockingScheduler(timezone=tz_default)

    grids = config.get("grids", {})
    if not grids:
        logger.warning("Нет сеток в конфиге — нечего запускать")
        return

    news_rt_cfg = config.get("news_realtime", {})
    news_rt_enabled = bool(news_rt_cfg.get("enabled", False))
    # FIX 2026-05-11 (DUAL SCHEDULER BUG): раньше читали ТОЛЬКО grid_name (singular).
    # После добавления grid_names (plural) в engine — legacy scheduler работал для
    # ОБОИХ гридов параллельно с realtime engine. Это давало дубль-публикации
    # (Иркутск дайджест 21:00 локального + legacy пост 16:00 МСК через 3 мин).
    # Поддерживаем оба формата для backward compat.
    news_rt_grids = news_rt_cfg.get("grid_names") or [news_rt_cfg.get("grid_name", "Города России")]
    if isinstance(news_rt_grids, str):
        news_rt_grids = [news_rt_grids]

    for grid_name in grids:
        settings = _grid_settings_util(config, grid_name)
        if not settings["enabled"]:
            logger.info(f"[{grid_name}] Сетка отключена — пропускаем")
            continue

        # Разделение логик: новостная сетка уходит в realtime engine, legacy-планировщик её не трогает
        if news_rt_enabled and grid_name in news_rt_grids:
            logger.info(f"[{grid_name}] Legacy-планировщик отключён (news_realtime включен)")
            continue

        tz = settings["timezone"]
        live_mode = settings["live_mode"]
        fetch_interval = settings["fetch_interval_min"]

        if live_mode:
            # Живой режим: fetch + публикация каждые N минут
            from apscheduler.triggers.interval import IntervalTrigger
            scheduler.add_job(
                make_grid_live_fetch(grid_name),
                IntervalTrigger(minutes=fetch_interval, timezone=tz),
                id=f"live_fetch_{grid_name}",
                name=f"Живой fetch [{grid_name}]",
                misfire_grace_time=60,
                coalesce=True,
                replace_existing=True,
            )
            logger.info(
                f"[{grid_name}] 🔴 ЖИВОЙ РЕЖИМ | "
                f"Fetch каждые {fetch_interval} мин | "
                f"Медиа: {settings['media_type']}"
            )
        else:
            fetch_h = settings["fetch_hour"]
            slots = config.get("grid_settings", {}).get(grid_name, {}).get("schedule_slots", [])

            if slots:
                # Режим произвольных слотов: "08:00", "10:30", ...
                for idx, slot in enumerate(slots):
                    try:
                        h, m = map(int, slot.split(":"))
                    except Exception:
                        logger.warning(f"[{grid_name}] Неверный слот: {slot!r}")
                        continue
                    scheduler.add_job(
                        make_grid_post_batch(grid_name, scheduler),
                        CronTrigger(hour=h, minute=m, timezone=tz),
                        id=f"post_batch_{grid_name}_{idx}",
                        name=f"Публикация [{grid_name}] {slot}",
                        misfire_grace_time=300,
                        coalesce=True,
                        replace_existing=True,
                    )
                logger.info(
                    f"[{grid_name}] Слоты: {', '.join(slots)} МСК | "
                    f"Fetch в {fetch_h}:00 МСК | Медиа: {settings['media_type']}"
                )
            else:
                # Обычный режим: публикация по расписанию + ночной fetch
                hours_str = _hours_str(settings["start_hour"], settings["end_hour"],
                                       settings["interval_hours"])
                scheduler.add_job(
                    make_grid_post_batch(grid_name, scheduler),
                    CronTrigger(hour=hours_str, minute=0, timezone=tz),
                    id=f"post_batch_{grid_name}",
                    name=f"Публикация [{grid_name}]",
                    misfire_grace_time=300,
                    coalesce=True,
                    replace_existing=True,
                )
                logger.info(
                    f"[{grid_name}] Публикации в {hours_str}:00 МСК | "
                    f"Fetch в {fetch_h}:00 МСК | "
                    f"Медиа: {settings['media_type']} | "
                    f"Постов/день: {settings['posts_per_day']}"
                )

            scheduler.add_job(
                make_grid_fetch(grid_name),
                CronTrigger(hour=fetch_h, minute=0, timezone=tz),
                id=f"nightly_fetch_{grid_name}",
                name=f"Fetch [{grid_name}]",
                misfire_grace_time=600,
                coalesce=True,
                replace_existing=True,
            )

            # Фоновый добор очередей между слотами (2026-08-10): снимает
            # с тика обязанность ждать фетч — публикации выходят вовремя.
            from apscheduler.triggers.interval import IntervalTrigger as _IT
            scheduler.add_job(
                make_grid_topup(grid_name),
                _IT(minutes=int((settings or {}).get("topup_interval_min", 7)),
                    timezone=tz),
                id=f"topup_{grid_name}",
                name=f"Дозабор [{grid_name}]",
                misfire_grace_time=120,
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )

    # Рекламный worker (rate-limited), проверка каждую минуту
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        _run_ads_worker,
        IntervalTrigger(minutes=1, timezone=tz_default),
        id="ads_worker",
        name="Ads worker",
        misfire_grace_time=30,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_ads_stats_worker,
        IntervalTrigger(minutes=5, timezone=tz_default),
        id="ads_stats_worker",
        name="Ads views stats worker",
        misfire_grace_time=30,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        _run_ads_delete_worker,
        IntervalTrigger(minutes=5, timezone=tz_default),
        id="ads_delete_worker",
        name="Ads auto-delete worker",
        misfire_grace_time=30,
        coalesce=True,
        replace_existing=True,
    )

    # Реалтайм-движок для новостной сетки (отдельный цикл)
    _start_news_realtime_engine_if_enabled(config)

    # Безопасная регулярная очистка orphan-медиа (не трогает pending/processing)
    scheduler.add_job(
        cleanup_orphan_media,
        IntervalTrigger(hours=1, timezone=tz_default),
        id="cleanup_media_hourly",
        name="Очистка orphan media (hourly)",
        misfire_grace_time=600,
        coalesce=True,
        replace_existing=True,
    )

    # Ежедневная дополнительная глубокая очистка в 04:00 МСК
    scheduler.add_job(
        cleanup_orphan_media,
        CronTrigger(hour=1, minute=0, timezone=tz),  # 04:00 МСК = 01:00 UTC
        id="cleanup_media_daily",
        name="Очистка медиафайлов (daily)",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    # ─── Prepare-then-fire integration ───
    _start_prepared_pipeline_if_enabled(config)

    # ─── Ad detection loop (Sprint 2 — Task 8) ───
    _start_ad_detection_if_enabled(config)

    # ─── MAX ad-cover: внеплановая реклама → cover через delay (24.06) ───
    _start_max_ad_cover_if_enabled(config)

    # ─── Cleanup cron (daily @ 04:00 МСК = 01:00 UTC) ───
    try:
        from prepared_pipeline.recovery import cleanup_old_rows
        scheduler.add_job(
            lambda: cleanup_old_rows(days=7),
            'cron', hour=1, minute=0,
            name='prepared_posts_cleanup',
        )
    except Exception:
        logger.exception("Failed to register prepared_posts_cleanup cron")

    # ─── bot_posted_mids TTL cleanup (daily @ 04:30 МСК = 01:30 UTC) ───
    try:
        from db import cleanup_old_bot_mids
        ad_cfg = config.get("ad_detection", {}) or {}
        ttl_h = int(ad_cfg.get("bot_mids_ttl_hours", 48))
        scheduler.add_job(
            lambda: cleanup_old_bot_mids(ttl_hours=ttl_h),
            'cron', hour=1, minute=30,
            name='bot_posted_mids_cleanup',
        )
    except Exception:
        logger.exception("Failed to register bot_posted_mids_cleanup cron")

    scheduler.start()

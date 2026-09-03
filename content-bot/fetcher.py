"""
Сбор контента из Telegram → БД.
Медиа скачивается в telegram.py сразу при парсинге.

Поддерживает мультисеточную архитектуру:
  fetch_grid(config, grid_name) — собирает контент для конкретной сетки
  fetch_all(config)             — собирает для всех сеток (обратная совместимость)
"""
import logging
import os
import random
import re
import sqlite3
from difflib import SequenceMatcher

import db
import cost_log
_COST_SERVICE = os.environ.get("CB_SERVICE_NAME", "content-bot")
from utils import load_config, grid_settings as _grid_settings_util
# Re-export _matches_profanity и PROFANITY_REGEX_PATTERNS для совместимости
# с прежними импортами в scheduler.py и news_realtime_engine.py.
from processor.filter import (  # noqa: F401
    should_skip,
    _matches_profanity,
    PROFANITY_REGEX_PATTERNS,
)
from sources import telegram

logger = logging.getLogger(__name__)


def _effective_fetch_filter(ch_cfg, grid_filter):
    """Фильтр ДОБОРА, отдельно от фильтра публикации.

    26.08: вязанию добавили видео-доноров, а видео так и не пришли — добор
    наполняется с первых источников списка, и фото-доноры съедают лимит.
    fetch_media_mode сужает именно сбор (например до video), не трогая
    публикацию: фото из очереди остаются подушкой, слоты не горят.
    """
    fm = (ch_cfg or {}).get("fetch_media_mode")
    if fm and fm != "any":
        return fm
    ch_media = (ch_cfg or {}).get("media_mode")
    return ch_media if ch_media and ch_media != "any" else grid_filter


def _is_skip_marker_text(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return bool(re.search(r"\bskip_military\b", t))


def _cleanup_rewrite_text(text: str) -> str:
    t = (text or "").strip()
    # частая ошибка LLM: "?." / "!." (в т.ч. с пробелом: "! .")
    t = re.sub(r"([?!])\s*\.(?=\s|$)", r"\1", t)
    t = re.sub(r"\s+", " ", t) if "\n" not in t else t
    return t.strip()


def _matches_unwanted_news_text(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е")
    if not t:
        return False
    bad = [
        r"\bприятн\w*\s+сн\w*\b",
        r"\bдобр(?:ой|ого)?\s+ноч\w*\b",
        r"\bдобр(?:ое|ого)?\s+утр\w*\b",
        r"\bспокойн\w*\s+ноч\w*\b",
        r"\bшикарн\w*\s+закат\b",
        r"\b(?:еще\s+один\s+)?закат\b.*\bсегодня\b",
        r"\bпроголос\w*\s+за\s+канал\b",
        r"\bприслать\s+новост\w*\b",
        r"\bприсылайте\s+(?:нам\s+)?(?:новост\w*|информац\w*|фото|видео)\b",
        r"\bздравствуй(?:те)?\b",
        r"\bвебинар\b",
        r"\bконкурс\b",
        r"\bучаствуй(?:те)?\b",
        r"\bпереходи(?:те)?\b",
        r"\bнажимай(?:те)?\b",
        r"\bбот\b.*\bтокен\b",
        r"\b\$[a-z]{2,}\b",
        r"\b\d{1,2}\s*\$\b",
        r"\bтелефон\s*:\s*\+?\d",
        r"\bмои\s+контакты\b",
        r"webinar\.",
        r"vk\.com/",
    ]
    return any(re.search(p, t, flags=re.IGNORECASE) for p in bad)


def _looks_too_short_or_broken(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    plain = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*", "", t)
    plain = re.sub(r"\s+", " ", plain).strip()
    # "Улицу." и подобные обрывки
    words = re.findall(r"[a-zа-яё0-9-]+", plain, flags=re.IGNORECASE)
    return len(words) <= 2 or len(plain) < 18


def _is_old_explicit_date(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е")
    m = re.search(r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", t)
    if not m:
        return False
    from datetime import datetime, timezone
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
        "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    d = int(m.group(1)); mo = months[m.group(2)]
    now = datetime.now(timezone.utc)
    try:
        dt = datetime(now.year, mo, d, tzinfo=timezone.utc)
    except Exception:
        return False
    return dt.date() < now.date()


def _is_near_duplicate_pending_same_source(channel: str, source_id: str, text: str) -> bool:
    """Soft-dedup: ловит почти-такой-же пост в окне последних дней.

    Расширено: проверяем не только pending, но и posted за 7 дней (same-source)
    и 3 дня (любой источник в канале). Это ловит когда rewriter создаёт
    разный MD5-хеш из одного и того же контента (например, один и тот же
    рецепт переписан с разной пунктуацией/порядком слов).
    """
    target = _normalize_for_match(text)
    if len(target) < 40:
        return False
    conn = db.get_conn()
    try:
        rows_same = conn.execute(
            """
            SELECT COALESCE(rewritten_text, original_text, "")
            FROM posts
            WHERE channel=? AND source_id=?
              AND status IN ("pending", "posted")
              AND COALESCE(posted_at, created_at) >= datetime("now", "-7 days")
            ORDER BY id DESC LIMIT 50
            """,
            (channel, source_id),
        ).fetchall()
        rows_any = conn.execute(
            """
            SELECT COALESCE(rewritten_text, original_text, "")
            FROM posts
            WHERE channel=?
              AND status IN ("pending", "posted")
              AND COALESCE(posted_at, created_at) >= datetime("now", "-3 days")
            ORDER BY id DESC LIMIT 100
            """,
            (channel,),
        ).fetchall()
    finally:
        conn.close()
    # Same-source: жёсткий порог 0.85 (источник часто репостит)
    for (cand_raw,) in rows_same:
        cand = _normalize_for_match(cand_raw or "")
        if len(cand) < 40:
            continue
        if SequenceMatcher(None, target, cand).ratio() >= 0.85:
            return True
    # Cross-source: мягкий 0.90
    for (cand_raw,) in rows_any:
        cand = _normalize_for_match(cand_raw or "")
        if len(cand) < 40:
            continue
        if SequenceMatcher(None, target, cand).ratio() >= 0.90:
            return True
    return False


# 2026-05-14: разделение на phase для архитектуры "URL = задача Claude rewriter".
# PRE — применяется к raw text ДО rewriter, ловит то что Claude не исправит
#       (явный spam intent + invite-only ссылки + @mentions).
# POST — применяется к final_text ПОСЛЕ rewriter, ловит всё включая обычные
#        URL как defense-in-depth (если Claude не вычистил).
# Backward compat: ANTI_AD_REGEX_PATTERNS = POST (наиболее строгий, как раньше),
# default phase='post' для старых вызовов из scheduler.py.

# Группа 1: invite-only / spam links — реальные индикаторы спама.
_ANTI_AD_INVITE_PATTERNS = [
    r"(?:ya\.cc/\S+|t\.me/\+|t\.me/joinchat)",
]

# Группа 2: @mentions — почти всегда промо/призыв подписаться в новостных
# raw-постах. Claude не превратит "@spam_channel реклама" в полезную новость.
_ANTI_AD_MENTIONS_PATTERNS = [
    r"@\w{4,}",
]

# Группа 3: spam intent — коммерция, призывы к подписке. Claude не превратит
# рекламу в новость, поэтому отбрасываем сразу до rewrite.
_ANTI_AD_SPAM_INTENT_PATTERNS = [
    r"\bподпи(?:шись|шитесь|шемся|сывайся|сывайтесь|шис)\b",  # narrowed 2026-06-18: CTA only
    r"\bкупи(?:те)?\b",  # narrowed 2026-06-18: imperative only
    r"\bскидк\w*\b",
    r"\b(?:закаж\w+|заказат\w+|заказыва\w+|заказан\w+|закажите)\b",  # narrowed (2026-05-07): не ловит "заказчик/заказали в материале"
    r"\bреклам\w*\b",
    r"\bпромок\w*\b",
    r"\b(?:пят[её]рочк\w*|магнит(?:[ауеыом]|ов|ам|ах|ами)?|fix\s*price|ozon|wildberries|wb|steam)\b",
    r"\bартикул\w*\b",
    r"\bподгузник\w*\b",
    r"\b(?:скидк|купи|закаж|акция|цена|стоит|за\s+всего)\w*[\s\S]{0,40}?\d{2,5}\s*(?:[.,]\s*\d{1,2})?\s*(?:₽|руб(?:\.|лей|ля)?)\b",  # narrowed (2026-05-07): только в коммерческом контексте
    r"market\.yandex\.ru",
    r"\berid\b",
    r"(?:подсчет|подсч[её]т)\s+калори",
    r"\bбжу\b",
    r"сфоткал\s+тарелку",
    r"первые\s+\d+\s+фото\s+каждый\s+день\s+—?\s*бесплатно",
]

# Группа 4: cleanable by Claude — обычные URL. Промт Claude rewriter:
# "без хештегов, без ссылок, без упоминаний каналов". Применяем только POST
# как defense-in-depth страховку.
# Узкий regex для www\.: требуем домен (минимум "www.X.Y") чтобы упоминание
# "www" в тексте без домена не блокировало (issue 2026-05-14: Волжский,
# Люберцы, Архангельск, Якутск страдали от этого).
_ANTI_AD_CLEANABLE_PATTERNS = [
    r"(?:https?://|www\.[a-z0-9-]{2,}\.[a-z]{2,})",
]

# PRE-rewrite: invite + mentions + spam intent (Claude не исправит).
ANTI_AD_REGEX_PATTERNS_PRE = (
    _ANTI_AD_INVITE_PATTERNS + _ANTI_AD_MENTIONS_PATTERNS + _ANTI_AD_SPAM_INTENT_PATTERNS
)

# POST-rewrite: PRE + cleanable URL (defense, если Claude не вычистил).
ANTI_AD_REGEX_PATTERNS_POST = (
    _ANTI_AD_INVITE_PATTERNS + _ANTI_AD_MENTIONS_PATTERNS
    + _ANTI_AD_SPAM_INTENT_PATTERNS + _ANTI_AD_CLEANABLE_PATTERNS
)

# Backward compat: старое имя = POST (наиболее строгое).
ANTI_AD_REGEX_PATTERNS = ANTI_AD_REGEX_PATTERNS_POST

BLACKLIST_PHRASES = [
    "читать далее в источнике",
    "переходи по ссылке",
    "подробности в описании",
    "все новости в нашем канале",
    "оперативно о чп",
    "в наших профильных каналах",
    "срочно тольятти",
    "чп тольятти",
]

# Глобальный анти-мат теперь живёт в processor.filter.PROFANITY_REGEX_PATTERNS
# (импортирован выше для обратной совместимости).


def _get_groq_client(config: dict):
    key = config.get("groq", {}).get("api_key", "")
    if not key:
        return None
    try:
        from groq import Groq
        return cost_log.make_client("groq", service=_COST_SERVICE, purpose="rewrite", api_key=key)
    except Exception as e:
        logger.warning(f"Не удалось инициализировать Groq: {e}")
        return None


def _get_ai_client_for_provider(config: dict, provider: str):
    """Возвращает AI-клиент для конкретного провайдера."""
    if provider == "groq":
        return _get_groq_client(config), "groq"
    if provider == "claude":
        key = config.get("anthropic", {}).get("api_key", "") or config.get("ai", {}).get("claude", {}).get("api_key", "")
        if key:
            try:
                import anthropic
                return cost_log.make_client("claude", service=_COST_SERVICE, purpose="rewrite", api_key=key), "claude"
            except Exception as e:
                logger.warning(f"Claude недоступен: {e}")
    if provider == "openai":
        key = config.get("openai", {}).get("api_key", "")
        if key:
            try:
                import openai
                return cost_log.make_client("openai", service=_COST_SERVICE, purpose="rewrite", api_key=key), "openai"
            except Exception as e:
                logger.warning(f"OpenAI недоступен: {e}")
    # Fallback на Groq
    return _get_groq_client(config), "groq"


def _get_ai_client(config: dict):
    """Возвращает активный AI-клиент (Groq / Claude / OpenAI / Gemini)."""
    active = config.get("ai", {}).get("active_provider", "groq")

    if active == "groq":
        return _get_groq_client(config), "groq"

    if active == "claude":
        key = config.get("ai", {}).get("claude", {}).get("api_key", "")
        if key:
            try:
                import anthropic
                return cost_log.make_client("claude", service=_COST_SERVICE, purpose="rewrite", api_key=key), "claude"
            except Exception as e:
                logger.warning(f"Claude недоступен: {e}")

    if active == "openai":
        key = config.get("ai", {}).get("openai", {}).get("api_key", "")
        if key:
            try:
                import openai
                return cost_log.make_client("openai", service=_COST_SERVICE, purpose="rewrite", api_key=key), "openai"
            except Exception as e:
                logger.warning(f"OpenAI недоступен: {e}")

    # Fallback на Groq
    return _get_groq_client(config), "groq"


def _clean(text: str) -> str:
    """Базовая очистка без AI.

    CLEAN_EXTEND (2026-05-07): расширено удаление шаблонных артефактов,
    которые иначе ловятся regex-фильтрами как false-positives:
      - markdown-картинки ![Alt](url)
      - date-stamps "7 мая 2026 г. 20:31"
      - "Читайте также: ...", "Источник: ...", "Фото: ...", "Видео: ..."
    """
    if not text:
        return ""
    # markdown-картинки в начале или внутри текста
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text)
    # date-stamps типа "7 мая 2026 г. 20:31" / "07.05.2026 14:00"
    text = re.sub(r'\b\d{1,2}\s+(?:янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)\w*\s+\d{4}\s*г?\.?\s*\d{1,2}[:.]\d{2}\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{1,2}\.\d{1,2}\.\d{4}\s+(?:в\s+)?\d{1,2}[:.]\d{2}\b', '', text)
    # "Читайте также:..." до конца строки
    text = re.sub(r'(?im)^\s*читайт[еь]?\s+(?:также|еще|ещё|по\s+теме)[\s:].*$', '', text)
    # "Источник: ..." до конца строки
    text = re.sub(r'(?im)^\s*источник[\s:].*$', '', text)
    # "Фото: …" / "Фото из …" / "Фото пресс-службы …" — single line
    text = re.sub(r'(?im)^\s*фото[\s:].*$', '', text)
    text = re.sub(r'(?im)^\s*видео[\s:].*$', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\bwww\.\S+', '', text)  # A2: bare www domains
    # F (lajv audit 2026-06-20): agency dateline e.g. "РЕСПУБЛИКА БУРЯТИЯ, /НИА-БУРЯТИЯ/."
    # embedded right after the headline makes the rewriter collapse (echoes it as the
    # body). Measured: 6/6 collapse with it, 0/4 without. Strip CAPS-region + /AGENCY/.
    text = re.sub(r'[А-ЯЁ][А-ЯЁ \-]{3,40},\s*/[^/\n]{2,40}/\.?\s*', '', text)
    # 2026-05-18: Handle-style names "username.something.something" (Instagram-like creator credits)
    # Example: 'pletenie.s.nastia' published as standalone line — это credit чужого канала
    # REQUIRE 2+ dots чтобы не false-positive на 'file.txt', 'site.ru', etc.
    text = re.sub(
        r'(?im)^[ \t]*[a-z][a-z0-9_]{1,}(?:\.[a-z0-9_]+){2,}\.?[ \t]*$',
        '', text
    )
    # 2026-05-18: Cross-channel branding "Мы в MAX/МАХ/Дзене/ОК/VK/Телеграм"
    text = re.sub(
        r'(?im)^.*?\bмы\s+в\s+(?:MAX|МАХ|telegram|telegramm?|телеграм\w*|tg|вк|вконтакте|дзен\w*|ок|одноклассник\w*|youtube|ютуб\w*|instagram|инстаграм\w*)\b.*$',
        '', text
    )
    # 2026-05-18 + F5 (24.06): ALL-CAPS заголовки (vyazalruk/bezformata пишут
    # кричащим капсом). Capitalize первую букву, остальное вниз.
    # F5 acronym-guard: НЕ трогаем сегменты с акронимами — capitalize сломал бы их
    # (ФСБ→Фсб, США→Сша, NASA→Nasa). Порог понижен до 4 букв (покрыть короткие
    # города-заголовки ХИМКИ/ОМСК), guard защищает от over-reach.
    _CAPS_ACRONYMS = frozenset({
        "ФСБ", "МЧС", "ГИБДД", "ГАИ", "США", "ООН", "РФ", "СССР", "ТАСС", "ВЦИОМ",
        "ДТП", "ЧП", "РПЦ", "НАТО", "СК", "МВД", "ФНС", "ЦБ", "УВД", "ФСИН", "СКР",
        "ВОЗ", "ПВО", "ВСУ", "ЖКХ", "ТЭЦ", "АЭС", "ГЭС", "ВВП", "НДС", "ООО",
        "ЗАГС", "ПДД", "РЖД", "ВТБ", "МФЦ", "ФМС", "ФСО", "РАН", "МГУ", "ЕГЭ",
        "ОГЭ", "СВО", "ЛНР", "ДНР", "ОАЭ", "КНР", "КНДР", "МКС", "РИА", "ВДВ", "ОМОН",
    })

    def _caps_has_acronym(seg):
        # известный кириллический акроним ИЛИ латинская аббревиатура (≥2 заглавных)
        for w in re.findall(r'[A-ZА-ЯЁ]{2,}', seg):
            if w in _CAPS_ACRONYMS or re.fullmatch(r'[A-Z]{2,}', w):
                return True
        return False

    def _normalize_caps_line(m):
        line = m.group(0)
        if (line.upper() == line and sum(1 for c in line if c.isalpha()) >= 4
                and not _caps_has_acronym(line)):
            return line.capitalize()
        return line
    text = re.sub(r'(?m)^[А-ЯЁA-Z][А-ЯЁA-Z\s«»",.!?\-]{3,}$', _normalize_caps_line, text)
    # ALL-CAPS заголовок СЛИТ с телом в одной строке через ". "
    # (bezformata: "ПРИМЕМ СТОЧНЫЕ ВОДЫ... . На территории..."). Regex выше требует
    # CAPS до конца строки ($) и такой кейс пропускает. Нормализуем ведущий
    # CAPS-сегмент до первого [.!?]+пробел (тоже с acronym-guard).
    def _normalize_caps_prefix(m):
        seg = m.group(1)
        if (seg.upper() == seg and sum(1 for c in seg if c.isalpha()) >= 4
                and not _caps_has_acronym(seg)):
            return seg.capitalize() + m.group(2)
        return m.group(0)
    text = re.sub(r'(?m)^([А-ЯЁA-Z][А-ЯЁA-Z\s«»"\-]{3,}?)([.!?]\s)', _normalize_caps_prefix, text)
    text = re.sub(r'[•·]\s*Подписат[а-я]+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'Подписат[а-я]+\s+в\s+\S+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'Подпиш[а-я]+\s+на\s+.*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'подпи(?:шись|шитесь|шемся|сывайся|сывайтесь|шис)[\s\S]*', '', text, flags=re.IGNORECASE)  # A2: CTA footer any form
    text = re.sub(r'(?im)^.*читай(?:те)?\s+.*\bв\s+(?:max|мах)\b.*$', '', text)  # A2: chitayte v MAX mirror
    text = re.sub(r'^(?:🔴\s*)?Подписаться\s*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^(?:\|\s*)?Предложить\s+новост[ьи].*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^(?:\|\s*)?Написать\s+нам.*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^(?:\|\s*)?Читать\s+далее.*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^\s*Смотрет[ьь]\s*$', '', text, flags=re.MULTILINE)
    # хвосты саморекламы локальных каналов (ЧП Тольятти и аналоги)
    text = re.sub(r'(?im)^.*оперативно\s+о\s+чп.*$', '', text)
    text = re.sub(r'(?im)^.*в\s+наших\s+профильных\s+каналах.*$', '', text)
    text = re.sub(r'(?im)^.*срочно\s+тольятти.*$', '', text)
    text = re.sub(r'(?im)^.*чп\s+тольятти.*$', '', text)
    # === Template-tail patterns (added 2026-05-05) — удалить шаблонные хвосты-CTA ===
    text = re.sub(r'(?im)^.*\b(?:заказать\s+рекламу|по\s+вопросам\s+рекламы|реклама\s+у\s+нас)\b.*$', '', text)  # A3 ad-inquiry
    text = re.sub(r'(?im)^.*\b(?:расскажи\s+(?:свою\s+)?новость|напиши\s+нам|пиши\s+нам)\b.*$', '', text)  # A4 send-news
    text = re.sub(r'(?im)^.*\b(?:смотри\s+(?:тут|здесь)|смотри\s+ниже)\b.*$', '', text)  # A5 see-here-cta
    text = re.sub(r'(?im)^.*\b(?:если\s+не\s+(?:грузит|откр)|дублиру\w+\s+в)\b.*$', '', text)  # A6 fallback-mirror
    text = re.sub(r'(?im)^.*?\b(?:прислать\s+новость|присылайте\s+(?:нам\s+)?(?:новост|информаци))\b.*$', '', text)  # A7 send-us-news (footer @chp_nv_86, @yoshka12_chp, @tambov68_chp — sanity 2026-05-05: 41 удалений / 0 overshoot)
    lines = [l for l in text.split('\n') if sum(c.isalpha() for c in l) >= 3 or not l.strip()]
    text = '\n'.join(lines)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _source_fetch_order(tg_channels, backup_sources, do_shuffle, rng=None):
    """Порядок опроса tg-доноров канала.

    DL 2026-07-31 (Дом Повара): цикл фетча break'ается при заполненной очереди,
    поэтому при фиксированном порядке хвост списка не опрашивается НИКОГДА
    (retsepty4/dom_resept/h824tty/ppreceptiki молчали с 26.04 при живых каналах).
    do_shuffle (grid_settings.fair_source_rotation, опт-ин — порядок городских
    сеток не трогаем) перемешивает primary-доноров, чтобы каждый получал шанс.
    backup_sources (channel_settings.<ch>.backup_sources) всегда в конце:
    резерв фетчится только при недоборе от остальных.
    """
    backup_set = set(backup_sources or ())
    primary = [c for c in tg_channels if c not in backup_set]
    backup = [c for c in tg_channels if c in backup_set]
    if do_shuffle:
        (rng or random).shuffle(primary)
    return primary + backup


def _grid_settings_util(config: dict, grid_name: str) -> dict:
    """Настройки сетки с fallback на глобальные."""
    global_sched = config.get("schedule", {})
    grid_cfg = config.get("grid_settings", {}).get(grid_name, {})
    return {
        "posts_per_day": grid_cfg.get("posts_per_day", global_sched.get("posts_per_day", 8)),
        "media_type":    grid_cfg.get("media_type", "any"),
        "rewrite":       grid_cfg.get("rewrite", config.get("rewrite", {}).get("enabled", True)),
    }


def fetch_grid(config: dict, grid_name: str) -> int:
    """Собирает контент для всех каналов указанной сетки."""
    from datetime import datetime, timezone, timedelta

    db.init_db()
    sources_cfg = config.get("sources", {})
    grid_channels = config.get("grids", {}).get(grid_name, [])
    settings = _grid_settings_util(config, grid_name)
    posts_per_channel = settings["posts_per_day"]
    queue_target = config.get("grid_settings", {}).get(grid_name, {}).get("queue_target", posts_per_channel)
    media_type_filter = settings["media_type"]
    do_rewrite = settings["rewrite"]
    live_mode = settings.get("live_mode", False)
    grid_prompt = config.get("grid_settings", {}).get(grid_name, {}).get("prompt", "") or ""
    grid_max_tokens = config.get("grid_settings", {}).get(grid_name, {}).get("max_tokens", None)
    # Провайдер AI для этой сетки (приоритет над глобальным)
    global_provider = config.get("ai", {}).get("active_provider",
                      config.get("rewrite", {}).get("model", "groq"))
    grid_ai_provider = config.get("grid_settings", {}).get(grid_name, {}).get("ai_provider", "") or global_provider
    ai_client, ai_provider = _get_ai_client_for_provider(config, grid_ai_provider)

    # Для живого режима: лимит 1 пост на канал, только свежие (не старше 1 часа)
    if live_mode:
        posts_per_channel = 1
        max_age_hours = config.get("grid_settings", {}).get(grid_name, {}).get("live_max_age_hours", 1)
        min_pub_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    else:
        min_pub_time = None

    added_total = 0

    # Каналы с источниками в этой сетке
    active_channels = [
        ch for ch in grid_channels
        if any(len(v) > 0 for v in sources_cfg.get(ch, {}).values())
    ]

    if not active_channels:
        logger.info(f"[{grid_name}] Нет каналов с источниками — пропускаем")
        return 0

    channel_settings = config.get("channel_settings", {})
    for niche in active_channels:
        # ВАЖНО: очередь pending никогда не чистим перед добором.
        # Добор только добавляет новые посты после дедуп-проверок.

        # Для обычного режима: держим очередь ровно до дневного таргета (по умолчанию posts_per_day)
        if not live_mode:
            pending_now = db.count_pending_posts(niche)
            need_add = max(0, int(queue_target) - int(pending_now))
            if need_add == 0:
                logger.info(f"[{grid_name}][{niche}] Очередь {pending_now}/{queue_target} — добор не нужен")
                continue
            channel_limit = need_add
        else:
            channel_limit = posts_per_channel

        # Настройка медиа: канал → сетка (канал имеет приоритет)
        ch_cfg = channel_settings.get(niche, {}) or {}
        effective_filter = _effective_fetch_filter(ch_cfg, media_type_filter)
        ch_prompt = (ch_cfg.get("rewrite_prompt") or "").strip()

        added = 0
        tg_channels = list(sources_cfg.get(niche, {}).get("telegram", []) or [])
        web_sources  = list(sources_cfg.get(niche, {}).get("web", []) or [])
        # Ротация primary-доноров + резервные в конец (см. _source_fetch_order).
        _fair = bool(config.get("grid_settings", {}).get(grid_name, {})
                     .get("fair_source_rotation", False))
        tg_channels = _source_fetch_order(
            tg_channels, ch_cfg.get("backup_sources") or (), _fair)

        # Telegram-источники.
        # download_media=True: для legacy-сетки скачиваем медиа локально, чтобы
        # pending в БД не протухали (cdn-telegram URL живут 1-2 часа).
        # Лайв-сетка использует news_realtime_engine с download_media=False (default).
        for channel in tg_channels:
            if added >= channel_limit:
                break
            try:
                _fp = _fetch_pages_for(config, grid_name, channel)
                _max_age = _source_max_age_days(config, channel)
                posts = _fetch_source_posts(channel, _fp, niche)
                for p in posts:
                    if added >= channel_limit:
                        break
                    if _post_out_of_window(p.get("pub_time"), _max_age):
                        continue
                    # Фильтр по возрасту — ТОЛЬКО свежие посты
                    if min_pub_time is not None:
                        pub_time = p.get("pub_time")
                        if pub_time is None:
                            # Нет времени публикации — пропускаем
                            continue
                        if pub_time < min_pub_time:
                            # Пост старше лимита — все следующие тоже старые, стоп
                            break
                    # Фильтр по типу медиа
                    if effective_filter == "video" and p.get("media_type") != "video":
                        continue
                    if effective_filter == "photo" and p.get("media_type") != "photo":
                        continue
                    if effective_filter == "text" and p.get("media_url"):
                        continue
                    if effective_filter == "require_media" and not p.get("media_url"):
                        continue
                    added += _save(
                        p, channel, niche,
                        ai_client, ai_provider, do_rewrite,
                        grid_prompt, grid_max_tokens,
                        ch_prompt,
                    )
            except Exception as e:
                logger.error(f"[{niche}] @{channel}: {e}")

        # Web/RSS источники (если не добрали)
        if added < posts_per_channel and web_sources:
            for url in web_sources:
                if added >= channel_limit:
                    break
                try:
                    posts = _fetch_rss(url)
                    for p in posts:
                        if added >= channel_limit:
                            break
                        # Фильтр по возрасту (живой режим)
                        if min_pub_time is not None:
                            pub_time = p.get("pub_time")
                            if pub_time is None or pub_time < min_pub_time:
                                continue
                        added += _save(
                            p, url, niche,
                            ai_client, ai_provider, do_rewrite,
                            grid_prompt, grid_max_tokens,
                            ch_prompt,
                        )
                except Exception as e:
                    logger.error(f"[{niche}] RSS {url}: {e}")

        logger.info(f"[{grid_name}][{niche}] +{added}/{channel_limit}")
        added_total += added

    return added_total


_MAX_FEED_DB = os.environ.get(
    "MAX_FEED_DB", "/home/openclaw/.openclaw/workspace/max-userbot/max_feed.db")


def _fetch_max_feed(chat_id: int) -> list:
    """Кандидаты из общей max_feed.db (наполняет max-userbot/max_bake_feed.py).

    Формат совпадает с telegram.fetch_channel, чтобы дальше по конвейеру
    ничего не различало источник. media_url помечен mxfile://<путь> — файл
    лежит в кэше юзербота и копируется в наш при скачивании.
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt, timezone as _tz
    out = []
    if not os.path.exists(_MAX_FEED_DB):
        return out
    try:
        conn = _sq.connect(_MAX_FEED_DB)
        rows = conn.execute(
            "SELECT msg_id, text, media_type, media_path, is_ad, pub_ts, "
            "views, reactions FROM max_feed WHERE chat_id=? AND is_ad=0 "
            "AND media_path IS NOT NULL ORDER BY msg_id DESC LIMIT 200",
            (chat_id,)).fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"max_feed read {chat_id}: {e}")
        return out
    for msg_id, text, mtype, mpath, is_ad, pub_ts, views, reactions in rows:
        out.append({
            "source_url": f"max://{chat_id}/{msg_id}",
            "text": text or "",
            "media_type": mtype,
            "media_url": ("mxfile://" + mpath) if mpath else None,
            "media_files": (["mxfile://" + mpath] if mpath else []),
            "pub_time": (_dt.fromtimestamp(pub_ts, _tz.utc) if pub_ts else None),
            "views": views or 0, "reactions": reactions or 0, "forwards": None,
        })
    return out


def _materialize_mxfile(m_url, media_type):
    """mxfile://<путь юзербота> -> копия в НАШЕМ кэше (или None, если файла нет).

    Своя копия обязательна: юзербот чистит свои файлы быстрее, чем очередь
    доходит до слота, иначе публикация падает в media_file_missing.
    """
    if not m_url or not str(m_url).startswith("mxfile://"):
        return None
    import hashlib as _hl
    import shutil as _sh
    src = str(m_url)[len("mxfile://"):]
    dst_dir = telegram.MEDIA_DIR
    ext = ".mp4" if media_type == "video" else ".jpg"
    dst = os.path.join(dst_dir, _hl.md5(str(m_url).encode()).hexdigest() + ext)
    try:
        if os.path.exists(dst) and os.path.getsize(dst) > 1000:
            return dst                      # уже копировали
        if os.path.exists(src) and os.path.getsize(src) > 1000:
            os.makedirs(dst_dir, exist_ok=True)
            _sh.copy2(src, dst)
            return dst
    except Exception as e:
        logger.warning(f"mxfile copy fail {src}: {e}")
    return None


def _fetch_source_posts(channel, pages, niche=None):
    """Один донор -> список кандидатов. Понимает префикс mx: (каналы MAX).

    16.08 выпечка встала именно потому, что mx:-донор уходил в telegram-фетчер
    как имя канала и молча возвращал пусто.
    """
    if str(channel).startswith("mx:"):
        try:
            posts = _fetch_max_feed(int(str(channel)[3:]))
        except (TypeError, ValueError):
            logger.error(f"[{niche}] кривой mx-донор: {channel}")
            return []
        for p in posts:
            local = _materialize_mxfile(p.get("media_url"), p.get("media_type"))
            if local:
                p["media_url"] = local
                p["media_files"] = [local]
            else:
                p["media_url"] = None       # файл вычищен — пусть отсеет фильтр
                p["media_files"] = []
        return posts
    return telegram.fetch_channel(channel, max_posts=pages * 20, max_pages=pages,
                                  download_media=True)


def fetch_channel(config: dict, niche: str, limit: int | None = None, grid_name: str | None = None) -> int:
    """Собирает контент для одного канала (niche).

    Нужен для on-demand добора в scheduler, чтобы не дёргать всю сетку.
    """
    from datetime import datetime, timezone, timedelta

    db.init_db()
    sources_cfg = config.get("sources", {})
    if niche not in sources_cfg:
        return 0

    # Определяем сетку канала, если явно не передали
    if not grid_name:
        for g, arr in (config.get("grids", {}) or {}).items():
            if niche in (arr or []):
                grid_name = g
                break

    settings = _grid_settings_util(config, grid_name) if grid_name else {
        "posts_per_day": 8,
        "media_type": "any",
        "rewrite": config.get("rewrite", {}).get("enabled", True),
    }

    posts_per_channel = int(settings.get("posts_per_day", 8) or 8)
    media_type_filter = settings.get("media_type", "any") or "any"
    do_rewrite = bool(settings.get("rewrite", True))

    grid_cfg = (config.get("grid_settings", {}).get(grid_name, {}) if grid_name else {}) or {}
    queue_target = int(grid_cfg.get("queue_target", posts_per_channel) or posts_per_channel)
    if limit is None:
        pending_now = db.count_pending_posts(niche)
        limit = max(0, queue_target - pending_now)
    limit = int(max(0, limit))
    if limit == 0:
        return 0

    global_provider = config.get("ai", {}).get("active_provider", config.get("rewrite", {}).get("model", "groq"))
    grid_ai_provider = grid_cfg.get("ai_provider", "") or global_provider
    ai_client, ai_provider = _get_ai_client_for_provider(config, grid_ai_provider)
    grid_prompt = grid_cfg.get("prompt", "") or ""
    grid_max_tokens = grid_cfg.get("max_tokens", None)

    # Live-mode ограничения (если канал в live-сетке)
    live_mode = bool(grid_cfg.get("live_mode", False)) if grid_name else False
    if live_mode:
        max_age_hours = int(grid_cfg.get("live_max_age_hours", 1) or 1)
        min_pub_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    else:
        min_pub_time = None

    channel_settings = config.get("channel_settings", {}) or {}
    ch_cfg = channel_settings.get(niche, {}) or {}
    effective_filter = _effective_fetch_filter(ch_cfg, media_type_filter)
    ch_prompt = (ch_cfg.get("rewrite_prompt") or "").strip()

    added = 0
    tg_channels = (sources_cfg.get(niche, {}) or {}).get("telegram", []) or []
    web_sources = (sources_cfg.get(niche, {}) or {}).get("web", []) or []
    # Ротация primary-доноров + резервные в конец — как в fetch_grid
    # (см. _source_fetch_order; on-demand добор страдал тем же break'ом).
    _fair = bool(grid_cfg.get("fair_source_rotation", False))
    tg_channels = _source_fetch_order(
        tg_channels, ch_cfg.get("backup_sources") or (), _fair)

    for channel in tg_channels:
        if added >= limit:
            break
        try:
            _fp = _fetch_pages_for(config, grid_name, channel)
            _max_age = _source_max_age_days(config, channel)
            posts = _fetch_source_posts(channel, _fp, niche)
            for p in posts:
                if added >= limit:
                    break
                if _post_out_of_window(p.get("pub_time"), _max_age):
                    continue
                if min_pub_time is not None:
                    pub_time = p.get("pub_time")
                    if pub_time is None:
                        continue
                    if pub_time < min_pub_time:
                        break
                if effective_filter == "video" and p.get("media_type") != "video":
                    continue
                if effective_filter == "photo" and p.get("media_type") != "photo":
                    continue
                if effective_filter == "text" and p.get("media_url"):
                    continue
                if effective_filter == "require_media" and not p.get("media_url"):
                    continue

                added += _save(
                    p, channel, niche,
                    ai_client, ai_provider, do_rewrite,
                    grid_prompt, grid_max_tokens,
                    ch_prompt,
                )
        except Exception as e:
            logger.error(f"[{niche}] @{channel}: {e}")

    if added < limit and web_sources:
        for url in web_sources:
            if added >= limit:
                break
            try:
                posts = _fetch_rss(url)
                for p in posts:
                    if added >= limit:
                        break
                    if min_pub_time is not None:
                        pub_time = p.get("pub_time")
                        if pub_time is None or pub_time < min_pub_time:
                            continue
                    added += _save(
                        p, url, niche,
                        ai_client, ai_provider, do_rewrite,
                        grid_prompt, grid_max_tokens,
                        ch_prompt,
                    )
            except Exception as e:
                logger.error(f"[{niche}] RSS {url}: {e}")

    return added


def fetch_all(config: dict) -> int:
    """Собирает контент для всех сеток. Обратная совместимость."""
    db.init_db()
    total = 0
    grids = config.get("grids", {})
    if grids:
        for grid_name in grids:
            total += fetch_grid(config, grid_name)
    else:
        # Старый режим — если нет сеток в конфиге
        logger.warning("Нет сеток в конфиге, используем legacy-режим")
        logger.warning("Нет сеток в конфиге — fetch пропущен")
    return total




def _matches_stopwords(text: str, channel: str) -> bool:
    """Проверяет текст поста по стоп-словам из конфига (глобальные + сетки + канал)."""
    if not text:
        return False
    cfg = load_config()
    t = _normalize_for_match(text)

    def _sw_match(stopword, text_norm):
        wn = _normalize_for_match(stopword)
        if not wn:
            return False
        try:
            return bool(re.search(rf"\b{re.escape(wn)}\b", text_norm, flags=re.IGNORECASE))
        except re.error:
            return wn in text_norm

    # Глобальные стоп-слова
    global_sw = cfg.get("filters", {}).get("global", {}).get("stopwords", [])
    for w in global_sw:
        if _sw_match(w, t):
            logger.debug(f"[{channel}] Стоп-слово '{w}': пропускаем")
            return True

    # Стоп-слова сеток в которых состоит канал
    grids = cfg.get("grids", {})
    for grid_name, grid_channels in grids.items():
        if channel in grid_channels:
            grid_sw = cfg.get("filters", {}).get("grids", {}).get(grid_name, {}).get("stopwords", [])
            for w in grid_sw:
                if _sw_match(w, t):
                    logger.debug(f"[{channel}] Стоп-слово сетки '{w}': пропускаем")
                    return True

    # Стоп-слова канала
    ch_sw = cfg.get("filters", {}).get("channels", {}).get(channel, {}).get("stopwords", [])
    for w in ch_sw:
        if _sw_match(w, t):
            logger.debug(f"[{channel}] Стоп-слово канала '{w}': пропускаем")
            return True

    return False


def _normalize_for_match(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060\u00ad]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _stopword_to_regex(sw: str) -> str:
    """Преобразует stopword/фразу в regex с мягкой морфологией (работа -> работала и т.п.)."""
    s = _normalize_for_match(sw)
    if not s:
        return ""
    tokens = re.findall(r"[a-zа-яё0-9+/.-]+", s, flags=re.IGNORECASE)
    if not tokens:
        return ""
    parts = []
    for tok in tokens:
        # короткие токены (напр. "пво") матчим как есть
        if len(tok) <= 4:
            parts.append(rf"{re.escape(tok)}")
            continue
        # STEM_NARROW (2026-05-07): stem 5 символов вместо 4 — меньше false-positives.
        # Раньше: "приз" (stem=приз) ловил "призналась/призывник".
        #         "обстрел" (stem=обст) ловил "обстановка/обстоятельства".
        # Теперь: "обстрел" (stem=обстр) ловит только обстрел/обстреля.
        # Морфоформы коротких слов (война→войны→войне) лучше задавать
        # явно через regex_block, а не автогенерацией.
        stem = tok[:5]
        parts.append(rf"{re.escape(stem)}[a-zа-яё0-9_-]*")
    return r"\b" + r"\s+".join(parts) + r"\b"


def _collect_scoped_regex(channel: str, phase: str = "post") -> list[str]:
    cfg = load_config()
    patterns = []

    # unified DB store (global + grid + channel)
    try:
        from regex_store import get_patterns_for_channel
        patterns.extend(get_patterns_for_channel(channel, cfg))
    except Exception:
        pass

    # config as live overlay (чтобы изменения из UI работали сразу, даже без рестарта)
    patterns.extend(cfg.get("filters", {}).get("global", {}).get("regex_block", []) or [])
    for grid_name, grid_channels in (cfg.get("grids", {}) or {}).items():
        if channel in grid_channels:
            patterns.extend(cfg.get("filters", {}).get("grids", {}).get(grid_name, {}).get("regex_block", []) or [])
    patterns.extend(cfg.get("filters", {}).get("channels", {}).get(channel, {}).get("regex_block", []) or [])

    # mandatory global anti-ad — phase-dependent
    if phase == "pre":
        patterns.extend(ANTI_AD_REGEX_PATTERNS_PRE)
    else:
        patterns.extend(ANTI_AD_REGEX_PATTERNS_POST)

    # Для новостных сеток: автоматически подвязываем stopwords как regex
    # из global + grid + channel scope, чтобы ловить морфологические формы.
    is_news_channel = (
        channel in ((cfg.get("grids", {}) or {}).get("Города России", []) or [])
        or channel in ((cfg.get("grids", {}) or {}).get("Города лайв", []) or [])
        or channel in ((cfg.get("grids", {}) or {}).get("Города в MAX", []) or [])
    )
    if is_news_channel:
        # global stopwords
        for sw in (cfg.get("filters", {}).get("global", {}).get("stopwords", []) or []):
            rx = _stopword_to_regex(sw)
            if rx:
                patterns.append(rx)

        # grid stopwords (для всех сеток, где состоит канал)
        for grid_name, grid_channels in (cfg.get("grids", {}) or {}).items():
            if channel in (grid_channels or []):
                for sw in (cfg.get("filters", {}).get("grids", {}).get(grid_name, {}).get("stopwords", []) or []):
                    rx = _stopword_to_regex(sw)
                    if rx:
                        patterns.append(rx)

        # channel stopwords
        for sw in (cfg.get("filters", {}).get("channels", {}).get(channel, {}).get("stopwords", []) or []):
            rx = _stopword_to_regex(sw)
            if rx:
                patterns.append(rx)

    # dedupe keep order
    seen = set(); out = []
    for p in patterns:
        p = (p or "").strip()
        if not p or p in seen:
            continue
        seen.add(p); out.append(p)
    return out


def _matches_regex_block(text: str, channel: str, phase: str = "post") -> bool:
    t = _normalize_for_match(text)
    if not t:
        return False
    for p in _collect_scoped_regex(channel, phase=phase):
        try:
            m = re.search(p, t, flags=re.IGNORECASE | re.UNICODE)
            if m:
                # INSTRUMENTATION 2026-05-13: лог matched pattern + match для диагностики false positives
                try:
                    logger.info(
                        f"[REGEX_BLOCK_HIT] channel={channel} pattern={p[:80]!r} "
                        f"match={m.group(0)[:60]!r} text={t[:120]!r}"
                    )
                except Exception:
                    pass
                return True
        except re.error:
            continue
    return False




# Bug #14 (2026-05-21): Commercial/advertising filter for Дача-сетка legacy scheduler.
# Engine Live+MAX уже имеет Bug #7 ad detector через external_ad_posts; здесь блокируем
# рекламу маркетплейсов / приложений / промокодов которая прилетает из TG sources
# dacha_ogorod и sovet_dom (часто публикуют скрытую рекламу Яндекс Доставки, СберМаркета,
# мобильных приложений и т.п.).
_COMMERCIAL_PATTERNS = [
    # Cat 1: Яндекс-сервисы / маркетплейсы Яндекса
    (r"\b(?:Я́?ндекс|Yandex)[\s.\-]?(?:Доставк\w+|Маркет\w*|Лавк\w+|Ед[аыеу]|Такси|Карт[ыаеу]|Плюс|Музык\w+|Афиш\w+|Заправк\w+|Драйв|Путешеств\w+|Деньги|Касс\w+|Кинопоиск|Дзен|Алис[ауые]|GO|Бизнес|Браузер|Толок\w+|Cloud|Эфир)\b", "yandex"),
    # Cat 2: Другие маркетплейсы
    (r"\b(?:Wildberries|(?-i:Озон|Ozon)|AliExpress|АлиЭкспресс|Lamoda|МегаМаркет|СберМегаМаркет|СберМаркет|СберЛогистик\w+|СберПрайм|КупиВИП|ЛитРес|Aviasales|Booking|Циан|ЦИАН|RuStore)\b", "marketplace"),
    # Cat 3: Банки/финтех
    (r"\b(?:Тинькофф|Tinkoff|Альфа[\s-]?Банк|Сбербанк|СберЗдоровь\w+|ВТБ|Райффайзен|Россельхозбанк|Газпромбанк|Открытие\s+банк)\b", "bank"),
    # Cat 4: CTA "купи/закажи + товар/услугу"
    (r"\b(?:купи(?:те)?|закажи(?:те)?|оформи(?:те)?\s+заказ|приобрест[иь]|приобрет[еаи]те)\s+(?:сейчас|до|в|на|на сайте|по|товар|услугу|подарок|курс|подписк)", "cta_buy"),
    # Cat 5: Призывы скачать приложение
    (r"\b(?:скача(?:й|йте|ть|ем)|устано(?:ви|вите|вить)|открой(?:те)?)\s+(?:наше\s+)?приложен", "app_download"),
    (r"\b(?:Play\s*Market|App\s*Store|Google\s*Play|RuStore|App\s*Gallery|апп\s+стор)\b", "app_store"),
    # Cat 6: Скидки / акции / промокоды
    (r"\bпромо[\s-]?код\w*", "promocode"),
    (r"\bскидк[ауеи]\s+(?:до\s+)?\d+\s*%", "discount_pct"),
    (r"\b(?:акция|акции|распродаж[ауеиы])\s+(?:на|до|только|в|с)", "sale"),
    (r"\b(?:только\s+сегодня|только\s+у\s+нас|выгодно\s+купить|спецпредложен)\w*", "urgency"),
    # Cat 7: Логистика-промо (характерно для рекламы такси/доставки)
    (r"\bподач[аеи]\s+(?:от\s+)?\d+\s*мин\w*", "delivery_eta"),
    (r"\bподачей\s+от\s+\d+", "delivery_eta2"),
    (r"\bдоставк[аеи]\s+за\s+\d+\s*(?:мин|час)", "delivery_speed"),
    (r"\bбесплатн(?:ая|ой|о)\s+доставк", "free_delivery"),
    (r"\bкурьер\s+за\s+\d", "courier_speed"),
    (r"\bгрузови[чк]\w+\s+(?:под|для|разн)", "trucks_promo"),
    (r"\bгрузчик\w+", "movers_promo"),
    (r"\bМежгород\b", "intercity_promo"),
    (r"\bопци[яю][\s\-]+[«\"][\w\s\-]+[»\"]", "branded_option"),
    # Cat 8: Promo-ссылки (приложения / промо-каналы)
    (r"https?://(?:apps\.apple\.com|play\.google\.com|rustore\.ru|appgallery\.huawei|telegram\.me/[^/\s]+|t\.me/[^/\s]+)/\S+", "app_link"),
    (r"\b(?:переходи(?:те)?|перейди(?:те)?)\s+(?:по\s+)?ссылк", "cta_link"),
    # Cat 9: Service-commerce CTA
    (r"\bсервис\s+(?:для|перевозок|доставки|такси|каршеринга)", "service_promo"),
    (r"\b(?:вызов(?:ите)?|вызвать)\s+(?:машину|такси|курьера|мастера)\s+(?:за|с|через|от)", "service_call"),
    # Cat 10 (2026-06-17): бизнес-адверториал — локальная реклама услуг (контакт-CTA + сервис-оффер).
    # Дыра: реклама химчистки обуви (Люберцы) пролезла — дисклеймер был в картинке, текст «новостеподобный».
    (r"\bв\s+личк\w+\b", "adv_dm"),
    (r"\bв\s+л\.?\s?с\.?\b", "adv_ls"),
    (r"\bв\s+директ\w*\b", "adv_direct"),
    (r"\bзапиш(?:ись|итесь)\b", "adv_book"),
    (r"\bостав(?:ь|ьте|ить)\s+заявк\w+", "adv_request"),
    (r"\bзабронир\w+", "adv_reserve"),
    (r"\b(?:whats\s*app|вотсап\w*|ватсап\w*|вацап\w*)\b", "adv_whatsapp"),
    (r"\bсамовывоз\w*", "adv_pickup"),
    (r"\bдоставк[аеиуой]\s+курьер\w*", "adv_courier_delivery"),
    (r"\b(?:вызов(?:ите)?|вызвать)\s+курьер\w*", "adv_courier_call"),
    (r"\b(?:бесплатн\w+\s+выезд\w*|выезд\w*\s+бесплатн\w+)", "adv_free_visit"),
]


# --- Спонсорская реклама в донорском посте (2026-07-26) ---
# Дачники 26.07 11:07 / Дачный уголок 25.07 10:30: донор dacha_idei_reshenia
# прислал рекламную интеграцию мебельщиков («Получите PDF-файл в закреплённом
# сообщении канала / Они делают кухонную мебель в Москве / Ребята с высоким
# рейтингом — 4.9 на Яндексе»), и НИ ОДИН фильтр _save её не поймал
# (blacklist/profanity/regex_block/stopwords пропустили, _matches_commercial
# в legacy-пути не вызывается). Скоринг: сильный маркер = 2, слабый = 1,
# режем при score >= 2. Одиночный слабый маркер НЕ режет — «они делают гнёзда»
# (птицы), «схема в закреплённом сообщении» (легит-пост канала) живут.
_PROMO_STRONG_RE = [
    re.compile(r"получит[еь]\s+\S{0,20}\s*(?:pdf|файл|гайд|чек-?лист|инструкци|каталог|подборк)\w*"
               r"[^.!?\n]{0,40}(?:в\s+закрепл|по\s+ссылк|в\s+бот)", re.I),
    re.compile(r"(?:рейтинг\w*|оценк\w*)\s*[—–-]?\s*\d[.,]\d\b", re.I),
    re.compile(r"\bна\s+яндекс(?:е|\s*картах|\s*маркет)", re.I),
    re.compile(r"\bпо\s+промокод\w*\b", re.I),
    re.compile(r"\bскидк\w+\s+\d{1,2}\s*%", re.I),
]
_PROMO_WEAK_RE = [
    re.compile(r"в\s+закрепл[её]нн\w+\s+сообщени", re.I),
    re.compile(r"\bони\s+(?:делают|изготавл\w+|производ\w+|шьют|строят|устанавл\w+|монтир\w+)"
               r"[^.!?\n]{0,60}\bв\s+[А-ЯЁ][а-яё]{3,}", re.I),
    re.compile(r"должны\s+быть\s+у\s+вас\s+в\b", re.I),
    re.compile(r"\b(?:пишите|обращайтесь|звоните)\s+им\b", re.I),
    re.compile(r"\bзакажите\b|\bоставьте\s+заявк", re.I),
    re.compile(r"\bребята\s+с\s+высоким\b|\bпроверенн\w+\s+мастер", re.I),
    re.compile(r"\bбесплатн\w+\s+замер\b|\bпод\s+ключ\b", re.I),
]


def _source_max_age_days(config: dict, source: str):
    """Окно забора для конкретного донора: channel_settings.<донор>.max_age_days.

    2026-08-10: доноры da4nie_zametki (молчит 75 дней) и prodvorru (99) НЕ
    выключены по решению юзера — вместо этого им дана глубина 25 страниц и
    окно года, чтобы выбрать непрочитанный архив. None = без ограничения
    (поведение по умолчанию для живых доноров)."""
    cs = ((config.get("channel_settings", {}) or {}).get(source, {}) or {})
    v = cs.get("max_age_days")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _post_out_of_window(pub_time, max_age_days, now=None) -> bool:
    """True — пост старше окна забора (значит пропускаем).

    Fail-open: нет окна или нет даты поста -> берём (False)."""
    if not max_age_days or pub_time is None:
        return False
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = now or _dt.now(_tz.utc)
    pt = pub_time
    if pt.tzinfo is None:
        pt = pt.replace(tzinfo=_tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    return pt < (now - _td(days=int(max_age_days)))


def _fetch_pages_for(config: dict, grid_name, channel: str) -> int:
    """Глубина фетча (страниц) для канала: channel_settings.<ch>.fetch_pages ->
    grid_settings.<grid>.fetch_pages -> 3 (2026-07-28).

    Зачем per-channel: близнецам вязания нужен архив доноров (иначе делят один
    узкий пул), а остальным Дача-каналам глубина 8 вредна — фетч дотягивался до
    постов 30-104-дневной давности; окно фетч-дедупа 30 дней их пропускало, а
    claim_pending_post сверяет text_hash по ВСЕЙ истории -> 80% добытого
    отбраковывалось на выдаче, пул пустел, слоты пропадали."""
    def _norm(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 1 <= n <= 25 else (25 if n > 25 else None)

    ch_cfg = ((config.get("channel_settings", {}) or {}).get(channel, {}) or {})
    n = _norm(ch_cfg.get("fetch_pages"))
    if n:
        return n
    if grid_name:
        gr_cfg = ((config.get("grid_settings", {}) or {}).get(grid_name, {}) or {})
        n = _norm(gr_cfg.get("fetch_pages"))
        if n:
            return n
    return 3


def _crop_source_watermark(files, source_id: str, config: dict):
    """Срезать нижнюю полосу с ватермаркой у медиа указанных источников.

    Донор hand_knit ставит плашку «Ручки вяжут | t.me/hand_knit» внизу слева,
    стабильно на 93.2-93.4% высоты (замер 16 картинок) — режем нижние
    config.source_media_crop.<source>.bottom_pct процентов (12 по умолчанию
    для hand_knit; живой тест на посте «сумка PRADA» подтвердил).

    ИДЕМПОТЕНТНО: пишем ОТДЕЛЬНЫЙ файл `<имя>_wm<pct>.jpg`, оригинал не
    трогаем. Один файл кэша шарится двумя каналами (skhemy_vyazaniya_3 и
    masterskaya_vyaza_2 берут одни и те же картинки hand_knit) — при
    повторном вызове возвращаем готовый файл, иначе срезали бы вдвое больше.
    Fail-open: любая ошибка -> исходный путь (пост не теряем)."""
    if not files:
        return files
    rule = ((config or {}).get("source_media_crop", {}) or {}).get(source_id)
    if not rule:
        return files
    try:
        pct = float(rule.get("bottom_pct", 0) or 0)
    except (TypeError, ValueError):
        return files
    if pct <= 0:
        return files
    pct = min(pct, 30.0)          # защита от опечатки (90% срезало бы кадр)

    out = []
    for f in files:
        try:
            if not f or not isinstance(f, str) or not os.path.exists(f):
                out.append(f)
                continue
            if "_wm" in os.path.basename(f):        # уже обрезанный путь
                out.append(f)
                continue
            base, ext = os.path.splitext(f)
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                out.append(f)                        # видео и прочее не трогаем
                continue
            dst = f"{base}_wm{int(pct)}{ext}"
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                out.append(dst)                      # кэш: уже резали
                continue
            from PIL import Image
            im = Image.open(f)
            w, h = im.size
            keep = int(h * (100.0 - pct) / 100.0)
            if keep < 50:
                out.append(f)
                continue
            im.crop((0, 0, w, keep)).save(dst, quality=93)
            logger.info(f"[crop] {source_id}: {os.path.basename(f)} "
                        f"{w}x{h} -> {w}x{keep} (-{pct:g}%)")
            out.append(dst)
        except Exception as e:
            logger.warning(f"[crop] {source_id} fail-open {f}: {e}")
            out.append(f)
    return out


def _matches_sponsored_promo(text: str) -> bool:
    """True — донорский пост содержит рекламную интеграцию (score >= 2).
    Сильный маркер (гайд-в-закрепе / рейтинг-на-Яндексе / промокод / скидка N%) = 2,
    слабый = 1. Пустой текст безопасен."""
    t = (text or "").strip()
    if not t:
        return False
    score = 0
    for rx in _PROMO_STRONG_RE:
        if rx.search(t):
            score += 2
            if score >= 2:
                return True
    for rx in _PROMO_WEAK_RE:
        if rx.search(t):
            score += 1
            if score >= 2:
                return True
    return False


def _matches_commercial(text: str) -> bool:
    """Bug #14: True если text содержит маркеры рекламы (маркетплейсы, приложения, промо).

    Logged for review; used in scheduler.py on publish-time check для Дача-сетки.
    Live+MAX engine использует Bug #7 ad detector через external_ad_posts.
    """
    if not text:
        return False
    for pattern, category in _COMMERCIAL_PATTERNS:
        try:
            m = re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE)
            if m:
                try:
                    logger.info(
                        f"[COMMERCIAL_HIT] cat={category!r} match={m.group(0)[:60]!r} "
                        f"text_preview={text[:120]!r}"
                    )
                except Exception:
                    pass
                return True
        except re.error:
            continue
    return False


# 2026-05-15: detect digest/aggregator-style posts (несколько новостей в одном)
# Источники типа @newsvladimirru шлют "Утренний дайджест: А, Б и В" — наш rewriter
# раскрывает заголовок в 3 секции. Skipаем такие candidates на этапе отбора.
_DIGEST_TRIGGERS = (
    # Прямые маркеры дайджестов
    "утренний дайджест:",
    "вечерний дайджест:",
    "дневной дайджест:",
    "ночной дайджест:",
    # Тематические рубрики (несколько тем в одном посте)
    "россия и мир:",
    "главные события дня:",
    "главные новости дня:",
    "итоги дня:",
    "новости дня:",
    # Контекстные фразы (приглашение к чтению нескольких новостей)
    "доброе утро всем читателям",
    "ознакомиться с главными новостями",
    "ежедневная рубрика",
    "подборка новостей",
    "топ новостей",
    "обзор событий",
    # 2026-05-17: добавлено после Tyumen digest incident — patterns БЕЗ двоеточия
    "#дайджест",
    "#подборка",
    "#новостинедели",
    "#итогинедели",
    # "главное в " удалено как too broad — может False positive на обычных новостях
    "главное за неделю",
    "главное за день",
    "итоги недели",
    # 2026-05-17: "топ-" удалён (false positive на "Топ-менеджер", "Топ магазин"). См. ниже regex для digest-only
    "за прошедшую неделю",
    "за минувшую неделю",
)

# 2026-05-15: lifestyle/recipe/garden filter — bezformata small city RSS
# часто включает не-новостной контент (рецепты, огородные советы, лайфхаки).
# Engine на новостных каналах не должен это публиковать.
import re as _re_lifestyle

_LIFESTYLE_TRIGGERS = (
    # Рецепты — хозяюшка / хозяйка стиль
    r"хоз[яю][йшк][аеуи]\w*\s+(поделилась|раскрыла|рассказала|научила)",
    r"хозяюшк\w*",
    r"рецепт\w*\s+(творожн|пирог|шарлотк|курник|закусок|салат|выпечк)",
    r"тесто\s+тает",
    r"тает\s+во\s+рту",
    r"для\s+блюда\s+нужн",
    r"\bиз\s+ингредиент",
    r"одной\s+пачки",
    # Огородные/садовые лайфхаки
    r"тл[яья]\s+даже\s+не",
    r"листь[яь]\s+гладкие",
    r"полей\s+(?:рассад|куст)",
    r"обмазала?\s+(?:перцы|кусты|рассад|грядк)",
    r"для\s+отпугивания\s+(?:тли|насеком|грызунов)",
    r"подкормк[аиу]\s+(?:для|клубник|помидор|огурц|перц)",
    r"перед\s+высадкой\s+(?:в\s+грунт|рассад)",
    r"секрет\s+(?:богатого|щедрого|обильного)\s+урожая",
    # 2026-05-17: GARDENING — найдено в prod (petropavlovsk-kamchatka, syktyvkar, stavropol)
    # Болезни/вредители plants — никогда не в новостях, только в "как защитить"
    r"\bплодожорк",
    r"\bпарш[ауие]\b",
    # Phenological phases — gardening only
    r"розового\s+бутона",
    r"фаз[аеу]\s+\w+\s+бутона",
    # Variety recommendations — "сорт крыжовника", "сорт яблони/смородины/..."
    r"сорт\s+(?:крыжовник|смородин|малин|клубник|вишн|сливы|яблон|груш|огурц|помидор|томат|перц|картофел|свёкл|моркови|капуст|кабач|редис)",
    # Лайфхак "что купить для дачи"
    r"(?:находки|подборка|выбор)\s+(?:из\s+)?\S+\s+для\s+(?:дачи|огорода|сада|садовод|дачник)",
    r"дачного\s+сезона",
    r"для\s+дачник",
    # Informal spray garden lingo
    r"\b(?:два|2)\s+пшика\b",
    r"пшика\s+на\s+дерево",
    # Crop protection how-tos
    r"обработк[аеуи]\s+(?:дерев|сада|растений|плодов|кустов|яблон|груш)",
    r"защитить\s+урожай",
    r"урожай\s+на\s+весь\s+сезон",
    # 2026-05-18: LISTICLE / подборка — clickbait формат "N мультфильмов для Y"
    # Found prod: kaliningrad post '6 фильмов для маленьких любителей древностей'
    # Conservative — только entertainment/lifestyle nouns (НЕ способ/метод/совет которые могут быть news)
    r"\b\d+\s+(?:мультфильм|мульт\b|фильм|сериал|книг|подарк|лайфхак|трюк|идей|секрет|причин\s+почему|развлеч)\w*",
)
_LIFESTYLE_PATTERNS = [_re_lifestyle.compile(p, _re_lifestyle.I) for p in _LIFESTYLE_TRIGGERS]

def _is_lifestyle_post(text):
    """True если post — рецепт/огородный лайфхак/lifestyle (не новость).

    Returns (is_lifestyle, matched_trigger).
    Verified on real samples — 5/5 lifestyle, 8/8 news (edge cases pass).
    """
    if not text or len(text) < 30:
        return False, None
    for pat in _LIFESTYLE_PATTERNS:
        m = pat.search(text)
        if m:
            return True, m.group(0)
    return False, None


def _is_digest_style_post(text):
    """Detect digest/aggregator post: несколько новостей в одном тексте.

    Returns (is_digest: bool, matched_trigger: str | None).
    Triggers verified on real samples — 100% precision/recall (3/3 digest, 8/8 single).
    2026-05-17: добавлены emoji-based detection (≥3 emoji-prefixed lines = digest pattern).
    2026-05-17: добавлен regex "топ-\\d+ (новост|событ)" — specific (без false positive на "топ-менеджер").
    """
    if not text or len(text) < 30:
        return False, None
    text_lower = text.lower()
    for trigger in _DIGEST_TRIGGERS:
        if trigger in text_lower:
            return True, trigger
    # 2026-05-17: regex для "топ-N новостей/событий" — НЕ matches "топ-менеджер"
    import re as _re_dig
    if _re_dig.search(r"топ-?\s*\d+\s+(?:новост|событ|город|сюжет|истори|материал)", text_lower):
        return True, "топ-N-новостей-regex"
    # 2026-06-12: структурный детект — >=3 буллета (список новостей) = дайджест
    if text.count("•") >= 3 or text.count("·") >= 3 or text.count("▪") >= 3:
        return True, "multi-bullet"
    # обзор/подборка/итоги за период (без двоеточия-триггеров)
    if _re_dig.search(r"обзор\w*\s+новост|за\s+последн\w+\s+\d+\s+час|начина\w+\s+\w+\s+с\s+обзор|подборк\w+\s+за|итоги\s+(?:дня|недели|суток)", text_lower):
        return True, "обзор-за-период"
    # 2026-05-17: multi-emoji removed — too many false positives (TG posts со structured emoji)
    return False, None



# 2026-05-15: expand digest posts → N individual articles via HTML parse.
# Источник @newsvladimirru шлёт дайджесты с URL https://newsvladimir.ru/fn_*.html.
# На странице — articleBody с pairs <a>title</a> + <p>body</p> для каждой новости.
# Парсим, возвращаем N candidates вместо одного digest → engine берёт каждый в свой slot.
import re as _re_dig

_DIGEST_URL_RE = _re_dig.compile(r"(https?://(?:m\.)?newsvladimir\.ru/fn_\d+\.html)", _re_dig.I)
_ARTICLE_BODY_RE = _re_dig.compile(
    r'<span\s+itemprop="articleBody"[^>]*>(.*?)</span>',
    _re_dig.I | _re_dig.S,
)
_ARTICLE_PAIR_RE = _re_dig.compile(
    r'<p>\s*<a\s+href="([^"]+fn_\d+\.html)"[^>]*>([^<]+)</a>\s*</p>\s*'
    r'<p>([\s\S]*?)</p>',
    _re_dig.I,
)

def _digest_strip_html(s):
    s = _re_dig.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&laquo;", "«").replace("&raquo;", "»")
    s = s.replace("&ndash;", "–").replace("&mdash;", "—").replace("&copy;", "©")
    s = _re_dig.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = _re_dig.sub(r"&[a-z]+;", "", s)
    return _re_dig.sub(r"\s+", " ", s).strip()

def _expand_digest_to_articles(text, pub_time=None):
    """If TG-post is digest with newsvladimir URL — fetch HTML, parse N articles.

    Returns list of dicts (same shape as fetcher returns) или None.
    """
    if not text:
        return None
    m = _DIGEST_URL_RE.search(text)
    if not m:
        return None
    digest_url = m.group(1).replace("m.newsvladimir.ru", "newsvladimir.ru")
    try:
        import requests as _rq
        r = _rq.get(digest_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }, timeout=8)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception as e:
        logger.warning(f"[digest_expand] fetch fail {digest_url}: {type(e).__name__}: {e}")
        return None

    body_match = _ARTICLE_BODY_RE.search(html)
    if not body_match:
        return None
    body_html = body_match.group(1)

    articles = []
    for m in _ARTICLE_PAIR_RE.finditer(body_html):
        url, title, body = m.group(1), m.group(2), m.group(3)
        title = _digest_strip_html(title)
        body = _digest_strip_html(body)
        if not title or not body or len(body) < 30:
            continue
        full = f"{title}. {body}"
        if len(full) > 100:
            articles.append({
                "text": full,
                "source_url": url,
                "pub_time": pub_time,
                "media_url": None,
                "media_type": None,
                "media_files": [],
            })
    if articles:
        logger.info(f"[digest_expand] {digest_url} → {len(articles)} articles")
    return articles or None



# 2026-05-15: expand digest posts → N individual articles via HTML parse.
# Источник @newsvladimirru шлёт дайджесты с URL https://newsvladimir.ru/fn_*.html.
# На странице — articleBody с pairs <a>title</a> + <p>body</p> для каждой новости.
# Парсим, возвращаем N candidates вместо одного digest → engine берёт каждый в свой slot.
import re as _re_dig

_DIGEST_URL_RE = _re_dig.compile(r"(https?://(?:m\.)?newsvladimir\.ru/fn_\d+\.html)", _re_dig.I)
_ARTICLE_BODY_RE = _re_dig.compile(
    r'<span\s+itemprop="articleBody"[^>]*>(.*?)</span>',
    _re_dig.I | _re_dig.S,
)
_ARTICLE_PAIR_RE = _re_dig.compile(
    r'<p>\s*<a\s+href="([^"]+fn_\d+\.html)"[^>]*>([^<]+)</a>\s*</p>\s*'
    r'<p>([\s\S]*?)</p>',
    _re_dig.I,
)

def _digest_strip_html(s):
    s = _re_dig.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&laquo;", "«").replace("&raquo;", "»")
    s = s.replace("&ndash;", "–").replace("&mdash;", "—").replace("&copy;", "©")
    s = _re_dig.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = _re_dig.sub(r"&[a-z]+;", "", s)
    return _re_dig.sub(r"\s+", " ", s).strip()

def _expand_digest_to_articles(text, pub_time=None):
    """If TG-post is digest with newsvladimir URL — fetch HTML, parse N articles.

    Returns list of dicts (same shape as fetcher returns) или None.
    """
    if not text:
        return None
    m = _DIGEST_URL_RE.search(text)
    if not m:
        return None
    digest_url = m.group(1).replace("m.newsvladimir.ru", "newsvladimir.ru")
    try:
        import requests as _rq
        r = _rq.get(digest_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }, timeout=8)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception as e:
        logger.warning(f"[digest_expand] fetch fail {digest_url}: {type(e).__name__}: {e}")
        return None

    body_match = _ARTICLE_BODY_RE.search(html)
    if not body_match:
        return None
    body_html = body_match.group(1)

    articles = []
    for m in _ARTICLE_PAIR_RE.finditer(body_html):
        url, title, body = m.group(1), m.group(2), m.group(3)
        title = _digest_strip_html(title)
        body = _digest_strip_html(body)
        if not title or not body or len(body) < 30:
            continue
        full = f"{title}. {body}"
        if len(full) > 100:
            articles.append({
                "text": full,
                "source_url": url,
                "pub_time": pub_time,
                "media_url": None,
                "media_type": None,
                "media_files": [],
            })
    if articles:
        logger.info(f"[digest_expand] {digest_url} → {len(articles)} articles")
    return articles or None



# 2026-05-15: per-channel geo filter for bezformata items.
# Проблема: local SMI агентства публикуют viral новости из других регионов
# ("Львы во Владимире" в RSS Камчатки). URL slug bezformata детерминированно
# содержит city name → надёжный фильтр.
_BEZFORMATA_OTHER_CITY_SLUGS = (
    # Центральная Россия
    "moskve", "moskvi", "moskva", "podmoskove", "podmoskovi",
    "peterburge", "peterburga", "spb",
    "vladimire", "vladimira", "vladimirskoy",
    "kaluge", "kalugi", "kaluzhskoy",
    "tule", "tuli", "tulskoy",
    "ryazani", "ryazaneskoy", "ryazanskoy",
    "tveri", "tverskoy",
    "yaroslavle", "yaroslavskoy",
    "ivanovo", "ivanovskoy",
    "kostrome", "kostromskoy",
    "vologde", "vologodskoy",
    "smolenske", "smolenskoy",
    "bryanske", "bryanskoy",
    "orle", "orlovskoy",
    "kurske", "kurskoy",
    "voroneje", "voronezhskoy",
    "lipetske", "lipetskoy",
    "tambove", "tambovskoy",
    # Юг
    "krasnodare", "krasnodarskom",
    "stavropole", "stavropolskom",
    "rostove", "rostovskoy",
    "volgograde", "volgogradskoy",
    "sochi",
    "mahachkale", "dagestane",
    "vladikavkaze",
    "groznom", "chechne",
    "nalchike", "kabardino",
    # Поволжье
    "kazani", "tatarstane",
    "ufe", "bashkortostane",
    "samare", "samarskoy",
    "saratove", "saratovskoy",
    "nnovgorode", "nizhegorodskoy", "novgorode",
    "permi", "permskom",
    "izhevske", "udmurtii",
    "joshkar", "marii",
    "saranske", "mordovii",
    "cheboksarah", "chuvashii",
    # Урал
    "ekaterinburge", "sverdlovskoy",
    "chelyabinske", "chelyabinskoy",
    "kurgane", "kurganskoy",
    "tyumene", "tyumenskoy",
    "magnitogorske",
    # Сибирь
    "novosibirske",
    "krasnoyarske",
    "irkutske",
    "omske",
    "kemerovo", "kuzbasse",
    "tomske",
    "barnaule", "altae",
    "ulan-ude", "buryatii",
    "chite",
    # Дальний Восток
    "habarovske",
    "vladivostoke", "primore",
    "yakutske", "saha",
    "magadane",
    "petropavlovske", "kamchatke", "kamchatskogo",
    "yujno-sahalinske", "sahaline",
    "anadire", "chukotke",
    # СЗФО
    "arhangelske", "arhangelskoy",
    "murmanske", "murmanskoy",
    "petrozavodske", "karelii",
    "syktyvkare", "komi",
    # Калининград
    "kaliningrade", "kaliningradskoy",
    # Крым
    "krimu", "krimskom", "sevastopole", "kerchi", "yalte",
    # Annexed
    "donetske", "lugaiske",
)


def _bezformata_extract_url_city(link_url: str) -> str | None:
    """Извлекает city slug из bezformata article URL.

    Match только по WORD BOUNDARY (slug делится на '-'), не substring.
    Защищает от false positive "tveri" в "chetverih" / "saha" в "saharniy".

    Example:
      /listnews/lvi-vo-vladimire/159692164/  -> words=[lvi,vo,vladimire] → "vladimire"
      /listnews/saharniy-diabet/...          -> words=[saharniy,diabet] → None (saha != saharniy)
    """
    if not link_url:
        return None
    import re as _re_geo
    m = _re_geo.search(r"/listnews/([a-z0-9-]+)/\d+/?", link_url, _re_geo.I)
    if not m:
        return None
    slug = m.group(1).lower()
    slug_words = slug.split("-")  # word boundary через дефис
    for city in _BEZFORMATA_OTHER_CITY_SLUGS:
        if city in slug_words:  # exact word match
            return city
    return None


def _bezformata_is_other_region(channel_key: str, link_url: str) -> tuple[bool, str | None]:
    """True если bezformata URL slug говорит про другой регион (не свой канал).

    Returns (is_other, matched_city).
    """
    found_city = _bezformata_extract_url_city(link_url)
    if not found_city:
        return False, None

    # Извлекаем own city из channel_key
    # channel_key типа: petropavlovsk_kamchatskij_v_max → own = petropavlovsk
    # vladimir_v_max → own = vladimir
    own_root = channel_key
    for suffix in ("_v_max", "_lajv"):
        idx = own_root.find(suffix)
        if idx > 0:
            own_root = own_root[:idx]
            break

    # 2026-05-15 v3: REGION_ALIASES для каналов где region name != city name.
    # Например ulan_ude канал — but bezformata slug содержит "buryatii" (Бурятия).
    # Каждый channel может иметь дополнительные accepted stems для своего региона.
    REGION_ALIASES = {
        # Республики Поволжья
        "kazan": ("tatarstan",),
        "naberezhnye_chelny": ("tatarstan",),
        "almetevsk": ("tatarstan",),
        "nizhnekamsk": ("tatarstan",),
        "zelenodolsk": ("tatarstan",),
        "ufa": ("bashkir", "bashkort"),
        "salavat": ("bashkir", "bashkort"),
        "neftekamsk": ("bashkir", "bashkort"),
        "oktyabrskij": ("bashkir", "bashkort"),
        "izhevsk": ("udmurt",),
        "glazov": ("udmurt",),
        "votkinsk": ("udmurt",),
        "joshkar_ola": ("marii",),
        "saransk": ("mordov",),
        "cheboksary": ("chuvash",),
        "novocheboksarsk": ("chuvash",),
        # Нижегородская
        "nizhnij_novgorod": ("nizhegoro",),
        "dzerzhinsk": ("nizhegoro",),
        "arzamas": ("nizhegoro",),
        # Северный Кавказ
        "vladikavkaz": ("osetii", "alanii"),
        "nalchik": ("kabard", "balkar"),
        "groznyj": ("chechen", "chechni"),
        "mahachkala": ("dagest",),
        "kaspijsk": ("dagest",),
        "derbent": ("dagest",),
        "cherkessk": ("karachae", "kchr"),
        "elista": ("kalmyk",),
        # Сибирь
        "abakan": ("hakas", "khakas"),
        "kyzyl": ("tuv",),
        "barnaul": ("altay", "altajskoy", "altajskoy"),
        "kemerovo": ("kuzbas",),
        "novokuzneck": ("kuzbas",),
        "mezhdurechensk": ("kuzbas",),
        "prokopevsk": ("kuzbas",),
        "ulan_ude": ("buryat", "buriat"),
        "chita": ("zabaykal",),
        # Дальний Восток
        "yakutsk": ("saha", "yakut"),
        "petropavlovsk_kamchatskij": ("kamchat",),
        "yujno_sahalinsk": ("sahalin",),
        "yuzhno_sahalinsk": ("sahalin",),
        "magadan": ("magadan",),
        "anadir": ("chukot",),
        "habarovsk": ("habarov",),
        "khabarovsk": ("habarov",),
        "komsomolsk_na_amure": ("habarov", "amur"),
        "blagoveschensk": ("amur",),
        "birobidzhan": ("evrey",),
        "vladivostok": ("primor",),
        "ussurijsk": ("primor",),
        "nakhodka": ("primor",),
        "artyom": ("primor",),
        # Урал
        "ekaterinburg": ("sverdlov",),
        "kamensk_uralskij": ("sverdlov",),
        "nizhnij_tagil": ("sverdlov",),
        "chelyabinsk": ("chelyab",),
        "magnitogorsk": ("chelyab",),
        "miass": ("chelyab",),
        "kopejsk": ("chelyab",),
        "zlatoust": ("chelyab",),
        "kurgan": ("kurgansk",),
        "tyumen": ("tyumen",),
        "tobolsk": ("tyumen",),
        "khanty_mansijsk": ("hanty", "khanty", "yugr"),
        "noyabrsk": ("yamal",),
        "perm": ("permsk",),
        "berezniki": ("permsk",),
        # СЗФО
        "petrozavodsk": ("karel",),
        "syktyvkar": ("komi",),
        "arkhangelsk": ("arhangel",),
        "murmansk": ("murmansk",),
        # СФО Тыва, Хакасия — выше
        # Юг
        "krasnodar": ("krasnodar", "kubani"),
        "sochi": ("krasnodar", "kubani"),
        "armavir": ("krasnodar", "kubani"),
        "novorossijsk": ("krasnodar", "kubani"),
        "rostov_na_donu": ("rostov",),
        "novocherkassk": ("rostov",),
        "taganrog": ("rostov",),
        "shakhty": ("rostov",),
        "volgodonsk": ("rostov",),
        "stavropol": ("stavrop",),
        "pyatigorsk": ("stavrop",),
        "kislovodsk": ("stavrop",),
        "essentuki": ("stavrop",),
        "nevinnomyssk": ("stavrop",),
        "mikhajlovsk": ("stavrop",),
        # Поволжье
        "samara": ("samar",),
        "syzran": ("samar",),
        "togliatti": ("samar",),
        "saratov": ("saratov",),
        "balakovo": ("saratov",),
        "engels": ("saratov",),
        "volgograd": ("volgograd",),
        "volzhskij": ("volgograd",),
        "kamyshin": ("volgograd",),
        "ulyanovsk": ("ulyanovsk",),
        "penza": ("penza",),
        "orenburg": ("orenburg",),
        "orsk": ("orenburg",),
        # ЦФО
        "moskva": ("moskva", "podmoskov"),
        "balashikha": ("podmoskov", "moskovskoy"),
        "korolyov": ("podmoskov", "moskovskoy"),
        "mytischi": ("podmoskov", "moskovskoy"),
        "podolsk": ("podmoskov", "moskovskoy"),
        "khimki": ("podmoskov", "moskovskoy"),
        "lyubercy": ("podmoskov", "moskovskoy"),
        "domodedovo": ("podmoskov", "moskovskoy"),
        "elektrostal": ("podmoskov", "moskovskoy"),
        "schyolkovo": ("podmoskov", "moskovskoy"),
        "kolomna": ("podmoskov", "moskovskoy"),
        "krasnogorsk": ("podmoskov", "moskovskoy"),
        "noginsk": ("podmoskov", "moskovskoy"),
        "obninsk": ("kaluzhsk", "kaluga"),
        # Калининград
        "kaliningrad": ("kaliningrad",),
        # Крым
        "krym": ("krym",),
        "sevastopol": ("sevastopol", "krym"),
        "simferopol": ("krym",),
        "kerch": ("krym",),
        "yalta": ("krym",),
    }

    # Проверим — found_city относится к own_root?
    # COMMON PREFIX matching с tolerance 2 chars в конце.
    # Это ловит "tula" matches "tulskoy" (cp=3, req=4-2=2) — свой регион.
    # Защищает: "vladimir" vs "vladivostok" (cp=5, req=8-2=6) → NO match.
    own_parts = own_root.split("_")  # ["petropavlovsk", "kamchatskij"]
    for own_part in own_parts:
        if len(own_part) < 4:
            continue
        # Common prefix length
        n = min(len(own_part), len(found_city))
        cp = 0
        for i in range(n):
            if own_part[i] != found_city[i]:
                break
            cp += 1
        # Tolerance: разрешаем разницу в 2 chars в конце own_part
        # ("tula" vs "tulskoy" cp=3, len(own)=4, req=2 → match)
        # ("orel" vs "orle" cp=2, len(own)=4, req=2 → match)
        # ("vladimir" vs "vladivostok" cp=5, len=8, req=6 → no match — защита)
        min_required = max(2, len(own_part) - 2)
        if cp >= min_required:
            return False, None  # это свой город — keep

    # Fallback: REGION_ALIASES для каналов где region != city
    aliases = REGION_ALIASES.get(own_root, ())
    for alias in aliases:
        if alias in found_city:
            return False, None  # свой регион (по alias) — keep

    return True, found_city


def _post_geo_mismatch(channel_key: str, text: str) -> tuple[bool, str | None]:
    """True если text говорит про PRIMARY geo location, не соответствующее каналу.

    2026-05-17 case (Vladivostok): "18 мая в Москве и средней полосе начнётся
    редкое погодное явление" — опубликован для vladivostok_lajv. URL slug нейтральный,
    но контент явно про Москву.

    Conservative: проверяем только первые 200 chars на МАРКЕРЫ primary location:
      "в москве", "москвичи", "в столице", "в подмосковье",
      "в санкт-петербурге", "в петербурге", "в питере", "петербуржцы".
    Returns (is_mismatch, mentioned_city).
    """
    if not text or len(text) < 30:
        return False, None
    head = text[:200].lower()

    # Channel's own root city
    own_root = channel_key
    for suffix in ("_v_max", "_lajv", "_2"):
        idx = own_root.find(suffix)
        if idx > 0:
            own_root = own_root[:idx]
            break
    # Strip trailing region tokens
    for region_suffix in ("_respublika_khakasiya", "_arkhangelskaya_oblast",
                           "_altajskij_kraj", "_penzenskaya_oblast"):
        if own_root.endswith(region_suffix):
            own_root = own_root[:-len(region_suffix)]
            break

    # Moscow region — каналы которые ARE Moscow/Подмосковье (skip filter)
    MOSCOW_OWN = {
        "moskva", "balashikha", "korolyov", "mytischi", "podolsk", "khimki",
        "lyubercy", "domodedovo", "elektrostal", "schyolkovo", "kolomna",
        "krasnogorsk", "noginsk", "moskva_oblast", "moskovskaya",
    }
    MOSCOW_MARKERS = [
        "в москве",
        "москвичи",
        "москвичей",
        "москвичам",
        "москвичами",
        "в столице",
        "в подмосковье",
        "в московской области",
        "в средней полосе",
    ]
    if own_root not in MOSCOW_OWN:
        for marker in MOSCOW_MARKERS:
            if marker in head:
                return True, f"Москва (marker={marker!r})"

    # SPb region
    SPB_OWN = {"sankt_peterburg", "spb", "kingisepp", "gatchina", "kolpino"}
    SPB_MARKERS = [
        "в санкт-петербурге",
        "в петербурге",
        "в питере",
        "петербуржцы",
        "петербуржцев",
        "петербуржцам",
        "в ленобласти",
        "в ленинградской области",
    ]
    if own_root not in SPB_OWN:
        for marker in SPB_MARKERS:
            if marker in head:
                return True, f"Санкт-Петербург (marker={marker!r})"

    return False, None


def _matches_blacklist_phrases(text: str) -> bool:
    t = _normalize_for_match(text)
    if not t:
        return False
    return any(ph in t for ph in BLACKLIST_PHRASES)


# _matches_profanity удалён — теперь импортируется из processor.filter (см. re-export в начале файла).


def _save(post: dict, source_id: str, channel: str,
          ai_client=None, ai_provider: str = "groq", do_rewrite: bool = True,
          grid_prompt: str = "", grid_max_tokens: int = None,
          channel_prompt: str = "") -> int:
    url = post.get("source_url", "")
    orig = post.get("text", "") or ""
    media_url = post.get("media_url")
    media_type = post.get("media_type")

    # Блокируем посты с inline URL-кнопками (markup)
    if post.get("has_inline_url_buttons"):
        if url:
            db.add_seen(url, channel)
        return 0

    # Черный список паразитных фраз
    if _matches_blacklist_phrases(orig):
        if url:
            db.add_seen(url, channel)
        return 0

    # Глобальный анти-мат (блокируем сразу)
    if _matches_profanity(orig):
        if url:
            db.add_seen(url, channel)
        return 0

    # Regex-блокировка (global + grid + channel + anti-ad)
    if _matches_regex_block(orig, channel):
        if url:
            db.add_seen(url, channel)
        return 0

    # Обрезка ватермарки источника (2026-07-29): hand_knit ставит плашку внизу.
    # Делаем ДО курации/сохранения, чтобы в БД попали уже чистые пути.
    try:
        from utils import load_config as _lc_cr
        _files_cr = post.get("media_files") or ([media_url] if media_url else [])
        _cropped = _crop_source_watermark(_files_cr, source_id, _lc_cr())
        if _cropped and _cropped != _files_cr:
            post["media_files"] = _cropped
            if media_url and media_url in _files_cr:
                media_url = _cropped[_files_cr.index(media_url)]
    except Exception as _cre:
        logger.warning(f"[crop] пропущено: {_cre}")

    # LLM-курация Дача/Вязание (2026-07-27): смысловой фильтр вместо гонки regex —
    # ловит тизеры-обрывки, офтоп и рекламу, которых нет в паттернах
    # («Те самые Брагиной, которые она снимала» — Дом Повара 27.07 15:01).
    # Fail-open внутри curate_dacha: сбой LLM никогда не блокирует публикацию.
    try:
        from utils import load_config as _lc_dc
        import dacha_curation as _dc
        _cfg_dc = _lc_dc()
        if (_cfg_dc.get('dacha_curation', {}) or {}).get('enabled'):
            _fn_dc = _dc.build_complete_fn(_cfg_dc)
            _ok_dc, _why_dc = _dc.curate_dacha(orig, channel, _cfg_dc, complete_fn=_fn_dc,
                                              media_type=media_type)
            if not _ok_dc:
                logger.info('[%s] curation reject (%s): %s', channel, _why_dc, (orig or '')[:60])
                if url:
                    db.add_seen(url, channel)
                return 0
    except Exception as _dce:
        logger.warning('[%s] dacha curation skipped: %r', channel, _dce)

    # Спонсорская реклама в донорском посте (2026-07-26, Дачники/Дачный уголок)
    if _matches_sponsored_promo(orig):
        logger.info(f"[{channel}] promo-ad blocked: {(orig or '')[:60]!r}")
        if url:
            db.add_seen(url, channel)
        return 0

    # Дубль: проверяем архив за 30 дней в рамках канала (url_hash ИЛИ text_hash)
    if db.is_duplicate(orig, channel, source_url=url):
        if url:
            db.add_seen(url, channel)
        return 0

    # Legacy-check по seen_posts_v2 (совместимость)
    if url and db.is_seen(url, channel):
        return 0

    if should_skip(orig, media_url):
        if url:
            db.add_seen(url, channel)
        return 0

    # Проверяем стоп-слова из конфига (глобальные + сетки)
    if _matches_stopwords(orig, channel):
        if url:
            db.add_seen(url, channel)
        return 0

    clean = _clean(orig)

    # Фильтр устаревших событий (вчерашние праздники/прошедшие даты)
    try:
        from processor.rewriter import is_outdated_event_text
        is_old, _why = is_outdated_event_text(clean or orig)
        if is_old:
            if url:
                db.add_seen(url, channel)
            return 0
    except Exception:
        pass

    if not clean.strip() and not media_url:
        if url:
            db.add_seen(url, channel)
        return 0

    if do_rewrite:
        # Приоритет промптов: channel.rewrite_prompt > grid.prompt > default
        effective_prompt = (channel_prompt or "").strip() or (grid_prompt or "")
        rewritten = _rewrite(clean, channel, media_type, ai_client, ai_provider,
                             effective_prompt, grid_max_tokens)
    else:
        rewritten = clean

    rewritten = _cleanup_rewrite_text(rewritten)

    # Жёсткий guard: маркеры отказа LLM не должны попадать в pending.
    if _is_skip_marker_text(rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    # Жёсткие news-ограничения до добавления в очередь
    if _matches_unwanted_news_text(orig) or _matches_unwanted_news_text(rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    if _is_old_explicit_date(orig) or _is_old_explicit_date(rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    if _looks_too_short_or_broken(rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    if _is_near_duplicate_pending_same_source(channel, source_id, rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    # Вторичная проверка устаревших событий после рерайта
    try:
        from processor.rewriter import is_outdated_event_text
        is_old2, _why2 = is_outdated_event_text(rewritten)
        if is_old2:
            if url:
                db.add_seen(url, channel)
            return 0
    except Exception:
        pass

    # Глобальный анти-мат после рерайта (LLM может сгенерировать токсичный текст)
    if _matches_profanity(rewritten):
        if url:
            db.add_seen(url, channel)
        return 0

    media_files = post.get("media_files", [])
    db.add_post("telegram", source_id, orig, rewritten, media_url, media_type, channel,
                media_files=media_files, source_url=url)
    if url:
        db.add_seen(url, channel)
    return 1


def _fetch_rss(url: str) -> list:
    """Парсит RSS-ленту и возвращает список постов."""
    try:
        import feedparser
        feed = feedparser.parse(url)
        posts = []
        for entry in feed.entries[:20]:
            text = entry.get("summary") or entry.get("title") or ""
            # Время публикации из RSS
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    from datetime import datetime, timezone
                    import time as _time
                    pub_time = datetime.fromtimestamp(
                        _time.mktime(entry.published_parsed), tz=timezone.utc
                    )
                except Exception:
                    pub_time = None
            posts.append({
                "text": text,
                "source_url": entry.get("link", ""),
                "media_url": None,
                "media_type": None,
                "media_files": [],
                "pub_time": pub_time,
            })
        return posts
    except Exception as e:
        logger.error(f"RSS parse error {url}: {e}")
        return []



def _strip_source_dateline(txt):
    """Вырезать байлайн-предложение «[Источник], ДД месяц.» из тела bezformata
    (Иркутск 26.06: «IrkutskMedia, 26 июня.» сразу после заголовка → рерайт цепляется
    за него как за лид и схлопывается, теряя контент — это НЕ стохастика, а наш
    необрезанный байлайн). FP-safe: требует ЗАПЯТУЮ перед датой и ТОЧКУ после месяца,
    поэтому «Сегодня, 26 июня, …» (запятая после) и заголовок «…утром 26 июня.» (нет
    запятой перед датой) НЕ трогаются. None/'' -> как есть."""
    if not txt:
        return txt
    import re as _re_sd
    pat = _re_sd.compile(
        r'(?:(?<=\.)|^)\s*[^.\n]{1,35},\s*\d{1,2}\s+'
        r'(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\.'
        r'(?=\s|$)', _re_sd.I)
    out = pat.sub(" ", txt)
    return _re_sd.sub(r"\s{2,}", " ", out).strip()


def _strip_leading_photo_credit(txt):
    """Срезать ВЕДУЩИЕ строки-подписи к фото (Фото: Иван И. Перейти в Фотобанк КП,
    Видео: ...) из тела bezformata — иначе rewrite коллапсирует на эту первую строку
    (Сочи 25.06: тело осталось одним 'Фото: Алексей БУЛАТОВ.'). Срезаем ТОЛЬКО ведущие
    такие строки; 'Фото:' в середине тела не трогаем. (None/'' -> как есть.)"""
    if not txt:
        return txt
    _pref = ("фото:", "фото :", "видео:", "видео :", "foto:", "photo:")
    lines = txt.splitlines()
    while lines:
        head = lines[0].strip()
        low = head.lower()
        if (not head) or low.startswith(_pref) or ("фотобанк" in low):
            lines.pop(0)
        else:
            break
    return (chr(10).join(lines)).strip()


def _bezformata_extract_body(html: str, url: str = "") -> "str | None":
    """Достать ПОЛНЫЙ текст статьи bezformata из уже скачанного HTML.

    RSS <description> bezformata — обрезанный тизер (обрывается на середине
    фразы). Тело статьи есть в HTML и извлекается trafilatura.extract
    (precision-режим). Возвращает очищенный текст или None (graceful fallback
    на тизер) если пусто / короче 150 / trafilatura недоступна.
    Добавлено 2026-06-12 (Task CL: Великие Луки/bezformata RSS-тизеры обрезаны).
    """
    if not html:
        return None
    try:
        import trafilatura as _trafi
    except Exception:
        return None
    try:
        txt = _trafi.extract(html, url=(url or None), favor_recall=False,
                             include_comments=False, include_tables=False)
    except Exception:
        return None
    if not txt:
        return None
    import re as _re_b
    # leading orphan cleanup: ведущие пробелы + сиротские combining/VS-знаки
    # (кейс abakan: orphan variation-selector U+FE0F перед буквой от trafilatura)
    txt = _re_b.sub("^[\\s‍⁠️]+", "", txt)
    # консервативный трим хвоста: заголовки блоков связанных ссылок (только в хвосте,
    # offset>200) — иначе «Ранее мы писали: <другая новость>» путает rewrite-суммаризацию.
    for _mk in ("Ранее мы писали", "Читайте также", "Читайте по теме",
                "Смотрите также", "Читать также"):
        _i = txt.find(_mk)
        if _i > 200:
            txt = txt[:_i]
            break
    txt = _strip_leading_photo_credit(txt)
    txt = _strip_source_dateline(txt)
    txt = txt.strip()
    if len(txt) < 150:
        return None
    return txt


def _source_link_is_article(url: str) -> bool:
    """True, если ссылка «Источник:» ведёт на СТРАНИЦУ СТАТЬИ, а не на главную.

    2026-08-11 (Владимир: клещи при ДТП): у перепечатки без картинки ветка
    source шла по ссылке «Источник» на ГЛАВНУЮ сайта-первоисточника и брала
    её og:image — фото чужой свежей новости. Главная = путь пустой/короткий
    и без цифр; статья почти всегда несёт id/дату/длинный slug."""
    try:
        from urllib.parse import urlparse
        pth = (urlparse(url).path or "").strip("/")
    except Exception:
        return False
    if len(pth) < 4:
        return False
    import re as _re_a
    return bool(_re_a.search(r"\d", pth) or len(pth) >= 20)


def _bezformata_resolve_article(article_url: str, headers: dict, timeout: float = 5.0,
                                retries: int = 1):
    """Fetch bezformata article HTML ОДИН раз → (image_url, image_kind, body).

    image_url/kind — лучшая картинка (og / local / None), как раньше.
    body — полный текст статьи (trafilatura) или None.
    Один GET переиспользуется и для картинки, и для текста (без доп. нагрузки).

    Priority картинки:
      1. og:image (original, full quality)
      2. <img .../content/imageNNN.jpg> subdomain-matched (bezformata local copy)
      3. None (caller falls back to enclosure GIF)

    retries: доп. попытки GET при таймауте/не-200 (bezformata часто транзиентно
    подвисает). Returns (best_url, kind, body); kind in ("og","local",None).
    """
    import requests as _rq
    import re as _re_local
    html = None
    for _attempt in range(retries + 1):
        try:
            r = _rq.get(article_url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                html = r.text
                break
            # не-200 (транзиентный 403/5xx) — ещё попытка
        except Exception:
            pass
    if html is None:
        return None, None, None

    img_url, img_kind = None, None
    _og_was_teaser = False
    # og:image — both attribute orders
    OG = _re_local.compile(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        _re_local.I,
    )
    OG_REV = _re_local.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        _re_local.I,
    )
    m = OG.search(html) or OG_REV.search(html)
    if m:
        og = m.group(1).strip()
        # 2026-05-18: filter only bezformata-domain generic logo, not external URLs
        og_lower = og.lower()
        is_bf_logo = "logobezformata" in og_lower or (".bezformata.com" in og_lower and "/pic/" in og_lower)
        # DL 2026-07-23: bezformata кладёт в og СВОЕЙ страницы тизер /f/266x136/ из
        # ленты первоисточника — часто фото ЧУЖОЙ новости (Камчатка-«дрон»,
        # Сахалин-«мяч»). Тизер не финал: идём дальше по цепочке (local → source),
        # у первоисточника og /f/original/ корректен. Не нашли лучше — без фото.
        if og and not is_bf_logo:
            if _is_teaser_size_url(og):
                _og_was_teaser = True
            else:
                img_url, img_kind = og, "og"

    # fallback: bezformata local JPEG copy (subdomain-matched, 2026-05-19 FIX).
    # Реальное body image имеет ТУ ЖЕ subdomain что у article URL; bare
    # `besformata.com` намеренно НЕ берётся — он почти всегда TOP-block.
    # DL 2026-07-23: при og-ТИЗЕРЕ local пропускаем — это копия того же тизера
    # (Камчатка-«дрон»: local = 150x100 даунскейл чужого превью) — сразу в source.
    if img_url is None and not _og_was_teaser:
        sub_match = _re_local.match(r'https?://([a-z0-9-]+)\.bezformata\.com/', article_url)
        if sub_match:
            sub = sub_match.group(1)
            LOC_SUB = _re_local.compile(
                r'<img[^>]+src=["\'](https?://' + _re_local.escape(sub) +
                r'\.bezformata\.com/content/image\d+\.jpg)["\']',
                _re_local.I,
            )
            m2 = LOC_SUB.search(html)
            if m2:
                img_url, img_kind = m2.group(1).strip(), "local"

    # 24.06: bezformata-перепечатка без своей картинки (ни og, ни local) — фото
    # живёт на ПЕРВОИСТОЧНИКЕ (КП/АИФ и т.п., часто в Фотобанке). В HTML есть блок
    # "Источник: <a href=...>". Идём туда и берём og:image оригинала.
    if img_url is None:
        src_m = _re_local.search(
            r'Источник[^<]{0,40}<a[^>]+href=["\']([^"\']+)["\']', html, _re_local.I)
        if src_m:
            orig_url = src_m.group(1).strip()
            if (orig_url.startswith(("http://", "https://"))
                    and "bezformata.com" not in orig_url.lower()
                    and _source_link_is_article(orig_url)):
                try:
                    _big_src = _resolve_og_image_for_article(orig_url, timeout=timeout)
                except Exception:
                    _big_src = None
                if _big_src:
                    img_url, img_kind = _big_src, "source"

    body = _bezformata_extract_body(html, article_url)
    return img_url, img_kind, body


def _bezformata_resolve_og_image(article_url: str, headers: dict, timeout: float = 5.0,
                                 retries: int = 1):
    """Backward-compat обёртка: только (image_url, kind).

    См. _bezformata_resolve_article (она же достаёт body). Сохранена для
    существующих вызовов (preparer.py step 4d) и тестов (test_bezformata_og.py).
    """
    img, kind, _body = _bezformata_resolve_article(article_url, headers, timeout, retries)
    return img, kind



# LRU cache for og:image lookups across channels — avoids duplicate HTTP fetches
# when several channels share the same source.
from functools import lru_cache as _lru_cache_og

_CHROME_IMG_TOKENS = (
    "probel",  # 2026-08-11: сквозные топ-картинки движка region.center (fileprobel1, vlsm*probel1)
    "favicon", "logo", "maket", "/avatar", "/sprite", "emoji",
    "/banner", "banner_", "/icon", "share-", "telegram",
    # DL 2026-07-06: bare "social" резал ГЛАВНОЕ og:image новостей (rzn.info кладёт его
    # в /socials/ как *_social_openGraph.jpg) -> публиковалось левое фото из тела (прилавок).
    # Ловим ТОЛЬКО соц-иконки/кнопки; og-карточки (social_openGraph/preview) проходят.
    "social-icon", "social_icon", "social-share", "social-btn",
    "social-button", "soc-icon", "soc_icon", "/social.svg", "/social.ico",
    "vk-", "ok-", "placeholder", "/stub", "default-",
    "probel", "randpoint", "besformata.com/pic", "region.center/data",
    "/modules", "img/date", "img/time", "/spacer", "1x1", "pixel",
    # DL 2026-07-13: bezformata og:image = соц-иконка из блока «партнёры» источника
    # (nabchelny.ru/upload/partners/vk.png = логотип ВК вместо фото новости)
    "/partners/", "partners_",
    # DL 2026-07-14: MK дефолт-заглушка (static.mk.ru/media/img/mk.ru/mkru_og_tag —
    # у филиалов мк-тува/мк-калуга без своего фото) + рекламный CDN брянского сайта
    # (bryansk-smi.ru/cdn/bars-new|swo — баннеры вербовки в og:image первоисточника).
    "/img/mk.ru/", "bryansk-smi.ru/cdn",
    # DL 2026-07-23: динамический генератор og-карточки-логотипа у PrimaMedia-сети
    # (irkutskmedia /opengraph/image/?site=IrkutskMedia.ru — у статей без фото).
    "/opengraph/image",
    # DL 2026-07-31 (Благовещенск 05:00): amur.life отдал баннер из рекламного
    # каталога /upload/ads/ (Газманов) как фото статьи об убийстве. Слэш-границы
    # обязательны: "/ads/" не бьёт "uploads"/"roads" (закреплено тестами).
    "/ads/", "/advert", "adfox", "/adv/", "/reklam",
    # DL 2026-08-12 (Обнинск): og:image первоисточника ngregion.ru =
    # логотип Joomla-плагина (/media/com_jursspublisher/jursspublisher.png).
    # /media/com_* — служебные ассеты Joomla-компонентов (CSS/JS/иконки),
    # контентные фото живут в /images/ и /media/k2/items/ — их не задевает.
    "jursspublisher", "/media/com_",
)


# Брендовая шара-карточка, чьё ИМЯ ФАЙЛА — голый домен (статичный дефолт-OG
# WordPress: moika78 `.../wp-content/uploads/2024/10/mojka.ru.webp`; так делают
# многие новостные WP-сайты). Форма `<имя>.<tld>.<imgext>` — какой у реальных
# статейных фото не бывает (там хеши / IMG_* / размеры). Хост, содержащий TLD
# (static.mk.ru, okean.org), НЕ задевается — смотрим только ИМЯ ФАЙЛА. DL 2026-07-01.
import re as _re_chrome
_DOMAIN_CARD_RE = _re_chrome.compile(
    r'/[a-z0-9_-]+\.(?:ru|рф|com|net|org|info|media|news|online|tv|press|pro)\.'
    r'(?:jpe?g|png|webp|gif|svg)(?:[?#]|$)', _re_chrome.I)

# DL 2026-07-13: ИМЯ ФАЙЛА = иконка соцсети (vk.png/ok.svg/dzen.svg...). Требуется
# слэш перед именем и img-расширение сразу после -> не задевает домен vk.com,
# 'book.png' (перед ok стоит 'o', не '/'), 'facebookPicture/..' (нет точки после facebook).
_SOCIAL_ICON_FILE_RE = _re_chrome.compile(
    r'/(?:vk|ok|odnoklassniki|dzen|zen|telegram|tg|whatsapp|viber|rutube|youtube|facebook|fb|instagram|twitter)\.'
    r'(?:png|svg|ico|gif|jpe?g|webp)(?:[?#]|$)', _re_chrome.I)

# DL 2026-07-23: WxH-сегмент в ПУТИ (не в имени файла) с max(w,h)<500 — формат
# тизеров-превью лент (PrimaMedia /f/266x136/, RSS-гифы /67x66/). Ресайзы крупнее
# (/1200x630/ у multiadminka) и /f/original|big/ не задеваются.
_TEASER_WH_RE = _re_chrome.compile(r'/(\d{2,4})x(\d{2,4})/')


def _is_teaser_size_url(url) -> bool:
    """True если в пути URL есть WxH-каталог тизер-размера (max < 500px) —
    превью из ленты/сайдбара, не статейное фото. None/'' -> False."""
    m = _TEASER_WH_RE.search(url or "")
    if not m:
        return False
    try:
        w, h = int(m.group(1)), int(m.group(2))
    except ValueError:
        return False
    return max(w, h) < 500


def _is_multiadminka_plate(url) -> bool:
    """True если multiadminka get_resized URL — дефолт-плашка рубрики (site-plates).

    DL 2026-07-23: движок multiadminka (mosregtoday и др. подмосковные) у статей
    БЕЗ фото отдаёт og:image=плашку «ОБЩЕСТВО»/рубрики. Оригинальный путь спрятан
    в urlsafe-base64 последнем сегменте — декодируем и ищем 'site-plates'."""
    u = url or ""
    if "multiadminka.ru/get_resized/" not in u.lower():
        return False
    try:
        from urllib.parse import urlparse as _up_pl
        seg = _up_pl(u).path.rsplit("/", 1)[-1]
        seg = seg.split(".", 1)[0]
        import base64 as _b64_pl
        dec = _b64_pl.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8", "ignore").lower()
    except Exception:
        return False
    return "site-plates" in dec or "site_plates" in dec


def _is_chrome_image(url) -> bool:
    """True если URL — 'хром' сайта (логотип/баннер/иконка) или спейсер/декорация,
    а не контентное фото (DL 2026-06-26: newsvladimir/newsivanovo og:image=логотип,
    region.center тело забито спейсерами 'probel'/'randpoint'; DL 2026-07-01:
    moika78 шара-карточка mojka.ru.webp — имя файла=домен). None/'' -> True."""
    if not url:
        return True
    u = url.lower()
    if any(t in u for t in _CHROME_IMG_TOKENS):
        return True
    if _SOCIAL_ICON_FILE_RE.search(url):
        return True
    if _is_multiadminka_plate(url):
        return True
    return bool(_DOMAIN_CARD_RE.search(url))





_OG_IMG_SEEN: dict = {}
_OG_IMG_SEEN_TTL = 6 * 3600.0


def _og_img_repeated(img_url: str, article_url: str) -> bool:
    """True — эта картинка уже возвращалась для ДРУГОЙ статьи за 6 часов.

    2026-08-11 (Владимир: клещи при ДТП): og первоисточника бывает
    ДИНАМИЧЕСКИМ — сайт отдаёт одну «свежую» картинку для всех статей
    (в теле статьи og-файла нет вовсе). Повтор для другого URL = сквозная."""
    import time as _t
    now = _t.monotonic()
    for k in [k for k, (ts, _) in _OG_IMG_SEEN.items()
              if now - ts > _OG_IMG_SEEN_TTL]:
        _OG_IMG_SEEN.pop(k, None)
    prev = _OG_IMG_SEEN.get(img_url)
    if prev is not None and prev[1] != article_url:
        return True
    _OG_IMG_SEEN[img_url] = (now, article_url)
    return False


def _og_img_dedup_wrap(_fn):
    """Фильтр сквозных og-картинок поверх lru-кэша resolver'а."""
    def _w(article_url, timeout=5.0):
        _r = _fn(article_url, timeout)
        if _r and _og_img_repeated(_r, article_url):
            logger.info(f"[og-dedup] сквозная og-картинка "
                        f"{_r[:70]} — пропущена")
            return None
        return _r
    _w.cache_clear = getattr(_fn, "cache_clear", lambda: None)
    return _w


@_og_img_dedup_wrap
@_lru_cache_og(maxsize=512)
def _resolve_og_image_for_article(article_url: str, timeout: float = 5.0) -> str | None:
    """Generic og:image extractor for any web article.

    Used as fallback when trafilatura_v2 returned image=None
    (commonly happens on kp.ru/aif.ru where meta tags use non-standard attribute order
    like `<meta data-rh="true" property="og:image" content="...">`).

    Priority:
      1. og:image (any attribute order — robust regex)
      2. twitter:image
      3. <link rel="image_src" href="...">
      4. <meta itemprop="image" content="...">
      5. None
    """
    if not article_url or not article_url.startswith(("http://", "https://")):
        return None
    import requests as _rq_og
    import re as _re_og
    try:
        r = _rq_og.get(
            article_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    # 1. og:image — robust: scan every <meta> tag, match property=og:image regardless of attribute order
    for m in _re_og.finditer(r'<meta\b([^>]*)>', html, _re_og.I):
        attrs = m.group(1)
        attrs_low = attrs.lower()
        if 'property=' not in attrs_low or 'og:image' not in attrs_low:
            continue
        # confirm it's specifically og:image (not og:image:width)
        if not _re_og.search(r'property=["\']og:image["\']', attrs, _re_og.I):
            continue
        c = _re_og.search(r'content=["\']([^"\']+)["\']', attrs, _re_og.I)
        if c:
            url = c.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                # DL 2026-07-23: относительный og (irkutskmedia /opengraph/...) —
                # приклеиваем домен, иначе в media_url уходит битый путь.
                from urllib.parse import urljoin as _uj_m
                url = _uj_m(article_url, url)
            if _is_chrome_image(url) or _is_teaser_size_url(url):
                continue
            return url

    # 1b. <img itemprop="...image" src=...> — лид-картинка статьи (DL: region.center)
    for m in _re_og.finditer(r'<img\b([^>]*itemprop=["\'][^"\']*\bimage\b[^"\']*["\'][^>]*)>', html, _re_og.I):
        _tag = m.group(1)
        _s = _re_og.search(r'(?:src|data-src)=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', _tag, _re_og.I)
        if _s:
            url = _s.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                from urllib.parse import urljoin as _uj
                url = _uj(article_url, url)
            if not _is_chrome_image(url) and not _is_teaser_size_url(url):
                return url

    # 2. twitter:image
    for m in _re_og.finditer(r'<meta\b([^>]*)>', html, _re_og.I):
        attrs = m.group(1)
        if not _re_og.search(r'(?:name|property)=["\']twitter:image(?::src)?["\']', attrs, _re_og.I):
            continue
        c = _re_og.search(r'content=["\']([^"\']+)["\']', attrs, _re_og.I)
        if c:
            url = c.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                # DL 2026-07-23: относительный og (irkutskmedia /opengraph/...) —
                # приклеиваем домен, иначе в media_url уходит битый путь.
                from urllib.parse import urljoin as _uj_m
                url = _uj_m(article_url, url)
            if _is_chrome_image(url) or _is_teaser_size_url(url):
                continue
            return url

    # 3. <link rel="image_src">
    m_link = _re_og.search(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']', html, _re_og.I)
    if m_link:
        url = m_link.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            from urllib.parse import urljoin as _uj_l
            url = _uj_l(article_url, url)
        if not _is_chrome_image(url) and not _is_teaser_size_url(url):
            return url

    # 4. <meta itemprop="image">
    for m in _re_og.finditer(r'<meta\b([^>]*)>', html, _re_og.I):
        attrs = m.group(1)
        if not _re_og.search(r'itemprop=["\']image["\']', attrs, _re_og.I):
            continue
        c = _re_og.search(r'content=["\']([^"\']+)["\']', attrs, _re_og.I)
        if c:
            url = c.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                # DL 2026-07-23: относительный og (irkutskmedia /opengraph/...) —
                # приклеиваем домен, иначе в media_url уходит битый путь.
                from urllib.parse import urljoin as _uj_m
                url = _uj_m(article_url, url)
            if _is_chrome_image(url) or _is_teaser_size_url(url):
                continue
            return url

    # 5. Первый крупный <img> — ТОЛЬКО из тела статьи. DL 2026-07-23: скан ВСЕЙ
    # страницы хватал тизер соседней новости из ленты (Казань-«заправка» у РВ,
    # google-ads-заглушку у mosregtoday). Контейнер: от itemprop=articleBody до
    # </article>, иначе первый <article>; контейнера нет / фото в теле нет ->
    # None (пост без фото лучше случайного).
    _scope = None
    m_ab = _re_og.search(r'<[a-z][^>]*itemprop=["\']articleBody["\']', html, _re_og.I)
    if m_ab:
        _tail = html[m_ab.start():]
        m_end = _re_og.search(r'</article\b', _tail, _re_og.I)
        _scope = _tail[:m_end.start()] if m_end else _tail[:30000]
    else:
        m_art = _re_og.search(r'<article\b.*?</article>', html, _re_og.I | _re_og.S)
        if m_art:
            _scope = m_art.group(0)
    if _scope:
        for m in _re_og.finditer(
            r'<img[^>]+(?:src|data-src|data-original|data-lazy)=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']',
            _scope, _re_og.I,
        ):
            url = m.group(1).strip()
            if url.startswith("//"): url = "https:" + url
            elif url.startswith("/"):
                from urllib.parse import urljoin
                url = urljoin(article_url, url)
            # Skip very small images (icons, favicons, logos) + тизеры/RV-превью
            if _is_chrome_image(url) or _is_teaser_size_url(url):
                continue
            if ".thumb." in url.lower():
                continue
            # Skip tiny URLs (probably tracking pixels)
            if len(url) < 30:
                continue
            # Found candidate — return first match
            return url

    return None



def _bezformata_filter_top_block(base_url: str, posts: list) -> list:
    """Filter bezformata RSS posts: keep only те что в TOP "Новости <city>" блоке.

    2026-05-18: bezformata main page имеет 2 секции — TOP curated news (clean)
    и BOTTOM auto-feed (содержит lifestyle/рецепты/садоводство). Marker между
    ними: "Последние новости " (BOTTOM block heading).

    Strategy: fetch main page → find first "Последние новости " → chars[0:pos] = TOP →
    extract /listnews/<slug>/<id> IDs → filter posts.

    Graceful fallback (return full posts) если main page или marker недоступны.
    """
    if not posts:
        return posts
    import requests as _req
    import re as _re_top
    try:
        r = _req.get(
            base_url.rstrip("/") + "/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"},
        )
        if r.status_code != 200:
            logger.warning(f"[bezformata_top] {base_url}: main HTTP {r.status_code}, fallback to full RSS")
            return posts
        html = r.text
        # 2026-05-18 v2 fix: использовать regex с capital letter requirement.
        # str.find("Последние новости ") ловил false positive в navigation
        # tooltip ("Последние новости о пожарах" — pos 5424), а нужно actual
        # heading (h1 title="Последние новости Петропавловск-Камчатского", pos 32092).
        # Capital cyrillic letter после пробела гарантирует actual city name.
        marker_match = _re_top.search(r"Последние новости [А-ЯЁ]", html)
        if not marker_match or marker_match.start() <= 0:
            logger.warning(f"[bezformata_top] {base_url}: BOTTOM heading marker not found, fallback")
            return posts
        pos_bottom = marker_match.start()
        top_html = html[:pos_bottom]
        # Extract IDs from TOP block (curated news)
        top_ids = set(_re_top.findall(r"/listnews/[a-z0-9_-]+/(\d+)/?", top_html))
        if not top_ids:
            logger.warning(f"[bezformata_top] {base_url}: 0 IDs в TOP block, fallback")
            return posts
        # 2026-05-18: Broaden whitelist с /incident/ + /wildfire/ IDs.
        # TOP block имеет 18 curated picks; /incident/ + /wildfire/ добавляют
        # ~80 news IDs (без lifestyle). Это даёт RSS coverage 30-40 posts (vs 5).
        for section in ("/incident/", "/wildfire/"):
            try:
                sr = _req.get(
                    base_url.rstrip("/") + section,
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"},
                )
                if sr.status_code == 200:
                    section_ids = set(_re_top.findall(r"/listnews/[a-z0-9_-]+/(\d+)/?", sr.text))
                    top_ids |= section_ids
            except Exception as _se:
                logger.debug(f"[bezformata_top] {base_url}{section}: skip {type(_se).__name__}: {_se}")
        # Filter posts by ID matching
        filtered = []
        for p in posts:
            src_url = p.get("source_url") or ""
            id_match = _re_top.search(r"/listnews/[a-z0-9_-]+/(\d+)/?", src_url)
            if id_match and id_match.group(1) in top_ids:
                filtered.append(p)
        logger.info(f"[bezformata_top] {base_url}: filter {len(posts)} -> {len(filtered)} posts (TOP block has {len(top_ids)} IDs)")
        return filtered
    except Exception as e:
        logger.warning(f"[bezformata_top] {base_url}: filter error {type(e).__name__}: {e}, fallback")
        return posts


def _fetch_bezformata_rss(url: str) -> list:
    """Special handler для bezformata.com URLs.

    Их article URLs блокируются (anti-bot защита), но RSS feeds работают.
    Если url НЕ оканчивается на /rss.xml — нормализуем до main /rss.xml.
    Возвращаем list of dict в формате _fetch_rss/_fetch_web_html.
    """
    import requests
    import re as _re
    import xml.etree.ElementTree as _ET
    from datetime import datetime as _dt, timezone as _tz

    # Normalize: vladimir.bezformata.com/incident/ → vladimir.bezformata.com/rss.xml
    m = _re.match(r"(https?://[^/]+\.bezformata\.com)/", url)
    if not m:
        return []
    rss_url = m.group(1) + "/rss.xml"

    try:
        r = requests.get(rss_url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if r.status_code != 200:
            logger.warning(f"_fetch_bezformata_rss {rss_url}: HTTP {r.status_code}")
            return []
        root = _ET.fromstring(r.text)
    except Exception as e:
        logger.warning(f"_fetch_bezformata_rss fetch fail {rss_url}: {type(e).__name__}: {e}")
        return []

    posts = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        link_el = item.find("link")
        pub_el = item.find("pubDate")

        title = (title_el.text or "").strip() if title_el is not None else ""
        desc = (desc_el.text or "").strip() if desc_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""

        # Combine title + desc как полный текст
        if title and desc and not desc.lower().startswith(title.lower()[:20]):
            full_text = f"{title}. {desc}"
        else:
            full_text = desc or title
        if not full_text or len(full_text) < 150:
            continue

        # pubDate parse (RSS RFC 822 format: "Fri, 15 May 2026 00:49:05 +0500")
        pub_time = None
        if pub_el is not None and pub_el.text:
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S"):
                try:
                    pub_time = _dt.strptime(pub_el.text.strip(), fmt)
                    if pub_time.tzinfo is None:
                        pub_time = pub_time.replace(tzinfo=_tz.utc)
                    break
                except ValueError:
                    continue

        # Media: bezformata RSS включает <enclosure url="..." type="image/..."> — 64% items
        media_url = None
        media_type = None
        enc_el = item.find("enclosure")
        if enc_el is not None:
            url_attr = enc_el.get("url")
            type_attr = enc_el.get("type") or ""
            if url_attr and url_attr.startswith("http"):
                media_url = url_attr
                if type_attr.startswith("image/"):
                    media_type = "photo"
                elif type_attr.startswith("video/"):
                    media_type = "video"
                else:
                    media_type = "photo"  # fallback for image URLs
        posts.append({
            "text": full_text,
            "source_url": link,
            "media_url": media_url,
            "media_type": media_type,
            "media_files": [media_url] if media_url else [],
            "pub_time": pub_time,
            "_title": title,  # служебный: для пересборки text при full-body upgrade (Task CL)
        })

    # 2026-05-18 NEW: Filter posts via TOP block FIRST (curated news only).
    # User discovered bezformata main page имеет TOP "Новости" (clean) + BOTTOM "Последние" (lifestyle).
    posts = _bezformata_filter_top_block(m.group(1), posts)

    # 2026-05-15 + 2026-05-18 reorder: og:image upgrade ПОСЛЕ TOP filter.
    # Это применяет upgrade к ВСЕМ filtered (clean) items, не только first 20 raw.
    # Prevents случая когда clean items не получили upgrade because они были после первых 20 raw.
    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        article_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": m.group(1) + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        # Apply upgrade к ВСЕМ filtered posts (typically 4-15 items, fast)
        targets = [(i, posts[i]) for i in range(len(posts)) if posts[i].get("source_url")]
        with _TPE(max_workers=10, thread_name_prefix="bf_og") as _ex:
            # В ФЕТЧЕ — быстро и без ретрая (retries=0): og-апгрейд НЕ должен угрожать
            # 15с-бюджету фетча. Если bezformata тупит — отдаём gif, а полноразмер
            # добьёт prepare-слой (вне бюджета). Иначе медленный bezformata валит источник.
            futs = {_ex.submit(_bezformata_resolve_article, p["source_url"], article_headers, 5.0, 0): i
                    for i, p in targets}
            n_og, n_local, n_kept, n_body = 0, 0, 0, 0
            for fut in _ac(futs, timeout=8):  # кап 8с: og-апгрейд не должен съедать 15с-бюджет фетча
                idx = futs[fut]
                try:
                    url2, kind, body2 = fut.result(timeout=0.1)
                except Exception:
                    url2, kind, body2 = None, None, None
                if url2 and kind == "og":
                    posts[idx]["media_url"] = url2
                    posts[idx]["media_type"] = "photo"
                    posts[idx]["media_files"] = [url2]
                    n_og += 1
                elif url2 and kind in ("local", "source"):
                    # DL 2026-07-23: kind="source" (og первоисточника по «Источник»)
                    # раньше проваливался в else и терялся — применяем как local.
                    posts[idx]["media_url"] = url2
                    posts[idx]["media_type"] = "photo"
                    posts[idx]["media_files"] = [url2]
                    n_local += 1
                else:
                    n_kept += 1  # keep enclosure GIF as last-resort fallback
                # full-body upgrade (Task CL): полный текст статьи вместо RSS-тизера
                # из той же закачки HTML. Берём только если заметно длиннее тизера.
                if body2:
                    _teaser = posts[idx].get("text", "")
                    if len(body2) > len(_teaser) + 50:
                        _t = posts[idx].get("_title", "") or ""
                        if _t and not body2.lower().startswith(_t.lower()[:20]):
                            posts[idx]["text"] = _t + ". " + body2
                        else:
                            posts[idx]["text"] = body2
                        n_body += 1
        logger.info(f"[bezformata] {rss_url}: og:image upgrade og={n_og} local={n_local} kept_gif={n_kept} full_body={n_body} (of {len(targets)} post-filter)")
    except Exception as _og_err:
        logger.warning(f"[bezformata] og:image resolve failed for {rss_url}: {_og_err}")

    # снять служебный ключ _title перед возвратом (Task CL)
    for _p in posts:
        _p.pop("_title", None)
    return posts



def _fetch_web_html(url: str) -> list:
    """HTML extraction через trafilatura_v2: текст + image + полный article body.
    Возвращает посты в том же формате что _fetch_rss (list of dict).
    Используется для source_type=='web' в news_realtime_engine.

    Преимущества над _fetch_rss:
    - картинка из meta (image) — обязательное поле
    - полный текст статьи (вместо summary)
    - покрывает сайты без RSS (через HTML scrape)
    """
    # 2026-05-14 fix: bezformata.com блокирует deep article fetch (article URLs
    # возвращают пусто). Используем их RSS feed напрямую.
    if ".bezformata.com" in url:
        return _fetch_bezformata_rss(url)

    try:
        from trafilatura_v2 import fetch_articles_from_source
    except Exception as e:
        logger.error(f"trafilatura_v2 import fail: {e}")
        return []
    try:
        articles = fetch_articles_from_source(url, max_articles=4) or []  # 2026-05-16: 8→4 reduce sub-fetches (50% load)
    except Exception as e:
        logger.warning(f"trafilatura fetch fail {url[:60]}: {type(e).__name__}: {e}")
        return []
    posts = []
    for a in articles:
        if a.error:
            continue
        # Min-length: 150ch чтобы Claude не выдавал meta-ответы из-за короткого источника
        if not a.text or len(a.text) < 150:
            continue
        # Сборка text: title + ". " + body (если title не уже в начале body)
        title = (a.title or "").strip()
        body = (a.text or "").strip()
        if title and not body.lower().startswith(title.lower()[:30]):
            text = title + ". " + body
        else:
            text = body
        # Дата публикации (если есть)
        pub_time = None
        if a.date:
            try:
                from datetime import datetime, timezone
                # trafilatura date формат: "YYYY-MM-DD" или "YYYY-MM-DD HH:MM:SS"
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        pub_time = datetime.strptime(a.date.strip(), fmt).replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        continue
            except Exception:
                pass
        # 2026-05-16: og:image fallback when trafilatura_v2 didn't extract image
        # (common on kp.ru/aif.ru — non-standard meta attribute order).
        img_url = a.image
        if not img_url and a.url:
            try:
                img_url = _resolve_og_image_for_article(a.url, timeout=5.0)
            except Exception as _og_e:
                logger.debug(f"og:image fallback failed for {a.url[:60]}: {type(_og_e).__name__}")
                img_url = None
        posts.append({
            "text": text,
            "source_url": a.url,
            "media_url": img_url,
            "media_type": "photo" if img_url else None,
            "media_files": [img_url] if img_url else [],
            "pub_time": pub_time,
        })
    # 2026-08-11 (Якутск/1sn: один портрет у «топлива» и «дождей»): если ОДНА
    # картинка встречается у нескольких статей пачки — это сквозная картинка
    # сайдбара/листинга, а не фото статьи. Лучше без фото, чем чужое.
    _img_count = {}
    for _p in posts:
        if _p.get("media_url"):
            _img_count[_p["media_url"]] = _img_count.get(_p["media_url"], 0) + 1
    for _p in posts:
        _u = _p.get("media_url")
        if _u and _img_count.get(_u, 0) >= 2:
            logger.info(f"[web-img] сквозная картинка у {_img_count[_u]} статей "
                        f"{(_u or '')[:70]} — снимаем")
            _p["media_url"], _p["media_type"], _p["media_files"] = None, None, []
    return posts


def _rewrite(text: str, channel: str, media_type: str = None,
             ai_client=None, ai_provider: str = "groq", grid_prompt: str = "",
             grid_max_tokens: int = None) -> str:
    if ai_client is None:
        from processor.rewriter import _fallback_caption
        return _fallback_caption(text, channel)
    try:
        from processor.rewriter import rewrite
        kwargs = {}
        if grid_max_tokens is not None:
            kwargs["max_tokens"] = grid_max_tokens

        # Канальный override лимита токенов (приоритет над сеткой)
        try:
            cfg = load_config()
            ch_max = (cfg.get("channel_settings", {}).get(channel, {}) or {}).get("max_tokens")
            if ch_max is not None and str(ch_max).strip() != "":
                kwargs["max_tokens"] = int(ch_max)
        except Exception:
            pass

        return rewrite(text, channel, ai_client, media_type=media_type,
                       provider=ai_provider, grid_prompt=grid_prompt or None, **kwargs)
    except Exception as e:
        logger.warning(f"AI ({ai_provider}) недоступен: {e}")
        from processor.rewriter import _fallback_caption
        return _fallback_caption(text, channel)

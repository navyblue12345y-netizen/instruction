"""
Рерайт текста через AI (Groq / Claude / OpenAI).
Генерирует короткую подпись со смайликом для всех каналов.
"""
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone

from utils import load_config
import os
import cost_log

logger = logging.getLogger(__name__)
_SERVICE_NAME = os.environ.get("CB_SERVICE_NAME", "content-bot")

CHANNEL_TOPICS = {
    "moda": "мода, стиль, одежда, образы",
    "vyazanie": "вязание, крючок, спицы, рукоделие",
    "dom": "дом, интерьер, уют, уборка, лайфхаки для дома",
}

CHANNEL_EMOJIS = {
    "moda": "👗",
    "dom": "🏠",
    "vyazanie": "🧶",
}

DEFAULT_EMOJI = "✨"

# Тематические эмодзи для fallback-оформления коротких Дача-постов (медиа+подпись),
# которые НЕ проходят через LLM. channel_key -> emoji.
DACHA_FALLBACK_EMOJIS = {
    "skhemy_vyazaniya_3": "🧶",
    "masterskaya_vyaza_2": "🧶",
    "dachniki_2": "🌱",
    "dachnyj_ugolok_2": "🌱",
    "nash_dom_2": "🏠",
    "dom_povara_2": "🍳",
}


def _ensure_leading_emoji(text, channel):
    """Формат «эмодзи пробел текст»: вычищаем ВСЕ эмодзи из подписи и ставим один
    тематический эмодзи канала в начало (без хвостовых/лишних эмодзи)."""
    cleaned = []
    for ch in (text or ""):
        o = ord(ch)
        if (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
                or 0x2190 <= o <= 0x21FF or o == 0xFE0F or o == 0x200D):
            continue
        cleaned.append(ch)
    t = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    if not t:
        return None
    emoji = DACHA_FALLBACK_EMOJIS.get(channel) or DEFAULT_EMOJI
    return "%s %s" % (emoji, t)
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content_bot.db")

RU_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

EVENT_KEYWORDS = [
    "пасх", "рождеств", "новый год", "маслениц", "8 марта", "23 февраля",
    "день побед", "праздник", "конкурс", "мероприяти", "фестиваль", "акция",
    # FIX 2026-05-11: спортивные/массовые события — раньше Псков "26 апреля
    # марафон" пропускался, потому что "марафон" не был в списке.
    "марафон", "забег", "соревнован", "выставк", "концерт", "митинг",
    "шествие", "парад", "субботник", "выборы", "пробег", "турнир",
    "чемпионат", "матч", "спартакиад", "церемони", "торжеств", "юбилей",
    "открытие", "закрытие сезон",
]

PAST_MARKERS = [
    # FIX 2026-05-13: убраны слишком широкие маркеры которые ловили РЕПОРТАЖИ
    # о прошедших событиях (false positives для votkinsk/magnitogorsk/vladimir):
    #   "был/была/были", "состоял/состоялась", "прошел/прошла",
    #   "проведён/проведен/проведена/проведено",
    #   "стартовал/финишировал/завершил/завершён/завершен/закончил".
    # Эти слова часто встречаются в новостных репортажах о состоявшихся
    # мероприятиях — мы должны такие новости публиковать, а блокировать
    # только устаревшие АНОНСЫ (но без явной даты в тексте отличить сложно).
    # Оставлены явные временные маркеры и поздравления.
    "вчера", "позавчера", "прошл", "прошедш",
    "отметили", "поздравляем с", "поздравили с",
]


def _holiday_date_utc(name: str, year: int):
    """Дата праздника (UTC) для базовой проверки просроченных поздравлений."""
    n = (name or "").lower()
    if n == "new_year":
        return datetime(year, 1, 1, tzinfo=timezone.utc)
    if n == "christmas":
        return datetime(year, 1, 7, tzinfo=timezone.utc)
    if n == "womens_day":
        return datetime(year, 3, 8, tzinfo=timezone.utc)
    if n == "defender_day":
        return datetime(year, 2, 23, tzinfo=timezone.utc)
    if n == "victory_day":
        return datetime(year, 5, 9, tzinfo=timezone.utc)
    # Православная Пасха (минимально достаточная таблица на ближайшие годы)
    if n == "easter":
        easter = {
            2025: (4, 20),
            2026: (4, 12),
            2027: (5, 2),
            2028: (4, 16),
            2029: (4, 8),
            2030: (4, 28),
        }
        md = easter.get(year)
        if md:
            return datetime(year, md[0], md[1], tzinfo=timezone.utc)
    return None


def _is_late_holiday_greeting(t: str, now: datetime) -> bool:
    """Блокирует поздравления, если праздничная дата уже заметно прошла."""
    if not re.search(r"\b(поздравля\w*|желаем|со\s+светлой|с\s+праздник\w*|христос\s+воскрес)\b", t):
        return False

    holiday = None
    if re.search(r"\bпасх\w*|христос\s+воскрес\b", t):
        holiday = "easter"
    elif re.search(r"\bнов(ым|ого)?\s+год\w*\b", t):
        holiday = "new_year"
    elif re.search(r"\bрождеств\w*\b", t):
        holiday = "christmas"
    elif re.search(r"\b8\s*марта\b", t):
        holiday = "womens_day"
    elif re.search(r"\b23\s*феврал\w*\b", t):
        holiday = "defender_day"
    elif re.search(r"\bдн[её]м?\s+побед\w*\b", t):
        holiday = "victory_day"

    if not holiday:
        return False

    d = _holiday_date_utc(holiday, now.year) or _holiday_date_utc(holiday, now.year - 1)
    if not d:
        return False

    # Допускаем короткое послепраздничное окно в 3 дня
    return (now.date() - d.date()).days > 3

CAPTION_PROMPT = """Ты редактор женского канала. Напиши ОДНУ вдохновляющую подпись к посту.

ПРАВИЛА:
1. Одно предложение, максимум 10 слов
2. Начни с подходящего смайлика
3. Пиши о теме поста позитивно и вдохновляюще
4. Без хештегов, без ссылок, без упоминаний каналов
5. НИКОГДА не пиши про "ошибку", "блокировку", "проблему" — только позитив
6. Если текст непонятный — напиши красивую общую фразу по теме канала

Тема канала: {topic}
Оригинальный текст: {text}

Примеры формата:
😍 1 платье и 4 образа на любой случай.
🏠 Как спрятать электрощиток в прихожей.
✨ Трансформация старой кровати за один день.
🧶 Ажурный узор крючком для летней шали.
🌿 Эластичный набор петель — просто и красиво.

Напиши только подпись:"""


def _recent_repetitive_openers(channel: str, limit: int = 80) -> list[str]:
    """Возвращает частые стартовые биграммы из недавних постов канала для анти-однообразия."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT rewritten_text FROM posts WHERE channel=? AND rewritten_text<>'' ORDER BY id DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    pairs = []
    for (txt,) in rows:
        t = (txt or "").strip().lower()
        t = re.sub(r"^[^\wа-яА-ЯёЁ]+", "", t)
        words = re.findall(r"[a-zа-яё0-9-]+", t)
        if len(words) >= 2:
            pairs.append(f"{words[0]} {words[1]}")
        elif words:
            pairs.append(words[0])
    if not pairs:
        return []

    cnt = Counter(pairs)
    out = [p for p, n in cnt.most_common(8) if n >= 2]
    # Явно блокируем штампы из скрина
    for forced in ["красивая коса", "красивые салфетки", "красивая", "красивые"]:
        if forced not in out:
            out.append(forced)
    return out[:10]


def _parse_russian_date(text: str, now: datetime) -> datetime | None:
    t = (text or "").lower()

    # формат: 12 апреля
    m = re.search(r"\b(\d{1,2})\s+([а-яё]+)\b", t)
    if m:
        day = int(m.group(1))
        mon_word = m.group(2)
        month = None
        for k, v in RU_MONTHS.items():
            if mon_word.startswith(k):
                month = v
                break
        if month:
            try:
                return datetime(now.year, month, day, tzinfo=timezone.utc)
            except Exception:
                pass

    # формат: 12.04 / 12/04 / 12-04
    m2 = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", t)
    if m2:
        day = int(m2.group(1))
        month = int(m2.group(2))
        year = m2.group(3)
        year = int(year) if year else now.year
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day, tzinfo=timezone.utc)
        except Exception:
            return None

    return None


def _parse_date_range(text: str, now: datetime) -> "tuple[datetime, datetime] | None":
    """Parse Russian date ranges like 'с 19 мая по 23 мая' or '19-23 мая'.

    Returns (start_dt, end_dt) or None.
    Supported patterns (case-insensitive):
      "с 19 мая по 23 мая"
      "с 19 по 23 мая"         (month only at end)
      "с 19 мая по 23"         (month only at start, end same month)
      "19-23 мая" or "19—23 мая"
      "с 19 мая по 23 июня"    (cross-month)
    """
    months = {
        "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
        "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    }

    def _resolve_month(word: "str | None") -> "int | None":
        if not word:
            return None
        for key, num in months.items():
            if word.startswith(key):
                return num
        return None

    lower = (text or "").lower()
    tz = now.tzinfo or timezone.utc

    # First try: "с D1 [M1?] по D2 [M2?]"
    m = re.search(
        r"с\s+(\d{1,2})(?:\s+([а-яё]+))?\s+по\s+(\d{1,2})(?:\s+([а-яё]+))?",
        lower,
    )
    if m:
        d1, mo1_str, d2, mo2_str = m.group(1), m.group(2), m.group(3), m.group(4)
        mo1 = _resolve_month(mo1_str)
        mo2 = _resolve_month(mo2_str)
        # Fill in missing month from the side that has it
        if mo1 is None and mo2 is not None:
            mo1 = mo2
        if mo2 is None and mo1 is not None:
            mo2 = mo1
        if mo1 is not None and mo2 is not None:
            try:
                year = now.year
                start = datetime(year, mo1, int(d1), tzinfo=tz)
                end = datetime(year, mo2, int(d2), tzinfo=tz)
                # Handle year wrap (e.g., "с 28 декабря по 3 января")
                if end < start:
                    end = end.replace(year=year + 1)
                return start, end
            except Exception:
                return None

    # Second try: "D1-D2 month" or "D1—D2 month"
    m = re.search(r"(\d{1,2})\s*[-—–]\s*(\d{1,2})\s+([а-яё]+)", lower)
    if m:
        d1, d2, mo_str = m.group(1), m.group(2), m.group(3)
        mo = _resolve_month(mo_str)
        if mo is not None:
            try:
                year = now.year
                start = datetime(year, mo, int(d1), tzinfo=tz)
                end = datetime(year, mo, int(d2), tzinfo=tz)
                return start, end
            except Exception:
                return None

    return None


def is_outdated_event_text(text: str, now: datetime | None = None) -> tuple[bool, str]:
    """Проверка на просроченные событийные посты (например, прошедшие праздники)."""
    if not text:
        return False, ""
    now = now or datetime.now(timezone.utc)
    t = (text or "").lower()

    # FIX 2026-07-22 (Воронеж «Кража в Победе»): свежая новость О старом событии.
    # Судебно-следственная хроника всегда ссылается на дату инцидента в прошлом
    # («инцидент произошёл 15 мая ... суд вынес приговор») — старая дата тут
    # БЭКГРАУНД, а не дата события новости. Раньше фильтр обнулял такой рерайт,
    # и в канал уходил огрызок-фолбэк из исходника.
    if re.search(
        r"\b(?:суд\w*|приговор\w*|осужд\w+|оштраф\w+|возбужд\w+|задержа\w+|"
        r"арестова\w+|расследован\w+|следстви\w+|прокуратур\w+|уголовн\w+|"
        r"подвед\w+\s+итог\w+|вынес\w*\s+(?:приговор|решение|постановление))\b",
        t,
    ):
        return False, "aftermath context (court/investigation)"

    # Bug #8 Rule 2: date-range rescue — if multi-day event still ongoing
    # (end_date >= today), the post is NOT outdated even if start date is past.
    # Must come BEFORE any past-date checks below.
    _dr = _parse_date_range(t, now)
    if _dr is not None:
        _r_start, _r_end = _dr
        if _r_end.date() >= now.date():
            return False, ""

    has_event = any(k in t for k in EVENT_KEYWORDS)
    has_past = any(m in t for m in PAST_MARKERS)
    d = _parse_russian_date(t, now)
    has_past_date = bool(d and d.date() < now.date())

    if _is_late_holiday_greeting(t, now):
        return True, "late holiday greeting"

    # Явный кейс: Пасха и другие праздники после даты события
    if has_event and (has_past or has_past_date):
        return True, "event marker + past context"

    # 2026-05-18 FIX (Bug #3 defense-in-depth): короткие event-title с годом
    # ("Широкая Масленица 2026 в Ижевске", "Пасха-2026", "День всех влюбленных
    # 2026") — нет past markers, нет explicit даты, но пик события давно прошёл.
    # Проверяем: если в тексте seasonal keyword + упоминается ТЕКУЩИЙ год И
    # типичная дата события (peak) уже прошла с запасом 14 дней — outdated.
    _SEASONAL_PEAKS = {
        # keyword (lowercase, .find) → (month, day) или None для "easter" (динамич.)
        "маслениц": None,         # Maslenitsa: 7 дней до Лента, ~Feb-Mar
        "пасх": "easter",         # _holiday_date_utc("easter", year)
        "христос воскрес": "easter",
        "день влюбл": (2, 14),  # "День влюбл..."
        "всех влюбл": (2, 14),  # "День всех влюблённых/влюбленных" — с филлером "всех"
        "валентин": (2, 14),
        "8 март": (3, 8),
        "23 феврал": (2, 23),
        "день побед": (5, 9),
        "рождеств": (1, 7),
        "новый год": (1, 1),
    }
    cur_year_str = str(now.year)
    if cur_year_str in t:
        for kw, peak_spec in _SEASONAL_PEAKS.items():
            if kw not in t:
                continue
            peak_date = None
            if peak_spec == "easter":
                ed = _holiday_date_utc("easter", now.year)
                if ed:
                    peak_date = ed.date()
            elif peak_spec is None and kw == "маслениц":
                # Maslenitsa = неделя до Великого Поста = 7 нед до Пасхи. Грубо:
                # Easter - 49 дней (приблиз. начало масленичной недели).
                ed = _holiday_date_utc("easter", now.year)
                if ed:
                    from datetime import timedelta as _td
                    peak_date = (ed - _td(days=42)).date()  # середина масленичной недели
            elif isinstance(peak_spec, tuple):
                try:
                    peak_date = datetime(now.year, peak_spec[0], peak_spec[1]).date()
                except Exception:
                    pass
            if peak_date and (now.date() - peak_date).days > 14:
                return True, f"seasonal '{kw}' {cur_year_str} past peak ({peak_date})"

    # Общий кейс: событие с датой в прошлом
    if has_past_date and re.search(
        r"\b(праздник|конкурс|мероприят|фестиваль|акция|день|марафон|забег|"
        r"соревнован|выставк|концерт|митинг|шествие|парад|субботник|выборы|"
        r"пробег|турнир|чемпионат|матч|спартакиад|церемони|торжеств)\b",
        t,
    ):
        return True, "dated event in the past"

    # 2026-05-19 FIX: forward-looking announcement с прошедшей датой.
    # Кейсы: Барнаул "на 18 мая прогнозируют ЧС в Алтайском крае",
    # Иркутск "18 мая ожидаются сильные дожди". Дата уже прошла (1-14 дней),
    # но в тексте forward-looking глагол → это **анонс/прогноз** на прошедшую
    # дату, значит outdated. Backward-looking (произошло/открыли) — пропускаем,
    # это ретроспектива и легитимная новость.
    if d and 1 <= (now.date() - d.date()).days <= 14:
        FORWARD_LOOKING = re.compile(
            r"\b(?:ожида[еюя]т\w*|пройд[ёе]т\w*|пройдут|состоит\w*|состоятся|"
            r"планиру[еюя]\w*|запланир\w+|готовит[ься]\w*|намечен\w*|анонсир\w+|"
            r"прогнозиру[еюя]\w*|прогноз\s+(?:на|для|погод|чрезвычай)|"
            r"\bбуд[еу]т\s+(?:дожд|жар|холод|снег|ветер|гроз|тепл|похолод|потеплен)|"
            r"начн[еёу]т\w*|откро[еёю]т\w+\s+(?:с|в)\s|"
            r"объявлен[аы]?\s+(?:опасност|желт|оранжев|красн|штормов))\b",
            re.I,
        )
        # backward-looking исключения (ретроспектива, OK публиковать)
        BACKWARD_LOOKING = re.compile(
            r"\b(?:прош[её]л\w*|прошло|состоял\w+|произош\w+|открыл\w+|"
            r"начал\w+|закончил\w+|завершил\w+|опубликовал\w+|"
            r"задержан\w+|спасен\w+|был[аио]?\s+|оказал\w+|"
            r"арестов\w+|осужд[её]н\w+|погиб\w*|спасли)\b",
            re.I,
        )
        if FORWARD_LOOKING.search(t) and not BACKWARD_LOOKING.search(t[:300]):
            return True, f"forward-looking announcement for past date {d.date()}"

    # Bug #8 Rule 1 (2026-05-20): scheduled past one-day event detection.
    # Pattern: past date (1-7 days) + concrete time range (HH:MM до HH:MM) +
    # future-tense infrastructure verb (отключат/закроют/перестанут работать...).
    # Real case: Barnaul "19 мая с 13:30 до 16:00 светофоры перестанут работать"
    # published 20 May 08:04 МСК — 18 hours after the event ended.
    # The combination of all three signals is unique to scheduled past events;
    # legitimate forward news ("Завтра отключат воду") lacks the past date.
    if d and 1 <= (now.date() - d.date()).days <= 7:
        _has_time_range = re.search(
            r"\b\d{1,2}[:.]\d{2}\s*(?:до|—|–|-)\s*\d{1,2}[:.]\d{2}\b",
            t,
        )
        _has_scheduled_verb = re.search(
            r"\b(?:перестан\w*|прекрат\w*|отключ\w*|закро\w*|ограничат\w*|"
            r"перекро\w*|приостанов\w*)\b",
            t,
        )
        if _has_time_range and _has_scheduled_verb:
            return True, f"scheduled past one-day event ({d.date()})"

    # FIX 2026-05-11: ЖЁСТКАЯ ПРОВЕРКА — если в тексте есть однозначная дата
    # старше 7 дней назад, это явно устаревший инфоповод (анонс не может быть
    # на 7+ дней в прошлое). Это ловит Псков "26 апреля" даже если в тексте
    # формально нет ключевых слов событий. Применяется к ЛЮБОМУ тексту.
    #
    # FIX 2026-05-12: контекстные защиты от ложных срабатываний (kyzyl_v_max
    # блокировался на датах рождения 2000-12-05 = 9289 days old). Если дата
    # принадлежит явно НЕ текущему году — это историческая ссылка (год основания,
    # дата рождения, ретроспектива), а не событие "сейчас" — игнорируем.
    # Дополнительно: если рядом с датой биографические/исторические маркеры
    # ("родился", "основан", "построен", "с XXXX года", "в XXXX году" с прошлым
    # годом, "1812-1825" диапазоны) — тоже игнорируем.
    if d:
        days_old = (now.date() - d.date()).days
        if days_old > 7:
            # Защита 1: явный год не равен текущему → историческая дата
            if d.year != now.year:
                return False, ""
            # Защита 2: биографический/исторический контекст в тексте
            # (для случаев когда _parse_russian_date взял "5 марта" без года
            # и подставил текущий год, но в тексте речь про прошлое событие
            # как ретроспективу — типа "с марта в городе действует")
            if re.search(
                r"родил[ас]\w*|"
                r"основан\w*|построен\w*|создан\w*|открыт\w+\s+в\s+\d{4}|"
                r"\b(?:с|в)\s+\d{4}[-–—]\d{4}\s+год\w*|"  # диапазоны 1812-1825
                r"\b(?:с|в)\s+\d{4}\s+год\w*",  # "с 1985 года" / "в 2010 году"
                t,
            ):
                return False, ""
            return True, f"date {d.date()} is {days_old} days old"

    return False, ""


_AI_SYSTEM_BAD_PATTERNS = [
    r"вы\s+не\s+предоставили\s+текст",
    r"я\s+не\s+могу\s+переписать",
    r"please\s+provide",
    r"as\s+an\s+ai",
    r"не\s+могу\s+выполнить",
    r"уточните\s+текст",
    r"я\s+готов\b",
    r"^\s*готов[!,.\s]",
    r"отправь\s+(?:исходный\s+)?текст(?:\s+для\s+рерайта)?",
    r"жду\s+исход",
    r"мне\s+нужен\s+исходный\s+текст",
    r"пожалуйста,?\s+поделись\s+полным\s+текстом",
    r"я\s+создам\s+ёмкий",
    r"вы\s+отправили\s+только\s+заголовок",
    r"вы\s+указали\s+только\s+заголовок",
    r"не\s+вижу\s+в\s+переданном\s+тексте\s+достаточной\s+информации",
    r"предоставьте\s+полный\s+текст",
    r"я\s+отредактирую\s+его\s+в\s+соответствии",
    r"текст\s+неполный",
    r"отсутствует\s+основная\s+информация",
    r"недостаточно\s+информации\s+для\s+рерайта",
    # Добавлено 2026-04-25 — ловим отказы Claude вида "не содержит конкретной информации"
    r"этот\s+текст\s+(?:слишком\s+)?корот",
    r"этот\s+пост\s+(?:слишком\s+)?корот",
    r"не\s+содержит\s+(?:конкретн|достаточн|материал|информ|связн)",
    r"текст\s+слишком\s+крат",
    r"мало\s+инф\w+\s+для",
    r"недостаточ\w+\s+(?:инф|контекст|данн)",
    r"нечего\s+переписыв",
    r"нечего\s+редактиров",
    r"я\s+не\s+могу\s+опубликовать",
    r"не\s+содержит\s+материала",
    r"это\s+не\s+текст\s+для",
    r"слишком\s+мало\s+текста",
    r"текст\s+не\s+содержит",
    # Добавлено 2026-04-29 — мета-комментарии Claude о тематике (вместо самого поста).
    # Кейс: Claude отвечает "Исходник содержит только ... Это допустимая тематика."
    # вместо того, чтобы сразу выдать рерайтнутый пост. Такой ответ нельзя
    # публиковать — нужно дать другому кандидату шанс.
    r"исходник\s+содержит",
    r"исходн[ыа]\w*\s+(?:текст|пост)\s+содержит",
    r"допустим[аяое]\w*\s+тематик",
    r"это\s+допустим[аяое]",
    r"военн[ыоа]\w*\s+тематик",
    r"без\s+военной",
    r"не\s+содержит\s+военн",
    r"техническ[аяое]\w*\s+ремарк",
    r"^\s*заголовок\s*:",
    r"^\s*текст\s+поста\s*:",
    r"соответствует\s+тематик",
    r"не\s+нарушает\s+требовани",
    r"контент\s+допустим",
    # Добавлено 2026-04-29 (вторая волна) — Claude просит дать ему текст
    # ("Пожалуйста, отправьте исходный текст поста...") — это тоже мета.
    r"пожалуйста,?\s+отправь?(?:те)?\s+исходный",
    r"пожалуйста,?\s+отправь?(?:те)?\s+(?:полный\s+)?текст",
    r"пожалуйста,?\s+(?:дайте|пришлите|поделитесь)\s+(?:исходный|полный|текстом|материал)",
    r"проверю\s+его\s+на\s+военн",
    r"если\s+тема\s+разрешен",
    r"выдам\s+(?:готов|переписанн|итогов)",
    # Служебные TG-сообщения от админов канала (1-е лицо, не новости).
    # Кейс: "Я пригласил фото в закреп" (Хабаровск, 29.04 13:04 МСК).
    # Это короткие командные сообщения — фетч их подхватил, Claude
    # их не отбросил.
    r"^\s*[\W_]*\s*я\s+пригласил",
    r"^\s*[\W_]*\s*я\s+(?:добавил|отправил|переслал|закрепил|опубликовал|загрузил|снял|удалил)\b",
    r"^\s*[\W_]*\s*я\s+пишу\s+(?:тут|здесь|сюда)",
    r"\bпригласил\s+(?:фото|видео|голосовое)\s+в\s+закреп",
    r"\bснял\s+с\s+закреп",
    r"\bзакреплен[оа]?\s+(?:в\s+)?канал",
    # Добавлено 2026-04-29 (третья волна, Рязань 19:03) — мета-ответы Claude
    # "Я понимаю, что вы проверяете мою компетентность. Это метаобращение..."
    # вместо реального поста.
    r"\bпроверя\w+\s+мою\s+компетентн",
    r"\bметаобращ\w+",
    r"\bне\s+участву\w+\s+в\s+тест\w+",
    r"\bя\s+работаю\s+по\s+инструкции",
    r"\bобрабатыва\w+\s+готовые\s+новости",
    r"\bне\s+создаю\s+контент",
    r"\bне\s+отвеча\w+\s+на\s+вопрос\w+\s+о\s+(?:моих|своих)",
    r"\bне\s+пост\s+для\s+(?:городского|канала)",
    r"\bесли\s+у\s+вас\s+есть\s+реальн\w+\s+новост",
    r"\bоформлю\s+(?:его|её|их)\s+по\s+всем\s+правил",
    # Добавлено 2026-04-30 (Таганрог 20:04 МСК после trafilatura rollout) —
    # Claude получает короткий фрагмент → возвращает meta-инструкцию
    # «Я вижу, что это заголовки... мне нужен полный исходный текст».
    r"\bя\s+вижу,?\s+что\s+это\s+(?:заголов|анонс)",
    r"\bэто\s+заголовки\s+или\s+анонс",
    r"\bанонс\w+\s+без\s+полного\s+текст",
    r"\bобработать\s+материал\s+по\s+(?:моим|своим)\s+правил",
    r"\bмне\s+нужен\s+полный\s+исходн",
    r"\bс\s+деталями\s+событ",
    r"\bпожалуйста,?\s+предостав",
    r"\bподготов(?:ить|лю)\s+корректн\w+\s+пост",
    r"\bтогда\s+я\s+смогу\s+подготов",
    r"\bкорректн\w+\s+пост\s+для\s+канал",
    r"\bинформацию\s+о\s+\w+\s+\(зачем",
    r"\b(?:контекст|деталь)\w+\s+и\s+(?:деталь|контекст)",
    # Добавлено 2026-05-05 (fix Class D): расширенные мета-фразы Claude.
    # Найдены реальными captions за 14 дней (см. sanity 2026-05-05) —
    # 28 случаев, 0 overshoot на новостных постах.
    r"^\s*привет[!,.\s]",  # Чита 'Привет! 👋', Иркутск 'Привет, друзья!'
    r"\bэто\s+(?:восклицание|выражение|слово)\s+(?:выражает|обозна|использу)",  # Чебоксары 'Ура! Это восклицание выражает'
    r"\bне\s+вижу\s+в\s+(?:этом\s+)?(?:исходник|тексте|сообщении|посте|материал)",  # Псков, Ростов, Йошкар-Ола, Тольятти
    r"\bэто\s+не\s+новость(?:\s+для\s+(?:публикации|канала|городск))?",  # Нальчик, Ставрополь, Ярославль, Кострома
    r"\bнедостаточно\s+информации\s+для\s+(?:обработки|оформления|публикации|поста)",  # Великий Новгород, Ярославль
    r"\bэто\s+риторическ\w+",  # Казань
    # 2026-05-17: после Tyumen digest incident — Claude правильно отказался,
    # но engine fallback опубликовал кашу. Эти patterns ловят случай:
    r"\bтекст\s+обрыва",  # "текст обрывается"
    r"\bтолько\s+заголовки",  # "содержит только заголовки"
    r"\bне\s+полноценн\w+\s+исходник",  # "не полноценный исходник для обработки"
    r"\bобрывки\s+заголов",
    r"\bначала\s+новостей",  # "начала новостей без полного содержания"
    # 2026-05-18: orekhovo 16:08 incident — Claude отказался обрабатывать
    # крипто-спам и явно сказал "Я редактор канала мирной повестки...
    # обрабатываю только легитимные события города... не контент для публикации".
    # Эти патерны раньше не покрывались, refusal опубликовался как пост.
    r"\bя\s+редактор\s+канал",
    r"\bобрабатыва\w+\s+только\s+легитимн",
    r"\bэто\s+(?:явно\s+)?спам(?:\s*[/\\]\s*мошен|/мошен)",
    r"\bкрипто[-\s]?схем",
    r"\bпризыв\w*\s+перейти\s+по\s+ссылк",
    r"\bне\s+(?:городская|местная)\s+новост",
    r"\bне\s+контент\s+для\s+публикац",
    r"\bлегитимн\w+\s+событ\w+\s+город",
]


# Hard ad-marker guard (Task BY 2026-05-28).
# Defense-in-depth against bypass of engine pre-rewrite filter.
# Patterns: erid with hash, Реклама.ООО, ИНН with 10/12 digits, ОГРН with 13/15 digits.
# False-positive rate near 0% - each pattern requires specific numeric/hash content.
_HARD_AD_GUARD_PATTERN = re.compile(
    r"\berid\b\s*:?\s*[A-Za-z0-9]{10,}|"
    r"Реклам[ау]\.\s*ООО|"
    r"\bИНН\s*:?\s*\d{10}(?:\d{2})?\b|"
    r"\bОГРН(?:ИП)?\s*:?\s*\d{13}(?:\d{2})?\b",
    flags=re.IGNORECASE,
)


def _is_ai_system_response(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(re.search(p, t, flags=re.IGNORECASE) for p in _AI_SYSTEM_BAD_PATTERNS)


# ============================================================
# Moderation hook (Task BV 2026-05-28)
# ============================================================
def _build_moderation_backend(cfg: dict):
    """Строит SemanticBackend по конфигу. None -> structural-only (fail-open)."""
    import os as _os
    sem = (cfg or {}).get("semantic_backend", "claude_haiku")
    fix = (cfg or {}).get("auto_fix_backend")

    def _claude():
        api_key = _os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        from anthropic import Anthropic
        from moderation.backends.claude_backend import ClaudeBackend
        return ClaudeBackend(client=cost_log.make_client("claude", service=_SERVICE_NAME, purpose="moderation", api_key=api_key), model="claude-haiku-4-5")

    def _deepseek():
        api_key = _os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            return None
        from moderation.backends.deepseek_backend import DeepSeekBackend
        return DeepSeekBackend(api_key=api_key)

    try:
        if sem == "claude_haiku":
            return _claude()
        if sem == "deepseek":
            det = _deepseek()
            if det is None:
                return None
            if fix == "claude":
                fb = _claude()
                if fb is not None:
                    from moderation.backends.hybrid_backend import HybridBackend
                    return HybridBackend(detect_backend=det, fix_backend=fb)
            return det
    except Exception as exc:
        logger.warning(f"[moderation hook] backend build failed: {exc}")
        return None
    return None


def run_moderation_hook(
    text: str,
    raw_text: str,
    channel: str,
    config_moderation: dict = None,
):
    """Run moderation pass on text. Returns text (possibly fixed), or None to skip slot.

    Gated by config_moderation["enabled"]. When disabled, returns text unchanged.
    Fail-open: on any error, returns text unchanged (do not break pipeline).
    """
    cfg = config_moderation or {}
    if not cfg.get("enabled"):
        return text

    enabled_channels = cfg.get("enabled_channels") or []
    if enabled_channels and channel not in enabled_channels:
        return text

    detection_only = bool(cfg.get("detection_only_mode", True))
    profile = cfg.get("profile", "news")

    backend = _build_moderation_backend(cfg)

    from moderation import moderate
    from moderation.audit import get_daily_cost_usd

    db_path = cfg.get("db_path") or "content_bot.db"

    ceiling = float(cfg.get("semantic_max_cost_per_day_usd", 50.0))
    if backend is not None:
        try:
            if get_daily_cost_usd(db_path) >= ceiling:
                logger.info(f"[moderation hook] daily cost ceiling reached - skipping semantic")
                backend = None
        except Exception:
            pass

    ctx = {
        "channel_key": channel,
        "slot_datetime": None,
        "region": "",
        "network": "",
        "network_type": "news",
        "db_path": db_path,
        "backend": backend,
        "detection_only_mode": detection_only,
        "auto_fix_max_attempts": int(cfg.get("auto_fix_max_attempts", 2)),
        "severity_overrides": cfg.get("severity_overrides") or {},
        "profile": profile,
    }

    if profile == "dacha":
        try:
            from moderation.dacha_clean import clean_dacha
            text = clean_dacha(text)
        except Exception:
            pass

    try:
        final_text, _info = moderate(text=text, raw_text=raw_text, ctx=ctx)
        if profile == "dacha" and final_text:
            try:
                from moderation.dacha_clean import dacha_clean_format
                _cf = dacha_clean_format(final_text, channel)
                final_text = _cf if (_cf and _cf.strip()) else None
            except Exception:
                pass
        return final_text
    except Exception as exc:
        logger.warning(f"[moderation hook] failed: {exc} - returning text unchanged")
        return text


def _looks_truncated(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    # явные обрывы на служебных словах
    if re.search(r"(?:^|\s)(и|а|но|или|либо|да|что|чтобы|как|если|когда|где|кто|в|на|по|с|к|из|у|от|до)\.?$", t, flags=re.IGNORECASE):
        return True
    # незакрытые скобки/кавычки
    if t.count("(") > t.count(")"):
        return True
    if t.count("«") > t.count("»"):
        return True
    if t.count('"') % 2 == 1:
        return True
    # типичный кейс вроде "20-летний и."
    if re.search(r"\b\d{1,2}-летн\w*\s+и\.?$", t, flags=re.IGNORECASE):
        return True
    return False


def _looks_foreign_language(text: str) -> bool:
    """Guard 2026-07-22 (Копейск/Магнитогорск «Zlatoust residents...»): модель
    изредка отвечает переводом на английский. Порог консервативный — латиница
    ЗАМЕТНО преобладает и её много; бренды в русском тексте (Honor X8c, GEELY,
    ISU, Wildberries) не задевает."""
    t = text or ""
    lat = len(re.findall(r"[A-Za-z]", t))
    if lat < 40:
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", t))
    return lat > cyr * 1.5


def _fallback_from_original(text: str, channel: str) -> str:
    """Безопасный fallback: 1-2 завершённых предложения из исходника."""
    src = (text or "").strip()
    if not src:
        return _fallback_caption(text, channel)
    src = re.sub(r"\s+", " ", src).strip()
    parts = split_sentences(src)
    out = " ".join([p.strip() for p in parts[:2] if p.strip()])
    out = out[:320].rstrip()
    if out and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", out):
        out += "."
    return out or _fallback_caption(text, channel)


def _is_news_channel(channel: str) -> bool:
    try:
        cfg = load_config()
        grids = cfg.get("grids", {}) or {}
        news_channels = set(grids.get("Города России", []) or [])
        news_channels.update(grids.get("Города лайв", []) or [])
        news_channels.update(grids.get("Города в MAX", []) or [])
        return channel in news_channels
    except Exception:
        return False


def _truncate_words(s: str, max_len: int) -> str:
    """Режет строку по последнему пробелу до max_len. Дополнительно: если
    итоговое окончание — служебное слово (предлог/союз из _PREPOSITIONS_LOWER),
    отрезает ещё одно слово и так далее. Защита: не режем если осталось <2 слов.

    Раньше функция могла оставить заголовок вида "...на улице Юрия в." — теперь
    такие хвосты сбрасываются в body, в title уходит более ранняя граница слов.
    """
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    cut = s[:max_len].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    # отрезаем хвостовые предлоги/союзы пока есть хотя бы 2 слова
    _trail_strip = ".!?…,;:-«»\"'"
    while " " in cut:
        last_word = cut.rsplit(" ", 1)[-1].lower().rstrip(_trail_strip)
        if last_word in _PREPOSITIONS_LOWER:
            new_cut = cut.rsplit(" ", 1)[0]
            if " " not in new_cut:
                break
            cut = new_cut
        else:
            break
    return cut.strip()


def _fit_first_sentence(title: str, max_len: int) -> str:
    """Пытается уместить первое предложение целиком в лимит; иначе мягко режет по словам."""
    t = re.sub(r"\s+", " ", (title or "").strip())
    if not t:
        return ""
    if len(t) <= max_len:
        return t

    parts = split_sentences(t)
    if parts:
        first = parts[0]
        if len(first) <= max_len:
            return first.rstrip(" ,;:-")

    return _truncate_words(t, max_len).rstrip(" ,;:-")


def _fit_complete_sentences(text: str, max_len: int) -> str:
    """Собирает только полные предложения в лимит, без обрыва хвоста."""
    src = re.sub(r"\s+", " ", (text or "").strip())
    if not src:
        return ""
    sents = split_sentences(src)
    out = []
    cur = ""
    for s in sents:
        cand = (cur + " " + s).strip() if cur else s
        if len(cand) <= max_len:
            cur = cand
            out.append(s)
        else:
            break
    if out:
        return " ".join(out).strip()
    # если даже первое не влезает — берём короткую завершённую выжимку
    short = _truncate_words(sents[0] if sents else src, max_len)
    short = short.rstrip(",;:—- ")
    if short and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", short):
        short += "."
    return short


def _remove_title_dup_from_body(title: str, body: str) -> str:
    t = (title or "").lower().strip()
    b = (body or "").strip()
    if not t or not b:
        return b

    t_norm = re.sub(r"[^\wа-яё\s]", " ", t)
    b_norm = re.sub(r"[^\wа-яё\s]", " ", b.lower())
    t_norm = re.sub(r"\s+", " ", t_norm).strip()
    b_norm = re.sub(r"\s+", " ", b_norm).strip()

    # если тело начинается тем же заголовком — срезаем дубль
    if b.lower().startswith(t):
        b = b[len(title):].lstrip(" .,:;—-\n")
        return b

    # мягкое совпадение по начальным токенам
    t_tokens = t_norm.split()[:6]
    if t_tokens:
        prefix = " ".join(t_tokens)
        if b_norm.startswith(prefix):
            # убрать первое предложение-дубль
            parts = split_sentences(b)
            if len(parts) > 1:
                return " ".join(parts[1:]).strip()

    return b


def _compress_news_body(body_src: str, title: str, max_len: int = 300) -> str:
    """Мини-суммаризация: суть в 1-2 завершённых предложениях, без дубля заголовка."""
    src = re.sub(r"\s+", " ", (body_src or "").strip())
    if not src:
        return ""

    # убрать дубли заголовка
    src = _remove_title_dup_from_body(title, src)

    # 1-2 предложения максимум
    sents = split_sentences(src)
    src = " ".join(sents[:2]) if sents else src

    body = _fit_complete_sentences(src, max_len)
    body = _remove_title_dup_from_body(title, body)

    # если всё ещё слишком длинно/тяжело — оставляем 1 компактное предложение
    if len(body) > max_len:
        first = (split_sentences(src) or [src])[0]
        # срез второстепенных хвостов после запятых
        if len(first) > max_len and "," in first:
            parts = [p.strip() for p in first.split(",") if p.strip()]
            acc = []
            cur = ""
            for p in parts:
                cand = (cur + ", " + p).strip(", ") if cur else p
                if len(cand) <= max_len - 1:
                    cur = cand
                    acc.append(p)
                else:
                    break
            first = cur or parts[0]
        body = _truncate_words(first, max_len).rstrip(",;:—- ")
        if body and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", body):
            body += "."

    return body.strip()


def _normalize_news_hyphenation(t: str) -> str:
    """Локальная нормализация дефисов/тире для новостного текста без ломки структуры."""
    s = (t or "")
    if not s:
        return s

    # Возраст/составные слова: 34-летняя, 10-этажный
    s = re.sub(r"\b(\d{1,4})\s*[—–-]\s*([а-яёa-z][а-яёa-z-]{1,40})\b", r"\1-\2", s, flags=re.IGNORECASE)

    # Длинное тире внутри слова -> дефис
    s = re.sub(r"(?<=[A-Za-zА-Яа-яЁё0-9])[—–](?=[A-Za-zА-Яа-яЁё0-9])", "-", s)

    return s


from text_split import (
    _PREPOSITIONS_LOWER,
    _COMPOUND_TOPONYMS,
    _TOPONYM_PREFIX_WORDS,
    _GEO_NAMED_WORDS,
    _safe_to_split,
    split_sentences,
)

def _format_news_two_part(text: str, original_text: str = "") -> str:
    """Формат MAX для новостей: короткий заголовок + (опционально) тело."""
    # FIX 2026-06-03: литеральные escape-последовательности из источника
    # (\n, \t, \r как ТЕКСТ — кейс Обнинск/bezformata) → пробел.
    _bs = chr(92)
    text = (text or "").replace(_bs + "n", " ").replace(_bs + "t", " ").replace(_bs + "r", " ")
    # EARLY PATH: если Claude уже отдал text с \n\n — уважаем его границу,
    # не пытаемся переразбить эвристиками. Закрывает кейсы:
    #  - Пенза (период после предлога «в»)
    #  - Челябинск (склейка через ЗАГЛАВНУЮ аббревиатуру ГИБДД)
    raw = (text or "").replace("**", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\n\n" in raw:
        head_block, _, body_block = raw.partition("\n\n")
        head_block = head_block.strip()
        body_block = body_block.strip()

        m_e = re.match(r"^([\U0001F300-\U0001FAFF\u2600-\u27BF])\s*(.*)$", head_block)
        emoji = m_e.group(1) if m_e else "📰"
        title = (m_e.group(2) if m_e else head_block).strip()

        title = re.sub(r"^[\-—–:]+\s*", "", title).rstrip(".!?… ")

        if len(title) > 110:
            title = _fit_first_sentence(title, 110)

        body = re.sub(r"^[\s.,!?;:]+", "", body_block).strip()

        # FIX 2026-07-22 (Донецк «...на ул.\n\nПетровского»): модель изредка рвёт
        # строку ПОСЛЕ сокращения — хвост-сирота уезжает отдельным абзацем.
        # Если заголовок кончается сокращением, а тело начинается одиноким
        # словом с заглавной — приклеиваем слово обратно к заголовку.
        if re.search(r"\s(?:ул|пр|пер|просп|пл|наб|ш|им|пос|мкр|корп|стр)\.?$", title, re.I):
            _m_orph = re.match(r"^([А-ЯЁ][а-яё]+)\s*(?:\n+|$)(.*)$", body, re.S)
            if _m_orph:
                title = f"{title.rstrip('.')}. {_m_orph.group(1)}"
                body = _m_orph.group(2).strip()

        # FIX 2026-05-11: защита 1 — заголовок НЕ должен заканчиваться предлогом/союзом.
        # Claude иногда нарушает (Рязань "...тысяч рублей В\n\nБашкортостане...").
        # Переносим последний предлог в начало body.
        title_words = re.findall(r"[А-Яа-яЁёA-Za-z]+", title)
        if title_words and title_words[-1].lower() in _PREPOSITIONS_LOWER:
            m_last = re.search(r"\s+[А-Яа-яЁёA-Za-z]+\s*$", title)
            if m_last:
                tail_prep = title[m_last.start():].strip()
                title = title[:m_last.start()].rstrip()
                if body and tail_prep:
                    body_first = body[0].lower() + body[1:] if body else ""
                    body = f"{tail_prep} {body_first}".strip()

        # FIX 2026-05-11: защита 2 — внутри title не должно быть склейки двух предложений.
        # Питер: "В Морском порту Кировского района загорелся ледокол «Ермак» На месте работают"
        # — две части без точки. Берём ПОСЛЕДНЮЮ валидную точку склейки —
        # заголовок остаётся максимально полным, правая часть уходит в body.
        #
        # FIX 2026-05-13: ужесточили lookbehind — раньше разделяли при ЛЮБОЙ строчной
        # букве/цифре слева, что приводило к ложным разрывам валидных заголовков:
        #   "разоблачил схему взлома Госуслуг..." → "...взлома" + "Госуслуг..."
        #   "назначена судьей Октябрьского..." → "...судьей" + "Октябрьского..."
        #   "В среду в центре и на севере Москвы..." → "...на севере" + "Москвы..."
        #   "перекрыли переезд на участке Ряжск..." → "...на участке" + "Ряжск..."
        # Теперь split срабатывает ТОЛЬКО когда левая часть заканчивается на закрывающую
        # кавычку или скобку (»") — это типичный конец цитаты/parenthetical, после
        # которого может реально начинаться новое предложение без точки (Питер «Ермак»).
        glue_re_t = re.compile(
            r"(?<=[»\"\)])\s+(?="
            r"(?:(?!Луну\b)(?:[А-ЯЁ][а-яё]{1,2}\s+)?[А-ЯЁ][а-яё]{3,})"
            r"|(?:В|Во|На|При|Для|Из|По|Под|От|К|У|С|Со)\s+[а-яё]{3,}"
            r"|(?:В|Во|На|При|Для|Из|По|Под|От|К|У|С|Со)\s+\d"
            r"|Это\b"
            r")"
        )
        title_glue_cands = []
        for m_glue in glue_re_t.finditer(title):
            left_t = title[:m_glue.start()].strip()
            right_t = title[m_glue.end():].strip()
            if 24 <= len(left_t) <= 110 and len(right_t) >= 8 and _safe_to_split(left_t, right_t):
                title_glue_cands.append((left_t, right_t))
        if title_glue_cands:
            left_t, right_t = title_glue_cands[-1]
            title = left_t
            if body:
                if right_t.rstrip().endswith((".", "!", "?")):
                    body = f"{right_t} {body}"
                else:
                    body = f"{right_t}. {body}"
            else:
                body = right_t + ("" if right_t.rstrip().endswith((".", "!", "?")) else ".")

        if not body or _is_ai_system_response(body):
            return f"{emoji} {title}".strip()

        if body and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", body):
            body += "."

        return f"{emoji} {title}\n\n{body}".strip()

    # EARLY PATH 2 (2026-05-04): single-line title-only caption ≤110 chars
    # без внутренних [.!?]\s+[А-Я] (= нет границ предложений) — это title без body
    # по промту "если исходник короткий — выдай только заголовок". Старый путь
    # ниже разрезает такие captions через glue-fix эвристики (Vladikavkaz/Mintrud).
    if "\n\n" not in raw and len(raw) <= 110 and not re.search(r"[.!?]\s+[А-ЯЁA-Z«„\"]", raw):
        m_e = re.match(r"^([\U0001F300-\U0001FAFF\u2600-\u27BF])\s*(.*)$", raw)
        emoji = m_e.group(1) if m_e else "📰"
        title = (m_e.group(2) if m_e else raw).strip()
        title = re.sub(r"^[\-—–:]+\s*", "", title).rstrip(".!?… ")
        return f"{emoji} {title}".strip()

    # Иначе — старый путь восстановления из однострочного формата
    # (Claude иногда отдаёт без \n\n, плюс backward-compat).
    t = re.sub(r"\s+", " ", raw)
    o = re.sub(r"\s+", " ", (original_text or "").replace("**", "").strip())

    m = re.match(r"^([\U0001F300-\U0001FAFF\u2600-\u27BF])\s*(.*)$", t)
    emoji = (m.group(1) if m else "📰")
    rest = (m.group(2) if m else t).strip()

    # Чиним склейку двух предложений без точки:
    # "... сбережениями граждан Кандидат экономических наук ..."
    for m_glue in re.finditer(r"(?<=[»\"\)])\s+(?=(?:(?!Луну\b)(?:[А-ЯЁ][а-яё]{1,2}\s+)?[А-ЯЁ][а-яё]{3,}|(?:В|Во|На|При|Для|Из|По|Под|От|К|У|С)\s+[а-яё]{3,}|Это\b))", rest):
        left = rest[:m_glue.start()].strip()
        right = rest[m_glue.end():].strip()
        if 24 <= len(left) <= 130 and len(right) >= 16 and _safe_to_split(left, right):
            rest = f"{left}. {right}"
            break

    parts = split_sentences(rest)
    if not parts:
        parts = [rest] if rest else []

    title = parts[0] if parts else (o[:120] if o else "Новость")
    title = re.sub(r"^[\-—–:]+\s*", "", title)
    title = title.rstrip(".!?… ")

    # Если модель склеила заголовок с пояснением через двоеточие/тире — укорачиваем заголовок.
    # 2026-05-17: НЕ обрезать если head < 30 chars — это цитата без контекста типа
    # «Мы зажигаем без огня»: семья из Хабаровска... — обрезка до цитаты убивает смысл.
    # Tail переносим в body вместо отбрасывания.
    for sep in (":", " — ", " - "):
        if sep in title and len(title) > 65:
            head, tail = title.split(sep, 1)
            if len(head.strip()) >= 30 and len(tail.strip()) >= 8:
                title = head.strip()
                break

    # Если очень длинный заголовок, переносим хвост в body
    o_parts = split_sentences(o)
    body_src = " ".join(parts[1:]).strip() or (" ".join(o_parts[:3]).strip() if o_parts else rest)

    # Дополнительная защита: если в title всё ещё склеились 2 предложения,
    # переносим хвост в body_src.
    for m_glue in re.finditer(r"(?<=[»\"\)])\s+(?=(?:(?!Луну\b)(?:[А-ЯЁ][а-яё]{1,2}\s+)?[А-ЯЁ][а-яё]{3,}|(?:В|Во|На|При|Для|Из|По|Под|От|К|У|С)\s+[а-яё]{3,}|Это\b))", title):
        left = title[:m_glue.start()].strip()
        right = title[m_glue.end():].strip()
        if 24 <= len(left) <= 130 and len(right) >= 10 and _safe_to_split(left, right):
            title = left
            body_src = (right + (". " + body_src if body_src else "")).strip()
            break

    if len(title) > 95:
        cut = _truncate_words(title, 85)
        tail = title[len(cut):].strip(" ,;:-") if len(title) > len(cut) else ""
        title = cut
        if tail:
            body_src = (tail + ". " + body_src).strip()

    looks_title_only = (len(o) <= 70) or (len(o_parts) <= 1 and len(o) <= 95)
    # Финальный лимит заголовка для новостей:
    # - title-only посты режем мягче, чтобы не терять смысл (как "... искусственного интеллекта")
    # - посты с body оставляем компактными
    title_limit = 130 if looks_title_only else 85
    title = _fit_first_sentence(title, title_limit)

    if looks_title_only:
        return f"{emoji} {title}".strip()

    body = _compress_news_body(body_src, title=title, max_len=520)
    if not body:
        body = _compress_news_body(o, title=title, max_len=520)
    body = re.sub(r"^[\s.,!?;:]+", "", body).strip()
    if body and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", body):
        body += "."

    if not body or _is_ai_system_response(body):
        return f"{emoji} {title}".strip()

    return f"{emoji} {title}\n\n{body}".strip()


def _source_has_cooking_steps(orig: str) -> bool:
    o = (orig or "").lower()
    if not o:
        return False
    markers = [
        "приготов", "как приготовить", "шаг", "смеш", "добав", "выпек", "запек", "вар", "жар",
        "туш", "нареж", "перемеш", "разогр", "духовк", "минут", "°c", "градус",
    ]
    if any(m in o for m in markers):
        return True
    if re.search(r"\b\d+\s*(мин|минут|°c|градус)", o):
        return True
    return False


def _format_generic_heading_layout(t: str) -> str:
    """Общий формат: заголовок, пустая строка, основной текст."""
    s = (t or "").strip()
    if not s:
        return s

    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Если уже есть блочная структура — просто нормализуем первую букву тела
    if "\n\n" in s:
        head, body = s.split("\n\n", 1)
        body = body.lstrip()
        if body:
            body = body[0].upper() + body[1:]
        return f"{head.strip()}\n\n{body}".strip()

    # 1) Частый кейс: "Заголовок: текст"
    m = re.match(r"^(\S+\s+[^:]{4,90}):\s*(.+)$", s)
    if m:
        head = m.group(1).strip().rstrip(" ,;:-")
        body = m.group(2).strip()
        if body:
            body = body[0].upper() + body[1:]
        return f"{head}\n\n{body}".strip()

    # 2) Кейс: "Заголовок. Текст"
    m2 = re.match(r"^(.{8,120}?[.!?])\s+(.+)$", s, flags=re.UNICODE)
    if m2:
        head = m2.group(1).strip().rstrip(":")
        body = m2.group(2).strip()
        if body:
            body = body[0].upper() + body[1:]
        return f"{head}\n\n{body}".strip()

    # 3) fallback: берём первую законченную фразу как заголовок
    m3 = re.match(r"^(.{8,140}?[.!?])\s+(.+)$", s, flags=re.UNICODE)
    if m3:
        head = m3.group(1).strip().rstrip(":")
        body = m3.group(2).strip()
        if body:
            body = body[0].upper() + body[1:]
        return f"{head}\n\n{body}".strip()

    return s


def _format_culinary_layout(t: str) -> str:
    """Для кулинарных каналов: заголовок + блоки с отступами и списками."""
    s = (t or "").strip()
    if not s:
        return s

    s = s.replace("**", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Приводим ключевые секции к блочному виду
    s = re.sub(
        r"\s*(Ингредиенты|Что\s+нужно|Что\s+нам\s+понадобится|Для\s+теста|Для\s+начинки|Для\s+панировки|Как\s+приготовить|Приготовление|Шаги)\s*:\s*",
        lambda m: f"\n\n{m.group(1)}:\n",
        s,
        flags=re.IGNORECASE,
    )

    # Если всё слеплено в одну строку — отделяем заголовок от тела
    if "\n\n" not in s:
        # 0) кулинарный частый кейс: "<эмодзи> Название блюда Описание..."
        m0 = re.search(r"^([^\n]{6,80}?)\s+([А-ЯЁ][а-яё]+,\s+.+)$", s, flags=re.UNICODE)
        if m0:
            head = m0.group(1).strip().rstrip(' ,;:-')
            body = m0.group(2).strip()
            if body:
                s = f"{head}\n\n{body}"

        # 1) стандартно: по первой завершённой фразе
        m = re.search(r"^(.{8,140}?[.!?])\s+(.+)$", s, flags=re.UNICODE)
        if m:
            head = m.group(1).strip()
            body = m.group(2).strip()
            if body:
                s = f"{head}\n\n{body}"
        else:
            # 2) fallback: часто модель склеивает заголовок и начало описания без точки
            m2 = re.search(r"^(.{8,90}?)\s+([А-ЯЁ][а-яё]{3,}.*)$", s, flags=re.UNICODE)
            if m2 and not re.search(r"[.!?]$", m2.group(1).strip()):
                head = m2.group(1).strip()
                body = m2.group(2).strip()
                if body and len(head.split()) >= 3:
                    s = f"{head}\n\n{body}"
            else:
                # 3) последний fallback для эмодзи-заголовков без явного разделителя
                if re.match(r"^[^\w\s]", s):
                    toks = s.split()
                    if len(toks) >= 8:
                        head = " ".join(toks[:6]).rstrip(" ,;:-")
                        body = " ".join(toks[6:]).strip()
                        if body:
                            s = f"{head}\n\n{body}"

    # Списки ингредиентов/пунктов
    s = re.sub(r":\s*[—-]\s+", ":\n— ", s)
    # Не режем любые тире по всему тексту, чтобы не ломать пары "ингредиент — количество"
    s = re.sub(r"(?<!\n)(\d{1,2}\.\s)", r"\n\1", s)

    # В блоке ингредиентов каждый пункт должен быть отдельной строкой
    def _fix_ing_block(m):
        body = m.group(2)
        raw_lines = []
        for raw in body.split("\n"):
            ln = (raw or "").strip()
            if not ln:
                continue
            ln = re.sub(r"^[—-]\s*", "", ln)
            raw_lines.append(ln)

        # Склеиваем пары вида:
        # "Сметана" + "100 г" -> "Сметана — 100 г"
        merged = []
        i = 0
        qty_re = re.compile(
            r"^(?:"
            r"~?\d+(?:[.,]\d+)?(?:\s*[–—-]\s*\d+(?:[.,]\d+)?)?\s*"
            r"(?:г|гр|кг|мл|л|"
            r"ст\.?(?:\s*л\.?)?|ч\.?(?:\s*л\.?)?|"
            r"столов(?:ая|ые)\s+ложк[аи]?|чайн(?:ая|ые)\s+ложк[аи]?|"
            r"шт\.?|зубч(?:ик|ика|иков)?\.?|щепотк\w*|по\s+вкусу)\b"
            r"|\d+\s*шт\.?"
            r"|по\s+вкусу"
            r")$",
            flags=re.IGNORECASE,
        )
        while i < len(raw_lines):
            cur = raw_lines[i]
            nxt = raw_lines[i + 1] if i + 1 < len(raw_lines) else ""
            if nxt and ("—" not in cur and "-" not in cur) and qty_re.search(nxt):
                merged.append(f"{cur} — {nxt}")
                i += 2
                continue
            merged.append(cur)
            i += 1

        lines = []
        for ln in merged:
            if not ln.startswith("— "):
                ln = "— " + ln
            lines.append(ln)

        body = "\n".join(lines)
        body = re.sub(r"\n{2,}", "\n", body)
        return f"{m.group(1)}:\n{body.strip()}"

    s = re.sub(
        r"(?is)(Что\s+нам\s+понадобится|Ингредиенты|Что\s+нужно|Для\s+теста|Для\s+начинки)\s*:\s*([\s\S]*?)(?=\n\n(?:Как\s+приготовить|Приготовление)\s*:|\Z)",
        _fix_ing_block,
        s,
    )

    # Чистим лишние пробелы и пустые строки
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _preserve_first_case(replacement_lower: str):
    """Return a re.sub callback that replaces matched text with `replacement_lower`,
    preserving the case of the first character of the matched text.

    Used to fix the bug where re.IGNORECASE + hardcoded lowercase replacement
    string would strip the capital letter from a sentence-initial compound
    word like 'Из-за' -> 'из-за'.
    """
    def _cb(m):
        matched = m.group()
        if matched and matched[0].isupper():
            return replacement_lower[0].upper() + replacement_lower[1:]
        return replacement_lower
    return _cb


# --- Task: strip meta recipe sections (нет ингредиентов в источнике) ---
_META_BLOCK_MARKERS = re.compile(
    r"(?i)(из\s+исходн\w+\s+текст|не\s+указан\w*\s+в\s+задани|не\s+указан[ыоа]\b|"
    r"не\s+формиру\w+|\bв\s+задани\w+|не\s+предоставлен\w*|отсутству\w+\s+в\s+исходн|"
    r"нет\s+(?:данн\w+|информац\w+|ингредиент\w*)|не\s+приведен\w*|не\s+приводятся|"
    r"подели\w+\s+полным\s+текст|пришлите\s+(?:полный\s+)?текст|"
    r"дали\s+только\s+заголов|без\s+контекст\w*\s+исходн)"
)

_RECIPE_SECTION_HEADER = re.compile(
    r"(?im)^\s*(Что\s+нам\s+понадобится|Ингредиенты|Что\s+нужно|Для\s+теста|"
    r"Для\s+начинки|Для\s+панировки|Как\s+приготовить|Приготовление|Шаги)\s*:?\s*$"
)


def _block_is_meta(content_lines):
    """True, если ВСЕ непустые строки секции — служебная мета (нет реального контента)."""
    nonempty = [x for x in content_lines if x.strip().strip("—-•*0123456789. )")]
    if not nonempty:
        return True
    return all(_META_BLOCK_MARKERS.search(x) for x in nonempty)


def _strip_meta_recipe_blocks(text):
    """Кулинария: убрать секции рецепта, чьё содержимое — мета-отговорка модели
    (в источнике нет ингредиентов/шагов). Заголовок и реальные секции сохраняются.
    Если остаётся только заголовок — это ожидаемо (видео без рецепта)."""
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    if not _RECIPE_SECTION_HEADER.search(text):
        # Нет заголовков секций: мета может быть прямо в теле. Чистим мета-строки,
        # кроме самой первой (заголовок блюда).
        kept = []
        for i, ln in enumerate(lines):
            if i > 0 and ln.strip() and _META_BLOCK_MARKERS.search(ln):
                continue
            kept.append(ln)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    preamble = []
    blocks = []
    cur_header = None
    cur_content = []
    seen = False
    for ln in lines:
        if _RECIPE_SECTION_HEADER.match(ln):
            if seen:
                blocks.append((cur_header, cur_content))
            else:
                preamble = cur_content
            cur_header = ln
            cur_content = []
            seen = True
        else:
            cur_content.append(ln)
    if seen:
        blocks.append((cur_header, cur_content))

    out = list(preamble)
    for header, content in blocks:
        if _block_is_meta(content):
            continue
        out.append(header)
        out.extend(content)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def beautify_content(text: str, channel: str = "") -> str:
    """Финальная шлифовка: пунктуация/пробелы/формат. + safety-маркеры устаревших событий."""
    t = (text or "").strip()
    if not t:
        return ""

    outdated, _ = is_outdated_event_text(t)
    if outdated:
        return ""

    if _is_ai_system_response(t):
        return ""

    is_culinary = channel in {"dom_povara", "dom_povara_2"}
    is_news = _is_news_channel(channel)

    # Нормализация пробелов и базовой пунктуации
    # Для кулинарии и новостей сохраняем переносы строк (структура заголовок/тело).
    if is_culinary or is_news:
        t = t.replace("\r\n", "\n").replace("\r", "\n")
    else:
        t = re.sub(r"\s+", " ", t)

    # Жёсткая очистка ссылочного мусора (для всех сеток)
    # 1) markdown inline links: [текст](https://...)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", t, flags=re.IGNORECASE)
    # 2) html links: <a href='...'>текст</a>
    t = re.sub(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", r"\1", t, flags=re.IGNORECASE)
    # 3) любые явные URL
    t = re.sub(r"https?://\S+", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(?:t\.me|telegram\.me|max\.ru)/\S+", " ", t, flags=re.IGNORECASE)
    # 4) @mentions
    t = re.sub(r"(?<!\w)@[a-zа-я0-9_]{3,}\b", " ", t, flags=re.IGNORECASE)

    # Удаляем хештеги в любом месте (не только в хвосте)
    t = re.sub(r"(?<!\w)#(?:[\wа-яё]+)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]+([,.!?;])", r"\1", t)
    t = re.sub(r"([,.!?;])(?![ \t\n]|$)", r"\1 ", t)
    # Не вставляем пробел в десятичных дробях: 6,5 / 6.5
    t = re.sub(r"(?<=\d)[,.]\s+(?=\d)", lambda m: m.group(0).strip(), t)
    if is_culinary or is_news:
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
    else:
        t = re.sub(r"\s{2,}", " ", t).strip()

    # Орфография: дефис vs тире
    # 1) Возраст/составные прилагательные: 10-летний, 65-летним, 3-этажный
    t = re.sub(r"\b(\d{1,4})\s*[—–-]\s*([а-яёa-z][а-яёa-z-]{1,40})\b", r"\1-\2", t, flags=re.IGNORECASE)

    # 2) Частицы -то/-либо/-нибудь: что-то, как-то, кто-либо, где-нибудь
    t = re.sub(
        r"\b(что|кто|где|куда|откуда|когда|зачем|почему|как|какой|какая|какое|какие|чей|чья|чьё|чьи)\s*[—–-]\s*(то|либо|нибудь)\b",
        r"\1-\2",
        t,
        flags=re.IGNORECASE,
    )

    # 3) Частые сложные слова, где иногда ошибочно ставится длинное тире с пробелами
    #    (из — за -> из-за, из — под -> из-под, всё — таки -> всё-таки и т.п.)
    t = re.sub(r"\bиз\s*[—–-]\s*за\b", _preserve_first_case("из-за"), t, flags=re.IGNORECASE)
    t = re.sub(r"\bиз\s*[—–-]\s*под\b", _preserve_first_case("из-под"), t, flags=re.IGNORECASE)
    t = re.sub(r"\bпо\s*[—–-]\s*над\b", _preserve_first_case("по-над"), t, flags=re.IGNORECASE)
    t = re.sub(r"\b(всё|все)\s*[—–-]\s*таки\b", r"\1-таки", t, flags=re.IGNORECASE)

    # Удаляем просьбы о реакциях/оценках
    t = re.sub(
        r"\b(?:как\s+вам(?:\s+идея)?\??|оцените(?:\s+идею|\s+пост|\s+способ|\s+это)?|"
        r"остав(?:ь|ьте)\s+реакц\w*|став(?:ь|ьте)\s+реакц\w*|постав(?:ь|ьте)\s+реакц\w*|"
        r"жду\s+реакц\w*)\b[.!?]*",
        "",
        t,
        flags=re.IGNORECASE,
    )

    # Удаляем клишированные хвосты "берите на заметку"
    t = re.sub(r"\bбери(?:те)?\s+на\s+заметку\b[.!?]*", "", t, flags=re.IGNORECASE)

    # Частая ошибка рерайта в новостях: "Ограничения коснут направлений"
    t = re.sub(r"\bкоснут\b(?=\s+[а-яё])", "коснутся", t, flags=re.IGNORECASE)

    # Удаляем оборванный хвост (например: "... 20-летний и")
    broken_tail = re.search(r"(?:^|\s)(и|а|но|или|либо|да|что|чтобы|как|если|когда|где|кто|в|на|по|с|к|из|у|от|до)\.?$", t, flags=re.IGNORECASE)
    if broken_tail:
        # сначала пробуем обрезать до последнего смыслового разделителя
        cut_pos = max(t.rfind(","), t.rfind(";"), t.rfind(" — "), t.rfind(" - "))
        if cut_pos > 20:
            t = t[:cut_pos].strip()
        else:
            # иначе убираем 1-2 последних слова
            t = re.sub(r"\s+\S+\s+(?:и|а|но|или|либо|да|что|чтобы|как|если|когда|где|кто|в|на|по|с|к|из|у|от|до)\.?$", "", t, flags=re.IGNORECASE).strip()
            t = re.sub(r"\s+(?:и|а|но|или|либо|да|что|чтобы|как|если|когда|где|кто|в|на|по|с|к|из|у|от|до)\.?$", "", t, flags=re.IGNORECASE).strip()

    # Убираем некорректные комбинации вроде "!." / "?."
    t = re.sub(r"([!?])\.(?=\s|$)", r"\1", t)
    t = re.sub(r"([!?])\.", r"\1", t)
    t = re.sub(r"\?\.+", "?", t)
    t = re.sub(r"!\.+", "!", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip(" ,;:-")
    t = re.sub(r"\n{3,}", "\n\n", t)

    # Точка в конце, если финал не завершён знаком
    if t and re.search(r"[\wа-яА-ЯёЁ\)\]\"]$", t):
        t += "."

    if is_culinary:
        t = _strip_meta_recipe_blocks(t)
        t = _format_culinary_layout(t)
    elif channel in {"dachnyj_ugolok_2", "skhemy_vyazaniya_3", "nash_dom_2", "masterskaya_vyaza_2", "dachniki_2"}:
        t = _format_generic_heading_layout(t)

    return t


def _empty_source_fallback(channel: str) -> str:
    # Для новостников не публикуем "пустые" заглушки
    if _is_news_channel(channel):
        return ""
    emoji = CHANNEL_EMOJIS.get(channel, DEFAULT_EMOJI)
    ch = (channel or "").lower()
    topic = (CHANNEL_TOPICS.get(channel, "") or "").lower()
    if "vyaz" in ch or "вяз" in ch or "вяз" in topic:
        return f"{emoji} Идея для вязания!"
    return f"{emoji} Свежая идея для вдохновения!"



_CREDIT_TOKEN_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_.\-]{2,29}$")
_CREDIT_DOMAIN_RE = re.compile(r"\.[a-z]{2,6}$", re.IGNORECASE)


def _strip_author_credit_lines(text):
    """Vyrezaet stroki-kredity avtorov iz syrca (dacha/vyazanie/ZhCA, 2026-08-18).

    Keis: post "Sad lyubitelya gortenziy." + strokoy nizhe nik "evitaludbarza"
    (kredit avtora video posle emodzi-markera) -> nik skleivalsya s zagolovkom.
    Udalyaem stroku-odinochny latinsky token esli: @-nik; soderzhit "_"/cifru;
    pohozh na domen; ili sosednyaya nepustaya stroka — emoji-only (kredit-marker).
    Stroki s probelami/russkim tekstom ne trogaem — sorta "Limelight" v spiskah zhivut.
    """
    if not text:
        return text
    lines = text.split("\n")
    n = len(lines)

    def _emoji_only(s):
        s = s.strip()
        return bool(s) and not re.search(r"[A-Za-zА-Яа-яЁё0-9]", s)

    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and _CREDIT_TOKEN_RE.match(s):
            tok = s.lstrip("@")
            drop = (s.startswith("@") or "_" in tok
                    or any(c.isdigit() for c in tok)
                    or bool(_CREDIT_DOMAIN_RE.search(tok)))
            if not drop:
                prev_ne = next((lines[j].strip() for j in range(i - 1, -1, -1)
                                if lines[j].strip()), "")
                next_ne = next((lines[j].strip() for j in range(i + 1, n)
                                if lines[j].strip()), "")
                drop = _emoji_only(prev_ne) or _emoji_only(next_ne)
            if drop:
                continue
        out.append(ln)
    return "\n".join(out)


def rewrite(text: str, channel: str, client, retries: int = 3,
            media_type: str = None, provider: str = "groq",
            grid_prompt: str = None, **kwargs) -> str:
    """
    Генерирует подпись через активный AI-провайдер.
    provider: "groq" | "claude" | "openai"
    grid_prompt: кастомный промпт сетки (если задан — используется вместо дефолтного)
    """
    # Hard ad-marker guard: defensive против обхода engine pre-rewrite filter (Task BY).
    # If raw contains erid/ИНН/ОГРН/Реклама.ООО -> return None,
    # engine sees None and continues -> next candidate from pool.
    if text and _HARD_AD_GUARD_PATTERN.search(text):
        logger.info("[ad_guard] hard ad marker in raw_text, returning None for try-next-candidate")
        return None

    # 2026-08-18: kredity avtorov (niki/domeny otdelnoy strokoy) — von iz
    # syrca do lyuboy obrabotki; tolko ne-novostnye (dacha/vyazanie/ZhCA).
    if text and not _is_news_channel(channel):
        text = _strip_author_credit_lines(text)

    # Дача/ЖЦА профиль: обрабатываем ИСТОЧНИК напрямую (D-блок + чистка/формат),
    # минуя креативный рерайт — экономия Claude, сохранение формата, без вымысла.
    try:
        from utils import load_config as _lc_d
        _mcfg = (_lc_d() or {}).get("moderation") or {}
        if (_mcfg.get("enabled") and _mcfg.get("profile") == "dacha"
                and channel in (_mcfg.get("enabled_channels") or [])):
            _raw_d = re.sub(r"\s+", " ", (text or "").strip())
            if len(_raw_d) < 30 or len(_raw_d.split()) <= 4:
                logger.info(f"[rewriter] dacha short input ({len(_raw_d)}c) -> fallback caption, no LLM")
                return _ensure_leading_emoji(_raw_d, channel) if _raw_d else None
            return run_moderation_hook(text=text, raw_text=text, channel=channel, config_moderation=_mcfg)
    except Exception as _de:
        logger.warning(f"[rewriter] dacha early-branch: {_de}")

    topic = CHANNEL_TOPICS.get(channel, "интересный контент")

    # Если у поста нет текста — не отправляем в LLM, ставим безопасный человеческий fallback.
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return _empty_source_fallback(channel)

    # Safe-mode: если текст короткий/бедный по фактам (особенно для media),
    # не позволяем модели "додумывать" детали и не даём публиковать служебные ответы.
    if raw:
        very_short = len(raw) <= 70 or len(raw.split()) <= 8
        low_info = bool(re.search(r"\b(бери(те)?\s+на\s+заметку|не\s+выбрасывай(те)?|смотри(те)?|подробности\s+видео|лайфхак)\b", raw, flags=re.IGNORECASE))

        # 2026-05-18 FIX: для news каналов safe-mode выдавал "✨ <title>." минуя
        # beautify_content/is_outdated_event_text/_is_ai_system_response.
        # Кейс: izhevsk_lajv sitemap "Широкая Масленица 2026 в Ижевске"
        # (32 chars, photo media) → "✨ Широкая Масленица 2026 в Ижевске." опубл.
        # Возвращаем "" — engine попробует следующего кандидата либо упадёт
        # в NO_SOURCES (что лучше чем мусорный сезонный пост в мае).
        if very_short and _is_news_channel(channel):
            return ""

        if ((media_type in ("video", "photo") and very_short) or (very_short and low_info)):
            base = raw
            base = re.sub(r"(?<!\w)#(?:[\wа-яё]+)", "", base, flags=re.IGNORECASE)
            base = re.sub(
                r"\b(?:как\s+вам(?:\s+идея)?\??|оцените(?:\s+идею|\s+пост|\s+способ|\s+это)?|"
                r"остав(?:ь|ьте)\s+реакц\w*|став(?:ь|ьте)\s+реакц\w*|постав(?:ь|ьте)\s+реакц\w*|"
                r"жду\s+реакц\w*)\b[.!?]*",
                "",
                base,
                flags=re.IGNORECASE,
            )
            base = re.sub(r"\bбери(?:те)?\s+на\s+заметку\b[.!?]*", "", base, flags=re.IGNORECASE)
            base = re.sub(r"\s+", " ", base).strip()
            base = re.sub(r"[!]{2,}", "!", base)
            base = re.sub(r"([!?])\.($|\s)", r"\1\2", base)
            base = base.rstrip(" .,:;—-")
            emoji = CHANNEL_EMOJIS.get(channel, DEFAULT_EMOJI)
            # Только аккуратный перефраз исходной фразы, без расширения смысла
            if not base:
                return emoji
            # Не добавляем точку после вопросительного/восклицательного знака
            if re.search(r"[!?…]$", base):
                return f"{emoji} {base}"
            return f"{emoji} {base}."

    forbidden_starts = _recent_repetitive_openers(channel)
    diversity_rules = ""
    if forbidden_starts:
        diversity_rules = (
            "\n\nАнти-однообразие:\n"
            "— Сформулируй уникальный, информативный заголовок/первую фразу по сути поста.\n"
            "— НЕ начинай с шаблонов: " + ", ".join(forbidden_starts[:8]) + ".\n"
            "— Избегай пустых эпитетов вроде 'красивая/красивые' без конкретики."
        )

    style_rules = (
        "\n\nПунктуация и стиль:\n"
        "— Пиши ТОЛЬКО на русском языке (названия брендов/техники можно оставлять как есть).\n"
        "— Идеальная русская пунктуация и орфография.\n"
        "— Пробелы после знаков препинания.\n"
        "— Заверши текст корректным знаком в конце."
    )

    # Новый pipeline (V2): grid_prompt без плейсхолдеров {text}/{topic} =
    # это полные инструкции для system. Текст идёт в user отдельно, плюс
    # включаем temperature=0.3 для дисциплинированного следования формату.
    # Старый pipeline сохранён ниже как fallback (с {text} в prompt → всё в user).
    use_system_pipeline = bool(grid_prompt) and ("{text}" not in grid_prompt) and ("{topic}" not in grid_prompt)

    system_prompt = ""
    # PROMPT_CACHING (2026-05-08): system_dynamic — часть промта которая
    # МЕНЯЕТСЯ от запроса к запросу (forbidden_starts) и поэтому НЕ кэшируется.
    # Идёт ПОСЛЕ system_prompt — это семантически эквивалентно старой склейке
    # `grid_prompt + diversity_rules + style_rules` (Claude видит ровно тот
    # же текст). Разбиение нужно только чтобы поставить cache_control только
    # на стабильную часть.
    system_dynamic = ""
    rewrite_temperature: float | None = None

    if grid_prompt:
        if use_system_pipeline:
            # Stable: grid_prompt (~2500 токенов, общий для всей сетки) — кэшируем.
            # Dynamic: diversity_rules (forbidden_starts, разные на каждом канале)
            #          + style_rules — небольшие блоки идущие после grid_prompt.
            system_prompt = grid_prompt
            system_dynamic = diversity_rules + style_rules
            prompt = (text or "")[:2000]
            rewrite_temperature = 0.3
        else:
            # Старый формат с плейсхолдерами {text}/{topic}
            prompt = grid_prompt.format(topic=topic, text=(text or "")[:2000])
            prompt = prompt + diversity_rules + style_rules
        max_tokens = kwargs.get("max_tokens", 800)
    else:
        prompt = CAPTION_PROMPT.format(topic=topic, text=(text or "")[:300]) + diversity_rules + style_rules
        max_tokens = kwargs.get("max_tokens", 60)

    provider_key = provider if provider in _PROVIDERS else "groq"
    # Слой 2 (structured): для claude-новостей Claude возвращает decision полем
    # через forced tool — skip физически не может утечь в контент. При publish
    # берём post_text и гоним через ту же пост-обработку. Если structured не дал
    # текста — fallback на обычный путь.
    if _use_structured(provider_key, grid_prompt, use_system_pipeline):
        _sd = _rewrite_structured(
            client, prompt, channel, retries, max_tokens,
            system_prompt=system_prompt, system_dynamic=system_dynamic,
            temperature=rewrite_temperature)
        if _sd.get("decision") == "skip":
            logger.info(f"[structured] {channel}: skip ({(_sd.get('skip_reason') or '')[:60]})")
            return "SKIP_NOT_NEWS"
        result = (_sd.get("post_text") or "").strip()
        if not result:
            result = _rewrite_via_provider(
                provider_key, prompt, channel, client, retries,
                max_tokens=max_tokens, fallback_text=text or "",
                system_prompt=system_prompt,
                system_dynamic=system_dynamic,
                temperature=rewrite_temperature,
            )
    else:
        result = _rewrite_via_provider(
            provider_key, prompt, channel, client, retries,
            max_tokens=max_tokens, fallback_text=text or "",
            system_prompt=system_prompt,
            system_dynamic=system_dynamic,
            temperature=rewrite_temperature,
        )

    _pre_beautify = result
    result = beautify_content(result, channel)
    # FIX 2026-07-22: beautify мог обнулить рерайт как «устаревший» — раньше это
    # маскировалось repair/fallback-цепочкой и в канал уходил огрызок из
    # исходника (Воронеж). Отбраковываем кандидата честно: preparer возьмёт
    # следующего (маркер <30 симв => ветка rewrite empty/short).
    if not result and _pre_beautify:
        try:
            if is_outdated_event_text(_pre_beautify)[0]:
                return "SKIP_OUTDATED"
        except Exception:
            pass
    # FIX 2026-07-22: рерайт не на русском (перевод-глюк модели) — отбраковка.
    if result and _looks_foreign_language(result):
        return "SKIP_NOT_RUSSIAN"

    # Анти-обрыв: если текст выглядит недописанным — делаем repair-pass.
    # FIX 2026-05-11: для НОВОСТНЫХ каналов repair-pass БЕЗ system_prompt
    # возвращал монолит без \n\n, что ломало формат заголовок\n\nтело и
    # приводило к склейкам ("ледокол «Ермак» На месте работают" в заголовке).
    # Решение: передаём system_prompt+system_dynamic в repair тоже —
    # Claude знает правила формата и сохраняет \n\n.
    if _looks_truncated(result):
        repair_prompt = (
            "Исправь текст, чтобы он был ПОЛНОСТЬЮ завершён и без обрывов. "
            "Сохрани смысл, не добавляй выдумок. Соблюдай ФОРМАТ: заголовок + ОДНА пустая строка + 1-2 предложения тела.\n\n"
            f"Исходник:\n{text or ''}\n\n"
            f"Проблемный рерайт:\n{result or ''}\n\n"
            "Верни только исправленный готовый текст."
        )
        try:
            repair_key = provider if provider in _PROVIDERS else "groq"
            # FIX 2026-05-11: для новостных каналов передаём system_prompt
            # тоже, чтобы repair сохранил формат заголовок\n\nтело.
            is_news_repair = _is_news_channel(channel) and bool(system_prompt)
            repaired = _rewrite_via_provider(
                repair_key, repair_prompt, channel, client, retries=1,
                max_tokens=max(int(max_tokens or 120), 120),
                fallback_text=text or "",
                system_prompt=system_prompt if is_news_repair else "",
                system_dynamic=system_dynamic if is_news_repair else "",
                temperature=rewrite_temperature if is_news_repair else None,
            )
            repaired = beautify_content(repaired, channel)
            # FIX 2026-05-11: ДОПОЛНИТЕЛЬНАЯ ЗАЩИТА — для новостных каналов
            # принимаем repaired ТОЛЬКО если в нём есть \n\n (правильный формат).
            # Иначе оставляем оригинальный result — даже если он "truncated",
            # это лучше чем монолит без \n\n.
            if repaired and not _looks_truncated(repaired):
                if _is_news_channel(channel) and "\n\n" not in repaired:
                    # repair вернул монолит — НЕ принимаем
                    pass
                else:
                    result = repaired
        except Exception:
            pass

    if not result or _looks_truncated(result):
        result = _fallback_from_original(text or "", channel)
        # fallback тоже должен пройти финальную пунктуационную шлифовку
        result = beautify_content(result, channel)

    if channel in {"dom_povara", "dom_povara_2"}:
        # Если в исходнике нет шагов/процесса приготовления, не оставляем выдуманный блок "Приготовление"
        if not _source_has_cooking_steps(text or ""):
            result = re.sub(r"\n\n(?:Как\s+приготовить|Приготовление)\s*:\s*[\s\S]*$", "", result, flags=re.IGNORECASE).strip()

    if _is_news_channel(channel):
        result = _format_news_two_part(result, original_text=text or "")
        result = _normalize_news_hyphenation(result)

    # Moderation hook (Task BV)
    try:
        from utils import load_config
        _cfg_mod = (load_config() or {}).get("moderation")
        _final_for_mod = result if isinstance(result, str) else None
        if _final_for_mod is not None:
            _result_after_mod = run_moderation_hook(
                text=_final_for_mod,
                raw_text=text,
                channel=channel,
                config_moderation=_cfg_mod,
            )
            if _result_after_mod is None:
                return None
            result = _result_after_mod
    except Exception as _mod_exc:
        logger.warning(f"[rewriter] moderation hook failed: {_mod_exc} - proceeding without moderation")

    # Последний защитный слой от артефактов вида "!." / "?."
    result = re.sub(r"([?!])\s*\.(?=\s|$)", r"\1", result or "")

    return result


def _openai_style_extract(response) -> str:
    """Извлекает текст из ответа API в стиле OpenAI chat.completions (Groq, OpenAI)."""
    return response.choices[0].message.content


def _claude_extract(response) -> str:
    """Извлекает текст из ответа Anthropic Claude messages API."""
    return response.content[0].text


def _groq_call(client, prompt: str, max_tokens: int):
    return client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.8,
    )


def _claude_call(client, prompt: str, max_tokens: int,
                 system_prompt: str = "", system_dynamic: str = "",
                 temperature: float | None = None):
    """
    Вызов Claude API.

    system_prompt:  стабильная часть system (одинакова между вызовами одной
                    сетки). При наличии — на неё ставится cache_control:
                    ephemeral, то есть Anthropic кэширует её на 5 мин и
                    последующие запросы платят 10% от input цены за этот
                    префикс вместо 100%.
    system_dynamic: динамическая часть system (forbidden_starts diversity
                    rules + style_rules). Идёт ПОСЛЕ stable части, без
                    cache_control. Текст один и тот же, что был раньше:
                    `grid_prompt + diversity_rules + style_rules` —
                    разбиение здесь только для целей кэширования.
    temperature:    если задан — переопределяет default (1.0).

    PROMPT_CACHING (2026-05-08): Haiku 4.5 требует ≥1024 токенов на
    кэшируемом блоке. grid_prompt сейчас ~2500 токенов — пройдёт точно.
    """
    kwargs = {
        "model": "claude-haiku-4-5",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        # PROMPT_CACHING: list of blocks с cache_control активируется ТОЛЬКО
        # когда system_prompt достаточно длинный. Anthropic минимум для Haiku
        # = 4096 input tokens на блоке кэша. На русском это ~10000 chars
        # (плотность ~2.4 chars/token).
        #
        # FIX 2026-05-11: ПОДНЯЛ ПОРОГ С 8500 ДО 10000 chars.
        # Анализ логов показал: при 8818 chars (~3674 tokens) Anthropic
        # ИГНОРИРОВАЛ cache_control (под минимумом 4096), и весь промт шёл
        # как RAW input. Это было невидимо в логах (sk=blocks), но cache_creation
        # и cache_read оставались = 0. Сетка v_max теряла ~$5/день из-за этого.
        # При 10000+ chars (>4167 tokens) кэш гарантированно активируется.
        full_system = system_prompt + (system_dynamic or "")
        if len(system_prompt) >= 10000:
            blocks = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            if system_dynamic:
                blocks.append({"type": "text", "text": system_dynamic})
            kwargs["system"] = blocks
        else:
            # промт пока короткий — передаём как строку (как было до правок)
            kwargs["system"] = full_system
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.messages.create(**kwargs)

    # PROMPT_CACHING (2026-05-08): логируем КАЖДЫЙ вызов чтобы видеть статус кэша.
    # cw=cache_creation, cr=cache_read. system_kind = "blocks" если list-of-blocks
    # (cache_control), иначе "string". sys_chars = длина system_prompt в chars.
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cr = getattr(usage, "cache_read_input_tokens", 0) or 0
            sys_obj = kwargs.get("system")
            system_kind = "blocks" if isinstance(sys_obj, list) else ("string" if sys_obj else "none")
            sys_chars = len(system_prompt or "")
            logger.info(
                f"[claude-cache] write={cw} read={cr} "
                f"input={getattr(usage, 'input_tokens', '?')} "
                f"sys_kind={system_kind} sys_chars={sys_chars}"
            )
    except Exception as e:
        logger.warning(f"[claude-cache] log error: {e}")
    return response


def _openai_call(client, prompt: str, max_tokens: int):
    return client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.8,
    )


# (provider_key): (api_call_fn, response_extract_fn, log_label, handles_rate_limit_explicit)
_PROVIDERS = {
    "groq":   (_groq_call,   _openai_style_extract, "Groq",   True),
    "claude": (_claude_call, _claude_extract,       "Claude", False),
    "openai": (_openai_call, _openai_style_extract, "OpenAI", False),
}


# ── СЛОЙ 2: structured output (перенос с Афиши 2026-07-24, эталон 2026-07-13) ──
# Claude возвращает решение publish/skip СТРУКТУРНЫМ ПОЛЕМ через forced tool call,
# а не свободным текстом. Рассуждение модели («Я редактор Telegram-канала, а не
# архитектурный критик…» — Нижнекамск 23.07) уходит в skip_reason и физически не
# может попасть в контент поста. Устраняет КЛАСС «мета-ответ утёк».
import os as _os
_STRUCTURED_REWRITE = _os.environ.get("STRUCTURED_REWRITE", "").strip().lower() in ("1", "true", "yes", "on")

_EMIT_POST_TOOL = {
    "name": "emit_post",
    "description": (
        "Верни результат обработки кандидата. ВСЕГДА вызывай этот инструмент и "
        "НИКОГДА не пиши обычным текстом. Если пост подходит каналу — decision=publish "
        "и post_text с готовым постом. Если не подходит (военная тема/СВО, не тот "
        "город/регион, реклама, мусор, мало фактов) — decision=skip и краткий skip_reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["publish", "skip"]},
            "post_text": {"type": "string",
                          "description": "Готовый пост: заголовок, пустая строка, 1-2 предложения тела. Только при decision=publish."},
            "skip_reason": {"type": "string",
                            "description": "Кратко почему пропуск. Только при decision=skip."},
        },
        "required": ["decision"],
    },
}


def _claude_call_structured(client, prompt: str, max_tokens: int,
                            system_prompt: str = "", system_dynamic: str = "",
                            temperature: float | None = None):
    """Как _claude_call, но с forced tool call emit_post — Claude обязан вернуть
    структурный ответ (decision + поля), а не свободный текст."""
    kwargs = {
        "model": "claude-haiku-4-5",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [_EMIT_POST_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_post"},
    }
    if system_prompt:
        full_system = system_prompt + (system_dynamic or "")
        if len(system_prompt) >= 10000:
            blocks = [{"type": "text", "text": system_prompt,
                       "cache_control": {"type": "ephemeral"}}]
            if system_dynamic:
                blocks.append({"type": "text", "text": system_dynamic})
            kwargs["system"] = blocks
        else:
            kwargs["system"] = full_system
    if temperature is not None:
        kwargs["temperature"] = temperature
    return client.messages.create(**kwargs)


def _extract_structured(response) -> dict:
    """Достаёт input из tool_use-блока emit_post. {} если блока нет."""
    for block in (getattr(response, "content", None) or []):
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == "emit_post":
            return dict(getattr(block, "input", {}) or {})
    return {}


def _rewrite_structured(client, prompt: str, channel: str, retries: int,
                        max_tokens: int, system_prompt: str = "",
                        system_dynamic: str = "", temperature: float | None = None) -> dict:
    """Structured-рерайт с retry. Возвращает {decision, post_text, skip_reason} или {}."""
    for attempt in range(max(1, retries)):
        try:
            resp = _claude_call_structured(client, prompt, max_tokens,
                                           system_prompt=system_prompt,
                                           system_dynamic=system_dynamic,
                                           temperature=temperature)
            data = _extract_structured(resp)
            if data.get("decision") in ("publish", "skip"):
                return data
        except Exception as e:
            logger.warning(f"[structured] rewrite attempt {attempt + 1} failed: {e}")
    return {}


def _use_structured(provider: str, grid_prompt, use_system_pipeline: bool) -> bool:
    """Structured применяется для claude-провайдера новостного пайплайна при
    включённом флаге STRUCTURED_REWRITE."""
    return bool(_STRUCTURED_REWRITE and provider == "claude"
                and grid_prompt and use_system_pipeline)


def _rewrite_via_provider(provider: str, prompt: str, channel: str, client,
                          retries: int, max_tokens: int = 60,
                          fallback_text: str = "",
                          system_prompt: str = "",
                          system_dynamic: str = "",
                          temperature: float | None = None) -> str:
    """Единая retry-обёртка для всех LLM-провайдеров.

    Заменяет три почти идентичные функции _rewrite_groq/_claude/_openai.
    Отличия провайдеров вынесены в _PROVIDERS: вызов API + экстрактор ответа
    + label для логов + флаг "знаем про rate-limit 'rate_limit_exceeded'".

    system_prompt + temperature: применяются только для Claude. Для других
    провайдеров игнорируются (groq/openai используют старый стиль).
    """
    spec = _PROVIDERS.get(provider) or _PROVIDERS["groq"]
    call_fn, extract_fn, label, handles_rate_limit = spec

    for attempt in range(retries):
        try:
            # Для Claude поддерживаем system+temperature через расширенную
            # сигнатуру _claude_call. Других провайдеров не трогаем.
            if provider == "claude" and (system_prompt or temperature is not None):
                response = _claude_call(client, prompt, max_tokens,
                                        system_prompt=system_prompt,
                                        system_dynamic=system_dynamic,
                                        temperature=temperature)
            else:
                response = call_fn(client, prompt, max_tokens)
            result = extract_fn(response).strip().strip('"\'')
            result = re.sub(r'#\S+', '', result).strip()
            if result:
                logger.info(f"[{channel}] {label} caption: {result}")
                return result
        except Exception as e:
            err = str(e)
            logger.warning(f"{label} ошибка (попытка {attempt+1}/{retries}): {err[:100]}")
            if handles_rate_limit and "rate_limit_exceeded" in err:
                time.sleep(30)
            elif attempt < retries - 1:
                time.sleep(2)
    return _fallback_caption(fallback_text, channel)


def _fallback_caption(text: str, channel: str) -> str:
    """Короткая подпись без AI — первое предложение + смайлик ниши."""
    emoji = CHANNEL_EMOJIS.get(channel, DEFAULT_EMOJI)
    if not text:
        return emoji
    m = re.match(r'^(.{10,80}?[.!?])', text.strip())
    if m:
        return f"{emoji} {m.group(1).strip()}"
    short = text.strip()[:80].rsplit(' ', 1)[0]
    return f"{emoji} {short}" if short else emoji

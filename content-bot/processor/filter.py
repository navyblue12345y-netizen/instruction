"""
Фильтр рекламных и нежелательных постов.
Поддерживает OCR для чтения текста с изображений (через Tesseract).
"""
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Нормализация: нижний регистр, убираем лишние символы и пробелы."""
    if not text:
        return ""
    # Нормализация Unicode (ё→е и т.п. не делаем — нужна ё для "ноченьки")
    t = text.lower()
    # Убираем лишние пробелы, переносы, табуляции
    t = re.sub(r'[\s\u00a0\u200b\u200c\u200d\ufeff]+', ' ', t)
    # Убираем эмодзи и спец символы, оставляем буквы/цифры/пробел/пунктуацию
    t = re.sub(r'[^\w\s\.,!?;:\-—\'\"«»]', ' ', t, flags=re.UNICODE)
    return t.strip()


# Regex-паттерны для приветствий и пожеланий (нельзя обойти вариациями)
_GREETING_PATTERNS = [
    # Доброе утро / добрый день / добрый вечер / доброй ночи
    r'добр\w+\s+(утр|ден|дн|вечер|ноч)\w*',
    # С добрым утром / с добрым вечером
    r'с\s+добр\w+\s+\w+',
    # Доброго утра/дня/вечера
    r'доброго\s+(утра|дня|вечера)',
    # Устойчивые фразы-пожелания
    r'хорошего\s+(дня|вечера|утра|уик-энда|дня)',
    r'прекрасного\s+(начала|дня|утра|вечера|уик)',
    r'спокойной\s+ноч\w+',
    r'сладких\s+снов',
    r'удачного\s+дня',
    r'приятного\s+(дня|вечера|утра)',
    r'пусть\s+(этот|сегодняшний)\s+(день|утро|вечер)',
    r'желаю\s+(вам|тебе|всем)\s+\w+\s+(дня|утра|вечера)',
    r'начн[еёи]\w+\s+день\s+с',
    r'заряд\w+\s+(на\s+весь\s+день|позитив)',
    # Утречко, утрешнее и т.п. — уменьшительно-ласкательные всегда приветствие
    r'\bутреч\w+',
    r'\bноченьк\w+',
    r'\bвечерок\w*',
    # "бодрого утра" / "чудесного утра" / "солнечного утра"
    r'\w+ого\s+утра\b',
    r'\w+ого\s+дня\b',
    # "поздравляю с новым днём" / "с новым утром"
    r'поздравля\w+\s+с\s+(новым|добрым)\s+(днём|утром|вечером)',
    # "желаю ... утра/дня/вечера"
    r'желаю\s+\w+\s+(утра|дня|вечера)',
    # "с новым днём" / "с новым утром"
    r'с\s+новым\s+(днём|утром|днем)',
    # Прямые приветствия в рерайте
    r'доброе\s+утро\b',
    r'добрый\s+(день|вечер)\b',
    r'доброй\s+ночи\b',
    # "Начни день/утро с улыбки/позитива/хорошего"
    r'начни\s+(день|утро|своё?\s+утро)\s+с\s+\w+',
    r'начн[иёе]\w*\s+(с\s+улыбки|с\s+позитива|с\s+хорошего)',
    # "с улыбки" / "с улыбкой" в начале предложения
    r'^[^.!?]*с\s+улыбк[иой]\b',
    # "день станет ярче/лучше" — типичные мотивационные шаблоны
    r'день\s+стан[еёи]\w+\s+(ярч|лучш|светл|счастлив)\w+',
    # "зарядись позитивом на весь день"
    r'зарядись\s+(позитивом|энергией|на\s+весь\s+день)',
    # "каждое утро" / "каждый день" + позитив
    r'каждое\s+утро\s+(начина|дари|приноси)\w+',
    # "просыпайся с улыбкой" / "просыпайся с радостью"
    r'просыпайся\s+с\s+(улыбк|радост|позитив)\w+',
    # "пожелание" / "желаем вам"
    r'желаем\s+(вам|тебе|всем)\s+(хорошего|прекрасного|отличного)',
    # "хорошего настроения" — универсальное пожелание
    r'хорошего\s+настроения',
    # "пусть каждый день" — мотивационные фразы
    r'пусть\s+каждый\s+(день|утро)\b',
]

_GREETING_RE = re.compile(
    '|'.join(f'(?:{p})' for p in _GREETING_PATTERNS),
    re.IGNORECASE | re.UNICODE
)


def is_greeting(text: str) -> bool:
    """True если текст содержит приветствие/пожелание которое нужно фильтровать."""
    if not text:
        return False
    normalized = _normalize(text)
    return bool(_GREETING_RE.search(normalized))


def extract_text_from_image(image_path: str) -> str:
    """Извлекает текст с изображения через Tesseract OCR."""
    if not image_path:
        return ""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 3")
        return text.strip()
    except Exception as e:
        logger.debug(f"OCR ошибка для {image_path}: {e}")
        return ""

AD_KEYWORDS = [
    # Спам про блокировку телеграм / переход в другие мессенджеры
    "блокировка telegram", "блокировка телеграм", "заблокируют телеграм",
    "телеграм заблокируют", "telegram заблокируют", "переходим в", "переходите в",
    "теперь мы в", "мы переехали в", "подписывайтесь в", "канал в max",
    "канал в вк", "канал в одноклассниках", "дублируем в",
    "резервный канал", "запасной канал", "зеркало канала",
    "срочно нужны", "обучим сами", "без опыта и знаний заработать",
    "вакансия", "набор на обучение", "пройди обучение",
    "т-банк", "тинькофф", "кэшбэк дня", "кэшбэк и бонусы",
    "промокод", "скидка", "акция на", "по акции",
    "промокод", "promo", "скидк", "скидочн",
    "реклам", "партнер", "спонсор",
    "купить по ссылке", "по ссылке в профиле",
    "донат", "задонатить", "поддержите",
    "оплат", "перевод на карту",
    "заказать у меня", "напишите в директ за ценой",
    "стоимость в директ", "цена в лс", "цена в директ",
    "подписывайтесь на мой", "мой магазин", "мой сайт",
    "ссылка в шапке", "ссылка в био",
    "affiliate", "#ad", "#реклама", "#партнерство",
    "новый бренд", "бренд ", "официальный сайт",
    "размерный ряд", "в наличии все размеры",
    "wb ", "wildberries", "ozon", "озон", "маркетплейс",
    "доставка по", "бесплатная доставка",
    # Продвижение чужих каналов
    "спасибо каналу", "переходи в канал", "подпишись на канал",
    "заходи в канал", "наш новый канал", "t.me/+", "t.me/joinchat",
]

PRICE_PATTERNS = ["руб.", "руб ", "₽", " р.", "rub"]

BUY_KEYWORDS = ["купить", "заказать", "приобрести", "оформить заказ", "в наличии", "цена", "стоимость"]

# Единый источник правды для мат-фильтра: regex с поддержкой обходов (б*л*я*дь).
# Объединён с тем что раньше был в fetcher.PROFANITY_REGEX_PATTERNS +
# старый substring-список MAT_KEYWORDS. Паттерны охватывают все формы,
# которые раньше ловил has_profanity через "хуев" и "нахер".
# WORD_BOUNDARY (2026-05-07): \b границы — иначе "рубля" ловит "бля", "Батрацкая" ловит "трац".
# Substring-ный поиск был критическим багом: re.sub('[^a-zа-яё0-9]+', '', t) склеивал
# слова, и "бл[яa@]" находил "бля" внутри "рубля", "сук" в "судосуки" и т.п.
PROFANITY_REGEX_PATTERNS = [
    r"\bб[лl][яa@]д\w*\b",           # бляд, блядский
    r"\bбл[яa@]\w*\b",                # бля, блять, бляха
    r"\bс[уy]к[аa@]\w*\b",            # сука, сукин
    r"\bх[уy][йиi1]\w*\b",            # хуй, хуи
    r"\bх[уy][её]в\w*\b",             # хуев, хуёв
    r"\bна[хx][уy][йиi1]\w*\b",       # нахуй
    r"\bна[хx]ер\w*\b",               # нахер, нахера
    r"\bп[иi1]зд\w*\b",               # пизд, пиздец
    r"\bе[б6][аa@]\w*\b",             # ебать, ебанутый
    r"\bёб\w*\b",                      # ёб, ёбаный
    r"\bе[б6]лан\w*\b",               # еблан
    r"\bмуд[аa@]к\w*\b",              # мудак
    r"\bгандон\w*\b",                  # гандон
    r"\bшлюх\w*\b",                    # шлюх
]


def _matches_profanity(text: str) -> bool:
    """Проверка на мат с word-boundary границами (без склеивания текста).

    WORD_BOUNDARY (2026-05-07): убран re.sub('[^a-zа-яё0-9]+', '', t) который раньше
    склеивал текст — это вызывало false-positives на "рубля" (содержит "бля"),
    "Батрацкая" (содержит "трац"), "цсукк" в "цисукку" и т.д.
    Теперь паттерны используют \b границы и ловят только полные слова.
    """
    if not text:
        return False
    t = text.lower()
    # Удаляем zero-width разделители (обходы вида б\u200bля)
    t = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060\u00ad]", "", t)
    for p in PROFANITY_REGEX_PATTERNS:
        try:
            if re.search(p, t, flags=re.IGNORECASE | re.UNICODE):
                return True
        except re.error:
            continue
    return False


def is_ad(text: str) -> bool:
    """Возвращает True если пост рекламный/нежелательный."""
    if not text:
        return False
    t = text.lower()

    # Прямые рекламные маркеры
    for kw in AD_KEYWORDS:
        if kw in t:
            return True

    # Цена + призыв к покупке = реклама
    has_price = any(p in t for p in PRICE_PATTERNS)
    has_buy = any(b in t for b in BUY_KEYWORDS)
    if has_price and has_buy:
        return True

    # Упоминание конкретного бренда/продукта + призыв = реклама
    brand_markers = ["размерный ряд", "антибактериальн", "защита от солнца",
                     "встроенные чашечки", "леггинс", "рашгард"]
    has_brand_detail = sum(1 for m in brand_markers if m in t)
    if has_brand_detail >= 2 and has_buy:
        return True

    return False


MALE_KEYWORDS = [
    "для мужчин", "мужской стиль", "мужская мода", "мужской гардероб",
    "мужской образ", "мужской лук", "мужская одежда", "мужские образы",
    "для него", "мужской джемпер", "мужской костюм", "мужская куртка",
]

def is_male_content(text: str) -> bool:
    """True если контент ориентирован на мужчин (не подходит для женского канала)."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in MALE_KEYWORDS)


# Ключевые слова по сезонам — блокируются когда сезон НЕ активен
SEASONAL_KEYWORDS = {
    "new_year": {
        "active_months": [11, 12, 1],  # ноябрь, декабрь, январь
        "keywords": [
            "с новым годом", "с рождеством", "рождественск",
            "предновогодн", "под ёлку", "новогодние подарки",
            "поздравляем с новым", "наступающим новым годом",
        ],
    },
    "feb23": {
        "active_months": [2],
        "keywords": ["23 февраля", "день защитника"],
    },
    "mar8": {
        "active_months": [3],
        "keywords": ["8 марта", "международный женский день", "с праздником весны"],
    },
    "may9": {
        "active_months": [5],
        "keywords": ["9 мая", "день победы", "с днём победы"],
    },
    "halloween": {
        "active_months": [10],
        "keywords": ["хэллоуин", "halloween"],
    },
}


def is_off_season(text: str) -> bool:
    """True если пост — праздничное поздравление не по текущему сезону."""
    if not text:
        return False
    from datetime import date
    current_month = date.today().month
    t = text.lower()
    for season, cfg in SEASONAL_KEYWORDS.items():
        if current_month in cfg["active_months"]:
            continue  # сейчас этот праздник актуален — не фильтруем
        if any(kw in t for kw in cfg["keywords"]):
            return True
    return False


def is_mostly_english(text: str) -> bool:
    """True если текст преимущественно на английском."""
    if not text:
        return False
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    latin = sum(1 for c in alpha if c.isascii())
    return latin > len(alpha) * 0.5


def should_skip(text: str, media_url: str = None, use_ocr: bool = True) -> bool:
    """
    True если пост нужно пропустить.
    use_ocr=True — читать текст с картинки через Tesseract если текст поста пустой.
    """
    if not text and not media_url:
        return True

    # OCR: читаем текст с картинки (всегда если есть медиа и это локальный файл)
    ocr_text = ""
    if media_url and use_ocr and not media_url.startswith("http"):
        ocr_text = extract_text_from_image(media_url)
        if ocr_text:
            logger.debug(f"OCR извлёк текст из {media_url}: {ocr_text[:100]}")

    # Используем оригинальный текст + OCR текст
    combined = (text or "") + " " + ocr_text

    if not combined.strip():
        return True

    if is_greeting(combined):
        return True
    if is_ad(combined):
        return True
    if _matches_profanity(combined):
        return True
    if is_off_season(combined):
        return True
    if is_mostly_english(combined):
        return True
    if is_male_content(combined):
        return True
    return False

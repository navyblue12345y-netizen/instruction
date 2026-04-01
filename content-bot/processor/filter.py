"""
Фильтр рекламных и нежелательных постов.
"""

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

MAT_KEYWORDS = [
    "бля", "блять", "пизд", "ебан", "хуй", "хуев",
    "ёбан", "нахуй", "нахер", "ёб твою",
]


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


def has_profanity(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in MAT_KEYWORDS)


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


def should_skip(text: str, media_url: str = None) -> bool:
    """True если пост нужно пропустить."""
    if not text and not media_url:
        return True
    # Пост без текста — пропускаем (не знаем что на картинке)
    if not text:
        return True
    if is_ad(text):
        return True
    if has_profanity(text):
        return True
    if is_off_season(text):
        return True
    if is_mostly_english(text):
        return True
    if is_male_content(text):
        return True
    return False

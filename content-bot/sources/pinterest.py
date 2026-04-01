"""
Парсинг Pinterest через публичный поиск (без авторизации).
"""
import re
import httpx
import logging

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Поисковые запросы по нишам
QUERIES = {
    "moda": [
        "модные образы весна 2026",
        "стильные луки женщинам 40+",
        "базовый гардероб женский",
        "французский стиль одежда",
        "капсульный гардероб 2026",
    ],
    "vyazanie": [
        "схемы вязания крючком",
        "техники вязания спицами",
        "вязание крючком для начинающих",
        "вязаные игрушки амигуруми схемы",
        "узоры вязания спицами",
    ],
    "dom": [
        "идеи интерьера квартиры",
        "уютный дом декор идеи",
        "лайфхаки уборка дом",
        "скандинавский интерьер",
        "хранение вещей дома идеи",
    ],
}

# Слова для фильтрации нерелевантного
SKIP_WORDS = [
    "купить", "магазин", "скидка", "sale", "shop", "store", "price",
    "по ссылке в", "ссылке в описани", "в шапке профиля", "в био",
    "перейди по", "переходи по", "оплат", "доставк",
]

MIN_TEXT_LEN = 15  # минимальная длина текста


def _parse_page(html: str, max_pins: int = 10) -> list[dict]:
    """Извлекает пины из HTML страницы Pinterest."""
    results = []

    # Находим скрипт с данными
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        if '"grid_title"' not in script:
            continue

        titles = re.findall(r'"grid_title":"([^"]+)"', script)
        imgs = re.findall(r"https://i\.pinimg\.com/736x/[a-f0-9/]+\.jpg", script)
        pin_urls = re.findall(r'"link":"(https://(?:www\.)?pinterest\.[a-z]+/pin/\d+[^"]*)"', script)
        descriptions = re.findall(r'"description":"([^"]{10,300})"', script)

        seen_imgs = set()

        for i, title in enumerate(titles):
            if len(results) >= max_pins:
                break

            img = imgs[i] if i < len(imgs) else None
            # Дедупликация по картинке
            if not img or img in seen_imgs:
                continue
            seen_imgs.add(img)

            url = pin_urls[i] if i < len(pin_urls) else "https://ru.pinterest.com/search/pins/"
            desc = descriptions[i] if i < len(descriptions) else ""

            # Используем описание если оно длиннее заголовка
            text = desc if len(desc) > len(title) else title
            text = text.strip()

            # Фильтруем рекламный контент и слишком короткий текст
            t_lower = text.lower()
            if any(w in t_lower for w in SKIP_WORDS):
                continue
            if len(text) < MIN_TEXT_LEN:
                continue
            # Пропускаем нерусскоязычный контент (больше половины латиницы)
            latin = sum(1 for c in text if c.isascii() and c.isalpha())
            if latin > len(text) * 0.5:
                continue

            results.append({
                "source_url": url,
                "text": text,
                "media_url": img,
                "media_type": "photo",
            })

        if results:
            break

    return results


def fetch_search(query: str, max_pins: int = 5) -> list[dict]:
    """Поиск пинов по запросу."""
    url = f"https://ru.pinterest.com/search/pins/?q={httpx.URL('', params={'q': query}).params}"
    try:
        resp = httpx.get(
            "https://ru.pinterest.com/search/pins/",
            params={"q": query},
            headers=HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
        pins = _parse_page(resp.text, max_pins)
        logger.info(f"Pinterest '{query}': найдено {len(pins)} пинов")
        return pins
    except Exception as e:
        logger.error(f"Pinterest ошибка '{query}': {e}")
        return []


def fetch_for_niche(niche: str, max_total: int = 10) -> list[dict]:
    """
    Собирает пины для ниши, чередуя поисковые запросы.
    """
    queries = QUERIES.get(niche, [])
    if not queries:
        return []

    results = []
    per_query = max(2, max_total // len(queries))

    for query in queries:
        if len(results) >= max_total:
            break
        pins = fetch_search(query, max_pins=per_query)
        # Дедупликация по URL
        existing_urls = {r["source_url"] for r in results}
        for p in pins:
            if p["source_url"] not in existing_urls:
                results.append(p)
                existing_urls.add(p["source_url"])

    logger.info(f"Pinterest [{niche}]: итого {len(results)} пинов")
    return results[:max_total]

"""
Парсинг RSS/сайтов: thesymbol.ru, club.osinka.ru, inmyroom.ru, alltime.ru
"""
import httpx
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ContentBot/1.0)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _get(url: str) -> BeautifulSoup | None:
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error(f"Ошибка загрузки {url}: {e}")
        return None


# ── thesymbol.ru ─────────────────────────────────────────────────────────────

def fetch_thesymbol(max_posts: int = 5) -> list[dict]:
    soup = _get("https://www.thesymbol.ru/fashion/news/")
    if not soup:
        return []
    results = []
    # thesymbol использует <article> с вложенными <a> и <img>
    articles = soup.select("article")[:max_posts * 2]
    for art in articles:
        link_el = art.select_one("a[href]")
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href.startswith("http"):
            href = "https://www.thesymbol.ru" + href
        # Текст из href (slug → читаемый заголовок) или alt изображения
        img_el = art.select_one("img")
        media_url = img_el.get("src") if img_el else None
        # Попробуем alt как заголовок
        title = (img_el.get("alt") or "").strip() if img_el else ""
        if not title:
            # Извлечём из URL slug
            slug = href.rstrip("/").split("/")[-1]
            title = slug.replace("-", " ").capitalize()
        if href and href != "https://www.thesymbol.ru/fashion/news/":
            results.append({
                "source_url": href,
                "text": title,
                "media_url": media_url,
                "media_type": "photo" if media_url else None,
            })
        if len(results) >= max_posts:
            break
    logger.info(f"thesymbol.ru: получено {len(results)} постов")
    return results


# ── club.osinka.ru ────────────────────────────────────────────────────────────

# Разделы с реальным полезным контентом по вязанию
OSINKA_KNITTING_FORUMS = [
    "forum-34",   # Советы и уроки по вязанию
    "forum-30",   # Авторские темы по вязанию
    "forum-52",   # Вяжем онлайн для взрослых
    "forum-78",   # Дом моделей
    "forum-146",  # Вязаный подиум
]

# Ключевые слова для фильтрации полезных тем (схемы, лайфхаки, уроки)
OSINKA_USEFUL_KEYWORDS = [
    "вяж", "схем", "узор", "крючк", "спиц", "урок", "мастер", "техник",
    "шаль", "носк", "свитер", "шапк", "варежк", "салфетк", "сумк",
    "жаккард", "косы", "ажур", "филейн", "ирланд", "энтрел", "цвет",
    "пряж", "петл", "вывяз", "убавл", "прибавл", "набор", "закрыт",
]

OSINKA_SKIP_KEYWORDS = [
    "закупочн", "магазин", "продаж", "пристрой", "организатор",
    "куплю", "продам", "отдам", "ищу описание", "помогите", "не могу",
    "закрытие сайта", "свежие новости", "объявлен",
]


def fetch_osinka(max_posts: int = 5) -> list[dict]:
    results = []

    for forum in OSINKA_KNITTING_FORUMS:
        if len(results) >= max_posts:
            break
        soup = _get(f"https://club.osinka.ru/{forum}")
        if not soup:
            continue

        for a in soup.select("a[href]"):
            if len(results) >= max_posts:
                break
            href = a.get("href", "")
            title = a.get_text(strip=True)

            if "topic" not in href or not title or len(title) < 8:
                continue

            t_lower = title.lower()

            # Пропускаем нерелевантные темы
            if any(kw in t_lower for kw in OSINKA_SKIP_KEYWORDS):
                continue

            # Берём только темы с полезными ключевыми словами
            if not any(kw in t_lower for kw in OSINKA_USEFUL_KEYWORDS):
                continue

            if not href.startswith("http"):
                href = "https://club.osinka.ru/" + href.lstrip("./")

            # Берём реальный текст и фото из первого поста темы
            body_text, media_url = _get_osinka_topic_content(href)

            # Если текст слишком короткий — пропускаем
            if not body_text or len(body_text) < 50:
                continue

            results.append({
                "source_url": href,
                "text": body_text,
                "media_url": media_url,
                "media_type": "photo" if media_url else None,
            })

    logger.info(f"club.osinka.ru: получено {len(results)} полезных постов")
    return results


def _get_osinka_topic_content(topic_url: str) -> tuple[str, str | None]:
    """
    Парсит первый содержательный пост темы осинки.
    Возвращает (text, image_url).
    """
    try:
        soup = _get(topic_url)
        if not soup:
            return "", None

        # Берём первый div.content (тело первого поста)
        content_divs = soup.select("div.content")
        if not content_divs:
            return "", None

        post_body = content_divs[0]

        # Убираем смайлики и цитаты
        for el in post_body.select("blockquote, div.quote, img[src*='smil'], img[src*='icon']"):
            el.decompose()

        text = post_body.get_text(separator=" ", strip=True)
        text = " ".join(text.split())  # нормализуем пробелы
        text = text[:700].strip()

        # Ищем первое реальное изображение
        media_url = None
        for img in post_body.select("img"):
            src = img.get("src", "")
            if not src:
                continue
            if not src.startswith("http"):
                src = "https://club.osinka.ru/" + src.lstrip("./")
            if ("smil" in src or "icon" in src or "avatar" in src
                    or "emoticon" in src or src.endswith(".gif")):
                continue
            media_url = src
            break

        # Также ищем внешние картинки в ссылках (часто хранят на radikal, imageban и т.д.)
        if not media_url:
            for a in post_body.select("a[href]"):
                href = a.get("href", "")
                if any(ext in href.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    if any(host in href for host in ["images.osinka", "imageban", "imgur", "radikal", "postimg"]):
                        media_url = href
                        break

        return text, media_url
    except Exception as e:
        logger.debug(f"Ошибка парсинга темы {topic_url}: {e}")
        return "", None


# ── inmyroom.ru ───────────────────────────────────────────────────────────────

def fetch_inmyroom(max_posts: int = 5) -> list[dict]:
    # inmyroom рендерится через JS — используем их sitemap/RSS или Google AMP версию
    soup = _get("https://www.inmyroom.ru/posts")
    if not soup:
        return []
    results = []
    # Ищем любые ссылки на статьи (длинные пути)
    links = [a for a in soup.select("a[href]")
             if "/posts/" in a.get("href", "") and len(a.get("href", "")) > 20]
    seen = set()
    for link in links[:max_posts * 3]:
        href = link.get("href", "")
        if not href.startswith("http"):
            href = "https://www.inmyroom.ru" + href
        if href in seen:
            continue
        seen.add(href)
        title = link.get_text(strip=True)
        img = link.select_one("img")
        media_url = img.get("src") or img.get("data-src") if img else None
        if title and len(title) > 10:
            results.append({
                "source_url": href,
                "text": title,
                "media_url": media_url,
                "media_type": "photo" if media_url else None,
            })
        if len(results) >= max_posts:
            break
    logger.info(f"inmyroom.ru: получено {len(results)} постов")
    return results


# ── alltime.ru ────────────────────────────────────────────────────────────────

def fetch_alltime(max_posts: int = 5) -> list[dict]:
    soup = _get("https://www.alltime.ru/blog/?page=list&blog=watchblog")
    if not soup:
        return []
    results = []
    posts = soup.select("div.blog-item, article, div.post")[:max_posts * 2]
    for post in posts:
        link_el = post.select_one("a[href]")
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href.startswith("http"):
            href = "https://www.alltime.ru" + href
        title_el = post.select_one("h2, h3, .title")
        title = title_el.get_text(strip=True) if title_el else link_el.get_text(strip=True)
        img_el = post.select_one("img")
        media_url = img_el.get("src") if img_el else None
        if title:
            results.append({
                "source_url": href,
                "text": title,
                "media_url": media_url,
                "media_type": "photo" if media_url else None,
            })
        if len(results) >= max_posts:
            break
    logger.info(f"alltime.ru: получено {len(results)} постов")
    return results

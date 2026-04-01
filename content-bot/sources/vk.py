"""
Парсинг публичных VK-групп через VK API (service token).

Получить service_token:
1. vk.com/apps → Создать приложение → Standalone
2. Настройки → Сервисный ключ доступа (service_token)
Публичные группы доступны без прав пользователя.
"""
import httpx
import logging

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.131"


def _resolve_group_id(group_name: str, token: str) -> int | None:
    """Получает числовой ID группы по короткому имени."""
    r = httpx.get(f"{VK_API}/groups.getById",
                  params={"group_id": group_name, "access_token": token, "v": VK_VERSION},
                  timeout=10)
    data = r.json()
    if "error" in data:
        logger.error(f"VK getById ошибка: {data['error']}")
        return None
    groups = data.get("response", [])
    return -groups[0]["id"] if groups else None


def fetch_group(group_name: str, max_posts: int = 10, token: str = "") -> list[dict]:
    """
    Получает последние посты публичной группы VK через API.
    group_name — slug (например 'vk.fstyle').
    token — VK service access token.
    """
    if not token:
        logger.warning("VK: service_token не задан, пропускаем VK")
        return []

    results = []
    try:
        # Получаем стену группы
        r = httpx.get(f"{VK_API}/wall.get",
                      params={
                          "domain": group_name,
                          "count": max_posts,
                          "filter": "owner",
                          "access_token": token,
                          "v": VK_VERSION,
                      },
                      timeout=15)
        data = r.json()

        if "error" in data:
            logger.error(f"VK wall.get ошибка для {group_name}: {data['error']}")
            return []

        items = data.get("response", {}).get("items", [])

        for item in items:
            # Пропускаем репосты
            if item.get("copy_history"):
                continue

            post_id = item.get("id")
            owner_id = item.get("owner_id")
            post_url = f"https://vk.com/wall{owner_id}_{post_id}"
            text = item.get("text", "").strip()

            # Медиа
            media_url = None
            media_type = None
            attachments = item.get("attachments", [])
            for att in attachments:
                if att["type"] == "photo":
                    sizes = att["photo"].get("sizes", [])
                    if sizes:
                        # Берём наибольшее фото
                        best = max(sizes, key=lambda s: s.get("width", 0))
                        media_url = best.get("url")
                        media_type = "photo"
                    break
                elif att["type"] == "video":
                    # Берём превью видео
                    video = att["video"]
                    imgs = video.get("image", [])
                    if imgs:
                        media_url = imgs[-1].get("url")
                        media_type = "photo"  # постим превью
                    break

            results.append({
                "source_url": post_url,
                "text": text,
                "media_url": media_url,
                "media_type": media_type,
            })

        logger.info(f"VK vk.com/{group_name}: получено {len(results)} постов")

    except Exception as e:
        logger.error(f"VK vk.com/{group_name} ошибка: {e}")

    return results

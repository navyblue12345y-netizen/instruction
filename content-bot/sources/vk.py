"""
Парсинг публичных VK-групп через VK API (service token).

Получить service_token:
1. vk.com/apps → Создать приложение → Standalone
2. Настройки → Сервисный ключ доступа (service_token)
Публичные группы доступны без прав пользователя.
"""
import httpx
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# DL 2026-08-06: глобальный rate-limiter — волна новостников готовит 200+
# каналов разом; без интервала пик легко пробивает ~3 rps сервисного ключа.
_RL_LOCK = threading.Lock()
_RL_LAST = [0.0]
_RL_MIN_INTERVAL = 0.35


_VK_STUB_THUMB = "/images/video/thumbs/video_x"

def _rate_gate():
    with _RL_LOCK:
        wait = _RL_MIN_INTERVAL - (time.time() - _RL_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _RL_LAST[0] = time.time()

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


_VK_VIDEO_MAX_SEC = 180   # длиннее — кадром: 47,6 МБ в MAX не лезет (ШЕФ 14.08)


def _build_player_url(video):
    """Ссылка на плеер из полей ленты — ВК в wall.get `player` не отдаёт
    (зонд 21.08: ветка vkvideo-2026-08-18 не срабатывала ни разу, 56 роликов
    за день ушли кадром). По video_ext.php?oid=&id=&hash= vk_video.download
    вытягивает HLS штатно (3 из 3, 1.9–25 МБ). None — если нет id/owner или
    ролик длиннее _VK_VIDEO_MAX_SEC."""
    oid, vid = video.get("owner_id"), video.get("id")
    if oid is None or vid is None:
        return None
    try:
        if int(video.get("duration") or 0) > _VK_VIDEO_MAX_SEC:
            return None
    except (TypeError, ValueError):
        pass
    url = "https://vk.com/video_ext.php?oid=%s&id=%s" % (oid, vid)
    if video.get("access_key"):
        url += "&hash=%s" % video["access_key"]
    return url


def _extract_media(item):
    """Медиа поста ВК -> {"media_url", "media_type"}.

    vkvideo-2026-08-18: у видео берём ссылку на плеер и подклеиваем превью
    запасным вариантом — препарер у слота вытянет сам ролик через HLS
    (sources/vk_video.py). Раньше публиковался только кадр-превью: 1884
    кандидата из 15510 и 68 постов за 18.08 ушли в эфир картинкой вместо видео.
    Служебную заглушку ВК (фикс «Пермь-лошади» 11.08) по-прежнему не берём.
    """
    from sources import vk_video as _vv
    _stub = globals().get("_VK_STUB_THUMB")
    media_url = None
    media_type = None
    for att in (item.get("attachments") or []):
        _t = att.get("type")
        if _t == "photo":
            sizes = (att.get("photo") or {}).get("sizes") or []
            if sizes:
                best = max(sizes, key=lambda s: s.get("width", 0))
                media_url = best.get("url")
                media_type = "photo"
            break
        if _t == "video":
            video = att.get("video") or {}
            imgs = video.get("image") or []
            preview = imgs[-1].get("url") if imgs else None
            if preview and _stub and _stub in preview:
                preview = None
            player = video.get("player") or _build_player_url(video)
            if player:
                media_url = _vv.pack(player, preview)
                media_type = "video"
            elif preview:
                media_url = preview
                media_type = "photo"      # плеера нет — как раньше, кадром
            break
    return {"media_url": media_url, "media_type": media_type}


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
        _rate_gate()
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
            # Пропускаем репосты и закрепы (закреп висит месяцами в топе стены)
            if item.get("copy_history") or item.get("is_pinned"):
                continue

            post_id = item.get("id")
            owner_id = item.get("owner_id")
            post_url = f"https://vk.com/wall{owner_id}_{post_id}"
            text = item.get("text", "").strip()

            # Медиа
            _m = _extract_media(item)
            media_url = _m["media_url"]
            media_type = _m["media_type"]

            # pub_time ОБЯЗАТЕЛЕН движку (свежесть, дедуп-окна, куратор)
            _ts = item.get("date") or 0
            pub_time = (datetime.fromtimestamp(_ts, timezone.utc)
                        if _ts else None)
            results.append({
                "source_url": post_url,
                "text": text,
                "media_url": media_url,
                "media_type": media_type,
                "pub_time": pub_time,
            })

        logger.info(f"VK vk.com/{group_name}: получено {len(results)} постов")

    except Exception as e:
        logger.error(f"VK vk.com/{group_name} ошибка: {e}")

    return results

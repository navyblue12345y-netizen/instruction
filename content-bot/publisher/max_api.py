"""
Публикация в каналы MAX через Bot API.
"""
import os
import httpx
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api.max.ru"


class MaxPublisher:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"Authorization": token, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, params: dict = None, **kwargs) -> dict | None:
        url = f"{BASE_URL}{path}"
        try:
            resp = httpx.request(method, url, headers=self.headers, params=params, timeout=30, **kwargs)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"MAX API {method} {path} → {resp.status_code}: {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"MAX API ошибка {path}: {e}")
            return None

    def upload_file(self, file_path: str, file_type: str = "image") -> str | None:
        """Загружает файл в MAX и возвращает token для вложения."""
        upload_type = "video" if file_type == "video" else "image"
        # Шаг 1: получаем upload URL через POST /uploads
        result = self._request("POST", "/uploads", params={"type": upload_type})
        if not result or "url" not in result:
            logger.error("Не удалось получить URL для загрузки файла")
            return None
        upload_url = result["url"]
        upload_token = result.get("token")

        # Шаг 2: если внешняя ссылка — скачиваем локально
        tmp_path = None
        if file_path.startswith("http"):
            try:
                import tempfile
                ext = ".mp4" if upload_type == "video" else ".jpg"
                tmp_path = tempfile.mktemp(suffix=ext)
                r = httpx.get(file_path, headers={"User-Agent": "Mozilla/5.0"},
                              timeout=60, follow_redirects=True)
                if r.status_code != 200 or len(r.content) < 1000:
                    logger.warning(f"Не удалось скачать медиа: HTTP {r.status_code}")
                    return None
                with open(tmp_path, "wb") as f:
                    f.write(r.content)
                file_path = tmp_path
            except Exception as e:
                logger.error(f"Ошибка скачивания медиа: {e}")
                return None

        try:
            with open(file_path, "rb") as f:
                file_content = f.read()

            # Для видео — multipart upload
            if upload_type == "video":
                files = {"data": (os.path.basename(file_path), file_content, "video/mp4")}
                resp = httpx.post(upload_url, files=files, timeout=120)
            else:
                # Для фото
                files = {"photo": (os.path.basename(file_path), file_content, "image/jpeg")}
                resp = httpx.post(upload_url, files=files, timeout=60)

            logger.debug(f"Upload response: {resp.status_code} {resp.text[:200]}")

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # Фото возвращает {"photos": {"key": {"token": "..."}}}
                    if "photos" in data:
                        for v in data["photos"].values():
                            if "token" in v:
                                return v["token"]
                    return data.get("token") or upload_token
                except Exception:
                    return upload_token
            else:
                logger.error(f"Загрузка файла провалилась: {resp.status_code} {resp.text[:200]}")
                return upload_token
        except Exception as e:
            logger.error(f"Ошибка загрузки файла: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def post(self, channel_id: int, text: str, media_url: str = None,
             media_type: str = None, media_files: list = None) -> bool:
        """
        Публикует пост в канал.
        media_url может быть локальным путём или http-ссылкой.
        """
        # chat_id передаётся как query-параметр
        params = {"chat_id": channel_id}
        payload: dict = {}
        attachments = []

        # Собираем все файлы для загрузки
        all_files = []
        if media_files:
            all_files = [f for f in media_files if f and os.path.exists(f)]
        if not all_files and media_url:
            all_files = [media_url] if os.path.exists(media_url) else [media_url]

        upload_failed = False
        for fpath in all_files:
            file_token = None
            # Пробуем до 3 раз
            for attempt in range(3):
                file_token = self.upload_file(fpath, file_type=media_type or "image")
                if file_token:
                    break
                if attempt < 2:
                    logger.info(f"Повтор загрузки медиа (попытка {attempt+2}/3)...")
                    import time as _time
                    _time.sleep(3)
            if file_token:
                att_type = "video" if media_type == "video" else "image"
                attachments.append({"type": att_type, "payload": {"token": file_token}})
            else:
                logger.warning(f"Медиа не загрузилось после 3 попыток: {fpath[:50]}")
                upload_failed = True

        # Если медиа было обязательным но не загрузилось — не публикуем
        if all_files and upload_failed and not attachments:
            logger.error("Все медиафайлы не загрузились — пост отменён")
            return False

        if text:
            payload["text"] = text[:4000]  # MAX лимит

        if attachments:
            payload["attachments"] = attachments

        # Для видео MAX нужно время на обработку — повторяем до 3 раз
        import time
        retries = 3 if media_type == "video" else 1
        for attempt in range(retries):
            result = self._request("POST", "/messages", params=params, json=payload)
            if result:
                logger.info(f"✅ Опубликовано в канал {channel_id}")
                return True
            if attempt < retries - 1:
                logger.info(f"Видео ещё обрабатывается, ждём 5 сек...")
                time.sleep(5)
        return False

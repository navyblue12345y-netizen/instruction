"""
Публикация в каналы MAX через Bot API.
"""
import os
import re
import time
import threading
import collections
import sqlite3
import datetime
import httpx
import logging

from utils import load_config

logger = logging.getLogger(__name__)

BASE_URL = "https://platform-api.max.ru"

# Путь к БД для персистенции AUTO-EDIT очереди.
# Та же БД что у engine — лежит рядом с processor/.
_AE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "content_bot.db",
)


def _ae_conn() -> sqlite3.Connection:
    """Connection к БД для AUTO-EDIT очереди. WAL уже включён глобально через db.py.
    Здесь не делаем PRAGMA — только что используем тот же файл."""
    c = sqlite3.connect(_AE_DB_PATH, timeout=10.0)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _ae_ensure_schema() -> None:
    """Гарантия что таблица существует. Идемпотентно."""
    try:
        c = _ae_conn()
        c.execute("""
        CREATE TABLE IF NOT EXISTS auto_edit_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            mid TEXT NOT NULL,
            sent_text TEXT NOT NULL,
            sent_format TEXT,
            expected_head_text TEXT NOT NULL,
            expected_head_len INTEGER NOT NULL,
            deadline_ts REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            note TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_aeq_status_dl ON auto_edit_queue(status, deadline_ts)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aeq_mid_uniq ON auto_edit_queue(mid)")
        c.commit()
        c.close()
    except Exception as e:
        logger.warning(f"AUTO-EDIT schema init failed: {e}")



class MaxPublisher:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"Authorization": token, "Content-Type": "application/json"}
        # P0-A: side-channel для умного retry на attachment.not.ready.
        # _request пишет сюда статус и тело последней ошибки, post_result
        # читает чтобы решить, делать ли длинный backoff (видео ещё в обработке).
        self._last_status_code: int | None = None
        self._last_error_body: str = ""

        # AUTO-EDIT очередь + долгоживущий worker thread.
        # Запускается лениво при первой публикации с auto_edit_check=True.
        # Worker daemon — умирает только когда основной процесс умирает,
        # независим от asyncio event loop / thread pool worker'ов engine.
        self._ae_queue: "collections.deque[tuple]" = collections.deque()
        self._ae_lock = threading.Lock()
        self._ae_worker_started = False
        # processed mids — чтобы не дёргать edit повторно если post_result
        # был вызван с тем же mid (например после retry); ограничено LRU 500.
        self._ae_processed: "collections.deque[str]" = collections.deque(maxlen=500)

    @staticmethod
    def _auto_bold_heading_markdown(text: str) -> str:
        """Делает первую строку жирной markdown-ом, если есть заголовок + тело."""
        s = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not s:
            return s
        if s.startswith("**") or s.startswith("<b>") or s.startswith("<strong>"):
            return s

        if "\n\n" in s:
            head, body = s.split("\n\n", 1)
            head = head.strip()
            body = body.strip()
            if head and body:
                # защитимся от случайных ** в заголовке
                safe_head = re.sub(r"\*{2,}", "", head).strip()
                return f"**{safe_head}**\n\n{body}".strip()

        return s

    def _request(self, method: str, path: str, params: dict = None, retries: int = 3, retry_network: bool = True, **kwargs) -> dict | None:
        import time as _time
        url = f"{BASE_URL}{path}"
        for attempt in range(retries):
            try:
                resp = httpx.request(method, url, headers=self.headers, params=params, timeout=30, **kwargs)
                # P0-A: запоминаем статус и тело — пригодится post_result для умного retry
                self._last_status_code = resp.status_code
                self._last_error_body = ""
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    self._last_error_body = resp.text[:500]
                    retry_after = int(resp.headers.get("Retry-After", 10))
                    logger.warning(f"MAX API rate limit (429) — ждём {retry_after}с (попытка {attempt+1}/{retries})")
                    _time.sleep(retry_after)
                    continue
                else:
                    self._last_error_body = resp.text[:500]
                    logger.error(f"MAX API {method} {path} → {resp.status_code}: {resp.text[:200]}")
                    return None
            except Exception as e:
                self._last_status_code = None
                self._last_error_body = f"{type(e).__name__}: {e}"
                logger.error(f"MAX API ошибка {path}: {e}")
                # 2026-08-07 (дубль Ижевска): для unsafe-запросов сетевое
                # исключение = НЕОПРЕДЕЛЁННЫЙ исход (POST мог дойти, ответ
                # утонул). Слепой повтор здесь создаёт второй пост — решение
                # о повторе принимает вызывающий (post_result смотрит ленту).
                if not retry_network:
                    return None
                if attempt < retries - 1:
                    _time.sleep(2)
        return None

    @staticmethod
    def _is_news_channel_by_id(channel_id: int | str) -> bool:
        """Авто-жирный заголовок включаем только для новостных сеток."""
        try:
            cfg = load_config()
            ch_map = (cfg.get("max", {}) or {}).get("channels", {}) or {}
            cid = str(channel_id)
            ch_key = next((k for k, v in ch_map.items() if str(v) == cid), None)
            if not ch_key:
                return False
            grids = cfg.get("grids", {}) or {}
            return ch_key in set(grids.get("Города России", []) or []) or ch_key in set(grids.get("Города лайв", []) or []) or ch_key in set(grids.get("Города в MAX", []) or [])
        except Exception:
            return False

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
                _dl_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0"}
                # 2026-05-14: bezformata.com и подобные требуют Referer для CDN media (anti-hotlinking)
                if "bezformata.com" in file_path or ".gif" in file_path.lower():
                    import re as _re_dl
                    _m = _re_dl.match(r"(https?://[^/]+)/", file_path)
                    if _m:
                        _dl_headers["Referer"] = _m.group(1) + "/"
                # 2026-05-19 FIX: timeout 60→10 — slow gov/CDN servers (mosreg.ru, kp.ru)
                # вешали слот на 60s × retry → 3-9 мин per channel. Лучше быстрый fail
                # → engine fallback на text-only.
                try:
                    r = httpx.get(file_path, headers=_dl_headers,
                                  timeout=10, follow_redirects=True)
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as _dl_e:
                    logger.warning(f"media fast-fail (timeout/conn): {file_path[:80]} {type(_dl_e).__name__}")
                    return None
                if r.status_code != 200 or len(r.content) < 1000:
                    # 4xx/5xx — не retry в caller (transient ≠ permanent)
                    logger.warning(f"Не удалось скачать медиа: HTTP {r.status_code} {file_path[:80]}")
                    return None
                # 2026-05-15: detect actual image format from magic bytes.
                # bezformata.com отдаёт GIF под content-type image/jpeg → MAX отвергает.
                # GIF → MAX считает video → VIDEO_VALIDATION_FAILED. Конвертируем в JPEG.
                content = r.content
                actual_ext = ext  # default
                if upload_type == "image":
                    if content[:6] in (b"GIF87a", b"GIF89a"):
                        actual_ext = ".gif"
                    elif content[:8].startswith(b"\x89PNG"):
                        actual_ext = ".png"
                    elif content[:3] == b"\xff\xd8\xff":
                        actual_ext = ".jpg"
                    elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
                        actual_ext = ".webp"
                # Convert GIF → JPEG (MAX отвергает GIF: VIDEO_VALIDATION_FAILED)
                if actual_ext == ".gif" and upload_type == "image":
                    try:
                        from PIL import Image
                        import io as _io
                        img = Image.open(_io.BytesIO(content))
                        img.seek(0)  # first frame
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        out_buf = _io.BytesIO()
                        img.save(out_buf, "JPEG", quality=90)
                        content = out_buf.getvalue()
                        actual_ext = ".jpg"
                        logger.info(f"[media] converted GIF → JPEG (first frame, {len(content)}B)")
                    except Exception as _conv_err:
                        logger.warning(f"[media] GIF → JPEG конвертация не удалась: {_conv_err}, отправляю как есть")
                if actual_ext != ext:
                    tmp_path = tmp_path.rsplit(".", 1)[0] + actual_ext
                    logger.info(f"[media] real format: {actual_ext} (URL ext was {ext})")
                with open(tmp_path, "wb") as f:
                    f.write(content)
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
                # Для фото — выбираем mime по реальному расширению
                _ext_lower = os.path.splitext(file_path)[1].lower()
                mime_map = {".gif": "image/gif", ".png": "image/png", ".webp": "image/webp"}
                mime = mime_map.get(_ext_lower, "image/jpeg")
                files = {"photo": (os.path.basename(file_path), file_content, mime)}
                resp = httpx.post(upload_url, files=files, timeout=60)

            logger.debug(f"Upload response: {resp.status_code} {resp.text[:200]}")

            if resp.status_code == 200:
                # Task 5 fix (2026-05-26): video uploads идут через vu.okcdn.ru
                # (Odnoklassniki CDN, Apache) и возвращают XML <retval>N</retval>,
                # НЕ JSON. N=1 — success. Для video case session_token из /uploads
                # И ЕСТЬ valid attachment token (verified live test publish).
                body_stripped = (resp.text or "").strip()
                if upload_type == "video" and body_stripped.startswith("<retval>"):
                    if body_stripped == "<retval>1</retval>":
                        if upload_token:
                            return upload_token
                        logger.warning(
                            "video upload OK but no upload_token from /uploads — fallback None"
                        )
                        return None
                    # Other retval values = upload failure
                    logger.warning(
                        f"video upload non-success retval: {body_stripped[:200]}"
                    )
                    return None
                # IMAGE PATH (or fallback): JSON parsing as before
                try:
                    data = resp.json()
                    # Bug 3 fix (2026-05-26): MAX иногда возвращает HTTP 200 с
                    # ошибкой в body — {"error_code":"503","error_data":"..."}.
                    # Это failure, не success.
                    if isinstance(data, dict) and data.get("error_code"):
                        logger.warning(
                            f"upload {upload_type} returned 200+error: {str(data)[:200]}"
                        )
                        return None
                    # Photo upload response shape: {"photos": {"key": {"token": "..."}}}
                    if isinstance(data, dict) and "photos" in data:
                        photos = data["photos"]
                        if isinstance(photos, dict):
                            for v in photos.values():
                                if isinstance(v, dict) and v.get("token"):
                                    return v["token"]
                    # Bug 1b fix (2026-05-26): video upload response shape — {"videos": {...}}.
                    # Раньше парсили только photos → fallback на video upload_token → invalid.
                    if isinstance(data, dict) and "videos" in data:
                        videos = data["videos"]
                        if isinstance(videos, dict):
                            for v in videos.values():
                                if isinstance(v, dict) and v.get("token"):
                                    return v["token"]
                    # Direct token field (нестандарт но legacy MAX иногда возвращал)
                    if isinstance(data, dict) and data.get("token"):
                        return data["token"]
                    # Bug 1a fix (2026-05-26): НЕ fallback на upload_token —
                    # для video uploads upload_token = session token, не attachment token.
                    # Лучше controlled None (text-only fallback в post_result) чем
                    # invalid token → "Invalid photo token" в MAX.
                    logger.warning(
                        f"upload {upload_type} response не содержит token: {str(data)[:200]}"
                    )
                    return None
                except Exception as e:
                    logger.warning(f"upload {upload_type} parse failed: {e}")
                    return None
            else:
                logger.error(
                    f"Загрузка файла провалилась: {resp.status_code} {resp.text[:200]}"
                )
                # Bug 1a fix: same reasoning — НЕ возвращаем upload_token.
                return None
        except Exception as e:
            logger.error(f"Ошибка загрузки файла: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _schedule_auto_edit_check(
        self, channel_id: int, mid: str | None,
        sent_text: str, sent_format: str | None,
    ) -> None:
        """Кладёт пост в очередь auto-edit. Реальную проверку/правку делает
        отдельный долгоживущий worker thread (см. _auto_edit_worker_loop).

        FIX MAX-bug (2026-04-30): MAX иногда захватывает первое предложение body
        в bold-entity при первой публикации. Edit того же текста MAX уважает —
        bold возвращается ровно по head.

        История попыток:
        - v1 (threading.Thread daemon на каждый пост) — не работал, daemon-child
          умирал вместе с asyncio.to_thread worker'ом.
        - v2/v3 (sync inline time.sleep(2.5)) — блокировал event loop на 2.5с
          per post; при медленном MAX list_messages — больше; работал ненадёжно.
        - v4 (queue + один долгоживущий worker, ТЕКУЩИЙ) — publisher не блокируется,
          worker thread живёт всё время процесса (daemon=True), независим от
          asyncio. Этот метод теперь только enqueue.
        """
        if not mid or not sent_text or "**" not in sent_text:
            logger.info(
                f"AUTO-EDIT SKIP enqueue mid={mid!r}: "
                f"mid={'ok' if mid else 'MISSING'}, "
                f"text={'ok' if sent_text else 'EMPTY'}, "
                f"has_stars={'**' in (sent_text or '')}"
            )
            return
        m = re.match(r'^\s*\*\*(.+?)\*\*', sent_text, re.DOTALL)
        if not m:
            logger.info(
                f"AUTO-EDIT SKIP enqueue mid={mid!r}: no leading **bold** in {sent_text[:80]!r}"
            )
            return
        expected_head_text = m.group(1).strip()
        expected_head_len = len(expected_head_text)
        deadline_ts = time.time() + 2.5  # ждать 2.5с после публикации

        # Дедуп: если этот mid уже обрабатывали — игнорируем
        if mid in self._ae_processed:
            return

        with self._ae_lock:
            self._ae_queue.append((
                deadline_ts, channel_id, mid, sent_text, sent_format,
                expected_head_text, expected_head_len,
            ))
            qlen = len(self._ae_queue)

        logger.info(
            f"AUTO-EDIT ENQUEUE mid={mid!r} expected_head_len={expected_head_len} "
            f"qlen={qlen}"
        )

        # Персистентная копия записи в БД — переживёт SIGKILL/OOM/рестарт.
        # На старте worker'а перечитаем 'pending' строки обратно в deque.
        try:
            c = _ae_conn()
            c.execute(
                """INSERT OR IGNORE INTO auto_edit_queue
                (channel_id, mid, sent_text, sent_format, expected_head_text,
                 expected_head_len, deadline_ts, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    int(channel_id), mid, sent_text, sent_format,
                    expected_head_text, expected_head_len, deadline_ts,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            c.commit()
            c.close()
        except Exception as e:
            logger.warning(f"AUTO-EDIT enqueue persist failed mid={mid}: {e}")

        # Lazy старт worker'а при первой публикации
        if not self._ae_worker_started:
            with self._ae_lock:
                if not self._ae_worker_started:
                    self._ae_worker_started = True
                    t = threading.Thread(
                        target=self._auto_edit_worker_loop,
                        daemon=True,
                        name="MaxAutoEditWorker",
                    )
                    t.start()
                    logger.info("AUTO-EDIT worker thread STARTED")

    def _auto_edit_worker_loop(self) -> None:
        """Долгоживущий worker. Каждые 0.5с вытаскивает из очереди элементы
        с истекшим deadline (post опубликован > 2.5с назад) → list_messages →
        сравнивает bold-entity с expected → если расширен, делает edit.

        Никогда не возвращает (бесконечный цикл). При exception — логирует и
        продолжает (worker не должен умирать).
        """
        logger.info("AUTO-EDIT worker loop запущен")
        # === Recovery после рестарта/краха: читаем все pending из БД и докидываем
        # в deque. Также любые 'processing' (если worker упал во время обработки)
        # помечаем 'pending' заново — пусть попробует ещё раз. Идемпотентно.
        _ae_ensure_schema()
        try:
            c = _ae_conn()
            # Вернуть processing в pending — это означает что прошлый worker не
            # успел зафиксировать done.
            c.execute("UPDATE auto_edit_queue SET status='pending' WHERE status='processing'")
            rows = c.execute(
                """SELECT channel_id, mid, sent_text, sent_format,
                          expected_head_text, expected_head_len, deadline_ts
                   FROM auto_edit_queue WHERE status='pending'"""
            ).fetchall()
            c.commit()
            c.close()
            recovered = 0
            with self._ae_lock:
                for ch_id, mid, st, sf, ehead, elen, dl in rows:
                    if mid in self._ae_processed:
                        continue
                    self._ae_queue.append((dl, ch_id, mid, st, sf, ehead, elen))
                    recovered += 1
            if recovered:
                logger.info(f"AUTO-EDIT recovery: восстановлено {recovered} pending записей")
        except Exception as e:
            logger.warning(f"AUTO-EDIT recovery failed: {e}")
        while True:
            try:
                time.sleep(0.5)
                now_ts = time.time()
                ready: list[tuple] = []
                with self._ae_lock:
                    # Берём из начала очереди всё, у чего deadline в прошлом
                    while self._ae_queue and self._ae_queue[0][0] <= now_ts:
                        ready.append(self._ae_queue.popleft())
                for item in ready:
                    self._auto_edit_process_one(*item)
            except Exception as e:
                logger.warning(
                    f"AUTO-EDIT worker iteration error: {type(e).__name__}: {e}"
                )

    def _ae_mark_status(self, mid: str, status: str, note: str = "") -> None:
        """Обновляет статус строки в auto_edit_queue. Best-effort, не падает."""
        try:
            c = _ae_conn()
            c.execute(
                """UPDATE auto_edit_queue
                SET status=?, processed_at=?, note=?
                WHERE mid=?""",
                (status, datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 note[:500], mid),
            )
            c.commit()
            c.close()
        except Exception as e:
            logger.warning(f"AUTO-EDIT mark_status mid={mid} {status}: {e}")

    def _auto_edit_process_one(
        self,
        deadline_ts: float, channel_id: int, mid: str,
        sent_text: str, sent_format: str | None,
        expected_head_text: str, expected_head_len: int,
    ) -> None:
        """Обрабатывает один элемент очереди: проверяет markup и при необходимости
        делает edit. Все return-ветки логируются для видимости."""
        # Пометить как обработанный СРАЗУ (даже если упадёт), чтобы повтор не делать
        self._ae_processed.append(mid)
        # Mark as 'processing' сразу (recovery после crash перевернёт обратно в pending)
        self._ae_mark_status(mid, "processing")
        try:
            msgs = self.list_messages(channel_id, limit=5) or []
            found = False
            for msg in msgs:
                body = msg.get('body') or {}
                cur_mid = body.get('mid') or msg.get('mid')
                if cur_mid != mid:
                    continue
                found = True
                markup = body.get('markup') or []
                strong = next((e for e in markup if e.get('type') == 'strong'), None)
                if not strong:
                    logger.info(f"AUTO-EDIT NO_STRONG mid={mid}: MAX не сохранил bold — skip")
                    self._ae_mark_status(mid, "skipped", "no strong markup")
                    return
                actual_len = strong.get('length', 0)
                # +5 chars запас на \n и emoji variation selectors (U+FE0F)
                if actual_len <= expected_head_len + 5:
                    logger.info(
                        f"AUTO-EDIT OK mid={mid}: "
                        f"actual_len={actual_len} expected≈{expected_head_len} (no fix)"
                    )
                    self._ae_mark_status(mid, "done",
                        f"ok actual={actual_len} expected={expected_head_len}")
                    return
                logger.warning(
                    f"AUTO-EDIT FIX mid={mid} channel={channel_id}: "
                    f"actual={actual_len} expected≈{expected_head_len} → editing"
                )
                ok = self.edit_message(channel_id, mid, sent_text,
                                       text_format=sent_format)
                logger.info(
                    f"AUTO-EDIT FIX result mid={mid}: {ok}"
                )
                self._ae_mark_status(mid, "done" if ok else "failed",
                    f"fix actual={actual_len} expected={expected_head_len} ok={ok}")
                return
            if not found:
                logger.warning(
                    f"AUTO-EDIT NOT_FOUND mid={mid} channel={channel_id}: "
                    f"не нашли в последних {len(msgs)} сообщениях канала"
                )
                self._ae_mark_status(mid, "skipped",
                    f"not found in last {len(msgs)} messages")
        except Exception as e:
            logger.warning(
                f"AUTO-EDIT process_one failed mid={mid}: {type(e).__name__}: {e}"
            )
            self._ae_mark_status(mid, "failed", f"{type(e).__name__}: {e}")

    @staticmethod
    def _norm_for_match(text: str) -> str:
        """Текст к виду «как хранит MAX»: без ** и [label](url), сжатые пробелы."""
        import re as _re
        t = text or ""
        t = t.replace("**", "")
        t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = _re.sub(r"\s+", " ", t).strip()
        return t[:100]

    def _find_own_recent_post(self, channel_id, text: str,
                              window_sec: int = 240) -> str | None:
        """Ищет в ленте канала СВЕЖИЙ пост с этим текстом. Возвращает mid.

        Защита от дубля (2026-08-07, Ижевск): MAX принял пост, ответ утонул
        в таймауте — перед повтором проверяем, не появился ли пост в ленте.
        """
        import time as _time
        want = self._norm_for_match(text)
        if not want:
            return None
        try:
            msgs = self.list_messages(channel_id, limit=6) or []
        except Exception:
            return None
        now_ms = _time.time() * 1000
        for m in msgs:
            b = (m or {}).get("body") or {}
            ts = m.get("timestamp") or 0
            if now_ms - ts > window_sec * 1000:
                continue
            if self._norm_for_match(b.get("text") or "") == want:
                return b.get("mid")
        return None

    def post_result(self, channel_id: int, text: str, media_url: str = None,
                    media_type: str = None, media_files: list = None,
                    markup: dict | None = None, text_format: str | None = None,
                    auto_edit_check: bool = True,
                    attachments_tokens: list[str] | None = None) -> dict:
        """
        Публикует пост в канал.
        media_url может быть локальным путём или http-ссылкой.

        auto_edit_check: если True (по умолчанию) — после публикации запускает
            проверку markup и при MAX-расширении bold делает edit. Передавай
            False для постов где это не нужно (morning_weather, evening_digest):
            короткий head/специальный формат — MAX не страдает + не хочется
            автоматического edit для контента где формат критичен.

        attachments_tokens: если передан (Prepare-then-fire, 2026-05-21) —
            используем готовые MAX-токены напрямую и пропускаем upload_file().
            Когда None — работаем по существующему upload-based пути (полная
            обратная совместимость).
        """
        # STAGING redirect: если в конфиге задан staging.redirect_channel_id —
        # все публикации уходят в тест-канал. В прод-конфиге секции staging нет — no-op.
        try:
            _stg = (load_config().get("staging", {}) or {})
            _rd = _stg.get("redirect_channel_id")
            if _rd:
                if int(_rd) != int(channel_id):
                    logger.info(f"[STAGING] redirect publish {channel_id} -> {_rd}")
                channel_id = int(_rd)
        except Exception as _stg_e:
            logger.warning(f"[STAGING] redirect check failed: {_stg_e}")

        # chat_id передаётся как query-параметр
        params = {"chat_id": channel_id}
        payload: dict = {}
        attachments = []

        if attachments_tokens:
            # Prepare-then-fire (2026-05-21): используем pre-uploaded MAX-токены
            # напрямую. Это позволяет engine выполнить тяжёлый upload заранее
            # (вне горячего слота), а в момент публикации только дёрнуть POST.
            # Тип по умолчанию image; для видео caller должен был аплоадить
            # как video (токен опаков с точки зрения API).
            for tok in attachments_tokens:
                if not tok:
                    continue
                att_type = "video" if media_type == "video" else "image"
                attachments.append({"type": att_type, "payload": {"token": tok}})
        else:
            # EXISTING UPLOAD-BASED PATH — полностью сохранён без изменений.
            # Собираем все файлы для загрузки.
            # ВАЖНО: после lazy-media рефактора media_url/media_files могут содержать
            # HTTP(S) URL'ы (cdn4.telesco.pe/...) — upload_file умеет их качать сам.
            # Поэтому валидным считаем как локальный путь (os.path.exists), так и URL.
            # Без этого фикса фильтр os.path.exists() выбрасывал ВСЕ URL и публиковалось
            # только первое фото из media_url (через fallback) — теряли альбомы.
            def _is_valid_media(f):
                if not f:
                    return False
                s = str(f)
                if s.startswith("http://") or s.startswith("https://"):
                    return True
                return os.path.exists(s)

            all_files = []
            if media_files:
                all_files = [f for f in media_files if _is_valid_media(f)]
            if not all_files and media_url:
                # fallback к media_url если media_files пустой/невалидный
                all_files = [media_url]

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
                return {"ok": False, "mid": None, "error": "media_upload_failed"}

        if text:
            effective_format = text_format
            effective_text = text
            if not effective_format and self._is_news_channel_by_id(channel_id):
                effective_text = self._auto_bold_heading_markdown(text)
                # Для автожирного используем markdown
                if effective_text != text:
                    effective_format = "markdown"
            payload["text"] = (effective_text or "")[:4000]  # MAX лимит

        if text_format in {"markdown", "html"}:
            payload["format"] = text_format
        elif text and payload.get("text", "").startswith("**"):
            payload["format"] = "markdown"

        if attachments:
            payload["attachments"] = attachments

        # Попытка передать кнопки/markup (если поддерживается API MAX)
        if markup:
            payload["markup"] = markup

        # P0-A: Умный retry с разделением причин.
        # Для видео MAX иногда отвечает 400 attachment.not.ready — оно ещё в обработке.
        # Раньше: фиксированный 5с × 3 попытки = 15с (мало для долгих видео).
        # Теперь: при attachment.not.ready/video.not.processed — длинный backoff
        # 5/10/20/30с (всего до 65с), при других ошибках — короткий 3с.
        import time
        # Первая попытка — для всех типов медиа. attachment-related retry —
        # только если медиа есть (видео или картинка может тоже зависнуть).
        max_attempts = 5 if attachments else 2  # без медиа — 1 retry достаточно
        attachment_backoffs = [5, 10, 20, 30]   # суммарно до 65с
        attempt = 0
        while attempt < max_attempts:
            result = self._request("POST", "/messages", params=params, json=payload, retry_network=False)
            if result:
                body = (result.get("message") or {}).get("body") or {}
                mid = body.get("mid") or result.get("mid")
                # Публичный URL поста: формат подтверждён живым примером
                # https://max.ru/c/<channel_id>/<mid_token>.
                # Если mid отсутствует — URL не строим (None).
                post_url = f"https://max.ru/c/{channel_id}/{mid}" if mid else None
                # Одноразовая диагностика формата токена MAX — пригодится для
                # вечернего дайджеста (нужны кликабельные ссылки на посты дня).
                # Полезно увидеть body_keys на случай, если share-токен лежит
                # отдельным полем (а mid — внутреннее число).
                logger.info(
                    f"✅ Опубликовано в канал {channel_id} | mid={mid!r} "
                    f"url={post_url} | body_keys={list(body.keys())}"
                )
                # FIX (2026-04-30): post-publish auto-edit. MAX иногда расширяет
                # bold-entity захватывая часть body (см. mass-edit 173 поста днём).
                # Edit того же текста всегда исправляет. Делаем fire-and-forget
                # в отдельном потоке чтоб не блокировать engine.
                # Пропускаем для morning_weather/evening_digest (см. kwarg).
                if auto_edit_check:
                    self._schedule_auto_edit_check(
                        channel_id=channel_id,
                        mid=mid,
                        sent_text=payload.get("text") or "",
                        sent_format=payload.get("format"),
                    )
                # Запись своего mid в bot_posted_mids (Ad detection Task 4):
                # для отличия «наш пост» vs «реклама от внешней сети» в
                # ad_detection_loop. Best-effort — DB issue не должна ломать публикацию.
                if mid:
                    try:
                        import db as _db
                        _db.insert_bot_posted_mid(str(channel_id), str(mid))
                    except Exception as _bp_e:
                        logger.warning(f"bot_posted_mids insert failed: {_bp_e}")
                return {"ok": True, "mid": mid, "post_url": post_url, "raw": result}

            # 2026-08-07 (дубль Ижевска): сетевое исключение = исход
            # неопределён — MAX мог принять пост. Прежде чем повторять,
            # смотрим ленту: пост уже там -> возвращаем его mid без повтора.
            if self._last_status_code is None and payload.get("text"):
                _existing = self._find_own_recent_post(
                    channel_id, payload.get("text") or "")
                if _existing:
                    logger.warning(
                        f"⚠ дубль предотвращён: пост уже в ленте {channel_id} "
                        f"(mid={_existing}) — таймаут съел ответ, повтор отменён")
                    try:
                        import db as _db
                        _db.insert_bot_posted_mid(str(channel_id), str(_existing))
                    except Exception as _bp_e:
                        logger.warning(f"bot_posted_mids insert failed: {_bp_e}")
                    _post_url = f"https://max.ru/c/{channel_id}/{_existing}"
                    return {"ok": True, "mid": _existing,
                            "post_url": _post_url, "raw": None,
                            "dedup_recovered": True}

            # Не получилось — анализируем причину и выбираем стратегию
            err_body = (self._last_error_body or "").lower()
            err_code = self._last_status_code
            is_attachment_pending = (
                "attachment.not.ready" in err_body
                or "video.not.processed" in err_body
                or "attachment.processing" in err_body
            )

            attempt += 1
            if attempt >= max_attempts:
                break

            if is_attachment_pending and attachments:
                # Длинный backoff — видео/картинка ещё в обработке у MAX
                backoff_idx = min(attempt - 1, len(attachment_backoffs) - 1)
                wait_sec = attachment_backoffs[backoff_idx]
                logger.info(
                    f"⏳ MAX: attachment ещё обрабатывается → ждём {wait_sec}с "
                    f"(попытка {attempt+1}/{max_attempts})"
                )
                time.sleep(wait_sec)
            else:
                # Постоянная ошибка (auth, rate, 5xx, etc) — короткий retry
                # на случай transient. После 2 неудач для не-медиа сдаёмся.
                # 2026-08-07: КРОМЕ неопределённого исхода (сетевое исключение,
                # status None) — лента уже проверена и поста там нет, один
                # повтор безопасен и нужен (раньше text-only падал с первого
                # таймаута, а pipeline-retry потом создавал дубль).
                if not attachments and self._last_status_code is not None:
                    break
                logger.info(
                    f"MAX POST /messages failed ({err_code}), короткий retry через 3с "
                    f"(попытка {attempt+1}/{max_attempts})"
                )
                time.sleep(3)

        return {"ok": False, "mid": None, "post_url": None, "error": "post_failed"}

    def post(self, channel_id: int, text: str, media_url: str = None,
             media_type: str = None, media_files: list = None,
             markup: dict | None = None, text_format: str | None = None,
             auto_edit_check: bool = True) -> bool:
        res = self.post_result(
            channel_id,
            text,
            media_url,
            media_type,
            media_files,
            markup=markup,
            text_format=text_format,
            auto_edit_check=auto_edit_check,
        )
        return bool(res.get("ok"))

    def delete_message(self, channel_id: int, message_id: str) -> bool:
        if not message_id:
            return False
        res = self._request("DELETE", "/messages", params={"chat_id": channel_id, "message_id": message_id})
        return bool(res is not None)

    def edit_message(self, channel_id: int, message_id: str, text: str,
                     markup: dict | None = None, text_format: str | None = None,
                     allow_unsafe_put: bool = True) -> bool:
        """Редактирует уже опубликованное сообщение (best-effort).

        MAX API в разных инсталляциях может поддерживать разные методы/пути,
        поэтому пробуем несколько безопасных вариантов.
        """
        if not message_id:
            return False

        effective_text = text or ""
        effective_format = text_format
        if not effective_format:
            maybe = self._auto_bold_heading_markdown(effective_text)
            if maybe != effective_text:
                effective_text = maybe
                effective_format = "markdown"

        payload: dict = {"text": effective_text[:4000]}
        if effective_format in {"markdown", "html"}:
            payload["format"] = effective_format
        if markup:
            payload["markup"] = markup

        # Вариант 1: PATCH /messages?chat_id&message_id
        res = self._request(
            "PATCH",
            "/messages",
            params={"chat_id": channel_id, "message_id": message_id},
            json=payload,
            retries=1,
        )
        if res is not None:
            body = (res.get("message") or {}).get("body") or {}
            mid = body.get("mid") or res.get("mid")
            if mid and str(mid) != str(message_id):
                logger.error(f"edit_message PATCH mismatch mid: expected={message_id} got={mid}")
                return False
            return True

        # В ряде инсталляций MAX редактирование работает только через PUT /messages.
        # Оставляем safety-проверку mid: если сервер вернул другой mid,
        # считаем это созданием нового поста и удаляем его best-effort.
        if allow_unsafe_put:
            res = self._request(
                "PUT",
                "/messages",
                params={"chat_id": channel_id, "message_id": message_id},
                json=payload,
                retries=1,
            )
            if res is not None:
                body = (res.get("message") or {}).get("body") or {}
                mid = body.get("mid") or res.get("mid")
                if mid and str(mid) != str(message_id):
                    logger.error(f"edit_message PUT mismatch mid: expected={message_id} got={mid}; cleaning up created message")
                    # best-effort cleanup: если PUT создал новый пост — удаляем его
                    try:
                        self.delete_message(channel_id, str(mid))
                    except Exception:
                        pass
                    return False
                return True

        # Вариант 2: POST /messages/edit?chat_id&message_id
        res = self._request(
            "POST",
            "/messages/edit",
            params={"chat_id": channel_id, "message_id": message_id},
            json=payload,
            retries=1,
        )
        if res is None:
            return False

        body = (res.get("message") or {}).get("body") or {}
        mid = body.get("mid") or res.get("mid")
        if mid and str(mid) != str(message_id):
            logger.error(f"edit_message POST mismatch mid: expected={message_id} got={mid}")
            return False
        return True

    def get_message_views(self, channel_id: int, message_id: str) -> int | None:
        """Возвращает просмотры конкретного сообщения (stat.views) через GET /messages."""
        if not message_id:
            return None
        res = self._request("GET", "/messages", params={"chat_id": channel_id, "message_id": message_id})
        if not res:
            return None
        try:
            msgs = res.get("messages") or []
            # ВАЖНО: API может вернуть список, где целевой mid не первый.
            for m in msgs:
                body = (m or {}).get("body") or {}
                if str(body.get("mid") or "") == str(message_id):
                    st = (m or {}).get("stat") or body.get("stat") or {}
                    v = st.get("views")
                    return int(v) if v is not None else None

            # fallback на первый элемент (если API не вернул body.mid)
            if msgs:
                m0 = msgs[0] or {}
                st0 = m0.get("stat") or (m0.get("body") or {}).get("stat") or {}
                v0 = st0.get("views")
                return int(v0) if v0 is not None else None
        except Exception:
            return None
        return None

    def list_messages(self, channel_id: int, limit: int = 40) -> list[dict]:
        """Возвращает последние сообщения канала (best-effort)."""
        params = {"chat_id": channel_id}
        # Некоторые инсталляции MAX API поддерживают count/limit; если нет — просто проигнорируется.
        params["count"] = int(limit)
        res = self._request("GET", "/messages", params=params)
        if not res:
            return []
        msgs = res.get("messages") or []
        return msgs[: max(1, int(limit))]

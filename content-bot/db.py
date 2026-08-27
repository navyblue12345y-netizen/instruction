"""
Работа с SQLite БД: инициализация, CRUD постов, дедупликация.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "content_bot.db")
SEEN_POSTS_TTL_DAYS = 30  # Хранить seen_posts не дольше N дней

logger = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    # timeout=30: ждать до 30с при database is locked (вместо немедленного raise).
    # WAL: writers не блокируют readers, drastically снижает lock contention
    #      между tg_admin_bot, news_realtime_engine и scheduler.
    # busy_timeout: дополнительная страховка на уровне SQLite engine (в мс).
    # synchronous=NORMAL: безопасно для WAL, ~3x быстрее FULL без потери durability.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass  # на случай странных режимов БД
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type    TEXT NOT NULL,
            source_id      TEXT NOT NULL,
            original_text  TEXT,
            rewritten_text TEXT,
            media_url      TEXT,
            media_type     TEXT,
            media_files    TEXT,
            channel        TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            skip_reason    TEXT,
            created_at     TEXT NOT NULL,
            posted_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS seen_posts_v2 (
            url_hash TEXT NOT NULL,
            channel  TEXT NOT NULL DEFAULT '',
            seen_at  TEXT NOT NULL,
            PRIMARY KEY (url_hash, channel)
        );
        CREATE TABLE IF NOT EXISTS slot_locks (
            channel    TEXT NOT NULL,
            slot_key   TEXT NOT NULL,
            locked_at  TEXT NOT NULL,
            PRIMARY KEY (channel, slot_key)
        );
        CREATE TABLE IF NOT EXISTS published_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL,
            text_hash  TEXT,
            url_hash   TEXT,
            post_id    INTEGER,
            status     TEXT NOT NULL DEFAULT 'posted',
            seen_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_posts_channel_status ON posts(channel, status);
        CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
        CREATE INDEX IF NOT EXISTS idx_seen_posts_v2_seen_at ON seen_posts_v2(seen_at);
        CREATE INDEX IF NOT EXISTS idx_seen_posts_v2_channel ON seen_posts_v2(channel);
        CREATE INDEX IF NOT EXISTS idx_history_channel_seen_at ON published_history(channel, seen_at);
        CREATE INDEX IF NOT EXISTS idx_history_channel_text_hash ON published_history(channel, text_hash);
        CREATE INDEX IF NOT EXISTS idx_history_channel_url_hash ON published_history(channel, url_hash);

        CREATE TABLE IF NOT EXISTS ad_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_time TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS ads_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            ad_text TEXT,
            media_file_id TEXT,
            markup TEXT,
            target_datetime TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at TEXT NOT NULL,
            published_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ads_schedule_channel_time ON ads_schedule(channel_id, target_datetime);
        CREATE INDEX IF NOT EXISTS idx_ads_schedule_status_time ON ads_schedule(status, target_datetime);
    """)
    # Миграции posts
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()}
    if "media_files" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN media_files TEXT")
    if "skip_reason" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN skip_reason TEXT")
    if "text_hash" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN text_hash TEXT")
    if "url_hash" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN url_hash TEXT")

    # Миграция: копируем данные из seen_posts в seen_posts_v2 если нужно
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'seen_posts' in tables and 'seen_posts_v2' in tables:
        count_old = conn.execute("SELECT COUNT(*) FROM seen_posts").fetchone()[0]
        count_new = conn.execute("SELECT COUNT(*) FROM seen_posts_v2").fetchone()[0]
        if count_old > 0 and count_new == 0:
            conn.execute("""
                INSERT OR IGNORE INTO seen_posts_v2 (url_hash, channel, seen_at)
                SELECT url_hash, '', seen_at FROM seen_posts
            """)
            logger.info(f"Миграция seen_posts → seen_posts_v2: {count_old} записей")

    # Backfill хешей в posts для старых записей
    missing = conn.execute(
        "SELECT id, original_text, source_id FROM posts WHERE text_hash IS NULL OR url_hash IS NULL"
    ).fetchall()
    for r in missing:
        conn.execute(
            "UPDATE posts SET text_hash=?, url_hash=COALESCE(url_hash, '') WHERE id=?",
            (text_hash(r[1] or ''), r[0]),
        )

    # Миграции ads_schedule (автоудаление / метрики)
    ads_cols = {r[1] for r in conn.execute("PRAGMA table_info(ads_schedule)").fetchall()}
    if "published_mid" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN published_mid TEXT")
    if "auto_delete_after_min" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN auto_delete_after_min INTEGER DEFAULT 0")
    if "delete_at" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN delete_at TEXT")
    if "delete_status" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN delete_status TEXT")
    if "deleted_at" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN deleted_at TEXT")
    if "delete_attempts" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN delete_attempts INTEGER DEFAULT 0")
    if "delete_error" not in ads_cols:
        conn.execute("ALTER TABLE ads_schedule ADD COLUMN delete_error TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_delete_due ON ads_schedule(delete_status, delete_at)")

    # Метрики охватов рекламы (срезы каждые 24 часа)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            mid TEXT NOT NULL,
            hours_since_publish INTEGER NOT NULL,
            views INTEGER,
            collected_at TEXT NOT NULL,
            payload TEXT,
            UNIQUE(ad_id, hours_since_publish)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_stats_ad ON ad_stats(ad_id, collected_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_stats_hours ON ad_stats(hours_since_publish, collected_at)")

    # Bug #6 (2026-05-20): ad_publish_log — per-grid ad-clearance cooldown source.
    # Наполняется руками когда выходит реклама (см. news_realtime_engine._get_recent_ad_for_tz_group).
    # Колонка grid (Города лайв / Города в MAX / NULL=legacy) изолирует cooldown между сетками.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ad_publish_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tz_group TEXT NOT NULL,
            ad_published_at TEXT NOT NULL,
            note TEXT,
            grid TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ad_pl_tz_time ON ad_publish_log(tz_group, ad_published_at)")
    # Bug #6: idempotent migration — добавить grid если таблица существовала без неё
    try:
        _ad_pl_cols = {r[1] for r in conn.execute("PRAGMA table_info(ad_publish_log)").fetchall()}
        if "grid" not in _ad_pl_cols:
            conn.execute("ALTER TABLE ad_publish_log ADD COLUMN grid TEXT")
    except Exception:
        pass

    # Внешние рекламные посты (когда публикуются не через наш ads_schedule)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_ad_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            mid TEXT NOT NULL,
            published_at TEXT NOT NULL,
            slot_local TEXT,
            detected_at TEXT NOT NULL,
            UNIQUE(channel_id, mid)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_ads_channel_pub ON external_ad_posts(channel_id, published_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_external_ad_mid ON external_ad_posts(mid)")

    # bot_posted_mids — наши mid'ы (для отличия от рекламы в ad_detection_loop).
    # INSERT происходит после каждой успешной publisher.post_result.
    # TTL 48h через cleanup_old_bot_mids cron (scheduler.py — Task 8).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_posted_mids (
            channel_id TEXT NOT NULL,
            mid TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, mid)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_mids_posted ON bot_posted_mids(posted_at)")

    # external_posts_seen — MAX ad-cover (24.06): watcher фиксирует ЧУЖИЕ посты
    # (рекламу) в каналах через push (message_created), ad_cover_tick публикует
    # cover-пост через delay_minutes. status: pending|published|skipped_our_own|
    # skipped_album_dup|skipped|error.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_posts_seen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_key TEXT NOT NULL,
            mid TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            cover_at TEXT,
            cover_prepared_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(channel_key, mid)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eps_status_seen ON external_posts_seen(status, seen_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eps_channel_seen ON external_posts_seen(channel_key, seen_at)")

    # Дефолтные рекламные слоты (редактируемые)
    slot_count = conn.execute("SELECT COUNT(*) FROM ad_slots").fetchone()[0]
    if slot_count == 0:
        for slot in ("09:00", "12:00", "15:00", "19:00"):
            conn.execute("INSERT OR IGNORE INTO ad_slots (slot_time, enabled) VALUES (?, 1)", (slot,))

    # Первичная миграция posted -> published_history
    hist_count = conn.execute("SELECT COUNT(*) FROM published_history").fetchone()[0]
    if hist_count == 0:
        conn.execute(
            """
            INSERT INTO published_history (channel, text_hash, url_hash, post_id, status, seen_at)
            SELECT channel, COALESCE(text_hash,''), COALESCE(url_hash,''), id, 'posted', COALESCE(posted_at, created_at)
            FROM posts
            WHERE status='posted'
            """
        )

    conn.commit()
    conn.close()

# ── Дедупликация ────────────────────────────────────────────────────────────

def _hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _normalize_text_for_hash(text: str) -> str:
    """Нормализация текста для стабильного дедуп-хеша."""
    if not text:
        return ""
    t = text.lower().replace("\u00a0", " ")
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def text_hash(text: str) -> str:
    norm = _normalize_text_for_hash(text)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def is_seen(url: str, channel: str = '') -> bool:
    """Проверяет видели ли этот URL для данного канала. channel='' — глобальная проверка."""
    conn = get_conn()
    if channel:
        row = conn.execute(
            "SELECT 1 FROM seen_posts_v2 WHERE url_hash=? AND channel=?",
            (_hash(url), channel)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM seen_posts_v2 WHERE url_hash=?", (_hash(url),)
        ).fetchone()
    conn.close()
    return row is not None


def add_seen(url: str, channel: str = ''):
    """Запоминает URL для данного канала. channel='' — глобальная запись."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts_v2 (url_hash, channel, seen_at) VALUES (?, ?, ?)",
        (_hash(url), channel, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()),
    )
    conn.commit()
    conn.close()


def _hash_or_empty(url: str | None) -> str:
    if not url:
        return ""
    return _hash(url)


def _normalize_text_for_fuzzy(text: str) -> str:
    """Нормализация для fuzzy-дедупа внутри канала (URL/хвосты/шум удаляем)."""
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"#\w+", " ", t)
    t = re.sub(r"[^a-zа-я0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _looks_like_near_duplicate(conn: sqlite3.Connection, channel: str, post_content: str, cutoff: str) -> bool:
    """Проверка почти-дублей в пределах канала по похожести текста."""
    target = _normalize_text_for_fuzzy(post_content)
    if len(target) < 40:
        return False

    # Последние уже опубликованные тексты канала
    posted_rows = conn.execute(
        """
        SELECT COALESCE(rewritten_text, original_text, '') AS txt
        FROM posts
        WHERE channel=? AND status='posted' AND posted_at>=?
        ORDER BY posted_at DESC
        LIMIT 300
        """,
        (channel, cutoff),
    ).fetchall()

    # И pending-очередь канала, чтобы не пускать дубль в тот же прогон
    pending_rows = conn.execute(
        """
        SELECT COALESCE(rewritten_text, original_text, '') AS txt
        FROM posts
        WHERE channel=? AND status='pending'
        ORDER BY created_at DESC
        LIMIT 120
        """,
        (channel,),
    ).fetchall()

    for row in list(posted_rows) + list(pending_rows):
        cand = _normalize_text_for_fuzzy((row[0] or ""))
        if len(cand) < 40:
            continue
        if cand == target:
            return True
        # Быстрый containment для почти идентичных текстов
        shorter, longer = (cand, target) if len(cand) <= len(target) else (target, cand)
        if len(shorter) >= 60 and shorter in longer and (len(shorter) / max(len(longer), 1) >= 0.88):
            return True
        # Fuzzy-сравнение: ловим одинаковые новости с разными URL/косметикой
        if SequenceMatcher(None, target, cand).ratio() >= 0.94:
            return True
    return False


def is_duplicate(post_content: str, channel: str, source_url: str | None = None, days: int = SEEN_POSTS_TTL_DAYS) -> bool:
    """
    Проверка дубля в рамках канала за последние N дней.
    Дубль если совпал text_hash ИЛИ url_hash.
    """
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
    th = text_hash(post_content)
    uh = _hash_or_empty(source_url)
    if not th and not uh:
        return False

    conn = get_conn()
    row = conn.execute(
        """
        SELECT 1
        FROM published_history
        WHERE channel=? AND seen_at>=?
          AND (
            (? <> '' AND text_hash=?)
            OR
            (? <> '' AND url_hash=?)
          )
        LIMIT 1
        """,
        (channel, cutoff, th, th, uh, uh),
    ).fetchone()
    if row is None:
        # Также защитимся от дублей внутри текущей очереди
        row = conn.execute(
            """
            SELECT 1
            FROM posts
            WHERE channel=? AND status='pending'
              AND (
                (? <> '' AND text_hash=?)
                OR
                (? <> '' AND url_hash=?)
              )
            LIMIT 1
            """,
            (channel, th, th, uh, uh),
        ).fetchone()

    # Второй слой: fuzzy-дедуп в пределах этого же канала
    # (URL-дедуп сохраняется выше как первый быстрый слой)
    if row is None and post_content:
        try:
            if _looks_like_near_duplicate(conn, channel, post_content, cutoff):
                conn.close()
                return True
        except Exception as e:
            logger.debug(f"fuzzy dedup check skipped for {channel}: {e}")

    conn.close()
    return row is not None


def add_history(channel: str, post_content: str, source_url: str | None, post_id: int | None = None, status: str = 'posted'):
    """Сохраняет запись в published_history (канал-специфично)."""
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO published_history (channel, text_hash, url_hash, post_id, status, seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (channel, text_hash(post_content), _hash_or_empty(source_url), post_id, status, datetime.now(timezone.utc).replace(tzinfo=None).isoformat()),
    )
    conn.commit()
    conn.close()


def cleanup_seen_posts():
    """Удаляет seen_posts/published_history старше SEEN_POSTS_TTL_DAYS дней."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=SEEN_POSTS_TTL_DAYS)).isoformat()
    conn = get_conn()
    cur1 = conn.execute("DELETE FROM seen_posts_v2 WHERE seen_at < ?", (cutoff,))
    cur2 = conn.execute("DELETE FROM published_history WHERE seen_at < ?", (cutoff,))
    deleted_seen = cur1.rowcount
    deleted_hist = cur2.rowcount
    conn.commit()
    conn.close()
    if deleted_seen > 0 or deleted_hist > 0:
        logger.info(
            f"🧹 Очистка истории: seen_posts={deleted_seen}, published_history={deleted_hist} "
            f"(старше {SEEN_POSTS_TTL_DAYS} дней)"
        )
    return deleted_seen + deleted_hist


# ── Посты ───────────────────────────────────────────────────────────────────

def add_post(source_type: str, source_id: str, original_text: str,
             rewritten_text: str, media_url: str, media_type: str,
             channel: str, media_files: list = None, source_url: str = None) -> int:
    conn = get_conn()
    th = text_hash(original_text or "")
    uh = _hash_or_empty(source_url)
    cur = conn.execute(
        """INSERT INTO posts
           (source_type, source_id, original_text, rewritten_text,
            media_url, media_type, media_files, channel, status, created_at, text_hash, url_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (source_type, source_id, original_text, rewritten_text,
         media_url, media_type,
         json.dumps(media_files) if media_files else None,
         channel, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), th, uh),
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return post_id


def mark_posted(post_id: int):
    conn = get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        "UPDATE posts SET status='posted', posted_at=? WHERE id=?",
        (now, post_id),
    )
    row = conn.execute(
        "SELECT channel, text_hash, url_hash FROM posts WHERE id=?",
        (post_id,),
    ).fetchone()
    if row:
        conn.execute(
            """
            INSERT INTO published_history (channel, text_hash, url_hash, post_id, status, seen_at)
            VALUES (?, ?, ?, ?, 'posted', ?)
            """,
            (row["channel"], row["text_hash"], row["url_hash"], post_id, now),
        )
    conn.commit()
    conn.close()


def mark_skipped(post_id: int, reason: str = None):
    """Помечает пост как пропущенный с опциональной причиной."""
    conn = get_conn()
    conn.execute(
        "UPDATE posts SET status='skipped', skip_reason=? WHERE id=?",
        (reason, post_id),
    )
    conn.commit()
    conn.close()


def get_pending_posts(channel: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM posts WHERE channel=? AND status='pending' ORDER BY created_at ASC LIMIT ?",
        (channel, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_pending_posts(channel: str) -> int:
    conn = get_conn()
    c = conn.execute("SELECT COUNT(*) FROM posts WHERE channel=? AND status='pending'", (channel,)).fetchone()[0]
    conn.close()
    return int(c)


# --- Каналы-близнецы: разведение контента (2026-07-27) ---
# «Мастерская Вяза» и «Схемы вязания» имеют ОДИНАКОВЫЕ 11 доноров и снимали
# верхушку одного списка -> 96% контента совпадало, клоны выходили с разницей
# в секунды. Cross-channel dedup выключен глобально (нужен городам, где новость
# обязана выйти в нескольких каналах), поэтому разводим ТОЧЕЧНО: только пары
# из config.sibling_dedup.groups. Подстраховка: если ВСЕ кандидаты — клоны
# близнеца, публикуем первого (мягкая деградация, слот не теряем).


def load_config():
    """Ленивый конфиг (мокается в тестах)."""
    try:
        from utils import load_config as _lc
        return _lc() or {}
    except Exception:
        return {}


def _sibling_channels(channel: str) -> tuple[list, int]:
    """(список каналов-близнецов, окно в часах). Пусто = разведение выключено."""
    try:
        cfg = load_config() or {}
        sd = cfg.get("sibling_dedup") or {}
        if not sd.get("enabled"):
            return [], 0
        window = int(sd.get("window_hours", 12) or 12)
        for group in (sd.get("groups") or []):
            if channel in group:
                return [c for c in group if c != channel], window
    except Exception:
        pass
    return [], 0


def claim_pending_post(channel: str, media_type_filter: str = "any",
                       video_first: bool = False) -> dict | None:
    """
    Атомарно забирает один pending-пост в processing, чтобы второй процесс его не взял.

    2026-04-29: добавлен дедуп по text_hash — fetcher периодически создаёт пары
    записей с одинаковым text_hash за 1-3мс (race condition), а планировщик их
    публиковал как разные посты. Теперь перед claim'ом проверяем что text_hash
    не совпадает с уже опубликованным в этом канале (за 7 дней). Если совпал —
    помечаем как 'duplicate' и берём следующий. До 20 попыток за вызов.
    """
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if media_type_filter == "video":
            media_filter = "AND media_type='video'"
        elif media_type_filter == "photo":
            media_filter = "AND media_type='photo'"
        elif media_type_filter == "text":
            media_filter = "AND (media_type IS NULL OR media_type='')"
        elif media_type_filter == "require_media":
            media_filter = "AND media_url IS NOT NULL AND media_url <> ''"
        else:
            media_filter = ""

        # Берём до 20 кандидатов (старейших), чтобы дедуп мог пропустить дубли.
        candidates = conn.execute(
            f"""
            SELECT * FROM posts
            WHERE channel=? AND status='pending' {media_filter}
            ORDER BY {"(CASE WHEN media_type='video' THEN 0 ELSE 1 END), " if video_first else ""}datetime(created_at) ASC
            LIMIT 20
            """,
            (channel,),
        ).fetchall()

        _sibs, _sib_window = _sibling_channels(channel)
        _deferred = []          # клоны близнеца — резерв на случай, если больше нечего
        for row in candidates:
            text_hash = row["text_hash"] if "text_hash" in row.keys() else None
            url_hash = row["url_hash"] if "url_hash" in row.keys() else None
            is_dup = False

            # Проверка text_hash в posts.posted
            if text_hash:
                dup = conn.execute(
                    "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                    " AND text_hash=? AND id != ? LIMIT 1",
                    (channel, text_hash, row["id"]),
                ).fetchone()
                if dup:
                    is_dup = True

            # Проверка url_hash в posts.posted (на случай разных text но того же source)
            if not is_dup and url_hash:
                dup = conn.execute(
                    "SELECT 1 FROM posts WHERE channel=? AND status='posted'"
                    " AND url_hash=? AND id != ? LIMIT 1",
                    (channel, url_hash, row["id"]),
                ).fetchone()
                if dup:
                    is_dup = True

            # Фоллбек: published_history за 7 дней
            if not is_dup and text_hash:
                dup = conn.execute(
                    "SELECT 1 FROM published_history WHERE channel=?"
                    " AND text_hash=? AND seen_at > datetime('now', '-7 days')"
                    " LIMIT 1",
                    (channel, text_hash),
                ).fetchone()
                if dup:
                    is_dup = True

            # Близнец уже выпустил этот текст недавно -> откладываем, берём другой
            if not is_dup and _sibs and text_hash:
                _ph = ",".join("?" * len(_sibs))
                _sd = conn.execute(
                    f"SELECT 1 FROM posts WHERE channel IN ({_ph}) AND status='posted'"
                    " AND text_hash=? AND posted_at > datetime('now', ?) LIMIT 1",
                    (*_sibs, text_hash, f"-{_sib_window} hours"),
                ).fetchone()
                if _sd:
                    _deferred.append(row)
                    continue

            if is_dup:
                # Пометить как duplicate и продолжить со следующим кандидатом
                conn.execute(
                    "UPDATE posts SET status='duplicate', skip_reason='text_hash dup at claim'"
                    " WHERE id=? AND status='pending'",
                    (row["id"],),
                )
                continue

            # Не дубль — пытаемся забрать
            conn.execute(
                "UPDATE posts SET status='processing' WHERE id=? AND status='pending'",
                (row["id"],),
            )
            changed = conn.execute("SELECT changes()").fetchone()[0]
            if changed == 0:
                # Кто-то перехватил — берём следующего
                continue

            claimed = conn.execute("SELECT * FROM posts WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            conn.close()
            return dict(claimed) if claimed else None

        # ПОДСТРАХОВКА: свободных не-клонов нет — публикуем отложенного клона,
        # иначе канал промолчит (лучше повтор у близнеца, чем пустой слот).
        for row in _deferred:
            conn.execute(
                "UPDATE posts SET status='processing' WHERE id=? AND status='pending'",
                (row["id"],),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 0:
                continue
            claimed = conn.execute("SELECT * FROM posts WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            conn.close()
            logger.info(f"[{channel}] sibling-клон опубликован (альтернатив нет)")
            return dict(claimed) if claimed else None

        # Все 20 кандидатов оказались дублями или перехвачены
        conn.commit()
        conn.close()
        return None
    except Exception:
        conn.rollback()
        conn.close()
        raise


def get_stats() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT channel, status, COUNT(*) as cnt FROM posts GROUP BY channel, status"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def requeue_stale_processing(older_than_minutes: int = 30) -> int:
    """Возвращает зависшие processing обратно в pending."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=older_than_minutes)).isoformat()
    conn = get_conn()
    cur = conn.execute(
        "UPDATE posts SET status='pending' WHERE status='processing' AND created_at < ?",
        (cutoff,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def get_skip_reasons(channel: str = None, limit: int = 20) -> list[dict]:
    """Возвращает последние причины пропуска постов."""
    conn = get_conn()
    if channel:
        rows = conn.execute(
            "SELECT channel, skip_reason, COUNT(*) as cnt FROM posts "
            "WHERE status='skipped' AND skip_reason IS NOT NULL AND channel=? "
            "GROUP BY channel, skip_reason ORDER BY cnt DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT channel, skip_reason, COUNT(*) as cnt FROM posts "
            "WHERE status='skipped' AND skip_reason IS NOT NULL "
            "GROUP BY channel, skip_reason ORDER BY cnt DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Ads ────────────────────────────────────────────────────────────────────

def get_ad_slots(enabled_only: bool = True) -> list[str]:
    conn = get_conn()
    if enabled_only:
        rows = conn.execute("SELECT slot_time FROM ad_slots WHERE enabled=1 ORDER BY slot_time").fetchall()
    else:
        rows = conn.execute("SELECT slot_time FROM ad_slots ORDER BY slot_time").fetchall()
    conn.close()
    return [r[0] for r in rows]


def set_ad_slots(slots: list[str]):
    conn = get_conn()
    conn.execute("DELETE FROM ad_slots")
    for s in sorted(set(slots)):
        conn.execute("INSERT INTO ad_slots (slot_time, enabled) VALUES (?,1)", (s,))
    conn.commit()
    conn.close()


def add_ad_campaign(channel_ids: list[str], ad_text: str, media_file_id: str | None,
                    markup: str | None, target_dates: list[str], slots: list[str],
                    channel_overrides: dict | None = None,
                    auto_delete_after_min: int = 0) -> int:
    """Создаёт записи ads_schedule для каналов/дат/слотов. Возвращает количество записей."""
    created = 0
    conn = get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    channel_overrides = channel_overrides or {}
    for ch in channel_ids:
        ov = channel_overrides.get(str(ch), {}) if isinstance(channel_overrides, dict) else {}
        ch_text = ov.get("ad_text", ad_text)
        ch_media = ov.get("media_file_id", media_file_id)
        ch_markup = ov.get("markup", markup)
        for d in target_dates:
            for sl in slots:
                target = f"{d}T{sl}:00"
                conn.execute(
                    """
                    INSERT INTO ads_schedule (channel_id, ad_text, media_file_id, markup, target_datetime, status, created_at, auto_delete_after_min)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (str(ch), ch_text, ch_media, ch_markup, target, now, int(auto_delete_after_min or 0)),
                )
                created += 1
    conn.commit()
    conn.close()
    return created


def get_due_ad(channel_id: str, local_now: datetime):
    """Возвращает pending-рекламу на текущий локальный слот канала (если есть)."""
    slot = local_now.strftime("%H:%M")
    target = local_now.strftime("%Y-%m-%dT%H:%M:00")
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM ads_schedule
        WHERE channel_id=? AND status='pending' AND target_datetime=?
        ORDER BY id ASC
        LIMIT 1
        """,
        (str(channel_id), target),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def claim_ad(ad_id: int) -> bool:
    """Атомарно переводит рекламу pending -> processing."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE ads_schedule SET status='processing' WHERE id=? AND status='pending'",
            (ad_id,),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        conn.close()
        return changed == 1
    except Exception:
        conn.rollback()
        conn.close()
        raise


def mark_ad_published(ad_id: int, published_mid: str | None = None):
    conn = get_conn()
    row = conn.execute("SELECT auto_delete_after_min FROM ads_schedule WHERE id=?", (ad_id,)).fetchone()
    ttl = int((row[0] if row and row[0] is not None else 0) or 0)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delete_at = (now + timedelta(minutes=ttl)).isoformat() if ttl > 0 else None
    delete_status = 'pending' if ttl > 0 else None
    conn.execute(
        """
        UPDATE ads_schedule
        SET status='published', published_at=?, published_mid=?, delete_at=?, delete_status=?
        WHERE id=?
        """,
        (now.isoformat(), published_mid, delete_at, delete_status, ad_id),
    )
    conn.commit()
    conn.close()


def mark_ad_error(ad_id: int, error: str):
    conn = get_conn()
    conn.execute(
        "UPDATE ads_schedule SET status='error', error=? WHERE id=?",
        (error[:500], ad_id),
    )
    conn.commit()
    conn.close()


def list_due_ad_deletions(limit: int = 100) -> list[dict]:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM ads_schedule
        WHERE status='published'
          AND delete_status='pending'
          AND delete_at IS NOT NULL
          AND delete_at <= ?
          AND published_mid IS NOT NULL
        ORDER BY delete_at ASC
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_ad_deleted(ad_id: int):
    conn = get_conn()
    conn.execute(
        "UPDATE ads_schedule SET delete_status='deleted', deleted_at=?, delete_error=NULL WHERE id=?",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), ad_id),
    )
    conn.commit(); conn.close()


def mark_ad_delete_error(ad_id: int, error: str):
    conn = get_conn()
    conn.execute(
        """
        UPDATE ads_schedule
        SET delete_status='error', delete_error=?, delete_attempts=COALESCE(delete_attempts,0)+1
        WHERE id=?
        """,
        ((error or '')[:500], ad_id),
    )
    conn.commit(); conn.close()


def list_ads_bookings_next_24h(now_utc: datetime | None = None, limit: int = 200) -> list[dict]:
    """Список рекламных броней (pending/published/error) в ближайшие 24 часа по target_datetime."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now_utc.isoformat(timespec='seconds')
    to_iso = (now_utc + timedelta(hours=24)).isoformat(timespec='seconds')
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM ads_schedule
        WHERE target_datetime >= ? AND target_datetime <= ?
        ORDER BY target_datetime ASC
        LIMIT ?
        """,
        (now_iso, to_iso, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ad_stats() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT status, COUNT(*) as c FROM ads_schedule GROUP BY status").fetchall()
    conn.close()
    m = {r[0]: r[1] for r in rows}
    return {
        "pending": m.get("pending", 0),
        "editing": m.get("editing", 0),
        "processing": m.get("processing", 0),
        "published": m.get("published", 0),
        "error": m.get("error", 0),
        "total": sum(m.values()),
    }


def _ext_virtual_ad_id(ext_id: int) -> int:
    # Диапазон, не пересекающийся с обычными ads_schedule.id
    return 1_000_000_000 + int(ext_id)


def upsert_external_ad_post(channel_id: str, mid: str, published_at: str, slot_local: str | None = None) -> int:
    """Сохраняет внешний рекламный пост и возвращает виртуальный ad_id для ad_stats."""
    # FP-guard (2026-06-17): НЕ записывать НАШ собственный пост как внешнюю рекламу.
    # Без этого collector ловил наш контентный пост (напр. слот 19:00) в ad-окне →
    # external_ad_posts → ложный slot_shift → дубль поста. is_bot_posted_mid = наши mid'ы.
    if is_bot_posted_mid(str(channel_id), str(mid)):
        return 0
    conn = get_conn()
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO external_ad_posts (channel_id, mid, published_at, slot_local, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(channel_id), str(mid), str(published_at), (slot_local or ""), now),
    )
    row = conn.execute(
        "SELECT id FROM external_ad_posts WHERE channel_id=? AND mid=? LIMIT 1",
        (str(channel_id), str(mid)),
    ).fetchone()
    conn.commit()
    conn.close()
    ext_id = int(row[0]) if row else 0
    return _ext_virtual_ad_id(ext_id) if ext_id else 0


def list_external_ads_for_view_stats(limit: int = 1000) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT id, channel_id, mid, published_at, slot_local, detected_at
        FROM external_ad_posts
        ORDER BY published_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ad_id"] = _ext_virtual_ad_id(int(d["id"]))
        out.append(d)
    return out


def list_ads_for_view_stats(limit: int = 500) -> list[dict]:
    """Опубликованные рекламные записи, по которым нужно собирать охваты."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM ads_schedule
        WHERE status='published'
          AND published_at IS NOT NULL
          AND published_mid IS NOT NULL
          AND COALESCE(auto_delete_after_min, 0) >= 1440
        ORDER BY published_at ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_ad_view_stat(ad_id: int, hours_since_publish: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM ad_stats WHERE ad_id=? AND hours_since_publish=? LIMIT 1",
        (ad_id, int(hours_since_publish)),
    ).fetchone()
    conn.close()
    return row is not None


def add_ad_view_stat(ad_id: int, channel_id: str, mid: str, hours_since_publish: int,
                     views: int | None, payload: str | None = None):
    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO ad_stats (
            ad_id, channel_id, mid, hours_since_publish, views, collected_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(ad_id), str(channel_id), str(mid), int(hours_since_publish),
            (int(views) if views is not None else None),
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            (payload or "")[:4000],
        ),
    )
    conn.commit()
    conn.close()


def ad_views_summary() -> dict:
    conn = get_conn()
    snapshots = conn.execute("SELECT COUNT(*) FROM ad_stats").fetchone()[0]
    ads_with_stats = conn.execute("SELECT COUNT(DISTINCT ad_id) FROM ad_stats").fetchone()[0]
    per = {}
    for h in (24, 48, 72):
        row = conn.execute(
            "SELECT COUNT(*), AVG(views) FROM ad_stats WHERE hours_since_publish=? AND views IS NOT NULL",
            (h,),
        ).fetchone()
        per[h] = {
            "count": int(row[0] or 0),
            "avg_views": int(float(row[1])) if row[1] is not None else None,
        }
    latest = conn.execute(
        """
        SELECT ad_id, channel_id, hours_since_publish, views, collected_at
        FROM ad_stats
        ORDER BY collected_at DESC
        LIMIT 8
        """
    ).fetchall()
    conn.close()
    return {
        "snapshots": int(snapshots or 0),
        "ads_with_stats": int(ads_with_stats or 0),
        "per": per,
        "latest": [dict(r) for r in latest],
    }


def list_ad_views(limit: int = 80) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT ad_id, channel_id, hours_since_publish, views, collected_at
        FROM ad_stats
        ORDER BY collected_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_ad_views_channel_agg() -> list[dict]:
    """Агрегированные охваты по каналам (24/48/72 + количество объявлений)."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
          channel_id,
          COUNT(DISTINCT ad_id) AS ads_count,
          SUM(CASE WHEN hours_since_publish=24 THEN COALESCE(views,0) ELSE 0 END) AS v24_sum,
          AVG(CASE WHEN hours_since_publish=24 THEN views END) AS v24_avg,
          SUM(CASE WHEN hours_since_publish=48 THEN COALESCE(views,0) ELSE 0 END) AS v48_sum,
          AVG(CASE WHEN hours_since_publish=48 THEN views END) AS v48_avg,
          SUM(CASE WHEN hours_since_publish=72 THEN COALESCE(views,0) ELSE 0 END) AS v72_sum,
          AVG(CASE WHEN hours_since_publish=72 THEN views END) AS v72_avg,
          MAX(collected_at) AS last_collected
        FROM ad_stats
        GROUP BY channel_id
        ORDER BY v24_sum DESC, ads_count DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ad_views_global_summary() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*), SUM(COALESCE(views,0)), AVG(views) FROM ad_stats").fetchone()
    by = {}
    for h in (24, 48, 72):
        r = conn.execute(
            "SELECT COUNT(*), SUM(COALESCE(views,0)), AVG(views) FROM ad_stats WHERE hours_since_publish=?",
            (h,),
        ).fetchone()
        by[h] = {
            "count": int(r[0] or 0),
            "sum": int(r[1] or 0),
            "avg": int(float(r[2])) if r[2] is not None else None,
        }
    conn.close()
    return {
        "count": int(total[0] or 0),
        "sum": int(total[1] or 0),
        "avg": int(float(total[2])) if total[2] is not None else None,
        "by": by,
    }


def list_pending_ads(limit: int = 500) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ads_schedule WHERE status='pending' ORDER BY target_datetime ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ad_by_id(ad_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM ads_schedule WHERE id=?", (ad_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def claim_ad_edit(ad_id: int) -> bool:
    """Lock ad for editing: pending -> editing."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE ads_schedule SET status='editing' WHERE id=? AND status='pending'",
            (ad_id,),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        conn.close()
        return changed == 1
    except Exception:
        conn.rollback()
        conn.close()
        raise


def release_ad_edit(ad_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE ads_schedule SET status='pending' WHERE id=? AND status='editing'", (ad_id,))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed == 1


def update_ad_text(ad_id: int, text: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE ads_schedule SET ad_text=? WHERE id=?", (text, ad_id))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed == 1


def update_ad_media(ad_id: int, media_file_id: str | None) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE ads_schedule SET media_file_id=? WHERE id=?", (media_file_id, ad_id))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed == 1


def update_ad_target_datetime(ad_id: int, target_dt: str) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE ads_schedule SET target_datetime=? WHERE id=?", (target_dt, ad_id))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed == 1


def delete_ad(ad_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM ads_schedule WHERE id=? AND status IN ('pending','editing','error')", (ad_id,))
    changed = cur.rowcount
    conn.commit(); conn.close()
    return changed == 1


def requeue_stale_ads_processing(older_than_minutes: int = 30) -> int:
    """Возвращает зависшие ads из processing обратно в pending."""
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=older_than_minutes)).isoformat()
    conn = get_conn()
    cur = conn.execute(
        "UPDATE ads_schedule SET status='pending' WHERE status='processing' AND created_at < ?",
        (cutoff,),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed


def check_ad_slot(channel_id: str, local_now: datetime) -> dict:
    """
    Проверка рекламного слота/паузы.
    - если на текущий слот есть реклама: блокируем контент
    - post-ad pause включаем ТОЛЬКО если в прошлом рекламном часу реально есть запись ads_schedule
      (pending/published) для этого канала.
    """
    slots = get_ad_slots(enabled_only=True)
    now_hm = local_now.strftime("%H:%M")

    # 1) Реклама в текущий слот
    ad = get_due_ad(channel_id, local_now)
    if ad:
        return {
            "has_ad": True,
            "ad": ad,
            "block_content": True,
            "reason": f"ad scheduled at {now_hm}",
            "shift_to": (local_now.replace(second=0, microsecond=0) + timedelta(hours=1)),
        }

    # 2) Пауза после рекламы (30-45 мин) — только при фактической рекламе в этот ad-час
    cur_minutes = local_now.hour * 60 + local_now.minute
    conn = get_conn()
    try:
        for s in slots:
            h, m = map(int, s.split(":"))
            ad_minutes = h * 60 + m
            delta = cur_minutes - ad_minutes
            if not (0 < delta <= 45):
                continue

            ad_dt_local = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            ad_target = ad_dt_local.strftime("%Y-%m-%dT%H:%M:00")

            # ВАЖНО: проверяем факт наличия рекламы в ads_schedule на этот ad-слот
            has_ad_record = conn.execute(
                """
                SELECT 1
                FROM ads_schedule
                WHERE channel_id=?
                  AND target_datetime=?
                  AND status IN ('pending', 'published')
                LIMIT 1
                """,
                (str(channel_id), ad_target),
            ).fetchone() is not None

            if not has_ad_record:
                # Нет рекламы в ad-слоте -> нет паузы, не блокируем контент
                continue

            shift = ad_dt_local + timedelta(hours=1)
            return {
                "has_ad": False,
                "ad": None,
                "block_content": True,
                "reason": f"post-ad pause after {s}",
                "shift_to": shift,
            }
    finally:
        conn.close()

    return {"has_ad": False, "ad": None, "block_content": False, "reason": None, "shift_to": None}


# ── Slot locks (атомарная защита 1 канал = 1 пост в слот) ──────────────────

def try_acquire_slot(channel: str, slot_key: str, ttl_minutes: int = 10) -> bool:
    """
    Атомарно пытается занять слот для канала.
    slot_key = "YYYY-MM-DD HH:MM" (округлено до слота).
    Возвращает True если слот занят успешно, False если уже занят.
    """
    from datetime import datetime, timezone, timedelta
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    # Чистим старые locks (старше ttl)
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).isoformat()
    conn.execute("DELETE FROM slot_locks WHERE locked_at < ?", (cutoff,))
    try:
        conn.execute(
            "INSERT INTO slot_locks (channel, slot_key, locked_at) VALUES (?, ?, ?)",
            (channel, slot_key, now)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        conn.close()
        return False


def release_slot(channel: str, slot_key: str):
    """Освобождает слот после публикации."""
    conn = get_conn()
    conn.execute("DELETE FROM slot_locks WHERE channel=? AND slot_key=?", (channel, slot_key))
    conn.commit()
    conn.close()


# ── bot_posted_mids helpers (Task 3 — ad-overlay) ──────────────────────────

def insert_bot_posted_mid(channel_id: str, mid: str) -> None:
    """INSERT OR IGNORE наш mid после публикации.

    Best-effort: исключения подавляются (DB lock не должен ломать публикацию).
    """
    if not channel_id or not mid:
        return
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO bot_posted_mids (channel_id, mid, posted_at) VALUES (?, ?, ?)",
                (str(channel_id), str(mid), datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def is_bot_posted_mid(channel_id: str, mid: str) -> bool:
    """True если mid был опубликован нами (есть в bot_posted_mids)."""
    if not channel_id or not mid:
        return False
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM bot_posted_mids WHERE channel_id=? AND mid=? LIMIT 1",
                (str(channel_id), str(mid)),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def _parse_iso_utc(s):
    """ISO-строку → aware UTC datetime. naive трактуется как UTC. None при ошибке."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bot_posted_after(channel_id: str, after_iso: str) -> bool:
    """True если НАШ бот публиковал в этот channel_id ПОЗЖЕ момента after_iso.

    Race-check для ad-cover: если штатный news-слот опубликовал пост в канал
    ПОСЛЕ того как watcher засёк рекламу — реклама уже перекрыта штатным постом,
    cover не нужен (иначе дубль «два поста в одну минуту»).

    Сравнение через парсинг datetime, НЕ лексикографически: posted_at пишется
    insert_bot_posted_mid через utcnow() (naive), а after_iso (seen_at watcher'а)
    — aware (+00:00); строковое сравнение разных форматов даёт неверный результат.
    """
    if not channel_id or not after_iso:
        return False
    after_dt = _parse_iso_utc(after_iso)
    if after_dt is None:
        return False
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT posted_at FROM bot_posted_mids WHERE channel_id=?",
                (str(channel_id),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return False
    for r in rows:
        pa = _parse_iso_utc(r[0])
        if pa is not None and pa > after_dt:
            return True
    return False


def is_planned_ad_time(channel_key: str, seen_at_iso: str, *,
                       before_min: int = 15, after_min: int = 55) -> bool:
    """True если реклама (seen_at) вышла в ПЛАНОВОЕ рекламное окно канала:
    [ad_hour - before_min, ad_hour + after_min] по ЛОКАЛЬНОМУ времени (tz_group +
    news_ad_windows). Плановую рекламу ad-cover НЕ крывает — её перекроет штатный
    news-слот (час тишины). Только ВНЕплановая (вне всех окон) подлежит cover.

    False (=внеплановая/неизвестно) если: нет канала/tz_group/ad_windows, кривой
    seen_at, пустые аргументы.
    """
    if not channel_key or not seen_at_iso:
        return False
    seen = _parse_iso_utc(seen_at_iso)
    if seen is None:
        return False
    try:
        conn = get_conn()
        try:
            r = conn.execute("SELECT tz_group FROM news_channels WHERE channel_key=?",
                             (channel_key,)).fetchone()
            tz_group = (r[0] if r else None)
            if not tz_group:
                return False
            ad_rows = conn.execute("SELECT ad_hour FROM news_ad_windows WHERE tz_group=?",
                                   (tz_group,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return False
    if not ad_rows:
        return False
    try:
        from slot_shift import TZ_OFFSETS
        off = 3 + TZ_OFFSETS.get(tz_group, 0)
    except Exception:
        off = 3
    local = seen + timedelta(hours=off)
    local_min = local.hour * 60 + local.minute
    for row in ad_rows:
        try:
            ah = str(row[0])
            ah_min = int(ah[:2]) * 60 + int(ah[3:5])
        except (ValueError, IndexError, TypeError):
            continue
        if (ah_min - before_min) <= local_min <= (ah_min + after_min):
            return True
    return False


def cleanup_old_bot_mids(ttl_hours: int = 48) -> int:
    """DELETE bot_posted_mids старше ttl_hours. Возвращает кол-во удалённых."""
    cutoff = (datetime.utcnow() - timedelta(hours=ttl_hours)).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM bot_posted_mids WHERE posted_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── external_posts_seen (MAX ad-cover watcher, 24.06) ──────────────────────
# Перенос зрелого механизма Насти (content-bot-client) на MAX-сеть.

def record_planned_ad_for_shift(channel_key: str, mid: str, seen_at_iso: str) -> bool:
    """Плановая реклама от watcher -> external_ad_posts (источник slot_shift).

    Кейс Челябинск 30.07.2026: плановый сканер ad_detection видит окно
    [час-15, час+30], реклама на 31-55-й минуте в external_ad_posts не
    попадала -> slot_shift о ней не знал -> следующий слот выходил ровно
    и перекрывал рекламу раньше «часа в топе» (16 случаев за день).
    Watcher видит рекламу мгновенно — доливаем его плановые детекты сюда.

    published_at пишем naive-UTC до секунд (формат ad_detection): строку
    с '+offset' slot_shift трактует как ЛОКАЛЬНУЮ и конверсия поясов врёт.
    Fail-open: нет канала / кривое время -> False, ничего не пишем."""
    if not channel_key or not mid or not seen_at_iso:
        return False
    from datetime import datetime as _dt
    try:
        ts = _dt.fromisoformat(str(seen_at_iso).replace("Z", "+00:00"))
        published_at = ts.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return False
    conn = get_conn()
    try:
        row = conn.execute("SELECT channel_id FROM news_channels WHERE channel_key=?",
                           (channel_key,)).fetchone()
        if not row or not row[0]:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO external_ad_posts "
            "(channel_id, mid, published_at, detected_at) VALUES (?, ?, ?, ?)",
            (str(row[0]), mid, published_at, _dt.utcnow().isoformat()))
        conn.commit()
        return True
    except Exception:
        logger.exception("record_planned_ad_for_shift failed")
        return False
    finally:
        conn.close()


def record_external_post(channel_key: str, mid: str, seen_at_iso: str,
                         text: str | None = None) -> bool:
    """INSERT OR IGNORE внешнего (не нашего) поста для последующего ad-cover.

    Возвращает True если запись новая, False если уже была (idempotent —
    watcher может пересмотреть тот же update)."""
    if not channel_key or not mid:
        return False
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO external_posts_seen "
            "(channel_key, mid, seen_at, text, status) VALUES (?, ?, ?, ?, 'pending')",
            (channel_key, str(mid), seen_at_iso, text),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_external_planned(channel_key: str, mid: str) -> bool:
    """Пометить внешний пост ПЛАНОВОЙ рекламой сразу при записи (2026-07-27).

    Фикс гонки: watcher писал status='pending', а skipped_planned ставил только
    ad_cover при своём тике — в окне гонки guard 3b считал плановую рекламу
    внеплановой (27.07: ложный UNPLANNED_AD_QUIET на 103 каналах). Обновляет
    только pending — финальные статусы (covered/error/...) не трогает."""
    if not channel_key or not mid:
        return False
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE external_posts_seen SET status='skipped_planned' "
            "WHERE channel_key=? AND mid=? AND status='pending'",
            (channel_key, str(mid)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def external_post_recent(channel_key: str, minutes: int = 60) -> bool:
    """True если watcher засёк внешний пост в канале за последние `minutes` минут.
    Используется ad-guard'ом publisher'а (не публиковать слот поверх свежей рекламы)."""
    if not channel_key:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM external_posts_seen WHERE channel_key=? AND seen_at>=? LIMIT 1",
            (channel_key, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def last_external_seen_within(channel_key: str, minutes: int,
                              db_path: str | None = None,
                              now=None) -> str | None:
    """MAX(seen_at) внешнего поста канала за последние `minutes`, иначе None.

    Guard «час тишины после внеплановой рекламы» (kuznec 2026-07-19): watcher
    пишет в external_posts_seen все чужие посты real-time; publisher откладывает
    слот, пока запись свежа (ad_guard знает только плановые news_ad_windows)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _now = now or _dt.now(_tz.utc)
    cutoff = (_now - _td(minutes=minutes)).isoformat()
    conn = sqlite3.connect(db_path, timeout=30.0) if db_path else get_conn()
    try:
        # FIX 2026-07-22: тихий час ТОЛЬКО от реально чужого/внепланового.
        # skipped_our_own (наш пост) и skipped_planned (плановая реклама, у неё
        # свой ad_guard) НЕ повод молчать — иначе каждый свой пост давал 60 мин
        # тишины и слоты умирали (stale) десятками (21.07: 114 потерянных).
        row = conn.execute(
            "SELECT MAX(seen_at) FROM external_posts_seen "
            "WHERE channel_key=? AND seen_at >= ? "
            "AND (status IS NULL OR status NOT LIKE 'skipped%')",
            (channel_key, cutoff),
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        if db_path:
            conn.close()


def fetch_pending_external_covers(seen_before_iso: str) -> list[dict]:
    """Pending external posts с seen_at <= seen_before_iso (готовы к cover). ASC по seen_at."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, channel_key, mid, seen_at, text FROM external_posts_seen "
            "WHERE status='pending' AND seen_at <= ? ORDER BY seen_at ASC",
            (seen_before_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_stale_planned_externals(grace_before_iso: str,
                                  hard_after_iso: str) -> list[dict]:
    """skipped_planned-реклама в окне [hard_after, grace_before] — кандидаты на
    реанимацию (2026-07-27): плановую метим при записи (гонка watcher/guard), но
    если штатный слот погиб и не перекрыл её за grace — возвращаем в pending,
    чтобы cover перекрыл сам (Пермь 27.07: реклама 09:00 висела до 11:00)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, channel_key, mid, seen_at, text FROM external_posts_seen "
            "WHERE status='skipped_planned' AND seen_at <= ? AND seen_at >= ? "
            "ORDER BY seen_at ASC LIMIT 40",
            (grace_before_iso, hard_after_iso),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reanimate_external(eps_id: int) -> bool:
    """skipped_planned -> pending (только из этого статуса). True если вернули."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE external_posts_seen SET status='pending' "
            "WHERE id=? AND status='skipped_planned'",
            (eps_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def external_ad_published_at(mid):
    """published_at рекламы из external_ad_posts по mid (для гарда возраста ad-cover).
    None если не найдено / нет mid."""
    if not mid:
        return None
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT published_at FROM external_ad_posts WHERE mid=? ORDER BY id DESC LIMIT 1",
            (str(mid),)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def mark_cover_status(eps_id: int, status: str,
                      cover_at_iso: str | None = None) -> None:
    """Помечает запись финальным статусом (только если ещё pending — защита от гонок).

    status ∈ {published, skipped_our_own, skipped_album_dup, skipped, error}."""
    conn = get_conn()
    try:
        if cover_at_iso is not None:
            conn.execute(
                "UPDATE external_posts_seen SET status=?, cover_at=? "
                "WHERE id=? AND status='pending'",
                (status, cover_at_iso, eps_id),
            )
        else:
            conn.execute(
                "UPDATE external_posts_seen SET status=? WHERE id=? AND status='pending'",
                (status, eps_id),
            )
        conn.commit()
    finally:
        conn.close()

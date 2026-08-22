"""Решение «какие уведомления слать сегодня» (чистая логика + идемпотентность).

due_today() читает подписки/баланс из finance.db и возвращает список уведомлений,
которые надо отправить сегодня и которые ещё НЕ отправлялись (sent_notifications).
Реальная отправка + mark_sent — в fin_bot (glue). `today` передаётся ISO-строкой.
"""
import sqlite3
from datetime import date

import cost_log
import fin_subs
import fin_balance


def _db(db_path):
    return db_path or cost_log.FINANCE_DB


def _prev_month(today: str) -> str:
    d = date.fromisoformat(today)
    y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return "%04d-%02d" % (y, m)


def already_sent(db_path, kind, key, day) -> bool:
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        r = conn.execute(
            "SELECT 1 FROM sent_notifications WHERE kind=? AND key=? AND day=? LIMIT 1",
            (kind, key, day)).fetchone()
        return r is not None
    finally:
        conn.close()


def mark_sent(db_path, kind, key, day) -> None:
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sent_notifications (kind, key, day) VALUES (?,?,?)",
            (kind, key, day))
        conn.commit()
    finally:
        conn.close()


def _load_active_subs(db_path):
    conn = sqlite3.connect(_db(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT name, next_due, notify_days_before, account_email, amount, currency "
            "FROM subscriptions WHERE active=1").fetchall()]
    finally:
        conn.close()


def today_spend_usd(db_path, day):
    """Сумма cost_usd по llm_usage за календарный день (UTC) day=YYYY-MM-DD."""
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE substr(ts_utc,1,10)=?",
            (day,)).fetchone()[0] or 0.0
    finally:
        conn.close()


def _has_credits_row(db_path, provider):
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        return conn.execute("SELECT 1 FROM api_credits WHERE provider=? LIMIT 1", (provider,)).fetchone() is not None
    finally:
        conn.close()


def _fallback_used_today(db_path, day):
    """True, если сегодня был расход с резервного Anthropic-ключа (provider='claude_fb')
    = основной баланс исчерпан, работаем на резерве."""
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        return conn.execute(
            "SELECT 1 FROM llm_usage WHERE provider='claude_fb' AND substr(ts_utc,1,10)=? LIMIT 1",
            (day,)).fetchone() is not None
    finally:
        conn.close()


def due_today(db_path, today, provider="anthropic", daily_cap_usd=None,
              now_utc_hour=None, daily_report_hour=None):
    """Список уведомлений на сегодня (ещё не отправленных). Каждый — dict с 'kind'/'key'."""
    items = []
    # 1) Ежемесячный отчёт 1-го числа (за прошлый месяц)
    if today.endswith("-01"):
        items.append({"kind": "monthly", "key": _prev_month(today), "month": _prev_month(today)})
    # 2) Напоминания об оплате (за 2 и за 1 день)
    for sub, days in fin_subs.due_reminders(_load_active_subs(db_path), today):
        items.append({
            "kind": "reminder",
            "key": "%s|%s|%d" % (sub["name"], sub["next_due"], days),
            "sub": sub, "days": days,
        })
    # 3) Низкий баланс API — раздельно по провайдерам (свой баланс у каждого)
    # openai-fin-2026-08-18: perebiraem VSE stroki api_credits (a ne dva zashitykh
    # providera) i propuskaem zamorozhennye (anthropic — dostup zakryt, ne dengi).
    import fin_sheets as _fs
    _frozen = getattr(_fs, "FROZEN_PROVIDERS", set())
    _conn_p = sqlite3.connect(db_path, timeout=10)
    try:
        _provs = [r[0] for r in _conn_p.execute(
            "SELECT provider FROM api_credits ORDER BY provider")]
    except Exception:
        _provs = []
    finally:
        _conn_p.close()
    for _prov in _provs:
        if _prov in _frozen:
            continue
        _alias = _fs.PROVIDER_ALIAS.get(_prov, _prov)
        if fin_balance.is_low(db_path, _prov, claude_alias=_alias):
            items.append({"kind": "low_balance", "key": "%s|%s" % (_prov, today), "provider": _prov})
    # 3b) Переключение на резервный Anthropic-ключ (основной исчерпан) — раз в день
    if _fallback_used_today(db_path, today):
        items.append({"kind": "fallback_active", "key": today})
    # 4) Дневной потолок расхода LLM
    if daily_cap_usd:
        _spend = today_spend_usd(db_path, today)
        if _spend > float(daily_cap_usd):
            items.append({"kind": "daily_cost", "key": today, "spend": _spend, "cap": float(daily_cap_usd)})
    # 5) Ежедневный отчёт расхода в группу (раз в день, после daily_report_hour UTC)
    if daily_report_hour is not None and now_utc_hour is not None and int(now_utc_hour) >= int(daily_report_hour):
        items.append({"kind": "daily_summary", "key": today, "day": today,
                      "spend": today_spend_usd(db_path, today)})
    # фильтр уже отправленных сегодня
    return [it for it in items if not already_sent(db_path, it["kind"], it["key"], today)]


def roll_passed_subs(db_path, today):
    """Переносит next_due активных подписок на следующий месяц, если дата прошла
    (делает напоминания помесячными). Догоняет несколько месяцев. Возвращает кол-во."""
    conn = sqlite3.connect(_db(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    n = 0
    try:
        for r in conn.execute("SELECT id, next_due FROM subscriptions WHERE active=1").fetchall():
            new = r["next_due"]
            while True:
                rolled = fin_subs.roll_if_passed(new, today)
                if rolled == new:
                    break
                new = rolled
            if new != r["next_due"]:
                conn.execute("UPDATE subscriptions SET next_due=? WHERE id=?", (new, r["id"]))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n

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
            "SELECT name, next_due, notify_days_before FROM subscriptions WHERE active=1").fetchall()]
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
    for _prov, _alias in (("anthropic", "claude"), ("deepseek", "deepseek")):
        if not _has_credits_row(db_path, _prov):
            continue
        if fin_balance.is_low(db_path, _prov, claude_alias=_alias):
            items.append({"kind": "low_balance", "key": "%s|%s" % (_prov, today), "provider": _prov})
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
        for r in conn.execute("SELECT rowid, next_due FROM subscriptions WHERE active=1").fetchall():
            new = r["next_due"]
            while True:
                rolled = fin_subs.roll_if_passed(new, today)
                if rolled == new:
                    break
                new = rolled
            if new != r["next_due"]:
                conn.execute("UPDATE subscriptions SET next_due=? WHERE rowid=?", (new, r["rowid"]))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return n

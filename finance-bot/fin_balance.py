"""Баланс API-кредитов (Anthropic): остаток = пополнено − потрачено с даты as_of.

Anthropic/Groq не отдают остаток по API, поэтому ведём свой леджер:
api_credits.topped_up_usd минус сумма cost_usd из llm_usage (provider==claude_alias,
ts_utc >= as_of). Алерт при остатке < threshold_usd.
"""
import sqlite3
import cost_log


def _db(db_path):
    return db_path or cost_log.FINANCE_DB


def remaining(db_path, provider, claude_alias="claude", fresh_secs=5400) -> float:
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        row = conn.execute(
            "SELECT topped_up_usd, as_of, provider_balance_usd, provider_balance_at"
            " FROM api_credits WHERE provider=?",
            (provider,)).fetchone()
        if not row:
            return 0.0
        topped, as_of, pbal, pat = row
        if pbal is not None and pat:
            from datetime import datetime, timezone
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(pat)).total_seconds()
                if age < fresh_secs:
                    return float(pbal)
            except Exception:
                pass
        spend = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE provider=? AND ts_utc >= ?",
            (claude_alias, as_of)).fetchone()[0] or 0.0
        return float(topped) - float(spend)
    finally:
        conn.close()


def threshold(db_path, provider) -> float:
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        row = conn.execute(
            "SELECT threshold_usd FROM api_credits WHERE provider=?", (provider,)).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def is_low(db_path, provider, claude_alias="claude") -> bool:
    return remaining(db_path, provider, claude_alias) < threshold(db_path, provider)


def add_topup(db_path, provider, amount_usd) -> None:
    """openai-fin-2026-08-18: ranshe UPDATE po neizvestnomu provideru molcha
    nichego ne delal — polzovatel videl 'ok', a summa nikuda ne popadala."""
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        cur = conn.execute(
            "UPDATE api_credits SET topped_up_usd = topped_up_usd + ? WHERE provider=?",
            (float(amount_usd), provider))
        if cur.rowcount == 0:
            known = [r[0] for r in conn.execute(
                "SELECT provider FROM api_credits ORDER BY provider")]
            raise ValueError("neizvestny provider %r; est: %s" % (provider, ", ".join(known)))
        conn.commit()
    finally:
        conn.close()


def set_baseline(db_path, provider, topped_up_usd, as_of):
    """Переустановка точки отсчёта баланса: точная сумма + as_of=сейчас.
    Дальше remaining() считает расход с этого момента (чистый старт, без ретро)."""
    conn = sqlite3.connect(_db(db_path), timeout=10)
    try:
        conn.execute("UPDATE api_credits SET topped_up_usd=?, as_of=? WHERE provider=?",
                     (float(topped_up_usd), as_of, provider))
        conn.commit()
    finally:
        conn.close()

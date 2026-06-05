"""Финбот: Telegram-бот с кнопками + планировщик уведомлений.

Чистые билдеры (build_*) покрыты тестами; сетевой glue (requests Telegram API,
long-poll, часовой тик) — без тестов. Токен: env FINANCE_BOT_TOKEN. Чат: env
FINANCE_CHAT_ID (default -1004292579780). БД: cost_log.FINANCE_DB.
"""
import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

import cost_log
import fin_fx
import fin_report
import fin_balance
import fin_scheduler

TOKEN = os.environ.get("FINANCE_BOT_TOKEN", "")
CHAT_ID = os.environ.get("FINANCE_CHAT_ID", "-1004292579780")
DAILY_CAP = float(os.environ.get("FINANCE_DAILY_CAP", "0") or 0)
DB = cost_log.FINANCE_DB
_API = "https://api.telegram.org/bot%s/%s"


# ---------------- pure builders (tested) ----------------
def build_main_menu():
    return {"inline_keyboard": [
        [{"text": "📊 За месяц", "callback_data": "month"},
         {"text": "📅 Сегодня", "callback_data": "today"}],
        [{"text": "🧾 Подписки", "callback_data": "subs"},
         {"text": "💳 Баланс", "callback_data": "balance"}],
        [{"text": "➕ Пополнить", "callback_data": "topup"}],
    ]}


SECTION_KEYS = ["month", "today", "subs", "balance", "topup"]
SECTION_TITLES = {"month": "📊 За месяц", "today": "📅 Сегодня",
                  "subs": "🧾 Подписки", "balance": "💳 Баланс", "topup": "➕ Пополнить"}


def build_section_nav(key):
    """Навигация между разделами: ◀ предыдущий · 🏠 меню · следующий ▶ (карусель)."""
    i = SECTION_KEYS.index(key)
    prev_k = SECTION_KEYS[(i - 1) % len(SECTION_KEYS)]
    next_k = SECTION_KEYS[(i + 1) % len(SECTION_KEYS)]
    return {"inline_keyboard": [[
        {"text": "◀", "callback_data": "sec:" + prev_k},
        {"text": "🏠 Меню", "callback_data": "menu"},
        {"text": "▶", "callback_data": "sec:" + next_k},
    ]]}


def _fmt_amt(a):
    a = float(a)
    return ("%.0f" % a) if a == int(a) else ("%.2f" % a)


def build_balance_text(remaining, threshold):
    warn = "  ⚠️ ПОРА ПОПОЛНИТЬ" if remaining < threshold else ""
    return "💳 Anthropic API: остаток ≈ $%.2f (порог $%.0f)%s" % (remaining, threshold, warn)


def build_subs_text(rows):
    if not rows:
        return "🧾 Активных подписок нет."
    lines = ["🧾 Подписки (по дате оплаты):"]
    for r in sorted(rows, key=lambda x: x.get("next_due", "")):
        owner = (" — " + r["owner"]) if r.get("owner") else ""
        lines.append("• %s%s: %s %s — оплата %s" % (
            r.get("name"), owner, _fmt_amt(r.get("amount", 0)), r.get("currency", ""), r.get("next_due")))
    return "\n".join(lines)


def build_reminder_text(sub, days):
    return "🔔 Через %d дн. оплата: %s — %s %s (%s)" % (
        days, sub.get("name"), _fmt_amt(sub.get("amount", 0)) if sub.get("amount") is not None else "?",
        sub.get("currency", ""), sub.get("next_due"))


# ---------------- db helpers ----------------
def load_subs(db_path=None):
    conn = sqlite3.connect(db_path or DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT name, category, amount, currency, next_due, notify_days_before, owner "
            "FROM subscriptions WHERE active=1 ORDER BY next_due").fetchall()]
    finally:
        conn.close()


def _sub_by_name(db_path, name):
    for r in load_subs(db_path):
        if r["name"] == name:
            return r
    return None


# ---------------- report glue ----------------
def _today_iso():
    return datetime.now(timezone.utc).date().isoformat()


def report_text(period):
    """period = 'YYYY-MM' (месяц) или 'YYYY-MM-DD' (день) — month_report фильтрует по префиксу ts_utc."""
    usd_rub = fin_fx.get_usd_rub(DB, _today_iso())
    rep = fin_report.month_report(DB, period, usd_rub)
    return fin_report.format_report_text(rep, period, usd_rub)


def balance_text():
    return build_balance_text(fin_balance.remaining(DB, "anthropic"),
                              fin_balance.threshold(DB, "anthropic"))


def notification_text(item):
    k = item["kind"]
    if k == "monthly":
        return "📊 Ежемесячный отчёт за %s:\n\n%s" % (item["month"], report_text(item["month"]))
    if k == "reminder":
        return build_reminder_text(item.get("sub", {}), item.get("days", 0))
    if k == "low_balance":
        return balance_text()
    if k == "daily_cost":
        return "⚠️ Дневной расход LLM ${:.2f} превысил потолок ${:.2f}".format(item.get("spend", 0), item.get("cap", 0))
    return ""


# ---------------- Telegram glue (untested) ----------------
def _api(method, **params):  # pragma: no cover
    if not requests or not TOKEN:
        return None
    try:
        return requests.post(_API % (TOKEN, method), json=params, timeout=30).json()
    except Exception:
        return None


def send_message(chat_id, text, reply_markup=None):  # pragma: no cover
    p = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        p["reply_markup"] = json.dumps(reply_markup)
    return _api("sendMessage", **p)


def _api_edit(chat_id, message_id, text, reply_markup=None):  # pragma: no cover
    """Редактирует существующее сообщение (навигация на месте, без новых сообщений)."""
    p = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        p["reply_markup"] = json.dumps(reply_markup)
    return _api("editMessageText", **p)


def section_body(key):  # pragma: no cover
    if key == "month":
        return report_text(_today_iso()[:7])
    if key == "today":
        return report_text(_today_iso())
    if key == "subs":
        return build_subs_text(load_subs())
    if key == "balance":
        return balance_text()
    if key == "topup":
        return "➕ Пополнение: отправьте  /topup anthropic <сумма $>"
    return ""


def _handle_callback(data, chat_id, message_id):  # pragma: no cover
    # Навигация на месте: редактируем ТО ЖЕ сообщение (без спама).
    if data == "menu":
        _api_edit(chat_id, message_id, "💰 Финбот — выберите раздел:", build_main_menu())
        return
    key = data[4:] if data.startswith("sec:") else data
    if key not in SECTION_KEYS:
        return
    text = SECTION_TITLES[key] + "\n\n" + section_body(key)
    _api_edit(chat_id, message_id, text, build_section_nav(key))


def _handle_text(text, chat_id):  # pragma: no cover
    text = (text or "").strip()
    if text.startswith("/start") or text.startswith("/menu"):
        send_message(chat_id, "Финбот. Выберите:", reply_markup=build_main_menu())
    elif text.startswith("/topup"):
        parts = text.split()
        if len(parts) >= 3:
            try:
                fin_balance.add_topup(DB, parts[1], float(parts[2]))
                send_message(chat_id, "✅ Пополнение учтено. " + balance_text())
            except Exception:
                send_message(chat_id, "Не понял сумму. Пример: /topup anthropic 50")
        else:
            send_message(chat_id, "Формат: /topup anthropic 50")


def _scheduler_loop():  # pragma: no cover
    while True:
        try:
            today = _today_iso()
            for it in fin_scheduler.due_today(DB, today, daily_cap_usd=(DAILY_CAP or None)):
                send_message(CHAT_ID, notification_text(it))
                fin_scheduler.mark_sent(DB, it["kind"], it["key"], today)
            fin_scheduler.roll_passed_subs(DB, today)
        except Exception:
            pass
        time.sleep(3600)


def main():  # pragma: no cover
    if not TOKEN:
        raise SystemExit("FINANCE_BOT_TOKEN не задан в env")
    threading.Thread(target=_scheduler_loop, name="fin-scheduler", daemon=True).start()
    send_message(CHAT_ID, "Финбот запущен.", reply_markup=build_main_menu())
    offset = None
    while True:
        resp = _api("getUpdates", offset=offset, timeout=50)
        if not resp or not resp.get("ok"):
            time.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            if "callback_query" in upd:
                cq = upd["callback_query"]
                _api("answerCallbackQuery", callback_query_id=cq["id"])
                _handle_callback(cq.get("data", ""), cq["message"]["chat"]["id"],
                                 cq["message"]["message_id"])
            elif "message" in upd and "text" in upd["message"]:
                _handle_text(upd["message"]["text"], upd["message"]["chat"]["id"])


if __name__ == "__main__":  # pragma: no cover
    main()

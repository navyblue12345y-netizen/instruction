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
import fin_sheets

TOKEN = os.environ.get("FINANCE_BOT_TOKEN", "")
CHAT_ID = os.environ.get("FINANCE_CHAT_ID", "-1004292579780")
DAILY_CAP = float(os.environ.get("FINANCE_DAILY_CAP", "0") or 0)
DAILY_REPORT_HOUR = int(os.environ.get("FINANCE_DAILY_REPORT_HOUR_UTC", "20") or 20)
DB = cost_log.FINANCE_DB
_API = "https://api.telegram.org/bot%s/%s"


def _parse_allowed(raw):
    """'111, 222,333' -> {111,222,333}. Пусто -> пустое множество (никого)."""
    out = set()
    for x in (raw or "").replace(" ", "").split(","):
        x = x.strip()
        if x and x.lstrip("-").isdigit():
            out.add(int(x))
    return out


ALLOWED_USERS = _parse_allowed(os.environ.get("FINANCE_ALLOWED_USERS", ""))
# ЛС-адресаты алертов (низкий баланс / напоминания об оплате) — личные сообщения руководителю(ям)
ALERT_DM_USERS = _parse_allowed(os.environ.get("FINANCE_ALERT_DM_USERS", ""))
# Google Sheets (живой реестр); если не заданы — лист просто не обновляется
GSHEETS_KEY_PATH = os.environ.get("GSHEETS_KEY_PATH", "")
GSHEETS_SHEET_ID = os.environ.get("GSHEETS_SHEET_ID", "")


def is_allowed(user_id):
    """Доступ к интерактиву (меню/отчёты/команды) — только у этих user_id в ЛС."""
    try:
        return int(user_id) in ALLOWED_USERS
    except Exception:
        return False


# типы уведомлений, дублируемые в ЛС руководителю (личные алерты)
DM_ALERT_KINDS = {"low_balance", "reminder", "fallback_active"}


def alert_recipients(kind, group_chat_id, dm_users):
    """Чаты для уведомления: всегда группа + (для алертов) ЛС руководителю(ям)."""
    out = [group_chat_id]
    if kind in DM_ALERT_KINDS:
        for u in dm_users:
            if u not in out:
                out.append(u)
    return out


def should_alert_low_balance(db_path, provider):
    """openai-fin-2026-08-18: alert tolko po zhivym provideram i tolko nizhe poroga."""
    if provider in getattr(fin_sheets, "FROZEN_PROVIDERS", set()):
        return False
    alias = fin_sheets.PROVIDER_ALIAS.get(provider, provider)
    return bool(fin_balance.is_low(db_path, provider, claude_alias=alias))


def low_balance_card_for(db_path, provider):
    """Карточка низкого баланса конкретного провайдера: остаток, ссылка, затронутые проекты, почта."""
    alias = fin_sheets.PROVIDER_ALIAS.get(provider, provider)
    rem = fin_balance.remaining(db_path, provider, claude_alias=alias)
    thr = fin_balance.threshold(db_path, provider)
    disp = fin_sheets.PROVIDER_DISPLAY.get(provider, provider)
    url = fin_sheets.TOPUP_URL.get(provider, "")
    projs = fin_sheets.provider_projects(db_path, alias)
    email = fin_sheets.account_email(db_path, provider)
    return fin_sheets.build_low_balance_card(disp, rem, thr, url, projs, email)


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


def build_section_nav(key=None):
    """Одна кнопка возврата в главное меню (действие, без карусели по разделам)."""
    return {"inline_keyboard": [[{"text": "◀ В меню", "callback_data": "menu"}]]}


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
    txt = "🔔 Через %d дн. оплата: %s — %s %s (%s)" % (
        days, sub.get("name"), _fmt_amt(sub.get("amount", 0)) if sub.get("amount") is not None else "?",
        sub.get("currency", ""), sub.get("next_due"))
    email = sub.get("account_email")
    if email:
        txt += "\nАккаунт: %s" % email
    return txt


# ---------------- db helpers ----------------
def load_subs(db_path=None):
    conn = sqlite3.connect(db_path or DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT name, category, amount, currency, next_due, notify_days_before, owner, account_email "
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


def build_balances_text(items):
    """items: (display_name, remaining_usd, threshold_usd[, frozen]).

    frozen-view-2026-08-18: у замороженного провайдера деньги на счёте есть, но
    воспользоваться ими нельзя (Anthropic 18.08 закрыл доступ проверкой личности),
    и пополнение тоже недоступно — поэтому «ПОРА ПОПОЛНИТЬ» там вводит в заблуждение.
    """
    if not items:
        return "💳 Балансы API: нет данных."
    out = ["💳 Балансы API:"]
    for it in items:
        name, rem, thr = it[0], it[1], it[2]
        frozen = it[3] if len(it) > 3 else False
        if frozen:
            note = "  ❄️ заморожен (доступ закрыт, пополнение недоступно)"
        elif rem < thr:
            note = "  ⚠️ ПОРА ПОПОЛНИТЬ"
        else:
            note = ""
        out.append("• %s: остаток ≈ $%.2f (порог $%.0f)%s" % (name, rem, thr, note))
    return "\n".join(out)


def balance_text():
    import sqlite3 as _sq
    _conn = _sq.connect(DB, timeout=10)
    try:
        provs = [r[0] for r in _conn.execute("SELECT provider FROM api_credits ORDER BY provider")]
    except Exception:
        provs = []
    finally:
        _conn.close()
    # openai-fin-2026-08-18: berem iz obshchego reestra, chtoby novy provider
    # (openai) ne trebovalos dobavlyat v dvuh mestah.
    _alias = dict(fin_sheets.PROVIDER_ALIAS)
    _disp = dict(fin_sheets.PROVIDER_DISPLAY)
    items = []
    for _p in provs:
        try:
            items.append((_disp.get(_p, _p),
                          fin_balance.remaining(DB, _p, claude_alias=_alias.get(_p, _p)),
                          fin_balance.threshold(DB, _p),
                          _p in getattr(fin_sheets, "FROZEN_PROVIDERS", set())))
        except Exception:
            pass
    if not items:
        return build_balance_text(fin_balance.remaining(DB, "anthropic"), fin_balance.threshold(DB, "anthropic"))
    return build_balances_text(items)


def notification_text(item):
    k = item["kind"]
    if k == "monthly":
        return "📊 Ежемесячный отчёт за %s:\n\n%s" % (item["month"], report_text(item["month"]))
    if k == "reminder":
        return build_reminder_text(item.get("sub", {}), item.get("days", 0))
    if k == "low_balance":
        return low_balance_card_for(DB, item.get("provider", "anthropic"))
    if k == "fallback_active":
        base = ("🔁 Основной Anthropic-ключ исчерпан — рерайт работает на РЕЗЕРВНОМ аккаунте.\n"
                "Пополни основной баланс. Если кончится и резерв — посты пойдут без ИИ-рерайта.")
        email = fin_sheets.account_email(DB, "anthropic")
        if email:
            base += "\nАккаунт (основной): %s" % email
        return base
    if k == "daily_cost":
        return "⚠️ Дневной расход LLM ${:.2f} превысил потолок ${:.2f}".format(item.get("spend", 0), item.get("cap", 0))
    if k == "daily_summary":
        return "📅 Расходы за день %s:\n\n%s" % (item.get("day"), report_text(item.get("day")))
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
        return ("➕ Пополнение — отправьте:\n"
                "•  /topup openai <сумма $>   (GPT-5.6 Luna — основной рерайт)\n"
                "•  /topup deepseek <сумма $>  (курация и модерация)\n\n"
                "Anthropic заморожен: 18.08 доступ закрыт проверкой личности "
                "при живом балансе, пополнение недоступно.")
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
    elif text.startswith("/sheet"):
        if GSHEETS_SHEET_ID and GSHEETS_KEY_PATH:
            try:
                res = fin_sheets.push(DB, GSHEETS_SHEET_ID, GSHEETS_KEY_PATH,
                                      period=_today_iso()[:7], updated_label=_msk_label())
                send_message(chat_id, "✅ Лист обновлён: %d аккаунтов, %d проектов." % (
                    res["registry_rows"], res["matrix_projects"]))
            except Exception as e:
                send_message(chat_id, "Ошибка обновления листа: %s" % str(e)[:140])
        else:
            send_message(chat_id, "Google Sheet не настроен (GSHEETS_SHEET_ID / GSHEETS_KEY_PATH).")


def _msk_label():  # pragma: no cover
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M МСК")


def _maybe_push_sheet():  # pragma: no cover
    """Обновить Google Sheet, если он настроен (раз в час из планировщика)."""
    if not (GSHEETS_SHEET_ID and GSHEETS_KEY_PATH):
        return
    try:
        fin_sheets.push(DB, GSHEETS_SHEET_ID, GSHEETS_KEY_PATH,
                        period=_today_iso()[:7], updated_label=_msk_label())
    except Exception:
        pass


def _scheduler_loop():  # pragma: no cover
    while True:
        try:
            today = _today_iso()
            now_h = datetime.now(timezone.utc).hour
            for it in fin_scheduler.due_today(DB, today, daily_cap_usd=(DAILY_CAP or None),
                                              now_utc_hour=now_h, daily_report_hour=DAILY_REPORT_HOUR):
                _text = notification_text(it)
                for _chat in alert_recipients(it["kind"], CHAT_ID, ALERT_DM_USERS):
                    send_message(_chat, _text)
                fin_scheduler.mark_sent(DB, it["kind"], it["key"], today)
            fin_scheduler.roll_passed_subs(DB, today)
            _maybe_push_sheet()
        except Exception:
            pass
        time.sleep(3600)


def _balance_sync_loop():  # pragma: no cover
    import fin_provider_balance
    while True:
        try:
            fin_provider_balance.sync_deepseek(DB)
        except Exception as e:
            print("balance-sync error:", e)
        time.sleep(1800)


def main():  # pragma: no cover
    if not TOKEN:
        raise SystemExit("FINANCE_BOT_TOKEN не задан в env")
    import fin_schema
    fin_schema.init_all()  # idempotent — применяет миграции схемы при старте
    threading.Thread(target=_scheduler_loop, name="fin-scheduler", daemon=True).start()
    _ingest_token = os.environ.get("INGEST_TOKEN", "").strip()
    if _ingest_token:
        import fin_ingest
        _iport = int(os.environ.get("INGEST_PORT", "8788") or 8788)
        threading.Thread(target=fin_ingest.serve_forever, args=(DB, _ingest_token),
                         kwargs={"port": _iport}, name="fin-ingest", daemon=True).start()
    threading.Thread(target=_balance_sync_loop, name="fin-balance-sync", daemon=True).start()
    # В группу стартовое меню НЕ шлём: группа = только уведомления.
    # Интерактив (меню/отчёты/команды) — только в ЛС у разрешённых юзеров.
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
                msg = cq.get("message") or {}
                chat = msg.get("chat") or {}
                uid = (cq.get("from") or {}).get("id")
                if chat.get("type") != "private" or not is_allowed(uid):
                    continue
                _handle_callback(cq.get("data", ""), chat.get("id"), msg.get("message_id"))
            elif "message" in upd and "text" in upd["message"]:
                m = upd["message"]
                chat = m.get("chat") or {}
                uid = (m.get("from") or {}).get("id")
                if chat.get("type") != "private":
                    continue  # в группах на команды/кнопки не реагируем
                if not is_allowed(uid):
                    send_message(chat.get("id"),
                                 "⛔ Нет доступа к финботу.\nВаш ID: %s\nПередайте его администратору." % uid)
                    continue
                _handle_text(m["text"], chat.get("id"))


if __name__ == "__main__":  # pragma: no cover
    main()

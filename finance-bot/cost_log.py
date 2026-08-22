"""Учёт LLM-rewrite расходов в общую finance.db. Best-effort — НЕ ломает rewrite.

Встраивается в processor/rewriter.py обоих сервисов (content-bot, content-bot-client).
Пишет одну строку на каждый провайдер-вызов: провайдер/модель/токены/стоимость/категория/purpose.
"""
import os
import re as _re
import sqlite3
import logging
import contextvars
from datetime import datetime, timezone

logger = logging.getLogger("cost_log")

FINANCE_DB = os.environ.get("FINANCE_DB", "/home/openclaw/.openclaw/workspace/finance.db")

# USD за 1M токенов. cache_write_x / cache_read_x — множители к input-цене.
PRICING = {
    ("claude", "claude-opus-4-7"): {"in": 5.0, "out": 25.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("claude", "claude-opus-4-6"): {"in": 5.0, "out": 25.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("claude", "claude-opus-4-5"): {"in": 5.0, "out": 25.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("claude", "claude-sonnet-4-6"): {"in": 3.0, "out": 15.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("claude", "claude-sonnet-4-5"): {"in": 3.0, "out": 15.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("claude", "claude-haiku-4-5"): {"in": 1.0, "out": 5.0, "cache_write_x": 1.25, "cache_read_x": 0.10},
    ("groq", "llama-3.3-70b-versatile"): {"in": 0.59, "out": 0.79, "cache_write_x": 0.0, "cache_read_x": 0.0},
    ("deepseek", "deepseek-chat"): {"in": 0.14, "out": 0.28, "cache_write_x": 0.0, "cache_read_x": 0.02},
}
DEFAULT_PRICE = {"in": 1.0, "out": 5.0, "cache_write_x": 1.25, "cache_read_x": 0.10}


def _normalize_model(model: str) -> str:
    """Отрезает дата-суффикс (-YYYYMMDD / -YYMMDD) у имени модели для поиска цены."""
    if not model:
        return model or ""
    return _re.sub(r"-\d{6,8}$", "", model)


# дача/вязание (legacy ЖЦА) — семейства каналов из config.yaml content-bot (Task 1.4).
DACHA_KEYS = {"dachnyj_ugolok_3", "skhemy_vyazaniya", "skhemy_vyazaniya_rukodelie_2"}
DACHA_PREFIXES = ("dachnyj", "skhemy_vyazaniya", "dacha", "vyazanie",
                  "nash_dom", "masterskaya_vyaza", "dachniki")


def classify(channel_key: str) -> str:
    """channel_key -> network_cat: raiony | v_max | zhca | lajv | other."""
    k = (channel_key or "").lower()
    if not k:
        return "other"
    if k.startswith("client_"):
        return "raiony"
    if "_v_max" in k:
        return "v_max"
    if k in DACHA_KEYS or k.startswith(DACHA_PREFIXES):
        return "zhca"
    return "lajv"  # _lajv, _lajv_*, _laj, naberezhnye_cheln, rostov_na_donu_2, ...


def cost_usd(provider, model, in_tok, out_tok, cache_write=0, cache_read=0) -> float:
    """Стоимость одного вызова в USD.

    ВАЖНО: у Anthropic `input_tokens` уже БЕЗ кэш-токенов — cache_creation /
    cache_read учитываются ОТДЕЛЬНО (cache_write ×1.25, cache_read ×0.1 от input-цены).
    Поэтому НЕ вычитаем cache из in_tok. У groq/openai/deepseek cache=0.
    """
    model = _normalize_model(model)
    p = PRICING.get((provider, model))
    if p is None:
        logger.warning("cost_log: unknown model pricing %s/%s -> DEFAULT", provider, model)
        p = DEFAULT_PRICE
    in_tok = in_tok or 0
    out_tok = out_tok or 0
    cache_write = cache_write or 0
    cache_read = cache_read or 0
    return (in_tok * p["in"]
            + cache_write * p["in"] * p["cache_write_x"]
            + cache_read * p["in"] * p["cache_read_x"]
            + out_tok * p["out"]) / 1e6


MODEL_BY_PROVIDER = {
    "claude": "claude-haiku-4-5",
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
}

# --- channel context (для авто-атрибуции вызовов прокси-клиента) ---
_CURRENT_CHANNEL = contextvars.ContextVar("cost_log_channel", default=None)


def set_channel(channel_key):
    """Ставит текущий канал для последующих LLM-вызовов в этом контексте."""
    _CURRENT_CHANNEL.set(channel_key)


def get_channel():
    try:
        return _CURRENT_CHANNEL.get()
    except Exception:
        return None


def _extract_usage(provider: str, response):
    """Достаёт токены из ответа провайдера. None если usage недоступен."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    if provider == "claude":
        return {
            "in_tok": getattr(u, "input_tokens", 0) or 0,
            "out_tok": getattr(u, "output_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
    # openai-style (groq / openai / deepseek)
    return {
        "in_tok": getattr(u, "prompt_tokens", 0) or 0,
        "out_tok": getattr(u, "completion_tokens", 0) or 0,
        "cache_write": 0,
        "cache_read": 0,
    }


def record_from_response(service, channel, provider, response, purpose="rewrite"):
    """Best-effort: извлечь usage/model из ответа и записать. Никогда не бросает."""
    try:
        model = getattr(response, "model", None) or MODEL_BY_PROVIDER.get(provider, provider)
        u = _extract_usage(provider, response)
        if not u:
            return
        record(service=service, channel_key=channel, provider=provider, model=model,
               in_tok=u["in_tok"], out_tok=u["out_tok"],
               cache_write=u["cache_write"], cache_read=u["cache_read"], purpose=purpose)
    except Exception as e:
        logger.warning("cost_log.record_from_response failed: %s: %s", type(e).__name__, str(e)[:120])


def init_schema():
    conn = sqlite3.connect(FINANCE_DB, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
                service TEXT NOT NULL, channel_key TEXT, network_cat TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL,
                in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
                cache_write_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
                cost_usd REAL NOT NULL, purpose TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts_utc)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_cat ON llm_usage(network_cat, ts_utc)")
        # миграция старой таблицы без purpose
        try:
            conn.execute("ALTER TABLE llm_usage ADD COLUMN purpose TEXT")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def record(service, channel_key, provider, model, in_tok, out_tok, cache_write=0, cache_read=0, purpose="rewrite"):
    """Best-effort запись одного вызова. Любая ошибка проглатывается (учёт не ломает rewrite)."""
    try:
        cat = classify(channel_key)
        c = cost_usd(provider, model, in_tok, out_tok, cache_write, cache_read)
        conn = sqlite3.connect(FINANCE_DB, timeout=10)
        try:
            conn.execute(
                "INSERT INTO llm_usage (ts_utc, service, channel_key, network_cat, provider, model,"
                " in_tokens, out_tokens, cache_write_tokens, cache_read_tokens, cost_usd, purpose)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), service, channel_key, cat, provider, model,
                 in_tok or 0, out_tok or 0, cache_write or 0, cache_read or 0, c, purpose))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("cost_log.record failed: %s: %s", type(e).__name__, str(e)[:120])


# === Tracked client proxy: ВСЕ LLM-вызовы через него считаются автоматически ===

class _CreateProxy:
    """Обёртка над real .create — после ответа пишет расход (best-effort, не ломает вызов)."""
    def __init__(self, real_create, service, provider, purpose):
        self._create = real_create
        self._svc = service
        self._provider = provider
        self._purpose = purpose

    def create(self, *a, **kw):
        resp = self._create(*a, **kw)  # реальный вызов — ошибки пробрасываем как есть
        try:
            record_from_response(self._svc, get_channel(), self._provider, resp, purpose=self._purpose)
        except Exception as e:
            logger.warning("cost_log proxy record failed: %s", e)
        return resp


class _ChatCompletionsHolder:
    """Промежуточный объект для openai/groq: client.chat.completions.create."""
    def __init__(self, real_chat, service, provider, purpose):
        self._real_chat = real_chat
        self._svc = service
        self._provider = provider
        self._purpose = purpose

    @property
    def completions(self):
        return _CreateProxy(self._real_chat.completions.create, self._svc, self._provider, self._purpose)


class CostTrackedClient:
    """Тонкий прокси над Anthropic/Groq/OpenAI клиентом. Делегирует всё,
    но перехватывает .messages.create / .chat.completions.create -> запись в llm_usage."""
    def __init__(self, real, service, provider, purpose="rewrite"):
        self._real = real
        self._svc = service
        self._provider = provider
        self._purpose = purpose

    @property
    def messages(self):  # anthropic
        return _CreateProxy(self._real.messages.create, self._svc, self._provider, self._purpose)

    @property
    def chat(self):  # openai / groq
        return _ChatCompletionsHolder(self._real.chat, self._svc, self._provider, self._purpose)

    def __getattr__(self, name):
        # вызывается только для отсутствующих атрибутов -> делегируем реальному клиенту
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["_real"], name)


def _build_raw_client(provider, *, api_key=None):
    if provider == "claude":
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    if provider == "groq":
        from groq import Groq
        return Groq(api_key=api_key)
    if provider == "openai":
        import openai
        return openai.OpenAI(api_key=api_key)
    raise ValueError("unknown provider for make_client: %s" % provider)


def make_client(provider, *, service, purpose, api_key=None):
    """Возвращает CostTrackedClient — реальный клиент + авто-учёт расхода каждого вызова."""
    raw = _build_raw_client(provider, api_key=api_key)
    return CostTrackedClient(raw, service, provider, purpose)

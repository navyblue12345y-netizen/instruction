"""
Рерайт текста через Groq API.
Генерирует короткую подпись со смайликом для всех каналов.
"""
import logging
import re
import time

logger = logging.getLogger(__name__)

CHANNEL_TOPICS = {
    "moda": "мода, стиль, одежда, образы",
    "vyazanie": "вязание, крючок, спицы, рукоделие",
    "dom": "дом, интерьер, уют, уборка, лайфхаки для дома",
}

CHANNEL_EMOJIS = {
    "moda": "👗",
    "dom": "🏠",
    "vyazanie": "🧶",
}

CAPTION_PROMPT = """Ты редактор женского канала. Напиши ОДНУ вдохновляющую подпись к посту.

ПРАВИЛА:
1. Одно предложение, максимум 10 слов
2. Начни с подходящего смайлика
3. Пиши о теме поста позитивно и вдохновляюще
4. Без хештегов, без ссылок, без упоминаний каналов
5. НИКОГДА не пиши про "ошибку", "блокировку", "проблему" — только позитив
6. Если текст непонятный — напиши красивую общую фразу по теме канала

Тема канала: {topic}
Оригинальный текст: {text}

Примеры формата:
😍 1 платье и 4 образа на любой случай.
🏠 Как спрятать электрощиток в прихожей.
✨ Трансформация старой кровати за один день.
🧶 Ажурный узор крючком для летней шали.
🌿 Эластичный набор петель — просто и красиво.

Напиши только подпись:"""


def rewrite(text: str, channel: str, client, retries: int = 3,
            media_type: str = None) -> str:
    """
    Генерирует короткую подпись через Groq.
    При ошибке возвращает fallback.
    """
    topic = CHANNEL_TOPICS.get(channel, "интересный контент")
    prompt = CAPTION_PROMPT.format(topic=topic, text=(text or "")[:300])

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=60,
                temperature=0.8,
            )
            result = response.choices[0].message.content.strip().strip('"\'')
            # Убираем хэштеги если Groq их добавил
            result = re.sub(r'#\S+', '', result).strip()
            if result:
                logger.info(f"[{channel}] Groq caption: {result}")
                return result
        except Exception as e:
            err = str(e)
            logger.warning(f"Groq ошибка (попытка {attempt+1}/{retries}): {err[:100]}")
            if "rate_limit_exceeded" in err:
                time.sleep(30)
            elif attempt < retries - 1:
                time.sleep(2)

    return _fallback_caption(text, channel)


def _fallback_caption(text: str, channel: str) -> str:
    """Короткая подпись без AI — первое предложение + смайлик ниши."""
    emoji = CHANNEL_EMOJIS.get(channel, "✨")
    if not text:
        return emoji
    m = re.match(r'^(.{10,80}?[.!?])', text.strip())
    if m:
        return f"{emoji} {m.group(1).strip()}"
    short = text.strip()[:80].rsplit(' ', 1)[0]
    return f"{emoji} {short}" if short else emoji

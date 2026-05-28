# ============================================================
# AI_HELPER.PY — общение с нейросетью Groq
# ============================================================

import os
import logging
import json
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ------------------------------------------------------------
# СТРОИМ СИСТЕМНЫЙ ПРОМПТ ДИНАМИЧЕСКИ
# Каждый раз когда бот отвечает — он получает свежий контекст:
# профиль, цели, долги, статистику
# ------------------------------------------------------------
def build_system_prompt(profile: dict, stats: dict, monthly: dict, goals: list, debts: list) -> str:

    # Профиль
    profile_text = "Информация о пользователе:\n"
    if profile:
        for key, value in profile.items():
            profile_text += f"  - {key}: {value}\n"
    else:
        profile_text += "  - Пока не заполнен\n"

    # Финансовая сводка
    finance_text = "Финансовая сводка:\n"
    if stats:
        finance_text += (
            f"  - Всего доходов: {stats.get('income', 0):,.0f}\n"
            f"  - Всего расходов: {stats.get('expense', 0):,.0f}\n"
            f"  - Общий баланс: {stats.get('balance', 0):,.0f}\n"
        )
    if monthly:
        finance_text += (
            f"  - Доходы за {monthly.get('month', 'месяц')}: {monthly.get('income', 0):,.0f}\n"
            f"  - Расходы за {monthly.get('month', 'месяц')}: {monthly.get('expense', 0):,.0f}\n"
        )

    # Цели
    goals_text = "Финансовые цели:\n"
    if goals:
        for g in goals:
            progress = 0
            if g.get("target_amount") and g["target_amount"] > 0:
                progress = (g.get("saved_amount", 0) / g["target_amount"]) * 100
            deadline = f", дедлайн: {g['deadline']}" if g.get("deadline") else ""
            goals_text += (
                f"  - {g['title']}: накоплено {g.get('saved_amount', 0):,.0f} "
                f"из {g.get('target_amount', 0):,.0f} ({progress:.0f}%){deadline}\n"
            )
    else:
        goals_text += "  - Целей пока нет\n"

    # Долги
    debts_text = "Долги:\n"
    i_owe = [d for d in debts if d["direction"] == "i_owe"]
    owe_me = [d for d in debts if d["direction"] == "owe_me"]

    if i_owe:
        debts_text += "  Я должен:\n"
        for d in i_owe:
            due = f", до {d['due_date']}" if d.get("due_date") else ""
            debts_text += f"    - {d['person']}: {d['amount']:,.0f} ({d.get('description', '')}){due}\n"
    if owe_me:
        debts_text += "  Должны мне:\n"
        for d in owe_me:
            due = f", до {d['due_date']}" if d.get("due_date") else ""
            debts_text += f"    - {d['person']}: {d['amount']:,.0f} ({d.get('description', '')}){due}\n"
    if not i_owe and not owe_me:
        debts_text += "  - Долгов нет\n"

    return f"""Ты — личный финансовый помощник в Telegram. Умный, дружелюбный, говоришь только по-русски.

{profile_text}
{finance_text}
{goals_text}
{debts_text}

ТВОИ ЗАДАЧИ:
1. Вести финансовый учёт — записывать доходы и расходы
2. Помнить контекст разговора и всё что знаешь о пользователе
3. Давать персональные советы на основе реальных данных выше
4. Помогать с целями и долгами
5. Если узнаёшь новое о пользователе — запоминать это

ПРАВИЛА:
- Всегда обращайся по имени если знаешь его
- Отвечай кратко — 2-4 предложения если не просят подробнее
- Опирайся на реальные данные выше, не выдумывай
- Если видишь финансовую операцию в тексте — скажи что записал
- Если пользователь рассказывает о себе (имя, работа, город, цели) — 
  обязательно скажи что запомнил это"""

# ------------------------------------------------------------
# РАСПОЗНАТЬ ФИНАНСОВЫЕ ОПЕРАЦИИ ИЗ ТЕКСТА
# ------------------------------------------------------------
def parse_transaction(text: str) -> list | None:
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": """Ты извлекаешь финансовые операции из текста.
Верни ТОЛЬКО валидный JSON-массив без пояснений, даже если операция одна:
[{"type": "expense" или "income", "amount": число, "category": "категория", "description": "описание"}]

Категории расходов: еда, транспорт, жильё, здоровье, развлечения, одежда, техника, другое
Категории доходов: зарплата, фриланс, подарок, другое

Если операций нет — верни: []

Примеры:
"потратил 500 на обед" → [{"type": "expense", "amount": 500, "category": "еда", "description": "обед"}]
"купил колбасы за 2000 и подписку за 500" → [{"type": "expense", "amount": 2000, "category": "еда", "description": "колбасы"}, {"type": "expense", "amount": 500, "category": "развлечения", "description": "подписка"}]
"нашёл 100 рублей" → [{"type": "income", "amount": 100, "category": "другое", "description": "нашёл деньги"}]
"как дела" → []
"меня зовут Никита" → []"""
                },
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=300,
        )

        raw = response.choices[0].message.content.strip()
        logger.info(f"🤖 ИИ распознал транзакции: {raw}")

        data = json.loads(raw)
        return data if data else None

    except json.JSONDecodeError:
        logger.error(f"❌ ИИ вернул не JSON: {raw}")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка parse_transaction: {e}")
        return None

# ------------------------------------------------------------
# ИЗВЛЕЧЬ ИНФОРМАЦИЮ О ПОЛЬЗОВАТЕЛЕ ИЗ ТЕКСТА
# Если человек говорит "меня зовут Никита" — запоминаем
# ------------------------------------------------------------
def extract_profile_info(text: str) -> dict | None:
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": """Извлеки личную информацию о пользователе из текста.
Верни ТОЛЬКО валидный JSON или пустой объект {}.

Возможные ключи:
- name (имя)
- city (город)
- job (профессия/работа)
- age (возраст)
- income_source (источник дохода)

Примеры:
"меня зовут Никита" → {"name": "Никита"}
"я живу в Москве и работаю дизайнером" → {"city": "Москва", "job": "дизайнер"}
"мне 28 лет" → {"age": "28"}
"потратил 500 на еду" → {}
"как дела" → {}"""
                },
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=150,
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return data if data else None

    except Exception as e:
        logger.error(f"❌ Ошибка extract_profile_info: {e}")
        return None

# ------------------------------------------------------------
# ГЛАВНАЯ ФУНКЦИЯ ОТВЕТА — с полным контекстом
# ------------------------------------------------------------
def chat_response(
    user_message: str,
    history: list,
    profile: dict,
    stats: dict,
    monthly: dict,
    goals: list,
    debts: list,
) -> str:
    try:
        system_prompt = build_system_prompt(profile, stats, monthly, goals, debts)

        full_messages = (
            [{"role": "system", "content": system_prompt}]
            + history
            + [{"role": "user", "content": user_message}]
        )

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=full_messages,
            temperature=0.7,
            max_tokens=500,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"❌ Ошибка chat_response: {e}")
        return "Произошла ошибка. Попробуй ещё раз."

# ------------------------------------------------------------
# ФИНАНСОВЫЙ СОВЕТ
# ------------------------------------------------------------
def get_financial_advice(profile: dict, stats: dict, monthly: dict, categories: dict, goals: list, debts: list) -> str:
    try:
        system_prompt = build_system_prompt(profile, stats, monthly, goals, debts)

        cats_text = "\nРасходы по категориям:\n"
        for cat, amount in categories.items():
            cats_text += f"  - {cat}: {amount:,.0f}\n"

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"{cats_text}\nДай персональный финансовый совет. "
                               f"Учти мои цели и долги если они есть. "
                               f"Укажи на главную проблему или что делаю хорошо. "
                               f"Максимум 5 предложений."
                }
            ],
            temperature=0.7,
            max_tokens=400,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        logger.error(f"❌ Ошибка get_financial_advice: {e}")
        return "Не удалось получить совет. Попробуй позже."

# ------------------------------------------------------------
# РАСПОЗНАТЬ ГОЛОСОВОЕ СООБЩЕНИЕ
# ------------------------------------------------------------
def transcribe_voice(audio_path: str) -> str | None:
    try:
        with open(audio_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=audio_file,
                language="ru",
            )
        text = transcription.text.strip()
        logger.info(f"🎤 Распознано: {text}")
        return text
    except Exception as e:
        logger.error(f"❌ Ошибка транскрипции: {e}")
        return None
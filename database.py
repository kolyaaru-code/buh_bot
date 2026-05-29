# ============================================================
# DATABASE.PY — всё общение с базой данных
# Версия 2: часовой пояс Екатеринбург (UTC+5), русские месяцы,
# поиск цели по названию для переспроса.
# ============================================================

import os
import logging
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# ЧАСОВОЙ ПОЯС — Екатеринбург, UTC+5
# Сервер живёт по UTC (или по своему времени), а нам нужно
# считать "месяц" и показывать даты по местному времени.
# ------------------------------------------------------------
LOCAL_TZ = timezone(timedelta(hours=5))

RU_MONTHS = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

def _now_local() -> datetime:
    """Текущее время в Екатеринбурге."""
    return datetime.now(LOCAL_TZ)

# ------------------------------------------------------------
# ПОДКЛЮЧЕНИЕ К SUPABASE
# ------------------------------------------------------------
def get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("❌ SUPABASE_URL или SUPABASE_KEY не найдены в .env!")
    return create_client(url, key)

# ============================================================
# ТРАНЗАКЦИИ
# ============================================================

def save_transaction(type_op: str, amount: float, category: str, description: str) -> bool:
    try:
        db = get_client()
        db.table("transactions").insert({
            "type":        type_op,
            "amount":      amount,
            "category":    category,
            "description": description,
        }).execute()
        logger.info(f"💾 Сохранено: {type_op} | {amount} | {category} | {description}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")
        return False

def get_stats() -> dict:
    try:
        db = get_client()
        result = db.table("transactions").select("type, amount").execute()
        rows = result.data
        total_income  = sum((r["amount"] or 0) for r in rows if r["type"] == "income")
        total_expense = sum((r["amount"] or 0) for r in rows if r["type"] == "expense")
        return {
            "income":  total_income,
            "expense": total_expense,
            "balance": total_income - total_expense,
            "count":   len(rows),
        }
    except Exception as e:
        logger.error(f"❌ Ошибка получения статистики: {e}")
        return None

def get_last_transactions(limit: int = 5) -> list:
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("type, amount, category, description, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории: {e}")
        return []

def get_expenses_by_category() -> dict:
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("category, amount")
            .eq("type", "expense")
            .execute()
        )
        rows = result.data
        categories = {}
        for row in rows:
            cat = row["category"] or "Без категории"
            categories[cat] = categories.get(cat, 0) + (row["amount"] or 0)
        return dict(sorted(categories.items(), key=lambda x: x[1], reverse=True))
    except Exception as e:
        logger.error(f"❌ Ошибка получения категорий: {e}")
        return {}

def get_monthly_stats() -> dict:
    """Статистика за текущий месяц по местному времени (Екатеринбург)."""
    try:
        db = get_client()
        now = _now_local()
        # Первый день текущего месяца, 00:00 по местному времени.
        # Переводим в UTC-формат с зоной — Supabase хранит время в UTC.
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_start_iso = month_start.isoformat()

        result = (
            db.table("transactions")
            .select("type, amount")
            .gte("created_at", month_start_iso)
            .execute()
        )
        rows = result.data
        total_income  = sum((r["amount"] or 0) for r in rows if r["type"] == "income")
        total_expense = sum((r["amount"] or 0) for r in rows if r["type"] == "expense")
        return {
            "income":  total_income,
            "expense": total_expense,
            "balance": total_income - total_expense,
            "count":   len(rows),
            "month":   f"{RU_MONTHS[now.month]} {now.year}",
        }
    except Exception as e:
        logger.error(f"❌ Ошибка месячной статистики: {e}")
        return None

# ============================================================
# ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ
# Хранит пары ключ-значение: "name" → "Никита"
# ============================================================

def get_profile() -> dict:
    """Получить весь профиль как словарь"""
    try:
        db = get_client()
        result = db.table("user_profile").select("key, value").execute()
        return {row["key"]: row["value"] for row in result.data}
    except Exception as e:
        logger.error(f"❌ Ошибка получения профиля: {e}")
        return {}

def set_profile(key: str, value: str) -> bool:
    """Сохранить или обновить поле профиля"""
    try:
        db = get_client()
        # upsert = обнови если есть, создай если нет
        db.table("user_profile").upsert({
            "key":   key,
            "value": value,
        }, on_conflict="key").execute()
        logger.info(f"👤 Профиль обновлён: {key} = {value}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения профиля: {e}")
        return False

# ============================================================
# ЦЕЛИ
# ============================================================

def get_goals(status: str = "active") -> list:
    try:
        db = get_client()
        result = (
            db.table("goals")
            .select("*")
            .eq("status", status)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения целей: {e}")
        return []

def find_goal_by_title(title: str, status: str = "active") -> dict | None:
    """
    Ищет активную цель с похожим названием (без учёта регистра и пробелов).
    Возвращает первую совпавшую цель или None.
    Используется для переспроса 'такая цель уже есть'.
    """
    try:
        needle = title.strip().lower().replace(" ", "")
        if not needle:
            return None
        for g in get_goals(status):
            existing = (g.get("title") or "").strip().lower().replace(" ", "")
            if existing == needle:
                return g
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка поиска цели: {e}")
        return None

def add_goal(title: str, target_amount: float, deadline: str = None) -> int | None:
    """Добавляет цель. Возвращает id созданной цели или None при ошибке."""
    try:
        db = get_client()
        data = {"title": title, "target_amount": target_amount}
        if deadline:
            data["deadline"] = deadline
        result = db.table("goals").insert(data).execute()
        logger.info(f"🎯 Цель добавлена: {title} | {target_amount}")
        if result.data and len(result.data) > 0:
            return result.data[0].get("id")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка добавления цели: {e}")
        return None

def update_goal_progress(goal_id: int, saved_amount: float) -> bool:
    try:
        db = get_client()
        db.table("goals").update({"saved_amount": saved_amount}).eq("id", goal_id).execute()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка обновления цели: {e}")
        return False

# ============================================================
# ДОЛГИ
# ============================================================

def get_debts(status: str = "active") -> list:
    try:
        db = get_client()
        result = (
            db.table("debts")
            .select("*")
            .eq("status", status)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения долгов: {e}")
        return []

def add_debt(direction: str, person: str, amount: float, description: str = "", due_date: str = None) -> bool:
    try:
        db = get_client()
        data = {
            "direction":   direction,
            "person":      person,
            "amount":      amount,
            "description": description,
        }
        if due_date:
            data["due_date"] = due_date
        db.table("debts").insert(data).execute()
        logger.info(f"💸 Долг добавлен: {direction} | {person} | {amount}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка добавления долга: {e}")
        return False

def close_debt(debt_id: int) -> bool:
    try:
        db = get_client()
        db.table("debts").update({"status": "paid"}).eq("id", debt_id).execute()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия долга: {e}")
        return False

# ============================================================
# ИСТОРИЯ ДИАЛОГА — хранится в базе, не в памяти!
# ============================================================

def save_message(role: str, content: str) -> bool:
    """Сохранить сообщение в историю"""
    try:
        db = get_client()
        db.table("chat_history").insert({
            "role":    role,
            "content": content,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сообщения: {e}")
        return False

def get_chat_history(limit: int = 20) -> list:
    """Получить последние N сообщений для контекста"""
    try:
        db = get_client()
        result = (
            db.table("chat_history")
            .select("role, content")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        # Разворачиваем — нам нужен хронологический порядок
        messages = result.data[::-1]
        return messages
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории диалога: {e}")
        return []

def clear_chat_history() -> bool:
    """Очистить историю диалога (удаляет все строки таблицы)."""
    try:
        db = get_client()
        # gte по created_at от 'начала времён' = удалить все строки.
        # Надёжнее, чем neq id 0, и не зависит от типа id.
        db.table("chat_history").delete().gte("created_at", "1970-01-01").execute()
        logger.info("🧹 История диалога очищена")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")
        return False
# ============================================================
# DATABASE.PY — всё общение с базой данных
# Версия 3.1 (Этап A): модель ЦЕЛЕЙ = «резерв».
#   • вклад в цель журналируется маркерной транзакцией (К9)
#   • жизненный цикл цели: complete (покупкой) / withdraw (без траты) (К8)
#   • защита от каскадной отмены долга (К10)
#   • hard_reset_all() для команды /reset
#
# МОДЕЛЬ «РЕЗЕРВ» (важно понимать всю математику):
#   Баланс — это ВСЕ живые деньги. Накопления на цель (saved_amount)
#   ФИЗИЧЕСКИ ЛЕЖАТ ВНУТРИ баланса — это не отдельный кошелёк, а лишь
#   виртуальная пометка «эти деньги я мысленно отложил».
#   Поэтому:
#     • вклад в цель      → баланс НЕ меняется, растёт только saved_amount;
#                           «свободно» = баланс − сумма всех saved_amount.
#     • покупка цели      → обычный расход (баланс падает) + снимаем резерв
#                           (saved_amount уменьшается) — атомарно, без задвоения.
#     • закрытие без траты → просто снять пометку (saved_amount → 0),
#                           баланс не трогаем, деньги «сами» станут свободными.
# ============================================================

import os
import logging
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# МЕТКИ-КАТЕГОРИИ для долговых движений.
# Любое движение долга порождает транзакцию с одной из этих
# категорий — так баланс отражает живые деньги, а в /categories
# мы их исключаем, чтобы не мешались с тратами на еду и т.п.
# ------------------------------------------------------------
DEBT_CATEGORIES = {
    "выдача долга",     # дал в долг → расход
    "возврат долга",    # мне вернули → доход
    "получен заём",     # взял в долг → доход
    "погашение долга",  # я вернул/погасил → расход
}

# ------------------------------------------------------------
# МЕТКА-КАТЕГОРИЯ для вклада в цель (модель «резерв», К9).
# Вклад в копилку — это НЕ трата: живые деньги никуда не ушли,
# они просто «помечены» как отложенные. Поэтому такая транзакция
# ДОЛЖНА исключаться из баланса, месячной статистики и категорий.
# Нужна она только чтобы вклад был ВИДЕН в истории и его можно
# было ОТМЕНИТЬ (раньше saved_amount менялся без следа).
# ------------------------------------------------------------
GOAL_DEPOSIT_CATEGORY = "в копилку"
GOAL_CATEGORIES = {GOAL_DEPOSIT_CATEGORY}

# Всё, что НЕ считается живым доходом/расходом: долговые движения + копилка.
# Единый фильтр для баланса и статистики.
NON_CASH_CATEGORIES = DEBT_CATEGORIES | GOAL_CATEGORIES

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

def save_transaction(tg_id: int, type_op: str, amount: float, category: str, description: str,
                     debt_id: int = None, goal_id: int = None) -> int | None:
    """Сохраняет транзакцию. Возвращает id созданной записи или None при ошибке."""
    try:
        db = get_client()
        row = {
            "user_tg_id":  tg_id,
            "type":        type_op,
            "amount":      amount,
            "category":    category,
            "description": description,
        }
        if debt_id is not None:
            row["debt_id"] = debt_id
        if goal_id is not None:
            row["goal_id"] = goal_id
        result = db.table("transactions").insert(row).execute()
        logger.info(f"💾 Сохранено: {type_op} | {amount} | {category} | {description}")
        if result.data and len(result.data) > 0:
            return result.data[0].get("id")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения: {e}")
        return None

def delete_transaction(tx_id: int) -> bool:
    """Удаляет транзакцию по id (для отмены операции)."""
    try:
        db = get_client()
        db.table("transactions").delete().eq("id", tx_id).execute()
        logger.info(f"🗑️ Транзакция удалена: id={tx_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка удаления транзакции: {e}")
        return False

def get_transaction(tx_id: int) -> dict | None:
    """Получить одну транзакцию по id."""
    try:
        db = get_client()
        result = db.table("transactions").select("*").eq("id", tx_id).execute()
        if result.data and len(result.data) > 0:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка получения транзакции: {e}")
        return None

def get_recent_transactions_full(tg_id: int, limit: int = 10) -> list:
    """Последние транзакции со всеми полями (id, debt_id, goal_id) — для поиска при отмене."""
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("*")
            .eq("user_tg_id", tg_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения последних транзакций: {e}")
        return []

def get_stats(tg_id: int) -> dict:
    """
    Общая статистика по ЖИВЫМ деньгам.
    Вклады в копилку (GOAL_CATEGORIES) исключаем: это не трата и не доход,
    деньги остаются в балансе, просто помечены как отложенные.
    Долговые движения В балансе УЧАСТВУЮТ (это реальное движение денег),
    поэтому здесь их НЕ исключаем — только копилку.
    """
    try:
        db = get_client()
        result = db.table("transactions").select("type, amount, category").eq("user_tg_id", tg_id).execute()
        rows = result.data
        total_income = sum(
            (r["amount"] or 0) for r in rows
            if r["type"] == "income" and (r.get("category") not in GOAL_CATEGORIES)
        )
        total_expense = sum(
            (r["amount"] or 0) for r in rows
            if r["type"] == "expense" and (r.get("category") not in GOAL_CATEGORIES)
        )
        # count — только «живые» операции, чтобы служебные вклады не мозолили глаза
        count = sum(1 for r in rows if r.get("category") not in GOAL_CATEGORIES)
        return {
            "income":  total_income,
            "expense": total_expense,
            "balance": total_income - total_expense,
            "count":   count,
        }
    except Exception as e:
        logger.error(f"❌ Ошибка получения статистики: {e}")
        return None

def get_last_transactions(tg_id: int, limit: int = 5) -> list:
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("type, amount, category, description, created_at")
            .eq("user_tg_id", tg_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения истории: {e}")
        return []

def get_expenses_by_category(tg_id: int) -> dict:
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("category, amount")
            .eq("user_tg_id", tg_id)
            .eq("type", "expense")
            .execute()
        )
        rows = result.data
        categories = {}
        for row in rows:
            cat = row["category"] or "Без категории"
            # долговые движения и вклады в копилку не показываем среди обычных трат
            if cat in NON_CASH_CATEGORIES:
                continue
            categories[cat] = categories.get(cat, 0) + (row["amount"] or 0)
        return dict(sorted(categories.items(), key=lambda x: x[1], reverse=True))
    except Exception as e:
        logger.error(f"❌ Ошибка получения категорий: {e}")
        return {}

def get_monthly_stats(tg_id: int) -> dict:
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
            .select("type, amount, category")
            .eq("user_tg_id", tg_id)
            .gte("created_at", month_start_iso)
            .execute()
        )
        rows = result.data
        # Вклады в копилку исключаем (не живые деньги), долги учитываем.
        total_income = sum(
            (r["amount"] or 0) for r in rows
            if r["type"] == "income" and (r.get("category") not in GOAL_CATEGORIES)
        )
        total_expense = sum(
            (r["amount"] or 0) for r in rows
            if r["type"] == "expense" and (r.get("category") not in GOAL_CATEGORIES)
        )
        count = sum(1 for r in rows if r.get("category") not in GOAL_CATEGORIES)
        return {
            "income":  total_income,
            "expense": total_expense,
            "balance": total_income - total_expense,
            "count":   count,
            "month":   f"{RU_MONTHS[now.month]} {now.year}",
        }
    except Exception as e:
        logger.error(f"❌ Ошибка месячной статистики: {e}")
        return None

# ============================================================
# ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ
# Хранит пары ключ-значение: "name" → "Никита"
# ============================================================

def get_profile(tg_id: int) -> dict:
    """Получить весь профиль как словарь"""
    try:
        db = get_client()
        result = db.table("user_profile").select("key, value").eq("user_tg_id", tg_id).execute()
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
# ЦЕЛИ (модель «резерв»)
# ============================================================

def get_goals(tg_id: int, status: str = "active") -> list:
    try:
        db = get_client()
        result = (
            db.table("goals")
            .select("*")
            .eq("user_tg_id", tg_id)
            .eq("status", status)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения целей: {e}")
        return []

def get_goal(goal_id: int) -> dict | None:
    """Получить одну цель по id (в любом статусе)."""
    try:
        db = get_client()
        result = db.table("goals").select("*").eq("id", goal_id).execute()
        if result.data and len(result.data) > 0:
            return result.data[0]
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка получения цели: {e}")
        return None

# ------------------------------------------------------------
# НОРМАЛИЗАЦИЯ НАЗВАНИЙ ЦЕЛЕЙ для устойчивого матчинга.
# Проблема: голосом говорят «макбук» (кириллица), а цель названа
# «MacBook» (латиница) — побуквенно это разные строки. Решение:
# приводим обе строки к единому «звуковому» виду через транслитерацию
# кириллицы в латиницу, убираем регистр/пробелы/дефисы. Тогда
# «макбук» и «MacBook» → оба «makbuk»/«macbook» и матчатся.
# ------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

def _normalize_title(s: str) -> str:
    """Приводит название к сравнимому виду: нижний регистр, транслит кириллицы,
    без пробелов/дефисов. 'MacBook' и 'Мак бук' → сравнимы."""
    s = (s or "").strip().lower()
    out = []
    for ch in s:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        # пробелы, дефисы, пунктуацию отбрасываем
    return "".join(out)

def find_goal_by_title(tg_id: int, title: str, status: str = "active") -> dict | None:
    """
    Ищет активную цель с похожим названием (устойчиво к раскладке и регистру).
    Возвращает первую совпавшую цель или None.
    Для интерактивного выбора используй find_goal_match() — она сообщает
    ещё и НАСКОЛЬКО уверенно совпало.
    """
    m = find_goal_match(tg_id, title, status)
    return m["goal"] if m else None

def find_goal_match(tg_id: int, title: str, status: str = "active") -> dict | None:
    """
    Как find_goal_by_title, но возвращает {"goal": ..., "exact": bool} или None.
    exact=True  — точное совпадение после нормализации (можно уверенно предлагать);
    exact=False — частичное вхождение (одно название содержит другое) —
                  совпадение вероятное, но не стопроцентное.
    """
    try:
        needle = _normalize_title(title)
        if not needle:
            return None
        goals = get_goals(tg_id, status)
        # 1) точное совпадение после нормализации
        for g in goals:
            if _normalize_title(g.get("title") or "") == needle:
                return {"goal": g, "exact": True}
        # 2) частичное вхождение (одно содержит другое)
        for g in goals:
            existing = _normalize_title(g.get("title") or "")
            if existing and (needle in existing or existing in needle):
                return {"goal": g, "exact": False}
        # 3) нечёткое совпадение (difflib) — предлагает ЛУЧШЕГО кандидата
        #    для подтверждения кнопкой. Порог низкий (0.4), потому что это
        #    НЕ авто-запись: пользователь всё равно подтверждает выбор или
        #    берёт другую цель из списка. Задача — просто угадать вероятную
        #    цель по умолчанию («макбук» → предложить «MacBook»).
        import difflib
        best = None
        best_ratio = 0.0
        for g in goals:
            existing = _normalize_title(g.get("title") or "")
            if not existing:
                continue
            ratio = difflib.SequenceMatcher(None, needle, existing).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = g
        if best is not None and best_ratio >= 0.4:
            return {"goal": best, "exact": False}
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка поиска цели: {e}")
        return None

def get_goals_brief(tg_id: int, status: str = "active") -> list:
    """Короткий список активных целей для кнопок выбора: [{id, title, saved, target}]."""
    try:
        out = []
        for g in get_goals(tg_id, status):
            out.append({
                "id": g["id"],
                "title": g.get("title") or "",
                "saved": g.get("saved_amount") or 0,
                "target": g.get("target_amount") or 0,
            })
        return out
    except Exception as e:
        logger.error(f"❌ Ошибка списка целей: {e}")
        return []

def get_saved_in_goals(tg_id: int, status: str = "active") -> float:
    """Сумма всех saved_amount по активным целям — 'сколько в копилке'."""
    try:
        total = 0
        for g in get_goals(tg_id, status):
            total += (g.get("saved_amount") or 0)
        return total
    except Exception as e:
        logger.error(f"❌ Ошибка подсчёта копилки: {e}")
        return 0

def add_to_goal(tg_id: int, goal_id: int, amount: float, journal: bool = False,
                description: str = None) -> dict | None:
    """
    Пополняет цель на amount (прибавляет к saved_amount). Может уйти в минус
    при откате (amount < 0) — тогда saved_amount не опускаем ниже нуля.

    journal=True  → ДОПОЛНИТЕЛЬНО пишет маркерную транзакцию «в копилку»
                    с goal_id (К9): вклад становится виден в истории и его
                    можно отменить. Баланс от этой транзакции НЕ страдает —
                    категория «в копилку» исключена из get_stats/monthly.
    journal=False → только двигает saved_amount, транзакцию НЕ пишет
                    (используется для ОТКАТА при отмене — там транзакция
                    уже удаляется отдельно, второй раз журналить не нужно).

    Возвращает обновлённую цель или None.
    """
    try:
        db = get_client()
        cur = db.table("goals").select("*").eq("id", goal_id).execute()
        if not cur.data:
            logger.error(f"❌ Цель id={goal_id} не найдена для пополнения")
            return None
        goal = cur.data[0]
        new_saved = (goal.get("saved_amount") or 0) + amount
        if new_saved < 0:
            new_saved = 0  # защита от отрицательной копилки при откате
        db.table("goals").update({"saved_amount": new_saved}).eq("id", goal_id).execute()
        logger.info(f"🎯 Цель id={goal_id}: saved {amount:+} → {new_saved}")
        goal["saved_amount"] = new_saved

        if journal and amount > 0:
            desc = description or f"в копилку: {goal.get('title', '')}"
            # тип "expense" — техническая условность (в копилку = деньги «уходят»
            # из свободных), но категория GOAL_DEPOSIT_CATEGORY исключает её из
            # баланса и статистики, так что на цифры это НЕ влияет.
            save_transaction(tg_id, "expense", amount, GOAL_DEPOSIT_CATEGORY,
                             desc, goal_id=goal_id)
        return goal
    except Exception as e:
        logger.error(f"❌ Ошибка пополнения цели: {e}")
        return None

def add_goal(tg_id: int, title: str, target_amount: float, deadline: str = None) -> int | None:
    """Добавляет цель. Возвращает id созданной цели или None при ошибке."""
    try:
        db = get_client()
        data = {"user_tg_id": tg_id, "title": title, "target_amount": target_amount}
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

def complete_goal(tg_id: int, goal_id: int, spent_amount: float, category: str, description: str) -> bool:
    """ЗАКРЫТЬ ЦЕЛЬ ПОКУПКОЙ (Атомарно через RPC).
    Пишет расход и обнуляет цель прямо внутри базы данных за один шаг."""
    try:
        db = get_client()
        result = db.rpc("rpc_complete_goal", {
            "p_user_tg_id": tg_id,  # <--- Передаем паспорт в БД
            "p_goal_id": goal_id,
            "p_amount": spent_amount,
            "p_category": category,
            "p_description": description
        }).execute()
        
        if result.data:
            logger.info(f"🏁 Цель id={goal_id} атомарно закрыта покупкой на {spent_amount}")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка атомарного закрытия цели: {e}")
        return False

def withdraw_goal(goal_id: int) -> dict | None:
    """
    ЗАКРЫТЬ ЦЕЛЬ БЕЗ ТРАТЫ (К8, сценарий «передумал копить»).

    Модель «резерв»: деньги всё это время лежали в балансе, отдельного
    кошелька нет. Поэтому «вернуть» физически нечего — достаточно СНЯТЬ
    ПОМЕТКУ: saved_amount → 0, статус → "withdrawn". Освободившаяся сумма
    автоматически снова станет «свободной» (свободно = баланс − Σsaved).
    Баланс НЕ трогаем, никаких транзакций НЕ пишем.

    Возвращает {..., "released": <сколько было в копилке>} или None.
    """
    try:
        db = get_client()
        cur = db.table("goals").select("*").eq("id", goal_id).execute()
        if not cur.data:
            logger.error(f"❌ Цель id={goal_id} не найдена для закрытия")
            return None
        goal = cur.data[0]
        released = goal.get("saved_amount") or 0
        db.table("goals").update(
            {"status": "withdrawn", "saved_amount": 0}
        ).eq("id", goal_id).execute()
        goal["status"] = "withdrawn"
        goal["saved_amount"] = 0
        goal["released"] = released
        logger.info(f"🚪 Цель id={goal_id} «{goal.get('title','')}» закрыта без траты, освобождено {released}")
        return goal
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия цели: {e}")
        return None

def reopen_goal(goal_id: int, status: str = "active") -> bool:
    """Вернуть цель в активные (для ОТМЕНЫ завершения/закрытия цели)."""
    try:
        db = get_client()
        db.table("goals").update({"status": status}).eq("id", goal_id).execute()
        logger.info(f"↩️ Цель id={goal_id} снова {status}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка возврата цели в активные: {e}")
        return False

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

def get_debts(tg_id: int, status: str = "active") -> list:
    try:
        db = get_client()
        result = (
            db.table("debts")
            .select("*")
            .eq("user_tg_id", tg_id)
            .eq("status", status)
            .order("created_at", desc=False)
            .execute()
        )
        return result.data
    except Exception as e:
        logger.error(f"❌ Ошибка получения долгов: {e}")
        return []

def add_debt(tg_id: int, direction: str, person: str, amount: float, description: str = "", due_date: str = None) -> int | None:
    """Добавляет долг. Возвращает id созданного долга или None при ошибке."""
    try:
        db = get_client()
        data = {
            "user_tg_id":  tg_id,
            "direction":   direction,
            "person":      person,
            "amount":      amount,
            "description": description,
        }
        if due_date:
            data["due_date"] = due_date
        result = db.table("debts").insert(data).execute()
        logger.info(f"💸 Долг добавлен: {direction} | {person} | {amount}")
        if result.data and len(result.data) > 0:
            return result.data[0].get("id")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка добавления долга: {e}")
        return None

def close_debt(debt_id: int) -> bool:
    try:
        db = get_client()
        db.table("debts").update({"status": "paid"}).eq("id", debt_id).execute()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка закрытия долга: {e}")
        return False

def delete_debt(debt_id: int) -> bool:
    """Удаляет долг по id (для отмены создания долга)."""
    try:
        db = get_client()
        db.table("debts").delete().eq("id", debt_id).execute()
        logger.info(f"🗑️ Долг удалён: id={debt_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка удаления долга: {e}")
        return False

def count_debt_transactions(debt_id: int) -> int:
    """
    Сколько транзакций привязано к долгу (К10).
    Нужно перед ОТМЕНОЙ транзакции-создания долга: если по долгу уже были
    погашения/возвраты (то есть транзакций > 1), простая отмена создаст
    рассинхрон (деньги «из воздуха», исчезнувший долг). bot.py по этому
    числу решает — можно молча отменить или надо предупредить пользователя.
    """
    try:
        db = get_client()
        result = (
            db.table("transactions")
            .select("id")
            .eq("debt_id", debt_id)
            .execute()
        )
        return len(result.data or [])
    except Exception as e:
        logger.error(f"❌ Ошибка подсчёта транзакций долга: {e}")
        return 0

def find_debt_by_person(tg_id: int, person: str, direction: str = None, status: str = "active") -> dict | None:
    """
    Ищет активный долг по имени человека (без учёта регистра).
    direction (опционально): 'i_owe' или 'owe_me' — сузить поиск.
    Возвращает первый совпавший долг или None.
    """
    try:
        needle = (person or "").strip().lower()
        if not needle:
            return None
        for d in get_debts(tg_id, status):
            d_person = (d.get("person") or "").strip().lower()
            if d_person != needle and needle not in d_person and d_person not in needle:
                continue
            if direction and d.get("direction") != direction:
                continue
            return d
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка поиска долга: {e}")
        return None

def reduce_debt(debt_id: int, amount: float) -> dict | None:
    """
    Уменьшает долг на amount. Если остаток <= 0 — помечает долг как 'paid'.
    Возвращает обновлённый долг {person, amount, direction, status} или None.
    """
    try:
        db = get_client()
        cur = db.table("debts").select("*").eq("id", debt_id).execute()
        if not cur.data:
            logger.error(f"❌ Долг id={debt_id} не найден")
            return None
        debt = cur.data[0]
        new_amount = (debt.get("amount") or 0) - amount
        if new_amount <= 0:
            db.table("debts").update({"amount": 0, "status": "paid"}).eq("id", debt_id).execute()
            debt["amount"] = 0
            debt["status"] = "paid"
            logger.info(f"💸 Долг id={debt_id} полностью погашен")
        else:
            db.table("debts").update({"amount": new_amount}).eq("id", debt_id).execute()
            debt["amount"] = new_amount
            logger.info(f"💸 Долг id={debt_id} уменьшен на {amount} → {new_amount}")
        return debt
    except Exception as e:
        logger.error(f"❌ Ошибка уменьшения долга: {e}")
        return None

def restore_debt(debt_id: int, amount: float) -> dict | None:
    """
    Возвращает долгу сумму обратно (прибавляет amount) и снова делает его активным.
    Используется при ОТМЕНЕ транзакции погашения/возврата долга —
    то есть когда мы откатываем УМЕНЬШЕНИЕ долга и он должен ожить.
    Возвращает обновлённый долг {..., amount, status} или None.
    """
    try:
        db = get_client()
        cur = db.table("debts").select("*").eq("id", debt_id).execute()
        if not cur.data:
            logger.error(f"❌ Долг id={debt_id} не найден для восстановления")
            return None
        debt = cur.data[0]
        new_amount = (debt.get("amount") or 0) + amount
        db.table("debts").update(
            {"amount": new_amount, "status": "active"}
        ).eq("id", debt_id).execute()
        debt["amount"] = new_amount
        debt["status"] = "active"
        logger.info(f"💸 Долг id={debt_id} восстановлен: +{amount} → {new_amount} (снова активен)")
        return debt
    except Exception as e:
        logger.error(f"❌ Ошибка восстановления долга: {e}")
        return None


# ============================================================
# ИСТОРИЯ ДИАЛОГА — хранится в базе, не в памяти!
# ============================================================

def save_message(tg_id: int, role: str, content: str) -> bool:
    """Сохранить сообщение в историю"""
    try:
        db = get_client()
        db.table("chat_history").insert({
            "user_tg_id": tg_id,
            "role":    role,
            "content": content,
        }).execute()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сообщения: {e}")
        return False

def get_chat_history(tg_id: int, limit: int = 20) -> list:
    """Получить последние N сообщений для контекста"""
    try:
        db = get_client()
        result = (
            db.table("chat_history")
            .select("role, content")
            .eq("user_tg_id", tg_id)
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

def clear_chat_history(tg_id: int) -> bool:
    """Очистить историю диалога (удаляет только строки пользователя)."""
    try:
        db = get_client()
        db.table("chat_history").delete().eq("user_tg_id", tg_id).execute()
        logger.info("🧹 История диалога очищена")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")
        return False


# ============================================================
# HARD RESET — полное обнуление для чистого теста «с нуля» (/reset)
# ============================================================

def hard_reset_all(tg_id: int) -> dict:
    """
    Стирает пользовательские данные во всех таблицах: транзакции, цели,
    долги, историю диалога и профиль. Возвращает отчёт {table: ok/err}.

    ⚠️ НЕОБРАТИМО. Вызывать только после ЯВНОГО двойного подтверждения в bot.py.
    Схему таблиц НЕ трогает — только строки. id-последовательности не сбрасывает
    (новые записи продолжат нумерацию дальше — это нормально и не мешает).

    Удаляем по первичному ключу id (.gte("id", 0)) — он есть во ВСЕХ пяти
    таблицах. Раньше использовался created_at, но у user_profile такой колонки
    нет (там updated_at), из-за чего профиль не чистился. id — надёжный
    общий предикат, не зависящий от имени колонки времени.
    """
    report = {}
    tables = ["transactions", "goals", "debts", "chat_history", "user_profile"]
    try:
        db = get_client()
        for t in tables:
            try:
                db.table(t).delete().eq("user_tg_id", tg_id).execute()
                report[t] = "ok"
                logger.info(f"🧨 RESET: таблица {t} очищена")
            except Exception as e:
                report[t] = f"err: {e}"
                logger.error(f"❌ RESET: не удалось очистить {t}: {e}")
        return report
    except Exception as e:
        logger.error(f"❌ RESET: критическая ошибка: {e}")
        report["_fatal"] = str(e)
        return report
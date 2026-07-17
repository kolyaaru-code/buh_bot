# ============================================================
# BOT.PY — версия 3
# Память и контекст + действия над сущностями:
# пополнение целей, погашение/возврат долгов, отмена операций.
# Баланс = живые деньги; долговые движения порождают транзакции.
# ============================================================

import os
import logging
import uuid
import tempfile
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import database as db
import ai_helper as ai

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID"))

# Часовой пояс для отображения дат — Екатеринбург, UTC+5
LOCAL_TZ = timezone(timedelta(hours=5))

# Категории-метки долговых движений (нужны при ОТМЕНЕ — определить судьбу долга):
#   создание долга ("выдача долга"/"получен заём")        -> при отмене долг ОБНУЛЯЕМ (reduce_debt)
#   уменьшение долга ("погашение долга"/"возврат долга")  -> при отмене долг ВОЗВРАЩАЕМ (restore_debt)
CREATION_DEBT_CATS = {"выдача долга", "получен заём"}
REDUCTION_DEBT_CATS = {"погашение долга", "возврат долга"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# ЗАЩИТА — только хозяин
# ------------------------------------------------------------
def is_owner(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if user.id != MY_TELEGRAM_ID:
        logger.warning(f"⛔ Чужой! ID: {user.id}, Имя: {user.full_name}")
        return False
    return True

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            return
        return await func(update, context)
    return wrapper

# ------------------------------------------------------------
# ВСПОМОГАТЕЛЬНОЕ
# ------------------------------------------------------------
def load_context(tg_id: int) -> dict:
    return {
        "profile": db.get_profile(tg_id),
        "stats":   db.get_stats(tg_id),
        "monthly": db.get_monthly_stats(tg_id),
        "goals":   db.get_goals(tg_id),
        "debts":   db.get_debts(tg_id),
    }

def _fmt_local_date(created_at: str) -> str:
    """Преобразует ISO-дату из базы (UTC) в дату по Екатеринбургу (ДД.ММ.ГГГГ)."""
    if not created_at:
        return "—"
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(LOCAL_TZ)
        return local.strftime("%d.%m.%Y")
    except Exception:
        return created_at[:10]

async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает индикатор 'печатает…' вместо мусорного сообщения."""
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        pass

def _stash(context: ContextTypes.DEFAULT_TYPE, payload: dict) -> str:
    """Кладёт данные под короткий токен в bot_data, возвращает токен (для callback_data)."""
    import time
    token = uuid.uuid4().hex[:12]
    payload["_ts"] = time.time()  # Запоминаем время создания
    context.bot_data[f"pending_{token}"] = payload
    return token

def _unstash(context: ContextTypes.DEFAULT_TYPE, token: str) -> dict | None:
    return context.bot_data.get(f"pending_{token}")

def _drop(context: ContextTypes.DEFAULT_TYPE, token: str):
    context.bot_data.pop(f"pending_{token}", None)

async def cleanup_old_tokens(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача: удаляет токены старше 24 часов (86400 секунд)."""
    import time
    now = time.time()
    to_delete = []
    
    for key, val in context.bot_data.items():
        if key.startswith("pending_"):
            # Если метки нет (старые данные) или прошло больше 24 часов
            if now - val.get("_ts", 0) > 86400:
                to_delete.append(key)
                
    for key in to_delete:
        context.bot_data.pop(key, None)
        
    if to_delete:
        logger.info(f"🧹 Уборка: удалено зависших кнопок — {len(to_delete)}")

# ============================================================
# КОМАНДЫ
# ============================================================

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = db.get_profile()
    name = profile.get("name", "")
    greeting = f"Привет, {name}! 👋" if name else "Привет! 👋"

    await update.message.reply_text(
        f"{greeting} Я твой личный финансовый помощник 💰\n\n"
        f"Я помню всё о тебе, твоих целях и долгах.\n"
        f"Просто общайся со мной как с человеком!\n\n"
        f"Примеры:\n"
        f"«Потратил 500 на обед» 🍕\n"
        f"«Получил зарплату 80000» 💵\n"
        f"«Хочу накопить на MacBook 150000» 🎯\n"
        f"«Отложил 10000 на MacBook» 🐷\n"
        f"«Саша должен мне 3000» 💸\n"
        f"«Саша вернул мне 3000» ✅\n"
        f"«Отмени последнюю операцию» ↩️\n\n"
        f"📋 Команды:\n"
        f"/balance — баланс\n"
        f"/month — статистика за месяц\n"
        f"/history — последние записи\n"
        f"/categories — расходы по категориям\n"
        f"/goals — мои цели\n"
        f"/debts — мои долги\n"
        f"/profile — мой профиль\n"
        f"/advice — совет от ИИ\n"
        f"/clear — очистить память диалога\n"
        f"/reset — стереть все данные (с подтверждением)"
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    stats = db.get_stats(tg_id)
    if not stats:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return
    if stats["count"] == 0:
        await update.message.reply_text("📊 Записей пока нет. Просто напиши что купил!")
        return

    saved = db.get_saved_in_goals(tg_id)
    free = stats["balance"] - saved

    emoji = "🟢" if stats["balance"] > 0 else "🔴" if stats["balance"] < 0 else "⚪"
    text = (
        f"📊 Общий баланс\n"
        f"{'─' * 25}\n"
        f"💰 Доходы:  {stats['income']:>12,.0f}\n"
        f"🛒 Расходы: {stats['expense']:>12,.0f}\n"
        f"{'─' * 25}\n"
        f"{emoji} Всего:  {stats['balance']:>12,.0f}\n"
    )
    if saved > 0:
        text += (
            f"🐷 В копилке на цели: {saved:,.0f}\n"
            f"✅ Свободно: {free:,.0f}\n"
        )
    text += f"\n📝 Всего записей: {stats['count']}"
    await update.message.reply_text(text)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    monthly = db.get_monthly_stats(tg_id)
    if not monthly:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return

    emoji = "🟢" if monthly["balance"] > 0 else "🔴" if monthly["balance"] < 0 else "⚪"
    await update.message.reply_text(
        f"📅 {monthly['month']}\n"
        f"{'─' * 25}\n"
        f"💰 Доходы:  {monthly['income']:>12,.0f}\n"
        f"🛒 Расходы: {monthly['expense']:>12,.0f}\n"
        f"{'─' * 25}\n"
        f"{emoji} Итого:  {monthly['balance']:>12,.0f}\n\n"
        f"📝 Операций за месяц: {monthly['count']}"
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    transactions = db.get_last_transactions(tg_id, 7)
    if not transactions:
        await update.message.reply_text("📭 История пуста.")
        return

    text = "🕐 Последние записи:\n" + "─" * 25 + "\n"
    for t in transactions:
        emoji = "💰" if t["type"] == "income" else "🛒"
        date = _fmt_local_date(t.get("created_at"))
        text += (
            f"{emoji} {t['amount']:,.0f} — {t['category']}\n"
            f"   📝 {t['description']}\n"
            f"   📅 {date}\n\n"
        )
    await update.message.reply_text(text)

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    cats = db.get_expenses_by_category(tg_id)
    if not cats:
        await update.message.reply_text("📭 Расходов пока нет.")
        return

    total = sum(cats.values())
    text = "📊 Расходы по категориям:\n" + "─" * 25 + "\n"
    for cat, amount in cats.items():
        percent = (amount / total * 100) if total > 0 else 0
        filled = int(percent / 10)
        bar = "█" * filled + "░" * (10 - filled)
        text += f"{bar} {percent:.0f}%\n{cat}: {amount:,.0f}\n\n"
    await update.message.reply_text(text)

async def goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    goals_list = db.get_goals(tg_id)
    if not goals_list:
        await update.message.reply_text(
            "🎯 Целей пока нет.\n\n"
            "Просто напиши мне:\n"
            "«Хочу накопить на MacBook 150000»"
        )
        return

    text = "🎯 Твои финансовые цели:\n" + "─" * 25 + "\n"
    for g in goals_list:
        target = g.get("target_amount", 0) or 0
        saved = g.get("saved_amount", 0) or 0
        filled = int((saved / target * 10)) if target > 0 else 0
        filled = max(0, min(filled, 10))
        bar = "█" * filled + "░" * (10 - filled)
        percent = (saved / target * 100) if target > 0 else 0
        deadline = f"\n   📅 Дедлайн: {g['deadline']}" if g.get("deadline") else ""
        text += (
            f"🎯 {g['title']}\n"
            f"   {bar} {percent:.0f}%\n"
            f"   Накоплено: {saved:,.0f} из {target:,.0f}{deadline}\n\n"
        )
    await update.message.reply_text(text)

async def debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    debts_list = db.get_debts(tg_id)
    if not debts_list:
        await update.message.reply_text(
            "💸 Долгов нет — отлично! 🎉\n\n"
            "Если появятся, просто напиши:\n"
            "«Занял у Саши 5000» или «Петя должен мне 3000»"
        )
        return

    i_owe = [d for d in debts_list if d["direction"] == "i_owe"]
    owe_me = [d for d in debts_list if d["direction"] == "owe_me"]

    text = "💸 Долги:\n" + "─" * 25 + "\n"

    if i_owe:
        total = sum(d["amount"] for d in i_owe)
        text += f"🛒 Я должен (итого: {total:,.0f}):\n"
        for d in i_owe:
            due = f" (до {d['due_date']})" if d.get("due_date") else ""
            text += f"  • {d['person']}: {d['amount']:,.0f} — {d.get('description', '')}{due}\n"
        text += "\n"

    if owe_me:
        total = sum(d["amount"] for d in owe_me)
        text += f"💰 Должны мне (итого: {total:,.0f}):\n"
        for d in owe_me:
            due = f" (до {d['due_date']})" if d.get("due_date") else ""
            text += f"  • {d['person']}: {d['amount']:,.0f} — {d.get('description', '')}{due}\n"

    await update.message.reply_text(text)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    prof = db.get_profile(tg_id)
    if not prof:
        await update.message.reply_text(
            "👤 Профиль пока пуст.\n\n"
            "Расскажи мне о себе:\n"
            "«Меня зовут Никита, мне 28 лет, я живу в Москве»"
        )
        return

    labels = {
        "name": "Имя",
        "age": "Возраст",
        "city": "Город",
        "job": "Работа",
        "income_source": "Доход",
    }
    text = "👤 Твой профиль:\n" + "─" * 25 + "\n"
    for key, value in prof.items():
        label = labels.get(key, key)
        text += f"{label}: {value}\n"
    await update.message.reply_text(text)

async def advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update, context)
    tg_id = update.effective_user.id
    ctx = load_context(tg_id)
    if not ctx["stats"] or ctx["stats"]["count"] == 0:
        await update.message.reply_text("📭 Мало данных для анализа. Добавь несколько записей!")
        return

    cats = db.get_expenses_by_category(tg_id)
    advice_text = ai.get_financial_advice(
        ctx["profile"], ctx["stats"], ctx["monthly"], cats, ctx["goals"], ctx["debts"]
    )
    await update.message.reply_text(f"💡 Совет:\n\n{advice_text}")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    db.clear_chat_history(tg_id)
    await update.message.reply_text("🧹 Память диалога очищена!")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    stats = db.get_stats(tg_id)
    goals_n = len(db.get_goals(tg_id))
    debts_n = len(db.get_debts(tg_id))
    cnt = stats.get("count", 0) if stats else 0
    token = _stash(context, {"kind": "reset_step1"})
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Да, стереть всё", callback_data=f"q:reset_confirm1:{token}"),
        InlineKeyboardButton("❌ Отмена",          callback_data=f"q:cancel_noop:{token}"),
    ]])
    await update.message.reply_text(
        "🧨 ПОЛНЫЙ СБРОС\n"
        "─────────────\n"
        "Будут БЕЗВОЗВРАТНО удалены ВСЕ данные:\n"
        f"  • операции (~{cnt})\n"
        f"  • цели ({goals_n})\n"
        f"  • долги ({debts_n})\n"
        "  • история диалога\n"
        "  • профиль\n\n"
        "Это нельзя отменить. Начать сброс?",
        reply_markup=kb,
    )

# ============================================================
# СОЗДАНИЕ ДОЛГА (двигает баланс через транзакцию)
# дал в долг (owe_me) → расход «выдача долга»
# взял в долг (i_owe) → доход «получен заём»
# ============================================================
def _create_debt_with_tx(tg_id: int, d: dict) -> str:
    debt_id = db.add_debt(tg_id, d["direction"], d["person"], d["amount"], d["description"], d.get("due_date"))
    if debt_id is None:
        return f"⚠️ Не удалось записать долг ({d['person']})"

    if d["direction"] == "owe_me":
        # я дал в долг — деньги ушли из кармана
        db.save_transaction(tg_id, "expense", d["amount"], "выдача долга",
                            f"дал в долг: {d['person']}", debt_id=debt_id)
        return f"💰 {d['person']} должен тебе: {d['amount']:,.0f} (списано с баланса)"
    else:
        # я взял в долг — деньги пришли в карман
        db.save_transaction(tg_id, "income", d["amount"], "получен заём",
                            f"взял в долг у: {d['person']}", debt_id=debt_id)
        return f"💸 Ты должен {d['person']}: {d['amount']:,.0f} (зачислено на баланс)"

# ============================================================
# РАЗБОР ЦЕЛЕЙ (создание)
# ============================================================
def _handle_goals(tg_id: int, goals_found: list) -> tuple[list, list]:
    saved_lines = []
    pending = []
    for g in goals_found:
        title = g["title"]
        target = g["target_amount"]
        deadline = g.get("deadline")
        existing = db.find_goal_by_title(tg_id, title)
        if existing:
            pending.append({"title": title, "target": target, "deadline": deadline})
        else:
            goal_id = db.add_goal(tg_id, title, target, deadline)
            if goal_id is not None:
                saved_lines.append(f"🎯 Цель «{title}» на {target:,.0f}")
    return saved_lines, pending

# ============================================================
# ОБРАБОТКА ACTIONS (действия над существующим)
# Возвращает (saved_lines, summary_parts, questions)
# ============================================================
def _handle_actions(tg_id: int, actions: list) -> tuple[list, list, list]:
    saved_lines = []
    summary_parts = []
    questions = []

    for a in actions:
        act = a["action"]

        # --- пополнить цель (баланс НЕ трогаем) ---
        if act == "goal_deposit":
            goal = db.find_goal_by_title(tg_id, a["goal_title"])
            if goal is None:
                questions.append({
                    "kind": "goal_missing",
                    "goal_title": a["goal_title"],
                    "amount": a["amount"],
                })
                continue
            updated = db.add_to_goal(tg_id, goal["id"], a["amount"], journal=True,
                                     description=f"в копилку: {goal.get('title','')}")
            if updated:
                tgt = updated.get("target_amount") or 0
                sv = updated.get("saved_amount") or 0
                pct = (sv / tgt * 100) if tgt > 0 else 0
                saved_lines.append(
                    f"🐷 В копилку «{updated['title']}»: +{a['amount']:,.0f} "
                    f"(итого {sv:,.0f} из {tgt:,.0f}, {pct:.0f}%)"
                )
                summary_parts.append(f"пополнена цель {updated['title']} на {a['amount']:.0f}")
            else:
                saved_lines.append(f"⚠️ Не удалось пополнить цель «{a['goal_title']}»")

        # --- я вернул/погасил свой долг ---
        elif act == "debt_repay":
            debt = db.find_debt_by_person(tg_id, a["person"], direction="i_owe")
            if debt is None:
                questions.append({
                    "kind": "repay_no_debt",
                    "person": a["person"],
                    "amount": a["amount"],
                })
                continue
            db.save_transaction(tg_id, "expense", a["amount"], "погашение долга",
                                f"погашение долга: {a['person']}", debt_id=debt["id"])
            updated = db.reduce_debt(debt["id"], a["amount"])
            if updated and updated.get("status") == "paid":
                saved_lines.append(f"✅ Долг перед {a['person']} погашен полностью (−{a['amount']:,.0f} с баланса)")
            elif updated:
                saved_lines.append(
                    f"📉 Долг перед {a['person']} уменьшен на {a['amount']:,.0f} "
                    f"(остаток {updated['amount']:,.0f}, списано с баланса)"
                )
            summary_parts.append(f"погашен долг {a['person']} на {a['amount']:.0f}")

        # --- мне вернули долг ---
        elif act == "debt_return":
            debt = db.find_debt_by_person(tg_id, a["person"], direction="owe_me")
            if debt is None:
                questions.append({
                    "kind": "return_no_debt",
                    "person": a["person"],
                    "amount": a["amount"],
                })
                continue
            db.save_transaction(tg_id, "income", a["amount"], "возврат долга",
                                f"возврат долга от: {a['person']}", debt_id=debt["id"])
            updated = db.reduce_debt(debt["id"], a["amount"])
            if updated and updated.get("status") == "paid":
                saved_lines.append(f"✅ {a['person']} вернул долг полностью (+{a['amount']:,.0f} на баланс)")
            elif updated:
                saved_lines.append(
                    f"📈 {a['person']} вернул {a['amount']:,.0f} "
                    f"(остаток долга {updated['amount']:,.0f}, зачислено на баланс)"
                )
            summary_parts.append(f"возврат долга от {a['person']} на {a['amount']:.0f}")

        # --- отмена операции (через переспрос) ---
        elif act == "cancel":
            questions.append({
                "kind": "cancel",
                "hint": a.get("hint", ""),
            })

        # --- купил то, на что копил: закрыть цель покупкой ---
        elif act == "goal_complete":
            goal = db.find_goal_by_title(tg_id, a["goal_title"])
            if goal is None:
                tx_id = db.save_transaction(tg_id, "expense", a["amount"],
                                            a.get("category") or "другое", a["goal_title"])
                if tx_id is not None:
                    saved_lines.append(f"🛒 {a['amount']:,.0f} — {a.get('category') or 'другое'} ({a['goal_title']})")
                    summary_parts.append(f"expense {a['amount']:.0f} ({a.get('category') or 'другое'})")
                continue
            
            # Атомарно пишем расход и закрываем цель
            done = db.complete_goal(
                tg_id,  # <--- добавили tg_id
                goal["id"], 
                a["amount"], 
                a.get("category") or "другое", 
                f"покупка цели: {goal.get('title','')}"
            )
            if done:
                saved = goal.get("saved_amount") or 0
                note = ""
                if a["amount"] > saved and saved > 0:
                    note = f" (в копилке было {saved:,.0f}, доплатил {a['amount']-saved:,.0f} из свободных)"
                elif a["amount"] < saved:
                    note = f" (в копилке было {saved:,.0f}, разница {saved-a['amount']:,.0f} вернулась в свободные)"
                saved_lines.append(
                    f"🏁 Цель «{goal.get('title','')}» закрыта покупкой: −{a['amount']:,.0f}{note}"
                )
                summary_parts.append(f"закрыта цель {goal.get('title','')} покупкой на {a['amount']:.0f}")
            else:
                saved_lines.append(f"⚠️ Расход записал, но цель «{goal.get('title','')}» закрыть не вышло")

        # --- передумал копить: закрыть цель без траты ---
        elif act == "goal_withdraw":
            goal = db.find_goal_by_title(tg_id, a["goal_title"])
            if goal is None:
                saved_lines.append(f"🤔 Цели «{a['goal_title']}» не нашёл — нечего закрывать")
                continue
            res = db.withdraw_goal(goal["id"])
            if res:
                released = res.get("released") or 0
                if released > 0:
                    saved_lines.append(
                        f"🚪 Цель «{goal.get('title','')}» закрыта. "
                        f"{released:,.0f} снова свободны (из копилки в общий баланс)"
                    )
                else:
                    saved_lines.append(f"🚪 Цель «{goal.get('title','')}» закрыта.")
                summary_parts.append(f"закрыта цель {goal.get('title','')} без траты")
            else:
                saved_lines.append(f"⚠️ Не удалось закрыть цель «{goal.get('title','')}»")

    return saved_lines, summary_parts, questions

# ============================================================
# ПЕРЕСПРОСЫ КНОПКАМИ (в конце, после основного ответа)
# ============================================================
async def _ask_questions(message, context: ContextTypes.DEFAULT_TYPE,
                         questions: list, pending_goals: list):
    tg_id = message.chat_id  # <--- Достаем паспорт прямо из чата

    # дубликаты целей
    for p in pending_goals:
        token = _stash(context, {"type": "goal_dup", **p})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Добавить вторую", callback_data=f"q:gdup_add:{token}"),
            InlineKeyboardButton("❌ Не добавлять",   callback_data=f"q:gdup_skip:{token}"),
        ]])
        await message.reply_text(
            f"🎯 Цель «{p['title']}» уже есть в списке.\n"
            f"Точно добавить ещё одну на {p['target']:,.0f}?",
            reply_markup=kb,
        )

    for q in questions:
        kind = q["kind"]

        if kind == "goal_missing":
            token = _stash(context, q)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Создать цель", callback_data=f"q:gmiss_add:{token}"),
                InlineKeyboardButton("❌ Отменить",     callback_data=f"q:cancel_noop:{token}"),
            ]])
            await message.reply_text(
                f"Цели «{q['goal_title']}» пока нет. Создать её и сразу отложить {q['amount']:,.0f}?",
                reply_markup=kb,
            )

        elif kind == "repay_no_debt":
            # Достаем все долги, где "я должен"
            active_debts = [d for d in db.get_debts(tg_id) if d["direction"] == "i_owe"]
            kb_rows = []
            
            # Генерируем кнопку для каждого существующего долга
            for d in active_debts:
                dtok = _stash(context, {"amount": q["amount"], "debt_id": d["id"], "person": d["person"]})
                kb_rows.append([InlineKeyboardButton(f"👤 {d['person']} (остаток {d['amount']:,.0f})", callback_data=f"q:repay_fix:{dtok}")])
                
            token = _stash(context, q)
            kb_rows.append([InlineKeyboardButton("💸 Просто в расход", callback_data=f"q:repay_exp:{token}")])
            kb_rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"q:cancel_noop:{token}")])
            
            await message.reply_text(
                f"🤔 Долга перед «{q['person']}» не нашёл. Возможно, опечатка?\n"
                f"Выбери нужный из списка или запиши как обычный расход:",
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )

        elif kind == "return_no_debt":
            # Достаем все долги, где "должны мне"
            active_debts = [d for d in db.get_debts(tg_id) if d["direction"] == "owe_me"]
            kb_rows = []
            
            for d in active_debts:
                dtok = _stash(context, {"amount": q["amount"], "debt_id": d["id"], "person": d["person"]})
                kb_rows.append([InlineKeyboardButton(f"👤 {d['person']} (остаток {d['amount']:,.0f})", callback_data=f"q:return_fix:{dtok}")])
                
            token = _stash(context, q)
            kb_rows.append([InlineKeyboardButton("💰 Просто в доход", callback_data=f"q:return_inc:{token}")])
            kb_rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"q:cancel_noop:{token}")])
            
            await message.reply_text(
                f"🤔 Долга «{q['person']} должен мне» не нашёл.\n"
                f"Выбери нужный из списка или запиши как обычный доход:",
                reply_markup=InlineKeyboardMarkup(kb_rows),
            )

        elif kind == "cancel":
            hint = (q.get("hint") or "").strip().lower()
            recent = db.get_recent_transactions_full(tg_id, 10)
            target_tx = None
            if hint:
                for t in recent:
                    desc = (t.get("description") or "").lower()
                    cat = (t.get("category") or "").lower()
                    if hint in desc or hint in cat:
                        target_tx = t
                        break
            else:
                target_tx = recent[0] if recent else None

            if target_tx is None:
                await message.reply_text("🤔 Не нашёл подходящую операцию для отмены.")
                continue

            # К10: если отменяем транзакцию-СОЗДАНИЕ долга, а по этому долгу уже
            # были другие движения (погашения/возвраты) — простая отмена создаст
            # рассинхрон (деньги «из воздуха», исчезнувший долг). Предупреждаем.
            warn = ""
            cat_tx = (target_tx.get("category") or "")
            if target_tx.get("debt_id") and cat_tx in CREATION_DEBT_CATS:
                n_tx = db.count_debt_transactions(target_tx["debt_id"])
                if n_tx > 1:
                    warn = (
                        f"\n\n⚠️ По этому долгу уже есть другие операции "
                        f"(всего {n_tx}). Если отменить именно создание долга, "
                        f"цифры могут разъехаться. Лучше сначала отмени погашения/возвраты."
                    )

            token = _stash(context, {"kind": "cancel_confirm", "tx_id": target_tx["id"], "tx": target_tx})
            emoji = "🛒" if target_tx["type"] == "income" else "💰"
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, отменить", callback_data=f"q:cancel_yes:{token}"),
                InlineKeyboardButton("❌ Нет",          callback_data=f"q:cancel_noop:{token}"),
            ]])
            await message.reply_text(
                f"Отменить операцию?\n"
                f"{emoji} {target_tx['amount']:,.0f} — {target_tx.get('category','')} "
                f"({target_tx.get('description','')}){warn}",
                reply_markup=kb,
            )

# ============================================================
# ВКЛАД В ЦЕЛЬ — карточка подтверждения с выбором цели
# Показывается ВСЕГДА. Матчим цель, предлагаем её; кнопкой можно
# выбрать любую другую активную цель или создать новую.
# Запись — только по «Да» (ИИ здесь не участвует → нет ложных «накоплено»).
# ============================================================
async def _ask_deposit(message, context: ContextTypes.DEFAULT_TYPE,
                       goal_title: str, amount: float):
    match = db.find_goal_match(goal_title)   # {"goal":..., "exact":bool} | None
    goals = db.get_goals_brief()

    # целей вообще нет → сразу предложить создать
    if not goals:
        token = _stash(context, {"kind": "dep_nogoals", "goal_title": goal_title, "amount": amount})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎯 Создать цель", callback_data=f"q:dep_new:{token}"),
            InlineKeyboardButton("❌ Отмена",       callback_data=f"q:cancel_noop:{token}"),
        ]])
        await message.reply_text(
            f"Целей пока нет. Создать цель «{goal_title}» и отложить {amount:,.0f}?",
            reply_markup=kb,
        )
        return

    proposed = match["goal"] if match else goals[0]  # если не угадали — первая как дефолт
    token = _stash(context, {
        "kind": "dep_confirm",
        "goal_id": proposed["id"],
        "goal_title_said": goal_title,
        "amount": amount,
    })
    sv = proposed.get("saved_amount") or 0
    tgt = proposed.get("target_amount") or 0
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Да, в «{proposed.get('title','')}»",
                              callback_data=f"q:dep_yes:{token}")],
        [InlineKeyboardButton("🎯 Выбрать другую цель", callback_data=f"q:dep_pick:{token}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"q:cancel_noop:{token}")],
    ])
    hint = "" if (match and match.get("exact")) else " (угадал приблизительно — проверь!)"
    await message.reply_text(
        f"🐷 Отложить {amount:,.0f} в цель «{proposed.get('title','')}»?{hint}\n"
        f"   Сейчас накоплено {sv:,.0f} из {tgt:,.0f}.",
        reply_markup=kb,
    )

def _deposit_pick_kb(context: ContextTypes.DEFAULT_TYPE, amount: float, said: str) -> InlineKeyboardMarkup:
    """Клавиатура со всеми активными целями для выбора, куда отложить."""
    rows = []
    for g in db.get_goals_brief():
        token = _stash(context, {
            "kind": "dep_pick_one", "goal_id": g["id"], "amount": amount, "goal_title_said": said,
        })
        rows.append([InlineKeyboardButton(
            f"🎯 {g['title']} ({g['saved']:,.0f}/{g['target']:,.0f})",
            callback_data=f"q:dep_goal:{token}",
        )])
    # создать новую + отмена
    tok_new = _stash(context, {"kind": "dep_nogoals", "goal_title": said, "amount": amount})
    rows.append([InlineKeyboardButton("➕ Создать новую цель", callback_data=f"q:dep_new:{tok_new}")])
    tok_cancel = _stash(context, {"kind": "dep_cancel"})
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"q:cancel_noop:{tok_cancel}")])
    return InlineKeyboardMarkup(rows)

def _do_deposit(tg_id: int, goal_id: int, amount: float) -> str:
    """Реальная запись вклада (журналируется, К9). Возвращает строку-итог."""
    updated = db.add_to_goal(tg_id, goal_id, amount, journal=True)
    if not updated:
        return "⚠️ Не удалось записать вклад."
    tgt = updated.get("target_amount") or 0
    sv = updated.get("saved_amount") or 0
    pct = (sv / tgt * 100) if tgt > 0 else 0
    return (f"🐷 Отложил в «{updated.get('title','')}»: +{amount:,.0f}\n"
            f"   Итого {sv:,.0f} из {tgt:,.0f} ({pct:.0f}%)")

# ============================================================
# ОБРАБОТЧИК ВСЕХ КНОПОК
# ============================================================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = query.from_user.id  # <--- СНЯЛИ ОХРАНУ И ДОСТАЛИ ПАСПОРТ!

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "q":
        await query.edit_message_text("⚠️ Не понял кнопку.")
        return
    _, action, token = parts

    if action.startswith("draft_"):
        await _on_draft_button(query, context, action)
        return

    payload = _unstash(context, token)
    if payload is None:
        await query.edit_message_text("⌛ Кнопка устарела. Повтори запрос, если нужно.")
        return

    if action == "confirm_yes":
        pending = payload.get("pending") or {}
        raw_text = payload.get("raw_text", "")
        try:
            await query.edit_message_text("⏳ Записываю…")
        except Exception as e:
            logger.error(f"❌ Не смог показать «Записываю…»: {e}")
        await _commit_analysis(tg_id, query, context, pending, raw_text)
        _drop(context, token)
        return
    elif action == "confirm_no":
        await query.edit_message_text("👌 Отменил. В базу ничего не записал.")
        _drop(context, token)
        return
    elif action == "confirm_edit":
        pending = payload.get("pending") or {}
        txs = pending.get("transactions") or []
        has_other = bool((pending.get("goals") or []) or (pending.get("debts") or [])
                         or (pending.get("actions") or []))
        _drop(context, token)
        if len(txs) == 1 and not has_other:
            t = txs[0]
            context.user_data["draft"] = {
                "type":            t.get("type"),
                "amount":          t.get("amount"),
                "category":        t.get("category"),
                "description":     t.get("description"),
                "awaiting":        None,
                "card_chat_id":    query.message.chat_id,
                "card_message_id": query.message.message_id,
            }
            await query.edit_message_text(
                _render_draft(context.user_data["draft"]), reply_markup=_draft_kb()
            )
        else:
            await query.edit_message_text(
                "✏️ Хорошо. Напиши или наговори операцию заново — я разберу и переспрошу."
            )
        return

    if action == "gdup_add":
        gid = db.add_goal(tg_id, payload["title"], payload["target"], payload.get("deadline"))
        if gid is not None:
            await query.edit_message_text(f"✅ Добавил вторую цель «{payload['title']}» на {payload['target']:,.0f}.")
        else:
            await query.edit_message_text("❌ Не получилось добавить цель.")
    elif action == "gdup_skip":
        await query.edit_message_text(f"👌 Ок, не добавляю «{payload['title']}».")

    elif action == "gmiss_add":
        gid = db.add_goal(tg_id, payload["goal_title"], payload["amount"], None)
        if gid is not None:
            db.add_to_goal(tg_id, gid, payload["amount"])
            await query.edit_message_text(
                f"🎯 Создал цель «{payload['goal_title']}» и отложил {payload['amount']:,.0f}.\n"
                f"⚠️ Целевую сумму не знаю — допиши, например: «цель {payload['goal_title']} 100000»."
            )
        else:
            await query.edit_message_text("❌ Не получилось создать цель.")

    elif action == "repay_exp":
        db.save_transaction(tg_id, "expense", payload["amount"], "погашение долга",
                            f"погашение (долг не найден): {payload['person']}")
        await query.edit_message_text(f"💸 Записал {payload['amount']:,.0f} как расход.")

    elif action == "return_inc":
        db.save_transaction(tg_id, "income", payload["amount"], "возврат долга",
                            f"возврат (долг не найден): {payload['person']}")
        await query.edit_message_text(f"💰 Записал {payload['amount']:,.0f} как доход.")

    elif action == "repay_fix":
        debt_id = payload["debt_id"]
        amount = payload["amount"]
        person = payload["person"]
        
        db.save_transaction(tg_id, "expense", amount, "погашение долга",
                            f"погашение долга: {person}", debt_id=debt_id)
        updated = db.reduce_debt(debt_id, amount)
        
        if updated and updated.get("status") == "paid":
            await query.edit_message_text(f"✅ Долг перед {person} погашен полностью (−{amount:,.0f} с баланса)")
        elif updated:
            await query.edit_message_text(
                f"📉 Долг перед {person} уменьшен на {amount:,.0f} "
                f"(остаток {updated['amount']:,.0f}, списано с баланса)"
            )
        else:
            await query.edit_message_text("⚠️ Ошибка при обновлении долга.")

    elif action == "return_fix":
        debt_id = payload["debt_id"]
        amount = payload["amount"]
        person = payload["person"]
        
        db.save_transaction(tg_id, "income", amount, "возврат долга",
                            f"возврат долга от: {person}", debt_id=debt_id)
        updated = db.reduce_debt(debt_id, amount)
        
        if updated and updated.get("status") == "paid":
            await query.edit_message_text(f"✅ {person} вернул долг полностью (+{amount:,.0f} на баланс)")
        elif updated:
            await query.edit_message_text(
                f"📈 {person} вернул {amount:,.0f} "
                f"(остаток долга {updated['amount']:,.0f}, зачислено на баланс)"
            )
        else:
            await query.edit_message_text("⚠️ Ошибка при обновлении долга.")

    elif action == "cancel_yes":
        tx = payload.get("tx", {})
        tx_id = payload.get("tx_id")
        full = db.get_transaction(tx_id) if tx_id else None

        if full and full.get("debt_id"):
            debt_id = full["debt_id"]
            cat = full.get("category") or ""
            amt = full.get("amount") or 0
            if cat in CREATION_DEBT_CATS:
                db.reduce_debt(debt_id, amt)
            elif cat in REDUCTION_DEBT_CATS:
                db.restore_debt(debt_id, amt)

        if full and full.get("goal_id"):
            db.add_to_goal(tg_id, full["goal_id"], -(full.get("amount") or 0))

        ok = db.delete_transaction(tx_id) if tx_id else False
        if ok:
            emoji = "💰" if tx.get("type") == "income" else "🛒"
            await query.edit_message_text(
                f"🗑️ Операция отменена:\n{emoji} {tx.get('amount',0):,.0f} — {tx.get('category','')}"
            )
        else:
            await query.edit_message_text("❌ Не удалось отменить операцию.")

    elif action == "reset_confirm1":
        token2 = _stash(context, {"kind": "reset_step2"})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🧨 ДА, ТОЧНО СТЕРЕТЬ", callback_data=f"q:reset_confirm2:{token2}"),
            InlineKeyboardButton("❌ Нет, оставить",     callback_data=f"q:cancel_noop:{token2}"),
        ]])
        await query.edit_message_text(
            "❗ Последнее предупреждение.\n"
            "После нажатия все данные исчезнут навсегда.\n\n"
            "Точно стереть всё и начать с нуля?",
            reply_markup=kb,
        )

    elif action == "reset_confirm2":
        report = db.hard_reset_all(tg_id)
        errors = {k: v for k, v in report.items() if v != "ok"}
        if not errors:
            await query.edit_message_text(
                "🧨 Готово. Все данные стёрты — бот как новый.\n"
                "Можешь начинать вести учёт с чистого листа."
            )
        else:
            await query.edit_message_text(
                "⚠️ Сброс выполнен частично. Проблемы:\n"
                + "\n".join(f"  • {k}: {v}" for k, v in errors.items())
            )

    elif action == "goalbuy_yes":
        goal_id = payload.get("goal_id")
        amount = payload.get("amount")
        category = payload.get("category") or "другое"
        title = payload.get("goal_title", "")
        goal = db.get_goal(goal_id) if goal_id else None
        saved = (goal.get("saved_amount") or 0) if goal else 0
        
        done = db.complete_goal(tg_id, goal_id, amount, category, f"покупка цели: {title}") if goal_id else False
        if done:
            note = ""
            if amount > saved and saved > 0:
                note = f"\nВ копилке было {saved:,.0f}, доплата {amount-saved:,.0f} из свободных."
            elif amount < saved:
                note = f"\nВ копилке было {saved:,.0f}, разница {saved-amount:,.0f} вернулась в свободные."
            await query.edit_message_text(
                f"🏁 Цель «{title}» закрыта покупкой: −{amount:,.0f}.{note}"
            )
        else:
            await query.edit_message_text(
                f"🛒 Записал расход {amount:,.0f} — {category}, "
                f"но цель «{title}» закрыть не вышло."
            )

    elif action == "goalbuy_no":
        amount = payload.get("amount")
        category = payload.get("category") or "другое"
        desc = payload.get("goal_title", "")
        db.save_transaction(tg_id, "expense", amount, category, desc)
        await query.edit_message_text(f"🛒 Записал как обычный расход: {amount:,.0f} — {category}.")

    elif action == "dep_yes":
        result = _do_deposit(tg_id, payload["goal_id"], payload["amount"])
        await query.edit_message_text("✅ " + result)

    elif action == "dep_pick":
        kb = _deposit_pick_kb(context, payload["amount"], payload.get("goal_title_said", ""))
        await query.edit_message_text(
            f"В какую цель отложить {payload['amount']:,.0f}?",
            reply_markup=kb,
        )

    elif action == "dep_goal":
        result = _do_deposit(tg_id, payload["goal_id"], payload["amount"])
        await query.edit_message_text("✅ " + result)

    elif action == "dep_new":
        gid = db.add_goal(tg_id, payload["goal_title"], payload["amount"], None)
        if gid is not None:
            db.add_to_goal(tg_id, gid, payload["amount"], journal=True)
            await query.edit_message_text(
                f"🎯 Создал цель «{payload['goal_title']}» и отложил {payload['amount']:,.0f}.\n"
                f"⚠️ Целевую сумму пока не знаю — задай её, например: «цель {payload['goal_title']} 100000»."
            )
        else:
            await query.edit_message_text("❌ Не получилось создать цель.")

    elif action == "cancel_noop":
        await query.edit_message_text("👌 Ок, ничего не меняю.")

    else:
        await query.edit_message_text("⚠️ Неизвестное действие.")

    _drop(context, token)

# ============================================================
# ЯДРО ОБРАБОТКИ ТЕКСТА — общее для текста и голоса
# ============================================================
# ============================================================
# ЧЕРНОВИК ОПЕРАЦИИ (кнопки полей)
# Одна машина на два случая:
#   1) разбор пустой, но есть сигнал операции (цифра/слово-действие) — уточняем;
#   2) «Исправить» у готовой операции — правим поля.
#
# ⚠️ СОСТОЯНИЕ ЧЕРНОВИКА ЖИВЁТ В ПАМЯТИ (context.user_data), НЕ в базе!
#    При перезапуске бота посреди правки черновик теряется (данные НЕ портятся —
#    бот просто попросит начать заново). Для МУЛЬТИПОЛЬЗОВАТЕЛЬСКОЙ версии это
#    состояние НАДО ПЕРЕНЕСТИ В БД (иначе на многих людях перезапуск = потеря правок).
#    Пометка оставлена специально, чтобы не забыть при заходе на user_id.
# ============================================================

_EXPENSE_WORDS = (
    "потратил", "потратила", "потрачено", "потратили", "купил", "купила", "купили",
    "заплатил", "заплатила", "оплатил", "оплатила", "истратил", "спустил", "расход",
)
_INCOME_WORDS = (
    "получил", "получила", "получили", "зарплата", "зарплату", "аванс", "премия",
    "премию", "доход", "заработал", "заработала", "пришло", "поступило", "поступила",
)


def _has_operation_signal(text: str) -> bool:
    """Похоже ли сообщение на попытку записать операцию: есть цифра ИЛИ слово-действие."""
    s = (text or "").lower()
    if re.search(r"\d", s):
        return True
    return any(w in s for w in _EXPENSE_WORDS) or any(w in s for w in _INCOME_WORDS)


def _infer_type(text: str):
    """Пытаемся понять тип: 'expense' / 'income' / None (по словам-сигналам)."""
    s = (text or "").lower()
    if any(w in s for w in _EXPENSE_WORDS):
        return "expense"
    if any(w in s for w in _INCOME_WORDS):
        return "income"
    return None


def _parse_amount(text: str):
    """Достаёт первое число из текста. Понимает '200 000', '200000', '200 тысяч'. Иначе None."""
    s = (text or "").lower()
    for junk in ("рублей", "рубля", "руб.", "руб", "₽", "rub"):
        s = s.replace(junk, "")
    m = re.search(r"\d[\d\s\u00a0.,]*", s)
    if not m:
        return None
    raw = m.group(0)
    mult = 1
    tail = s[m.end():m.end() + 12]
    if re.search(r"^\s*(тысяч|тысячи|тысяча|тыс|k|к)\b", tail):
        mult = 1000
    raw = raw.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    
    # ПРАВИЛО 3 ЦИФР: если точка ровно одна и после неё 3 цифры — это тысячи
    if raw.count(".") == 1:
        if len(raw.split(".")[1]) == 3:
            raw = raw.replace(".", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        val = float(raw) * mult
    except ValueError:
        return None
        
    if val > 1_000_000_000:  # Защита БД от Numeric Overflow (максимум 1 млрд)
        return None
        
    return val if val > 0 else None


def _render_draft(draft: dict) -> str:
    """Текст карточки-черновика с текущими значениями полей."""
    t = draft.get("type")
    type_str = "🛒 Расход" if t == "expense" else "💰 Доход" if t == "income" else "— не указан"
    amount = draft.get("amount")
    amount_str = f"{amount:,.0f}" if amount is not None else "— не указана"
    cat = draft.get("category") or "— не указана"
    desc = draft.get("description") or "—"
    return (
        "🧾 Черновик операции\n"
        "─────────────\n"
        f"Тип: {type_str}\n"
        f"Сумма: {amount_str}\n"
        f"Категория: {cat}\n"
        f"Описание: {desc}\n\n"
        "Заполни недостающее кнопками и нажми «✅ Готово»."
    )


def _draft_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰↔🛒 Сменить тип", callback_data="q:draft_type:-")],
        [InlineKeyboardButton("✏️ Сумма",      callback_data="q:draft_ask_amount:-"),
         InlineKeyboardButton("🏷️ Категория",  callback_data="q:draft_ask_category:-")],
        [InlineKeyboardButton("📝 Описание",    callback_data="q:draft_ask_desc:-")],
        [InlineKeyboardButton("✅ Готово",      callback_data="q:draft_done:-"),
         InlineKeyboardButton("❌ Отмена",      callback_data="q:draft_cancel:-")],
    ])


async def _send_draft_card(message, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет карточку-черновик и запоминает её id, чтобы обновлять на месте."""
    draft = context.user_data.get("draft")
    if not draft:
        return
    sent = await message.reply_text(_render_draft(draft), reply_markup=_draft_kb())
    draft["card_chat_id"] = sent.chat_id
    draft["card_message_id"] = sent.message_id


async def _refresh_draft_card(context: ContextTypes.DEFAULT_TYPE):
    """Обновляет ранее отправленную карточку-черновик (после ввода значения словом)."""
    draft = context.user_data.get("draft")
    if not draft:
        return
    cid = draft.get("card_chat_id")
    mid = draft.get("card_message_id")
    if not cid or not mid:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=cid, message_id=mid,
            text=_render_draft(draft), reply_markup=_draft_kb(),
        )
    except Exception as e:
        logger.error(f"❌ Не смог обновить карточку черновика: {e}")


async def _apply_draft_field(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Применяет присланный текст как значение поля, которое ждали (awaiting)."""
    draft = context.user_data.get("draft")
    if not draft:
        return
    field = draft.get("awaiting")

    if field == "amount":
        amt = _parse_amount(text)
        if amt is None:
            await update.message.reply_text(
                "🤔 Не вижу число. Пришли сумму цифрами (например: 500), "
                "или нажми «❌ Отмена» на карточке."
            )
            return  # остаёмся в ожидании суммы
        draft["amount"] = amt
    elif field == "category":
        draft["category"] = text.strip()
    elif field == "description":
        draft["description"] = text.strip()

    draft["awaiting"] = None
    await _refresh_draft_card(context)
    await update.message.reply_text("✅ Обновил. Проверь карточку выше и нажми «✅ Готово».")


async def _on_draft_button(query, context: ContextTypes.DEFAULT_TYPE, action: str):
    """Кнопки карточки-черновика. Состояние — в context.user_data['draft']."""
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("⌛ Черновик не найден. Начни операцию заново.")
        return

    if action == "draft_type":
        t = draft.get("type")
        draft["type"] = "income" if t == "expense" else "expense"   # None -> expense
        await query.edit_message_text(_render_draft(draft), reply_markup=_draft_kb())

    elif action == "draft_ask_amount":
        draft["awaiting"] = "amount"
        await query.message.reply_text("✏️ Отправь сумму числом (например: 500).")
    elif action == "draft_ask_category":
        draft["awaiting"] = "category"
        await query.message.reply_text("🏷️ Напиши категорию словом (например: еда, транспорт, зарплата).")
    elif action == "draft_ask_desc":
        draft["awaiting"] = "description"
        await query.message.reply_text("📝 Напиши короткое описание (например: обед в кафе).")

    elif action == "draft_done":
        tg_id = query.from_user.id  # <--- Достаем паспорт пользователя
        missing = []
        if draft.get("type") is None:
            missing.append("тип")
        if draft.get("amount") is None:
            missing.append("сумму")
        if missing:
            await query.edit_message_text(
                _render_draft(draft) + f"\n\n⚠️ Укажи {' и '.join(missing)} — без этого не запишу.",
                reply_markup=_draft_kb(),
            )
            return
        tx = {
            "type":        draft["type"],
            "amount":      draft["amount"],
            "category":    draft.get("category") or "другое",
            "description": draft.get("description") or "",
        }
        pending = {"transactions": [tx], "goals": [], "debts": [], "actions": []}
        raw_text = draft.get("description") or f"{tx['type']} {tx['amount']:.0f}"
        context.user_data.pop("draft", None)

        try:
            await query.edit_message_text("⏳ Записываю…")
        except Exception as e:
            logger.error(f"❌ Не смог показать «Записываю…» в черновике: {e}")

        await _commit_analysis(tg_id, query, context, pending, raw_text)

    elif action == "draft_cancel":
        context.user_data.pop("draft", None)
        await query.edit_message_text("👌 Отменил, ничего не записал.")


def _render_preview(pending: dict) -> str:
    """Человеческое описание того, что ИИ понял — для карточки подтверждения."""
    lines = []
    for t in pending.get("transactions", []):
        if t["type"] == "expense":
            lines.append(f"🛒 Расход: {t['amount']:,.0f} — {t['category']} ({t['description']})")
        else:
            lines.append(f"💰 Доход: {t['amount']:,.0f} — {t['category']} ({t['description']})")
    for d in pending.get("debts", []):
        if d["direction"] == "owe_me":
            lines.append(f"💸 Новый долг: {d['person']} должен тебе {d['amount']:,.0f}")
        else:
            lines.append(f"💸 Новый долг: ты должен {d['person']} {d['amount']:,.0f}")
    for g in pending.get("goals", []):
        lines.append(f"🎯 Новая цель: «{g['title']}» на {g['target_amount']:,.0f}")
    for a in pending.get("actions", []):
        if a["action"] == "goal_deposit":
            lines.append(f"🐷 В копилку «{a['goal_title']}»: +{a['amount']:,.0f}")
        elif a["action"] == "debt_repay":
            lines.append(f"✅ Погашение долга: {a['person']} — {a['amount']:,.0f}")
        elif a["action"] == "debt_return":
            lines.append(f"📈 Вернули долг: {a['person']} — {a['amount']:,.0f}")
        elif a["action"] == "goal_complete":
            lines.append(f"🏁 Закрыть цель «{a['goal_title']}» покупкой: −{a['amount']:,.0f}")
        elif a["action"] == "goal_withdraw":
            lines.append(f"🚪 Закрыть цель «{a['goal_title']}» без траты (деньги вернутся в свободные)")
    body = "\n".join(f"  {ln}" for ln in lines)
    return f"🤔 Я понял так:\n{body}\n\nВсё верно?"

def _is_command_intent(text: str) -> str | None:
    """Перехватчик: проверяет, не просит ли пользователь базовую команду естественным языком."""
    s = (text or "").lower()
    
    # Если в тексте есть цифры — это финансовая операция, а не команда меню
    if any(char.isdigit() for char in s):
        return None
        
    # Если текста слишком много (больше 50 символов), скорее всего это сложный запрос
    if len(s) > 50:
        return None

    if "баланс" in s or "остаток" in s: return "balance"
    if "истори" in s or "последние" in s: return "history"
    if "месяц" in s or "статистик" in s: return "month"
    if "категори" in s: return "categories"
    if "цели" in s or "копилк" in s: return "goals"
    if "долг" in s or "долж" in s or "доложен" in s: return "debts"
    if "профиль" in s: return "profile"
    
    return None

async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await _typing(update, context)
    tg_id = update.effective_user.id  # <--- Достаем ID пользователя

    # 1. FSM: Если мы находимся в состоянии заполнения черновика,
    # перехватываем ВЕСЬ текст как ответ на вопрос бота, игнорируя команды.
    draft = context.user_data.get("draft")
    if draft and draft.get("awaiting"):
        await _apply_draft_field(update, context, text)
        return

    # 2. Перехват команд естественным языком (выполняется только если мы НЕ в черновике)
    intent = _is_command_intent(text)
    if intent == "balance": return await balance(update, context)
    if intent == "history": return await history(update, context)
    if intent == "month": return await month(update, context)
    if intent == "categories": return await categories(update, context)
    if intent == "goals": return await goals(update, context)
    if intent == "debts": return await debts(update, context)
    if intent == "profile": return await profile(update, context)

    analysis = ai.analyze_message(text)

    # Профиль применяем сразу — это не деньги, подтверждать «меня зовут…» ни к чему.
    if analysis["profile"]:
        for key, value in analysis["profile"].items():
            db.set_profile(tg_id, key, value)
        logger.info(f"👤 Обновлён профиль: {analysis['profile']}")

    # Действия делим: «отмена» и «вклад в цель» — свои отдельные потоки,
    # их НЕ заворачиваем в общую карточку записи.
    write_actions = [a for a in analysis["actions"]
                     if a["action"] in ("debt_repay", "debt_return",
                                        "goal_complete", "goal_withdraw")]
    cancel_actions = [a for a in analysis["actions"] if a["action"] == "cancel"]
    deposit_actions = [a for a in analysis["actions"] if a["action"] == "goal_deposit"]

    # ВКЛАД В ЦЕЛЬ (goal_deposit) — свой поток с подтверждением и выбором цели.
    # Обрабатываем ПЕРВЫМ и ВСЕГДА карточкой (как решили). ИИ здесь НЕ вызывается,
    # поэтому бот физически не может соврать «накоплено X» до реальной записи —
    # это чинит старый баг ложного подтверждения вклада.
    if deposit_actions:
        for dep in deposit_actions:
            await _ask_deposit(update.message, context, dep["goal_title"], dep["amount"])
        if cancel_actions:
            _, _, questions = _handle_actions(cancel_actions)
            await _ask_questions(update.message, context, questions, [])
        return

    # Есть ли что записывать — то, что нужно подтвердить карточкой?
    needs_card = bool(
        analysis["transactions"] or analysis["goals"] or analysis["debts"] or write_actions
    )

    # СТРАХОВКА К8: если ИИ вернул ОБЫЧНЫЙ расход, а его описание похоже на активную
    # цель — вероятно, человек купил то, на что копил, но ИИ не распознал goal_complete.
    # Спросим отдельной кнопкой: «Это покупка по цели? Закрыть цель?». Это НЕ мешает
    # обычной карточке — если человек скажет «нет», расход запишется как есть.
    goal_hints = []
    if len(analysis["transactions"]) == 1 and not analysis["goals"] and not write_actions:
        t = analysis["transactions"][0]
        if t["type"] == "expense" and (t.get("category") not in db.NON_CASH_CATEGORIES):
            match = db.find_goal_by_title(t.get("description") or "")
            if match:
                goal_hints.append({
                    "goal_id": match["id"],
                    "goal_title": match.get("title", ""),
                    "amount": t["amount"],
                    "category": t.get("category") or "другое",
                    "saved": match.get("saved_amount") or 0,
                })

    if needs_card and not goal_hints:
        pending = {
            "transactions": analysis["transactions"],
            "goals":        analysis["goals"],
            "debts":        analysis["debts"],
            "actions":      write_actions,
        }
        token = _stash(context, {"kind": "confirm", "pending": pending, "raw_text": text})
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data=f"q:confirm_yes:{token}")],
            [InlineKeyboardButton("✏️ Исправить", callback_data=f"q:confirm_edit:{token}"),
             InlineKeyboardButton("❌ Нет",       callback_data=f"q:confirm_no:{token}")],
        ])
        await update.message.reply_text(_render_preview(pending), reply_markup=kb)

        # редкий случай: в одном сообщении и запись, и «отмени» — покажем отмену отдельно
        if cancel_actions:
            _, _, questions = _handle_actions(cancel_actions)
            await _ask_questions(update.message, context, questions, [])
        return

    # СТРАХОВКА К8: расход совпал с активной целью — показываем развилку
    # (закрыть цель покупкой ИЛИ записать как обычный расход). Один выбор,
    # без риска двойной записи.
    if goal_hints:
        h = goal_hints[0]
        raw_text = text
        pay_token = _stash(context, {
            "kind": "goal_buy", **h, "raw_text": raw_text,
        })
        saved = h["saved"]
        info = f" В копилке уже {saved:,.0f}." if saved > 0 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏁 Да, закрыть цель покупкой",
                                  callback_data=f"q:goalbuy_yes:{pay_token}")],
            [InlineKeyboardButton("🛒 Нет, обычный расход",
                                  callback_data=f"q:goalbuy_no:{pay_token}")],
        ])
        await update.message.reply_text(
            f"🤔 Похоже, ты купил то, на что копил.\n"
            f"Цель «{h['goal_title']}», покупка на {h['amount']:,.0f}.{info}\n\n"
            f"Закрыть цель этой покупкой?",
            reply_markup=kb,
        )
        if cancel_actions:
            _, _, questions = _handle_actions(cancel_actions)
            await _ask_questions(update.message, context, questions, [])
        return

    # Записывать нечего. Возможно, только «отмени».
    if cancel_actions:
        _, _, questions = _handle_actions(cancel_actions)
        await _ask_questions(update.message, context, questions, [])
        return

    # НОВОЕ: разбор пустой, но в сообщении есть СИГНАЛ операции (цифра или слово-действие).
    # Значит человек имел в виду трату/доход, но чего-то не хватило (типа или суммы).
    # НЕ зовём болтливый ИИ (он бы соврал «записал»), а поднимаем ЧЕРНОВИК на кнопках.
    if _has_operation_signal(text):
        context.user_data["draft"] = {
            "type":        _infer_type(text),
            "amount":      _parse_amount(text),
            "category":    None,
            "description": None,
            "awaiting":    None,
        }
        await _send_draft_card(update.message, context)
        return

    # Иначе — обычный разговор (или только профиль). Отвечает ИИ.
    # Иначе — обычный разговор (или только профиль). Отвечает ИИ.
    ctx = load_context(tg_id)
    chat_hist = db.get_chat_history(tg_id, 20)
    saved_summary = "обновлён профиль" if analysis["profile"] else ""
    response = ai.chat_response(
        text, chat_hist,
        ctx["profile"], ctx["stats"], ctx["monthly"], ctx["goals"], ctx["debts"],
        saved_summary=saved_summary,
    )
    db.save_message(tg_id, "user", text)
    db.save_message(tg_id, "assistant", response)
    await update.message.reply_text(response)


async def _commit_analysis(tg_id: int, query, context: ContextTypes.DEFAULT_TYPE, pending: dict, raw_text: str):
    """
    Реальная запись в базу — вызывается ТОЛЬКО после нажатия «✅ Да».
    pending = {"transactions": [...], "goals": [...], "debts": [...], "actions": [...]}.
    Профиль применяется раньше (до карточки), отмена сюда не попадает.
    """
    saved_results = []
    saved_summary_parts = []

    # 1. Обычные транзакции
    for t in pending.get("transactions", []):
        tx_id = db.save_transaction(tg_id, t["type"], t["amount"], t["category"], t["description"])
        if tx_id is not None:
            emoji = "💰" if t["type"] == "income" else "🛒"
            saved_results.append(f"{emoji} {t['amount']:,.0f} — {t['category']} ({t['description']})")
            saved_summary_parts.append(f"{t['type']} {t['amount']:.0f} ({t['category']})")
        else:
            saved_results.append(f"⚠️ Не удалось записать: {t['amount']:,.0f} — {t['category']}")

    # 2. Новые долги (двигают баланс)
    for d in pending.get("debts", []):
        line = _create_debt_with_tx(tg_id, d)
        saved_results.append(line)
        saved_summary_parts.append(f"новый долг {d['direction']} {d['person']} {d['amount']:.0f}")

    # 3. Цели (создание) — дубликаты уводим в переспрос
    goal_lines, pending_goals = _handle_goals(tg_id, pending.get("goals", []))
    saved_results.extend(goal_lines)
    for _ in goal_lines:
        saved_summary_parts.append("новая цель")

    # 4. Действия над существующим (пополнение цели / погашение / возврат долга)
    action_lines, action_summary, questions = _handle_actions(tg_id, pending.get("actions", []))
    saved_results.extend(action_lines)
    saved_summary_parts.extend(action_summary)

    # 5. Свежий контекст, история, ответ ИИ
    ctx = load_context(tg_id)
    chat_hist = db.get_chat_history(tg_id, 20)
    saved_summary = "; ".join(saved_summary_parts)
    ai_text = ai.chat_response(
        raw_text, chat_hist,
        ctx["profile"], ctx["stats"], ctx["monthly"], ctx["goals"], ctx["debts"],
        saved_summary=saved_summary,
    )

    # 6. Сохраняем диалог (ТОЛЬКО чистый текст ИИ, без технических заголовков!)
    db.save_message(tg_id, "user", raw_text)
    db.save_message(tg_id, "assistant", ai_text)

    # Формируем финальное сообщение для Telegram
    final_msg = ai_text
    if saved_results:
        final_msg = "✅ Записал:\n" + "\n".join(saved_results) + "\n\n" + ai_text

    # 7. Заменяем карточку «Я понял так…» на итог
    try:
        await query.edit_message_text(final_msg)
    except Exception as e:
        logger.error(f"❌ Не смог отредактировать карточку: {e}")
        await query.message.reply_text(final_msg)

    # 8. Переспросы (дубликаты целей + цель/долг не найдены)
    if pending_goals or questions:
        await _ask_questions(query.message, context, questions, pending_goals)

# ------------------------------------------------------------
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# ------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"📩 Сообщение: {text}")
    await process_text(update, context, text)

# ------------------------------------------------------------
# ГОЛОСОВЫЕ СООБЩЕНИЯ 🎤
# ------------------------------------------------------------
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update, context)

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
        text = ai.transcribe_voice(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if not text:
        await update.message.reply_text("❌ Не удалось распознать речь. Попробуй ещё раз.")
        return

    await update.message.reply_text(f"🎤 Распознал: «{text}»")
    await process_text(update, context, text)

# ============================================================
# ЗАПУСК
# ============================================================
def main():
    logger.info("🚀 Запускаем бота с памятью...")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       start))
    app.add_handler(CommandHandler("balance",    balance))
    app.add_handler(CommandHandler("month",      month))
    app.add_handler(CommandHandler("history",    history))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("goals",      goals))
    app.add_handler(CommandHandler("debts",      debts))
    app.add_handler(CommandHandler("profile",    profile))
    app.add_handler(CommandHandler("advice",     advice))
    app.add_handler(CommandHandler("clear",      clear_history))
    app.add_handler(CommandHandler("reset",      reset_cmd))

    # все inline-кнопки идут через единый обработчик (callback_data начинается с "q:")
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^q:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Нанимаем уборщика: запускать каждый час (3600 сек), первый раз — через 10 сек после старта
    app.job_queue.run_repeating(cleanup_old_tokens, interval=3600, first=10)

    logger.info("✅ Бот с памятью готов!")
    app.run_polling()

if __name__ == "__main__":
    main()
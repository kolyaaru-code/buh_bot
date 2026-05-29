# ============================================================
# BOT.PY — версия 2
# Память и контекст + реальное добавление целей/долгов,
# переспрос дубликатов целей кнопками, единое ядро обработки.
# ============================================================

import os
import logging
import uuid
import tempfile
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
def load_context() -> dict:
    return {
        "profile": db.get_profile(),
        "stats":   db.get_stats(),
        "monthly": db.get_monthly_stats(),
        "goals":   db.get_goals(),
        "debts":   db.get_debts(),
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
        # запасной вариант — просто первые 10 символов
        return created_at[:10]

async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает индикатор 'печатает…' вместо мусорного сообщения 'Думаю...'."""
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        pass

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
        f"«Саша должен мне 3000» 💸\n"
        f"«Меня зовут Никита, работаю дизайнером» 👤\n\n"
        f"📋 Команды:\n"
        f"/balance — баланс\n"
        f"/month — статистика за месяц\n"
        f"/history — последние записи\n"
        f"/categories — расходы по категориям\n"
        f"/goals — мои цели\n"
        f"/debts — мои долги\n"
        f"/profile — мой профиль\n"
        f"/advice — совет от ИИ\n"
        f"/clear — очистить память диалога"
    )

@owner_only
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_stats()
    if not stats:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return
    if stats["count"] == 0:
        await update.message.reply_text("📊 Записей пока нет. Просто напиши что купил!")
        return

    emoji = "🟢" if stats["balance"] > 0 else "🔴" if stats["balance"] < 0 else "⚪"
    await update.message.reply_text(
        f"📊 Общий баланс\n"
        f"{'─' * 25}\n"
        f"📥 Доходы:  {stats['income']:>12,.0f}\n"
        f"📤 Расходы: {stats['expense']:>12,.0f}\n"
        f"{'─' * 25}\n"
        f"{emoji} Баланс: {stats['balance']:>12,.0f}\n\n"
        f"📝 Всего записей: {stats['count']}"
    )

@owner_only
async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monthly = db.get_monthly_stats()
    if not monthly:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return

    emoji = "🟢" if monthly["balance"] > 0 else "🔴" if monthly["balance"] < 0 else "⚪"
    await update.message.reply_text(
        f"📅 {monthly['month']}\n"
        f"{'─' * 25}\n"
        f"📥 Доходы:  {monthly['income']:>12,.0f}\n"
        f"📤 Расходы: {monthly['expense']:>12,.0f}\n"
        f"{'─' * 25}\n"
        f"{emoji} Итого:  {monthly['balance']:>12,.0f}\n\n"
        f"📝 Операций за месяц: {monthly['count']}"
    )

@owner_only
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    transactions = db.get_last_transactions(7)
    if not transactions:
        await update.message.reply_text("📭 История пуста.")
        return

    text = "🕐 Последние записи:\n" + "─" * 25 + "\n"
    for t in transactions:
        emoji = "📥" if t["type"] == "income" else "📤"
        date = _fmt_local_date(t.get("created_at"))
        text += (
            f"{emoji} {t['amount']:,.0f} — {t['category']}\n"
            f"   📝 {t['description']}\n"
            f"   📅 {date}\n\n"
        )
    await update.message.reply_text(text)

@owner_only
async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = db.get_expenses_by_category()
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

@owner_only
async def goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    goals_list = db.get_goals()
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

@owner_only
async def debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts_list = db.get_debts()
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
        text += f"📤 Я должен (итого: {total:,.0f}):\n"
        for d in i_owe:
            due = f" (до {d['due_date']})" if d.get("due_date") else ""
            text += f"  • {d['person']}: {d['amount']:,.0f} — {d.get('description', '')}{due}\n"
        text += "\n"

    if owe_me:
        total = sum(d["amount"] for d in owe_me)
        text += f"📥 Должны мне (итого: {total:,.0f}):\n"
        for d in owe_me:
            due = f" (до {d['due_date']})" if d.get("due_date") else ""
            text += f"  • {d['person']}: {d['amount']:,.0f} — {d.get('description', '')}{due}\n"

    await update.message.reply_text(text)

@owner_only
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prof = db.get_profile()
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

@owner_only
async def advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update, context)
    ctx = load_context()
    if not ctx["stats"] or ctx["stats"]["count"] == 0:
        await update.message.reply_text("📭 Мало данных для анализа. Добавь несколько записей!")
        return

    cats = db.get_expenses_by_category()
    advice_text = ai.get_financial_advice(
        ctx["profile"], ctx["stats"], ctx["monthly"], cats, ctx["goals"], ctx["debts"]
    )
    await update.message.reply_text(f"💡 Совет:\n\n{advice_text}")

@owner_only
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_chat_history()
    await update.message.reply_text("🧹 Память диалога очищена!")

# ============================================================
# РАЗБОР ЦЕЛЕЙ
# Новые добавляем сразу. Дубликаты складываем в список для вопроса.
# Возвращает: (строки_подтверждения, список_дубликатов_для_вопроса)
# ============================================================
def _handle_goals(goals_found: list) -> tuple[list, list]:
    saved_lines = []
    pending = []  # цели-дубликаты, по которым нужен вопрос кнопками
    for g in goals_found:
        title = g["title"]
        target = g["target_amount"]
        deadline = g.get("deadline")
        existing = db.find_goal_by_title(title)
        if existing:
            pending.append({"title": title, "target": target, "deadline": deadline})
        else:
            goal_id = db.add_goal(title, target, deadline)
            if goal_id is not None:
                saved_lines.append(f"🎯 Цель «{title}» на {target:,.0f}")
    return saved_lines, pending

# ============================================================
# ОЧЕРЕДЬ ВОПРОСОВ ПРО ДУБЛИКАТЫ ЦЕЛЕЙ (кнопками)
# Данные о цели держим в context.bot_data по короткому токену,
# чтобы не превышать лимит callback_data (64 байта).
# ============================================================
async def _ask_goal_duplicates(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: list):
    for p in pending:
        token = uuid.uuid4().hex[:12]
        # сохраняем параметры цели под токеном
        context.bot_data[f"goal_{token}"] = p
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Добавить вторую", callback_data=f"gadd:{token}"),
            InlineKeyboardButton("❌ Не добавлять",   callback_data=f"gskip:{token}"),
        ]])
        await update.message.reply_text(
            f"🎯 Цель «{p['title']}» уже есть в списке.\n"
            f"Точно добавить ещё одну на {p['target']:,.0f}?",
            reply_markup=keyboard,
        )

async def goal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки 'Добавить вторую / Не добавлять'."""
    query = update.callback_query
    await query.answer()

    # доступ к кнопкам — тоже только хозяину
    if query.from_user.id != MY_TELEGRAM_ID:
        return

    try:
        action, token = query.data.split(":", 1)
    except ValueError:
        await query.edit_message_text("⚠️ Не понял кнопку.")
        return

    key = f"goal_{token}"
    p = context.bot_data.get(key)

    if p is None:
        # данные уже использованы или потерялись (например, после перезапуска бота)
        await query.edit_message_text("⌛ Кнопка устарела. Напиши цель ещё раз, если нужно.")
        return

    if action == "gadd":
        goal_id = db.add_goal(p["title"], p["target"], p.get("deadline"))
        if goal_id is not None:
            await query.edit_message_text(f"✅ Добавил вторую цель «{p['title']}» на {p['target']:,.0f}.")
        else:
            await query.edit_message_text("❌ Не получилось добавить цель, попробуй ещё раз.")
    elif action == "gskip":
        await query.edit_message_text(f"👌 Ок, не добавляю «{p['title']}».")
    else:
        await query.edit_message_text("⚠️ Неизвестное действие.")

    # чистим за собой
    context.bot_data.pop(key, None)

# ============================================================
# ЯДРО ОБРАБОТКИ ТЕКСТА — общее для текста и голоса
# ============================================================
async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    await _typing(update, context)

    # 1. Один запрос к ИИ — сразу всё: транзакции, профиль, цели, долги
    analysis = ai.analyze_message(text)

    saved_results = []  # строки-подтверждения для пользователя
    saved_summary_parts = []  # короткая сводка для ИИ (чтобы он не дублировал)

    # 2. Транзакции
    for t in analysis["transactions"]:
        success = db.save_transaction(t["type"], t["amount"], t["category"], t["description"])
        if success:
            emoji = "📥" if t["type"] == "income" else "📤"
            saved_results.append(f"{emoji} {t['amount']:,.0f} — {t['category']} ({t['description']})")
            saved_summary_parts.append(f"{t['type']} {t['amount']:.0f} ({t['category']})")
        else:
            saved_results.append(f"⚠️ Не удалось записать: {t['amount']:,.0f} — {t['category']}")

    # 3. Профиль
    if analysis["profile"]:
        for key, value in analysis["profile"].items():
            db.set_profile(key, value)
        logger.info(f"👤 Обновлён профиль: {analysis['profile']}")
        saved_summary_parts.append("обновлён профиль")

    # 4. Долги — добавляем как есть (без переспроса)
    for d in analysis["debts"]:
        success = db.add_debt(
            d["direction"], d["person"], d["amount"], d["description"], d.get("due_date")
        )
        if success:
            if d["direction"] == "i_owe":
                saved_results.append(f"💸 Ты должен {d['person']}: {d['amount']:,.0f}")
            else:
                saved_results.append(f"💰 {d['person']} должен тебе: {d['amount']:,.0f}")
            saved_summary_parts.append(f"долг {d['direction']} {d['person']} {d['amount']:.0f}")

    # 5. Цели — новые сразу, дубликаты в очередь вопросов
    goal_lines, pending_goals = _handle_goals(analysis["goals"])
    saved_results.extend(goal_lines)
    for line in goal_lines:
        saved_summary_parts.append("новая цель")

    # 6. Свежий контекст и история (баланс уже актуален после записей)
    ctx = load_context()
    history = db.get_chat_history(20)
    saved_summary = "; ".join(saved_summary_parts)

    # 7. Ответ ИИ — он НЕ дублирует "записал", только живой комментарий
    response = ai.chat_response(
        text, history,
        ctx["profile"], ctx["stats"], ctx["monthly"], ctx["goals"], ctx["debts"],
        saved_summary=saved_summary,
    )

    # 8. Подтверждение записей (наш код) + ответ ИИ
    if saved_results:
        response = "Записал:\n" + "\n".join(saved_results) + "\n\n" + response

    # 9. Сохраняем диалог
    db.save_message("user", text)
    db.save_message("assistant", response)

    await update.message.reply_text(response)

    # 10. В самом конце — вопросы про дубликаты целей (не задерживают остальное)
    if pending_goals:
        await _ask_goal_duplicates(update, context, pending_goals)

# ------------------------------------------------------------
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# ------------------------------------------------------------
@owner_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"📩 Сообщение: {text}")
    await process_text(update, context, text)

# ------------------------------------------------------------
# ГОЛОСОВЫЕ СООБЩЕНИЯ 🎤
# ------------------------------------------------------------
@owner_only
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
        # файл удаляем в любом случае — даже если распознавание упало
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

    # кнопки по дубликатам целей
    app.add_handler(CallbackQueryHandler(goal_callback, pattern=r"^g(add|skip):"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("✅ Бот с памятью готов!")
    app.run_polling()

if __name__ == "__main__":
    main()
# ============================================================
# BOT.PY — финальная версия с памятью и контекстом
# ============================================================

import os
import logging
import asyncio
import tempfile
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import database as db
import ai_helper as ai

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# ЗАЩИТА — только хозяин
# ------------------------------------------------------------
def is_owner(update: Update) -> bool:
    user_id = update.effective_user.id
    if user_id != MY_TELEGRAM_ID:
        logger.warning(f"⛔ Чужой! ID: {user_id}, Имя: {update.effective_user.full_name}")
        return False
    return True

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            return
        return await func(update, context)
    return wrapper

# ------------------------------------------------------------
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ — загружает весь контекст из БД
# Вызывается перед каждым ответом ИИ
# ------------------------------------------------------------
def load_context() -> dict:
    return {
        "profile": db.get_profile(),
        "stats":   db.get_stats(),
        "monthly": db.get_monthly_stats(),
        "goals":   db.get_goals(),
        "debts":   db.get_debts(),
    }

# ------------------------------------------------------------
# /start
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# /balance
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# /month — статистика за текущий месяц
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# /history
# ------------------------------------------------------------
@owner_only
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    transactions = db.get_last_transactions(7)
    if not transactions:
        await update.message.reply_text("📭 История пуста.")
        return

    text = "🕐 Последние записи:\n" + "─" * 25 + "\n"
    for t in transactions:
        emoji = "📥" if t["type"] == "income" else "📤"
        date = t["created_at"][:10]
        text += (
            f"{emoji} {t['amount']:,.0f} — {t['category']}\n"
            f"   📝 {t['description']}\n"
            f"   📅 {date}\n\n"
        )
    await update.message.reply_text(text)

# ------------------------------------------------------------
# /categories
# ------------------------------------------------------------
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
        bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
        text += f"{bar} {percent:.0f}%\n{cat}: {amount:,.0f}\n\n"
    await update.message.reply_text(text)

# ------------------------------------------------------------
# /goals — мои финансовые цели
# ------------------------------------------------------------
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
        target = g.get("target_amount", 0)
        saved = g.get("saved_amount", 0)
        progress = int((saved / target * 10)) if target > 0 else 0
        bar = "█" * progress + "░" * (10 - progress)
        percent = (saved / target * 100) if target > 0 else 0
        deadline = f"\n   📅 Дедлайн: {g['deadline']}" if g.get("deadline") else ""
        text += (
            f"🎯 {g['title']}\n"
            f"   {bar} {percent:.0f}%\n"
            f"   Накоплено: {saved:,.0f} из {target:,.0f}{deadline}\n\n"
        )
    await update.message.reply_text(text)

# ------------------------------------------------------------
# /debts — мои долги
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# /profile — мой профиль
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# /advice
# ------------------------------------------------------------
@owner_only
async def advice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤔 Анализирую твои финансы...")
    ctx = load_context()
    if not ctx["stats"] or ctx["stats"]["count"] == 0:
        await update.message.reply_text("📭 Мало данных для анализа. Добавь несколько записей!")
        return

    categories = db.get_expenses_by_category()
    advice_text = ai.get_financial_advice(
        ctx["profile"], ctx["stats"], ctx["monthly"], categories, ctx["goals"], ctx["debts"]
    )
    await update.message.reply_text(f"💡 Совет:\n\n{advice_text}")

# ------------------------------------------------------------
# /clear — очистить историю диалога
# ------------------------------------------------------------
@owner_only
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_chat_history()
    await update.message.reply_text("🧹 Память диалога очищена!")

# ------------------------------------------------------------
# ОБРАБОТКА ТЕКСТА — главная магия 🪄
# ------------------------------------------------------------
@owner_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    logger.info(f"📩 Сообщение: {text}")

    await update.message.reply_text("🤔 Думаю...")

    # 1. Ищем финансовые операции
    transactions = ai.parse_transaction(text)
    saved_results = []

    if transactions:
        for t in transactions:
            amount   = t.get("amount", 0)
            type_op  = t.get("type")
            category = t.get("category", "другое")
            desc     = t.get("description", text)

            success = db.save_transaction(type_op, amount, category, desc)
            await asyncio.sleep(0.5)

            if success:
                emoji = "📥" if type_op == "income" else "📤"
                saved_results.append(f"{emoji} {amount:,.0f} — {category} ({desc})")

    # 2. Ищем информацию о пользователе
    profile_info = ai.extract_profile_info(text)
    if profile_info:
        for key, value in profile_info.items():
            db.set_profile(key, value)
            await asyncio.sleep(0.3)
        logger.info(f"👤 Обновлён профиль: {profile_info}")

    # 3. Загружаем свежий контекст и историю
    ctx = load_context()
    history = db.get_chat_history(20)

    # 4. Получаем ответ ИИ с полным контекстом
    response = ai.chat_response(text, history, ctx["profile"], ctx["stats"], ctx["monthly"], ctx["goals"], ctx["debts"])

    # 5. Если были транзакции — добавляем их к ответу
    if saved_results:
        response = "Записал:\n" + "\n".join(saved_results) + "\n\n" + response

    # 6. Сохраняем диалог в базу
    db.save_message("user", text)
    db.save_message("assistant", response)

    await update.message.reply_text(response)

# ------------------------------------------------------------
# ГОЛОСОВЫЕ СООБЩЕНИЯ 🎤
# ------------------------------------------------------------
@owner_only
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎤 Слушаю...")

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    await voice_file.download_to_drive(tmp_path)
    text = ai.transcribe_voice(tmp_path)
    os.remove(tmp_path)

    if not text:
        await update.message.reply_text("❌ Не удалось распознать речь. Попробуй ещё раз.")
        return

    await update.message.reply_text(f"🎤 Распознал: «{text}»")

    # Дальше обрабатываем как обычный текст
    transactions = ai.parse_transaction(text)
    saved_results = []

    if transactions:
        for t in transactions:
            amount   = t.get("amount", 0)
            type_op  = t.get("type")
            category = t.get("category", "другое")
            desc     = t.get("description", text)

            success = db.save_transaction(type_op, amount, category, desc)
            await asyncio.sleep(0.5)

            if success:
                emoji = "📥" if type_op == "income" else "📤"
                saved_results.append(f"{emoji} {amount:,.0f} — {category} ({desc})")

    profile_info = ai.extract_profile_info(text)
    if profile_info:
        for key, value in profile_info.items():
            db.set_profile(key, value)
            await asyncio.sleep(0.3)

    ctx = load_context()
    history = db.get_chat_history(20)
    response = ai.chat_response(text, history, ctx["profile"], ctx["stats"], ctx["monthly"], ctx["goals"], ctx["debts"])

    if saved_results:
        response = "Записал:\n" + "\n".join(saved_results) + "\n\n" + response

    db.save_message("user", text)
    db.save_message("assistant", response)

    await update.message.reply_text(response)

# ------------------------------------------------------------
# ЗАПУСК
# ------------------------------------------------------------
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    logger.info("✅ Бот с памятью готов!")
    app.run_polling()

if __name__ == "__main__":
    main()
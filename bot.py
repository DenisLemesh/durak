"""
bot.py — Telegram бот для игры Дурак
Запуск: python bot.py
"""
import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from database import upsert_user

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name if user else "Игрок"

    if user:
        upsert_user(
            tg_id=user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
        )

    keyboard = [[
        InlineKeyboardButton(
            "🃏 Играть в Дурака",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "♠ *Дурак* — классическая карточная игра.\n\n"
        "Нажми кнопку ниже чтобы начать играть:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "♠ *Правила Дурака:*\n\n"
        "1. Атакующий кладёт карту на стол\n"
        "2. Защитник отбивает старшей картой той же масти или козырем\n"
        "3. Нельзя отбить — берёшь все карты\n"
        "4. После хода добирают до 6 карт\n"
        "5. Кто последний с картами — *Дурак!*\n\n"
        "/start — начать игру",
        parse_mode="Markdown"
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    print("✅ Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()

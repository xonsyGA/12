import asyncio
import datetime
import json
import logging
import math
import os
import random
import re
import sqlite3
import string
import threading
import time
from datetime import timedelta
from decimal import Decimal

import aiohttp
import aiosqlite
import psutil
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command  # ✅ aiogram 2

from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardMarkup, KeyboardButton
)

from telethon import TelegramClient
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.types import (InputReportReasonChildAbuse,
                               InputReportReasonIllegalDrugs,
                               InputReportReasonOther,
                               InputReportReasonPersonalDetails,
                               InputReportReasonPornography,
                               InputReportReasonSpam,
                               InputReportReasonViolence)

# Конфигурация
API_TOKEN = "8182013176:AAFLIYlklQ4apVA_9Vfxd3RMWXyKmtQeV9o"  # Замените на токен вашего бота
CRYPTO_PAY_TOKEN = "383398:AAhdyCfw36AysWXq2DZzVP2YPsQAAaaMBnt"  # Замените на токен Crypto Pay
API_ID = 26484445  # Замените на ваш API ID Telegram
API_HASH = "1181da6642bfbd3bd7275754d3f16a30"  # Замените на ваш API Hash Telegram

ADMINS = [1398436088, 7708264491]  # Замените на ID Telegram администраторов
LOG_GROUP_ID = -1002405386039  # Замените на ID группы для логов (с минусом)
CHANNEL_ID = -1002405386039  # Замените на ID вашего канала (с минусом)
LOG_CHAT_ID = -1002405386039  # Замените на ID группы для логов (должно совпадать с LOG_GROUP_ID)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Цены и длительности подписки
SUBSCRIPTION_PRICES = {
    "day": 1.0,
    "week": 3.0,
    "month": 4.5,
    "forever": 6.0
}
SUBSCRIPTION_DURATIONS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "forever": 3650,  # Очень большое число для "навсегда"
}

# Причины репортов
REPORT_REASONS = {
    "spam": (InputReportReasonSpam(), "Сообщение содержит спам"),
    "violence": (InputReportReasonViolence(), "Сообщение содержит насилие"),
    "pornography": (InputReportReasonPornography(), "Сообщение содержит порнографию"),
    "child_abuse": (
        InputReportReasonChildAbuse(),
        "Сообщение содержит жестокое обращение с детьми",
    ),
    "illegal_drugs": (
        InputReportReasonIllegalDrugs(),
        "Сообщение содержит незаконные материалы (наркотики)",
    ),
    "personal_details": (
        InputReportReasonPersonalDetails(),
        "Сообщение содержит личные данные",
    ),
    "other": (InputReportReasonOther(), "Сообщение нарушает правила"),
}

# Состояния
class Form(StatesGroup):
    demolition_link = State()
    report_reason = State()
    block_user_id = State()
    add_subscription_id = State()
    add_subscription_duration = State()
    remove_subscription_id = State()
    broadcast_message = State()
    support_message = State()
    in_support = State()
    payment_verification = State()
    promo_code = State()
    create_promo_code = State()
    create_promo_duration = State()
    create_promo_uses = State()

# --- Database Functions ---
DATABASE_NAME = "database.db"

def get_db_connection():
    conn = sqlite3.connect(DATABASE_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        subscription_status TEXT DEFAULT 'none',
        subscription_expiry_date TEXT DEFAULT NULL,
        is_blocked INTEGER DEFAULT 0,
        demolition_count INTEGER DEFAULT 0,
        last_demolition INTEGER DEFAULT 0
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS promocodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        duration_days INTEGER NOT NULL,
        uses_left INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def register_user_in_db(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (telegram_id) VALUES (?)", (user_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def activate_subscription(user_id, duration):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT subscription_expiry_date FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    current_expiry_date_str = result[0] if result else None

    if current_expiry_date_str:
        try:
            current_expiry_date = datetime.datetime.strptime(current_expiry_date_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            current_expiry_date = datetime.datetime.strptime(current_expiry_date_str, '%Y-%m-%d')
        if current_expiry_date > datetime.datetime.now():
            new_expiry_date = current_expiry_date + timedelta(days=duration)
        else:
            new_expiry_date = datetime.datetime.now() + timedelta(days=duration)
    else:
        new_expiry_date = datetime.datetime.now() + timedelta(days=duration)

    expiry_date = new_expiry_date.strftime('%Y-%m-%d %H:%M:%S') if duration else None

    cursor.execute("""
        UPDATE users
        SET subscription_status = 'active',
            subscription_expiry_date = ?
        WHERE telegram_id = ?
    """, (expiry_date, user_id))
    
    conn.commit()
    conn.close()

def check_subscription(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT subscription_status, subscription_expiry_date FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        status, expiry_date = result
        if status == 'active':
            if expiry_date is None:
                return True
            try:
                expiry_datetime = datetime.datetime.strptime(expiry_date, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                expiry_datetime = datetime.datetime.strptime(expiry_date, '%Y-%m-%d')
            
            return expiry_datetime >= datetime.datetime.now()
    return False

# --- Promo Code Functions ---
def validate_promo_code(code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, duration_days, uses_left FROM promocodes WHERE code = ?", (code,))
    result = cursor.fetchone()
    conn.close()
    if result:
        promo_id, duration_days, uses_left = result
        return promo_id, duration_days, uses_left
    return None, None, None

def apply_promo_code(user_id, promo_id, duration_days):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT uses_left FROM promocodes WHERE id = ?", (promo_id,))
    result = cursor.fetchone()
    if not result or result[0] <= 0:
        conn.close()
        return False, "Промокод больше не действителен."

    activate_subscription(user_id, duration_days)

    cursor.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE id = ?", (promo_id,))
    conn.commit()
    conn.close()
    return True, None

# --- Handlers ---
@dp.message_handler(Command("start"), state="*")
async def start_command(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    await state.finish()
    register_user_in_db(user_id)

    try:
        await bot.send_sticker(
            chat_id=message.chat.id,
            sticker="CAACAgIAAxkBAAEMsY9ntNuvDYfY3nT5BzLrOGINNPRpCwAC-wADVp29ClYO2zPbysnmNgQ"
        )

        main_menu = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton("📜 Профиль", callback_data="profile"),
            InlineKeyboardButton("💳 Подписка", callback_data="buy"),
            InlineKeyboardButton("🎁 Промокод", callback_data="promo"),
            InlineKeyboardButton("ℹ️ О Боте", callback_data="about_bot"),
            InlineKeyboardButton("🚨 Сносер", callback_data="demolition")
        )

        if user_id in ADMINS:
            main_menu.add(InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel"))

        with open("banner.png", "rb") as banner:
            await bot.send_photo(
                message.chat.id,
                banner,
                caption="🎉 Добро пожаловать в бота!\n\n⚡ Выберите опцию:",
                reply_markup=main_menu,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка при проверке подписки: {e}")
        await message.answer("❌ Произошла ошибка при проверке подписки.")

@dp.callback_query_handler(lambda c: c.data == "promo")
async def promo_command(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.send_message(callback_query.message.chat.id, "Введите промокод:")
    await Form.promo_code.set()

@dp.message_handler(state=Form.promo_code)
async def process_promo_code(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    promo_code = message.text.strip()

    promo_id, duration_days, uses_left = validate_promo_code(promo_code)

    if promo_id:
        success, error_message = apply_promo_code(user_id, promo_id, duration_days)
        if success:
            await message.answer(
                f"✅ Промокод активирован! Вам предоставлена подписка на {duration_days} дней.",
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
            )
            await bot.send_message(
                LOG_CHAT_ID,
                f"🎁 Пользователь {user_id} активировал промокод {promo_code} на {duration_days} дней.",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"❌ {error_message}",
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
            )
    else:
        await message.answer(
            "❌ Неверный промокод.",
            reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
        )

    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "buy")
async def buy(call: types.CallbackQuery):
    markup = InlineKeyboardMarkup()
    subscription_options = [
        ("💊 1 день - 1$", "buy_3"),
        ("💊 7 дней - 3$", "buy_7"),
        ("💊 Месяц - 6$", "buy_31"),
        ("💊 Навсегда - 15$", "forever")
    ]
    for option_text, callback_data in subscription_options:
        markup.add(InlineKeyboardButton(option_text, callback_data=callback_data))

    markup.add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
    
    await bot.edit_message_caption(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        caption="<b>💎 Оплата через @send\n⌛️ Выберите срок подписки:</b>",
        reply_markup=markup,
        parse_mode="HTML"
    )

async def generate_payment_link(payment_system, amount):
    api_url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    data = {
        "asset": payment_system,
        "amount": float(amount)
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, headers=headers, json=data) as response:
                if response.status == 200:
                    json_data = await response.json()
                    invoice = json_data.get("result")
                    payment_link = invoice.get("pay_url")
                    invoice_id = invoice.get("invoice_id")
                    return payment_link, invoice_id
                else:
                    logger.error(f"Error creating invoice: {response.status}")
                    return None, None
    except Exception as e:
        logger.error(f"Exception in generate_payment_link: {e}")
        return None, None

async def get_invoice_status(invoice_id):
    api_url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                if response.status == 200:
                    json_data = await response.json()
                    if json_data.get("ok"):
                        invoices = json_data.get("result")
                        if invoices and 'items' in invoices and invoices['items']:
                            status = invoices['items'][0]['status']
                            payment_link = invoices['items'][0]['pay_url']
                            amount = Decimal(str(invoices['items'][0]['amount']))
                            value = invoices['items'][0]['asset']
                            return status, payment_link, amount, value
                else:
                    logger.error(f"Error getting invoice status: {response.status}")
    except Exception as e:
        logger.error(f"Exception in get_invoice_status: {e}")
    return None, None, None, None

async def get_exchange_rates():
    api_url = "https://pay.crypt.bot/api/getExchangeRates"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                if response.status == 200:
                    json_data = await response.json()
                    if json_data.get("ok"):
                        return json_data["result"]
                else:
                    logger.error(f"Error getting exchange rates: {response.status}")
    except Exception as e:
        logger.error(f"Exception in get_exchange_rates: {e}")
    return []

async def convert_to_crypto(amount, asset):
    rates = await get_exchange_rates()
    rate = None
    for exchange_rate in rates:
        if exchange_rate["source"] == asset and exchange_rate["target"] == 'USD':
            rate = Decimal(str(exchange_rate["rate"]))
            break

    if rate is None:
        raise Exception(f"<b>🎲 Не удалось найти курс обмена для {asset}</b>")

    amount = Decimal(str(amount))
    return amount / rate

@dp.callback_query_handler(lambda c: c.data.startswith("buy_"))
async def subscription_duration_selected(call: types.CallbackQuery):
    duration = call.data
    markup = InlineKeyboardMarkup()
    currency_options = [
        ("💵 USDT", "currency_USDT_" + duration),
        ("💎 TON", "currency_TON_" + duration),
        ("💰 NOT", "currency_NOT_" + duration),
        ("🪙 BTC", "currency_BTC_" + duration),
        ("💶 ETH", "currency_ETH_" + duration)
    ]
    for option_text, callback_data in currency_options:
        markup.add(InlineKeyboardButton(option_text, callback_data=callback_data))

    markup.add(InlineKeyboardButton("❌ Назад", callback_data="buy"))
    
    await bot.edit_message_caption(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        caption="<b>💸 Выберите валюту для оплаты:</b>",
        reply_markup=markup,
        parse_mode="HTML"
    )

@dp.callback_query_handler(lambda c: c.data.startswith("currency_"))
async def currency_selected(call: types.CallbackQuery):
    parts = call.data.split('_')
    currency = parts[1]
    duration_parts = parts[2:]
    duration = "_".join(duration_parts)

    amount = get_amount_by_duration(duration.replace('buy_', ''))

    try:
        converted_amount = await convert_to_crypto(amount, currency)
        payment_link, invoice_id = await generate_payment_link(currency, converted_amount)
        if payment_link and invoice_id:
            markup = InlineKeyboardMarkup()
            oplata = InlineKeyboardButton("💰 Оплатить", url=f"{payment_link}")
            check_payment_button = InlineKeyboardButton("💸 Проверить оплату", callback_data=f"check_payment:{call.from_user.id}:{invoice_id}")
            markup.add(oplata, check_payment_button)
            
            markup.add(InlineKeyboardButton("❌ Назад", callback_data=duration))
            
            await bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption="<b>💸 Счет для оплаты:</b>",
                reply_markup=markup,
                parse_mode="HTML"
            )
        else:
            await bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption="<b>❌ Не удалось создать счет для оплаты. Пожалуйста, попробуйте позже.</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        await bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=str(e),
            parse_mode="HTML"
        )

def get_amount_by_duration(duration):
    prices = {
        '3': 1.0,
        '7': 3.0,
        '31': 6.0,
        'forever': 15.0
    }
    return prices.get(duration, 0)

@dp.callback_query_handler(lambda c: c.data.startswith("check_payment:"))
async def check_payment(call: types.CallbackQuery):
    _, user_id_str, invoice_id_str = call.data.split(':')
    user_id = int(user_id_str)
    invoice_id = invoice_id_str

    if user_id == call.from_user.id:
        status, payment_link, amount, value = await get_invoice_status(invoice_id)

        if status == "paid":
            duration_days = get_duration_by_amount(amount)

            if duration_days > 0:
                activate_subscription(user_id, duration_days)

                await bot.send_message(
                    LOG_CHAT_ID,
                    f"<b>💸 Пользователь {user_id} оплатил подписку.\n"
                    f"Количество дней: {duration_days}\n"
                    f"Цена: {amount} {value}</b>",
                    parse_mode="HTML"
                )

                await bot.edit_message_caption(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    caption="<b>✅ Оплата подтверждена! Подписка активирована. Спасибо за покупку.</b>",
                    parse_mode="HTML"
                )
            else:
                await bot.edit_message_caption(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    caption="<b>❌ Ошибка. Недопустимая сумма оплаты.</b>",
                    parse_mode="HTML"
                )
        else:
            await bot.answer_callback_query(call.id, "❌ Оплата не найдена. Попробуйте позже!")
    else:
        await bot.answer_callback_query(call.id, "❌ Вы не можете проверить эту оплату.", show_alert=True)

def get_duration_by_amount(amount):
    amount = round(float(amount), 2)
    if amount <= 1:
        return 1
    elif amount <= 3:
        return 7
    elif amount <= 5:
        return 30
    elif amount <= 10:
        return 3650
    else:
        return 0

@dp.callback_query_handler(lambda c: c.data == "about_bot")
async def about_bot(callback_query: types.CallbackQuery):
    about_text = (
        "❄️ <b>Добро пожаловать в нашего бота!</b> \n\n"
        "<b>🔹 Что вы можете найти здесь?</b>\n"
        "— Уникальные функции.\n"
        "— Подписки и возможности.\n"
        "— Полезные инструменты.\n\n"
        "<b>📌 Контакты:</b>\n"
        "🔹 Ворки: позже добавим\n"
        "🔹 Овнер: @haustet\n"
        "🔹 Ахуенный кодер: @L0_ng\n"
        "🔹 Тех. Поддержка: @haustet\n\n"
        "❤️ Мы всегда готовы помочь вам!"
    )

    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="admin_panel"))

    try:
        with open("banner.png", "rb") as banner:
            await bot.send_photo(
                callback_query.message.chat.id,
                banner,
                caption=about_text,
                parse_mode="HTML",
                reply_markup=markup
            )
    except FileNotFoundError:
        await bot.send_message(callback_query.message.chat.id, "Error: banner.png not found.")

report_reason_menu = InlineKeyboardMarkup(row_width=1)
report_reason_menu.add(
    InlineKeyboardButton(text="Спам", callback_data="report_reason_spam"),
    InlineKeyboardButton(text="Насилие", callback_data="report_reason_violence"),
    InlineKeyboardButton(text="Порнография", callback_data="report_reason_pornography"),
    InlineKeyboardButton(text="Жестокое обращение с детьми", callback_data="report_reason_child_abuse"),
    InlineKeyboardButton(text="Наркотики", callback_data="report_reason_illegal_drugs"),
    InlineKeyboardButton(text="Личные данные", callback_data="report_reason_personal_details"),
    InlineKeyboardButton(text="Другое", callback_data="report_reason_other")
)

@dp.callback_query_handler(lambda c: c.data == "demolition")
async def demolition_command(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id

    if not check_subscription(user_id):
        await callback_query.message.answer("❌ У вас нет активной подписки.\n⚡ Приобретите ее в магазине.")
        await callback_query.answer()
        return

    await state.finish()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT last_demolition FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    last_demolition_time = result[0] if result else 0

    current_time = int(time.time())
    cooldown_time = 300

    if current_time - last_demolition_time < cooldown_time:
        remaining_time = cooldown_time - (current_time - last_demolition_time)
        await callback_query.message.answer(
            f"❌ Вы уже использовали бота недавно!\n⏳ Подождите {remaining_time // 60} минут и {remaining_time % 60} секунд перед повторным применением бота."
        )
        await callback_query.answer()
        return

    cursor.execute("UPDATE users SET last_demolition = ? WHERE telegram_id = ?", (current_time, user_id))
    conn.commit()
    conn.close()

    try:
        with open("profile_banner.png", "rb") as profile_banner:
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
            await bot.edit_message_media(
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                media=types.InputMediaPhoto(profile_banner, caption="*Отправьте ссылку на сообщение для отправки жалоб:*", parse_mode="Markdown"),
                reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("Отмена", callback_data="cancel_demolition"))
            )
    except FileNotFoundError:
        await callback_query.message.answer("⚠️ Файл с баннером не найден. Проверьте наличие 'profile_banner.png'.")

    await Form.demolition_link.set()
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "cancel_demolition", state="*")
async def cancel_demolition(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await callback_query.message.answer("Действие отменено.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu")))

@dp.message_handler(state=Form.demolition_link)
async def handle_demolition_link(message: types.Message, state: FSMContext):
    link = message.text.strip()

    if not re.match(r'https://t.me/\w+/\d+', link):
        await message.answer("❌ Неверный формат ссылки. Попробуйте снова.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu")))
        return

    await state.update_data(demolition_link=link)
    await message.answer("Выберите причину жалобы:", reply_markup=report_reason_menu)
    await Form.report_reason.set()

reason_mapping = {
    "report_reason_spam": InputReportReasonSpam(),
    "report_reason_violence": InputReportReasonViolence(),
    "report_reason_pornography": InputReportReasonPornography(),
    "report_reason_child_abuse": InputReportReasonChildAbuse(),
    "report_reason_illegal_drugs": InputReportReasonIllegalDrugs(),
    "report_reason_personal_details": InputReportReasonPersonalDetails(),
    "report_reason_other": InputReportReasonOther()
}

@dp.callback_query_handler(lambda c: c.data.startswith("report_reason_"), state=Form.report_reason)
async def handle_report_reason(callback_query: types.CallbackQuery, state: FSMContext):
    reason_code = callback_query.data
    reason = reason_mapping.get(reason_code)

    if reason is None:
        await callback_query.message.answer("❌ Неизвестная причина жалобы.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu")))
        await state.finish()
        return

    data = await state.get_data()
    link = data.get("demolition_link")

    start_message = await callback_query.message.answer("🚀 Начинаю отправку жалоб...")

    success_count, failure_count = await handle_demolition(link, callback_query.from_user.id, reason)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_demolition = ? WHERE telegram_id = ?", (int(time.time()), callback_query.from_user.id))
    conn.commit()
    conn.close()

    await start_message.delete()

    report_text = (
        "🚨 Жалобы успешно отправлены!\n\n"
        f"✅ Успешно: {success_count}\n"
        f"❌ Не Успешно: {failure_count}\n\n⚡Благодарим за вашу активность."
    )
    await callback_query.message.answer(report_text, reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu")))

    await bot.send_message(
        LOG_GROUP_ID,
        f"👤 Пользователь @{callback_query.from_user.username or callback_query.from_user.id}\n🩸 Запустил отправку жалоб на {link}\n"
        f"⚡ Причина: {reason_code[14:].capitalize()}\n"
        f"✅ Успешно: {success_count}\n"
        f"❌ Неудачно: {failure_count}"
    )

    await state.finish()

async def process_message_link(link):
    try:
        match = re.match(r'https://t.me/(\w+)/(\d+)', link)
        if not match:
            raise ValueError("Неверный формат ссылки.")
        channel_username, message_id = match.groups()
        return channel_username, int(message_id)
    except Exception as e:
        logger.error(f"Ошибка обработки ссылки: {e}")
        return None, None

async def report_message(client, chat, msg_id, reason):
    try:
        await client(ReportRequest(
            peer=chat,
            id=[msg_id],
            reason=reason,
            message="Сообщение нарушает правила"
        ))
        logger.info(f"Жалоба отправлена: {msg_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке жалобы: {e}")
        return False

async def handle_demolition(link, user_id, reason):
    success_count = 0
    failure_count = 0
    invalid_sessions = []

    complaints_per_account = 1

    channel_username, message_id = await process_message_link(link)
    if not channel_username:
        return success_count, failure_count

    async def process_session(session_file):
        nonlocal success_count, failure_count
        client = TelegramClient(f"sessions/{session_file}", API_ID, API_HASH)
        try:
            await client.connect()
            await client.get_me()
            channel = await client.get_entity(channel_username)

            for _ in range(complaints_per_account):
                if await report_message(client, channel, message_id, reason):
                    success_count += 1
                else:
                    failure_count += 1
        except Exception as e:
            logger.error(f"Ошибка с аккаунтом {session_file}: {e}")
            failure_count += 1

            if "The user has been deleted/deactivated" in str(e) or "USER_DEACTIVATED" in str(e):
                invalid_sessions.append(session_file)
        finally:
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Ошибка при отключении клиента {session_file}: {e}")

    tasks = []
    for session_file in os.listdir("sessions"):
        if session_file.endswith(".session"):
            tasks.append(process_session(session_file))

    await asyncio.gather(*tasks)

    for session_file in invalid_sessions:
        try:
            os.remove(f"sessions/{session_file}")
            logger.info(f"Удалена недействительная сессия: {session_file}")
        except Exception as e:
            logger.error(f"Ошибка при удалении недействительной сессии {session_file}: {e}")

    return success_count, failure_count

# --- Admin Panel Handlers ---
admin_panel_keyboard = InlineKeyboardMarkup(row_width=1)
admin_panel_keyboard.add(
    InlineKeyboardButton("➕ Добавить письку", callback_data="add_subscription"),
    InlineKeyboardButton("➖ Удалить письку", callback_data="remove_subscription"),
    InlineKeyboardButton("➕ Создать порно", callback_data="create_promo"),
    InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data="block_user"),
    InlineKeyboardButton("✅ Разблокировать пользователя", callback_data="unblock_user"),
    InlineKeyboardButton("📢 Рассылка", callback_data="broadcast"),
    InlineKeyboardButton("📊 Сиська", callback_data="statistics"),
    InlineKeyboardButton("❌ Назад", callback_data="back_to_menu")
)

@dp.callback_query_handler(lambda c: c.data == "admin_panel")
async def admin_panel(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.edit_message_caption(
            chat_id=callback_query.message.chat.id,
            message_id=callback_query.message.message_id,
            caption="🛠️ Админ-панель",
            reply_markup=admin_panel_keyboard,
            parse_mode="HTML"
        )
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.callback_query_handler(lambda c: c.data == "add_subscription")
async def add_subscription_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите Telegram ID пользователя, которому нужно добавить подписку:")
        await Form.add_subscription_id.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.add_subscription_id)
async def process_add_subscription_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await state.update_data(add_subscription_user_id=user_id)
        subscription_options_keyboard = InlineKeyboardMarkup(row_width=1)
        subscription_options_keyboard.add(
            InlineKeyboardButton("1 день", callback_data="add_subscription_duration_day"),
            InlineKeyboardButton("7 дней", callback_data="add_subscription_duration_week"),
            InlineKeyboardButton("30 дней", callback_data="add_subscription_duration_month"),
            InlineKeyboardButton("Навсегда", callback_data="add_subscription_duration_forever"),
            InlineKeyboardButton("Отмена", callback_data="admin_panel")
        )
        await message.answer("Выберите срок подписки:", reply_markup=subscription_options_keyboard)
        await Form.add_subscription_duration.set()
    except ValueError:
        await message.answer("Неверный формат Telegram ID. Введите число.")
    except Exception as e:
        await message.answer(f"Произошла ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith("add_subscription_duration_"), state=Form.add_subscription_duration)
async def process_add_subscription_duration(callback_query: types.CallbackQuery, state: FSMContext):
    duration = callback_query.data.split("_")[-1]
    duration_days = SUBSCRIPTION_DURATIONS.get(duration)
    if duration_days is None:
        await callback_query.answer("Неверный срок подписки.")
        return

    data = await state.get_data()
    user_id = data.get("add_subscription_user_id")

    if user_id is None:
        await callback_query.answer("Не найден Telegram ID пользователя.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user_id,))
    user_exists = cursor.fetchone()
    conn.close()

    if not user_exists:
        await callback_query.answer("Пользователь с таким Telegram ID не найден.")
        await state.finish()
        return
    
    activate_subscription(user_id, duration_days)
    
    await bot.send_message(
        LOG_CHAT_ID,
        f"➕ Администратор {callback_query.from_user.id} добавил подписку пользователю {user_id} на {duration_days} дней.",
        parse_mode="HTML"
    )

    await callback_query.message.answer(f"Подписка на {duration_days} дней успешно добавлена пользователю {user_id}.", reply_markup=admin_panel_keyboard)
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "remove_subscription")
async def remove_subscription_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите Telegram ID пользователя, у которого нужно удалить подписку:")
        await Form.remove_subscription_id.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.remove_subscription_id)
async def process_remove_subscription(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users
            SET subscription_status = 'none',
                subscription_expiry_date = NULL
            WHERE telegram_id = ?
        """, (user_id,))
        conn.commit()
        conn.close()
        
        await bot.send_message(
            LOG_CHAT_ID,
            f"➖ Администратор {message.from_user.id} удалил подписку у пользователя {user_id}.",
            parse_mode="HTML"
        )

        await message.answer(f"Подписка успешно удалена у пользователя {user_id}.", reply_markup=admin_panel_keyboard)
        await state.finish()
    except ValueError:
        await message.answer("Неверный формат Telegram ID. Введите число.")
    except Exception as e:
        await message.answer(f"Произошла ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data == "create_promo")
async def create_promo_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите промокод:")
        await Form.create_promo_code.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.create_promo_code)
async def process_promo_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    await state.update_data(code=code)
    await bot.send_message(message.chat.id, "Введите срок действия промокода в днях:")
    await Form.create_promo_duration.set()

@dp.message_handler(state=Form.create_promo_duration)
async def process_promo_duration(message: types.Message, state: FSMContext):
    try:
        duration = int(message.text)
        if duration <= 0:
            await message.answer("Срок действия должен быть положительным числом.")
            return
        await state.update_data(duration=duration)
        await bot.send_message(message.chat.id, "Введите количество использований промокода:")
        await Form.create_promo_uses.set()
    except ValueError:
        await message.answer("Неверный формат срока действия. Введите число.")

@dp.message_handler(state=Form.create_promo_uses)
async def process_promo_uses(message: types.Message, state: FSMContext):
    try:
        uses = int(message.text)
        if uses <= 0:
            await message.answer("Количество использований должно быть положительным числом.")
            return
        await state.update_data(uses=uses)

        data = await state.get_data()
        code = data.get("code")
        duration = data.get("duration")
        uses = data.get("uses")

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO promocodes (code, duration_days, uses_left) VALUES (?, ?, ?)", (code, duration, uses))
            conn.commit()
            await bot.send_message(message.chat.id, f"Промокод {code} успешно создан на {duration} дней и {uses} использований.", reply_markup=admin_panel_keyboard)
            await bot.send_message(
                LOG_CHAT_ID,
                f"Администратор {message.from_user.id} создал промокод {code} на {duration} дней и {uses} использований.",
                parse_mode="HTML"
            )
        except sqlite3.IntegrityError:
            await message.answer("Промокод с таким кодом уже существует.")
        finally:
            conn.close()

        await state.finish()
    except ValueError:
        await message.answer("Неверный формат количества использований. Введите число.")

@dp.callback_query_handler(lambda c: c.data == "block_user")
async def block_user_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите Telegram ID пользователя, которого нужно заблокировать:")
        await Form.block_user_id.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.block_user_id)
async def process_block_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_blocked = 1 WHERE telegram_id = ?", (user_id,))
        conn.commit()
        conn.close()
        
        await bot.send_message(
            LOG_CHAT_ID,
            f"Администратор {message.from_user.id} заблокировал пользователя {user_id}.",
            parse_mode="HTML"
        )
        
        await message.answer(f"Пользователь {user_id} успешно заблокирован.", reply_markup=admin_panel_keyboard)
        await state.finish()
    except ValueError:
        await message.answer("Неверный формат Telegram ID. Введите число.")
    except Exception as e:
        await message.answer(f"Произошла ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data == "unblock_user")
async def unblock_user_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите Telegram ID пользователя, которого нужно разблокировать:")
        await Form.block_user_id.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.block_user_id)
async def process_unblock_user(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_blocked = 0 WHERE telegram_id = ?", (user_id,))
        conn.commit()
        conn.close()

        await bot.send_message(
            LOG_CHAT_ID,
            f"Администратор {message.from_user.id} разблокировал пользователя {user_id}.",
            parse_mode="HTML"
        )
        
        await message.answer(f"Пользователь {user_id} успешно разблокирован.", reply_markup=admin_panel_keyboard)
        await state.finish()
    except ValueError:
        await message.answer("Неверный формат Telegram ID. Введите число.")
    except Exception as e:
        await message.answer(f"Произошла ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data == "broadcast")
async def broadcast_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        await bot.send_message(callback_query.message.chat.id, "Введите сообщение для рассылки:")
        await Form.broadcast_message.set()
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.message_handler(state=Form.broadcast_message)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    broadcast_message = message.text
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id FROM users")
    user_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    success_count = 0
    failure_count = 0
    
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, broadcast_message)
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения пользователю {user_id}: {e}")
            failure_count += 1

    await bot.send_message(
        LOG_CHAT_ID,
        f"Администратор {message.from_user.id} отправил рассылку.\nУспешно: {success_count}\nНеудачно: {failure_count}",
        parse_mode="HTML"
    )
        
    await message.answer(f"Рассылка завершена.\nУспешно отправлено: {success_count}\nНеудачно отправлено: {failure_count}", reply_markup=admin_panel_keyboard)
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == "statistics")
async def statistics_command(callback_query: types.CallbackQuery):
    if callback_query.from_user.id in ADMINS:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE subscription_status = 'active'")
        active_subscriptions = cursor.fetchone()[0]
        conn.close()

        cpu_usage = psutil.cpu_percent(interval=1)
        ram_usage = psutil.virtual_memory().percent

        session_count = len([f for f in os.listdir("sessions") if f.endswith(".session")])
        
        statistics_message = (
            "📊 Статистика бота:\n\n"
            f"Всего пользователей: {total_users}\n"
            f"Активных подписок: {active_subscriptions}\n\n"
            f"CPU Usage: {cpu_usage}%\n"
            f"RAM Usage: {ram_usage}%\n\n"
            f"Аккаунтов в работе: {session_count}"
        )
        
        await bot.send_message(callback_query.message.chat.id, statistics_message, reply_markup=admin_panel_keyboard)
    else:
        await callback_query.answer("У вас нет прав для выполнения этой команды.")

@dp.callback_query_handler(lambda c: c.data == "profile")
async def profile_command(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT subscription_status, subscription_expiry_date FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        status, expiry_date = result
        if status == 'active':
            expiry_date_str = expiry_date
            if expiry_date_str is None:
                expiry_message = "Подписка навсегда"
            else:
                expiry_datetime = datetime.datetime.strptime(expiry_date_str, '%Y-%m-%d %H:%M:%S')
                expiry_message = expiry_datetime.strftime('%d.%m.%Y %H:%M:%S')
            status_message = f"{expiry_message}"
        else:
            status_message = "отсутствует"
    else:
        status_message = "Не зарегистрирован"

    profile_text = (
        "👤 Ваш профиль:\n\n"
        f"ID: {user_id}\n"
        f"Подписка до: {status_message}\n\nБлагодарим вас за то что выбираете нас."
    )
    
    try:
        with open("profile_banner.png", "rb") as profile_banner:
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Назад", callback_data="back_to_menu"))
            await bot.edit_message_media(
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                media=types.InputMediaPhoto(profile_banner, caption=profile_text, parse_mode="Markdown"),
                reply_markup=markup
            )
    except FileNotFoundError:
        await callback_query.message.answer("Файл с баннером не найден. Проверьте наличие 'profile_banner.png'.")

@dp.callback_query_handler(lambda c: c.data == "back_to_menu", state="*")
async def back_to_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await state.finish()
    user_id = callback_query.from_user.id
    
    main_menu = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        InlineKeyboardButton("💳 Подписка", callback_data="buy"),
        InlineKeyboardButton("🎁 Промокод", callback_data="promo"),
        InlineKeyboardButton("ℹ️ О Боте", callback_data="about_bot"),
        InlineKeyboardButton("🚨 Сносер", callback_data="demolition")
    )


    if user_id in ADMINS:
        main_menu.add(InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel"))

    try:
        with open("banner.png", "rb") as banner:
            await bot.edit_message_media(
                chat_id=callback_query.message.chat.id,
                message_id=callback_query.message.message_id,
                media=types.InputMediaPhoto(banner, caption="🎉 Добро пожаловать в бота!\n\n⚡ Выберите опцию:", parse_mode="Markdown"),
                reply_markup=main_menu
            )
    except FileNotFoundError:
        await callback_query.message.answer("Файл с баннером не найден. Проверьте наличие 'banner.png'.")

async def on_startup(dp):
    init_db()
    logger.info("Database initialized.")

if __name__ == '__main__':
    async def main():
        await on_startup(dp)
        await dp.start_polling()

    asyncio.run(main())
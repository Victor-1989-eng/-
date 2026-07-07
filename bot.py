import os
import io
import csv
import json
import logging
import aiohttp
import pandas as pd
import requests
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from threading import Thread
from flask import Flask

# Включаем логирование
logging.basicConfig(level=logging.INFO)

TOKEN = "7911273494:AAF7kzkhB6vnWJIodrRojR3eWJkH036681s"
ADMIN_ID = "7215386084"  # Твой Telegram ID для уведомлений

if not TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Токен или ID админа не заданы!")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# Прямая ссылка на твою личную Google Таблицу (Экспорт в CSV)
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/10J9cWFta1wfNNxh2MQj0kZ5oetgxetJleVSLJ6qc3m8/export?format=csv"

# Фоновый веб-сервер для прохождения проверки портов на Render
app = Flask('')

@app.route('/')
def home():
    return "Бот запущен и успешно прошел Port Binding!"

def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

# Функция для получения актуального курса USDT к гривне
async def get_usdt_rate():
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=uah"
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['tether']['uah']
    except Exception as e:
        logging.error(f"Не удалось получить курс валют: {e}")
    return 41.5  # Запасной курс

# Функция для чтения актуальной базы данных товаров прямо из Google Таблицы
def get_products():
    try:
        response = requests.get(GOOGLE_SHEET_URL, timeout=10)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        df.columns = df.columns.str.strip()
        # Превращаем DataFrame в список словарей и чистим строки от кавычек
        products = []
        for row in df.to_dict(orient='records'):
            cleaned_row = {str(k): str(v).replace('"', '').strip() for k, v in row.items()}
            products.append(cleaned_row)
        return products
    except Exception as e:
        logging.error(f"Ошибка при чтении Google Sheets: {e}")
        return []

# ХЕНДЛЕР /start С КНОПКОЙ-МИНИ-САЙТОМ
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    kb = InlineKeyboardMarkup()
    web_app_url = "https://victor-1989-eng.github.io/-/" 
    
    kb.add(InlineKeyboardButton(
        text="🛍️ Открыть магазин YAROMA", 
        web_app=types.WebAppInfo(url=web_app_url)
    ))
    
    welcome_text = (
        "👋 **Добро пожаловать в инновационный парфюмерный бутик YAROMA!**\n\n"
        "Мы создаем элитные духи на основе лучших европейских концентратов.\n\n"
        "👇 Нажмите на кнопку ниже, чтобы открыть интерактивную витрину:"
    )
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

# ХЕНДЛЕР ПОЛУЧЕНИЯ ДАННЫХ ИЗ МИНИ-САЙТА (Когда нажали «Оформить быстрый заказ» на сайте)
@dp.message_handler(content_types=types.ContentType.WEB_APP_DATA)
async def process_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        user = message.from_user
        username = f"@{user.username}" if user.username else "Нет юзернейма"
        
        # 1. Подтверждение клиенту
        await message.answer(
            f"✅ **Заявка оформлена через мини-сайт!**\n\n"
            f"📦 **Товар:** {data['name']}\n"
            f"🧪 **Объем/Тип:** {data['volume']}\n"
            f"🔢 **Количество:** {data['qty']} шт.\n"
            f"💰 **Итого к оплате:** {data['price']}\n\n"
            f"Наш менеджер уже пишет вам в ЛС для подтверждения доставки!"
        )
        
        # 2. Ищем товар в Google Таблице для получения себестоимости (cost)
        products = get_products()
        product = next((p for p in products if str(p['id']) == str(data['product_id'])), None)
        
        # Достаем чистые цифры из цены сайта (убираем " грн")
        price_actual = float(data['price'].replace('грн', '').replace(' ', '').strip())
        
        cost_грн = 0.0
        if product and 'cost' in product:
            try:
                cost_грн = float(product['cost']) * int(data['qty'])
            except:
                cost_грн = 0.0
        
        profit_грн = price_actual - cost_грн
        usdt_rate = await get_usdt_rate()
        profit_usdt = profit_грн / usdt_rate
        
        # 3. Отправка уведомления админу
        admin_text = (
            f"🚨 **НОВЫЙ ЗАКАЗ ИЗ МИНИ-АПП!**\n\n"
            f"👤 **Покупатель:** {user.full_name} ({username})\n"
            f"🆔 **ID:** `{user.id}`\n\n"
            f"📦 **Товар:** {data['name']}\n"
            f"🧪 **Объем:** {data['volume']}\n"
            f"🔢 **Количество:** {data['qty']} шт.\n"
            f"💰 **Выручка:** {price_actual} грн\n"
            f"📉 **Себестоимость:** {cost_грн} грн\n"
            f"📈 **Прибыль:** {profit_грн:.2f} грн\n\n"
            f"💵 **Заработано: ${profit_usdt:.2f}**"
        )
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Ошибка обработки заказа из WebApp: {e}")

# ХЕНДЛЕР НАЖАТИЯ КНОПКИ «КУПИТЬ» ВНУТРИ СТАРОГО ИНТЕРФЕЙСА БОТА
@dp.callback_query_handler(lambda c: c.data.startswith('buy_'))
async def process_buying(callback_query: types.CallbackQuery):
    product_id = callback_query.data.split('_')[1]
    products = get_products()
    product = next((p for p in products if str(p['id']) == str(product_id)), None)
    
    if not product:
        await bot.answer_callback_query(callback_query.id, text="Товар не найден в таблице.")
        return
        
    await bot.answer_callback_query(callback_query.id)
    
    user = callback_query.from_user
    username = f"@{user.username}" if user.username else "Нет юзернейма"
    
    await bot.send_message(
        chat_id=user.id,
        text=f"✅ **Заявка принята!**\n\nВы выбрали: *{product['name']}* за *{product['price']} грн*.\n"
             f"Наш менеджер уже связывается с вами в личных сообщениях."
    )
    
    try:
        price_грн = float(product['price'])
        cost_грн = float(product['cost']) if 'cost' in product else 0.0
        profit_грн = price_грн - cost_грн
        
        usdt_rate = await get_usdt_rate()
        profit_usdt = profit_грн / usdt_rate
        
        admin_text = (
            f"🚨 **НОВЫЙ ЗАКАЗ ИЗ КНОПОК БОТА!**\n\n"
            f"👤 **Покупатель:** {user.full_name} ({username})\n"
            f"🆔 **ID:** `{user.id}`\n\n"
            f"📦 **Товар:** {product['name']}\n"
            f"💰 **Цена:** {price_грн} грн\n"
            f"📉 **Себестоимость:** {cost_грн} грн\n"
            f"📈 **Прибыль:** {profit_грн:.2f} грн\n\n"
            f"💵 **Заработано: ${profit_usdt:.2f}**"
        )
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка подсчета прибыли в callback: {e}")

# ЕДИНСТВЕННАЯ ТОЧКА ВХОДА ДЛЯ ЗАПУСКА
if __name__ == "__main__":
    # Шаг 1: Сначала запускаем фоновый веб-сервер, чтобы Render сразу увидел порт 8000/PORT
    print("Запуск Flask веб-сервера для проверки портов Render...")
    keep_alive()
    
    # Шаг 2: Только после этого запускаем бесконечный опрос Telegram
    print("Запуск Telegram бота...")
    executor.start_polling(dp, skip_updates=True)

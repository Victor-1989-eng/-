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
from flask import Flask, request, jsonify
from flask_cors import CORS
from urllib.parse import parse_qs

# Включаем логирование
logging.basicConfig(level=logging.INFO)

TOKEN = "7911273494:AAF7kzkhB6vnWJIodrRojR3eWJkH036681s"
ADMIN_ID = "7215386084"  # Твой Telegram ID для уведомлений

if not TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Токен или ID админа не заданы!")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# Ссылка на экспорт твоей Google Таблицы
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/10J9cWFta1wfNNxh2MQj0kZ5oetgxetJleVSLJ6qc3m8/export?format=csv"

app = Flask('')
CORS(app)

@app.route('/')
def home():
    return "Бот запущен и успешно прошел Port Binding!"

# ПРИЕМ КОРЗИНЫ С СУММАРНЫМ ПОДСЧЕТОМ
@app.route('/submit-order', methods=['POST'])
def handle_submit_order():
    try:
        req_data = request.get_json()
        if not req_data or 'cart' not in req_data or 'initData' not in req_data:
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        cart = req_data['cart']
        init_data_raw = req_data['initData']
        
        # Парсим пользователя
        parsed_init_data = parse_qs(init_data_raw)
        if 'user' not in parsed_init_data:
            return jsonify({"status": "error", "message": "No user data"}), 400
            
        user_json = json.loads(parsed_init_data['user'][0])
        user_id = user_json.get('id')
        full_name = f"{user_json.get('first_name', 'Покупатель')} {user_json.get('last_name', '')}".strip()
        username = f"@{user_json['username']}" if 'username' in user_json else "Нет юзернейма"

        # Загружаем товары из Google Таблицы один раз для сверки себестоимости
        products_db = get_products()
        
        total_items_price = 0.0
        total_items_cost = 0.0
        products_lines_text = ""

        # Перебираем все товары из корзины
        for idx, item in enumerate(cart, 1):
            item_price = float(item['price'])
            item_qty = int(item['qty'])
            total_items_price += item_price
            
            # Ищем себестоимость в базе
            db_product = next((p for p in products_db if str(p['id']) == str(item['product_id'])), None)
            item_cost_single = 0.0
            if db_product and 'cost' in db_product:
                try:
                    item_cost_single = float(db_product['cost'])
                except:
                    item_cost_single = 0.0
            
            # Считаем общую себестоимость этой позиции (себестоимость * кол-во)
            item_total_cost = item_cost_single * item_qty
            total_items_cost += item_total_cost

            # Формируем строку списка для чека
            products_lines_text += f"{idx}. 📦 *{item['name']}*\n   🧪 Объём: {item['volume']} | 🔢 Кол-во: {item_qty} шт.\n   💰 Цена: {item_price} грн\n\n"

        # Расчет прибыли
        total_profit_грн = total_items_price - total_items_cost
        
        # Загружаем курс USDT
        usdt_rate = 41.5
        try:
            res_rate = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=uah", timeout=3).json()
            usdt_rate = res_rate['tether']['uah']
        except:
            pass
            
        total_profit_usdt = total_profit_грн / usdt_rate

        # 1. Отправляем красивый чек клиенту
        client_text = (
            f"🛍️ **Ваш заказ успешно оформлен!**\n\n"
            f"{products_lines_text}"
            f"💳 **Итого к оплате:** {total_items_price} грн\n\n"
            f"Наш менеджер уже связывается с вами в ЛС для подтверждения доставки!"
        )
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": user_id, "text": client_text, "parse_mode": "Markdown"
        })

        # 2. Отправляем детальный финансовый отчет тебе (админу)
        admin_text = (
            f"🚨 **НОВЫЙ ЗАКАЗ ИЗ КОРЗИНЫ МИНИ-АПП!**\n\n"
            f"👤 **Покупатель:** {full_name} ({username})\n"
            f"🆔 **ID:** `{user_id}`\n\n"
            f"📋 **Список товаров:**\n{products_lines_text}"
            f"💰 **Общая выручка:** {total_items_price} грн\n"
            f"📉 **Общая себестоимость:** {total_items_cost} грн\n"
            f"📈 **Чистая прибыль:** {total_profit_грн:.2f} грн\n\n"
            f"💵 **Заработано: ${total_profit_usdt:.2f}**"
        )
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": ADMIN_ID, "text": admin_text, "parse_mode": "Markdown"
        })

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Ошибка при обработке корзины: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def run_web_server():
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web_server)
    t.daemon = True
    t.start()

def get_products():
    try:
        response = requests.get(GOOGLE_SHEET_URL, timeout=10)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text))
        df.columns = df.columns.str.strip()
        products = []
        for row in df.to_dict(orient='records'):
            cleaned_row = {str(k): str(v).replace('"', '').strip() for k, v in row.items()}
            products.append(cleaned_row)
        return products
    except Exception as e:
        logging.error(f"Ошибка при чтении Google Sheets: {e}")
        return []

@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    kb = InlineKeyboardMarkup()
    web_app_url = "https://victor-1989-eng.github.io/-/" 
    kb.add(InlineKeyboardButton(text="🛍️ Открыть магазин YAROMA", web_app=types.WebAppInfo(url=web_app_url)))
    
    welcome_text = (
        "👋 **Добро пожаловать в инновационный парфюмерный бутик YAROMA!**\n\n"
        "Мы создаем элитные духи на основе лучших европейских концентратов.\n\n"
        "👇 Нажмите на кнопку ниже, чтобы открыть интерактивную витрину с корзиной:"
    )
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

if __name__ == "__main__":
    print("Запуск Flask веб-сервера...")
    keep_alive()
    print("Запуск Telegram бота...")
    executor.start_polling(dp, skip_updates=True)

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

logging.basicConfig(level=logging.INFO)

# Теперь бот будет брать значения из настроек Render, а не из открытого кода!
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

if not TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Токены или ID админа не найдены в переменных окружения!")

if not TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Токен или ID админа не заданы!")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/10J9cWFta1wfNNxh2MQj0kZ5oetgxetJleVSLJ6qc3m8/export?format=csv"

app = Flask('')
CORS(app)

@app.route('/')
def home():
    return "Бот запущен и успешно прошел Port Binding!"

# ПРОКСИ ДЛЯ ЗАПРОСА ГОРОДОВ ИЗ НОВОЙ ПОЧТЫ
@app.route('/get-cities', methods=['POST'])
def get_np_cities():
    try:
        data = request.get_json()
        search_name = data.get('cityName', '')
        
        payload = {
            "apiKey": API_KEY_NOVAPOSHTA,
            "modelName": "Address",
            "calledMethod": "getCities",
            "methodProperties": {
                "FindByString": search_name,
                "Limit": "10"
            }
        }
        res = requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload, timeout=5).json()
        if res.get('success'):
            return jsonify(res.get('data', [])), 200
        return jsonify([]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ПРОКСИ ДЛЯ ЗАПРОСА ОТДЕЛЕНИЙ ИЗ НОВОЙ ПОЧТЫ
@app.route('/get-warehouses', methods=['POST'])
def get_np_warehouses():
    try:
        data = request.get_json()
        city_ref = data.get('cityRef', '')
        
        payload = {
            "apiKey": API_KEY_NOVAPOSHTA,
            "modelName": "Address",
            "calledMethod": "getWarehouses",
            "methodProperties": {
                "CityRef": city_ref,
                "Limit": "250"
            }
        }
        res = requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload, timeout=5).json()
        if res.get('success'):
            return jsonify(res.get('data', [])), 200
        return jsonify([]), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ПРИЕМ ЗАКАЗА (КОШИК + ДОСТАВКА)
@app.route('/submit-order', methods=['POST'])
def handle_submit_order():
    try:
        req_data = request.get_json()
        if not req_data or 'cart' not in req_data or 'initData' not in req_data or 'delivery' not in req_data:
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        cart = req_data['cart']
        delivery = req_data['delivery']
        init_data_raw = req_data['initData']
        
        parsed_init_data = parse_qs(init_data_raw)
        if 'user' not in parsed_init_data:
            return jsonify({"status": "error", "message": "No user data"}), 400
            
        user_json = json.loads(parsed_init_data['user'][0])
        user_id = user_json.get('id')
        full_name = f"{user_json.get('first_name', 'Покупатель')} {user_json.get('last_name', '')}".strip()
        username = f"@{user_json['username']}" if 'username' in user_json else "Нет юзернейма"

        products_db = get_products()
        
        total_items_price = 0.0
        total_items_cost = 0.0
        products_lines_text = ""

        for idx, item in enumerate(cart, 1):
            item_price = float(item['price'])
            item_qty = int(item['qty'])
            total_items_price += item_price
            
            db_product = next((p for p in products_db if str(p['id']) == str(item['product_id'])), None)
            item_cost_single = 0.0
            if db_product and 'cost' in db_product:
                try:
                    item_cost_single = float(db_product['cost'])
                except:
                    item_cost_single = 0.0
            
            item_total_cost = item_cost_single * item_qty
            total_items_cost += item_total_cost

            products_lines_text += f"{idx}. 📦 *{item['name']}*\n   🧪 Об'єм: {item['volume']} | 🔢 Кіл-ть: {item_qty} шт.\n   💰 Ціна: {item_price} грн\n\n"

        total_profit_грн = total_items_price - total_items_cost
        
        usdt_rate = 41.5
        try:
            res_rate = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=uah", timeout=3).json()
            usdt_rate = res_rate['tether']['uah']
        except:
            pass
            
        total_profit_usdt = total_profit_грн / usdt_rate

        # 1. Текстовый чек клиенту (на украинском языке)
        client_text = (
            f"🛍️ **Ваше замовлення успішно оформлено!**\n\n"
            f"{products_lines_text}"
            f"💳 **Разом до сплати:** {total_items_price} грн\n\n"
            f"🚚 **Доставка:** Нова Пошта\n"
            f"📍 **Адреса:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n\n"
            f"Наш менеджер вже готує посилку до відправки!"
        )
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": user_id, "text": client_text, "parse_mode": "Markdown"
        })

        # 2. Финансовый отчет тебе в ТГ (на русском, с сохранением авторасчета в USDT)
        admin_text = (
            f"🚨 **НОВЫЙ ЗАКАЗ ИЗ КОРЗИНЫ!**\n\n"
            f"👤 **Покупатель:** {full_name} ({username})\n"
            f"🆔 **ID:** `{user_id}`\n\n"
            f"📋 **Список товаров:**\n{products_lines_text}"
            f"🚚 **ДАННЫЕ ДЛЯ ОТПРАВКИ (НОВАЯ ПОЧТА):**\n"
            f"• **ФИО:** {delivery['name']}\n"
            f"• **Тел:** {delivery['phone']}\n"
            f"• **Город:** {delivery['city']}\n"
            f"• **Отделение:** {delivery['warehouse']}\n\n"
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
        return []

@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    kb = InlineKeyboardMarkup()
    web_app_url = "https://victor-1989-eng.github.io/-/" 
    kb.add(InlineKeyboardButton(text="🛍️ Відкрити магазин YAROMA", web_app=types.WebAppInfo(url=web_app_url)))
    
    welcome_text = (
        "👋 **Вітаємо в інноваційному парфумерному бутіку YAROMA!**\n\n"
        "Ми створюємо елітні парфуми на основі найкращих європейських концентратів.\n\n"
        "👇 Натисніть на кнопку нижче, щоб відкрити інтерактивну вітрину з інтеграцією Нової Пошти:"
    )
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

if __name__ == "__main__":
    keep_alive()
    executor.start_polling(dp, skip_updates=True)

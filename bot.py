import os
import io
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from urllib.parse import parse_qs
from threading import Thread

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# Библиотеки Google
import gspread
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID") 
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

SPREADSHEET_ID = "10J9cWFta1wfNNxh2MQj0kZ5oetgxetJleVSLJ6qc3m8"
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Токены или ID админа не найдены!")

# Инициализируем бота с поддержкой памяти для пошаговых сценариев (FSM)
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

app = Flask('')
CORS(app)

# --- СОСТОЯНИЯ ДЛЯ АДМИНКИ (FSM) ---
class AddProductState(StatesGroup):
    name = State()
    category = State()
    price = State()
    description = State()
    image = State()

class DeleteProductState(StatesGroup):
    confirm = State()

def get_google_sheet_client():
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except Exception as e:
        logging.error(f"Ошибка авторизации Google: {e}")
        return None

# Вспомогательная функция проверки: является ли пользователь хозяином какого-то магазина
def check_owner(user_id):
    client = get_google_sheet_client()
    if not client:
        return None
    try:
        shops_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        records = shops_sheet.get_all_records()
        for row in records:
            if str(row.get('owner_id')).strip() == str(user_id).strip():
                return row # Возвращаем всю инфу о магазине клиента [shop_id, name, sheet_name и т.д.]
    except Exception as e:
        logging.error(f"Ошибка при проверке хозяина: {e}")
    return None

@app.route('/')
def home(): return "Платформа pro_teleg.ua запущена!"

@app.route('/get-cities', methods=['POST'])
def get_np_cities():
    data = request.get_json()
    payload = {"apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getCities", "methodProperties": {"FindByString": data.get('cityName', ''), "Limit": "10"}}
    return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))

@app.route('/get-warehouses', methods=['POST'])
def get_np_warehouses():
    data = request.get_json()
    payload = {"apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getWarehouses", "methodProperties": {"CityRef": data.get('cityRef', ''), "Limit": "250"}}
    return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))

@app.route('/submit-order', methods=['POST'])
def handle_submit_order():
    try:
        req_data = request.get_json()
        cart, delivery, init_data_raw = req_data['cart'], req_data['delivery'], req_data['initData']
        shop_id = req_data.get('shop_id', 'yaroma')

        user_json = json.loads(parse_qs(init_data_raw)['user'][0])
        user_id = user_json.get('id')
        c_name = f"{user_json.get('first_name', 'Покупець')} {user_json.get('last_name', '')}".strip()
        c_user = f"@{user_json['username']}" if 'username' in user_json else "Немає юзернейму"

        client = get_google_sheet_client()
        owner_tg_id, shop_name = ADMIN_ID, "Магазин"
        if client:
            try:
                records = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS").get_all_records()
                for row in records:
                    if str(row.get('shop_id')).strip() == str(shop_id).strip():
                        owner_tg_id = str(row.get('owner_id')).strip()
                        shop_name = row.get('name', shop_name)
                        break
            except: pass

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            total_price += float(item['price'])
            p_text += f"{idx}. 📦 *{item['name']}*\n   🧪 Об'єм: {item['volume']} | 🔢 Кіл-ть: {item['qty']} шт.\n   💰 Ціна: {item['price']} грн\n\n"

        client_text = f"🛍️ **Ваше замовлення в магазині {shop_name} успішно оформлено!**\n\n{p_text}💳 **Разом до сплати:** {total_price} грн\n💰 **Спосіб оплати:** {delivery.get('payment', 'Не вказано')}\n\n🚚 **Доставка:** Нова Пошта\n📍 **Адреса:** {delivery['city']}, {delivery['warehouse']}\n👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n\nДякуємо!"
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": user_id, "text": client_text, "parse_mode": "Markdown"})

        owner_text = f"🚨 **НОВЕ ЗАМОВЛЕННЯ Z ВІТРИНИ! ({shop_name})**\n\n👤 **Покупець:** {c_name} ({c_user})\n🆔 **ID:** `{user_id}`\n\n📋 **Список товарів:**\n{p_text}🚚 **ДОСТАВКА:**\n• **ПІБ:** {delivery['name']}\n• **Тел:** {delivery['phone']}\n• **Місто:** {delivery['city']}\n• **Відділення:** {delivery['warehouse']}\n\n💳 **Оплата:** {delivery.get('payment', 'Не вказано')}\n💰 **Сума:** {total_price} грн"
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": owner_tg_id, "text": owner_text, "parse_mode": "Markdown"})

        if str(owner_tg_id) != str(ADMIN_ID):
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": ADMIN_ID, "text": f"⚙️ Лог: В `{shop_name}` новий заказ на {total_price} грн."})

        if client:
            try:
                client.open_by_key(SPREADSHEET_ID).worksheet("ORDERS").append_row([datetime.now().strftime("%d.%m.%Y %H:%M"), shop_id, shop_name, total_price, round(total_price * 0.10, 2)])
            except: pass

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000)))
Thread(target=run_web_server, daemon=True).start()

# --- ТЕЛЕГРАМ БОТ: ЛОГИКА АДМИН-ПАНЕЛИ ХОЗЯЕВОВ ---

@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    kb = InlineKeyboardMarkup()
    web_app_url = "https://victor-1989-eng.github.io/-/" 
    kb.add(InlineKeyboardButton(text="🚀 Запустити pro_teleg.ua", web_app=types.WebAppInfo(url=web_app_url)))
    
    welcome_text = (
        "<b>👋 Вітаємо на платформі pro_teleg.ua!</b>\n\n"
        "Тут зібрані найкращі інтерактивні магазини України, які працюють автоматично прямо у вашому месенджері.\n\n"
        "👇 Натисніть на кнопку нижче, щоб відкрити головний каталог брендів та обрати потрібний магазин:"
    )
    # Поменяли parse_mode на "HTML" и обернули жирный текст в теги <b>...</b>
    await message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")

@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data:
        await message.answer("❌ **Доступ обмежено.**\nЦя команда доступна лише офіційним партнерам платформи pro_teleg.ua.")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("➕ Додати новий товар"), KeyboardButton("❌ Видалити товар"))
    await message.answer(f"⚙️ **Кабінет власника магазину: {shop_data['name']}**\n\nОберіть дію на клавіатурі нижче:", reply_markup=kb)

# --- СЦЕНАРИЙ: ДОБАВЛЕНИЕ ТОВАРА ---
@dp.message_handler(lambda msg: msg.text == "➕ Додати новий товар")
async def add_product_start(message: types.Message):
    if not check_owner(message.from_user.id): return
    await message.answer("📝 **Крок 1/5:** Введіть НАЗВУ товару (наприклад: *Парфуми YAROMA Supreme*):", parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())
    await AddProductState.name.set()

@dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📁 **Крок 2/5:** Введіть КАТЕГОРІЮ (наприклад: *Жіночі*, *Чоловічі*, *Унісекс* або *Сети*):")
    await AddProductState.category.set()

@dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("💰 **Крок 3/5:** Введіть ЦІНУ товару в гривнях (лише число, наприклад: *450*):")
    await AddProductState.price.set()

@dp.message_handler(state=AddProductState.price)
async def add_product_price(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Будь ласка, введіть числове значення ціни!")
        return
    await state.update_data(price=int(message.text))
    await message.answer("📖 **Крок 4/5:** Введіть ОПИС товару (характеристики, ноти аромату або склад):")
    await AddProductState.description.set()

@dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("📸 **Крок 5/5:** Надішліть ФОТОГРАФІЮ товару (одним зображенням):")
    await AddProductState.image.set()

@dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    # Генерируем прямую публичную ссылку на файл через Telegram API
    file_info = await bot.get_file(photo.file_id)
    public_img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

    user_data = await state.get_data()
    shop_data = check_owner(message.from_user.id)

    client = get_google_sheet_client()
    if client and shop_data:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
            # Генерируем ID на основе количества строк
            new_id = f"id_{len(sheet.get_all_values()) + 1}"
            
            # Добавляем строку: ID, Название, Категория, Цена, Себестоимость(0), Опис, Фото
            sheet.append_row([new_id, user_data['name'], user_data['category'], user_data['price'], 0, user_data['description'], public_img_url])
            
            await message.answer(f"✅ **Успішно додано!**\nТовар \"{user_data['name']}\" завантажено на ваш лист `{shop_data['sheet_name']}` та активовано у WebApp.")
        except Exception as e:
            await message.answer(f"❌ Помилка запису в таблицю: {e}")
    
    await state.finish()
    await admin_panel(message)

# --- СЦЕНАРИЙ: УДАЛЕНИЕ ТОВАРА ---
@dp.message_handler(lambda msg: msg.text == "❌ Видалити товар")
async def delete_product_start(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data: return

    client = get_google_sheet_client()
    if not client: return

    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
        records = sheet.get_all_records()
        
        if not records:
            await message.answer("📦 У вашому магазині ще немає жодного товару.")
            return

        kb = InlineKeyboardMarkup(row_width=1)
        for row in records:
            prod_name = row.get('name') or row.get('Название')
            prod_id = row.get('id') or row.get('ID')
            kb.add(InlineKeyboardButton(text=f"🗑️ {prod_name}", callback_data=f"del_{prod_id}"))
            
        await message.answer("👇 **Оберіть товар зі списку нижче, який потрібно видалити:**", reply_markup=kb)
    except Exception as e:
        await message.answer(f"❌ Помилка зчитування бази: {e}")

@dp.callback_query_handler(lambda call: call.data.startswith('del_'))
async def delete_product_confirm(call: types.CallbackQuery, state: FSMContext):
    prod_id = call.data.replace('del_', '')
    shop_data = check_owner(call.from_user.id)
    
    client = get_google_sheet_client()
    if client and shop_data:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
            data = sheet.get_all_values()
            
            row_index = -1
            prod_name = "Товар"
            for idx, row in enumerate(data):
                if row[0] == prod_id:
                    row_index = idx + 1 # gspread строки считает с 1
                    prod_name = row[1]
                    break
            
            if row_index != -1:
                sheet.delete_rows(row_index) # Удаляем строку физически
                await call.answer(f"Товар успішно видалено!", show_alert=True)
                await call.message.edit_text(f"🗑️ **Товар \"{prod_name}\" успішно видалено** з вашого каталогу.")
            else:
                await call.answer("Товар не знайдено", show_alert=True)
        except Exception as e:
            await call.message.answer(f"❌ Помилка видалення: {e}")
            
    await admin_panel(call.message)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)

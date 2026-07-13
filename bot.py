import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from urllib.parse import parse_qs
from threading import Thread

import psycopg2  # Драйвер для подключения к Neon.tech

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.exceptions import MessageToDeleteNotFound

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ И ТОКЕНЫ ---
CLIENT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SELLER_TOKEN = os.environ.get("SELLER_BOT_TOKEN")
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")  # Токен вашего 3-го (админского) бота
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not CLIENT_TOKEN or not SELLER_TOKEN or not ADMIN_BOT_TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Проверьте ВСЕ токены ботов и ID админа в настройках Render!")

client_bot = Bot(token=CLIENT_TOKEN)
client_dp = Dispatcher(client_bot, storage=MemoryStorage())

seller_bot = Bot(token=SELLER_TOKEN)
seller_dp = Dispatcher(seller_bot, storage=MemoryStorage())

admin_bot = Bot(token=ADMIN_BOT_TOKEN)
admin_dp = Dispatcher(admin_bot, storage=MemoryStorage())

app = Flask('')
CORS(app)

# --- БАЗА ДАННЫХ (NEON.TECH) ---
def init_db():
    pass

def read_db():
    if not DATABASE_URL:
        return {"shops": {}, "orders": []}
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT json_content FROM bot_data WHERE key_name = 'main_db';")
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return json.loads(row[0])
        return {"shops": {}, "orders": []}
    except Exception as e:
        logging.error(f"⚠️ Ошибка чтения из Neon: {e}")
        return {"shops": {}, "orders": []}

def write_db(data):
    if not DATABASE_URL:
        return
    try:
        json_str = json.dumps(data, ensure_ascii=False, indent=4)
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE bot_data SET json_content = %s WHERE key_name = 'main_db';", (json_str,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"⚠️ Ошибка записи в Neon: {e}")

def get_owner_shops(user_id):
    db = read_db()
    return {s_id: s for s_id, s in db["shops"].items() if str(s["owner_id"]) == str(user_id)}

# --- СОСТОЯНИЯ FSM (ПРОДАВЦЫ) ---
class CreateShopState(StatesGroup):
    shop_id = State()
    name = State()
    emoji = State()

class AddProductState(StatesGroup):
    target_shop = State()
    name = State()
    category = State()
    has_variants = State()
    variants_list = State()
    variants_prices = State()
    single_price = State()
    description = State()
    image = State()
    v_list = State()
    v_index = State()
    compiled_variants = State()

async def save_msg_id(state: FSMContext, message_id: int):
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    msg_ids.append(message_id)
    await state.update_data(messages_to_delete=msg_ids)

async def clear_fsm_chat_history(chat_id: int, state: FSMContext):
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    for m_id in msg_ids:
        try:
            await seller_bot.delete_message(chat_id=chat_id, message_id=m_id)
        except MessageToDeleteNotFound: pass
        except Exception: pass

def get_cancel_kb():
    return InlineKeyboardMarkup().add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))


# --- API ENDPOINTS (FLASK + ЗАЩИТНЫЙ ФИЛЬТР) ---
@app.route('/')
def home(): 
    return "API Платформы Активна!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    db = read_db()
    # 🛡️ Отдаем в WebApp ТОЛЬКО активные магазины. Никакой pending (модерация) контент на сайт не попадет.
    flat_shops = [
        {"shop_id": s_id, "name": s["name"], "emoji": s["emoji"], "status": s["status"]} 
        for s_id, s in db["shops"].items() if s.get("status") == "active"
    ]
    return jsonify(flat_shops)

@app.route('/get-shop-products/<shop_id>', methods=['GET'])
def get_shop_products(shop_id):
    db = read_db()
    shop = db["shops"].get(shop_id)
    if not shop or shop.get('status') != 'active':
        return jsonify([])
    return jsonify(shop.get("products", []))

@app.route('/get-cities', methods=['POST'])
def get_np_cities():
    data = request.get_json() or {}
    payload = {"apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getCities", "methodProperties": {"FindByString": data.get('cityName', ''), "Limit": "20"}}
    try: return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload, timeout=10).json().get('data', []))
    except Exception: return jsonify([])

@app.route('/get-warehouses', methods=['POST'])
def get_np_warehouses():
    data = request.get_json() or {}
    payload = {"apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getWarehouses", "methodProperties": {"CityRef": data.get('cityRef', ''), "Limit": "500"}}
    try: return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload, timeout=10).json().get('data', []))
    except Exception: return jsonify([])

@app.route('/submit-order', methods=['POST'])
def handle_submit_order():
    try:
        req_data = request.get_json()
        cart = req_data['cart']
        delivery = req_data['delivery']
        init_data_raw = req_data['initData']
        shop_id = req_data.get('shop_id')

        user_json = json.loads(parse_qs(init_data_raw)['user'][0])
        buyer_tg_id = user_json.get('id')

        db = read_db()
        shop = db["shops"].get(shop_id)

        if not shop or shop.get('status') != 'active':
            return jsonify({"status": "error", "message": "Shop not active"}), 403

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            variant_str = f" ({item['selected_variant']} мл)" if item.get('selected_variant') else ""
            item_total = float(item['price']) * int(item.get('qty', 1))
            total_price += item_total
            p_text += f"{idx}. 📦 *{item['name']}{variant_str}*\n   🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"ord_approve:{buyer_tg_id}:{total_price}:{shop_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}")
        )

        owner_text = (
            f"📥 **НОВЕ ЗАМОВЛЕННЯ З КОРЗИНИ!**\n"
            f"🏪 Магазин: *{shop['name']}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n"
        )
        
        requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={
            "chat_id": shop['owner_id'], "text": owner_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
        }, timeout=10)

        return jsonify({"status": "pending_approval"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =======================================================
# 🛍️ БОТ ПОКУПАТЕЛЕЙ (CLIENT BOT)
# =======================================================
@client_dp.message_handler(commands=['start'])
async def client_welcome(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🛍️ Перейти до веб-магазину", web_app=types.WebAppInfo(url="https://victor-1989-eng.github.io/-/")))
    await message.answer(
        "<b>Ласкаво просимо до маркетплейсу pro_teleg.ua! 📦</b>\n\n"
        "Оберіть потрібний магазин зі списку у додатку та робіть замовлення!",
        reply_markup=kb, parse_mode="HTML"
    )


# =======================================================
# 💼 БОТ ПРОДАВЦОВ (SELLER BOT)
# =======================================================
def get_seller_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🏪 Мої Магазини"), KeyboardButton("➕ Додати новий товар"),
           KeyboardButton("🗑️ Видалити товар"), KeyboardButton("❌ Видалити магазин"))
    return kb

@seller_dp.message_handler(commands=['start'], state='*')
async def seller_welcome(message: types.Message, state: FSMContext):
    await state.finish() 
    await message.answer("👋 **Вітаємо в кабінеті керування для Продавців платформи!**", reply_markup=get_seller_menu())

@seller_dp.message_handler(text="🏪 Мої Магазини", state='*')
async def seller_shops_list(message: types.Message, state: FSMContext):
    await state.finish()
    shops = get_owner_shops(message.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    
    if not shops:
        kb.add(InlineKeyboardButton(text="➕ Створити перший магазин", callback_data="make_shop_wizard"))
        await message.answer("ℹ️ У вас ще немає створених магазинів на платформі.", reply_markup=kb)
        return
    
    for s_id, s in shops.items():
        status_text = "🔎 На модерації" if s['status'] == 'pending' else ( "✅ Активний" if s['status'] == 'active' else "❌ Заморожений" )
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']} ({status_text})", callback_data=f"view_shop_{s_id}"))
    
    kb.add(InlineKeyboardButton(text="➕ Додати ще один магазин", callback_data="make_shop_wizard"))
    await message.answer("🏪 **Ваш список магазинів:**", reply_markup=kb, parse_mode="Markdown")

@seller_dp.callback_query_handler(lambda call: call.data.startswith('view_shop_'))
async def view_shop_details(call: types.CallbackQuery):
    s_id = call.data.split('_')[2]
    db = read_db()
    shop = db["shops"].get(s_id)
    if not shop: return
    
    status_map = {"active": "✅ Активний", "pending": "⏳ На модерації (не видно в додатку)", "frozen": "❌ Заморожений"}
    text = (
        f"🏪 **Управління магазином: {shop['name']}**\n\n"
        f"• ID бренду: `{s_id}`\n"
        f"• Статус в системі: {status_map.get(shop['status'], shop['status'])}\n"
        f"• Кількість товарів: {len(shop.get('products', []))} шт.\n"
        f"• Борг платформи (5%): *{shop['debt']} грн*"
    )
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="back_to_shops_list"))
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@seller_dp.callback_query_handler(lambda call: call.data == "back_to_shops_list")
async def back_to_shops_list_handler(call: types.CallbackQuery):
    shops = get_owner_shops(call.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        status_text = "🔎 На модерації" if s['status'] == 'pending' else ( "✅ Активний" if s['status'] == 'active' else "❌ Заморожений" )
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']} ({status_text})", callback_data=f"view_shop_{s_id}"))
    kb.add(InlineKeyboardButton(text="➕ Додати ще один магазин", callback_data="make_shop_wizard"))
    await call.message.edit_text("🏪 **Ваш список магазинів:**", reply_markup=kb, parse_mode="Markdown")

# МАСТЕР СОЗДАНИЯ МАГАЗИНА (ПО УМОЛЧАНИЮ СТАТУС PENDING)
@seller_dp.callback_query_handler(lambda call: call.data == "make_shop_wizard")
async def start_shop_wizard(call: types.CallbackQuery):
    await call.message.answer("📝 **КРОК 1:** Введіть унікальний ID магазину англійськими літерами:")
    await CreateShopState.shop_id.set()

@seller_dp.message_handler(state=CreateShopState.shop_id)
async def process_shop_id(message: types.Message, state: FSMContext):
    s_id = message.text.strip().lower()
    db = read_db()
    if s_id in db["shops"]:
        await message.answer("❌ Цей ID вже зайнятий! Введіть інший:")
        return
    await state.update_data(shop_id=s_id)
    await message.answer("📝 **КРОК 2:** Введіть публічну назву вашого магазину:")
    await CreateShopState.name.set()

@seller_dp.message_handler(state=CreateShopState.name)
async def process_shop_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📝 **КРОК 3:** Надішліть **один емодзі** для іконки бренду:")
    await CreateShopState.emoji.set()

@seller_dp.message_handler(state=CreateShopState.emoji)
async def process_shop_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    db = read_db()
    db["shops"][user_data['shop_id']] = {
        "name": user_data['name'], "emoji": emoji, "owner_id": message.from_user.id,
        "debt": 0.0, "status": "pending", "products": []
    }
    write_db(db)
    
    # 🛡️ Рубеж 1: Мгновенное уведомление в ВЫДЕЛЕННЫЙ АДМИН-БОТ
    admin_kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"adm_appr_{user_data['shop_id']}"),
        InlineKeyboardButton(text="❌ Відхилити та видалити", callback_data=f"adm_decl_{user_data['shop_id']}")
    )
    try:
        await admin_bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 **ЗАЯВКА НА НОВИЙ МАГАЗИН!**\n\n"
                 f"🏪 Назва: *{user_data['name']}*\n"
                 f"🆔 ID: `{user_data['shop_id']}`\n"
                 f"🎭 Емодзі: {emoji}\n"
                 f"👤 Власник ID: `{message.from_user.id}`\n\n"
                 f"Прийміть рішення щодо активации бренда:",
            reply_markup=admin_kb, parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

    await message.answer("⏳ **Магазин успішно створено та відправлено на модерацію адміну!**\nВін з'явиться на сайті відразу після схвалення.", reply_markup=get_seller_menu())
    await state.finish()

# ТОКЕНЫ И КОНТРОЛЬ ТОВАРОВ
@seller_dp.message_handler(text="➕ Додати новий товар", state='*')
async def add_product_start(message: types.Message, state: FSMContext):
    await state.finish() 
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        await message.answer("❌ У вас немає магазинів!")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"addto_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))
    m1 = await message.answer("📋 Оберіть магазин для додавання товару:", reply_markup=kb)
    await AddProductState.target_shop.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m1.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('addto_'), state=AddProductState.target_shop)
async def add_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.split('_')[1]
    await state.update_data(target_shop=shop_id)
    m2 = await call.message.answer("✍️ Введіть назву товару:", reply_markup=get_cancel_kb())
    await AddProductState.name.set()
    await save_msg_id(state, m2.message_id)

@seller_dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    m3 = await message.answer("✍️ Введіть назву категорії:", reply_markup=get_cancel_kb())
    await AddProductState.category.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m3.message_id)

@seller_dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton(text="Так (Об'єми)", callback_data="var_yes"), InlineKeyboardButton(text="Ні (Фіксована)", callback_data="var_no"))
    m4 = await message.answer("✍️ Чи є варіативність мілілітрів?", reply_markup=kb)
    await AddProductState.has_variants.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m4.message_id)

@seller_dp.callback_query_handler(lambda call: call.data in ["var_yes", "var_no"], state=AddProductState.has_variants)
async def add_product_variant_decision(call: types.CallbackQuery, state: FSMContext):
    if call.data == "var_yes":
        await state.update_data(has_variants=True)
        m5 = await call.message.answer("✍️ Введіть доступні об'єми через кому (наприклад: `10,30,50`):", reply_markup=get_cancel_kb())
        await AddProductState.variants_list.set()
    else:
        await state.update_data(has_variants=False)
        m5 = await call.message.answer("✍️ Введіть фіксовану ціну в грн:", reply_markup=get_cancel_kb())
        await AddProductState.single_price.set()
    await save_msg_id(state, m5.message_id)

@seller_dp.message_handler(state=AddProductState.single_price)
async def add_product_single_price(message: types.Message, state: FSMContext):
    await state.update_data(single_price=message.text.strip())
    m6 = await message.answer("✍️ Напишіть опис товару:", reply_markup=get_cancel_kb())
    await AddProductState.description.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m6.message_id)

@seller_dp.message_handler(state=AddProductState.variants_list)
async def add_product_variants_list(message: types.Message, state: FSMContext):
    v_list = [v.strip() for v in message.text.split(',') if v.strip()]
    await save_msg_id(state, message.message_id)
    await state.update_data(v_list=v_list, v_index=0, compiled_variants=[])
    m6 = await message.answer(f"Введіть ціну в грн для **{v_list[0]} мл**:", reply_markup=get_cancel_kb())
    await AddProductState.variants_prices.set()
    await save_msg_id(state, m6.message_id)

@seller_dp.message_handler(state=AddProductState.variants_prices)
async def add_product_variants_prices(message: types.Message, state: FSMContext):
    price = message.text.strip()
    s_data = await state.get_data()
    v_list = s_data["v_list"]
    v_idx = s_data["v_index"]
    compiled_variants = s_data["compiled_variants"]
    await save_msg_id(state, message.message_id)
    
    compiled_variants.append({"volume": v_list[v_idx], "price": price})
    next_idx = v_idx + 1
    
    if next_idx < len(v_list):
        await state.update_data(v_index=next_idx, compiled_variants=compiled_variants)
        m_next = await message.answer(f"Введіть ціну в грн для **{v_list[next_idx]} мл**:", reply_markup=get_cancel_kb())
        await save_msg_id(state, m_next.message_id)
    else:
        await state.update_data(compiled_variants=compiled_variants)
        m_desc = await message.answer("✍️ Напишіть опис товару:", reply_markup=get_cancel_kb())
        await AddProductState.description.set()
        await save_msg_id(state, m_desc.message_id)

@seller_dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    m7 = await message.answer("✍️ Надішліть одне фото товару:", reply_markup=get_cancel_kb())
    await AddProductState.image.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m7.message_id)

@seller_dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    photo = message.photo[-1]
    file_info = await seller_bot.get_file(photo.file_id)
    img_url = f"https://api.telegram.org/file/bot{SELLER_TOKEN}/{file_info.file_path}"
    
    user_data = await state.get_data()
    s_id = user_data["target_shop"]
    db = read_db()
    
    new_prod = {
        "id": f"id_{len(db['shops'][s_id]['products']) + 1}_{datetime.now().strftime('%s')}",
        "name": user_data["name"], "category": user_data["category"],
        "description": user_data["description"], "image_url": img_url, "has_variants": user_data["has_variants"]
    }
    if user_data["has_variants"]:
        new_prod["variants"] = user_data["compiled_variants"]
        new_prod["price"] = user_data["compiled_variants"][0]["price"]
    else:
        new_prod["price"] = user_data["single_price"]
        
    db["shops"][s_id]["products"].append(new_prod)
    write_db(db)
    await clear_fsm_chat_history(message.chat.id, state)
    await message.answer("✅ Товар додано до каталогу!", reply_markup=get_seller_menu())
    await state.finish()

@seller_dp.callback_query_handler(lambda call: call.data == "cancel_product_creation", state='*')
async def cancel_product_creation_handler(call: types.CallbackQuery, state: FSMContext):
    await clear_fsm_chat_history(call.message.chat.id, state)
    await state.finish()
    await call.message.answer("❌ Додавання товару скасовано.", reply_markup=get_seller_menu())
    await call.answer()

@seller_dp.message_handler(text="🗑️ Видалити товар", state='*')
async def delete_product_start(message: types.Message, state: FSMContext):
    await state.finish()
    shops = get_owner_shops(message.from_user.id)
    if not shops: return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items(): kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"listdel_{s_id}"))
    await message.answer("📋 Оберіть магазин для видалення товару:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('listdel_'))
async def delete_product_list(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    db = read_db()
    kb = InlineKeyboardMarkup(row_width=1)
    for p in db["shops"].get(s_id, {}).get("products", []):
        kb.add(InlineKeyboardButton(text=f"🗑️ {p['name']}", callback_data=f"confprod_{s_id}_{p['id']}"))
    await call.message.edit_text("Оберіть товар для видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confprod_'))
async def delete_product_execute(call: types.CallbackQuery):
    _, s_id, p_id = call.data.split('_')
    db = read_db()
    if s_id in db["shops"]:
        db["shops"][s_id]["products"] = [p for p in db["shops"][s_id]["products"] if p["id"] != p_id]
        write_db(db)
        await call.message.edit_text("✅ Товар видалено.")

@seller_dp.message_handler(text="❌ Видалити магазин", state='*')
async def delete_shop_start(message: types.Message, state: FSMContext):
    await state.finish()
    shops = get_owner_shops(message.from_user.id)
    if not shops: return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items(): kb.add(InlineKeyboardButton(text=f"❌ {s['name']}", callback_data=f"delshop_{s_id}"))
    await message.answer("⚠️ Оберіть магазин для ПОВНОГО видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('delshop_'))
async def delete_shop_execute(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    db = read_db()
    if s_id in db["shops"] and str(db["shops"][s_id]["owner_id"]) == str(call.from_user.id):
        del db["shops"][s_id]
        write_db(db)
        await call.message.edit_text("💥 Магазин повністю видалено.")

@seller_dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action, buyer_id = action_data[0], action_data[1]
    if action == "ord_approve":
        total_price, shop_id = float(action_data[2]), action_data[3]
        commission = round(total_price * 0.05, 2)
        db = read_db()
        if shop_id in db["shops"]:
            db["shops"][shop_id]["debt"] = round(db["shops"][shop_id]["debt"] + commission, 2)
            db["orders"].append({"date": datetime.now().strftime("%d.%m.%Y %H:%M"), "shop_id": shop_id, "total": total_price, "commission": commission})
            write_db(db)
            await client_bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{db['shops'][shop_id]['name']}\" підтверджено!**")
            
            # 💰 АВТОМАТИЧЕСКИЙ РАСЧЕТ ПРОФИТА В АДМИН-БОТ
            await admin_bot.send_message(ADMIN_ID, f"💰 **Earned $5.50**\n(Комісія {commission} грн з замовлення бренду `{shop_id}`).")
            await call.message.edit_text(call.message.text + f"\n\n✅ Підтверджено. Комісія {commission} грн додана до рахунку.")
    elif action == "ord_decline":
        await client_bot.send_message(buyer_id, "❌ Замовлення відхилено продавцем.")
        await call.message.edit_text(call.message.text + "\n\n❌ Скасовано.")
    await call.answer()


# =======================================================
# 🔐 ВЕТКА ОБРАБОТКИ ВЫДЕЛЕННОГО АДМИН-БОТА (ADMIN BOT)
# =======================================================
def get_admin_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📊 Статистика платформи"), 
        KeyboardButton("💰 Хто винен (Борги)"),
        KeyboardButton("🔎 Заявки на модерацію")
    )
    return kb

@admin_dp.message_handler(commands=['start'])
async def admin_start(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): 
        return
    await message.answer(
        "🔒 **Вітаємо в головній панелі керування платформи pro_teleg.ua!**\n\n"
        "Використовуйте меню нижче для повного контролю системи.", 
        reply_markup=get_admin_menu()
    )

@admin_dp.message_handler(text="📊 Статистика платформи")
async def admin_stats(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    db = read_db()
    shops = db.get("shops", {})
    orders = db.get("orders", [])
    
    active_cnt = sum(1 for s in shops.values() if s.get("status") == "active")
    pending_cnt = sum(1 for s in shops.values() if s.get("status") == "pending")
    frozen_cnt = sum(1 for s in shops.values() if s.get("status") == "frozen")
    
    total_turnover = sum(o.get("total", 0) for o in orders)
    total_commissions = sum(o.get("commission", 0) for o in orders)
    
    text = (
        f"📊 **СТАТИСТИКА ПЛАТФОРМИ:**\n\n"
        f"🏪 Всього магазинів: *{len(shops)}*\n"
        f"  └ ✅ Активні: {active_cnt}\n"
        f"  └ ⏳ На модерації: {pending_cnt}\n"
        f"  └ ❌ Заморожені: {frozen_cnt}\n\n"
        f"🛍️ Успішних замовлень: *{len(orders)}*\n"
        f"💰 Загальний оборот: *{total_turnover} грн*\n"
        f"📈 Заробіток платформи (5%): *{total_commissions} грн*"
    )
    await message.answer(text, parse_mode="Markdown")

@admin_dp.message_handler(text="💰 Хто винен (Борги)")
async def admin_debts(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    db = read_db()
    debtors = {s_id: s for s_id, s in db.get("shops", {}).items() if s.get("debt", 0) > 0}
    
    if not debtors:
        await message.answer("✅ Наразі жоден магазин не має заборгованості перед платформою.")
        return
        
    await message.answer("📋 **Список магазинів із заборгованістю:**\n(Натисніть кнопку під магазином, щоб списати борг після оплати)")
    
    for s_id, s in debtors.items():
        kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton(text="💵 Оплачено (Обнулити)", callback_data=f"adm_clear_debt_{s_id}")
        )
        await message.answer(
            f"🏪 Магазин: *{s['name']}* (`{s_id}`)\n"
            f"👤 Власник ID: `{s['owner_id']}`\n"
            f"💰 Сума боргу: *{s['debt']} грн*\n"
            f"Статус: {s['status']}",
            reply_markup=kb, parse_mode="Markdown"
        )

@admin_dp.message_handler(text="🔎 Заявки на модерацію")
async def admin_pending_list(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    db = read_db()
    pending_shops = {s_id: s for s_id, s in db.get("shops", {}).items() if s.get("status") == "pending"}
    
    if not pending_shops:
        await message.answer("👌 Немає активних заявок на модерацію. Все перевірено!")
        return
        
    for s_id, s in pending_shops.items():
        kb = InlineKeyboardMarkup(row_width=2).add(
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"adm_appr_{s_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"adm_decl_{s_id}")
        )
        await message.answer(
            f"🔔 **Заявка на модерацію:**\n\n"
            f"🏪 Назва: {s['name']}\n"
            f"🆔 ID: `{s_id}`\n"
            f"🎭 Емодзі: {s['emoji']}",
            reply_markup=kb, parse_mode="Markdown"
        )

@admin_dp.callback_query_handler(lambda call: call.data.startswith('adm_'))
async def handle_admin_actions(call: types.CallbackQuery):
    if str(call.from_user.id) != str(ADMIN_ID): return
    data_parts = call.data.split('_')
    action = data_parts[1]
    db = read_db()
    
    if action == "clear":
        shop_id = data_parts[3]
        if shop_id in db["shops"]:
            old_debt = db["shops"][shop_id]["debt"]
            db["shops"][shop_id]["debt"] = 0.0
            if db["shops"][shop_id]["status"] == "frozen":
                db["shops"][shop_id]["status"] = "active"
            write_db(db)
            await call.message.edit_text(call.message.text + f"\n\n💸 **Борг {old_debt} грн успішно списано! Магазин розморожено.**")
            try:
                await seller_bot.send_message(chat_id=db["shops"][shop_id]["owner_id"], text=f"✅ **Ваш платіж зараховано!** Баланс комісії оновлено, а роботу магазину \"{db['shops'][shop_id]['name']}\" повністю відновлено.")
            except Exception: pass
    else:
        shop_id = data_parts[2]
        if shop_id not in db["shops"]:
            await call.answer("Магазин не знайдено.")
            return
        owner_id = db["shops"][shop_id]["owner_id"]
        
        if action == "appr":
            db["shops"][shop_id]["status"] = "active"
            write_db(db)
            await call.message.edit_text(call.message.text + "\n\n✅ **СХВАЛЕНО! Магазин активований.**")
            try: await seller_bot.send_message(chat_id=owner_id, text=f"🎉 **Ваш магазин \"{db['shops'][shop_id]['name']}\" успішно активований на платформі!**")
            except Exception: pass
        elif action == "decl":
            shop_name = db["shops"][shop_id]["name"]
            del db["shops"][shop_id]
            write_db(db)
            await call.message.edit_text(call.message.text + "\n\n❌ **ВІДХИЛЕНО ТА ВИДАЛЕНО!**")
            try: await seller_bot.send_message(chat_id=owner_id, text=f"❌ **Ваш запит на створення магазину \"{shop_name}\" відхилено модератором.**")
            except Exception: pass
    await call.answer()


# --- АВТО-БИЛЛИНГ И ШТРАФЫ ---
def run_monday_billing_job():
    db = read_db()
    for s_id, s in db["shops"].items():
        if s["debt"] > 0:
            invoice_text = f"📊 **Час щотижневого біллінгу!**\nБорг комісії: *{s['debt']} грн*.\nРеквізити: `4441 1111 2222 3333`."
            try: requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": invoice_text, "parse_mode": "Markdown"}, timeout=10)
            except Exception: pass

def run_tuesday_penalty_job():
    db = read_db()
    changed = False
    for s_id, s in db["shops"].items():
        if s["debt"] > 0 and s["status"] == "active":
            db["shops"][s_id]["status"] = "frozen"
            changed = True
            try: requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": "❌ **Магазин ЗАМОРОЖЕНО за несплату комісії!** Покупці тимчасово не бачать товари."}, timeout=10)
            except Exception: pass
    if changed: write_db(db)

scheduler = BackgroundScheduler(timezone="Europe/Kiev")
scheduler.add_job(run_monday_billing_job, CronTrigger(day_of_week='mon', hour=9, minute=0))
scheduler.add_job(run_tuesday_penalty_job, CronTrigger(day_of_week='tue', hour=9, minute=0))
scheduler.start()

Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000))), daemon=True).start()

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(client_bot.delete_webhook(drop_pending_updates=True))
        loop.run_until_complete(seller_bot.delete_webhook(drop_pending_updates=True))
        loop.run_until_complete(admin_bot.delete_webhook(drop_pending_updates=True))
    except Exception: pass
    
    loop.create_task(client_dp.start_polling())
    loop.create_task(seller_dp.start_polling())
    loop.create_task(admin_dp.start_polling())
    print("🚀 Мультиплатформа с изолированной админкой активна!")
    loop.run_forever()

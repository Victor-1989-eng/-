import os
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from urllib.parse import parse_qs
from threading import Thread

import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils.exceptions import MessageToDeleteNotFound

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

# --- CONFIG & TOKENS ---
CLIENT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SELLER_TOKEN = os.environ.get("SELLER_BOT_TOKEN")
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
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

# --- ЧИСТЫЕ ФУНКЦИИ ПОДКЛЮЧЕНИЯ И ЗАПРОСОВ NEON ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM ban_list WHERE user_id = %s;", (str(user_id),))
    banned = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return banned

def get_owner_shops(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM shops WHERE owner_id = %s;", (int(user_id),))
    shops = cursor.fetchall()
    cursor.close()
    conn.close()
    return {s['shop_id']: dict(s) for s in shops}

# --- МИДДЛВАРЬ И ИСТОРИЯ ЧАТОВ ---
async def save_msg_id(state: FSMContext, message_id: int):
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    msg_ids.append(message_id)
    await state.update_data(messages_to_delete=msg_ids)

async def clear_chat_history(bot: Bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    msg_ids = data.get("messages_to_delete", [])
    for m_id in msg_ids:
        try: await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except MessageToDeleteNotFound: pass
        except Exception: pass
    await state.update_data(messages_to_delete=[])

# --- STATES ---
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

class AdminBroadcastState(StatesGroup):
    target_type = State()
    message_text = State()

class AdminShopSearchState(StatesGroup):
    shop_id = State()

class AdminBanState(StatesGroup):
    user_id = State()

def get_cancel_kb():
    return InlineKeyboardMarkup().add(InlineKeyboardButton(text="❌ Скасувати операцію", callback_data="cancel_action"))


# =======================================================
# 🌐 API ENDPOINTS (FLASK С ПОЛНОЙ МОДЕРНИЗАЦИЕЙ БД)
# =======================================================
@app.route('/')
def home(): 
    return "API Платформы Активна (Neon Modernized)!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, name, emoji, status FROM shops WHERE status = 'active';")
    shops = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify(list(shops))

@app.route('/get-shop-products/<shop_id>', methods=['GET'])
def get_shop_products_endpoint(shop_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # 1. Проверяем статус магазина
        cursor.execute("SELECT status FROM shops WHERE shop_id = %s;", (shop_id,))
        shop = cursor.fetchone()
        if not shop or shop['status'] != 'active':
            return jsonify([])
            
        # 2. Выбираем товары
        cursor.execute("""
            SELECT 
                id, 
                name, 
                category, 
                description, 
                image_url, 
                has_variants, 
                price, 
                variants_json
            FROM products 
            WHERE shop_id = %s;
        """, (shop_id,))
        
        products = cursor.fetchall()
        
        formatted_products = []
        for p in products:
            prod_dict = dict(p)
            
            # Принудительно переводим ID в строку, чтобы во фронтенде работало сравнение (p.id === id)
            prod_dict['id'] = str(prod_dict['id'])
            
            # Парсим варианты в массив объектов для фронтенда
            v_data = prod_dict.get('variants_json')
            if v_data is not None:
                if isinstance(v_data, str):
                    try:
                        prod_dict['variants'] = json.loads(v_data)
                    except Exception:
                        prod_dict['variants'] = []
                else:
                    prod_dict['variants'] = v_data
            else:
                prod_dict['variants'] = []
                
            if 'variants_json' in prod_dict:
                del prod_dict['variants_json']
                
            # Цену тоже делаем строкой на всякий случай
            prod_dict['price'] = str(prod_dict['price'])
                
            formatted_products.append(prod_dict)

        return jsonify(formatted_products)
        
    except Exception as e:
        logging.error(f"Ошибка в get-shop-products: {e}")
        return jsonify([])
    finally:
        cursor.close()
        conn.close()
            
            
            

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
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        req_data = request.get_json()
        cart = req_data['cart']
        delivery = req_data['delivery']
        init_data_raw = req_data['initData']
        shop_id = req_data.get('shop_id')

        # Защита веб-сервера от падения при парсинге initData
        try:
            parsed = parse_qs(init_data_raw)
            if 'user' not in parsed:
                return jsonify({"status": "error", "message": "Invalid initData"}), 400
            user_json = json.loads(parsed['user'][0])
            buyer_tg_id = user_json.get('id')
        except Exception:
            return jsonify({"status": "error", "message": "Failed to parse initData"}), 400

        # Проверка бана
        cursor.execute("SELECT 1 FROM ban_list WHERE user_id = %s;", (str(buyer_tg_id),))
        if cursor.fetchone():
            return jsonify({"status": "error", "message": "User banned"}), 403

        # Проверка активности магазина
        cursor.execute("SELECT name, owner_id, status FROM shops WHERE shop_id = %s;", (shop_id,))
        shop = cursor.fetchone()
        if not shop or shop['status'] != 'active':
            return jsonify({"status": "error", "message": "Shop not active"}), 403

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            variant_str = f" ({item['selected_variant']} мл)" if item.get('selected_variant') else ""
            item_total = float(item['price']) * int(item.get('qty', 1))
            total_price += item_total
            p_text += f"{idx}. 📦 *{item['name']}{variant_str}*\n   🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        commission = round(total_price * 0.05, 2)

        # Сохраняем заказ атомарно в базу данных
        cursor.execute("""
            INSERT INTO orders (shop_id, buyer_id, total_price, commission, status, delivery_json, cart_json)
            VALUES (%s, %s, %s, %s, 'new', %s, %s) RETURNING id;
        """, (shop_id, buyer_tg_id, total_price, commission, json.dumps(delivery), json.dumps(cart)))
        
        order_id = cursor.fetchone()['id']
        conn.commit()

        # Клавиатура для продавца (передаем валидную JSON-строку)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"ord_approve:{buyer_tg_id}:{commission}:{shop_id}:{order_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}:{order_id}")
        )

        owner_text = (
            f"📥 **НОВЕ ЗАМОВЛЕННЯ №{order_id}!**\n"
            f"🏪 Магазин: *{shop['name']}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n"
        )
        
        requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={
            "chat_id": shop['owner_id'], "text": owner_text, "parse_mode": "Markdown", "reply_markup": json.dumps(kb.to_python())
        }, timeout=10)

        return jsonify({"status": "pending_approval", "order_id": order_id}), 200
    except Exception as e:
        conn.rollback()
        logging.error(f"Error in submit-order: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()
        conn.close()


# =======================================================
# 🛍️ БОТ ПОКУПАТЕЛЕЙ (CLIENT BOT)
# =======================================================
@client_dp.message_handler(commands=['start'])
async def client_welcome(message: types.Message):
    if is_banned(message.from_user.id): return
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
    if is_banned(message.from_user.id): return
    await state.finish() 
    await message.answer("👋 **Вітаємо в кабінеті керування для Продавців платформи!**", reply_markup=get_seller_menu())

@seller_dp.message_handler(text="🏪 Мої Магазини", state='*')
async def seller_shops_list(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
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
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM shops WHERE shop_id = %s;", (s_id,))
    shop = cursor.fetchone()
    
    if not shop: 
        cursor.close()
        conn.close()
        return
        
    cursor.execute("SELECT COUNT(*) as cnt FROM products WHERE shop_id = %s;", (s_id,))
    prod_cnt = cursor.fetchone()['cnt']
    cursor.close()
    conn.close()
    
    status_map = {"active": "✅ Активний", "pending": "⏳ На модерації (не видно в додатку)", "frozen": "❌ Заморожений"}
    text = (
        f"🏪 **Управління магазином: {shop['name']}**\n\n"
        f"• ID бренду: `{s_id}`\n"
        f"• Статус в системі: {status_map.get(shop['status'], shop['status'])}\n"
        f"• Кількість товарів: {prod_cnt} шт.\n"
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

# --- МАСТЕР СОЗДАНИЯ МАГАЗИНА ---
@seller_dp.callback_query_handler(lambda call: call.data == "make_shop_wizard")
async def start_shop_wizard(call: types.CallbackQuery, state: FSMContext):
    m1 = await call.message.answer(
        "📝 **КРОК 1 из 3: Унікальний ID бренду**\n\n"
        "Введіть короткий техничний ID вашого магазину англійськими літерами (наприклад: `perfume_shop`).\n"
        "⚠️ ID має складатися тільки з латиниці, цифр або знаку підкреслення, без пробілів!",
        reply_markup=get_cancel_kb()
    )
    await CreateShopState.shop_id.set()
    await save_msg_id(state, call.message.message_id)
    await save_msg_id(state, m1.message_id)

@seller_dp.message_handler(state=CreateShopState.shop_id)
async def process_shop_id(message: types.Message, state: FSMContext):
    s_id = message.text.strip().lower()
    await save_msg_id(state, message.message_id)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM shops WHERE shop_id = %s;", (s_id,))
    exists = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    
    if exists:
        m_err = await message.answer("❌ Цей ID вже зайнятий іншим користувачем! Введіть іншу комбінацію літер:")
        await save_msg_id(state, m_err.message_id)
        return
        
    await state.update_data(shop_id=s_id)
    m2 = await message.answer(
        "📝 **КРОК 2 из 3: Публічна назва**\n\n"
        "Введіть красиву назву вашого магазину, яку бачитимуть покупці на вітрині (наприклад: `Elite Perfume UA`):",
        reply_markup=get_cancel_kb()
    )
    await CreateShopState.name.set()
    await save_msg_id(state, m2.message_id)

@seller_dp.message_handler(state=CreateShopState.name)
async def process_shop_name(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(name=message.text.strip())
    m3 = await message.answer(
        "📝 **КРОК 3 из 3: Іконка-Емодзі**\n\n"
        "Надішліть **рівно один емодзі**, який найкраще характеризує ваш асортимент (наприклад: 🧴, 🛍️, 💄).\n"
        "Цей емодзі стане логотипом вашої вкладки в додатку.",
        reply_markup=get_cancel_kb()
    )
    await CreateShopState.emoji.set()
    await save_msg_id(state, m3.message_id)

@seller_dp.message_handler(state=CreateShopState.emoji)
async def process_shop_emoji(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO shops (shop_id, name, emoji, owner_id, debt, status)
        VALUES (%s, %s, %s, %s, 0.00, 'pending');
    """, (user_data['shop_id'], user_data['name'], emoji, message.from_user.id))
    conn.commit()
    cursor.close()
    conn.close()
    
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
                 f"Прийміть рішення щодо активації бренда:",
            reply_markup=admin_kb, parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

    await clear_chat_history(seller_bot, message.chat.id, state)
    await message.answer("⏳ **Магазин успішно створено та відправлено на модерацію адміну!**\nВін з'явиться на вітрині сайту одразу після схвалення.", reply_markup=get_seller_menu())
    await state.finish()

# --- ДОБАВЛЕНИЕ ТОВАРОВ В БД ---
@seller_dp.message_handler(text="➕ Додати новий товар", state='*')
async def add_product_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.finish() 
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        await message.answer("❌ У вас немає створених магазинів! Спочатку створіть хоча б один бренд.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"addto_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))
    m1 = await message.answer("📋 **КРОК 1:** Оберіть магазин зі списку, куди буде завантажено товар:", reply_markup=kb)
    await AddProductState.target_shop.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m1.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('addto_'), state=AddProductState.target_shop)
async def add_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.split('_')[1]
    await state.update_data(target_shop=shop_id)
    m2 = await call.message.answer("✍️ **КРОК 2:** Введіть **публічну назву товару** (наприклад: `Chanel Bleau de Chanel`):", reply_markup=get_cancel_kb())
    await AddProductState.name.set()
    await save_msg_id(state, m2.message_id)

@seller_dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(name=message.text.strip())
    m3 = await message.answer("✍️ **КРОК 3:** Введіть **назву категорії** для групування на сайті (наприклад: `Чоловічі парфуми`, `Унісекс`):", reply_markup=get_cancel_kb())
    await AddProductState.category.set()
    await save_msg_id(state, m3.message_id)

@seller_dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(category=message.text.strip())
    kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton(text="Так (Різні об'єми)", callback_data="var_yes"), 
        InlineKeyboardButton(text="Ні (Фіксована ціна)", callback_data="var_no")
    )
    m4 = await message.answer("✍️ **КРОК 4:** Чи має ваш товар варіативність в мілілітрах?", reply_markup=kb)
    await AddProductState.has_variants.set()
    await save_msg_id(state, m4.message_id)

@seller_dp.callback_query_handler(lambda call: call.data in ["var_yes", "var_no"], state=AddProductState.has_variants)
async def add_product_variant_decision(call: types.CallbackQuery, state: FSMContext):
    if call.data == "var_yes":
        await state.update_data(has_variants=True)
        m5 = await call.message.answer("✍️ **КРОК 5:** Введіть доступні об'єми **через кому без пробілів** (наприклад: `10,30,50`):", reply_markup=get_cancel_kb())
        await AddProductState.variants_list.set()
    else:
        await state.update_data(has_variants=False)
        m5 = await call.message.answer("✍️ **КРОК 5:** Введіть фіксовану **вартість товару в гривнях** (тільки число):", reply_markup=get_cancel_kb())
        await AddProductState.single_price.set()
    await save_msg_id(state, m5.message_id)

@seller_dp.message_handler(state=AddProductState.single_price)
async def add_product_single_price(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(single_price=message.text.strip())
    m6 = await message.answer("✍️ **КРОК 6:** Напишіть **розгорнутий опис товару**:", reply_markup=get_cancel_kb())
    await AddProductState.description.set()
    await save_msg_id(state, m6.message_id)

@seller_dp.message_handler(state=AddProductState.variants_list)
async def add_product_variants_list(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    v_list = [v.strip() for v in message.text.split(',') if v.strip()]
    await state.update_data(v_list=v_list, v_index=0, compiled_variants=[])
    m6 = await message.answer(f"Введіть ціну в грн для об'єму **{v_list[0]} мл**:", reply_markup=get_cancel_kb())
    await AddProductState.variants_prices.set()
    await save_msg_id(state, m6.message_id)

@seller_dp.message_handler(state=AddProductState.variants_prices)
async def add_product_variants_prices(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    price = message.text.strip()
    s_data = await state.get_data()
    v_list = s_data["v_list"]
    v_idx = s_data["v_index"]
    compiled_variants = s_data["compiled_variants"]
    
    compiled_variants.append({"volume": v_list[v_idx], "price": price})
    next_idx = v_idx + 1
    
    if next_idx < len(v_list):
        await state.update_data(v_index=next_idx, compiled_variants=compiled_variants)
        m_next = await message.answer(f"Введіть ціну в грн для об'єму **{v_list[next_idx]} мл**:", reply_markup=get_cancel_kb())
        await save_msg_id(state, m_next.message_id)
    else:
        await state.update_data(compiled_variants=compiled_variants)
        m_desc = await message.answer("✍️ **КРОК 6:** Напишіть **розгорнутий опис товару**:", reply_markup=get_cancel_kb())
        await AddProductState.description.set()
        await save_msg_id(state, m_desc.message_id)

@seller_dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(description=message.text.strip())
    m7 = await message.answer("✍️ **КРОК 7 (Фінал):** Надішліть **одне якісне фото товару**:", reply_markup=get_cancel_kb())
    await AddProductState.image.set()
    await save_msg_id(state, m7.message_id)

@seller_dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    photo = message.photo[-1]
    file_info = await seller_bot.get_file(photo.file_id)
    img_url = f"https://api.telegram.org/file/bot{SELLER_TOKEN}/{file_info.file_path}"
    
    user_data = await state.get_data()
    s_id = user_data["target_shop"]
    
    has_vars = user_data["has_variants"]
    base_price = user_data["compiled_variants"][0]["price"] if has_vars else user_data["single_price"]
    vars_json = json.dumps(user_data["compiled_variants"]) if has_vars else "[]"

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO products (shop_id, name, category, description, image_url, has_variants, price, variants_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
    """, (s_id, user_data["name"], user_data["category"], user_data["description"], img_url, has_vars, base_price, vars_json))
    conn.commit()
    cursor.close()
    conn.close()
    
    await clear_chat_history(seller_bot, message.chat.id, state)
    await message.answer("✅ **Товар успішно додано до каталогу бренду та опубліковано на вітрині!**", reply_markup=get_seller_menu())
    await state.finish()

@seller_dp.callback_query_handler(lambda call: call.data == "cancel_product_creation", state='*')
async def cancel_product_creation_handler(call: types.CallbackQuery, state: FSMContext):
    await clear_chat_history(seller_bot, call.message.chat.id, state)
    await state.finish()
    await call.message.answer("❌ Додавання товару скасовано.", reply_markup=get_seller_menu())
    await call.answer()

@seller_dp.message_handler(text="🗑️ Видалити товар", state='*')
async def delete_product_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.finish()
    shops = get_owner_shops(message.from_user.id)
    if not shops: return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items(): kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"listdel_{s_id}"))
    await message.answer("📋 Оберіть магазин для видалення товару:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('listdel_'))
async def delete_product_list(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, name FROM products WHERE shop_id = %s;", (s_id,))
    prods = cursor.fetchall()
    cursor.close()
    conn.close()
    
    kb = InlineKeyboardMarkup(row_width=1)
    for p in prods:
        kb.add(InlineKeyboardButton(text=f"🗑️ {p['name']}", callback_data=f"confprod_{s_id}_{p['id']}"))
    await call.message.edit_text("Оберіть товар для видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confprod_'))
async def delete_product_execute(call: types.CallbackQuery):
    _, s_id, p_id = call.data.split('_')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM products WHERE id = %s AND shop_id = %s;", (int(p_id), s_id))
    conn.commit()
    cursor.close()
    conn.close()
    await call.message.edit_text("✅ Товар видалено.")

@seller_dp.message_handler(text="❌ Видалити магазин", state='*')
async def delete_shop_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await state.finish()
    shops = get_owner_shops(message.from_user.id)
    if not shops: return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items(): kb.add(InlineKeyboardButton(text=f"❌ {s['name']}", callback_data=f"delshop_{s_id}"))
    await message.answer("⚠️ Оберіть магазин для ПОВНОГО видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('delshop_'))
async def delete_shop_execute(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM shops WHERE shop_id = %s AND owner_id = %s;", (s_id, call.from_user.id))
    conn.commit()
    cursor.close()
    conn.close()
    await call.message.edit_text("💥 Магазин повністю видалено.")

# --- ПОДТВЕРЖДЕНИЕ / ОТКЛОНЕНИЕ ЗАКАЗА ПРОДАВЦОМ ---
@seller_dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action, buyer_id = action_data[0], action_data[1]
    order_id = int(action_data[-1])
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if action == "ord_approve":
        commission = float(action_data[2])
        shop_id = action_data[3]
        
        # Начисляем комиссию в долг и обновляем статус заказа атомарно
        cursor.execute("UPDATE shops SET debt = debt + %s WHERE shop_id = %s;", (commission, shop_id))
        cursor.execute("UPDATE orders SET status = 'approved' WHERE id = %s;", (order_id,))
        
        # Берем название для красивого текста
        cursor.execute("SELECT name FROM shops WHERE shop_id = %s;", (shop_id,))
        shop_name = cursor.fetchone()['name']
        conn.commit()
        
        try:
            await client_bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{shop_name}\" підтверджено!**")
        except Exception: pass
        
        # МОМЕНТАЛЬНОЕ УВЕДОМЛЕНИЕ О ПРИБЫЛИ В АДМИНКУ В USDT
        # Считаем эквивалент в долларах по стандартному курсу платформы или выводим напрямую
        await admin_bot.send_message(ADMIN_ID, f"💰 **Earned ${commission:.2f} USDT**\n(Комісія {commission} грн з замовлення бренду `{shop_id}`).")
        await call.message.edit_text(call.message.text + f"\n\n✅ Підтверджено. Комісія {commission} грн додана до рахунку.")
        
    elif action == "ord_decline":
        cursor.execute("UPDATE orders SET status = 'declined' WHERE id = %s;", (order_id,))
        conn.commit()
        try:
            await client_bot.send_message(buyer_id, "❌ Замовлення відхилено продавцем.")
        except Exception: pass
        await call.message.edit_text(call.message.text + "\n\n❌ Скасовано.")
        
    cursor.close()
    conn.close()
    await call.answer()


# =======================================================
# 🔐 ВЕТКА АДМИН-БОТА (ADMIN BOT)
# =======================================================
def get_admin_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📊 Статистика платформи"), 
        KeyboardButton("💰 Хто винен (Борги)"),
        KeyboardButton("🔎 Заявки на модерацію"),
        KeyboardButton("📢 Масова рассылка"),
        KeyboardButton("🔍 Управління магазином (Пошук)"),
        KeyboardButton("⛔ Чорний список (Бан)")
    )
    return kb

@admin_dp.message_handler(commands=['start'], state='*')
async def admin_start(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != str(ADMIN_ID): return
    await state.finish()
    await message.answer(
        "🔒 **Вітаємо в розширеній інженерній панелі керування маркетплейсом!**\n\n"
        "Усі системи активні.", 
        reply_markup=get_admin_menu()
    )

@admin_dp.callback_query_handler(lambda call: call.data == "cancel_action", state='*')
async def cancel_admin_action(call: types.CallbackQuery, state: FSMContext):
    if str(call.from_user.id) != str(ADMIN_ID): return
    await clear_chat_history(admin_bot, call.message.chat.id, state)
    await state.finish()
    await call.message.answer("❌ Операцію скасовано. Повернення до головного меню.", reply_markup=get_admin_menu())
    await call.answer()

# 1. ЧИСТАЯ СТАТИСТИКА
@admin_dp.message_handler(text="📊 Статистика платформи", state='*')
async def admin_stats(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active, SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending, SUM(CASE WHEN status='frozen' THEN 1 ELSE 0 END) as frozen FROM shops;")
    s_stats = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) as cnt, SUM(total_price) as turnover, SUM(commission) as earnings FROM orders WHERE status='approved';")
    o_stats = cursor.fetchone()
    
    cursor.execute("SELECT id, shop_id, total_price, commission, created_at FROM orders ORDER BY id DESC LIMIT 7;")
    last_orders = cursor.fetchall()
    
    cursor.close()
    conn.close()

    log_text = ""
    for o in last_orders:
        date_str = o['created_at'].strftime("%d.%m.%Y %H:%M")
        log_text += f"• `[{date_str}]` Бренд: `{o['shop_id']}` | Сума: {o['total_price']} грн (Комісія: {o['commission']} грн)\n"
    if not log_text: log_text = "Історія замовлень порожня.\n"

    turnover = o_stats['turnover'] or 0
    earnings = o_stats['earnings'] or 0

    text = (
        f"📊 **СТАТИСТИКА ПЛАТФОРМИ:**\n\n"
        f"🏪 Всього брендів: *{s_stats['total']}*\n"
        f"  └ ✅ Активні: {s_stats['active']}\n"
        f"  └ ⏳ Модерація: {s_stats['pending']}\n"
        f"  └ ❌ Заморожені: {s_stats['frozen']}\n\n"
        f"🛍️ Успішних угод: *{o_stats['cnt']}*\n"
        f"💰 Загальний оборот сайту: *{turnover} грн*\n"
        f"📈 Заробіток платформи: *{earnings} грн*\n\n"
        f"📋 **ОСТАННІ ЗАМОВЛЕННЯ (ЖУРНАЛ АУДИТУ):**\n{log_text}"
    )
    await message.answer(text, parse_mode="Markdown")

# 2. МАССОВАЯ РАССЫЛКА
@admin_dp.message_handler(text="📢 Масова рассылка", state='*')
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != str(ADMIN_ID): return
    await state.finish()
    kb = InlineKeyboardMarkup(row_width=1).add(
        InlineKeyboardButton(text="💼 Усім продавцям (Seller Bot)", callback_data="bc_type_seller"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_action")
    )
    m = await message.answer("📢 **МАСТЕР МАСОВОЇ РАССИЛКИ**", reply_markup=kb)
    await AdminBroadcastState.target_type.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m.message_id)

@admin_dp.callback_query_handler(lambda call: call.data.startswith('bc_type_'), state=AdminBroadcastState.target_type)
async def admin_broadcast_type_selected(call: types.CallbackQuery, state: FSMContext):
    b_type = call.data.split('_')[2]
    await state.update_data(target_type=b_type)
    m = await call.message.answer(f"✍️ **Введіть текст повідомлення для продавців:**", reply_markup=get_cancel_kb())
    await AdminBroadcastState.message_text.set()
    await save_msg_id(state, m.message_id)

@admin_dp.message_handler(state=AdminBroadcastState.message_text)
async def admin_broadcast_execute(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    s_data = await state.get_data()
    text_to_send = message.text
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT owner_id FROM shops;")
    targets = cursor.fetchall()
    cursor.close()
    conn.close()
            
    success_cnt = 0
    for row in targets:
        try:
            await seller_bot.send_message(chat_id=row[0], text=f"📢 **ПОВІДОМЛЕННЯ ВІД АДМІНІСТРАЦІЇ:**\n\n{text_to_send}")
            success_cnt += 1
        except Exception: pass
        
    await clear_chat_history(admin_bot, message.chat.id, state)
    await message.answer(f"🚀 **Рассылка успішно завершена!**\nДоставлено продавцям: {success_cnt} шт.", reply_markup=get_admin_menu())
    await state.finish()

# 3. ПОИСК МАГАЗИНА
@admin_dp.message_handler(text="🔍 Управління магазином (Пошук)", state='*')
async def admin_search_shop_start(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != str(ADMIN_ID): return
    await state.finish()
    m = await message.answer("🔍 **ПОШУК БРЕНДУ В СИСТЕМІ**", reply_markup=get_cancel_kb())
    await AdminShopSearchState.shop_id.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m.message_id)

@admin_dp.message_handler(state=AdminShopSearchState.shop_id)
async def admin_search_shop_execute(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    s_id = message.text.strip().lower()
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM shops WHERE shop_id = %s;", (s_id,))
    shop = cursor.fetchone()
    cursor.close()
    conn.close()
    
    if not shop:
        m_err = await message.answer("❌ Магазин з таким ID не знайдено! Спробуйте ще раз:")
        await save_msg_id(state, m_err.message_id)
        return
        
    await clear_chat_history(admin_bot, message.chat.id, state)
    
    kb = InlineKeyboardMarkup(row_width=2).add(
        InlineKeyboardButton(text="❄️ Заморозити", callback_data=f"man_freeze_{s_id}"),
        InlineKeyboardButton(text="🔥 Розморозити", callback_data=f"man_unfreeze_{s_id}"),
        InlineKeyboardButton(text="🗑️ Повне видалення", callback_data=f"man_forcedel_{s_id}"),
        InlineKeyboardButton(text="❌ Закрити", callback_data="cancel_action")
    )
    
    await message.answer(
        f"🏪 **КАРТКА КЕРУВАННЯ МАГАЗИНОМ:**\n\n"
        f"• Назва бренда: *{shop['name']}*\n"
        f"• ID в базі: `{s_id}`\n"
        f"• Власник (Telegram ID): `{shop['owner_id']}`\n"
        f"• Баланс заборгованості: *{shop['debt']} грн*\n"
        f"• Поточний статус: *{shop['status']}*\n", reply_markup=kb, parse_mode="Markdown"
    )
    await state.finish()

# 4. СИСТЕМА БАНА
@admin_dp.message_handler(text="⛔ Чорний список (Бан)", state='*')
async def admin_ban_start(message: types.Message, state: FSMContext):
    if str(message.from_user.id) != str(ADMIN_ID): return
    await state.finish()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM ban_list;")
    banned_rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    banned_str = ", ".join([f"`{row[0]}`" for row in banned_rows]) if banned_rows else "Список порожній"
    
    m = await message.answer(
        f"⛔ **БАН-СИСТЕМА (ЧОРНИЙ СПИСОК)**\n\nЗабанені ID:\n{banned_str}\n\n✍️ Введіть ID для бан/розбан:", 
        reply_markup=get_cancel_kb(), parse_mode="Markdown"
    )
    await AdminBanState.user_id.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m.message_id)

@admin_dp.message_handler(state=AdminBanState.user_id)
async def admin_ban_execute(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    target_id = message.text.strip()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM ban_list WHERE user_id = %s;", (target_id,))
    exists = cursor.fetchone() is not None
    
    if exists:
        cursor.execute("DELETE FROM ban_list WHERE user_id = %s;", (target_id,))
        msg_text = f"✅ Користувача `{target_id}` успішно **розбанено**!"
    else:
        cursor.execute("INSERT INTO ban_list (user_id) VALUES (%s);", (target_id,))
        msg_text = f"⛔ Користувача `{target_id}` успішно **забанено**!"
        
    conn.commit()
    cursor.close()
    conn.close()
    
    await clear_chat_history(admin_bot, message.chat.id, state)
    await message.answer(msg_text, reply_markup=get_admin_menu(), parse_mode="Markdown")
    await state.finish()

# 5. КНОПКИ БОРГОВ И МОДЕРАЦИИ
@admin_dp.message_handler(text="💰 Хто винен (Борги)", state='*')
async def admin_debts(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, name, debt, status FROM shops WHERE debt > 0;")
    debtors = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not debtors:
        await message.answer("✅ Наразі жоден магазин не має заборгованості.")
        return
        
    await message.answer("📋 **Список магазинів із заборгованістю:**")
    for s in debtors:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="💵 Оплачено (Обнулити)", callback_data=f"adm_clear_debt_{s['shop_id']}"))
        await message.answer(f"🏪 Магазин: *{s['name']}* (`{s['shop_id']}`)\n💰 Сума боргу: *{s['debt']} грн*", reply_markup=kb, parse_mode="Markdown")

@admin_dp.message_handler(text="🔎 Заявки на модерацію", state='*')
async def admin_pending_list(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, name FROM shops WHERE status = 'pending';")
    pending_shops = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not pending_shops:
        await message.answer("👌 Немає активних заявок на модерацію. Все перевірено!")
        return
    for s in pending_shops:
        kb = InlineKeyboardMarkup(row_width=2).add(InlineKeyboardButton(text="✅ Схвалити", callback_data=f"adm_appr_{s['shop_id']}"), InlineKeyboardButton(text="❌ Відхилити", callback_data=f"adm_decl_{s['shop_id']}"))
        await message.answer(f"🔔 **Заявка:**\n🏪 Назва: {s['name']}\n🆔 ID: `{s['shop_id']}`", reply_markup=kb, parse_mode="Markdown")

@admin_dp.callback_query_handler(lambda call: call.data.startswith(('adm_', 'man_')), state='*')
async def handle_all_admin_callbacks(call: types.CallbackQuery):
    if str(call.from_user.id) != str(ADMIN_ID): return
    data_parts = call.data.split('_')
    prefix, action, target = data_parts[0], data_parts[1], data_parts[2]
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if action == "clear":
        shop_id = data_parts[3]
        cursor.execute("SELECT debt, owner_id FROM shops WHERE shop_id = %s;", (shop_id,))
        sh = cursor.fetchone()
        if sh:
            cursor.execute("UPDATE shops SET debt = 0.00, status = CASE WHEN status='frozen' THEN 'active' ELSE status END WHERE shop_id = %s;", (shop_id,))
            conn.commit()
            await call.message.edit_text(call.message.text + f"\n\n💸 **Борг {sh['debt']} грн списано!**")
            try: await seller_bot.send_message(sh['owner_id'], "✅ **Ваш платіж зараховано!**")
            except Exception: pass
            
    elif prefix == "man":
        cursor.execute("SELECT owner_id FROM shops WHERE shop_id = %s;", (target,))
        sh = cursor.fetchone()
        if sh:
            if action == "freeze":
                cursor.execute("UPDATE shops SET status = 'frozen' WHERE shop_id = %s;", (target,))
                await call.message.edit_text(call.message.text + "\n\n❄️ **Магазин заблоковано вручную!**")
                try: await seller_bot.send_message(sh['owner_id'], "⚠️ **Ваш магазин був тимчасово заморожений адміністрацією сайту!**")
                except Exception: pass
            elif action == "unfreeze":
                cursor.execute("UPDATE shops SET status = 'active' WHERE shop_id = %s;", (target,))
                await call.message.edit_text(call.message.text + "\n\n🔥 **Магазин успішно розморожений!**")
                try: await seller_bot.send_message(sh['owner_id'], "🎉 **Роботу вашого магазину повністю відновлено!**")
                except Exception: pass
            elif action == "forcedel":
                cursor.execute("DELETE FROM shops WHERE shop_id = %s;", (target,))
                await call.message.edit_text(call.message.text + "\n\n💥 **Бренд безповоротно видалено з системи!**")
            conn.commit()
        
    else:
        cursor.execute("SELECT owner_id, name FROM shops WHERE shop_id = %s;", (target,))
        sh = cursor.fetchone()
        if sh:
            if action == "appr":
                cursor.execute("UPDATE shops SET status = 'active' WHERE shop_id = %s;", (target,))
                await call.message.edit_text(call.message.text + "\n\n✅ **СХВАЛЕНО!**")
                try: await seller_bot.send_message(sh['owner_id'], f"🎉 **Ваш магазин \"{sh['name']}\" успішно активований!**")
                except Exception: pass
            elif action == "decl":
                cursor.execute("DELETE FROM shops WHERE shop_id = %s;", (target,))
                await call.message.edit_text(call.message.text + "\n\n❌ **ВІДХИЛЕНО ТА ВИДАЛЕНО!**")
            conn.commit()
            
    cursor.close()
    conn.close()
    await call.answer()


# --- АВТО-БИЛЛИНГ С КЛАССИЧЕСКИМ ИЗМЕНЕНИЕМ СТАТУСА ---
def run_monday_billing_job():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, name, owner_id, debt FROM shops WHERE debt > 0;")
    shops = cursor.fetchall()
    cursor.close()
    conn.close()
    
    for s in shops:
        invoice_text = f"📊 **Час щотижневого біллінгу!**\nБорг комісії: *{s['debt']} грн*.\nРеквізити: `4441 1111 2222 3333`."
        try: requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": invoice_text, "parse_mode": "Markdown"}, timeout=10)
        except Exception: pass

def run_tuesday_penalty_job():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, owner_id FROM shops WHERE debt > 0 AND status = 'active';")
    shops = cursor.fetchall()
    
    for s in shops:
        cursor.execute("UPDATE shops SET status = 'frozen' WHERE shop_id = %s;", (s['shop_id'],))
        try: requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": "❌ **Магазин ЗАМОРОЖЕНО за несплату комісії!**"}, timeout=10)
        except Exception: pass
        
    conn.commit()
    cursor.close()
    conn.close()

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
    print("🚀 Модернизированная инфраструктура Neon запущена!")
    loop.run_forever()

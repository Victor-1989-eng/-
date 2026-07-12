import os
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
from aiogram.utils.exceptions import MessageToDeleteNotFound

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Импорты для ORM SQLAlchemy
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, String, Float, Integer, BigInteger, Boolean, ForeignKey, Text
from sqlalchemy.future import select

logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ И ТОКЕНЫ ---
CLIENT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SELLER_TOKEN = os.environ.get("SELLER_BOT_TOKEN")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

if not CLIENT_TOKEN or not SELLER_TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Проверьте токены ботов и ID админа в настройках Render!")

client_bot = Bot(token=CLIENT_TOKEN)
client_dp = Dispatcher(client_bot, storage=MemoryStorage())

seller_bot = Bot(token=SELLER_TOKEN)
seller_dp = Dispatcher(seller_bot, storage=MemoryStorage())

app = Flask('')
CORS(app)

# --- НАСТРОЙКА БАЗЫ ДАННЫХ POSTGRESQL (SQLALCHEMY) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    # Отсекаем хвост с query-параметрами (?sslmode=...), чтобы они не ломали драйвер
    if "?" in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.split("?")[0]
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = "sqlite+aiosqlite:///fallback_local.db"

# Передаем правильный SSL-контекст, который понимает asyncpg
connect_args = {}
if "postgresql+asyncpg" in DATABASE_URL:
    connect_args = {"ssl": "require"}

engine = create_async_engine(
    DATABASE_URL, 
    echo=False, 
    pool_pre_ping=True, 
    connect_args=connect_args
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# --- МОДЕЛИ ТАБЛИЦ ---
class Shop(Base):
    __tablename__ = "shops"
    
    shop_id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    emoji = Column(String, nullable=False)
    owner_id = Column(BigInteger, nullable=False, index=True) 
    debt = Column(Float, default=0.0)
    status = Column(String, default="active")
    
    products = relationship("Product", back_populates="shop", cascade="all, delete-orphan")

class Product(Base):
    __tablename__ = "products"
    
    id = Column(String, primary_key=True)
    shop_id = Column(String, ForeignKey("shops.shop_id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    has_variants = Column(Boolean, default=False)
    price = Column(String, nullable=False) 
    variants_json = Column(Text, nullable=True)

    shop = relationship("Shop", back_populates="products")

class OrderRecord(Base):
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, nullable=False)
    shop_id = Column(String, nullable=False)
    total = Column(Float, nullable=False)
    commission = Column(Float, nullable=False)

async def init_db_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# --- АСИНХРОННЫЕ ФУНКЦИИ КВЕРИНГА БД ---
async def get_owner_shops_db(user_id):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Shop).where(Shop.owner_id == int(user_id)))
        shops = result.scalars().all()
        return {s.shop_id: {"name": s.name, "emoji": s.emoji, "owner_id": s.owner_id, "debt": s.debt, "status": s.status} for s in shops}

async def get_shop_db(shop_id):
    async with AsyncSessionLocal() as session:
        return await session.get(Shop, shop_id)

async def add_shop_db(shop_id, name, emoji, owner_id):
    async with AsyncSessionLocal() as session:
        new_shop = Shop(shop_id=shop_id, name=name, emoji=emoji, owner_id=int(owner_id), debt=0.0, status="active")
        session.add(new_shop)
        await session.commit()

async def add_product_db(shop_id, prod_id, name, category, description, image_url, has_variants, price, variants=None):
    async with AsyncSessionLocal() as session:
        v_json = json.dumps(variants, ensure_ascii=False) if variants else None
        new_prod = Product(
            id=prod_id, shop_id=shop_id, name=name, category=category,
            description=description, image_url=image_url, has_variants=has_variants,
            price=str(price), variants_json=v_json
        )
        session.add(new_prod)
        await session.commit()

async def get_shop_products_db(shop_id):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Product).where(Product.shop_id == shop_id))
        products = result.scalars().all()
        output = []
        for p in products:
            item = {
                "id": p.id, "name": p.name, "category": p.category,
                "description": p.description, "image_url": p.image_url,
                "has_variants": p.has_variants, "price": p.price
            }
            if p.variants_json:
                item["variants"] = json.loads(p.variants_json)
            output.append(item)
        return output

async def delete_product_db(shop_id, prod_id):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Product).where(Product.shop_id == shop_id, Product.id == prod_id))
        p = result.scalar_one_or_none()
        if p:
            await session.delete(p)
            await session.commit()

async def delete_shop_db(shop_id):
    async with AsyncSessionLocal() as session:
        shop = await session.get(Shop, shop_id)
        if shop:
            await session.delete(shop)
            await session.commit()

# --- СОСТОЯНИЯ FSM ДЛЯ БОТА ПРОДАВЦОВ ---
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

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ОЧИСТКИ ОЧЕРЕДИ СООБЩЕНИЙ ---
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
        except MessageToDeleteNotFound:
            pass
        except Exception:
            pass

def get_cancel_kb():
    return InlineKeyboardMarkup().add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))

# --- ENDPOINTS ДЛЯ ВЕБ-МАГАЗИНА (FLASK) ---
@app.route('/')
def home(): 
    return "API Мультивендорной Платформы Активно!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    async def fetch():
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Shop))
            return res.scalars().all()
    shops = asyncio.run(fetch())
    flat_shops = [{"shop_id": s.shop_id, "name": s.name, "emoji": s.emoji, "status": s.status} for s in shops]
    return jsonify(flat_shops)

@app.route('/get-shop-products/<shop_id>', methods=['GET'])
def get_shop_products(shop_id):
    products = asyncio.run(get_shop_products_db(shop_id))
    return jsonify(products)

@app.route('/get-cities', methods=['POST'])
def get_np_cities():
    data = request.get_json() or {}
    payload = {
        "apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getCities",
        "methodProperties": {"FindByString": data.get('cityName', ''), "Limit": "20"}
    }
    try: return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))
    except: return jsonify([])

@app.route('/get-warehouses', methods=['POST'])
def get_np_warehouses():
    data = request.get_json() or {}
    payload = {
        "apiKey": API_KEY_NOVAPOSHTA, "modelName": "Address", "calledMethod": "getWarehouses",
        "methodProperties": {"CityRef": data.get('cityRef', ''), "Limit": "500"}
    }
    try: return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))
    except: return jsonify([])

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

        shop = asyncio.run(get_shop_db(shop_id))

        if not shop or shop.status == 'frozen':
            return jsonify({"status": "error", "message": "Shop frozen or not found"}), 403

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
            f"🏪 Магазин: *{shop.name}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n"
        )
        
        requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={
            "chat_id": shop.owner_id, "text": owner_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
        })

        return jsonify({"status": "pending_approval"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =======================================================
# 🛍️ БОТ ДЛЯ ПОКУПАТЕЛЕЙ
# =======================================================
@client_dp.message_handler(commands=['start'])
async def client_welcome(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🛍️ Перейти до веб-магазину", web_app=types.WebAppInfo(url="https://victor-1989-eng.github.io/-/")))
    
    await message.answer(
        "<b>Ласкаво просимо до маркетплейсу pro_teleg.ua! 📦</b>\n\n"
        "<b>Коротка інструкція для покупця:</b>\n"
        "1. Натисніть кнопку <b>'Перейти до веб-магазину'</b> нижче.\n"
        "2. Оберіть потрібний магазин зі списку та додайте товари до кошика.\n"
        "3. Вкажіть ваші дані для доставки Новою Поштою та надішліть замовлення.\n"
        "4. Очікуйте на підтвердження від продавця прямо у цьому чаті! ✅",
        reply_markup=kb, parse_mode="HTML"
    )


# =======================================================
# 💼 БОТ ДЛЯ ПРОДАВЦОВ — КАБИНЕТ УПРАВЛЕНИЯ
# =======================================================
def get_seller_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("🏪 Мої Магазини"), KeyboardButton("➕ Додати новий товар"),
        KeyboardButton("🗑️ Видалити товар"), KeyboardButton("❌ Видалити магазин")
    )
    return kb

@seller_dp.message_handler(commands=['start'], state='*')
async def seller_welcome(message: types.Message, state: FSMContext):
    await state.finish()
    await message.answer(
        "👋 <b>Вітаємо в кабінеті керування для Продавців платформи pro_teleg.ua!</b>\n\n"
        "Оберіть дію на клавіатурі нижче. Кожна кнопка відкриє окреме изолироване меню.",
        reply_markup=get_seller_menu(), parse_mode="HTML"
    )

@seller_dp.callback_query_handler(lambda call: call.data == "cancel_product_creation", state='*')
async def cancel_product_creation_handler(call: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await clear_fsm_chat_history(call.message.chat.id, state)
        await state.finish()
        await call.message.answer("❌ **Додавання товару скасовано.** Процес перервано, історія очищена.", reply_markup=get_seller_menu())
    await call.answer()

@seller_dp.message_handler(text="🏪 Мої Магазини", state='*')
async def seller_shops_list(message: types.Message, state: FSMContext):
    await state.finish()
    shops = await get_owner_shops_db(message.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    
    if not shops:
        kb.add(InlineKeyboardButton(text="➕ Створити перший магазин", callback_data="make_shop_wizard"))
        await message.answer("ℹ️ У вас ще немає створених магазинів на платформі.", reply_markup=kb)
        return
    
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']} (Перегляд)", callback_data=f"view_shop_{s_id}"))
    
    kb.add(InlineKeyboardButton(text="➕ Додати ще один магазин", callback_data="make_shop_wizard"))
    await message.answer("🏪 **Ваш список магазинів:**\nОберіть бренд для перегляду деталей або створіть новий:", reply_markup=kb, parse_mode="Markdown")

@seller_dp.callback_query_handler(lambda call: call.data.startswith('view_shop_'))
async def view_shop_details(call: types.CallbackQuery):
    s_id = call.data.split('_')[2]
    shop = await get_shop_db(s_id)
    if not shop:
        await call.answer("Магазин не знайдено.")
        return
    
    prods = await get_shop_products_db(s_id)
    status_emoji = "✅ Активний" if shop.status == "active" else "❌ Заморожений"
    text = (
        f"🏪 **Управління магазином: {shop.name}**\n\n"
        f"• ID бренду: `{s_id}`\n"
        f"• Статус в sistemі: {status_emoji}\n"
        f"• Кількість товарів: {len(prods)} шт.\n"
        f"• Накопичений борг платформи (5%): *{shop.debt} грн*"
    )
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="back_to_shops_list"))
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@seller_dp.callback_query_handler(lambda call: call.data == "back_to_shops_list")
async def back_to_shops_list_handler(call: types.CallbackQuery):
    shops = await get_owner_shops_db(call.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']} (Перегляд)", callback_data=f"view_shop_{s_id}"))
    
    kb.add(InlineKeyboardButton(text="➕ Додати ще один магазин", callback_data="make_shop_wizard"))
    await call.message.edit_text("🏪 **Ваш список магазинів:**\nОберіть бренд для перегляду деталей або створіть новий:", reply_markup=kb, parse_mode="Markdown")

# МАСТЕР СОЗДАНИЯ МАГАЗИНА
@seller_dp.callback_query_handler(lambda call: call.data == "make_shop_wizard")
async def start_shop_wizard(call: types.CallbackQuery):
    await call.message.answer("📝 **КРОК 1:** Введіть унікальний ID магазину **англійськими літерами** (наприклад: `perfume`, `beauty`):")
    await CreateShopState.shop_id.set()

@seller_dp.message_handler(state=CreateShopState.shop_id)
async def process_shop_id(message: types.Message, state: FSMContext):
    s_id = message.text.strip().lower()
    shop = await get_shop_db(s_id)
    if shop is not None:
        await message.answer("❌ Цей ID вже зайнятий! Введіть інший англійский ID:")
        return
    await state.update_data(shop_id=s_id)
    await message.answer("📝 **КРОК 2:** Введіть публічну назву вашого магазину (яку побачать покупці):")
    await CreateShopState.name.set()

@seller_dp.message_handler(state=CreateShopState.name)
async def process_shop_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📝 **КРОК 3:** Надішліть **один емодзі**, який стане іконкою бренду в головному меню:")
    await CreateShopState.emoji.set()

@seller_dp.message_handler(state=CreateShopState.emoji)
async def process_shop_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    # Теперь запись в PostgreSQL гарантированно дождется выполнения
    await add_shop_db(user_data['shop_id'], user_data['name'], emoji, message.from_user.id)
    
    await message.answer("🎉 **Магазин успішно створено!** Перейдіть до меню додавання товарів.", reply_markup=get_seller_menu())
    await state.finish()

# ДОБАВЛЕНИЕ НОВОГО ТОВАРA
@seller_dp.message_handler(text="➕ Додати новий товар", state='*')
async def add_product_start(message: types.Message, state: FSMContext):
    await state.finish()
    shops = await get_owner_shops_db(message.from_user.id)
    if not shops:
        await message.answer("❌ У вас немає магазинів! Спочатку створіть хоча б один через автоматичний майстер.")
        return
    
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"addto_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))
    
    m1 = await message.answer("📋 **Додавання товару.** Оберіть магазин, куди додати нову позицію:", reply_markup=kb)
    await AddProductState.target_shop.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m1.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('addto_'), state=AddProductState.target_shop)
async def add_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.split('_')[1]
    await state.update_data(target_shop=shop_id)
    m2 = await call.message.answer("✍️ **Шаг 1: Назва товару.**\nВведіть комерційну назву продукту (наприклад: *Духи Chanel No.5*):", reply_markup=get_cancel_kb())
    await AddProductState.name.set()
    await save_msg_id(state, m2.message_id)

@seller_dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    m3 = await message.answer("✍️ **Шаг 2: Категорія товару.**\nВведіть назву категорії для фільтрації у веб-додатку (наприклад: *Чоловічі парфуми*, *Унісекс*, *Аксесуари*):", reply_markup=get_cancel_kb())
    await AddProductState.category.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m3.message_id)

@seller_dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(text="Так (Об'єми/Мл)", callback_data="var_yes"),
        InlineKeyboardButton(text="Ні (Фіксована ціна)", callback_data="var_no")
    )
    kb.add(InlineKeyboardButton(text="❌ Скасувати додавання", callback_data="cancel_product_creation"))
    m4 = await message.answer("✍️ **Шаг 3: Варіативність продукту.**\nЧи потрібно додати вибір мілілітрів/об'ємів для этого товару?", reply_markup=kb)
    await AddProductState.has_variants.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m4.message_id)

@seller_dp.callback_query_handler(lambda call: call.data in ["var_yes", "var_no"], state=AddProductState.has_variants)
async def add_product_variant_decision(call: types.CallbackQuery, state: FSMContext):
    if call.data == "var_yes":
        await state.update_data(has_variants=True)
        m5 = await call.message.answer("✍️ **Шаг 3.1: Введення об'ємів.**\nВведіть доступні мілілітри **через кому без пробілів**\n(Наприклад: `10,35,60` или `30,50,100`):", reply_markup=get_cancel_kb())
        await AddProductState.variants_list.set()
        await save_msg_id(state, m5.message_id)
    else:
        await state.update_data(has_variants=False)
        m5 = await call.message.answer("✍️ **Шаг 3.1: Ціна товару.**\nВведіть фіксовану вартість товару в гривнях (тільки цифри, наприклад `650`):", reply_markup=get_cancel_kb())
        await AddProductState.single_price.set()
        await save_msg_id(state, m5.message_id)

@seller_dp.message_handler(state=AddProductState.single_price)
async def add_product_single_price(message: types.Message, state: FSMContext):
    await state.update_data(single_price=message.text.strip())
    m6 = await message.answer("✍️ **Шаг 4: Опис товару.**\nНапишіть детальний опис (ноти, характеристики, комплектація):", reply_markup=get_cancel_kb())
    await AddProductState.description.set()
    await save_msg_id(state, message.message_id)
    await save_msg_id(state, m6.message_id)

@seller_dp.message_handler(state=AddProductState.variants_list)
async def add_product_variants_list(message: types.Message, state: FSMContext):
    v_raw = message.text.strip().split(',')
    v_list = [v.strip() for v in v_raw if v.strip()]
    await save_msg_id(state, message.message_id)
    
    if not v_list:
        m_err = await message.answer("Помилка формату. Введіть числа через кому, наприклад: 10,35,60", reply_markup=get_cancel_kb())
        await save_msg_id(state, m_err.message_id)
        return
        
    await state.update_data(v_list=v_list, v_index=0, compiled_variants=[])
    m6 = await message.answer(f"✍️ **Збір цін для об'ємів.**\nВведіть ціну в грн для об'єму **{v_list[0]} мл**:", reply_markup=get_cancel_kb())
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
        m_next = await message.answer(f"Введіть ціну в грн для об'єму **{v_list[next_idx]} мл**:", reply_markup=get_cancel_kb())
        await save_msg_id(state, m_next.message_id)
    else:
        await state.update_data(compiled_variants=compiled_variants)
        m_desc = await message.answer("✍️ **Шаг 4: Опис товару.**\nНапишіть детальний опис продукту:", reply_markup=get_cancel_kb())
        await AddProductState.description.set()
        await save_msg_id(state, m_desc.message_id)

@seller_dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    m7 = await message.answer("✍️ **Шаг 5: Фотографія.**\nЗавантажте та надішліть ОДНЕ зображення для картки товару:", reply_markup=get_cancel_kb())
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
    has_vars = user_data["has_variants"]
    
    prods = await get_shop_products_db(s_id)
    prod_id = f"id_{len(prods) + 1}_{datetime.now().strftime('%s')}"
    
    if has_vars:
        v_list = user_data["compiled_variants"]
        base_price = user_data["compiled_variants"][0]["price"]
    else:
        v_list = None
        base_price = user_data["single_price"]
        
    await add_product_db(
        shop_id=s_id, prod_id=prod_id, name=user_data["name"],
        category=user_data["category"], description=user_data["description"],
        image_url=img_url, has_variants=has_vars, price=base_price, variants=v_list
    )
    
    await clear_fsm_chat_history(message.chat.id, state)
    
    await message.answer("✅ **Товар успішно завантажено та додано до вітрини бренду!**\nВесь процес оформлення очищено з історії чату.", reply_markup=get_seller_menu())
    await state.finish()

# КНОПКА 3: Удаление товаров
@seller_dp.message_handler(text="🗑️ Видалити товар", state='*')
async def delete_product_start(message: types.Message, state: FSMContext):
    await state.finish()
    shops = await get_owner_shops_db(message.from_user.id)
    if not shops:
        await message.answer("У вас немає магазинів.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"listdel_{s_id}"))
    await message.answer("📋 **Видалення товарів.** Оберіть магазин для перегляду його каталогу:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('listdel_'))
async def delete_product_list(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    prods = await get_shop_products_db(s_id)
    if not prods:
        await call.message.answer("В цьому магазині ще немає завантажених товарів.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for p in prods:
        kb.add(InlineKeyboardButton(text=f"🗑️ Видалити {p['name']}", callback_data=f"confprod_{s_id}_{p['id']}"))
    await call.message.edit_text("Оберіть конкретний товар для безповоротного видаления:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confprod_'))
async def delete_product_execute(call: types.CallbackQuery):
    _, s_id, p_id = call.data.split('_')
    await delete_product_db(s_id, p_id)
    await call.message.edit_text("✅ Товар повністю видалено з вітрини WebApp.")

# КНОПКА 4: Удаление магазина
@seller_dp.message_handler(text="❌ Видалити магазин", state='*')
async def delete_shop_start(message: types.Message, state: FSMContext):
    await state.finish()
    shops = await get_owner_shops_db(message.from_user.id)
    if not shops:
        await message.answer("У вас немає створених магазинів.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"❌ Видалити {s['emoji']} {s['name']}", callback_data=f"delshop_{s_id}"))
    await message.answer("⚠️ **УВАГА!** Видалення магазину повністю видалить бренд та всі пов'язані товари. Оберіть магазин для видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('delshop_'))
async def delete_shop_execute(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    shop = await get_shop_db(s_id)
    if shop and str(shop.owner_id) == str(call.from_user.id):
        await delete_shop_db(s_id)
        await call.message.edit_text("💥 Магазин, каталог та налаштування повністю стерті з системи.")
    await call.answer()

# ОБРАБОТКА РЕШЕНИЙ ПО ЗАКАЗАМ КОРЗИНЫ
@seller_dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action = action_data[0]
    buyer_id = action_data[1]
    
    if action == "ord_approve":
        total_price = float(action_data[2])
        shop_id = action_data[3]
        commission = round(total_price * 0.05, 2)
        
        async with AsyncSessionLocal() as session:
            shop = await session.get(Shop, shop_id)
            if shop:
                shop.debt = round(shop.debt + commission, 2)
                new_order = OrderRecord(
                    date=datetime.now().strftime("%d.%m.%Y %H:%M"),
                    shop_id=shop_id, total=total_price, commission=commission
                )
                session.add(new_order)
                await session.commit()
                
                await client_bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{shop.name}\" підтверджено продавцем!**\n⏳ Очікуйте на ТТН доставки.")
                await client_bot.send_message(ADMIN_ID, f"💰 **Зароблено {commission} грн**\n(5% комісії від замовлення в магазині `{shop_id}` на суму {total_price} грн записано в борг).")
                await call.message.edit_text(call.message.text + f"\n\n✅ **Ви підтвердили замовлення. Комісія платформи {commission} грн (5%) додана до рахунку.**")
                
    elif action == "ord_decline":
        await client_bot.send_message(buyer_id, "❌ На жаль, ваше замовлення було відхилено продавцем.")
        await call.message.edit_text(call.message.text + "\n\n❌ **Ви скасували це замовлення.**")
    await call.answer()


# --- АВТО-БИЛЛИНГ ---
def run_monday_billing_job():
    async def process():
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Shop))
            shops = res.scalars().all()
            for s in shops:
                if s.debt > 0:
                    invoice_text = f"📊 **Час щотижневого біллінгу!**\nВаш поточний борг по комісії (5%) складає: *{s.debt} грн*.\n\nРеквізити для оплати: `4441 1111 2222 3333`.\n\nПісля оплати напишіть головному адміну для обнулення рахунку."
                    requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s.owner_id, "text": invoice_text, "parse_mode": "Markdown"})
    asyncio.run(process())

def run_tuesday_penalty_job():
    async def process():
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Shop))
            shops = res.scalars().all()
            for s in shops:
                if s.debt > 0 and s.status == "active":
                    s.status = "frozen"
                    requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s.owner_id, "text": "❌ **Ваш магазин ЗАМОРОЖЕНО за несплату щотижневої комісії!** Покупці тимчасово не бачать ваші товари в додатку."})
            await session.commit()
    asyncio.run(process())

scheduler = BackgroundScheduler(timezone="Europe/Kiev")
scheduler.add_job(run_monday_billing_job, CronTrigger(day_of_week='mon', hour=9, minute=0))
scheduler.add_job(run_tuesday_penalty_job, CronTrigger(day_of_week='tue', hour=9, minute=0))
scheduler.start()

Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000))), daemon=True).start()

# --- ИСПРАВЛЕННЫЙ ЗАПУСК ДВУХ БОТОВ БЕЗ КОНФЛИКТА ОБНОВЛЕНИЙ ---
async def start_all_bots():
    # Инициализация таблиц базы данных PostgreSQL перед стартом
    await init_db_tables()
    
    try:
        await client_bot.delete_webhook(drop_pending_updates=True)
        await seller_bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"Предупреждение при очистке вебхуков: {e}")
        
    print("🚀 Системы клиент-сервер успешно активны на базе PostgreSQL!")
    
    # Запуск поллинга для обоих диспетчеров параллельно в рамках одной задачи
    await asyncio.gather(
        client_dp.start_polling(),
        seller_dp.start_polling()
    )

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_all_bots())

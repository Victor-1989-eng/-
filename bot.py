import os
import json
import logging
from urllib.parse import parse_qs
import requests
from flask import Flask, request, jsonify
from threading import Thread

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

import psycopg2
from psycopg2.extras import RealDictCursor

# --- НАЛАШТУВАННЯ ЛОГУВАННЯ ---
logging.basicConfig(level=logging.INFO)

# --- ТОКЕНИ ТА НАЛАШТУВАННЯ (Змінні оточення) ---
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))  # Вкажіть ваш Telegram ID адміна
SELLER_TOKEN = os.environ.get("SELLER_TOKEN")
CLIENT_TOKEN = os.environ.get("CLIENT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Налаштування юзернейму та веб-додатка для клієнтського бота
CLIENT_BOT_USERNAME = os.environ.get("CLIENT_BOT_USERNAME", "pro_teleg_bot")
WEB_APP_SHORT_NAME = os.environ.get("WEB_APP_SHORT_NAME", "shop")
GITHUB_FRONTEND_URL = os.environ.get("GITHUB_FRONTEND_URL", "https://victor-1989-eng.github.io/-/")

# Ініціалізація ботів
seller_bot = Bot(token=SELLER_TOKEN)
client_bot = Bot(token=CLIENT_TOKEN)
admin_bot = Bot(token=SELLER_TOKEN)  # Адмін отримує сповіщення через бота продавця

storage = MemoryStorage()
seller_dp = Dispatcher(seller_bot, storage=storage)
client_dp = Dispatcher(client_bot, storage=storage)

app = Flask(__name__)

# --- СТАН ФОРМИ СТВОРЕННЯ МАГАЗИНУ (Тепер всього 2 кроки) ---
class CreateShopState(StatesGroup):
    name = State()
    emoji = State()

# --- СТАНИ ДЛЯ ДОДАВАННЯ/ВИДАЛЕННЯ ТОВАРІВ ---
class AddProductState(StatesGroup):
    shop_id = State()
    name = State()
    price = State()
    variants = State()
    image_url = State()

class DeleteProductState(StatesGroup):
    shop_id = State()
    product_id = State()

class DeleteShopState(StatesGroup):
    shop_id = State()

# --- ПІДКЛЮЧЕННЯ ДО БД ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

# --- ДОПОМІЖНІ ФУНКЦІЇ ДЛЯ ОЧИЩЕННЯ ЧАТУ ---
async def save_msg_id(state: FSMContext, msg_id: int):
    data = await state.get_data()
    msg_ids = data.get("msg_ids", [])
    if msg_id not in msg_ids:
        msg_ids.append(msg_id)
        await state.update_data(msg_ids=msg_ids)

async def clear_chat_history(bot: Bot, chat_id: int, state: FSMContext):
    data = await state.get_data()
    msg_ids = data.get("msg_ids", [])
    for m_id in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except Exception:
            pass
    await state.update_data(msg_ids=[])

def is_banned(user_id: int) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM ban_list WHERE user_id = %s;", (str(user_id),))
    banned = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return banned

def get_owner_shops(owner_id: int) -> dict:
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT shop_id, name, emoji, status, debt FROM shops WHERE owner_id = %s;", (str(owner_id),))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {r['shop_id']: r for r in rows}

# --- КЛАВІАТУРИ ---
def get_seller_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("🏪 Мої Магазини"),
        KeyboardButton("➕ Додати товар"),
        KeyboardButton("🗑️ Видалити товар"),
        KeyboardButton("❌ Видалити магазин")
    )
    return kb

def get_cancel_kb():
    return InlineKeyboardMarkup().add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_wizard"))

# --- ОБРОБКА КОМАНДИ /START ТА СТАРТОВОГО МЕНЮ ---
@seller_dp.message_handler(commands=['start'], state='*')
async def seller_welcome(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()
    
    try: await seller_bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception: pass

    welcome_text = (
        f"👋 **Вітаємо на платформі мобільних маркетплейсів pro_teleg!**\n\n"
        f"Тут ви можете створити власний повноцінний Web App інтернет-магазин прямо всередині Telegram всього за декілька хвилин та продавати товари через TikTok / Instagram.\n\n"
        f"⚙️ **Як розпочати роботу:**\n"
        f"1️⃣ Натисніть кнопку **«🏪 Мої Магазини»** нижче.\n"
        f"2️⃣ Створіть свій бренд (ми автоматично згенеруємо для вас постійне посилання на основі вашого ID).\n"
        f"3️⃣ Додайте перші товари та надішліть магазин на швидку модерацію.\n\n"
        f"💵 **Умови платформи:**\n"
        f"• Створення та налаштування магазину — **безкоштовно**.\n"
        f"• Комісія сервісу складає всього **5%** лише з успішно виконаних замовлень (нараховується на баланс вашого магазину).\n\n"
        f"📌 *Скористайтеся меню нижче, щоб розпочати налаштування вашого бізнесу!*"
    )
    
    m = await message.answer(welcome_text, reply_markup=get_seller_menu(), parse_mode="Markdown")
    await save_msg_id(state, m.message_id)

# --- СКАСУВАННЯ ДІЙ ---
@seller_dp.callback_query_handler(lambda call: call.data == "cancel_wizard", state='*')
async def cancel_wizard_handler(call: types.CallbackQuery, state: FSMContext):
    await clear_chat_history(seller_bot, call.message.chat.id, state)
    await state.finish()
    await call.answer("Дію скасовано")
    m = await call.message.answer("📥 Головне меню продавця:", reply_markup=get_seller_menu())
    await save_msg_id(state, m.message_id)

# --- ШВИДКИЙ МАЙСТЕР СТВОРЕННЯ МАГАЗИНУ (id12345678) ---
@seller_dp.callback_query_handler(lambda call: call.data == "make_shop_wizard", state='*')
async def start_shop_wizard(call: types.CallbackQuery, state: FSMContext):
    await clear_chat_history(seller_bot, call.message.chat.id, state)
    
    # Автоматично генеруємо залізобетонний ID на основі Telegram ID продавця
    auto_shop_id = f"id{call.from_user.id}"
    await state.update_data(shop_id=auto_shop_id)

    m1 = await call.message.answer(
        "📝 **КРОК 1 із 2: Публічна назва бренду**\n\n"
        "Введіть красиву назву вашого магазину, яку бачитимуть покупці на вітрині (наприклад: `Elite Perfume UA`):",
        reply_markup=get_cancel_kb()
    )
    await CreateShopState.name.set()
    await save_msg_id(state, call.message.message_id)
    await save_msg_id(state, m1.message_id)

@seller_dp.message_handler(state=CreateShopState.name)
async def process_shop_name(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(name=message.text.strip())
    
    m2 = await message.answer(
        "📝 **КРОК 2 із 2: Іконка-Емодзі**\n\n"
        "Надішліть **рівно один емодзі**, який найкраще характеризує ваш асортимент (наприклад: 🧴, 🛍️, 💄).\n"
        "Цей емодзі стане логотипом вашої вкладки в додатку.",
        reply_markup=get_cancel_kb()
    )
    await CreateShopState.emoji.set()
    await save_msg_id(state, m2.message_id)

@seller_dp.message_handler(state=CreateShopState.emoji)
async def process_shop_emoji(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    emoji_char = message.text.strip()
    
    # Валідація на довжину (один символ емодзі може мати довжину до 4 байт в UTF)
    if len(emoji_char) > 4:
        m_err = await message.answer("⚠️ Будь ласка, надішліть тільки один емодзі!")
        await save_msg_id(state, m_err.message_id)
        return

    data = await state.get_data()
    shop_id = data['shop_id']
    name = data['name']
    owner_id = str(message.from_user.id)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Записуємо магазин у базу як 'pending' (чекає на схвалення адміном)
        cursor.execute("""
            INSERT INTO shops (shop_id, owner_id, name, emoji, status, debt)
            VALUES (%s, %s, %s, %s, 'pending', 0.0)
            ON CONFLICT (shop_id) DO UPDATE SET name = EXCLUDED.name, emoji = EXCLUDED.emoji;
        """, (shop_id, owner_id, name, emoji_char))
        conn.commit()
        
        # Сповіщення адміна про новий магазин на модерацію
        admin_kb = InlineKeyboardMarkup().add(
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"adm_appr:{shop_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"adm_decl:{shop_id}")
        )
        await admin_bot.send_message(
            ADMIN_ID, 
            f"🏪 **Новий магазин на модерації!**\n\n"
            f"👤 Власник ID: `{owner_id}`\n"
            f"🆔 ID магазину: `{shop_id}`\n"
            f"🏷️ Назва: {emoji_char} {name}", 
            reply_markup=admin_kb
        )
    except Exception as e:
        conn.rollback()
        logging.error(f"Error saving shop: {e}")
    finally:
        cursor.close()
        conn.close()

    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()

    success_text = (
        f"🎉 **Ваш магазин \"{emoji_char} {name}\" успішно створено!**\n\n"
        f"⏳ Наразі він відправлений на швидку модерацію адміністратором платформи.\n"
        f"Як тільки магазин буде активовано, ви отримаєте сповіщення і зможете додавати товари!"
    )
    m_res = await message.answer(success_text, reply_markup=get_seller_menu(), parse_mode="Markdown")
    await save_msg_id(state, m_res.message_id)

# --- РОБОТА З ГОЛОВНИМ МЕНЮ (Очищення історії) ---
@seller_dp.message_handler(text="🏪 Мої Магазини", state='*')
async def seller_shops_list(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()
    
    try: await seller_bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception: pass

    shops = get_owner_shops(message.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    
    if not shops:
        kb.add(InlineKeyboardButton(text="➕ Створити перший магазин", callback_data="make_shop_wizard"))
        m = await message.answer("ℹ️ У вас ще немає створених магазинів на платформі.", reply_markup=kb)
        await save_msg_id(state, m.message_id)
        return
    
    for s_id, s in shops.items():
        status_text = "🔎 На модерації" if s['status'] == 'pending' else ( "✅ Активний" if s['status'] == 'active' else "❌ Заморожений" )
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']} ({status_text})", callback_data=f"view_shop_{s_id}"))
    
    kb.add(InlineKeyboardButton(text="➕ Додати ще один магазин", callback_data="make_shop_wizard"))
    m = await message.answer("🏪 **Ваш список магазинів:**", reply_markup=kb, parse_mode="Markdown")
    await save_msg_id(state, m.message_id)

# Деталі магазину та посилання на Web App
@seller_dp.callback_query_handler(lambda call: call.data.startswith('view_shop_'))
async def view_shop_details(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.replace('view_shop_', '')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT name, emoji, status, debt FROM shops WHERE shop_id = %s;", (shop_id,))
    shop = cursor.fetchone()
    cursor.close()
    conn.close()

    if not shop:
        await call.answer("Магазин не знайдено.")
        return

    # Золоте посилання продавця
    link = f"https://t.me/{CLIENT_BOT_USERNAME}/{WEB_APP_SHORT_NAME}?startapp={shop_id}"
    
    text = (
        f"{shop['emoji']} **Магазин:** {shop['name']}\n"
        f"🆔 ID бренду: `{shop_id}`\n"
        f"📊 Статус: `{shop['status']}`\n"
        f"💸 Накопичений борг комісії: {shop['debt']} грн\n\n"
        f"🔗 **Ваше постійне посилання на вітрину:**\n`{link}`\n\n"
        f"📎 *Покупці зможуть відкривати ваш магазин прямо через це посилання в Telegram!*"
    )
    
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton(text="🔙 Назад до списку", callback_data="back_to_shops")
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@seller_dp.callback_query_handler(lambda call: call.data == "back_to_shops", state='*')
async def back_to_shops_handler(call: types.CallbackQuery, state: FSMContext):
    await seller_shops_list(call.message, state)

# --- ДОДАВАННЯ ТОВАРУ ---
@seller_dp.message_handler(text="➕ Додати товар", state='*')
async def add_product_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()
    
    shops = get_owner_shops(message.from_user.id)
    active_shops = {s_id: s for s_id, s in shops.items() if s['status'] == 'active'}
    
    if not active_shops:
        m = await message.answer("⚠️ У вас немає активних магазинів для додавання товарів. Створіть магазин або зачекайте модерації.")
        await save_msg_id(state, m.message_id)
        return
        
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in active_shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"add_to_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_wizard"))
    
    m = await message.answer("🎯 Оберіть магазин, до якого хочете додати товар:", reply_markup=kb)
    await save_msg_id(state, m.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('add_to_'), state='*')
async def add_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.replace('add_to_', '')
    await state.update_data(shop_id=shop_id)
    await AddProductState.name.set()
    
    m = await call.message.edit_text("📝 Введіть назву товару (наприклад: `Chanel Chance`):", reply_markup=get_cancel_kb())
    await save_msg_id(state, m.message_id)

@seller_dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    await state.update_data(name=message.text.strip())
    await AddProductState.price.set()
    m = await message.answer("💰 Введіть базову ціну товару в грн (тільки цифри, наприклад: `1450`):", reply_markup=get_cancel_kb())
    await save_msg_id(state, m.message_id)

@seller_dp.message_handler(state=AddProductState.price)
async def add_product_price(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    try:
        price = float(message.text.strip())
    except ValueError:
        m_err = await message.answer("⚠️ Введіть коректне число!")
        await save_msg_id(state, m_err.message_id)
        return
        
    await state.update_data(price=price)
    await AddProductState.variants.set()
    m = await message.answer(
        "🧪 Введіть варіанти об'єму (мл) та націнку у форматі `об'єм:націнка` через кому.\n"
        "Приклад: `30:0, 50:400, 100:900` (де 30мл без націнки, а 50мл на 400 грн дорожче базової ціни).\n"
        "Якщо товар без варіантів, напишіть: `0:0`", 
        reply_markup=get_cancel_kb()
    )
    await save_msg_id(state, m.message_id)

@seller_dp.message_handler(state=AddProductState.variants)
async def add_product_variants(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    variants_raw = message.text.strip()
    
    # Спроба парсингу для валідації
    try:
        variants_list = []
        for pair in variants_raw.split(','):
            v, p = pair.split(':')
            variants_list.append({"variant": v.strip(), "price": float(p.strip())})
    except Exception:
        m_err = await message.answer("⚠️ Неправильний формат! Будь ласка, введіть за шаблоном `об'єм:націнка` (наприклад `30:0, 50:300`):")
        await save_msg_id(state, m_err.message_id)
        return

    await state.update_data(variants=json.dumps(variants_list))
    await AddProductState.image_url.set()
    m = await message.answer("📸 Надішліть посилання на картинку товару (пряме URL на зображення, наприклад з imgur або telegra.ph):", reply_markup=get_cancel_kb())
    await save_msg_id(state, m.message_id)

@seller_dp.message_handler(state=AddProductState.image_url)
async def add_product_image(message: types.Message, state: FSMContext):
    await save_msg_id(state, message.message_id)
    image_url = message.text.strip()
    
    if not (image_url.startswith('http://') or image_url.startswith('https://')):
        m_err = await message.answer("⚠️ Це не схоже на посилання! Надішліть коректне URL посилання:")
        await save_msg_id(state, m_err.message_id)
        return

    data = await state.get_data()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO products (shop_id, name, price, variants, image_url)
            VALUES (%s, %s, %s, %s, %s);
        """, (data['shop_id'], data['name'], data['price'], data['variants'], image_url))
        conn.commit()
        await clear_chat_history(seller_bot, message.chat.id, state)
        await state.finish()
        m_res = await message.answer(f"🎉 Товар **\"{data['name']}\"** успішно додано на вашу вітрину!", reply_markup=get_seller_menu(), parse_mode="Markdown")
        await save_msg_id(state, m_res.message_id)
    except Exception as e:
        conn.rollback()
        logging.error(f"Error adding product: {e}")
        await message.answer("❌ Сталася помилка під час збереження товару.")
    finally:
        cursor.close()
        conn.close()

# --- ВИДАЛЕННЯ ТОВАРУ ---
@seller_dp.message_handler(text="🗑️ Видалити товар", state='*')
async def delete_product_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()
    
    shops = get_owner_shops(message.from_user.id)
    active_shops = {s_id: s for s_id, s in shops.items() if s['status'] == 'active'}
    
    if not active_shops:
        m = await message.answer("⚠️ У вас немає активних магазинів.")
        await save_msg_id(state, m.message_id)
        return
        
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in active_shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"del_from_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_wizard"))
    
    m = await message.answer("🎯 Оберіть магазин, з якого хочете видалити товар:", reply_markup=kb)
    await save_msg_id(state, m.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('del_from_'), state='*')
async def delete_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.replace('del_from_', '')
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, name, price FROM products WHERE shop_id = %s;", (shop_id,))
    products = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not products:
        await call.message.edit_text("ℹ️ У цьому магазині ще немає товарів.", reply_markup=get_cancel_kb())
        return
        
    kb = InlineKeyboardMarkup(row_width=1)
    for p in products:
        kb.add(InlineKeyboardButton(text=f"{p['name']} ({p['price']} грн)", callback_data=f"confirm_del_p_{p['id']}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_wizard"))
    
    await DeleteProductState.product_id.set()
    await state.update_data(shop_id=shop_id)
    await call.message.edit_text("🗑️ Оберіть товар для безповоротного видалення:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confirm_del_p_'), state=DeleteProductState.product_id)
async def delete_product_confirm(call: types.CallbackQuery, state: FSMContext):
    p_id = int(call.data.replace('confirm_del_p_', ''))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM products WHERE id = %s;", (p_id,))
        conn.commit()
        await clear_chat_history(seller_bot, call.message.chat.id, state)
        await state.finish()
        m = await call.message.answer("✅ Товар успішно видалено з вашої вітрини!", reply_markup=get_seller_menu())
        await save_msg_id(state, m.message_id)
    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting product: {e}")
        await call.answer("❌ Помилка видалення.")
    finally:
        cursor.close()
        conn.close()

# --- ВИДАЛЕННЯ МАГАЗИНУ ---
@seller_dp.message_handler(text="❌ Видалити магазин", state='*')
async def delete_shop_start(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id): return
    await clear_chat_history(seller_bot, message.chat.id, state)
    await state.finish()
    
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        m = await message.answer("⚠️ У вас немає створених магазинів.")
        await save_msg_id(state, m.message_id)
        return
        
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"confirm_del_s_{s_id}"))
    kb.add(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_wizard"))
    
    await DeleteShopState.shop_id.set()
    m = await message.answer("🚨 **УВАГА! Видалення магазину призведе до видалення всіх його товарів!**\nОберіть бренд для видалення:", reply_markup=kb, parse_mode="Markdown")
    await save_msg_id(state, m.message_id)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confirm_del_s_'), state=DeleteShopState.shop_id)
async def delete_shop_confirm(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.replace('confirm_del_s_', '')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Спочатку видаляємо всі товари магазину, потім сам магазин
        cursor.execute("DELETE FROM products WHERE shop_id = %s;", (shop_id,))
        cursor.execute("DELETE FROM shops WHERE shop_id = %s;", (shop_id,))
        conn.commit()
        await clear_chat_history(seller_bot, call.message.chat.id, state)
        await state.finish()
        m = await call.message.answer("🗑️ Магазин та всі його товари успішно видалені з бази даних.", reply_markup=get_seller_menu())
        await save_msg_id(state, m.message_id)
    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting shop: {e}")
        await call.answer("❌ Помилка видалення.")
    finally:
        cursor.close()
        conn.close()

# --- ОБРОБНИКИ ДЛЯ АДМІНІСТРАТОРА (Модерація магазинів) ---
@seller_dp.callback_query_handler(lambda call: call.data.startswith('adm_'))
async def process_admin_moderation(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    
    action, shop_id = call.data.split(':')
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if action == "adm_appr":
        cursor.execute("UPDATE shops SET status = 'active' WHERE shop_id = %s RETURNING owner_id, name, emoji;", (shop_id,))
        shop = cursor.fetchone()
        conn.commit()
        
        await call.message.edit_text(call.message.text + "\n\n✅ **СХВАЛЕНО! Магазин активований.**")
        try:
            await seller_bot.send_message(
                shop['owner_id'], 
                f"🎉 **Вітаємо! Ваш магазин \"{shop['emoji']} {shop['name']}\" успішно пройшов модерацію та тепер активний!**\n"
                f"Ви можете наповнювати його товарами через меню та починати продажі."
            )
        except Exception: pass
        
    elif action == "adm_decl":
        cursor.execute("SELECT owner_id, name, emoji FROM shops WHERE shop_id = %s;", (shop_id,))
        shop = cursor.fetchone()
        cursor.execute("DELETE FROM shops WHERE shop_id = %s;", (shop_id,))
        conn.commit()
        
        await call.message.edit_text(call.message.text + "\n\n❌ **ВІДХИЛЕНО! Магазин видалений з бази.**")
        try:
            await seller_bot.send_message(
                shop['owner_id'], 
                f"❌ **На жаль, ваш магазин \"{shop['emoji']} {shop['name']}\" не пройшов модерацію.**\n"
                f"Будь ласка, переконайтеся у коректності даних та спробуйте ще раз."
            )
        except Exception: pass
        
    cursor.close()
    conn.close()

# --- ОБРОБНИК РУЧНОГО ВИДАЛЕННЯ КАРТКИ ЗАМОВЛЕННЯ ПРОДАВЦЕМ ---
@seller_dp.callback_query_handler(lambda call: call.data == "delete_order_msg", state='*')
async def delete_order_message_handler(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        await call.answer("❌ Не вдалося видалити повідомлення.")
    await call.answer()

# =======================================================
# 🛍️ БОТ ПОКУПАТЕЛЕЙ (CLIENT BOT)
# =======================================================
@client_dp.message_handler(commands=['start'])
async def client_welcome(message: types.Message):
    if is_banned(message.from_user.id): return
    
    # Створюємо кнопку для запуску WebApp магазину з використанням GITHUB_FRONTEND_URL
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🛍️ Перейти до веб-магазину", web_app=types.WebAppInfo(url=GITHUB_FRONTEND_URL)))
    
    welcome_client = (
        f"👋 **Ласкаво просимо до нашої мережі мобільних маркетплейсів!**\n\n"
        f"Натисніть кнопку нижче, щоб відкрити зручну вітрину та оформити замовлення в один клік!"
    )
    await message.answer(welcome_client, reply_markup=kb, parse_mode="Markdown")

# --- FLASK-СЕРВЕР ДЛЯ ОБРОБКИ ЗАМОВЛЕНЬ ТА API ---
@app.route('/')
def home():
    return "API Платформы Активна (Neon Modernized)!", 200

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

        # Безпечний парсинг initData Telegram WebApp
        try:
            parsed = parse_qs(init_data_raw)
            if 'user' not in parsed:
                return jsonify({"status": "error", "message": "Invalid initData"}), 400
            user_json = json.loads(parsed['user'][0])
            buyer_tg_id = user_json.get('id')
        except Exception:
            return jsonify({"status": "error", "message": "Failed to parse initData"}), 400

        # Перевірка на бан
        cursor.execute("SELECT 1 FROM ban_list WHERE user_id = %s;", (str(buyer_tg_id),))
        if cursor.fetchone():
            return jsonify({"status": "error", "message": "User banned"}), 403

        # Отримання даних магазину
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
            p_text += f"{idx}. 📦 *{item['name']}{variant_str}*\n    🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        # Нараховуємо 5% комісії від загальної суми
        commission = round(total_price * 0.05, 2)

        # Заносимо замовлення у БД із прихованими у JSON контактами
        cursor.execute("""
            INSERT INTO orders (shop_id, buyer_id, total_price, commission, status, delivery_json, cart_json)
            VALUES (%s, %s, %s, %s, 'new', %s, %s) RETURNING id;
        """, (shop_id, buyer_tg_id, total_price, commission, json.dumps(delivery), json.dumps(cart)))
        
        order_id = cursor.fetchone()['id']
        conn.commit()

        # Кнопки для продавця (передаємо необхідні аргументи для підтвердження або відхилення)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити та отримати контакти", callback_data=f"ord_approve:{buyer_tg_id}:{commission}:{shop_id}:{order_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}:{order_id}")
        )

        # Анонімне повідомлення: контакти приховані до підтвердження замовлення!
        owner_text = (
            f"📥 **НОВЕ ЗАМОВЛЕННЯ №{order_id}!**\n"
            f"🏪 Магазин: *{shop['name']}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"📍 **Місто доставки:** {delivery['city']}\n"
            f"💳 **Спосіб оплати:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n\n"
            f"🔒 *Особисті дані покупця (ПІБ, телефон та відділення пошти) будуть відкриті автоматично після підтвердження замовлення та списання комісії сервісу.*"
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

# --- ОБРОБНИК РІШЕННЯ ПРОДАВЦЯ (Зняття комісії та розкриття контактів) ---
@seller_dp.callback_query_handler(lambda call: call.data.startswith('ord_'), state='*')
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action, buyer_id = action_data[0], action_data[1]
    order_id = int(action_data[-1])
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    delete_kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton(text="🗑️ Видалити це замовлення з чату", callback_data="delete_order_msg")
    )
    
    if action == "ord_approve":
        commission = float(action_data[2])
        shop_id = action_data[3]
        
        # 1. Зараховуємо борг комісії на магазин та міняємо статус у БД
        cursor.execute("UPDATE shops SET debt = debt + %s WHERE shop_id = %s;", (commission, shop_id))
        cursor.execute("UPDATE orders SET status = 'approved' WHERE id = %s;", (order_id,))
        
        # 2. Дістаємо приховані раніше дані доставки клієнта
        cursor.execute("SELECT delivery_json FROM orders WHERE id = %s;", (order_id,))
        order_db = cursor.fetchone()
        
        cursor.execute("SELECT name FROM shops WHERE shop_id = %s;", (shop_id,))
        shop_name = cursor.fetchone()['name']
        conn.commit()
        
        # Повідомляємо клієнта
        try:
            await client_bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{shop_name}\" підтверджено!**")
        except Exception: pass
        
        # Миттєве сповіщення в адмінку про заробіток в USDT (за поточним курсом 41.5)
        commission_usdt = round(commission / 41.5, 2)
        await admin_bot.send_message(ADMIN_ID, f"💰 **Earned ${commission_usdt:.2f} USDT**\n(Комісія {commission} грн з замовлення бренду `{shop_id}`).")
        
        # Парсимо контакти покупця
        try:
            deliv = json.loads(order_db['delivery_json'])
            client_contacts = (
                f"\n\n🔑 **КОНТАКТИ ДЛЯ ВІДПРАВКИ (РОЗКРИТО):**\n"
                f"👤 **Отримувач:** {deliv.get('name')}\n"
                f"📞 **Телефон:** `{deliv.get('phone')}`\n"
                f"🚚 **Доставка:** {deliv.get('city')}, {deliv.get('warehouse')}\n"
            )
        except Exception:
            client_contacts = "\n\n⚠️ Помилка зчитування контактів клієнта."

        # Оновлюємо повідомлення продавця: прибираємо замок та відкриваємо ПІБ/Телефон клієнта
        await call.message.edit_text(
            call.message.text.replace(
                "🔒 *Особисті дані покупця (ПІБ, телефон та відділення пошти) будуть відкриті автоматично після підтвердження замовлення та списання комісії сервісу.*", 
                "🟢 **ЗАМОВЛЕННЯ УСПІШНО ПІДТВЕРДЖЕНО!**"
            ) + f"{client_contacts}\n💸 Комісія {commission} грн додана до вашого рахунку.",
            reply_markup=delete_kb,
            parse_mode="Markdown"
        )
        await call.answer("🟢 Замовлення підтверджено! Контакти відкрито.")
        
    elif action == "ord_decline":
        cursor.execute("UPDATE orders SET status = 'declined' WHERE id = %s;", (order_id,))
        conn.commit()
        try:
            await client_bot.send_message(buyer_id, "❌ Замовлення відхилено продавцем.")
        except Exception: pass
        
        await call.message.edit_text(
            call.message.text.replace(
                "🔒 *Особисті дані покупця (ПІБ, телефон та відділення пошти) будуть відкриті автоматично після підтвердження замовлення та списання комісії сервісу.*",
                "🔴 **ЗАМОВЛЕННЯ ВІДХИЛЕНО ПРОДАВЦЕМ!**"
            ),
            reply_markup=delete_kb,
            parse_mode="Markdown"
        )
        await call.answer("🔴 Замовлення скасовано.")
        
    cursor.close()
    conn.close()

# --- ЗАПУСК ПОТОКІВ ДЛЯ ОБОХ ДИСПЕТЧЕРІВ ---
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    # Flask запускається в окремому фоновому потоці
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Оскільки у нас два окремих робочих бота в одному файлі, запускаємо їх паралельно:
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(client_dp.start_polling())
    
    # Запуск основного диспетчера продавця (блокуючий виклик)
    executor.start_polling(seller_dp, skip_updates=True)

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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ И ТОКЕНЫ ---
CLIENT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")      # Бот для покупателей (@Gismete_bot)
SELLER_TOKEN = os.environ.get("SELLER_BOT_TOKEN")        # Бот для продавцов
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")            # Твой личный Telegram ID
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

DB_FILE = "database.json"

if not CLIENT_TOKEN or not SELLER_TOKEN or not ADMIN_ID:
    raise ValueError("ОШИБКА: Проверьте токены ботов и ID админа в настройках Render!")

# Инициализация ботов
client_bot = Bot(token=CLIENT_TOKEN)
client_dp = Dispatcher(client_bot, storage=MemoryStorage())

seller_bot = Bot(token=SELLER_TOKEN)
seller_dp = Dispatcher(seller_bot, storage=MemoryStorage())

app = Flask('')
CORS(app)

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def init_db():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump({"shops": {}, "orders": []}, f, ensure_ascii=False, indent=4)

def read_db():
    init_db()
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"shops": {}, "orders": []}

def write_db(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def get_owner_shops(user_id):
    db = read_db()
    return {s_id: s for s_id, s in db["shops"].items() if str(s["owner_id"]) == str(user_id)}

# --- СОСТОЯНИЯ FSM ДЛЯ БОТА ПРОДАВЦОВ ---
class CreateShopState(StatesGroup):
    shop_id = State()
    name = State()
    emoji = State()

class AddProductState(StatesGroup):
    target_shop = State()
    name = State()
    category = State()
    price = State()
    description = State()
    image = State()

# --- ENDPOINTS ДЛЯ ВЕБ-МАГАЗИНА (FLASK) ---
@app.route('/')
def home(): return "API Мультивендорной Платформы Активно!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    db = read_db()
    flat_shops = [{"shop_id": s_id, "name": s["name"], "emoji": s["emoji"], "status": s["status"]} for s_id, s in db["shops"].items()]
    return jsonify(flat_shops)

@app.route('/get-shop-products/<shop_id>', methods=['GET'])
def get_shop_products(shop_id):
    db = read_db()
    shop = db["shops"].get(shop_id)
    return jsonify(shop.get("products", []) if shop else [])

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

        db = read_db()
        shop = db["shops"].get(shop_id)

        if not shop or shop.get('status') == 'frozen':
            return jsonify({"status": "error"}), 403

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            item_total = float(item['price']) * int(item.get('qty', 1))
            total_price += item_total
            p_text += f"{idx}. 📦 *{item['name']}*\n   🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"ord_approve:{buyer_tg_id}:{total_price}:{shop_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}")
        )

        owner_text = (
            f"📥 **НОВЕ ЗАМОВЛЕННЯ В КОРЗИНУ!**\n"
            f"🏪 Магазин: *{shop['name']}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n"
        )
        
        # Заказ летит прямо в Бот для продавцов
        requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={
            "chat_id": shop['owner_id'], "text": owner_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
        })

        return jsonify({"status": "pending_approval"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# =======================================================
# 🏪 БОТ ДЛЯ ПОКУПАТЕЛЕЙ (@Gismete_bot) — ИНСТРУКЦИЯ И ВЕБ-МАГАЗИН
# =======================================================

@client_dp.message_handler(commands=['start'])
async def client_welcome(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🛍️ Перейти до веб-магазину", web_app=types.WebAppInfo(url="https://victor-1989-eng.github.io/-/")))
    
    # Строгая, понятная инструкция для покупателя
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
# 💼 БОТ ДЛЯ ПРОДАВЦОВ — КАНАЛ УПРАВЛЕНИЯ И КОРЗИНА ЗАКАЗОВ
# =======================================================

def get_seller_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("🏪 Мої Магазини"), KeyboardButton("➕ Додати новий товар"),
        KeyboardButton("🗑️ Видалити товар"), KeyboardButton("❌ Видалити магазин")
    )
    return kb

@seller_dp.message_handler(commands=['start'])
async def seller_welcome(message: types.Message):
    await message.answer(
        "👋 **Вітаємо в кабінеті керування для Продавців платформи pro_teleg.ua!**\n\n"
        "Тут ви можете повністю керувати своїм бізнесом:\n"
        "• Створювати автономні магазини\n"
        "• Наповнювати їх товарами або видаляти їх\n"
        "• Приймати та обробляти замовлення покупців\n\n"
        "📊 Комісія платформи за успішні замовлення становить **5%**.",
        reply_markup=get_seller_menu(), parse_mode="Markdown"
    )

@seller_dp.message_handler(lambda msg: msg.text == "🏪 Мої Магазини")
async def seller_shops_list(message: types.Message):
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="➕ Створити магазин", callback_data="make_shop_wizard"))
        await message.answer("ℹ️ У вас ще немає створених магазинів на платформі.", reply_markup=kb)
        return
    
    text = "📋 **Ваші діючі магазини:**\n\n"
    for s_id, s in shops.items():
        status_emoji = "✅ Активний" if s["status"] == "active" else "❌ Заморожений"
        text += f"{s['emoji']} *{s['name']}* (ID: `{s_id}`)\n" \
                f"   • Статус: {status_emoji}\n" \
                f"   • Накопичений борг (5%): {s['debt']} грн.\n\n"
    await message.answer(text, parse_mode="Markdown")

# Мастер создания магазина
@seller_dp.callback_query_handler(lambda call: call.data == "make_shop_wizard")
async def start_shop_wizard(call: types.CallbackQuery):
    await call.message.answer("📝 **КРОК 1:** Введіть унікальний ID магазину **англійськими літерами** (наприклад: `boots`, `tech`):")
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
    await message.answer("📝 **КРОК 3:** Надішліть **один емодзі** для іконки-логотипу:")
    await CreateShopState.emoji.set()

@seller_dp.message_handler(state=CreateShopState.emoji)
async def process_shop_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    db = read_db()
    db["shops"][user_data['shop_id']] = {
        "name": user_data['name'], "emoji": emoji, "owner_id": message.from_user.id,
        "debt": 0.0, "status": "active", "products": []
    }
    write_db(db)
    await message.answer("🎉 **Магазин успішно створено!** Тепер ви можете додавати товари.", reply_markup=get_seller_menu())
    await state.finish()

# Добавление товаров
@seller_dp.message_handler(lambda msg: msg.text == "➕ Додати новий товар")
async def add_product_start(message: types.Message):
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        await message.answer("❌ Спочатку створіть магазин у вкладці '🏪 Мої Магазини'")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"addto_{s_id}"))
    await message.answer("📋 Оберіть магазин для додавання товару:", reply_markup=kb)
    await AddProductState.target_shop.set()

@seller_dp.callback_query_handler(lambda call: call.data.startswith('addto_'), state=AddProductState.target_shop)
async def add_product_shop_selected(call: types.CallbackQuery, state: FSMContext):
    shop_id = call.data.split('_')[1]
    await state.update_data(target_shop=shop_id)
    await call.message.answer("📦 **Назва товару:**")
    await AddProductState.name.set()

@seller_dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📦 **Категорія товару:**")
    await AddProductState.category.set()

@seller_dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("📦 **Ціна (у гривнях, тільки число):**")
    await AddProductState.price.set()

@seller_dp.message_handler(state=AddProductState.price)
async def add_product_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text.strip())
    await message.answer("📦 **Опис товару:**")
    await AddProductState.description.set()

@seller_dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("📦 **Фотографія товару:** Завантажте фото:")
    await AddProductState.image.set()

@seller_dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_info = await seller_bot.get_file(photo.file_id)
    img_url = f"https://api.telegram.org/file/bot{SELLER_TOKEN}/{file_info.file_path}"
    
    user_data = await state.get_data()
    s_id = user_data["target_shop"]
    
    db = read_db()
    new_prod = {
        "id": f"id_{len(db['shops'][s_id]['products']) + 1}_{datetime.now().strftime('%s')}",
        "name": user_data["name"], "category": user_data["category"],
        "price": user_data["price"], "description": user_data["description"], "image_url": img_url
    }
    db["shops"][s_id]["products"].append(new_prod)
    write_db(db)
    
    await message.answer("✅ **Товар додано до вашого магазину!**", reply_markup=get_seller_menu())
    await state.finish()

# Удаление товаров
@seller_dp.message_handler(lambda msg: msg.text == "🗑️ Видалити товар")
async def delete_product_start(message: types.Message):
    shops = get_owner_shops(message.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"{s['emoji']} {s['name']}", callback_data=f"listdel_{s_id}"))
    await message.answer("📋 Оберіть магазин для перегляду товарів:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('listdel_'))
async def delete_product_list(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    db = read_db()
    prods = db["shops"].get(s_id, {}).get("products", [])
    if not prods:
        await call.message.answer("У цьому магазині поки немає товарів.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for p in prods:
        kb.add(InlineKeyboardButton(text=f"🗑️ Видалити {p['name']}", callback_data=f"confprod_{s_id}_{p['id']}"))
    await call.message.answer("Оберіть товар для видалення з вітрини:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('confprod_'))
async def delete_product_execute(call: types.CallbackQuery):
    _, s_id, p_id = call.data.split('_')
    db = read_db()
    if s_id in db["shops"]:
        db["shops"][s_id]["products"] = [p for p in db["shops"][s_id]["products"] if p["id"] != p_id]
        write_db(db)
        await call.message.edit_text("✅ Товар повністю видалено.")

# Удаление магазина
@seller_dp.message_handler(lambda msg: msg.text == "❌ Видалити магазин")
async def delete_shop_start(message: types.Message):
    shops = get_owner_shops(message.from_user.id)
    if not shops:
        await message.answer("У вас немає створених магазинів.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for s_id, s in shops.items():
        kb.add(InlineKeyboardButton(text=f"❌ Видалити {s['emoji']} {s['name']}", callback_data=f"delshop_{s_id}"))
    await message.answer("⚠️ **УВАГА!** Видалення магазину повністю зітре його з бази даних разом із товарами. Оберіть магазин:", reply_markup=kb)

@seller_dp.callback_query_handler(lambda call: call.data.startswith('delshop_'))
async def delete_shop_execute(call: types.CallbackQuery):
    s_id = call.data.split('_')[1]
    db = read_db()
    if s_id in db["shops"] and str(db["shops"][s_id]["owner_id"]) == str(call.from_user.id):
        del db["shops"][s_id]
        write_db(db)
        await call.message.edit_text("💥 Магазин та всі його товари повністю видалені.")
    await call.answer()

# Обработка корзины заказов (кнопки одобрения/отклонения)
@seller_dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action = action_data[0]
    buyer_id = action_data[1]
    
    if action == "ord_approve":
        total_price = float(action_data[2])
        shop_id = action_data[3]
        
        # ТОЧНЫЙ СЧЕТ КОМИССИИ 5%
        commission = round(total_price * 0.05, 2)
        
        db = read_db()
        if shop_id in db["shops"]:
            db["shops"][shop_id]["debt"] = round(db["shops"][shop_id]["debt"] + commission, 2)
            db["orders"].append({
                "date": datetime.now().strftime("%d.%m.%Y %H:%M"), "shop_id": shop_id,
                "total": total_price, "commission": commission
            })
            write_db(db)
            
            # Пишем покупателю в БОТ ДЛЯ ПОКУПАТЕЛЕЙ
            await client_bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{db['shops'][shop_id]['name']}\" підтверджено продавцем!**\n⏳ Очікуйте на ТТН доставки.")
            
            # Пишем тебе (Супер-Админу) отчет о прибыли платформы
            await client_bot.send_message(ADMIN_ID, f"💰 **Зароблено {commission} грн**\n(5% комісії від замовлення в магазині `{shop_id}` на суму {total_price} грн записано в борг).")
            
            await call.message.edit_text(call.message.text + f"\n\n✅ **Ви підтвердили замовлення. Комісія платформи {commission} грн (5%) додана до рахунку.**")
                
    elif action == "ord_decline":
        await client_bot.send_message(buyer_id, "❌ На жаль, ваше замовлення було відхилено продавцем.")
        await call.message.edit_text(call.message.text + "\n\n❌ **Ви скасували це замовлення.**")
    await call.answer()


# =======================================================
# ⏰ АВТО-БИЛЛИНГ И ЗАПУСК СЕРВЕРА
# =======================================================

def run_monday_billing_job():
    db = read_db()
    for s_id, s in db["shops"].items():
        if s["debt"] > 0:
            invoice_text = f"📊 **Час щотижневого біллінгу!**\nВаш поточний борг по комісії (5%) складає: *{s['debt']} грн*.\n\nРеквізити для оплати: `4441 1111 2222 3333`.\n\nПісля оплати напишіть головному адміну для обнулення рахунку."
            requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": invoice_text, "parse_mode": "Markdown"})

def run_tuesday_penalty_job():
    db = read_db()
    for s_id, s in db["shops"].items():
        if s["debt"] > 0 and s["status"] == "active":
            db["shops"][s_id]["status"] = "frozen"
            requests.post(f"https://api.telegram.org/bot{SELLER_TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": "❌ **Ваш магазин ЗАМОРОЖЕНО за несплату щотижневої комісії!** Покупці тимчасово не бачать ваші товари в додатку."})
    write_db(db)

scheduler = BackgroundScheduler(timezone="Europe/Kiev")
scheduler.add_job(run_monday_billing_job, CronTrigger(day_of_week='mon', hour=9, minute=0))
scheduler.add_job(run_tuesday_penalty_job, CronTrigger(day_of_week='tue', hour=9, minute=0))
scheduler.start()

Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000))), daemon=True).start()

if __name__ == "__main__":
    init_db()
    import asyncio
    
    # Создаем чистый цикл событий
    loop = asyncio.get_event_loop()
    
    # Перед запуском принудительно удаляем старые вебхуки у ОБОИХ ботов,
    # чтобы они не конфликтовали в режиме polling
    try:
        print("Очистка старых вебхуков...")
        loop.run_until_complete(client_bot.delete_webhook(drop_pending_updates=True))
        loop.run_until_complete(seller_bot.delete_webhook(drop_pending_updates=True))
        print("✨ Вебхуки успешно очищены!")
    except Exception as e:
        print(f"Предупреждение при очистке вебхуков: {e}")
    
    # Запускаем опрос для каждого бота
    loop.create_task(client_dp.start_polling())
    loop.create_task(seller_dp.start_polling())
    
    print("🚀 Система готова. Клиентский и Продавцов боты успешно запущены!")
    loop.run_forever()

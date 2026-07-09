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

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

DB_FILE = "database.json"

if not TOKEN or not ADMIN_ID:
    raise ValueError("КРИТИЧЕСКАЯ ОШИБКА: Токен бота или ID супер-админа не заданы!")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

app = Flask('')
CORS(app)

# --- ЛОКАЛЬНАЯ JSON БАЗА ДАННЫХ ---
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

def check_owner(user_id):
    db = read_db()
    for shop_id, shop in db["shops"].items():
        if str(shop["owner_id"]) == str(user_id):
            shop["shop_id"] = shop_id
            return shop
    return None

# --- СОСТОЯНИЯ FSM ---
class CreateShopState(StatesGroup):
    shop_id = State()
    name = State()
    emoji = State()

class AddProductState(StatesGroup):
    name = State()
    category = State()
    price = State()
    description = State()
    image = State()

# --- МАРШРУТЫ FLASK ---
@app.route('/')
def home(): 
    return "API SaaS автономной платформы работает!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    db = read_db()
    flat_shops = []
    for s_id, s in db["shops"].items():
        flat_shops.append({
            "shop_id": s_id,
            "name": s["name"],
            "emoji": s["emoji"],
            "status": s["status"]
        })
    return jsonify(flat_shops)

@app.route('/get-shop-products/<shop_id>', methods=['GET'])
def get_shop_products(shop_id):
    db = read_db()
    shop = db["shops"].get(shop_id)
    if not shop:
        return jsonify([])
    return jsonify(shop.get("products", []))

@app.route('/get-cities', methods=['POST'])
def get_np_cities():
    data = request.get_json() or {}
    payload = {
        "apiKey": API_KEY_NOVAPOSHTA,
        "modelName": "Address",
        "calledMethod": "getCities",
        "methodProperties": {"FindByString": data.get('cityName', ''), "Limit": "20"}
    }
    try:
        return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))
    except Exception as e:
        return jsonify([])

@app.route('/get-warehouses', methods=['POST'])
def get_np_warehouses():
    data = request.get_json() or {}
    payload = {
        "apiKey": API_KEY_NOVAPOSHTA,
        "modelName": "Address",
        "calledMethod": "getWarehouses",
        "methodProperties": {"CityRef": data.get('cityRef', ''), "Limit": "500"}
    }
    try:
        return jsonify(requests.post("https://api.novaposhta.ua/v2.0/json/", json=payload).json().get('data', []))
    except Exception as e:
        return jsonify([])

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

        if not shop:
            return jsonify({"status": "error", "message": "Shop not found"}), 404
        if shop.get('status') == 'frozen':
            return jsonify({"status": "error", "message": "Shop is suspended"}), 403

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            item_total = float(item['price']) * int(item.get('qty', 1))
            total_price += item_total
            p_text += f"{idx}. 📦 *{item['name']}*\n   🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити та відправити", callback_data=f"ord_approve:{buyer_tg_id}:{total_price}:{shop_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}")
        )

        owner_text = (
            f"🚨 **НОВЕ ЗАМОВЛЕННЯ!**\n"
            f"🏪 Магазин: *{shop['name']}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n"
        )
        
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": shop['owner_id'], "text": owner_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
        })

        return jsonify({"status": "pending_approval"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def run_web_server(): 
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000)))

Thread(target=run_web_server, daemon=True).start()

# --- ОБРАБОТКА ПОДТВЕРЖДЕНИЯ ---
@dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action = action_data[0]
    buyer_id = action_data[1]
    
    if action == "ord_approve":
        total_price = float(action_data[2])
        shop_id = action_data[3]
        commission = round(total_price * 0.10, 2)
        
        db = read_db()
        if shop_id in db["shops"]:
            db["shops"][shop_id]["debt"] = round(db["shops"][shop_id]["debt"] + commission, 2)
            db["orders"].append({
                "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                "shop_id": shop_id,
                "total": total_price,
                "commission": commission
            })
            write_db(db)
            
            await bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{db['shops'][shop_id]['name']}\" підтверджено!**\n⏳ Очікуйте на ТТН.")
            await bot.send_message(ADMIN_ID, f"💰 **Зароблено {commission} грн**\n(10% комісії від замовлення в магазині `{shop_id}` на суму {total_price} грн записано в борг).")
            await call.message.edit_text(call.message.text + f"\n\n✅ **Ви підтвердили замовлення. Комісія {commission} грн додана до рахунку.**")
                
    elif action == "ord_decline":
        await bot.send_message(buyer_id, "❌ На жаль, ваше замовлення було відхилено продавцем.")
        await call.message.edit_text(call.message.text + "\n\n❌ **Ви скасували це замовлення.**")
    await call.answer()

# --- ОНБОРДИНГ С ИНСТРУКЦИЯМИ ---
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🚀 Відкрити платформу", web_app=types.WebAppInfo(url="https://victor-1989-eng.github.io/-/")))
    
    if shop_data:
        if shop_data['status'] == 'frozen':
            await message.answer(f"❌ **Ваш магазин закритий через борг {shop_data['debt']} грн.**")
        else:
            await message.answer(f"👋 Вітаємо! Ваш магазин *{shop_data['name']}* активний.\n\n"
                                 f"ℹ️ **Як керувати магазином:**\n"
                                 f"• Використовуйте команду /admin для додавання чи видалення товарів.\n"
                                 f"• Ваші покупці знайдуть вас на головній сторінці платформи.", reply_markup=kb, parse_mode="Markdown")
    else:
        kb.add(InlineKeyboardButton(text="➕ Створити свій магазин безкоштовно", callback_data="create_shop_start"))
        await message.answer("<b>👋 Вітаємо на платформі pro_teleg.ua!</b>\n\n"
                             "Тут ви можете створити власний автономний інтернет-магазин, який працює безпосередньо всередині Telegram Mini App.\n\n"
                             "📌 **Як це працює:**\n"
                             "1. Ви створюєте магазин за 3 прості кроки.\n"
                             "2. Додаєте свої товари, ціни та фото.\n"
                             "3. Покупці оформлюють замовлення, а ви отримуєте сповіщення з даними Нової Пошти сюди в чат.\n"
                             "4. Платформа бере всього 10% комісії від успішних замовлень.", reply_markup=kb, parse_mode="HTML")

@dp.callback_query_handler(lambda call: call.data == "create_shop_start")
async def start_shop_wizard(call: types.CallbackQuery):
    if check_owner(call.from_user.id): return
    await call.message.answer("📝 **КРОК 1: Створення унікального ID магазину**\n\n"
                              "Введіть короткий техничний ідентифікатор вашого бренду **англійськими літерами** (наприклад: `perfume`, `yaroma`, `mystore`).\n\n"
                              "⚠️ _Має складатися лише з латинських літер, без пробілів та спецсимволів. Він потрібен для системних посилань!_")
    await CreateShopState.shop_id.set()

@dp.message_handler(state=CreateShopState.shop_id)
async def process_wizard_id(message: types.Message, state: FSMContext):
    s_id = message.text.strip().lower()
    db = read_db()
    if s_id in db["shops"]:
        await message.answer("❌ Цей ID вже зайнятий іншим користувачем! Придумайте інший варіант:")
        return
    await state.update_data(shop_id=s_id)
    await message.answer("📝 **КРОК 2: Публічна назва магазину**\n\n"
                         "Введіть красиву назву вашого бренду (наприклад: `YAROMA Parfum`, `Арабські парфуми`, `Luxury Style`).\n\n"
                         "👉 _Цю назву будуть бачити всі покупці на головній сторінці платформи та у каталозі товарів._")
    await CreateShopState.name.set()

@dp.message_handler(state=CreateShopState.name)
async def process_wizard_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📝 **КРОК 3: Іконка-логотип (Емодзі)**\n\n"
                         "Надішліть **один емодзі**, який найкраще відображає ваш магазин (наприклад: 🧪, 🛍️, 💄, ✨, 📦).\n\n"
                         "👉 _Цей смайлик буде відображатися як аватарка вашого магазину на головній сторінці._")
    await CreateShopState.emoji.set()

@dp.message_handler(state=CreateShopState.emoji)
async def process_wizard_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    db = read_db()
    db["shops"][user_data['shop_id']] = {
        "name": user_data['name'],
        "emoji": emoji,
        "owner_id": message.from_user.id,
        "debt": 0.0,
        "status": "active",
        "products": []
    }
    write_db(db)
    await message.answer("🎉 **ВІТАЄМО! Ваш інтернет-магазин успішно створено!**\n\n"
                         "🚀 Тепер ви повноцінний партнер платформи. Щоб заповнити вітрину товарами та відкрити продажі, напишіть команду /admin")
    await state.finish()

# --- КАНЕЦЬ АДМІНКИ С ИНСТРУКЦИЯМИ ---
@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data or shop_data['status'] == 'frozen': return
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("➕ Додати новий товар"), KeyboardButton("❌ Видалити товар"))
    await message.answer(f"⚙️ **Кабінет управління магазином: {shop_data['name']}**\n"
                         f"💰 Поточний борг по комісії: {shop_data['debt']} грн.\n\n"
                         f"📋 **Оберіть дію на клавіатурі нижче:**\n"
                         f"• Щоб завантажити новий товар на вітрину — натисніть 'Додати новий товар'\n"
                         f"• Щоб прибрати позицію з продажу — натисніть 'Видалити товар'", reply_markup=kb, parse_mode="Markdown")

@dp.message_handler(lambda msg: msg.text == "➕ Додати новий товар")
async def add_product_start(message: types.Message):
    await message.answer("📦 **ДОДАВАННЯ ТОВАРУ — КРОК 1**\n\nВведіть **НАЗВУ** товару (наприклад: `Chanel Chance 50ml` або `Олія жасмину опт`):", reply_markup=types.ReplyKeyboardRemove())
    await AddProductState.name.set()

@dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📦 **КРОК 2: КАТЕГОРІЯ**\n\nВведіть назву категорії для фільтрації (наприклад: `Жіночі парфуми`, `Унісекс`, `Ефірні олії`):")
    await AddProductState.category.set()

@dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("📦 **КРОК 3: ЦІНА**\n\nВведіть вартість товару в гривнях цифрами (наприклад: `450` або `1250`):\n\n_⚠️ Вводьте лише число, без літер 'грн'._")
    await AddProductState.price.set()

@dp.message_handler(state=AddProductState.price)
async def add_product_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text.strip())
    await message.answer("📦 **КРОК 4: ОПИС ТОВАРУ**\n\nНапишіть детальний опис (склад, ноти аромату, спосіб застосування, характеристики бренду):")
    await AddProductState.description.set()

@dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("📦 **КРОК 5: ЗАВАНТАЖЕННЯ ФОТО**\n\nНадішліть картинку/фотографію цього товару прямо сюди в чат:")
    await AddProductState.image.set()

@dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
    
    user_data = await state.get_data()
    shop_info = check_owner(message.from_user.id)
    
    if shop_info:
        db = read_db()
        s_id = shop_info["shop_id"]
        new_prod = {
            "id": f"id_{len(db['shops'][s_id]['products']) + 1}",
            "name": user_data["name"],
            "category": user_data["category"],
            "price": user_data["price"],
            "description": user_data["description"],
            "image_url": img_url
        }
        db["shops"][s_id]["products"].append(new_prod)
        write_db(db)
        await message.answer("✅ **Товар успішно додано!** Він миттєво з'явився на вашій вітрині в Mini App.")
    
    await state.finish()
    await admin_panel(message)

@dp.message_handler(lambda msg: msg.text == "❌ Видалити товар")
async def delete_product_start(message: types.Message):
    shop_info = check_owner(message.from_user.id)
    if not shop_info: return
    db = read_db()
    prods = db["shops"][shop_info["shop_id"]]["products"]
    if not prods:
        await message.answer("Каталог вашого магазину наразі порожній.")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    for p in prods:
        kb.add(InlineKeyboardButton(text=f"🗑️ {p['name']}", callback_data=f"del_{shop_info['shop_id']}:{p['id']}"))
    await message.answer("📋 Оберіть товар зі списку нижче, який ви бажаєте видалити з вітрини:", reply_markup=kb)

@dp.callback_query_handler(lambda call: call.data.startswith('del_'))
async def delete_product_confirm(call: types.CallbackQuery):
    s_id, p_id = call.data.replace('del_', '').split(':')
    db = read_db()
    if s_id in db["shops"]:
        db["shops"][s_id]["products"] = [p for p in db["shops"][s_id]["products"] if p["id"] != p_id]
        write_db(db)
        await call.answer("Видалено!")
        await call.message.edit_text("🗑️ Товар успішно видалено з вашої вітрини.")
    await admin_panel(call.message)

@dp.message_handler(lambda msg: msg.text == "💵 Я оплатив")
async def pay_rep(message: types.Message):
    shop = check_owner(message.from_user.id)
    if shop and shop["debt"] > 0:
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="✅ Обнулити борг", callback_data=f"pay_ok:{shop['shop_id']}"))
        await bot.send_message(ADMIN_ID, f"Заявка від {shop['name']}. Борг: {shop['debt']} грн.", reply_markup=kb)
        await message.answer("⏳ Заявку надіслано головному адміністратору. Очікуйте підтвердження протягом дня.")

@dp.callback_query_handler(lambda call: call.data.startswith('pay_ok:'))
async def approve_pay(call: types.CallbackQuery):
    s_id = call.data.split(':')[1]
    db = read_db()
    if s_id in db["shops"]:
        db["shops"][s_id]["debt"] = 0.0
        db["shops"][s_id]["status"] = "active"
        write_db(db)
        await call.message.edit_text("✅ Рахунок обнулено успішно!")
        await bot.send_message(db["shops"][s_id]["owner_id"], "🎉 Вашу оплату підтверджено! Магазин знову активовано та відкрито для покупців.")
    await call.answer()

# --- КРОН БИЛЛИНГ ---
def run_monday_billing_job():
    db = read_db()
    for s_id, s in db["shops"].items():
        if s["debt"] > 0:
            invoice_text = f"📊 Понеділок! Час щотижневого біллінгу. Ваш поточний борг складає: *{s['debt']} грн*.\n\nРеквізити для оплати: `4441 1111 2222 3333`.\n\nНатисніть кнопку '💵 Я оплатив' на клавіатурі нижче після здійснення переказу, щоб уникнути блокування."
            kb = ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton("💵 Я оплатил"))
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": invoice_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()})

def run_tuesday_penalty_job():
    db = read_db()
    for s_id, s in db["shops"].items():
        if s["debt"] > 0 and s["status"] == "active":
            db["shops"][s_id]["status"] = "frozen"
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": s["owner_id"], "text": "❌ Ваш магазин ЗАМОРОЖЕНО за несплату щотижневої комісії платформи! Клієнти більше не бачать ваші товари. Для розблокування перекажіть суму боргу та надішліть звіт."})
    write_db(db)

scheduler = BackgroundScheduler(timezone="Europe/Kiev")
scheduler.add_job(run_monday_billing_job, CronTrigger(day_of_week='mon', hour=9, minute=0))
scheduler.add_job(run_tuesday_penalty_job, CronTrigger(day_of_week='tue', hour=9, minute=0))
scheduler.start()

if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)

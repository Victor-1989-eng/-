import os
import io
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from urllib.parse import parse_qs
from threading import Thread

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# Библиотеки Google Таблиц
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Фоновый планировщик задач для биллинга
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO)

# Инициализация переменных окружения из Render
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID")  # Твой личный Telegram ID (Супер-админ)
API_KEY_NOVAPOSHTA = os.environ.get("NOVA_POSHTA_API_KEY")

SPREADSHEET_ID = "10J9cWFta1wfNNxh2MQj0kZ5oetgxetJleVSLJ6qc3m8"
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

if not TOKEN or not ADMIN_ID:
    raise ValueError("КРИТИЧЕСКАЯ ОШИБКА: Токен бота или ID супер-админа не заданы в Render!")

# Настройка Aiogram & Flask
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

app = Flask('')
CORS(app)

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

# --- ВЗАИМОДЕЙСТВИЕ С БАЗОЙ ДАННЫХ ---
def get_google_sheet_client():
    if not GOOGLE_CREDENTIALS_JSON:
        logging.error("Переменная GOOGLE_CREDENTIALS_JSON не найдена.")
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)
    except Exception as e:
        logging.error(f"Ошибка авторизации Google Sheets: {e}")
        return None

def check_owner(user_id):
    """Возвращает информацию о магазине пользователя, если он существует"""
    client = get_google_sheet_client()
    if not client: return None
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        all_rows = sheet.get_all_values() 
        for row in all_rows[1:]: 
            if len(row) >= 7 and str(row[3]).strip() == str(user_id).strip():
                return {
                    'shop_id': row[0],
                    'name': row[1],
                    'emoji': row[2],
                    'owner_id': row[3],
                    'sheet_name': row[4],
                    'debt': float(row[5] if row[5] else 0),
                    'status': row[6]
                }
    except Exception as e:
        logging.error(f"Ошибка в check_owner: {e}")
    return None

# --- МАРШРУТЫ FLASK (ОБРАБОТКА WEBAPP) ---
@app.route('/')
def home(): 
    return "API SaaS платформы pro_teleg.ua работает в штатном режиме!"

@app.route('/get-shops-status', methods=['GET'])
def get_shops_status():
    """Эндпоинт для фронтенда, чтобы WebApp знал, кто заморожен, а кто активен"""
    client = get_google_sheet_client()
    if not client: return jsonify([])
    try:
        records = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS").get_all_records()
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

@app.route('/submit-order', methods=['POST'])
def handle_submit_order():
    """Принимает заказ из WebApp и отправляет его продавцу на верификацию"""
    try:
        req_data = request.get_json()
        cart = req_data['cart']
        delivery = req_data['delivery']
        init_data_raw = req_data['initData']
        shop_id = req_data.get('shop_id')

        user_json = json.loads(parse_qs(init_data_raw)['user'][0])
        buyer_tg_id = user_json.get('id')

        client = get_google_sheet_client()
        if not client: return jsonify({"status": "error", "message": "Database error"}), 500

        # Ищем владельца и проверяем статус магазина
        shops_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        shops_records = shops_sheet.get_all_records()
        
        target_shop = None
        for row in shops_records:
            if str(row.get('shop_id')).strip() == str(shop_id).strip():
                target_shop = row
                break

        if not target_shop:
            return jsonify({"status": "error", "message": "Shop not found"}), 44
        if target_shop.get('status') == 'frozen':
            return jsonify({"status": "error", "message": "Shop is suspended"}), 403

        owner_tg_id = target_shop.get('owner_id')
        shop_name = target_shop.get('name')

        total_price = 0.0
        p_text = ""
        for idx, item in enumerate(cart, 1):
            item_total = float(item['price']) * int(item.get('qty', 1))
            total_price += item_total
            p_text += f"{idx}. 📦 *{item['name']}*\n   🔢 Кіл-ть: {item.get('qty', 1)} шт. | 💰 Ціна: {item['price']} грн\n\n"

        # Формируем структуру данных заказа для передачи через CallbackQuery кнопки подтверждения
        # Так как размер callback_data ограничен 64 байтами, мы сохраняем временные данные заказа в мета-сообщение продавцу
        order_meta = {
            "shop_id": shop_id,
            "shop_name": shop_name,
            "buyer_id": buyer_tg_id,
            "total": total_price,
            "delivery": delivery,
            "cart_text": p_text
        }
        
        # Отправляем продавцу запрос на подтверждение
        kb = InlineKeyboardMarkup(row_width=2)
        # В callback_data передаем только экшен, сами данные вытянем из текста сообщения при нажатии
        kb.add(
            InlineKeyboardButton(text="✅ Підтвердити та відправити", callback_data=f"ord_approve:{buyer_tg_id}:{total_price}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ord_decline:{buyer_tg_id}")
        )

        owner_text = (
            f"🚨 **НОВЕ ЗАМОВЛЕННЯ ПОТРЕБУЄ ПІДТВЕРДЖЕННЯ!**\n"
            f"🏪 Магазин: *{shop_name}* (`{shop_id}`)\n\n"
            f"📋 **Товари:**\n{p_text}"
            f"🚚 **Доставка:** {delivery['city']}, {delivery['warehouse']}\n"
            f"👤 **Отримувач:** {delivery['name']} ({delivery['phone']})\n"
            f"💳 **Оплата:** {delivery.get('payment')}\n"
            f"💰 **Сума замовлення:** {total_price} грн\n\n"
            f"⚠️ *Натисніть кнопку нижче для підтвердження. Після підтвердження вам нарахується 10% комісії платформи в борг.*"
        )
        
        # Отправляем продавцу
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": owner_tg_id, "text": owner_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
        })

        return jsonify({"status": "pending_approval"}), 200
    except Exception as e:
        logging.error(f"Ошибка в submit-order: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def run_web_server(): 
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8000)))

Thread(target=run_web_server, daemon=True).start()


# --- ОБРАБОТКА ПОДТВЕРЖДЕНИЯ ЗАКАЗОВ ПРОДАВЦОМ ---
@dp.callback_query_handler(lambda call: call.data.startswith('ord_'))
async def process_order_decision(call: types.CallbackQuery):
    action_data = call.data.split(':')
    action = action_data[0]
    buyer_id = action_data[1]
    
    if action == "ord_approve":
        total_price = float(action_data[2])
        commission = round(total_price * 0.10, 2)
        
        # Получаем данные текущего магазина продавца
        shop_data = check_owner(call.from_user.id)
        if not shop_data: return

        client = get_google_sheet_client()
        if client:
            try:
                # 1. Записываем начисление долга продавцу в лист SHOPS
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
                cells = sheet.findall(shop_data['shop_id'])
                if cells:
                    row_idx = cells[0].row
                    current_debt = float(sheet.cell(row_idx, 6).value or 0)
                    new_debt = round(current_debt + commission, 2)
                    sheet.update_cell(row_idx, 6, new_debt) # Обновляем колонку F (Долг)

                # 2. Логируем успешный заказ в общую таблицу ORDERS
                client.open_by_key(SPREADSHEET_ID).worksheet("ORDERS").append_row([
                    datetime.now().strftime("%d.%m.%Y %H:%M"), 
                    shop_data['shop_id'], 
                    shop_data['name'], 
                    total_price, 
                    commission
                ])
                
                # 3. Оповещаем покупателя
                await bot.send_message(buyer_id, f"🎉 **Ваше замовлення в магазині \"{shop_data['name']}\" підтверджено продавцем!**\n⏳ Очікуйте на ТТН доставки.")
                
                # 4. Оповещаем Главного Админа платформы (тебя) про автоматический заработок в USDT/эквиваленте
                await bot.send_message(ADMIN_ID, f"💰 **Зароблено ${commission}** (10% від замовлення в магазині `{shop_data['shop_id']}` на суму {total_price} грн). Долг записан продавцу.")
                
                # Обновляем сообщение продавца
                await call.message.edit_text(call.message.text + f"\n\n✅ **Ви підтвердили замовлення. Комісія платформи {commission} грн додана до вашого балансу рахунку.**")
            except Exception as e:
                await call.answer(f"Помилка бд: {e}", show_alert=True)
                
    elif action == "ord_decline":
        await bot.send_message(buyer_id, "❌ На жаль, ваше замовлення було відхилено продавцем (немає в наявності або невірні дані).")
        await call.message.edit_text(call.message.text + "\n\n❌ **Ви скасували це замовлення. Комісія не нарахована.**")
    
    await call.answer()


# --- АВТОМАТИЧЕСКИЙ ОНБОРДИНГ И КОНСТРУКТОР МАГАЗИНА ---
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton(text="🚀 Відкрити платформу pro_teleg.ua", web_app=types.WebAppInfo(url="https://victor-1989-eng.github.io/-/")))
    
    if shop_data:
        if shop_data['status'] == 'frozen':
            await message.answer(f"❌ **Ваш магазин \"{shop_data['name']}\" ЗАМОРОЖЕНО за неуплату комісії.**\n\nАдмін-панель заблокована. Будь ласка, погасіть заборгованість {shop_data['debt']} грн, надіславши чек адміністратору @SuperAdmin.")
        else:
            await message.answer(f"👋 **Вітаємо в кабінеті платформи!**\n\nВи є власником активного магазину *{shop_data['name']}*.\n\n⚙️ Для управління каталогом товарів введіть команду /admin", reply_markup=kb, parse_mode="Markdown")
    else:
        kb.add(InlineKeyboardButton(text="➕ Створити свій магазин безкоштовно", callback_data="create_shop_start"))
        await message.answer(
            "<b>👋 Вітаємо на платформі розумних Telegram-магазинів pro_teleg.ua!</b>\n\n"
            "Запустіть власну торгову точку з інтеграцією Нової Пошти абсолютно безкоштовно.\n\n"
            "💵 **Умови використання:** Платформа не бере передоплат. Ви сплачуєте лише **10% комісії** від суми замовлень, які ви особисто підтвердили. Розрахунок — щопонеділка.\n\n"
            "Бажаєте створити свій бренд прямо зараз? Натисніть кнопку нижче 👇", 
            reply_markup=kb, parse_mode="HTML"
        )

@dp.callback_query_handler(lambda call: call.data == "create_shop_start")
async def start_shop_wizard(call: types.CallbackQuery):
    if check_owner(call.from_user.id): return
    await call.message.answer("✨ **Запуск майстра створення магазину**\n\n**Крок 1 із 3:** Введіть унікальний текстовий ID магазину англійськими літерами (наприклад: `yaroma`, `stones_shop`):")
    await CreateShopState.shop_id.set()

@dp.message_handler(state=CreateShopState.shop_id)
async def process_wizard_id(message: types.Message, state: FSMContext):
    s_id = message.text.strip().lower()
    if not s_id.isalnum():
        await message.answer("❌ Помилка! ID повинен містити тільки латинські літери або цифри без пробілів та знаків.")
        return
    
    client = get_google_sheet_client()
    if client:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        if sheet.findall(s_id):
            await message.answer("❌ Цей ID вже зайнятий іншим брендом. Придумайте інший:")
            return
            
    await state.update_data(shop_id=s_id)
    await message.answer("📛 **Крок 2 із 3:** Введіть красиву публічну НАЗВУ вашого магазину (наприклад: *Парфумерія YAROMA* або *Світ Джасперу*):")
    await CreateShopState.name.set()

@dp.message_handler(state=CreateShopState.name)
async def process_wizard_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("🧪 **Крок 3 із 3:** Надішліть один ЕМОДЗІ, який буде відображатися як фірмова іконка вашого бренду в каталоге (наприклад: 🧪, 💎, 🛍️, 🪵):")
    await CreateShopState.emoji.set()

@dp.message_handler(state=CreateShopState.emoji)
async def process_wizard_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    user_data = await state.get_data()
    
    client = get_google_sheet_client()
    if not client:
        await message.answer("❌ Сталася критична помилка підключення до БД.")
        await state.finish()
        return

    sheet_name = f"products_{user_data['shop_id']}"
    try:
        doc = client.open_by_key(SPREADSHEET_ID)
        
        # АВТОМАТИЧЕСКАЯ ГЕНЕРАЦИЯ ЛИСТА ДЛЯ НОВОГО ПРОДАВЦА
        new_sheet = doc.add_worksheet(title=sheet_name, rows="300", cols="10")
        new_sheet.append_row(["id", "name", "category", "price", "old_price", "description", "image_url"])
        
        # РЕГИСТРАЦИЯ В СИСТЕМЕ БИЛЛИНГА
        shops_sheet = doc.worksheet("SHOPS")
        shops_sheet.append_row([user_data['shop_id'], user_data['name'], emoji, str(message.from_user.id), sheet_name, 0, "active"])
        
        await message.answer(
            f"🎉 **ВІТАЄМО! Магазин \"{user_data['name']}\" успішно створено та інтегровано в WebApp!**\n\n"
            f"Він вже доступний усім покупцям платформи.\n"
            f"Введіть команду /admin для переходу в особистий кабінет управління товарною сіткою."
        )
    except Exception as e:
        await message.answer(f"❌ Помилка автоматизації Google таблиць: {e}")
        
    await state.finish()


# --- КАБИНЕТ УПРАВЛЕНИЯ ТОВАРАМИ (/admin) ---
@dp.message_handler(commands=['admin'])
async def admin_panel(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data:
        await message.answer("❌ У вас ще немає магазину. Введіть /start, щоб створити його.")
        return
        
    if shop_data['status'] == 'frozen':
        await message.answer(f"❌ **Ваш кабінет заблоковано через заборгованість по комісії платформи ({shop_data['debt']} грн).**\nБудь ласка, здійсніть оплату.")
        return

    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("➕ Додати новий товар"), KeyboardButton("❌ Видалити товар"))
    await message.answer(f"⚙️ **Кабінет власника бренду: {shop_data['name']}**\n💰 Поточний борг по комісії: {shop_data['debt']} грн.\n\nОберіть дію на клавіатурі:", reply_markup=kb)

@dp.message_handler(lambda msg: msg.text == "➕ Додати новий товар")
async def add_product_start(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data or shop_data['status'] == 'frozen': return
    await message.answer("📝 **Введіть НАЗВУ товару:**", reply_markup=types.ReplyKeyboardRemove())
    await AddProductState.name.set()

@dp.message_handler(state=AddProductState.name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("📁 **Введіть КАТЕГОРІЮ товару (наприклад: Жіночі, Чоловічі, Унісекс, Кабошони):**")
    await AddProductState.category.set()

@dp.message_handler(state=AddProductState.category)
async def add_product_category(message: types.Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("💰 **Введіть ЦІНУ товару в гривнях (лише ціле число):**")
    await AddProductState.price.set()

@dp.message_handler(state=AddProductState.price)
async def add_product_price(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введіть коректне число!")
        return
    await state.update_data(price=int(message.text))
    await message.answer("📖 **Введіть ОПИС товару:**")
    await AddProductState.description.set()

@dp.message_handler(state=AddProductState.description)
async def add_product_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("📸 **Надішліть ФОТОГРАФІЮ товару:**")
    await AddProductState.image.set()

@dp.message_handler(content_types=['photo'], state=AddProductState.image)
async def add_product_image(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    public_img_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

    user_data = await state.get_data()
    shop_data = check_owner(message.from_user.id)

    client = get_google_sheet_client()
    if client and shop_data:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
            new_id = f"id_{len(sheet.get_all_values()) + 1}"
            sheet.append_row([new_id, user_data['name'], user_data['category'], user_data['price'], 0, user_data['description'], public_img_url])
            await message.answer(f"✅ Товар \"{user_data['name']}\" успішно завантажено на вітрину!")
        except Exception as e:
            await message.answer(f"❌ Помилка запису в таблицю: {e}")
    
    await state.finish()
    await admin_panel(message)

@dp.message_handler(lambda msg: msg.text == "❌ Видалити товар")
async def delete_product_start(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data or shop_data['status'] == 'frozen': return
    client = get_google_sheet_client()
    if not client: return
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
        records = sheet.get_all_records()
        if not records:
            await message.answer("📦 Творів не знайдено, каталог порожній.")
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for row in records:
            kb.add(InlineKeyboardButton(text=f"🗑️ {row.get('name')}", callback_data=f"del_{row.get('id')}"))
        await message.answer("👇 **Оберіть товар для видалення з вітрини:**", reply_markup=kb)
    except Exception as e: pass

@dp.callback_query_handler(lambda call: call.data.startswith('del_'))
async def delete_product_confirm(call: types.CallbackQuery):
    prod_id = call.data.replace('del_', '')
    shop_data = check_owner(call.from_user.id)
    client = get_google_sheet_client()
    if client and shop_data:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(shop_data['sheet_name'])
            data = sheet.get_all_values()
            row_index = -1
            for idx, row in enumerate(data):
                if row[0] == prod_id:
                    row_index = idx + 1
                    break
            if row_index != -1:
                sheet.delete_rows(row_index)
                await call.answer("Товар видалено з бази!", show_alert=True)
                await call.message.edit_text("🗑️ Товар успішно видалено з вашої вітрини.")
        except: pass
    await admin_panel(call.message)


# --- МОДУЛЬ 1-КЛИК ПОДТВЕРЖДЕНИЯ ОПЛАТ ДЛЯ СУПЕР-АДМИНА ---
@dp.message_handler(lambda msg: msg.text == "💵 Я оплатив")
async def seller_reported_payment(message: types.Message):
    shop_data = check_owner(message.from_user.id)
    if not shop_data: return
    
    if shop_data['debt'] <= 0:
        await message.answer("💰 У вас немає заборгованості перед платформою.")
        return

    # Отправляем инвойс-уведомление тебе (Супер-Админу)
    kb = InlineKeyboardMarkup().add(InlineKeyboardButton(text="✅ Підтвердити платіж та обнулити борг", callback_data=f"pay_confirm:{shop_data['shop_id']}"))
    
    await bot.send_message(ADMIN_ID, 
        f"🔔 **ЗАЯВКА НА ОПЛАТУ КОМІСІЇ!**\n\n"
        f"🏬 Магазин: *{shop_data['name']}* (`{shop_data['shop_id']}`)\n"
        f"👤 Власник: [{message.from_user.full_name}](tg://user?id={message.from_user.id})\n"
        f"💵 Сума боргу в базі: *{shop_data['debt']} грн*\n\n"
        f"Перевірте баланс вашої картки. Якщо кошти надійшли, натисніть кнопку нижче:",
        reply_markup=kb, parse_mode="Markdown"
    )
    await message.answer("⏳ **Заявку надіслано адміністратору.**\nПісля перевірки зарахування грошей на карту, ваш рахунок буде обнулено, а статус автоматично оновиться.")

@dp.callback_query_handler(lambda call: call.data.startswith('pay_confirm:'))
async def superadmin_approve_payment(call: types.CallbackQuery):
    if str(call.from_user.id) != str(ADMIN_ID): return
    
    target_shop_id = call.data.split(':')[1]
    client = get_google_sheet_client()
    if client:
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
            cells = sheet.findall(target_shop_id)
            if cells:
                row_idx = cells[0].row
                owner_id = sheet.cell(row_idx, 4).value
                
                # Обнуляем долг (Колонка F) и возвращаем статус active (Колонка G)
                sheet.update_cell(row_idx, 6, 0)
                sheet.update_cell(row_idx, 7, "active")
                
                await call.message.edit_text(call.message.text + "\n\n✅ **Платіж успішно підтверджено. Рахунок обнулено, магазин активовано!**")
                await bot.send_message(owner_id, "🎉 **Адміністратор підтвердив вашу оплату комісії!**\nВаш баланс обнулено, магазин повністю активний. Дякуємо за співпрацю!")
        except Exception as e:
            await call.answer(f"Помилка: {e}", show_alert=True)
    await call.answer()


# =====================================================================
# --- АВТОНОМНЫЙ КРОН-МОДУЛЬ БИЛЛИНГА (ПОНЕДЕЛЬНИК / ВТОРНИК) ---
# =====================================================================
def run_monday_billing_job():
    """Каждый понедельник считывает долги и выставляет счета"""
    client = get_google_sheet_client()
    if not client: return
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        records = sheet.get_all_records()
        
        admin_report = "📊 **ЗВІТ ПО КОМІСІЇ ЗА ТИЖДЕНЬ (ПОНЕДЕЛЬНИК):**\n\n"
        total_expected = 0
        
        for row in records:
            debt = float(row.get('Текущий_Долг_грн') or 0)
            owner_id = row.get('owner_id')
            shop_name = row.get('name')
            
            if debt > 0:
                total_expected += debt
                admin_report += f"• `{row.get('shop_id')}` — {debt} грн ({row.get('status')})\n"
                
                # Отправляем счет продавцу
                invoice_text = (
                    f"📊 **ПОНЕДІЛОК — ЧАС РОЗРАХУНКУ ЗА ІНФРАСТРУКТУРУ!**\n\n"
                    f"Ваша накопичена комісія платформи (10%) становить: *{debt} грн*.\n\n"
                    f"💳 **Реквізити для оплати (Монобанк):** `4441 1111 2222 3333`\n"
                    f"👤 Отримувач: Твій ПІБ\n\n"
                    f"⚠️ **Важливо:** Після переказу грошей надішліть скріншот чеку адміністратору @SuperAdmin та натисніть кнопку **«💵 Я оплатил»** нижче. "
                    f"Якщо оплата не надійде, завтра (у вівторок) о 09:00 ранку ваш магазин буде автоматично ЗАМОРОЖЕНО."
                )
                kb = ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton("💵 Я оплатил"))
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                    "chat_id": owner_id, "text": invoice_text, "parse_mode": "Markdown", "reply_markup": kb.to_python()
                })
                
        admin_report += f"\n💰 **Всього очікується до виплати:** {total_expected} грн"
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": ADMIN_ID, "text": admin_report, "parse_mode": "Markdown"})
    except Exception as e:
        logging.error(f"Ошибка биллинга понедельника: {e}")

def run_tuesday_penalty_job():
    """Каждый вторник проверяет долги и автоматически замораживает неплательщиков"""
    client = get_google_sheet_client()
    if not client: return
    try:
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("SHOPS")
        all_rows = sheet.get_all_values()
        
        frozen_list = "🛑 **ЗВІТ ПРО АВТОМАТИЧНУ ЗАМОРОЗКУ (ВІВТОРОК):**\n\n"
        any_frozen = False
        
        # Перебираем строки со второй
        for idx, row in enumerate(all_rows[1:], start=2):
            debt = float(row[5] if row[5] else 0)
            status = row[6]
            owner_id = row[3]
            shop_name = row[1]
            
            if debt > 0 and status == "active":
                # Замораживаем в таблице
                sheet.update_cell(idx, 7, "frozen")
                any_frozen = True
                frozen_list += f"• Магазин `{row[0]}` ({shop_name}) — Борг: {debt} грн\n"
                
                # Уведомляем продавца о блокировке
                block_text = (
                    f"❌ **ВАШ МАГАЗИН ЗАМОРОЖЕНО ЗА НЕУПЛАТУ КОМІСІЇ!**\n\n"
                    f"Доступ до кабінету /admin та відображення ваших товарів у додатку WebApp призупинено. "
                    f"Для розблокування переведіть {debt} грн на реквізиты платформи та зв'яжіться з @SuperAdmin."
                )
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
                    "chat_id": owner_id, "text": block_text, "reply_markup": {"remove_keyboard": True}
                })
                
        if not any_frozen: frozen_list += "Злісних неплатників не виявлено. Усі магазини працюють!"
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": ADMIN_ID, "text": frozen_list, "parse_mode": "Markdown"})
    except Exception as e:
        logging.error(f"Ошибка карателя вторника: {e}")

# Настройка планировщика APScheduler
scheduler = BackgroundScheduler(timezone="Europe/Kiev")
# Понедельник в 09:00 — Выставление счетов
scheduler.add_job(run_monday_billing_job, CronTrigger(day_of_week='mon', hour=9, minute=0))
# Вторник в 09:00 — Заморозка должников
scheduler.add_job(run_tuesday_penalty_job, CronTrigger(day_of_week='tue', hour=9, minute=0))
scheduler.start()

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)

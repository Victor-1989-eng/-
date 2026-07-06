import os
import csv
import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import asyncio

# Логирование
logging.basicConfig(level=logging.INFO)

# Конфигурация (Порт для Render и токены)
PORT = int(os.environ.get("PORT", 8080))
TOKEN = "7911273494:AAF7kzkhB6vnWJIodrRojR3eWJkH036681s"
ADMIN_ID = 7215386084  # ЗАМЕНИ НА СВОЙ ТЕЛЕГРАМ ID

# Курс для расчета в USDT (примерный, можно подставить динамический)
USDT_RATE = 45.0 

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# --- ФУНКЦИИ БАЗЫ ДАННЫХ CSV ---
def get_products():
    products = []
    if os.path.exists("products.csv"):
        with open("products.csv", mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                products.append(row)
    return products

# --- КЛАВИАТУРЫ ---
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("🛍️ Каталог духов"))
main_menu.add(KeyboardButton("ℹ️ О бренде"), KeyboardButton("📦 Доставка"))

# --- ХЕНДЛЕРЫ КОМАНД ---
@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! Добро пожаловать в парфюмерную лабораторию. "
        f"Здесь ты можешь заказать уникальные нишевые ароматы на натуральных маслах.", 
        reply_markup=main_menu
    )

@dp.message_handler(lambda msg: msg.text == "ℹ️ О бренде")
async def about_brand(message: types.Message):
    await message.answer(
        "✨ **Наш бренд** — это сочетание цифровой точности и дикой природы.\n\n"
        "Каждый флакон выдерживается строго **4 недели** для полного раскрытия молекул масел. "
        "Мы создаем концептуальную нишу, которой нет на полках обычных магазинов."
    )

@dp.message_handler(lambda msg: msg.text == "📦 Доставка")
async def delivery_info(message: types.Message):
    await message.answer("🚀 Доставка по всей Украине Новой Почтой или Укрпочтой. Отправка в течение 1-2 дней.")

@dp.message_handler(lambda msg: msg.text == "🛍️ Каталог духов")
async def show_categories(message: types.Message):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👨 Мужские", callback_data="cat_Мужские"),
        InlineKeyboardButton("🌓 Унисекс", callback_data="cat_Унисекс"),
        InlineKeyboardButton("🧪 Наборы пробников", callback_data="cat_Сеты")
    )
    await message.answer("Выберите интересующую категорию ароматов:", reply_markup=kb)

# Просмотр товаров в категории
@dp.callback_query_handler(lambda c: c.data.startswith('cat_'))
async def process_category(callback_query: types.CallbackQuery):
    category = callback_query.data.split('_')[1]
    products = get_products()
    filtered = [p for p in products if p['category'] == category]
    
    if not filtered:
        await bot.answer_callback_query(callback_query.id, text="В этой категории пока нет товаров.")
        return

    await bot.answer_callback_query(callback_query.id)
    
    for p in filtered:
        text = f"🔥 **{p['name']}**\n\n{p['description']}\n\n💰 Цена: {p['price']} грн"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🛒 Купить флакон", callback_data=f"buy_{p['id']}"))
        await bot.send_message(callback_query.from_user.id, text, reply_markup=kb, parse_mode="Markdown")

# Логика покупки и моментального уведомления админа с расчетом прибыли в USDT
@dp.callback_query_handler(lambda c: c.data.startswith('buy_'))
async def process_buy(callback_query: types.CallbackQuery):
    prod_id = callback_query.data.split('_')[1]
    products = get_products()
    product = next((p for p in products if p['id'] == prod_id), None)
    
    if not product:
        await bot.answer_callback_query(callback_query.id, text="Товар не найден.")
        return

    await bot.answer_callback_query(callback_query.id)
    
    # 1. Отвечаем клиенту
    await bot.send_message(
        callback_query.from_user.id,
        f"✅ Вы выбрали **{product['name']}**.\n"
        f"Наш менеджер свяжется с вами в ближайшее время для уточнения данных доставки!"
    )
    
    # 2. Считаем чистую прибыль в грн и USDT
    price = float(product['price'])
    cost = float(product['cost'])
    profit_uan = price - cost
    profit_usdt = profit_uan / USDT_RATE
    
    # 3. Моментальный рапорт супер-админу в Telegram
    admin_report = (
        f"🔔 **НОВЫЙ ЗАКАЗ!**\n"
        f"👤 Покупатель: @{callback_query.from_user.username or 'без юзернейма'} (ID: {callback_query.from_user.id})\n"
        f"📦 Товар: {product['name']} ({product['price']} грн)\n"
        f"---------------------------\n"
        f"📈 Чистая прибыль: +{profit_uan:.2f} грн\n"
        f"💵 Earned ${profit_usdt:.2f} USDT"
    )
    
    await bot.send_message(ADMIN_ID, admin_report, parse_mode="Markdown")

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (ФУНКЦИЯ ПРОГРЕВА) ---
async def handle_ping(request):
    return web.Response(text="Bot is alive and smelling good!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Web server started on port {PORT}")

# Запуск всего вместе
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server()) # Запуск веб-сервера на порту Render
    executor.start_polling(dp, loop=loop, skip_updates=True)

import os
import csv
import logging
import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Включаем логирование, чтобы видеть ошибки в консоли Render
logging.basicConfig(level=logging.INFO)

# Инициализация бота. Токен и ID админа берутся из переменных окружения (Environment Variables)
TOKEN = "7911273494:AAF7kzkhB6vnWJIodrRojR3eWJkH036681s"
ADMIN_ID = "7215386084"  # Твой Telegram ID для уведомлений о прибыли

if not TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения BOT_TOKEN не задана!")
if not ADMIN_ID:
    raise ValueError("ОШИБКА: Переменная окружения ADMIN_ID не задана!")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# Функция для загрузки актуального курса USDT к гривне через открытый API
async def get_usdt_rate():
    try:
        async with aiohttp.ClientSession() as session:
            # Используем CoinGecko API для получения стабильного курса
            url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=uah"
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    return data['tether']['uah']
    except Exception as e:
        logging.error(f"Не удалось получить курс валют: {e}")
    return 41.5  # Стабильный запасной курс на случай сбоя API

# Функция для чтения базы данных товаров из CSV
def get_products():
    products = []
    if not os.path.exists('products.csv'):
        return products
    with open('products.csv', mode='r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        for row in reader:
            products.append(row)
    return products

# ОБНОВЛЕННЫЙ ХЕНДЛЕР /start С КНОПКОЙ-МИНИ-САЙТОМ
@dp.message_handler(commands=['start'])
async def send_welcome(message: types.Message):
    # Создаем специальную кнопку типа WebApp
    kb = InlineKeyboardMarkup()
    
    # Твоя ссылка на опубликованный HTML файл на GitHub Pages
    web_app_url = "https://victor-1989-eng.github.io/-/" 
    
    kb.add(InlineKeyboardButton(
        text="🛍️ Открыть магазин YAROMA", 
        web_app=types.WebAppInfo(url=web_app_url)
    ))
    
    welcome_text = (
        "👋 **Добро пожаловать в инновационный парфюмерный бутик!**\n\n"
        "Мы создаем элитные духи на основе лучших европейских концентратов.\n\n"
        "👇 Нажмите на кнопку ниже, чтобы открыть интерактивную витрину с выбором объема:"
    )
    
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

# ХЕНДЛЕР ПОЛУЧЕНИЯ ДАННЫХ ИЗ МИНИ-САЙТА (Когда нажали «Купить»)
@dp.message_handler(content_types=types.ContentType.WEB_APP_DATA)
async def process_web_app_data(message: types.Message):
    import json
    # Получаем JSON строку, которую отправила функция sendOrder() из HTML
    data = json.loads(message.web_app_data.data)
    
    # Отправляем подтверждение клиенту
    await message.answer(
        f"✅ **Заявка оформлена через мини-сайт!**\n\n"
        f"📦 **Товар:** {data['name']}\n"
        f"🧪 **Объем:** {data['volume']}\n"
        f"🔢 **Количество:** {data['qty']} шт.\n"
        f"💰 **Итого к оплате:** {data['price']}\n\n"
        f"Наш менеджер уже пишет вам в ЛС для подтверждения доставки!"
    )
    
    # Отправка админу (тебе) с автоматическим расчетом прибыли тоже будет работать здесь!
    # (Сюда можно вставить блок отправки в ADMIN_ID, который мы делали ранее).
# ХЕНДЛЕР ПРОСМОТРА КАТЕГОРИИ (Вывод карточек с фото)
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
        # Формируем премиальное описание под фото
        caption_text = (
            f"{p['description']}\n\n"
            f"🏷️ **Категория:** {p['category']}\n"
            f"💰 **Цена:** {p['price']} грн"
        )
        
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🛒 Оформить заказ", callback_data=f"buy_{p['id']}"))
        
        # Если есть ссылка на фото, шлем картинку с текстом внизу
        if p.get('image_url') and p['image_url'].startswith('http'):
            try:
                await bot.send_photo(
                    chat_id=callback_query.from_user.id,
                    photo=p['image_url'],
                    caption=caption_text,
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Ошибка отправки фото для товара {p['id']}: {e}")
                # Резервный вариант, если фотохостинг упал
                await bot.send_message(
                    chat_id=callback_query.from_user.id,
                    text=f"🔥 **{p['name']}**\n\n{caption_text}",
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
        else:
            await bot.send_message(
                chat_id=callback_query.from_user.id,
                text=f"🔥 **{p['name']}**\n\n{caption_text}",
                reply_markup=kb,
                parse_mode="Markdown"
            )

# ХЕНДЛЕР НАЖАТИЯ КНОПКИ «КУПИТЬ»
@dp.callback_query_handler(lambda c: c.data.startswith('buy_'))
async def process_buying(callback_query: types.CallbackQuery):
    product_id = callback_query.data.split('_')[1]
    products = get_products()
    product = next((p for p in products if p['id'] == product_id), None)
    
    if not product:
        await bot.answer_callback_query(callback_query.id, text="Товар не найден.")
        return
        
    await bot.answer_callback_query(callback_query.id)
    
    user = callback_query.from_user
    username = f"@{user.username}" if user.username else "Нет юзернейма"
    
    # 1. Отправляем подтверждение клиенту
    await bot.send_message(
        chat_id=user.id,
        text=f"✅ **Заявка принята!**\n\nВы выбрали: *{product['name']}* за *{product['price']} грн*.\n"
             f"Наш менеджер уже связывается с вами в личных сообщениях для уточнения доставки."
    )
    
    # 2. Считаем чистую прибыль в грн и переводим в USDT
    price_грн = float(product['price'])
    cost_грн = float(product['cost'])
    profit_грн = price_грн - cost_грн
    
    usdt_rate = await get_usdt_rate()
    profit_usdt = profit_грн / usdt_rate
    
    # 3. Отправляем уведомление тебе (админу)
    admin_text = (
        f"🚨 **НОВЫЙ ЗАКАЗ!**\n\n"
        f"👤 **Покупатель:** {user.full_name} ({username})\n"
        f"🆔 **ID:** `{user.id}`\n\n"
        f"📦 **Товар:** {product['name']}\n"
        f"💰 **Цена:** {price_грн} грн\n"
        f"📉 **Себестоимость:** {cost_грн} грн\n"
        f"📈 **Прибыль:** {profit_грн:.2f} грн\n\n"
        f"💵 **Заработано: ${profit_usdt:.2f}**"
    )
    
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)

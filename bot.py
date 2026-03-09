import asyncio
import logging
import os
import sqlite3
import json
import aiosmtplib
from email.message import EmailMessage
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ========== Конфигурация ==========
API_TOKEN = os.getenv("API_TOKEN", "YOUR_BOT_TOKEN")          # Токен бота
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "123456789")       # Telegram ID администратора (куда слать заявки)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")   # Почта для заявок

# Данные для отправки почты (SMTP)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "your_email@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "your_password")

DATABASE = "septic_bot.db"

logging.basicConfig(level=logging.INFO)

# ========== Инициализация БД ==========
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()

    # Пользователи
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Септики (товары/услуги)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price INTEGER,
            capacity INTEGER,          -- максимальное количество человек
            high_groundwater BOOLEAN,  -- подходит ли для высоких грунтовых вод
            clay_soil BOOLEAN,         -- подходит ли для глинистых грунтов
            image_url TEXT
        )
    ''')

    # Заявки
    cur.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            answers TEXT,               -- JSON с ответами на вопросы
            name TEXT,
            phone TEXT,
            status TEXT DEFAULT 'new',  -- new, contacted, closed
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    ''')

    # Добавляем тестовые септики, если таблица пуста
    cur.execute("SELECT COUNT(*) FROM products")
    if cur.fetchone()[0] == 0:
        products = [
            ("Септик Топас 5", "Компактная модель для дачи", 85000, 5, 1, 1, ""),
            ("Септик Топас 8", "Для большого дома", 115000, 8, 1, 1, ""),
            ("Септик Эко-Гранд 6", "Энергонезависимый, подходит для высоких грунтовых вод", 95000, 6, 1, 0, ""),
            ("Септик Эко-Гранд 10", "Увеличенный объём для глинистых почв", 135000, 10, 0, 1, ""),
            ("Септик Био-Дек 4", "Для сезонного проживания", 65000, 4, 0, 0, "")
        ]
        cur.executemany('''
            INSERT INTO products (name, description, price, capacity, high_groundwater, clay_soil, image_url)
            VALUES (?,?,?,?,?,?,?)
        ''', products)

    conn.commit()
    conn.close()

# ---------- Работа с пользователями ----------
def register_user(user_id, username, full_name):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)",
                (user_id, username, full_name))
    conn.commit()
    conn.close()

def update_user_phone(user_id, phone):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET phone = ? WHERE user_id = ?", (phone, user_id))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, full_name, phone FROM users WHERE user_id=?", (user_id,))
    user = cur.fetchone()
    conn.close()
    return user

# ---------- Работа с септиками ----------
def get_all_products():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, price, capacity FROM products")
    products = cur.fetchall()
    conn.close()
    return products

def get_product(product_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, price FROM products WHERE id=?", (product_id,))
    product = cur.fetchone()
    conn.close()
    return product

def find_matching_products(people_count, high_groundwater, clay_soil):
    """
    Подбирает подходящие септики на основе ответов пользователя.
    Возвращает список кортежей (id, name, price, description)
    """
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    query = "SELECT id, name, description, price FROM products WHERE capacity >= ?"
    params = [people_count]
    if high_groundwater:
        query += " AND high_groundwater = 1"
    if clay_soil:
        query += " AND clay_soil = 1"
    cur.execute(query, params)
    products = cur.fetchall()
    conn.close()
    return products

# ---------- Работа с заявками ----------
def save_lead(user_id, product_id, answers, name, phone):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    answers_json = json.dumps(answers, ensure_ascii=False)
    cur.execute('''
        INSERT INTO leads (user_id, product_id, answers, name, phone, status)
        VALUES (?, ?, ?, ?, ?, 'new')
    ''', (user_id, product_id, answers_json, name, phone))
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()
    return lead_id

def get_user_leads(user_id):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute('''
        SELECT l.id, p.name, l.name, l.phone, l.status, l.created_at
        FROM leads l
        JOIN products p ON l.product_id = p.id
        WHERE l.user_id = ?
        ORDER BY l.created_at DESC
    ''', (user_id,))
    leads = cur.fetchall()
    conn.close()
    return leads

# ========== FSM для квиза ==========
class QuizFSM(StatesGroup):
    people_count = State()        # количество человек
    groundwater = State()         # уровень грунтовых вод (высокий/низкий)
    soil_type = State()           # тип грунта (песок/глина)
    name = State()                # имя клиента
    phone = State()               # телефон

# ========== Клавиатуры ==========
def main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🔍 Подобрать септик"))
    builder.add(KeyboardButton(text="📋 Мои заявки"))
    builder.add(KeyboardButton(text="ℹ️ О септиках"))
    builder.add(KeyboardButton(text="📞 Контакты"))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def cancel_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❌ Отмена"))
    return builder.as_markup(resize_keyboard=True)

def people_count_keyboard():
    builder = InlineKeyboardBuilder()
    options = [("1-3 человека", "1-3"), ("4-6 человек", "4-6"), ("7-10 человек", "7-10")]
    for text, data in options:
        builder.button(text=text, callback_data=f"people_{data}")
    builder.adjust(1)
    return builder.as_markup()

def groundwater_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Низкий (грунтовые воды глубоко)", callback_data="gw_low")
    builder.button(text="Высокий (вода близко к поверхности)", callback_data="gw_high")
    builder.adjust(1)
    return builder.as_markup()

def soil_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Песок / супесь", callback_data="soil_sand")
    builder.button(text="Глина / суглинок", callback_data="soil_clay")
    builder.adjust(1)
    return builder.as_markup()

def product_selection_keyboard(products):
    """Клавиатура с найденными септиками для выбора"""
    builder = InlineKeyboardBuilder()
    for p in products:
        builder.button(text=f"{p[1]} - {p[3]} руб.", callback_data=f"prod_{p[0]}")
    builder.adjust(1)
    return builder.as_markup()

def request_contact_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Отправить номер телефона", request_contact=True))
    builder.add(KeyboardButton(text="❌ Отмена"))
    return builder.as_markup(resize_keyboard=True)

# ========== Функции отправки уведомлений ==========
async def send_email_notification(lead_id, user_info, product_name, answers, name, phone):
    """Отправка заявки на почту"""
    subject = f"Новая заявка №{lead_id} на септик"
    body = f"""
    Поступила новая заявка с сайта (Telegram-бот).

    Заявка №: {lead_id}
    Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}

    Клиент: {name}
    Телефон: {phone}
    Telegram: @{user_info[1]} (ID: {user_info[0]})

    Выбранный септик: {product_name}

    Ответы на вопросы:
    • Количество человек: {answers.get('people_count')}
    • Уровень грунтовых вод: {answers.get('groundwater')}
    • Тип грунта: {answers.get('soil_type')}
    """
    message = EmailMessage()
    message.set_content(body.strip())
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = ADMIN_EMAIL

    try:
        await aiosmtplib.send(
            message,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASSWORD,
            use_tls=False,
            start_tls=True
        )
        logging.info(f"Email sent for lead {lead_id}")
    except Exception as e:
        logging.error(f"Email sending failed: {e}")

async def send_telegram_notification(lead_id, user_info, product_name, answers, name, phone):
    """Отправка заявки в Telegram администратору"""
    text = f"""
🔔 <b>Новая заявка #{lead_id}</b>

👤 <b>Клиент:</b> {name}
📞 <b>Телефон:</b> {phone}
📱 <b>Telegram:</b> @{user_info[1]} (ID: {user_info[0]})

🏷 <b>Выбранный септик:</b> {product_name}

📋 <b>Ответы:</b>
• Людей: {answers.get('people_count')}
• УГВ: {answers.get('groundwater')}
• Грунт: {answers.get('soil_type')}

⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}
    """.strip()
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Telegram notification failed: {e}")

# ========== Хэндлеры ==========
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    register_user(user.id, user.username, user.full_name)
    welcome_text = (
        f"👋 Добро пожаловать, {user.full_name}!\n\n"
        "Я помогу вам подобрать оптимальный септик для вашего участка.\n"
        "Пройдите небольшой опрос, и я предложу подходящие модели.\n"
        "Или вы можете просто посмотреть каталог септиков."
    )
    await message.answer(welcome_text, reply_markup=main_menu_keyboard())

# ----- Квиз -----
@dp.message(F.text == "🔍 Подобрать септик")
async def start_quiz(message: Message, state: FSMContext):
    await state.set_state(QuizFSM.people_count)
    await message.answer("Сколько человек будет постоянно проживать в доме?", reply_markup=people_count_keyboard())

@dp.callback_query(StateFilter(QuizFSM.people_count), F.data.startswith("people_"))
async def quiz_people(callback: CallbackQuery, state: FSMContext):
    people = callback.data.split("_")[1]  # "1-3", "4-6", "7-10"
    await state.update_data(people_count=people)
    await callback.message.edit_text(f"Выбрано: {people} человек.")
    await state.set_state(QuizFSM.groundwater)
    await callback.message.answer("Каков уровень грунтовых вод на участке?", reply_markup=groundwater_keyboard())
    await callback.answer()

@dp.callback_query(StateFilter(QuizFSM.groundwater), F.data.startswith("gw_"))
async def quiz_groundwater(callback: CallbackQuery, state: FSMContext):
    gw = "высокий" if callback.data == "gw_high" else "низкий"
    await state.update_data(groundwater=gw)
    await callback.message.edit_text(f"Уровень грунтовых вод: {gw}.")
    await state.set_state(QuizFSM.soil_type)
    await callback.message.answer("Какой тип грунта преобладает?", reply_markup=soil_keyboard())
    await callback.answer()

@dp.callback_query(StateFilter(QuizFSM.soil_type), F.data.startswith("soil_"))
async def quiz_soil(callback: CallbackQuery, state: FSMContext):
    soil = "глина" if callback.data == "soil_clay" else "песок"
    await state.update_data(soil_type=soil)
    await callback.message.edit_text(f"Тип грунта: {soil}.")
    # Получаем все ответы
    data = await state.get_data()
    people_map = {"1-3": 3, "4-6": 6, "7-10": 10}
    people_count = people_map[data['people_count']]
    high_gw = (data['groundwater'] == "высокий")
    clay = (data['soil_type'] == "глина")
    # Подбираем септики
    products = find_matching_products(people_count, high_gw, clay)
    if not products:
        await callback.message.answer("К сожалению, подходящих септиков не найдено. Попробуйте изменить критерии или свяжитесь с нами.")
        await state.clear()
        await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    # Сохраняем найденные продукты в состоянии (чтобы потом знать, что выбрал пользователь)
    await state.update_data(products=products)
    await state.set_state(QuizFSM.name)  # Переходим к запросу имени
    # Показываем список септиков для выбора
    await callback.message.answer("Вот подходящие модели. Выберите одну:", reply_markup=product_selection_keyboard(products))
    await callback.answer()

@dp.callback_query(StateFilter(QuizFSM.name), F.data.startswith("prod_"))
async def quiz_select_product(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split("_")[1])
    await state.update_data(selected_product=product_id)
    await callback.message.edit_text("Септик выбран. Теперь укажите ваше имя.")
    await callback.message.answer("Введите ваше имя:", reply_markup=cancel_keyboard())
    await callback.answer()

@dp.message(StateFilter(QuizFSM.name), F.text)
async def quiz_get_name(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return
    name = message.text.strip()
    await state.update_data(name=name)
    await state.set_state(QuizFSM.phone)
    # Спрашиваем телефон, предлагаем отправить через кнопку
    await message.answer("Введите ваш номер телефона для связи или нажмите кнопку ниже:", reply_markup=request_contact_keyboard())

@dp.message(StateFilter(QuizFSM.phone), F.contact)
async def quiz_get_phone_contact(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await process_quiz_completion(message, state)

@dp.message(StateFilter(QuizFSM.phone), F.text)
async def quiz_get_phone_text(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())
        return
    phone = message.text.strip()
    await state.update_data(phone=phone)
    await process_quiz_completion(message, state)

async def process_quiz_completion(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    # Сохраняем телефон пользователя в БД, если его нет
    user = get_user(user_id)
    if user and not user[3]:  # если нет телефона
        update_user_phone(user_id, data['phone'])
    # Сохраняем заявку
    answers = {
        "people_count": data['people_count'],
        "groundwater": data['groundwater'],
        "soil_type": data['soil_type']
    }
    product = get_product(data['selected_product'])
    lead_id = save_lead(user_id, data['selected_product'], answers, data['name'], data['phone'])
    # Отправляем уведомления
    user_info = (user_id, message.from_user.username or "нет username")
    asyncio.create_task(send_email_notification(lead_id, user_info, product[1], answers, data['name'], data['phone']))
    asyncio.create_task(send_telegram_notification(lead_id, user_info, product[1], answers, data['name'], data['phone']))
    # Подтверждение пользователю
    await message.answer(
        f"✅ Спасибо, {data['name']}! Ваша заявка принята.\n"
        f"Вы выбрали: {product[1]}\n"
        f"Наш менеджер свяжется с вами в ближайшее время по телефону {data['phone']}.",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

# ----- Мои заявки -----
@dp.message(F.text == "📋 Мои заявки")
async def my_leads(message: Message):
    user_id = message.from_user.id
    leads = get_user_leads(user_id)
    if not leads:
        await message.answer("У вас пока нет заявок.")
        return
    text = "Ваши заявки:\n\n"
    for lead in leads:
        status_emoji = {"new": "🆕", "contacted": "📞", "closed": "✅"}.get(lead[4], "❓")
        text += f"{status_emoji} Заявка №{lead[0]} от {lead[5][:10]} — {lead[1]}\n"
    await message.answer(text)

# ----- О септиках (каталог) -----
@dp.message(F.text == "ℹ️ О септиках")
async def show_products(message: Message):
    products = get_all_products()
    text = "Наши септики:\n\n"
    for p in products:
        text += f"• {p[1]} — {p[3]} руб.\n   {p[2]}\n\n"
    await message.answer(text)

# ----- Контакты -----
@dp.message(F.text == "📞 Контакты")
async def show_contacts(message: Message):
    text = ("📍 Наш офис: г. Москва, ул. Строителей, д. 10\n"
            "📞 Телефон: +7 (495) 123-45-67\n"
            "🌐 Сайт: www.septiki.ru\n"
            "📧 Email: info@septiki.ru")
    await message.answer(text)

# ----- Отмена -----
@dp.message(F.text == "❌ Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu_keyboard())

# ----- Обработка неизвестных сообщений -----
@dp.message()
async def unknown_message(message: Message):
    await message.answer("Извините, я не понимаю. Используйте кнопки меню.")

# ========== Запуск ==========
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

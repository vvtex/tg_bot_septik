import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto
)

# === Конфигурация ===
API_TOKEN = os.getenv('API_TOKEN')
MANAGER_ID = os.getenv('MANAGER_ID')
if MANAGER_ID:
    MANAGER_ID = int(MANAGER_ID)

DATABASE_PATH = 'bot.db'
db_lock = asyncio.Lock()  # Глобальная блокировка для доступа к БД

# === Инициализация БД ===
async def init_db():
    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    phone TEXT,
                    address TEXT,
                    septic_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_interaction TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    utm_source TEXT,
                    utm_medium TEXT,
                    utm_campaign TEXT,
                    utm_term TEXT,
                    utm_content TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    service_type TEXT NOT NULL,
                    address TEXT,
                    septic_type TEXT,
                    status TEXT DEFAULT 'новый',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    assigned_manager INTEGER,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            ''')
            await db.commit()

# === Состояния FSM ===
class SepticTankStates(StatesGroup):
    choosing_service = State()
    entering_address = State()
    entering_septic_type = State()
    confirming_phone = State()
    broadcast_message = State()
    broadcast_confirm = State()

# === Клавиатуры ===
def get_service_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚛 Срочная откачка")],
            [KeyboardButton(text="🔧 Ремонт оборудования")],
            [KeyboardButton(text="🏡 Монтаж нового септика")],
            [KeyboardButton(text="❓ Не знаю, нужна диагностика")]
        ],
        resize_keyboard=True
    )

def get_phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📞 Отправить контакт", request_contact=True)]
        ],
        resize_keyboard=True
    )

def get_menu_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В главное меню", callback_data="menu")]
        ]
    )

def get_after_request_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Оставить ещё заявку", callback_data="menu")]
        ]
    )

# === Вспомогательные функции ===
def parse_utm_start(payload: str) -> dict:
    utm = {}
    if payload:
        parts = payload.split('&')
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                if key.startswith('utm_'):
                    utm[key] = value
    return utm

def validate_phone(phone: str) -> bool:
    digits = re.sub(r'\D', '', phone)
    return len(digits) >= 10

async def notify_manager(bot: Bot, lead_data: dict):
    if not MANAGER_ID:
        return
    text = (
        f"🔔 Новая заявка!\n"
        f"Услуга: {lead_data['service_type']}\n"
        f"Имя: {lead_data['full_name']}\n"
        f"Телефон: {lead_data['phone']}\n"
        f"Адрес: {lead_data['address']}\n"
        f"Марка септика: {lead_data.get('septic_type', 'не указана')}\n"
        f"UTM: {lead_data.get('utm', 'нет')}"
    )
    await bot.send_message(chat_id=MANAGER_ID, text=text)

# === Возврат в меню ===
async def back_to_menu(message: Message, state: FSMContext, edit: bool = False):
    await state.clear()
    await state.set_state(SepticTankStates.choosing_service)
    if edit:
        await message.edit_text(
            "Понимаю, проблемы с септиком — это неприятно. Поможем решить их быстро и с гарантией. 👷‍♂️\n"
            "Выберите, что именно нужно:",
            reply_markup=get_service_keyboard()
        )
    else:
        await message.answer(
            "Понимаю, проблемы с септиком — это неприятно. Поможем решить их быстро и с гарантией. 👷‍♂️\n"
            "Выберите, что именно нужно:",
            reply_markup=get_service_keyboard()
        )
    # Логируем действие (используем блокировку)
    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                (message.from_user.id, 'back_to_menu')
            )
            await db.commit()

# === Роутер и диспетчер ===
router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

# === Обработчики ===
@router.message(Command("menu"))
@router.message(Command("cancel"))
async def cmd_menu(message: Message, state: FSMContext):
    await back_to_menu(message, state)

@router.callback_query(F.data == "menu")
async def callback_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await back_to_menu(callback.message, state, edit=True)

@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    args = message.text.split()
    payload = args[1] if len(args) > 1 else ""
    utm = parse_utm_start(payload)

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('SELECT id FROM users WHERE telegram_id = ?', (message.from_user.id,))
            user = await cursor.fetchone()
            if not user:
                await db.execute('''
                    INSERT INTO users 
                    (telegram_id, username, full_name, utm_source, utm_medium, utm_campaign, utm_term, utm_content)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    message.from_user.id,
                    message.from_user.username,
                    message.from_user.full_name,
                    utm.get('utm_source'),
                    utm.get('utm_medium'),
                    utm.get('utm_campaign'),
                    utm.get('utm_term'),
                    utm.get('utm_content')
                ))
                await db.execute(
                    'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                    (message.from_user.id, 'start_new_user')
                )
            else:
                if utm:
                    await db.execute('''
                        UPDATE users SET
                            utm_source = COALESCE(?, utm_source),
                            utm_medium = COALESCE(?, utm_medium),
                            utm_campaign = COALESCE(?, utm_campaign),
                            utm_term = COALESCE(?, utm_term),
                            utm_content = COALESCE(?, utm_content),
                            last_interaction = CURRENT_TIMESTAMP
                        WHERE telegram_id = ?
                    ''', (
                        utm.get('utm_source'),
                        utm.get('utm_medium'),
                        utm.get('utm_campaign'),
                        utm.get('utm_term'),
                        utm.get('utm_content'),
                        message.from_user.id
                    ))
                await db.execute(
                    'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                    (message.from_user.id, 'start_existing_user')
                )
            await db.commit()

    await message.answer(
        "Понимаю, проблемы с септиком — это неприятно. Поможем решить их быстро и с гарантией. 👷‍♂️\n"
        "Выберите, что именно нужно:",
        reply_markup=get_service_keyboard()
    )
    await state.set_state(SepticTankStates.choosing_service)

@router.message(SepticTankStates.choosing_service, F.text.in_(["🚛 Срочная откачка", "🔧 Ремонт оборудования", "🏡 Монтаж нового септика", "❓ Не знаю, нужна диагностика"]))
async def service_chosen(message: Message, state: FSMContext):
    service = message.text
    await state.update_data(service_type=service)

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                (message.from_user.id, f'chose_service:{service}')
            )
            await db.commit()

    expert_text = (
        "🔍 Как понять, что пора откачивать септик?\n"
        "• Неприятный запах возле люка\n"
        "• Медленный уход воды в раковине/унитазе\n"
        "• Влажная почва вокруг септика\n\n"
        "Если заметили эти признаки, лучше вызвать мастера для профилактики."
    )
    await message.answer(expert_text)

    await message.answer(
        "Укажите, пожалуйста, адрес объекта (город, улица, дом):",
        reply_markup=get_menu_inline_keyboard()
    )
    await state.set_state(SepticTankStates.entering_address)

@router.message(SepticTankStates.entering_address)
async def address_entered(message: Message, state: FSMContext):
    address = message.text
    await state.update_data(address=address)

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                (message.from_user.id, f'entered_address:{address}')
            )
            await db.commit()

    await message.answer(
        "Если знаете марку или тип септика (например, Танк, Топас, Юнилос), напишите. "
        "Если нет, просто нажмите /skip или отправьте прочерк.\n\n"
        "Вы также можете вернуться в меню с помощью кнопки ниже.",
        reply_markup=get_menu_inline_keyboard()
    )
    await state.set_state(SepticTankStates.entering_septic_type)

@router.message(SepticTankStates.entering_septic_type)
async def septic_type_entered(message: Message, state: FSMContext):
    septic = message.text
    if septic and septic not in ['/skip', '-']:
        await state.update_data(septic_type=septic)
        action = f'entered_septic:{septic}'
    else:
        await state.update_data(septic_type=None)
        action = 'skipped_septic'

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                (message.from_user.id, action)
            )
            await db.commit()

    # Демонстрация ценности
    pdf_path = "media/price.pdf"
    if os.path.exists(pdf_path):
        await message.answer_document(FSInputFile(pdf_path), caption="Наш прайс-лист на основные услуги")

    photos = ["media/photo1.jpg", "media/photo2.jpg", "media/photo3.jpg"]
    media_group = []
    for i, photo in enumerate(photos):
        if os.path.exists(photo):
            if i == 0:
                media_group.append(FSInputFile(photo))
            else:
                media_group.append(InputMediaPhoto(media=FSInputFile(photo)))
    if media_group:
        await message.answer_media_group(media_group)

    benefits = (
        "Наши преимущества:\n"
        "✅ Выезд за 1 час\n"
        "✅ Работаем круглосуточно\n"
        "✅ Гарантия до 2 лет\n"
        "✅ Спецтехника и свое оборудование"
    )
    await message.answer(benefits)

    await message.answer(
        "Готовы вызвать мастера для осмотра/откачки? Нажмите кнопку ниже, чтобы отправить контакт.",
        reply_markup=get_phone_keyboard()
    )
    await state.set_state(SepticTankStates.confirming_phone)

@router.message(SepticTankStates.confirming_phone, F.contact)
async def phone_received_contact(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    await process_phone(message, state, phone)

@router.message(SepticTankStates.confirming_phone, F.text)
async def phone_received_text(message: Message, state: FSMContext):
    phone = message.text
    if validate_phone(phone):
        await process_phone(message, state, phone)
    else:
        await message.answer(
            "Номер телефона некорректен. Пожалуйста, введите номер в формате +7XXXXXXXXXX или нажмите кнопку 'Отправить контакт'",
            reply_markup=get_phone_keyboard()
        )

async def process_phone(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    service_type = data.get('service_type')
    address = data.get('address')
    septic_type = data.get('septic_type')

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('SELECT id, utm_source, utm_medium, utm_campaign FROM users WHERE telegram_id = ?', (message.from_user.id,))
            user = await cursor.fetchone()
            if not user:
                # Создаём пользователя, если его нет (на всякий случай)
                await db.execute('''
                    INSERT INTO users (telegram_id, username, full_name, phone, address, septic_type)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (message.from_user.id, message.from_user.username, message.from_user.full_name, phone, address, septic_type))
                await db.commit()
                cursor = await db.execute('SELECT last_insert_rowid()')
                user_id = (await cursor.fetchone())[0]
                utm_source = utm_medium = utm_campaign = None
            else:
                user_id = user[0]
                utm_source = user[1]
                utm_medium = user[2]
                utm_campaign = user[3]
                # Обновляем данные пользователя
                await db.execute('''
                    UPDATE users SET phone = ?, address = ?, septic_type = ?, last_interaction = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (phone, address, septic_type, message.from_user.id))

            # Создаём заявку
            await db.execute('''
                INSERT INTO leads (user_id, service_type, address, septic_type, status)
                VALUES (?, ?, ?, ?, 'новый')
            ''', (user_id, service_type, address, septic_type))
            # Логируем
            await db.execute(
                'INSERT INTO logs (user_id, action) VALUES (?, ?)',
                (message.from_user.id, f'lead_created:{service_type}')
            )
            await db.commit()

    lead_data = {
        'service_type': service_type,
        'full_name': message.from_user.full_name,
        'phone': phone,
        'address': address,
        'septic_type': septic_type,
        'utm': f"source={utm_source}, medium={utm_medium}, campaign={utm_campaign}"
    }
    await notify_manager(message.bot, lead_data)

    await message.answer(
        f"✅ Спасибо, {message.from_user.first_name}! Ваша заявка принята и передана менеджеру.\n"
        f"Данные сохранены в нашей системе. Менеджер свяжется с вами в течение 5-10 минут.",
        reply_markup=get_after_request_inline_keyboard()
    )
    await state.clear()

# === Админские команды (без изменений, но тоже используют db_lock) ===
async def is_admin(message: Message) -> bool:
    return MANAGER_ID is not None and message.from_user.id == MANAGER_ID

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_admin(message):
        return
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('SELECT COUNT(*) FROM users')
            total_users = (await cursor.fetchone())[0]

            cursor = await db.execute(
                'SELECT COUNT(*) FROM leads WHERE date(created_at) = date(?)',
                (today.isoformat(),)
            )
            leads_today = (await cursor.fetchone())[0]

            cursor = await db.execute(
                'SELECT COUNT(*) FROM leads WHERE created_at >= ?',
                (week_ago.isoformat(),)
            )
            leads_week = (await cursor.fetchone())[0]

            cursor = await db.execute('SELECT COUNT(DISTINCT user_id) FROM leads')
            users_with_leads = (await cursor.fetchone())[0]

    text = (
        f"📊 Статистика:\n"
        f"Всего пользователей: {total_users}\n"
        f"Пользователей с заявками: {users_with_leads}\n"
        f"Заявок сегодня: {leads_today}\n"
        f"Заявок за 7 дней: {leads_week}\n"
    )
    await message.answer(text)

@router.message(Command("get_leads"))
async def cmd_get_leads(message: Message):
    if not await is_admin(message):
        return
    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('''
                SELECT id, service_type, status, created_at FROM leads
                ORDER BY created_at DESC LIMIT 10
            ''')
            leads = await cursor.fetchall()

    if not leads:
        await message.answer("Нет заявок.")
        return

    text = "Последние 10 заявок:\n"
    for lead in leads:
        lead_id, service_type, status, created_at = lead
        dt = datetime.fromisoformat(created_at).strftime('%d.%m %H:%M')
        text += f"#{lead_id} {service_type} - {status} ({dt})\n"
    await message.answer(text)

@router.message(Command("lead"))
async def cmd_lead(message: Message, command: CommandObject):
    if not await is_admin(message):
        return
    args = command.args
    if not args:
        await message.answer("Укажите ID заявки: /lead 123")
        return
    try:
        lead_id = int(args.split()[0])
    except:
        await message.answer("Неверный формат ID.")
        return

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('''
                SELECT l.id, l.service_type, l.status, l.created_at, l.address, l.septic_type,
                       u.full_name, u.username, u.phone, u.utm_source, u.utm_medium, u.utm_campaign
                FROM leads l
                JOIN users u ON l.user_id = u.id
                WHERE l.id = ?
            ''', (lead_id,))
            lead = await cursor.fetchone()

    if not lead:
        await message.answer("Заявка не найдена.")
        return

    (lead_id, service_type, status, created_at, address, septic_type,
     full_name, username, phone, utm_source, utm_medium, utm_campaign) = lead

    text = (
        f"📋 Заявка #{lead_id}\n"
        f"Услуга: {service_type}\n"
        f"Статус: {status}\n"
        f"Дата: {created_at}\n"
        f"Клиент: {full_name} (@{username})\n"
        f"Телефон: {phone}\n"
        f"Адрес: {address}\n"
        f"Септик: {septic_type or 'не указан'}\n"
        f"UTM: {utm_source} / {utm_medium} / {utm_campaign}"
    )
    await message.answer(text)

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    await message.answer("Введите сообщение для рассылки (только текст):")
    await state.set_state(SepticTankStates.broadcast_message)

@router.message(SepticTankStates.broadcast_message)
async def broadcast_message_received(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    text = message.text
    await state.update_data(broadcast_text=text)
    await message.answer(
        f"Текст рассылки:\n\n{text}\n\nПодтвердите отправку всем пользователям?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")]
        ])
    )
    await state.set_state(SepticTankStates.broadcast_confirm)

@router.callback_query(SepticTankStates.broadcast_confirm, F.data == "broadcast_confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MANAGER_ID:
        await callback.answer("Нет доступа")
        return
    await callback.answer()
    data = await state.get_data()
    text = data.get('broadcast_text')

    async with db_lock:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            cursor = await db.execute('SELECT telegram_id FROM users')
            user_ids = [row[0] for row in await cursor.fetchall()]

    await callback.message.edit_text(f"Начинаю рассылку {len(user_ids)} пользователям...")

    success = 0
    fail = 0
    for uid in user_ids:
        try:
            await callback.bot.send_message(uid, text)
            success += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    await callback.message.answer(f"Рассылка завершена. Успешно: {success}, ошибок: {fail}")
    await state.clear()

@router.callback_query(SepticTankStates.broadcast_confirm, F.data == "broadcast_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != MANAGER_ID:
        await callback.answer("Нет доступа")
        return
    await callback.answer()
    await callback.message.edit_text("Рассылка отменена.")
    await state.clear()

# === Запуск ===
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    if not API_TOKEN:
        raise ValueError("Не задан API_TOKEN")
    bot = Bot(token=API_TOKEN)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())

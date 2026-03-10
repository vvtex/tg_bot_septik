import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, InputMediaPhoto
)
from dotenv import load_dotenv
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Text, ForeignKey, func, select
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship

# Загрузка переменных окружения
load_dotenv()
API_TOKEN = os.getenv('API_TOKEN')
MANAGER_ID = int(os.getenv('MANAGER_ID', 0))
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite+aiosqlite:///bot.db')

# Настройка базы данных
engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

# Модели данных
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    septic_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_interaction = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    utm_source = Column(String, nullable=True)
    utm_medium = Column(String, nullable=True)
    utm_campaign = Column(String, nullable=True)
    utm_term = Column(String, nullable=True)
    utm_content = Column(String, nullable=True)
    leads = relationship("Lead", back_populates="user")

class Lead(Base):
    __tablename__ = 'leads'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    service_type = Column(String, nullable=False)
    address = Column(String, nullable=True)
    septic_type = Column(String, nullable=True)
    status = Column(String, default='новый')
    created_at = Column(DateTime, default=datetime.utcnow)
    assigned_manager = Column(Integer, nullable=True)
    user = relationship("User", back_populates="leads")

class Log(Base):
    __tablename__ = 'logs'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    action = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Инициализация базы данных
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Состояния FSM
class SepticTankStates(StatesGroup):
    choosing_service = State()
    entering_address = State()
    entering_septic_type = State()
    confirming_phone = State()
    broadcast_message = State()
    broadcast_confirm = State()

# Клавиатуры
service_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚛 Срочная откачка")],
        [KeyboardButton(text="🔧 Ремонт оборудования")],
        [KeyboardButton(text="🏡 Монтаж нового септика")],
        [KeyboardButton(text="❓ Не знаю, нужна диагностика")]
    ],
    resize_keyboard=True
)

phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📞 Отправить контакт", request_contact=True)]
    ],
    resize_keyboard=True
)

# Вспомогательные функции
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

# Создаём роутер и диспетчер
router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

# ----- Обработчики -----

@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    args = message.text.split()
    payload = args[1] if len(args) > 1 else ""
    utm = parse_utm_start(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                utm_source=utm.get('utm_source'),
                utm_medium=utm.get('utm_medium'),
                utm_campaign=utm.get('utm_campaign'),
                utm_term=utm.get('utm_term'),
                utm_content=utm.get('utm_content')
            )
            session.add(user)
            await session.commit()
        else:
            # обновляем UTM, если пришли новые
            if utm.get('utm_source'):
                user.utm_source = utm['utm_source']
            # аналогично для других
            await session.commit()

    await message.answer(
        "Понимаю, проблемы с септиком — это неприятно. Поможем решить их быстро и с гарантией. 👷‍♂️\n"
        "Выберите, что именно нужно:",
        reply_markup=service_keyboard
    )
    await state.set_state(SepticTankStates.choosing_service)

@router.message(SepticTankStates.choosing_service, F.text.in_(["🚛 Срочная откачка", "🔧 Ремонт оборудования", "🏡 Монтаж нового септика", "❓ Не знаю, нужна диагностика"]))
async def service_chosen(message: Message, state: FSMContext):
    service = message.text
    await state.update_data(service_type=service)

    expert_text = (
        "🔍 Как понять, что пора откачивать септик?\n"
        "• Неприятный запах возле люка\n"
        "• Медленный уход воды в раковине/унитазе\n"
        "• Влажная почва вокруг септика\n\n"
        "Если заметили эти признаки, лучше вызвать мастера для профилактики."
    )
    await message.answer(expert_text)

    await message.answer("Укажите, пожалуйста, адрес объекта (город, улица, дом):")
    await state.set_state(SepticTankStates.entering_address)

@router.message(SepticTankStates.entering_address)
async def address_entered(message: Message, state: FSMContext):
    address = message.text
    await state.update_data(address=address)

    await message.answer(
        "Если знаете марку или тип септика (например, Танк, Топас, Юнилос), напишите. "
        "Если нет, просто нажмите /skip или отправьте прочерк."
    )
    await state.set_state(SepticTankStates.entering_septic_type)

@router.message(SepticTankStates.entering_septic_type)
async def septic_type_entered(message: Message, state: FSMContext):
    septic = message.text
    if septic and septic not in ['/skip', '-']:
        await state.update_data(septic_type=septic)
    else:
        await state.update_data(septic_type=None)

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
        "Готовы вызвать мастера для осмотра/откачки?",
        reply_markup=phone_keyboard
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
            reply_markup=phone_keyboard
        )

async def process_phone(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    service_type = data.get('service_type')
    address = data.get('address')
    septic_type = data.get('septic_type')

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one()
        user.phone = phone
        user.address = address
        user.septic_type = septic_type

        lead = Lead(
            user_id=user.id,
            service_type=service_type,
            address=address,
            septic_type=septic_type,
            status='новый'
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    lead_data = {
        'service_type': service_type,
        'full_name': message.from_user.full_name,
        'phone': phone,
        'address': address,
        'septic_type': septic_type,
        'utm': f"source={user.utm_source}, medium={user.utm_medium}, campaign={user.utm_campaign}"
    }
    await notify_manager(message.bot, lead_data)

    await message.answer(
        f"Спасибо, {message.from_user.first_name}! Ваша заявка принята. Менеджер свяжется с вами в течение 5-10 минут.",
        reply_markup=None
    )
    await state.clear()

# ----- Админские команды (доступны только MANAGER_ID) -----
async def is_admin(message: Message) -> bool:
    return message.from_user.id == MANAGER_ID

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not await is_admin(message):
        return
    today = datetime.utcnow().date()
    week_ago = today - timedelta(days=7)

    async with AsyncSessionLocal() as session:
        total_users = await session.scalar(select(func.count(User.id)))
        leads_today = await session.scalar(
            select(func.count(Lead.id)).where(func.date(Lead.created_at) == today)
        )
        leads_week = await session.scalar(
            select(func.count(Lead.id)).where(Lead.created_at >= week_ago)
        )
        users_with_leads = await session.scalar(
            select(func.count(func.distinct(Lead.user_id)))
        )

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
    async with AsyncSessionLocal() as session:
        leads = await session.execute(
            select(Lead).order_by(Lead.created_at.desc()).limit(10)
        )
        leads = leads.scalars().all()

    if not leads:
        await message.answer("Нет заявок.")
        return

    text = "Последние 10 заявок:\n"
    for lead in leads:
        text += f"#{lead.id} {lead.service_type} - {lead.status} ({lead.created_at.strftime('%d.%m %H:%M')})\n"
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

    async with AsyncSessionLocal() as session:
        lead = await session.get(Lead, lead_id)
        if not lead:
            await message.answer("Заявка не найдена.")
            return
        user = await session.get(User, lead.user_id)

    text = (
        f"📋 Заявка #{lead.id}\n"
        f"Услуга: {lead.service_type}\n"
        f"Статус: {lead.status}\n"
        f"Дата: {lead.created_at}\n"
        f"Клиент: {user.full_name} (@{user.username})\n"
        f"Телефон: {user.phone}\n"
        f"Адрес: {lead.address}\n"
        f"Септик: {lead.septic_type or 'не указан'}\n"
        f"UTM: {user.utm_source} / {user.utm_medium} / {user.utm_campaign}"
    )
    await message.answer(text)

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    await message.answer("Введите сообщение для рассылки (можно с медиа, но пока только текст):")
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

    async with AsyncSessionLocal() as session:
        users = await session.execute(select(User.telegram_id))
        user_ids = [row[0] for row in users]

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

# ----- Запуск бота -----
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=API_TOKEN)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())

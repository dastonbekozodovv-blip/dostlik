"""
💖 Telegram "Do'stlik Testi" Boti 💖
🌐 Railway Server uchun maxsus tayyorlangan (FastAPI + Webhook)
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, Dict, List, Tuple

# === AIOGRAM KUTUBXONALARI ===
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, Update, 
    InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command

# === SQLALCHEMY KUTUBXONALARI ===
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey, JSON, select, func
from sqlalchemy.pool import NullPool, StaticPool

# === QO'SHIMCHA KUTUBXONALAR ===
from pydantic_settings import BaseSettings
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from contextlib import asynccontextmanager

# ==========================================
# ⚙️ 1. KONFIGURATSIYA VA SOZLAMALAR
# ==========================================
load_dotenv()

class Settings(BaseSettings):
    """Barcha muhit o'zgaruvchilari (Environment Variables)"""
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", 0))
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "bot_username")
    
    PORT: int = int(os.getenv("PORT", 8000))
    HOST: str = os.getenv("HOST", "0.0.0.0")
    RAILWAY_PUBLIC_DOMAIN: str = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "secret123")
    
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./friendship_bot.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

settings = Settings()

# 📝 Jurnallashtirish (Logging)
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==========================================
# 💾 2. MA'LUMOTLAR BAZASI MODELLARI
# ==========================================
Base = declarative_base()

class User(Base):
    """👤 Foydalanuvchi ma'lumotlari"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=False)
    
    quiz_completed = Column(Boolean, default=False)
    quiz_started_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    quiz_answers = relationship("QuizAnswer", back_populates="user", cascade="all, delete-orphan")
    friendships = relationship("Friendship", foreign_keys="[Friendship.owner_id]", back_populates="owner", cascade="all, delete-orphan")

class Question(Base):
    """❓ Savollar bazasi"""
    __tablename__ = "questions"
    
    id = Column(Integer, primary_key=True)
    question_number = Column(Integer, nullable=False)
    text = Column(String(500), nullable=False)
    options = Column(JSON, nullable=False)
    correct_answer = Column(String(10), nullable=False)

class QuizAnswer(Base):
    """✅ Foydalanuvchi javoblari"""
    __tablename__ = "quiz_answers"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    selected_answer = Column(String(10), nullable=False)
    
    user = relationship("User", back_populates="quiz_answers")

class Friendship(Base):
    """🤝 Do'stlik natijalari"""
    __tablename__ = "friendships"
    
    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.user_id"), nullable=False, index=True)
    friend_id = Column(Integer, nullable=False, index=True)
    compatibility_score = Column(Float, nullable=False)
    tested_at = Column(DateTime, default=datetime.utcnow)
    
    owner = relationship("User", back_populates="friendships", foreign_keys=[owner_id])

class DatabaseManager:
    """🛠 DB Boshqaruvchisi"""
    def __init__(self):
        self.engine = None
        self.async_session_maker = None
    
    async def initialize(self):
        pool_class = StaticPool if "sqlite" in settings.DATABASE_URL else NullPool
        # PostgreSQL dagi postgres:// ni postgresql:// ga o'zgartirish (SQLAlchemy uchun)
        db_url = settings.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://") if settings.DATABASE_URL.startswith("postgres://") else settings.DATABASE_URL
        
        self.engine = create_async_engine(db_url, poolclass=pool_class)
        self.async_session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Ma'lumotlar bazasi tayyor!")

    async def get_session(self) -> AsyncSession:
        return self.async_session_maker()

db_manager = DatabaseManager()

# ==========================================
# 🧩 3. BAZA VA MANTIQ FUNKSIYALARI
# ==========================================
class QuizState(StatesGroup):
    quiz_in_progress = State()
    friend_quiz_in_progress = State()

async def get_or_create_user(user_id: int, first_name: str, username: Optional[str] = None):
    async with await db_manager.get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(user_id=user_id, first_name=first_name, username=username)
            session.add(user)
            await session.commit()
        return user

async def calculate_compatibility(owner_id: int, friend_id: int) -> float:
    async with await db_manager.get_session() as session:
        owner_result = await session.execute(select(QuizAnswer).where(QuizAnswer.user_id == owner_id))
        owner_answers = {qa.question_id: qa.selected_answer for qa in owner_result.scalars()}
        
        friend_result = await session.execute(select(QuizAnswer).where(QuizAnswer.user_id == friend_id))
        friend_answers = {qa.question_id: qa.selected_answer for qa in friend_result.scalars()}
        
        matching = sum(1 for q_id in owner_answers if q_id in friend_answers and owner_answers[q_id] == friend_answers[q_id])
        total = len(owner_answers)
        return (matching / total * 100) if total > 0 else 0

# ==========================================
# 🎨 4. RASM YARATISH (PILLOW)
# ==========================================
def _generate_image_sync(owner_name: str, friend_name: str, score: float) -> str:
    """Sinxron rasm chizish funksiyasi (Bloklanmasligi uchun alohida thread'da chaqiriladi)"""
    try:
        WIDTH, HEIGHT = 800, 600
        image = Image.new('RGB', (WIDTH, HEIGHT), (240, 248, 255))
        draw = ImageDraw.Draw(image)
        
        # Matnlar
        draw.text((WIDTH // 2, 80), "💝 DO'STLIK TESTI NATIJASI 💝", fill=(25, 25, 112), font=None, anchor="mm")
        draw.text((200, 250), f"👤 {owner_name}", fill=(25, 25, 112), font=None, anchor="mm")
        draw.text((600, 250), f"👤 {friend_name}", fill=(25, 25, 112), font=None, anchor="mm")
        
        # Foiz
        draw.text((WIDTH // 2, 250), f"{score:.0f}%", fill=(220, 20, 60), font=None, anchor="mm")
        
        # Izoh
        status = "✨ Ajoyib do'stlar! ✨" if score > 70 else "🌱 Yana ko'proq suhbatlashing! 🌱"
        draw.text((WIDTH // 2, 450), status, fill=(25, 25, 112), font=None, anchor="mm")
        
        image_path = f"result_{datetime.now().timestamp()}.png"
        image.save(image_path)
        return image_path
    except Exception as e:
        logger.error(f"❌ Rasm yaratishda xato: {e}")
        return ""

async def generate_result_image(owner_name: str, friend_name: str, score: float) -> str:
    # Bloklanishni oldini olish uchun to_thread ishlatamiz
    return await asyncio.to_thread(_generate_image_sync, owner_name, friend_name, score)

# ==========================================
# ⌨️ 5. KEYBOARD VA SAVOLLAR
# ==========================================
def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Testni Boshlash", callback_data="start_quiz")],
        [InlineKeyboardButton(text="ℹ️ Qanday ishlaydi?", callback_data="help")],
    ])

def get_quiz_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="A", callback_data=f"ans_{question_id}_A"), InlineKeyboardButton(text="B", callback_data=f"ans_{question_id}_B")],
        [InlineKeyboardButton(text="C", callback_data=f"ans_{question_id}_C"), InlineKeyboardButton(text="D", callback_data=f"ans_{question_id}_D")],
    ])

SAMPLE_QUESTIONS = [
    {"q_num": 1, "text": "🍕 Qaysi taomni ko'proq yoqtirasiz?", "opts": ["A) Osh", "B) Fast-Food", "C) Shashlik", "D) Shirinliklar"]},
    {"q_num": 2, "text": "🏖 Dam olishni qayerda yoqtirasiz?", "opts": ["A) Tog'da", "B) Dengiz bo'yida", "C) Uyda", "D) Xorijda"]},
    {"q_num": 3, "text": "🎵 Qaysi musiqani ko'proq tinglaysiz?", "opts": ["A) Pop", "B) Klassik", "C) Rap", "D) Sokin musiqa"]},
]

async def seed_questions():
    async with await db_manager.get_session() as session:
        if await session.scalar(select(func.count(Question.id))) == 0:
            for q in SAMPLE_QUESTIONS:
                session.add(Question(question_number=q["q_num"], text=q["text"], options=q["opts"], correct_answer="A"))
            await session.commit()
            logger.info("✅ Savollar bazaga yuklandi!")

# ==========================================
# 🤖 6. BOT HANDLERLARI (MANTIQ)
# ==========================================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = await get_or_create_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    
    # Do'stlik linki orqali kirganligini tekshirish
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        owner_id = int(args[1])
        if owner_id != message.from_user.id:
            await state.update_data(owner_id=owner_id)
            await message.answer(
                f"👋 Salom, {user.first_name}!\n\n"
                f"🎯 Sizni do'stingiz testga taklif qildi!\n"
                f"Qani ko'ramiz, uning didini qanchalik yaxshi bilasiz? 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔥 Boshladik!", callback_data="start_friend_quiz")
                ]])
            )
            return

    await message.answer(
        f"👋 Assalomu alaykum, <b>{user.first_name}</b>!\n\n"
        f"💖 <b>Do'stlik Testi</b> botiga xush kelibsiz!\n"
        f"O'z savollaringizga javob bering va do'stlaringizga yuboring. Ko'ramiz kim sizni yaxshiroq bilar ekan! 😜",
        reply_markup=get_main_keyboard()
    )

@router.callback_query(F.data.in_(["start_quiz", "start_friend_quiz"]))
async def start_quiz_handler(call: CallbackQuery, state: FSMContext):
    await call.message.delete()
    
    # Eskidan qolgan javoblarni tozalash
    async with await db_manager.get_session() as session:
        await session.execute(QuizAnswer.__table__.delete().where(QuizAnswer.user_id == call.from_user.id))
        await session.commit()

    questions = await get_all_questions()
    if not questions:
        return await call.answer("Savollar bazasi bo'sh!", show_alert=True)

    # State'ni o'rnatish
    if call.data == "start_friend_quiz":
        await state.set_state(QuizState.friend_quiz_in_progress)
    else:
        await state.set_state(QuizState.quiz_in_progress)

    await state.update_data(current_q_index=0, total_q=len(questions))
    await send_question(call, questions[0], 1, len(questions))

async def send_question(call: CallbackQuery, question: Question, q_num: int, total_q: int):
    opts_text = "\n".join(question.options)
    text = f"❓ <b>Savol {q_num}/{total_q}:</b>\n\n{question.text}\n\n{opts_text}"
    await call.message.answer(text, reply_markup=get_quiz_keyboard(question.id))

async def get_all_questions() -> List[Question]:
    async with await db_manager.get_session() as session:
        return (await session.execute(select(Question).order_by(Question.question_number))).scalars().all()

@router.callback_query(F.data.startswith("ans_"))
async def process_answer(call: CallbackQuery, state: FSMContext):
    _, q_id, answer = call.data.split("_")
    user_id = call.from_user.id
    
    # Javobni saqlash
    async with await db_manager.get_session() as session:
        session.add(QuizAnswer(user_id=user_id, question_id=int(q_id), selected_answer=answer))
        await session.commit()

    data = await state.get_data()
    current_idx = data.get("current_q_index", 0) + 1
    total_q = data.get("total_q", 1)
    questions = await get_all_questions()

    await call.message.edit_reply_markup(reply_markup=None) # Tugmalarni olib tashlash

    if current_idx < total_q:
        await state.update_data(current_q_index=current_idx)
        await send_question(call, questions[current_idx], current_idx + 1, total_q)
    else:
        # 🏁 Test tugadi
        current_state = await state.get_state()
        
        if current_state == QuizState.quiz_in_progress.state:
            # Asosiy egasi tugatdi
            bot_me = await call.bot.get_me()
            link = f"https://t.me/{bot_me.username}?start={user_id}"
            await call.message.answer(
                f"🎉 <b>Ajoyib! Siz barcha savollarga javob berdingiz.</b>\n\n"
                f"Endi do'stlaringizni sinab ko'rish vaqti keldi. Quyidagi linkni ularga yuboring:\n\n"
                f"🔗 <code>{link}</code>"
            )
        else:
            # Do'sti tugatdi
            owner_id = data.get("owner_id")
            score = await calculate_compatibility(owner_id, user_id)
            
            await call.message.answer("⏳ Natijangiz hisoblanmoqda... Rasm tayyorlanyapti 🎨")
            
            # Bazadan ismlarni olish
            async with await db_manager.get_session() as session:
                owner = await session.scalar(select(User).where(User.user_id == owner_id))
            
            owner_name = owner.first_name if owner else "Do'stingiz"
            friend_name = call.from_user.first_name
            
            # Rasm yaratish va yuborish
            img_path = await generate_result_image(owner_name, friend_name, score)
            
            if img_path and os.path.exists(img_path):
                photo = FSInputFile(img_path)
                caption = f"💝 <b>{owner_name}</b> va <b>{friend_name}</b> ning moslik darajasi: <b>{score:.0f}%</b>"
                
                # Do'stga yuborish
                await call.message.answer_photo(photo, caption=caption)
                
                # Egasiga yuborish (Xato bermasligi uchun try-except)
                try:
                    await call.bot.send_photo(chat_id=owner_id, photo=FSInputFile(img_path), caption=f"🔔 Do'stingiz testdan o'tdi!\n\n" + caption)
                except Exception as e:
                    logger.warning(f"Egasiga xabar yuborishda xato: {e}")
                
                os.remove(img_path) # Rasmni o'chirish
            else:
                await call.message.answer(f"💝 Moslik darajangiz: {score:.0f}%")
        
        await state.clear()

@router.callback_query(F.data == "help")
async def help_handler(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "ℹ️ <b>Qanday ishlaydi?</b>\n\n"
        "1️⃣ O'zingiz haqingizdagi savollarga javob berasiz.\n"
        "2️⃣ Bot sizga maxsus havola (link) beradi.\n"
        "3️⃣ Havolani do'stlaringizga yuborasiz.\n"
        "4️⃣ Ular sizning javoblaringizni topishga harakat qilishadi.\n"
        "5️⃣ Bot chiroyli rasm shaklida foizlarni hisoblab beradi! 📊",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Ortga", callback_data="start_quiz")]])
    )

# ==========================================
# 🚀 7. FASTAPI VA RAILWAY UCHUN WEBHOOK
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # App ishga tushganda (Startup)
    logger.info("🚀 Server ishga tushmoqda...")
    await db_manager.initialize()
    await seed_questions()
    
    # Webhook o'rnatish
    webhook_url = f"https://{settings.RAILWAY_PUBLIC_DOMAIN}{settings.WEBHOOK_PATH}"
    logger.info(f"🔗 Webhook URL: {webhook_url}")
    await bot.set_webhook(url=webhook_url, secret_token=settings.WEBHOOK_SECRET)
    
    yield
    
    # App to'xtaganda (Shutdown)
    logger.info("🛑 Server to'xtatilmoqda...")
    await bot.delete_webhook()
    await db_manager.engine.dispose()

app = FastAPI(title="Do'stlik Testi Bot", lifespan=lifespan)
bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

@app.post(settings.WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Telegram'dan kelgan xabarlarni qabul qilish"""
    # Xavfsizlik: Secret tokenni tekshirish
    secret_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_token != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Yaroqsiz Secret Token")

    update_data = await request.json()
    update = Update(**update_data)
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"message": "🤖 Do'stlik Testi Boti onlayn! (Railway)"}

if __name__ == "__main__":
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level=settings.LOG_LEVEL.lower())

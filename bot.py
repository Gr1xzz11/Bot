import asyncio
import logging
import sys
import re
import sqlite3 

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = '8394122518:AAGwqm3gujAyAQH00WFeP1vqh8AMaTqbKL0' 
# 1. Впишите сюда ID всех владельцев
BOT_OWNERS = {
    123456789: "Основной Владелец",  # <--- ID и имя первого владельца
    987654321: "Второстепенный Программист" # <--- ID и имя второго владельца
}

# Глобальный словарь для кэширования админов (чтобы не дергать БД каждый раз)
ADMINS_DB = {}

# Словарь для хранения "активных чатов"
USER_CHAT_MAP = {}

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN)
dp = Dispatcher()


# --- ================================== ---
# ---       БЛОК: БАЗА ДАННЫХ (SQLITE)   ---
# --- ================================== ---

def db_init():
    """Инициализирует базу данных"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_banned INTEGER DEFAULT 0,
                is_blocked_bot INTEGER DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                admin_id INTEGER PRIMARY KEY,
                admin_name TEXT
            )
        """)
        db.commit()

async def db_add_user(user_id: int):
    """Добавляет пользователя в БД при /start"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        cursor.execute(
            "UPDATE users SET is_blocked_bot = 0 WHERE user_id = ?", (user_id,)
        )
        db.commit()

async def db_ban_user(user_id: int, status: bool):
    """Блокирует или разблокирует пользователя (админом)"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if status else 0, user_id)
        )
        db.commit()

async def db_is_user_banned(user_id: int) -> bool:
    """Проверяет, забанен ли юзер админом"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] == 1 if result else False

async def db_set_user_blocked(user_id: int, status: bool):
    """Помечает, что юзер заблокировал бота (при ошибке отправки)"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute(
            "UPDATE users SET is_blocked_bot = ? WHERE user_id = ?", (1 if status else 0, user_id)
        )
        db.commit()

async def db_get_stats():
    """Получает статистику из БД"""
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute("SELECT COUNT(user_id) FROM users")
        total_users = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(user_id) FROM users WHERE is_banned = 1")
        banned_by_admin = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(user_id) FROM users WHERE is_blocked_bot = 1")
        blocked_bot = cursor.fetchone()[0]
        
        return {
            "total": total_users,
            "banned": banned_by_admin,
            "blocked": blocked_bot
        }

async def db_load_admins():
    """Загружает админов из БД в кэш ADMINS_DB"""
    global ADMINS_DB
    ADMINS_DB = {}
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute("SELECT admin_id, admin_name FROM admins")
        rows = cursor.fetchall()
        for row in rows:
            ADMINS_DB[row[0]] = row[1]
    logging.info(f"Загружено админов: {len(ADMINS_DB)}")

async def db_add_admin(admin_id: int, admin_name: str):
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute("INSERT OR REPLACE INTO admins (admin_id, admin_name) VALUES (?, ?)", (admin_id, admin_name))
        db.commit()
    await db_load_admins() # Обновляем кэш

async def db_del_admin(admin_id: int):
    with sqlite3.connect('livegram.db') as db:
        cursor = db.cursor()
        cursor.execute("DELETE FROM admins WHERE admin_id = ?", (admin_id,))
        db.commit()
    await db_load_admins() # Обновляем кэш


# --- ================================== ---
# ---       БЛОК: КЛАВИАТУРЫ           ---
# --- ================================== ---

start_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Выбор админа")]],
    resize_keyboard=True, one_time_keyboard=True
)
in_chat_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Поменять админа")]], resize_keyboard=True
)

def get_admin_inline_kb():
    """Генерирует инлайн-клавиатуру выбора админа из кэша ADMINS_DB"""
    builder = InlineKeyboardBuilder()
    if not ADMINS_DB:
        builder.add(InlineKeyboardButton(text="Нет доступных админов", callback_data="no_admins"))
        return builder.as_markup()
        
    for admin_id, admin_name in ADMINS_DB.items():
        builder.add(InlineKeyboardButton(
            text=admin_name,
            callback_data=f"select_admin_{admin_id}"
        ))
    builder.adjust(1)
    return builder.as_markup()


# --- ================================== ---
# --- БЛОК: ХЭНДЛЕРЫ АДМИНИСТРАТОРА (ВЛАДЕЛЬЦА) ---
# --- ================================== ---

# Команды для ВЛАДЕЛЬЦЕВ БОТА (те, кто в списке BOT_OWNERS)
@dp.message(Command("add_admin"), F.from_user.id.in_(BOT_OWNERS))
async def owner_add_admin(message: Message):
    try:
        _, admin_id, *name_parts = message.text.split()
        admin_name = " ".join(name_parts)
        if not admin_name:
            await message.reply("Ошибка. Формат: /add_admin <ID> <Имя>")
            return
        await db_add_admin(int(admin_id), admin_name)
        await message.reply(f"✅ Админ {admin_name} (ID: {admin_id}) добавлен.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}\nФормат: /add_admin <ID> <Имя>")

@dp.message(Command("del_admin"), F.from_user.id.in_(BOT_OWNERS))
async def owner_del_admin(message: Message):
    try:
        _, admin_id = message.text.split()
        await db_del_admin(int(admin_id))
        await message.reply(f"✅ Админ (ID: {admin_id}) удален.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}\nФормат: /del_admin <ID>")

# Команды для ВСЕХ АДМИНОВ (включая владельцев)
@dp.message(Command("ban"), F.from_user.id.in_(ADMINS_DB.keys()))
async def admin_ban_user(message: Message):
    try:
        _, user_id = message.text.split()
        await db_ban_user(int(user_id), True)
        await message.reply(f"✅ Пользователь (ID: {user_id}) забанен.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}\nФормат: /ban <ID>")

@dp.message(Command("unban"), F.from_user.id.in_(ADMINS_DB.keys()))
async def admin_unban_user(message: Message):
    try:
        _, user_id = message.text.split()
        await db_ban_user(int(user_id), False)
        await message.reply(f"✅ Пользователь (ID: {user_id}) разбанен.")
    except Exception as e:
        await message.reply(f"Ошибка: {e}\nФормат: /unban <ID>")

@dp.message(Command("stats"), F.from_user.id.in_(ADMINS_DB.keys()))
async def admin_show_stats(message: Message):
    stats = await db_get_stats()
    text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 **Всего нажали /start:** {stats['total']}\n"
        f"🚫 **Забанено админами:** {stats['banned']}\n"
        f"❌ **Заблокировали бота:** {stats['blocked']} (Обновляется при попытке ответа)"
    )
    await message.answer(text, parse_mode="Markdown")


# --- ================================== ---
# ---       БЛОК: ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ  ---
# --- ================================== ---

# Универсальная проверка на бан
async def check_ban(message: Message | CallbackQuery) -> bool:
    """Проверяет, забанен ли юзер. True - забанен, False - нет."""
    user_id = message.from_user.id
    if await db_is_user_banned(user_id):
        if isinstance(message, Message):
            await message.answer("Вы были заблокированы администрацией.")
        elif isinstance(message, CallbackQuery):
            await message.answer("Вы были заблокированы администрацией.", show_alert=True)
        return True
    return False

@dp.message(CommandStart())
async def send_welcome(message: Message):
    user_id = message.from_user.id
    await db_add_user(user_id) 
    
    if await check_ban(message): return

    await message.answer(
        ( # <-- Добавлена открывающая скобка
        "Здравствуйте! 👋\n\n"
        "Это бот обратной связи. Нажмите кнопку 'Выбор админа', чтобы начать.\n\n"
        "Разработчик бота: Админ Крис (ручками своими делал)"
        ), # <-- Добавлена закрывающая скобка и запятая перед следующим аргументом
        reply_markup=start_kb
    )
    
    if user_id in USER_CHAT_MAP:
        del USER_CHAT_MAP[user_id]
@dp.message(F.text == "Выбор админа")
async def show_admin_choice(message: Message):
    if await check_ban(message): return
    await message.answer(
        "Выберите, кому вы хотите задать вопрос:",
        reply_markup=get_admin_inline_kb()
    )

@dp.message(F.text == "Поменять админа")
async def change_admin_handler(message: Message):
    if await check_ban(message): return
    
    if message.from_user.id in USER_CHAT_MAP:
        del USER_CHAT_MAP[message.from_user.id]
        
    await message.answer(
        "Вы завершили диалог. Кому теперь хотите написать?",
        reply_markup=get_admin_inline_kb()
    )
    await message.answer("Или нажмите /start", reply_markup=start_kb)

@dp.callback_query(F.data.startswith("select_admin_"))
async def admin_selected(callback: CallbackQuery):
    if await check_ban(callback): return
    
    try:
        admin_id = int(callback.data.split("_")[-1])
        if admin_id not in ADMINS_DB:
            await callback.answer("Ошибка: Админ не найден (возможно, удален).", show_alert=True)
            await callback.message.edit_text("Попробуйте выбрать другого админа:", reply_markup=get_admin_inline_kb())
            return

        user_id = callback.from_user.id
        USER_CHAT_MAP[user_id] = admin_id
        admin_name = ADMINS_DB[admin_id]
        
        await callback.message.edit_text(
            f"Вы подключены к: **{admin_name}**.\n\n"
            "Можете отправлять ваше сообщение.", parse_mode="Markdown"
        )
        await callback.message.answer(
            "Чтобы сменить админа, нажмите кнопку внизу.",
            reply_markup=in_chat_kb
        )
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Ошибка в admin_selected: {e}")
        await callback.answer("Произошла ошибка. Попробуйте /start", show_alert=True)

@dp.message(F.chat.type == "private", 
            ~F.text.startswith('/'), 
            F.text.not_in({"Выбор админа", "Поменять админа"}))
async def user_message_to_admin(message: Message):
    if await check_ban(message): return
    
    user_id = message.from_user.id
    if user_id not in USER_CHAT_MAP:
        await message.answer("Пожалуйста, сначала выберите админа.", reply_markup=start_kb)
        return
        
    admin_id = USER_CHAT_MAP[user_id]
    user_info = f"📩 Сообщение от {message.from_user.full_name} (ID: {user_id})"
    
    try:
        await message.copy_to(
            chat_id=admin_id,
            caption=f"{user_info}\n\n{message.caption or ''}",
            parse_mode="Markdown" 
        )
        if message.text:
             await bot.send_message(
                 admin_id, f"{user_info}\n\n{message.text}", parse_mode="Markdown"
             )
        
    except TelegramForbiddenError:
        logging.warning(f"Админ {admin_id} заблокировал бота. Удаляем его.")
        await db_del_admin(admin_id)
        await message.answer("❗️Не удалось отправить. Админ больше недоступен. "
                             "Попробуйте /start и выберите другого админа.")
        if user_id in USER_CHAT_MAP:
            del USER_CHAT_MAP[user_id]
    except Exception as e:
        logging.error(f"Ошибка при пересылке админу {admin_id}: {e}")
        await message.answer("❗️Не удалось отправить. Попробуйте /start и выберите другого админа.")
        if user_id in USER_CHAT_MAP:
            del USER_CHAT_MAP[user_id]

# --- ================================== ---
# ---    БЛОК: ХЭНДЛЕР АДМИНА (ОТВЕТЫ)   ---
# --- ================================== ---

USER_ID_PATTERN = re.compile(r"\(ID: (\d+)\)")

@dp.message(F.chat.type == "private", F.from_user.id.in_(ADMINS_DB.keys()), F.reply_to_message)
async def admin_reply_to_user(message: Message):
    admin_id = message.from_user.id
    original_message = message.reply_to_message
    
    text_to_parse = original_message.text or original_message.caption
    if not text_to_parse:
         await message.reply("⚠️ Ошибка: Не могу найти ID (пустое сообщение). Отвечайте на текст.")
         return
    
    match = USER_ID_PATTERN.search(text_to_parse)
    if not match:
        await message.reply("⚠️ Ошибка: Не могу найти ID. Отвечайте (Reply) на сообщения от бота.")
        return
        
    user_id = int(match.group(1))
    
    if await db_is_user_banned(user_id):
        await message.reply(f"⚠️ Ошибка: Пользователь {user_id} забанен. Вы не можете ему ответить. "
                            f"Чтобы разбанить, используйте /unban {user_id}")
        return
        
    try:
        # Получаем имя админа из кэша, если нет - "Администратор"
        admin_name = ADMINS_DB.get(admin_id, "Администратор") 
        
        await message.copy_to(
            chat_id=user_id,
            caption=f"Ответ от {admin_name}:\n\n{message.caption or ''}",
            parse_mode="Markdown"
        )
        if message.text:
             await bot.send_message(
                 user_id,
                 f"Ответ от **{admin_name}**:\n\n{message.text}",
                 parse_mode="Markdown"
             )
        
    except TelegramForbiddenError:
        logging.info(f"Пользователь {user_id} заблокировал бота. Помечаем в БД.")
        await db_set_user_blocked(user_id, True)
        await message.reply(f"❗️Не удалось отправить. Пользователь {user_id} заблокировал бота.")
    except Exception as e:
        logging.error(f"Ошибка при ответе пользователю {user_id}: {e}")
        await message.reply(f"❗️Не удалось отправить ответ. {e}")


# --- ================================== ---
# ---       БЛОК: ЗАПУСК БОТА          ---
# --- ================================== ---

async def main():
    db_init()
    await db_load_admins()
    
# ✅ ИСПРАВЛЕННЫЙ БЛОК (Использует owner_name из цикла)
    for owner_id, owner_name in BOT_OWNERS.items():
        if owner_id not in ADMINS_DB:
            logging.info(f"Владелец {owner_id} ({owner_name}) не найден в админах. Добавляю...")
            # Исправлено: используем owner_name
            await db_add_admin(owner_id, owner_name)
    
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен.")

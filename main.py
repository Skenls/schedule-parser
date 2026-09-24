import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai
from google.genai import types as genai_types
from telethon import TelegramClient, events

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация клиентов
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=config.GEMINI_API_KEY)
telethon_client = TelegramClient('channel_listener', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

TZ = ZoneInfo(config.TIMEZONE)

# Сетка звонков: (конец_урока_ч, конец_урока_м, время_уведомления_ч, время_уведомления_м)
BELL_SCHEDULE = {
    "weekday": {
        1: ((8, 45), (8, 30)),
        2: ((9, 40), (9, 25)),
        3: ((10, 35), (10, 20)),
        4: ((11, 30), (11, 15)),
        5: ((12, 25), (12, 10)),
        6: ((13, 30), (13, 15)),
        7: ((14, 25), (14, 10)),
        8: ((15, 20), (15, 5)),
    },
    "saturday": {
        1: ((8, 45), (8, 30)),
        2: ((9, 40), (9, 25)),
        3: ((10, 35), (10, 20)),
        4: ((11, 30), (11, 15)),
        5: ((12, 25), (12, 10)),
        6: ((13, 20), (13, 5)),
        7: ((14, 15), (14, 0)),
        8: ((15, 10), (14, 55)),
    }
}

# --- ИНИЦИАЛИЗАЦИЯ И РАБОТА С БД ---

async def init_db():
    async with aiosqlite.connect("schedule.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                subgroup INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                date TEXT,
                lesson_num INTEGER,
                subgroup INTEGER,
                subject TEXT,
                auditorium TEXT,
                teacher TEXT,
                PRIMARY KEY (date, lesson_num, subgroup)
            )
        """)
        await db.commit()


async def has_schedule_for_date(target_date: str) -> bool:
    """Проверяет, есть ли в базе хотя бы один урок на указанную дату."""
    async with aiosqlite.connect("schedule.db") as db:
        async with db.execute("SELECT COUNT(*) FROM schedule WHERE date = ?", (target_date,)) as cursor:
            count = (await cursor.fetchone())[0]
            return count > 0


async def cleanup_past_schedule(today_str: str):
    """Удаляет из базы расписание за прошедшие дни (date < today)."""
    async with aiosqlite.connect("schedule.db") as db:
        await db.execute("DELETE FROM schedule WHERE date < ?", (today_str,))
        await db.commit()
    logger.info(f"Очистка БД: удалены прошедшие дни до {today_str}.")


async def save_schedule_for_date(target_date: str, lessons: list[dict]):
    """Перезаписывает расписание на конкретную дату."""
    async with aiosqlite.connect("schedule.db") as db:
        await db.execute("DELETE FROM schedule WHERE date = ?", (target_date,))
        for item in lessons:
            await db.execute("""
                INSERT INTO schedule (date, lesson_num, subgroup, subject, auditorium, teacher)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                target_date,
                item["lesson_num"],
                item["subgroup"],
                item["subject"],
                str(item.get("auditorium", "")),
                str(item.get("teacher", ""))
            ))
        await db.commit()
    logger.info(f"✅ Расписание на {target_date} успешно сохранено/перезаписано в БД.")


# --- РАСПОЗНАВАНИЕ ЧЕРЕЗ GEMINI (С RETRY И FALLBACK) ---

def parse_image_with_gemini(image_bytes: bytes, target_group: str, fallback_date: str | None = None) -> dict:
    prompt = f"""
    Проанализируй фото расписания колледжа.
    1. Найди дату в заголовке листа (например: '07.09.26г.'). 
       Преобразуй её в ISO формат: 'YYYY-MM-DD'. Если дата не видна или обрезана, используй подсказку: '{fallback_date or "null"}'.
    2. Проверь наличие группы '{target_group}'. Если группы нет, верни group_found: false.
    3. Если группа найдена, собери уроки:
       - lesson_num: номер пары (1-8).
       - subgroup: 0 (вся группа), 1 (подгруппа 1), 2 (подгруппа 2). Если на один урок 2 записи — делай 1 и 2.
       - subject: предмет.
       - auditorium: номер кабинета (строка).
       - teacher: преподаватель.

    Ответ СТРОГО валидным JSON:
    {{
        "date": "YYYY-MM-DD" или null,
        "group_found": true/false,
        "lessons": [
            {{
                "lesson_num": 1,
                "subgroup": 0,
                "subject": "Название",
                "auditorium": "304",
                "teacher": "Иванов"
            }}
        ]
    }}
    """
    models_to_try = ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-3.5-flash-lite']

    for model_name in models_to_try:
        for attempt in range(2):  # Делаем 2 попытки на модель при сетевых сбоях
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=[
                        genai_types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                        prompt
                    ],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )
                return json.loads(response.text)
            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    logger.warning(f"Сервер перегружен (503) на {model_name} (попытка {attempt+1}/2). Ждем 3 сек...")
                    time.sleep(3)
                    continue
                elif "429" in err_str:
                    logger.warning(f"Лимит 429 на {model_name}. Пробуем следующую модель...")
                    time.sleep(2)
                    break  # Выходим на следующую модель из списка
                else:
                    logger.error(f"Ошибка {model_name}: {e}")
                    break  # Переходим к следующей модели

    return {}


async def process_photo_message(msg, today_str: str) -> str | None:
    """Скачивает фото и сохраняет расписание при обнаружении группы."""
    if not msg.photo:
        return None

    img_data = await msg.download_media(file=bytes)
    result = await asyncio.to_thread(parse_image_with_gemini, img_data, config.TARGET_GROUP)

    doc_date = result.get("date")
    if not doc_date:
        return None

    if result.get("group_found") and result.get("lessons"):
        await save_schedule_for_date(doc_date, result["lessons"])
        return doc_date

    return None


# --- СИНХРОНИЗАЦИЯ РАСПИСАНИЯ ---

async def sync_schedule_if_needed():
    """Синхронизирует расписание при старте и в 06:00."""
    now = datetime.now(TZ)
    if now.weekday() == 6:
        return

    today_str = now.strftime("%Y-%m-%d")

    # 1. Очищаем старые дни
    await cleanup_past_schedule(today_str)

    # 2. Проверяем наличие расписания на сегодня
    if await has_schedule_for_date(today_str):
        logger.info(f"Расписание на сегодня ({today_str}) уже есть в базе. Пропуск поиска.")
        return

    logger.info(f"Расписания на сегодня ({today_str}) нет в базе. Ищем в канале...")
    target_chat = getattr(config, 'CHANNEL_TARGET', getattr(config, 'CHANNEL_USERNAME', None))
    max_history = 15

    async for msg in telethon_client.iter_messages(target_chat, limit=max_history):
        if not msg.photo:
            continue

        logger.info(f"Скачиваем фото из поста ID: {msg.id}...")
        parsed_date = await process_photo_message(msg, today_str)
        await asyncio.sleep(3)

        if await has_schedule_for_date(today_str):
            logger.info(f"🎉 Расписание на сегодня ({today_str}) успешно найдено и загружено!")
            break

        if parsed_date and parsed_date > today_str:
            logger.info(f"Сохранен лист на будущее ({parsed_date}). Продолжаем поиск сегодняшнего...")


# --- СЛУШАТЕЛЬ КАНАЛА TELEGRAM ---

@telethon_client.on(events.NewMessage())
async def handle_channel_post(event):
    chat = await event.get_chat()
    target = getattr(config, 'CHANNEL_TARGET', getattr(config, 'CHANNEL_USERNAME', None))

    is_target_channel = False
    if isinstance(target, str):
        if (getattr(chat, 'username', '') or '').lower() == target.lower().lstrip('@'):
            is_target_channel = True
    elif isinstance(target, int) and event.chat_id == target:
        is_target_channel = True

    if not is_target_channel or not event.photo:
        return

    logger.info("В канале появился новый пост с фото! Запускаем обработку...")
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")

    parsed_date = await process_photo_message(event.message, today_str)
    if parsed_date:
        logger.info(f"Пост успешно обработан. Данные на {parsed_date} обновлены.")


# --- УВЕДОМЛЕНИЯ И ПЛАНИРОВЩИК ---

async def send_lesson_alert(today_str: str, next_lesson_num: int, break_start_str: str, target_user_id: int | None = None):
    """Отправляет уведомление всем (по расписанию) или конкретному пользователю (/test)."""
    async with aiosqlite.connect("schedule.db") as db:
        async with db.execute(
            "SELECT subgroup, subject, auditorium FROM schedule WHERE date = ? AND lesson_num = ?",
            (today_str, next_lesson_num)
        ) as cursor:
            next_lessons = await cursor.fetchall()

        if target_user_id:
            async with db.execute("SELECT user_id, subgroup FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
                users = await cursor.fetchall()
        else:
            async with db.execute("SELECT user_id, subgroup FROM users") as cursor:
                users = await cursor.fetchall()

    for user_id, user_sub in users:
        matched = []
        if next_lessons:
            for sub, subj, aud in next_lessons:
                if user_sub == 0 or sub == 0 or sub == user_sub:
                    matched.append((sub, subj, aud))

        if not next_lessons or not matched:
            text = (
                f"🔔 В **{break_start_str}** начинается перемена!\n\n"
                f"Следующего ({next_lesson_num}) урока у вас нет — можно отдыхать."
            )
        else:
            text = f"⏳ В **{break_start_str}** начинается перемена!\n\n📌 **Следующий урок ({next_lesson_num}):**\n"
            for sub, subj, aud in matched:
                sub_label = f" (Подгруппа {sub})" if sub > 0 else ""
                room = f"каб. {aud}" if aud else "кабинет не указан"
                text += f"• **{subj}**{sub_label} — 🚪 {room}\n"

        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление {user_id}: {e}")


async def notification_loop():
    last_6am_check_date = None

    while True:
        now = datetime.now(TZ)
        today_str = now.strftime("%Y-%m-%d")
        weekday = now.weekday()

        # Ежедневная проверка в 06:00 утра
        if now.hour == 6 and now.minute == 0 and last_6am_check_date != today_str:
            last_6am_check_date = today_str
            logger.info("⏰ 06:00 утра: выполняем плановую проверку расписания на день...")
            try:
                await sync_schedule_if_needed()
            except Exception as e:
                logger.error(f"Ошибка утренней синхронизации: {e}")

        # Проверка звонков (Пн-Сб)
        if weekday != 6:
            day_key = "saturday" if weekday == 5 else "weekday"
            schedule_grid = BELL_SCHEDULE[day_key]

            for lesson_num, (break_start_time, notify_time) in schedule_grid.items():
                if now.hour == notify_time[0] and now.minute == notify_time[1]:
                    break_str = f"{break_start_time[0]:02d}:{break_start_time[1]:02d}"
                    next_lesson_num = lesson_num + 1
                    await send_lesson_alert(today_str, next_lesson_num, break_str)

        await asyncio.sleep(60 - datetime.now(TZ).second)


# --- AIOGRAM: КОМАНДЫ БОТА ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="1 подгруппа", callback_data="set_sub_1")
    builder.button(text="2 подгруппа", callback_data="set_sub_2")
    builder.button(text="Вся группа", callback_data="set_sub_0")
    builder.adjust(2, 1)

    await message.answer(
        f"Привет! Я отслеживаю расписание для группы **{config.TARGET_GROUP}**.\n"
        "Выбери свою подгруппу, чтобы получать точные кабинеты за 15 минут до перемены:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("set_sub_"))
async def set_subgroup(callback: types.CallbackQuery):
    sub_val = int(callback.data.split("_")[-1])
    async with aiosqlite.connect("schedule.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, subgroup) VALUES (?, ?)",
            (callback.from_user.id, sub_val)
        )
        await db.commit()

    sub_title = "Обе подгруппы" if sub_val == 0 else f"{sub_val}-я подгруппа"
    await callback.message.edit_text(f"✅ Настройки сохранены! Выбрана: **{sub_title}**.")


@dp.message(Command("today"))
async def cmd_today(message: types.Message):
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")

    async with aiosqlite.connect("schedule.db") as db:
        async with db.execute("SELECT subgroup FROM users WHERE user_id = ?", (message.from_user.id,)) as c:
            row = await c.fetchone()
            user_sub = row[0] if row else 0

        async with db.execute(
            "SELECT lesson_num, subgroup, subject, auditorium FROM schedule WHERE date = ? ORDER BY lesson_num, subgroup",
            (today_str,)
        ) as c:
            rows = await c.fetchall()

    if not rows:
        await message.answer(f"📅 На сегодня ({today_str}) расписание в базе не найдено.")
        return

    sub_label = "Вся группа" if user_sub == 0 else f"{user_sub}-я подгруппа"
    text = f"📅 **Расписание на сегодня ({today_str})**\nПрофиль: **{sub_label}**\n\n"

    for l_num, sub, subj, aud in rows:
        if user_sub == 0 or sub == 0 or sub == user_sub:
            sub_info = f" _(подгр. {sub})_" if sub > 0 else ""
            room = f"каб. **{aud}**" if aud else "каб. не указан"
            text += f"• **{l_num} пара:** {subj}{sub_info} — {room}\n"

    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("test"))
async def cmd_test(message: types.Message):
    """Отправляет тестовое уведомление ТОЛЬКО вызвавшему команду."""
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    await send_lesson_alert(
        today_str=today_str,
        next_lesson_num=2,
        break_start_str="09:40",
        target_user_id=message.from_user.id
    )
    await message.answer("Тестовое уведомление отправлено.")


# --- СТАРТ ВСЕХ СЕРВИСОВ ---

async def main():
    await init_db()
    logger.info("База данных инициализирована.")

    print("\n--- АВТОРИЗАЦИЯ TELETHON ---")
    await telethon_client.start()
    logger.info("Telethon подключен.")

    # Проверка базы при запуске бота
    await sync_schedule_if_needed()

    await asyncio.gather(
        telethon_client.run_until_disconnected(),
        dp.start_polling(bot),
        notification_loop()
    )


if __name__ == "__main__":
    asyncio.run(main())
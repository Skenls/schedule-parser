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

# Подавляем избыточные логи сторонних библиотек
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Инициализация клиентов
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=config.GEMINI_API_KEY)
telethon_client = TelegramClient('channel_listener', config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

TZ = ZoneInfo(config.TIMEZONE)
parse_lock = asyncio.Lock()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором."""
    admin_ids = getattr(config, 'ADMIN_IDS', [])
    if isinstance(admin_ids, int):
        return user_id == admin_ids
    if isinstance(admin_ids, (list, tuple, set)):
        return user_id in admin_ids
    if isinstance(admin_ids, str):
        try:
            return user_id in [int(x.strip()) for x in admin_ids.split(",") if x.strip()]
        except ValueError:
            return False
    return False

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


def normalize_lessons(lessons: list[dict]) -> list[dict]:
    """
    Нормализует список уроков от Gemini:
    - Проверяет корректность lesson_num и subgroup.
    - Если для одного lesson_num две записи с subgroup=0, переводит их в subgroup 1 и 2.
    - Объединяет дублирующиеся записи с одинаковыми (lesson_num, subgroup).
    """
    if not lessons:
        return []

    by_lesson: dict[int, list[dict]] = {}
    for item in lessons:
        if not isinstance(item, dict):
            continue
        try:
            l_num = int(item.get("lesson_num", 0))
        except (ValueError, TypeError):
            continue
        if l_num < 1 or l_num > 10:
            continue
        try:
            sub = int(item.get("subgroup", 0))
        except (ValueError, TypeError):
            sub = 0

        subject = str(item.get("subject", "")).strip()
        if not subject:
            continue
        auditorium = str(item.get("auditorium", "")).strip()
        teacher = str(item.get("teacher", "")).strip()

        cleaned_item = {
            "lesson_num": l_num,
            "subgroup": sub,
            "subject": subject,
            "auditorium": auditorium,
            "teacher": teacher
        }
        by_lesson.setdefault(l_num, []).append(cleaned_item)

    normalized: list[dict] = []
    for l_num, items in sorted(by_lesson.items()):
        zeros = [it for it in items if it["subgroup"] == 0]
        non_zeros = [it for it in items if it["subgroup"] != 0]

        if len(zeros) == 2 and not non_zeros:
            zeros[0]["subgroup"] = 1
            zeros[1]["subgroup"] = 2

        merged_by_sub: dict[int, dict] = {}
        for it in items:
            sub = it["subgroup"]
            if sub not in merged_by_sub:
                merged_by_sub[sub] = dict(it)
            else:
                existing = merged_by_sub[sub]
                if it["subject"] and it["subject"] not in existing["subject"]:
                    existing["subject"] = f"{existing['subject']} / {it['subject']}"
                if it["auditorium"] and it["auditorium"] not in existing["auditorium"]:
                    existing["auditorium"] = f"{existing['auditorium']} / {it['auditorium']}" if existing["auditorium"] else it["auditorium"]
                if it["teacher"] and it["teacher"] not in existing["teacher"]:
                    existing["teacher"] = f"{existing['teacher']}, {it['teacher']}" if existing["teacher"] else it["teacher"]

        normalized.extend(merged_by_sub.values())

    return normalized


async def save_schedule_for_date(target_date: str, lessons: list[dict]):
    """Перезаписывает расписание на конкретную дату с защитой от дубликатов."""
    clean_lessons = normalize_lessons(lessons)
    if not clean_lessons:
        logger.warning(f"Нет валидных уроков для сохранения на {target_date}.")
        return

    async with aiosqlite.connect("schedule.db") as db:
        await db.execute("DELETE FROM schedule WHERE date = ?", (target_date,))
        for item in clean_lessons:
            await db.execute("""
                INSERT OR REPLACE INTO schedule (date, lesson_num, subgroup, subject, auditorium, teacher)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                target_date,
                item["lesson_num"],
                item["subgroup"],
                item["subject"],
                item["auditorium"],
                item["teacher"]
            ))
        await db.commit()
    logger.info(f"✅ Расписание на {target_date} успешно сохранено/перезаписано в БД ({len(clean_lessons)} уроков).")


# --- РАСПОЗНАВАНИЕ ЧЕРЕЗ GEMINI (С RETRY И FALLBACK) ---

def parse_image_with_gemini(image_bytes: bytes, target_group: str, fallback_date: str | None = None) -> dict:
    prompt = f"""
    Проанализируй фото расписания колледжа.
    1. Найди дату в заголовке листа (например: '07.09.26г.'). 
       Преобразуй её в ISO формат: 'YYYY-MM-DD'. Если дата не видна или обрезана, используй подсказку: '{fallback_date or "null"}'.
    2. Проверь наличие группы '{target_group}'. Если группы нет, верни group_found: false.
    3. Если группа найдена, собери уроки:
       - lesson_num: номер пары (1-8).
       - subgroup: 0 (вся группа), 1 (подгруппа 1), 2 (подгруппа 2).
       - subject: предмет.
       - auditorium: номер кабинета (строка).
       - teacher: преподаватель.

    ВАЖНО:
    - Пара (lesson_num, subgroup) должна быть УНИКАЛЬНОЙ.
    - Если на одну пару приходится 2 записи (разные дисциплины или подгруппы), ОБЯЗАТЕЛЬНО укажи для одной subgroup: 1, а для другой subgroup: 2.
    - Не дублируй номер пары с одной и той же подгруппой.

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
                text = response.text
                if not text:
                    logger.warning(f"Пустой ответ от {model_name}.")
                    continue
                text = text.strip()
                if text.startswith("```"):
                    lines = text.splitlines()
                    if lines and lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    text = "\n".join(lines).strip()
                return json.loads(text)
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


async def process_photo_message(msg, fallback_date: str | None = None) -> str | None:
    """Скачивает фото и сохраняет расписание при обнаружении группы."""
    if not msg.photo:
        return None

    img_data = await msg.download_media(file=bytes)
    result = await asyncio.to_thread(parse_image_with_gemini, img_data, config.TARGET_GROUP, fallback_date)

    doc_date = result.get("date")
    if not doc_date or not isinstance(doc_date, str):
        return None

    doc_date = doc_date.strip()

    if result.get("group_found") and result.get("lessons"):
        await save_schedule_for_date(doc_date, result["lessons"])
        return doc_date

    return None


# --- СИНХРОНИЗАЦИЯ РАСПИСАНИЯ ---

async def sync_schedule_if_needed(force: bool = False) -> list[str]:
    """
    Синхронизирует расписание при старте, в 06:00 или по вызову /parse.
    Возвращает список сохраненных/обновленных дат.
    """
    async with parse_lock:
        now = datetime.now(TZ)
        if not force and now.weekday() == 6:
            return []

        today_str = now.strftime("%Y-%m-%d")

        # 1. Очищаем старые дни
        await cleanup_past_schedule(today_str)

        # 2. Проверяем наличие расписания на сегодня (если не force)
        if not force and await has_schedule_for_date(today_str):
            logger.info(f"Расписание на сегодня ({today_str}) уже есть в базе. Пропуск поиска.")
            return []

        logger.info(f"Ищем расписание в канале (force={force})...")
        target_chat = getattr(config, 'CHANNEL_TARGET', None) or getattr(config, 'CHANNEL_USERNAME', None)
        max_history = 15
        updated_dates: list[str] = []

        async for msg in telethon_client.iter_messages(target_chat, limit=max_history):
            if not msg.photo:
                continue

            logger.info(f"Скачиваем фото из поста ID: {msg.id}...")
            parsed_date = await process_photo_message(msg, today_str)
            await asyncio.sleep(2)

            if parsed_date:
                if parsed_date not in updated_dates:
                    updated_dates.append(parsed_date)

                if parsed_date == today_str and not force:
                    logger.info(f"🎉 Расписание на сегодня ({today_str}) успешно найдено и загружено!")
                    break

                if parsed_date > today_str and not force:
                    logger.info(f"Сохранен лист на будущее ({parsed_date}). Продолжаем поиск сегодняшнего...")

        return updated_dates


# --- СЛУШАТЕЛЬ КАНАЛА TELEGRAM ---

@telethon_client.on(events.NewMessage())
async def handle_channel_post(event):
    if not event.photo:
        return

    target_id = config.CHANNEL_USERNAME if isinstance(getattr(config, 'CHANNEL_USERNAME', None), int) else None
    target_name = getattr(config, 'CHANNEL_TARGET', None)

    is_target_channel = False
    if target_id and event.chat_id == target_id:
        is_target_channel = True
    elif target_name:
        try:
            chat = await event.get_chat()
            if (getattr(chat, 'username', '') or '').lower() == str(target_name).lower().lstrip('@'):
                is_target_channel = True
        except Exception:
            pass

    if not is_target_channel:
        return

    logger.info("В целевом канале появился новый пост с фото! Запускаем обработку...")
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")

    async with parse_lock:
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
    last_notified: set[tuple[str, int]] = set()

    while True:
        now = datetime.now(TZ)
        today_str = now.strftime("%Y-%m-%d")
        weekday = now.weekday()

        # Ежедневная проверка в 06:00 утра
        if now.hour == 6 and now.minute == 0 and last_6am_check_date != today_str:
            last_6am_check_date = today_str
            last_notified.clear()
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
                    alert_key = (today_str, lesson_num)
                    if alert_key not in last_notified:
                        last_notified.add(alert_key)
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


@dp.message(Command("parse"))
async def cmd_parse(message: types.Message):
    """Принудительно запускает поиск и парсинг расписания из канала (только админ)."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    if parse_lock.locked():
        await message.answer("⚠️ Парсинг уже выполняется. Пожалуйста, подождите...")
        return

    status_msg = await message.answer("⏳ Запускаю ручной парсинг расписания из канала...")
    try:
        updated_dates = await sync_schedule_if_needed(force=True)
        if updated_dates:
            dates_str = ", ".join(sorted(set(updated_dates)))
            await status_msg.edit_text(
                f"✅ Парсинг успешно завершён!\nОбновлены данные на: **{dates_str}**.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"ℹ️ Парсинг завершён.\nНовых расписаний для группы **{config.TARGET_GROUP}** в последних постах канала не найдено.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка при ручном парсинге (/parse): {e}")
        await status_msg.edit_text(f"❌ Произошла ошибка при парсинге: {e}")


@dp.message(Command("test"))
async def cmd_test(message: types.Message):
    """Отправляет тестовое уведомление ТОЛЬКО вызвавшему админу."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

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
    try:
        await sync_schedule_if_needed()
    except Exception as e:
        logger.error(f"Ошибка синхронизации расписания при старте: {e}")

    await asyncio.gather(
        telethon_client.run_until_disconnected(),
        dp.start_polling(bot),
        notification_loop()
    )


if __name__ == "__main__":
    asyncio.run(main())
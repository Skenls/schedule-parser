import asyncio
import io
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image
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
        0: ((8, 0), (7, 45)),
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
        0: ((8, 0), (7, 45)),
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
    - Удаляет ошибочно попавшую в кабинет спецмедгруппу (СМГ -> кабинет пустой).
    - Если для одного lesson_num строки дублируются (одинаковый предмет, ауд и преподаватель) —
      схлопывает в один урок для всей группы (subgroup: 0).
    - Если для урока только одна запись — выставляет subgroup: 0 (вся группа).
    - Если для одного lesson_num две разные записи с subgroup=0, переводит их в subgroup 1 и 2.
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

        # Если в кабинет ошибочно попала спецмедгруппа (СМГ)
        if auditorium.lower().startswith("смг"):
            auditorium = ""

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
        # Если только один урок в этой паре — это вся группа (subgroup: 0)
        if len(items) == 1:
            items[0]["subgroup"] = 0
            normalized.append(items[0])
            continue

        # Проверяем, являются ли строки одинаковыми (дубликат строк бланка для всей группы)
        first = items[0]
        all_same = True
        for it in items[1:]:
            same_subj = first["subject"].lower() == it["subject"].lower()
            same_aud = first["auditorium"] == it["auditorium"]
            same_teacher = (
                not first["teacher"] or not it["teacher"] or
                first["teacher"].lower() == it["teacher"].lower()
            )
            if not (same_subj and same_aud and same_teacher):
                all_same = False
                break

        if all_same:
            teacher = first["teacher"] or items[1]["teacher"]
            normalized.append({
                "lesson_num": l_num,
                "subgroup": 0,
                "subject": first["subject"],
                "auditorium": first["auditorium"],
                "teacher": teacher
            })
            continue

        # Если строки разные, но у обеих subgroup == 0 — разносим по подгруппам 1 и 2
        zeros = [it for it in items if it["subgroup"] == 0]
        if len(zeros) == 2 and len(items) == 2:
            items[0]["subgroup"] = 1
            items[1]["subgroup"] = 2

        # Схлопываем дубли с одинаковым subgroup внутри пары
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


# --- РАСПОЗНАВАНИЕ ЧЕРЕЗ GEMINI (С КАДРИРОВАНИЕМ И НОРМАЛИЗАЦИЕЙ) ---

def crop_group_image(image_bytes: bytes, box_2d: list) -> bytes:
    """Обрезает и масштабирует область расписания группы для повышения точности OCR."""
    im = Image.open(io.BytesIO(image_bytes))
    w, h = im.size
    ymin, xmin, ymax, xmax = box_2d
    if ymax <= 1.0 and xmax <= 1.0 and (ymax > 0 or xmax > 0):
        ymin, xmin, ymax, xmax = [int(v * 1000) for v in (ymin, xmin, ymax, xmax)]

    # Колонки расписания колледжа: левая половина листа (~0..54%) или правая (~46..99%)
    if xmin > 400:
        actual_xmin = int(0.46 * w)
        actual_xmax = int(0.99 * w)
    else:
        actual_xmin = int(0.01 * w)
        actual_xmax = int(0.54 * w)

    actual_ymin = max(0, int(ymin * h / 1000) - int(0.008 * h))
    actual_ymax = min(h, int(ymax * h / 1000) + int(0.02 * h))

    cropped = im.crop((actual_xmin, actual_ymin, actual_xmax, actual_ymax))
    large = cropped.resize((cropped.width * 2, cropped.height * 2), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    large.save(out, format="JPEG", quality=95)
    return out.getvalue()


def normalize_parsed_lessons(lessons: list[dict]) -> list[dict]:
    """Детерминированно очищает, объединяет дубликаты и нормализует подгруппы уроков."""
    by_num: dict[int, list[dict]] = {}
    for l in lessons:
        try:
            num = int(l.get("lesson_num", 0))
        except (ValueError, TypeError):
            continue
        if num < 1 or num > 10:
            continue
        subj = str(l.get("subject", "") or "").strip()
        aud = str(l.get("auditorium", "") or "").strip()
        teach = str(l.get("teacher", "") or "").strip()
        sub = l.get("subgroup", 0)
        try:
            sub = int(sub)
        except (ValueError, TypeError):
            sub = 0

        if aud.lower().startswith("смг"):
            aud = ""
        if "смг" in subj.lower():
            aud = ""
            sub = 2

        if subj and subj != "-" and subj.lower() != "нет":
            by_num.setdefault(num, []).append({
                "lesson_num": num,
                "subgroup": sub,
                "subject": subj,
                "auditorium": aud,
                "teacher": teach
            })

    result = []
    for num, items in sorted(by_num.items()):
        if not items:
            continue
        if len(items) == 1:
            items[0]["subgroup"] = 0
            result.append(items[0])
        else:
            it1 = items[0]
            it2 = items[1]

            # 1. Проверяем физкультуру и спецмедгруппу (СМГ)
            is_fiz = (
                any("физич" in it["subject"].lower() or "физ" in it["subject"].lower() for it in items) or
                any("смг" in it["subject"].lower() for it in items)
            )
            if is_fiz:
                s1 = it1["subject"] if "смг" not in it1["subject"].lower() else "ФизичКультура"
                s2 = it2["subject"] if "смг" in it2["subject"].lower() else "СМГ"
                result.append({"lesson_num": num, "subgroup": 1, "subject": s1, "auditorium": "", "teacher": it1["teacher"]})
                result.append({"lesson_num": num, "subgroup": 2, "subject": s2, "auditorium": "", "teacher": it2["teacher"]})
                continue

            # 2. Проверяем дубликат строки для всей группы (одинаковый предмет, кабинет и преподаватель)
            same_subj = it1["subject"].lower() == it2["subject"].lower()
            same_aud = it1["auditorium"] == it2["auditorium"]
            same_teach = (not it1["teacher"] or not it2["teacher"] or it1["teacher"].lower() == it2["teacher"].lower())

            if same_subj and same_aud and same_teach:
                result.append({
                    "lesson_num": num,
                    "subgroup": 0,
                    "subject": it1["subject"],
                    "auditorium": it1["auditorium"],
                    "teacher": it1["teacher"] or it2["teacher"]
                })
            else:
                # 3. Деление на подгруппы (разные кабинеты, преподаватели или предметы)
                it1["subgroup"] = 1
                it2["subgroup"] = 2
                result.append(it1)
                result.append(it2)

    return result


def parse_image_with_gemini(image_bytes: bytes, target_group: str, fallback_date: str | None = None) -> dict:
    models_to_try = [
        "gemini-3.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
    ]

    prompt_stage1 = f"""
    Найди дату расписания в заголовке листа колледжа (например '25.09.26г.') и границы блока расписания целевой группы '{target_group}'.
    Преобразуй дату в ISO формат: 'YYYY-MM-DD'. Если дата не видна или обрезана, используй подсказку: '{fallback_date or "null"}'.
    Блок группы включает строку с названием группы и все строки уроков под ней до следующей группы или конца таблицы.
    
    КРИТИЧЕСКИ ВАЖНО: Ищи СТРОГО целевую группу '{target_group}'!
    НЕ ПУТАЙ с похожими группами (например '10П', '8П', '9П', '110б', '100б' и т.д. — это ДРУГИЕ группы)!
    Если на листе представлена похожая группа (например '10П'), но нет СТРОГО '{target_group}', верни group_found: false!
    
    Верни строго JSON:
    {{
        "date": "YYYY-MM-DD" или null,
        "group_found": true/false,
        "box_2d": [ymin, xmin, ymax, xmax]
    }}
    """

    stage1_result = None
    for model_name in models_to_try:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt_stage1
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )
            text = response.text
            if not text:
                continue
            text = text.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            stage1_result = json.loads(text)
            if stage1_result:
                break
        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str:
                logger.warning(f"Сервер перегружен (503) на {model_name} в Stage 1. Пробуем следующую модель...")
                continue
            elif "429" in err_str:
                logger.warning(f"Лимит 429 на {model_name} в Stage 1. Пробуем следующую модель...")
                continue
            else:
                logger.warning(f"Ошибка {model_name} в Stage 1: {e}")
                continue

    if not stage1_result:
        return {}

    detected_date = stage1_result.get("date") or fallback_date
    if not stage1_result.get("group_found"):
        logger.info(f"Группа {target_group} не найдена на листе расписания (дата: {detected_date}).")
        return {"date": detected_date, "group_found": False, "lessons": []}

    box_2d = stage1_result.get("box_2d")
    lessons = []

    # Stage 2: Распознавание на кропе высокого разрешения
    if box_2d and isinstance(box_2d, (list, tuple)) and len(box_2d) == 4:
        try:
            crop_bytes = crop_group_image(image_bytes, box_2d)
            prompt_stage2 = f"""
            На увеличенном изображении представлена часть таблицы расписания колледжа.
            1. Внимательно посмотри в левый столбец, где крупно написано название группы.
               Какая группа там написана?
               Если в левой ячейке группы написано НЕ '{target_group}' (например '10П', '8П', '9П' или любая другая группа), верни:
               {{
                 "group_matched": false,
                 "detected_group": "название_найденной_группы",
                 "lessons": []
               }}

            2. ТОЛЬКО если в левой ячейке группы СТРОГО и ТОЧНО написано '{target_group}':
               Проанализируй строки таблицы для группы '{target_group}'.
               Обрати внимание на колонки:
               - 'No ур': номера пар (цифры: 1, 2, 3, 4, 5, 6, 7, 8...).
               - 'Дисциплина': названия предметов
               - 'Аудитория': кабинеты
               - 'Преподаватель': ФИО

               ВАЖНЫЕ ПРАВИЛА ВЕРТИКАЛЬНОГО СОПОСТАВЛЕНИЯ СТРОК И НОМЕРОВ ПАР:
               - Внимательно смотри строго по горизонтали: какой номер пары в колонке 'No ур' соответствует строке предмета.
               - ДЕЛЕНИЕ НА ПОДГРУППЫ:
                 * Если у пары ДВЕ строки (два разных кабинета, например 310 и 515, или разные преподаватели/предметы), номер пары пишется ОДИН РАЗ напротив первой строки, а напротив второй строки в колонке 'No ур' ПУСТО (нет новой цифры). Обе эти строки относятся к этой одной паре (subgroup 1 и subgroup 2)!
                 * Если по физкультуре сверху 'ФизичКультура', а снизу 'СМГ' (или 'СМГ6') — это подгруппы 1 и 2 одной пары. Кабинет у физкультуры и СМГ пустой ("").
               - ВСЯ ГРУППА:
                 * Если пара для всей группы (один кабинет, один предмет), то у каждой такой пары СВОЙ отдельный номер в колонке 'No ур' (например, цифры 4, 5, 6, 7, 8 идут подряд, каждая напротив своей строки). Не объединяй строки с разными номерами пар!
                 * Если пара напечатана одинаково в две строки (один предмет, одинаковый кабинет и одинаковый преподаватель) — это ОДИН урок для всей группы (subgroup: 0).
               - Если сверху или снизу виден фрагмент чужой группы, НЕ включай их! Извлекай ТОЛЬКО уроки для группы '{target_group}'!
               - Никогда не перескакивай и не сдвигай номера пар: проверь, что каждая цифра в колонке 'No ур' попала в итоговый список уроков!

               Ответ строго валидным JSON:
               {{
                 "group_matched": true,
                 "detected_group": "{target_group}",
                 "lessons": [
                   {{"lesson_num": 2, "subgroup": 1, "subject": "...", "auditorium": "...", "teacher": "..."}},
                   {{"lesson_num": 2, "subgroup": 2, "subject": "...", "auditorium": "...", "teacher": "..."}}
                 ]
               }}
            """

            for model_name in models_to_try:
                try:
                    response = ai_client.models.generate_content(
                        model=model_name,
                        contents=[
                            genai_types.Part.from_bytes(data=crop_bytes, mime_type="image/jpeg"),
                            prompt_stage2
                        ],
                        config=genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=0.1
                        )
                    )
                    text = response.text
                    if not text:
                        continue
                    text = text.strip()
                    if text.startswith("```"):
                        lines = text.splitlines()
                        if lines and lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        text = "\n".join(lines).strip()
                    d2 = json.loads(text)
                    if d2.get("group_matched") is False:
                        logger.warning(
                            f"Кроп содержит группу '{d2.get('detected_group')}', а не '{target_group}'. Отклоняем лист."
                        )
                        return {"date": detected_date, "group_found": False, "lessons": []}
                    raw_lessons = d2.get("lessons", [])
                    if raw_lessons:
                        lessons = normalize_parsed_lessons(raw_lessons)
                        logger.info(f"✅ Stage 2 успешно извлек {len(lessons)} уроков для {target_group} через {model_name}.")
                        break
                except Exception as e:
                    err_str = str(e)
                    if "503" in err_str or "UNAVAILABLE" in err_str:
                        logger.warning(f"Сервер перегружен (503) на {model_name} в Stage 2. Пробуем следующую модель...")
                        continue
                    elif "429" in err_str:
                        logger.warning(f"Лимит 429 на {model_name} в Stage 2. Пробуем следующую модель...")
                        continue
                    else:
                        logger.warning(f"Ошибка {model_name} в Stage 2: {e}")
                        continue
        except Exception as e:
            logger.error(f"Ошибка кадрирования изображения в Stage 2: {e}")

    # Fallback на прямое распознавание полного листа, если кроп не удался или не дал уроков
    if not lessons:
        logger.warning("Stage 2 не вернул уроков, запускаем fallback на распознавание всего листа...")
        prompt_fallback = f"""
        Проанализируй фото расписания колледжа.
        1. Найди в таблице целевую группу '{target_group}'.
           Если группы '{target_group}' нет на листе (например лист других групп), верни: {{"lessons": []}}
        2. Изучи строки таблицы для группы '{target_group}':
           - 'No ур': номера пар
           - 'Дисциплина': предметы
           - 'Аудитория': кабинеты
        3. Если у пары две строки (разные кабинеты/преподаватели/предметы или физкультура+СМГ) — это подгруппы 1 и 2 одной пары.
        4. Если у каждого предмета свой отдельный номер в 'No ур' — это самостоятельные пары для всей группы (subgroup: 0).
        5. Если предмет и кабинет одинаковые в двух строках — это вся группа (subgroup: 0).

        Ответ в JSON:
        {{
            "lessons": [
                {{"lesson_num": 1, "subgroup": 0, "subject": "...", "auditorium": "...", "teacher": "..."}}
            ]
        }}
        """
        for model_name in models_to_try:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=[
                        genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        prompt_fallback
                    ],
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )
                text = response.text
                if not text:
                    continue
                d_fb = json.loads(text)
                raw_fb = d_fb.get("lessons", [])
                if raw_fb:
                    lessons = normalize_parsed_lessons(raw_fb)
                    break
            except Exception:
                continue

    return {
        "date": detected_date,
        "group_found": True,
        "lessons": lessons
    }


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

                if parsed_date == today_str:
                    logger.info(f"🎉 Расписание на сегодня ({today_str}) успешно найдено и загружено!")
                    break

                if parsed_date < today_str:
                    logger.info(f"Встречен лист за прошлый день ({parsed_date}). Прекращаем поиск.")
                    break

                if parsed_date > today_str:
                    logger.info(f"Сохранен лист на будущее ({parsed_date}). Продолжаем поиск сегодняшнего...")

        return updated_dates


async def sync_nextday_schedule() -> tuple[bool, str, str]:
    """
    Ищет в канале расписание на следующий учебный день.
    Возвращает (success: bool, target_next_date: str, found_date: str):
    - success=True: найдено и сохранено расписание на target_next_date.
    - success=False, found_date=today_str: встречен сегодняшний лист, значит следующий день еще не опубликован.
    - success=False, found_date="": ничего подходящего не найдено в пределах лимита.
    """
    async with parse_lock:
        now = datetime.now(TZ)
        today_str = now.strftime("%Y-%m-%d")
        if now.weekday() == 5:
            next_date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            next_date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        target_chat = getattr(config, 'CHANNEL_TARGET', None) or getattr(config, 'CHANNEL_USERNAME', None)
        max_history = 15

        logger.info(f"Запуск ручного поиска расписания на следующий день ({next_date_str})...")

        async for msg in telethon_client.iter_messages(target_chat, limit=max_history):
            if not msg.photo:
                continue

            logger.info(f"Скачиваем фото из поста ID: {msg.id} для проверки следующего дня...")
            parsed_date = await process_photo_message(msg, next_date_str)
            await asyncio.sleep(2)

            if parsed_date:
                if parsed_date == next_date_str:
                    logger.info(f"🎉 Расписание на следующий день ({next_date_str}) успешно найдено и сохранено!")
                    return True, next_date_str, parsed_date

                if parsed_date == today_str:
                    logger.info(f"Встречен лист на сегодня ({today_str}). Листа на следующий день ({next_date_str}) ещё нет.")
                    return False, next_date_str, today_str

                if parsed_date < today_str:
                    logger.info(f"Встречен лист за прошлый день ({parsed_date}). Листа на следующий день ({next_date_str}) нет.")
                    return False, next_date_str, parsed_date

        return False, next_date_str, ""


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

async def send_lesson_alert(
    today_str: str,
    lesson_num: int,
    break_start_str: str,
    target_user_id: int | None = None,
    force: bool = False
):
    """
    Отправляет уведомления пользователям с учетом их индивидуального расписания и подгрупп.
    - lesson_num: текущий номер слота (0 — уведомление за 15 мин до 1-го урока).
    - break_start_str: время начала перемены или первого урока (HH:MM).
    - target_user_id: если задан, отправляет только этому пользователю (/test).
    - force: если True, принудительно отправляет тестовое уведомление.
    """
    next_lesson_num = lesson_num + 1

    async with aiosqlite.connect("schedule.db") as db:
        async with db.execute(
            "SELECT lesson_num, subgroup, subject, auditorium FROM schedule WHERE date = ?",
            (today_str,)
        ) as cursor:
            all_today_lessons = await cursor.fetchall()

        if target_user_id:
            async with db.execute("SELECT user_id, subgroup FROM users WHERE user_id = ?", (target_user_id,)) as cursor:
                users = await cursor.fetchall()
        else:
            async with db.execute("SELECT user_id, subgroup FROM users") as cursor:
                users = await cursor.fetchall()

    for user_id, user_sub in users:
        user_lessons = [
            (l_num, sub, subj, aud)
            for l_num, sub, subj, aud in all_today_lessons
            if user_sub == 0 or sub == 0 or sub == user_sub
        ]

        # Для принудительного теста (/test)
        if force:
            matched = [(sub, subj, aud) for l_num, sub, subj, aud in user_lessons if l_num == next_lesson_num]
            if matched:
                text = f"⏳ В **{break_start_str}** начинается перемена!\n\n📌 **Следующий урок ({next_lesson_num}):**\n"
                for sub, subj, aud in matched:
                    sub_label = f" (Подгруппа {sub})" if sub > 0 else ""
                    room = f"каб. {aud}" if aud else "кабинет не указан"
                    text += f"• **{subj}**{sub_label} — 🚪 {room}\n"
            else:
                text = (
                    f"🔔 В **{break_start_str}** начинается перемена!\n\n"
                    f"Следующего ({next_lesson_num}) урока у вас нет — можно отдыхать."
                )
            try:
                await bot.send_message(user_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление {user_id}: {e}")
            continue

        # Если у пользователя сегодня вообще нет уроков — не беспокоим
        if not user_lessons:
            continue

        user_lesson_nums = {l_num for l_num, _, _, _ in user_lessons}
        first_lesson = min(user_lesson_nums)
        last_lesson = max(user_lesson_nums)

        # 1. До первого урока: уведомляем ТОЛЬКО за 15 минут до первого урока пользователя (lesson_num == first_lesson - 1)
        if lesson_num < first_lesson - 1:
            continue

        # 2. После последнего урока дня: больше никаких уведомлений не присылаем
        if lesson_num > last_lesson:
            continue

        # 3. Уведомление перед первым уроком (при lesson_num == 0 и первом уроке 1)
        if lesson_num == 0:
            matched = [(sub, subj, aud) for l_num, sub, subj, aud in user_lessons if l_num == 1]
            text = f"⏳ В **{break_start_str}** начинается 1-й урок!\n\n📌 **Следующий урок (1):**\n"
            for sub, subj, aud in matched:
                sub_label = f" (Подгруппа {sub})" if sub > 0 else ""
                room = f"каб. {aud}" if aud else "кабинет не указан"
                text += f"• **{subj}**{sub_label} — 🚪 {room}\n"

        # 4. Уведомление в конце последнего урока дня
        elif lesson_num == last_lesson:
            text = (
                f"🔔 В **{break_start_str}** заканчиваются уроки!\n\n"
                f"Следующего ({next_lesson_num}) урока у вас нет — можно отдыхать."
            )

        # 5. Уведомление в течение дня (перед следующим уроком или окном)
        else:
            matched = [(sub, subj, aud) for l_num, sub, subj, aud in user_lessons if l_num == next_lesson_num]
            if matched:
                text = f"⏳ В **{break_start_str}** начинается перемена!\n\n📌 **Следующий урок ({next_lesson_num}):**\n"
                for sub, subj, aud in matched:
                    sub_label = f" (Подгруппа {sub})" if sub > 0 else ""
                    room = f"каб. {aud}" if aud else "кабинет не указан"
                    text += f"• **{subj}**{sub_label} — 🚪 {room}\n"
            else:
                text = (
                    f"🔔 В **{break_start_str}** начинается перемена!\n\n"
                    f"Следующего ({next_lesson_num}) урока у вас нет — можно отдыхать."
                )

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
                        await send_lesson_alert(today_str, lesson_num, break_str)

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


@dp.message(Command("nextday"))
async def cmd_nextday(message: types.Message):
    """Показывает расписание на следующий учебный день."""
    now = datetime.now(TZ)
    # Если суббота (5), следующим днем обычно является понедельник (+2 дня)
    if now.weekday() == 5:
        sunday_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        monday_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        if await has_schedule_for_date(sunday_str):
            target_date = sunday_str
        else:
            target_date = monday_str
    else:
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    async with aiosqlite.connect("schedule.db") as db:
        async with db.execute("SELECT subgroup FROM users WHERE user_id = ?", (message.from_user.id,)) as c:
            row = await c.fetchone()
            user_sub = row[0] if row else 0

        async with db.execute(
            "SELECT lesson_num, subgroup, subject, auditorium FROM schedule WHERE date = ? ORDER BY lesson_num, subgroup",
            (target_date,)
        ) as c:
            rows = await c.fetchall()

    if not rows:
        await message.answer(f"📅 На следующий день ({target_date}) расписание в базе не найдено.")
        return

    user_rows = [
        (l_num, sub, subj, aud)
        for l_num, sub, subj, aud in rows
        if user_sub == 0 or sub == 0 or sub == user_sub
    ]

    sub_label = "Вся группа" if user_sub == 0 else f"{user_sub}-я подгруппа"
    text = f"📅 **Расписание на следующий день ({target_date})**\nПрофиль: **{sub_label}**\n\n"

    if not user_rows:
        text += "Пар для вашей подгруппы нет — можно отдыхать!"
    else:
        for l_num, sub, subj, aud in user_rows:
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


@dp.message(Command("parsenext"))
async def cmd_parsenext(message: types.Message):
    """Ручной поиск и парсинг расписания на следующий учебный день (только админ)."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    if parse_lock.locked():
        await message.answer("⚠️ Парсинг уже выполняется. Пожалуйста, подождите...")
        return

    now = datetime.now(TZ)
    if now.weekday() == 5:
        next_date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        next_date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    status_msg = await message.answer(f"⏳ Ищу в канале расписание на следующий день ({next_date_str})...")
    try:
        success, target_next_date, found_date = await sync_nextday_schedule()
        if success:
            await status_msg.edit_text(
                f"✅ Расписание на следующий день (**{target_next_date}**) успешно найдено и сохранено в базе!",
                parse_mode="Markdown"
            )
        elif found_date == datetime.now(TZ).strftime("%Y-%m-%d"):
            await status_msg.edit_text(
                f"ℹ️ Расписания на следующий день (**{target_next_date}**) в канале ещё нет.\n"
                f"Последний опубликованный лист в канале — на сегодня (**{found_date}**).",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"ℹ️ Расписание на следующий день (**{target_next_date}**) в последних постах канала не найдено.",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка при ручном парсинге следующего дня (/parsenext): {e}")
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
        lesson_num=1,
        break_start_str="09:40",
        target_user_id=message.from_user.id,
        force=True
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
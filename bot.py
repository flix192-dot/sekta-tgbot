import os
import re
import io
import json
import time
import logging
import asyncio
import tempfile
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import httpx
import firebase_admin
from firebase_admin import credentials, db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── LOGGING ────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── CONFIG ─────────────────────────────────────────────────
BOT_TOKEN     = os.environ['BOT_TOKEN']
FIREBASE_URL  = os.environ['FIREBASE_URL']
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GROQ_API_KEY  = os.environ.get('GROQ_API_KEY', '')

TZ_ARTEM  = ZoneInfo('Europe/Kyiv')
TZ_BOGDAN = ZoneInfo('Europe/Paris')

USERS = {
    int(os.environ['TG_ARTEM']):  'artem',
    int(os.environ['TG_BOGDAN']): 'bogdan',
}
PLAYER_TG    = {v: k for k, v in USERS.items()}
PLAYER_NAMES = {'artem': 'Артём', 'bogdan': 'Богдан'}
PLAYER_TZ    = {'artem': TZ_ARTEM, 'bogdan': TZ_BOGDAN}

REMIND_OPTIONS = [
    (10,   '10 минут'),
    (15,   '15 минут'),
    (30,   '30 минут'),
    (60,   '1 час'),
    (120,  '2 часа'),
    (1440, 'За день'),
]

# Контрольные точки в течение дня для напоминаний о невыполненных привычках
# (локальное время игрока, HH:MM). Последнее время в списке считается "финальным".
HABIT_REMINDER_TIMES = ['13:00', '17:00', '21:00', '22:45']

GEMINI_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-3.1-flash-lite:generateContent?key={key}'
)
WHISPER_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'

# ─── FIREBASE INIT ───────────────────────────────────────────
firebase_creds = json.loads(os.environ['FIREBASE_CREDS'])
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})

# ─── IN-MEMORY STATE ─────────────────────────────────────────
sent_reminders: set[tuple] = set()
_reminder_settings_cache: dict[str, list[int]] = {}
pending: dict[int, dict] = {}
_habit_reminder_enabled_cache: dict[str, bool] = {}
_habit_reminder_sent: set[tuple] = set()

# ─── HELPERS ────────────────────────────────────────────────
def now_for(player: str) -> datetime:
    return datetime.now(PLAYER_TZ[player])

def now_local() -> datetime:
    return datetime.now(TZ_ARTEM)

def resolve_date(text: str) -> date | None:
    text = text.lower().strip()
    today = now_local().date()
    if 'послезавтра' in text: return today + timedelta(days=2)
    if 'завтра' in text:      return today + timedelta(days=1)
    if 'сегодня' in text:     return today
    m = re.search(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:   return date(year, month, day)
        except ValueError: return None
    return None

def resolve_time(text: str) -> str | None:
    text = text.lower()
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"
    m = re.search(r'в\s+(\d{1,2})(?:\s*(утра|вечера|ночи|дня))?', text)
    if m:
        h = int(m.group(1))
        suffix = m.group(2)
        if suffix in ('вечера', 'ночи') and h < 12: h += 12
        if suffix == 'дня' and h < 12:              h += 12
        if 0 <= h <= 23: return f"{h:02d}:00"
    return None

def resolve_duration(text: str) -> int:
    text = text.lower()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(час|ч\b|минут|мин\b)', text)
    if m:
        val = float(m.group(1).replace(',', '.'))
        unit = m.group(2)
        if 'час' in unit or unit == 'ч': return int(val * 60)
        return int(val)
    return 60

def parse_event(text: str) -> dict | None:
    text = re.sub(r'^/\w+\s*', '', text).strip()
    text_clean = re.sub(
        r'^(поставь|добавь|создай|запланируй|напомни|запиши)\s+(задачу|событие|встречу|дело|напоминание)?\s*',
        '', text, flags=re.IGNORECASE
    ).strip()

    event_date = resolve_date(text_clean)
    event_time = resolve_time(text_clean)
    duration   = resolve_duration(text_clean)

    title = text_clean
    title = re.sub(r'\b(завтра|сегодня|послезавтра)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\d{1,2}[./]\d{1,2}(?:[./]\d{4})?', '', title)
    title = re.sub(r'\bв\s+\d{1,2}(?::\d{2})?\s*(?:утра|вечера|ночи|дня)?\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\b\d+\s*(?:час|ч\b|минут|мин\b)\w*\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s{2,}', ' ', title).strip(' ,.-')

    if not title: return None
    return {'date': event_date, 'time': event_time, 'duration': duration, 'title': title}

def write_event_to_firebase(player_id: str, event: dict) -> str:
    ref     = db.reference('/sekta/calEvents')
    current = ref.get() or []
    if isinstance(current, dict):
        current = list(current.values())

    event_date = event['date']
    event_time = event['time'] or '09:00'
    date_str   = event_date.strftime('%Y-%m-%d') if event_date else ''

    h, m = map(int, event_time.split(':'))
    end_min  = h * 60 + m + event['duration']
    end_time = f"{(end_min // 60) % 24:02d}:{end_min % 60:02d}"

    ev_id = f"ev_{int(time.time() * 1000)}"
    new_event = {
        'id':       ev_id,
        'title':    event['title'],
        'date':     date_str,
        'time':     event_time,
        'endTime':  end_time,
        'color':    '#c8a96e' if player_id == 'artem' else '#4caf7d',
        'owner':    player_id,
        'sourceType': 'bot',
        'remindersEnabled': True,
    }
    current.append(new_event)
    ref.set(current)
    return ev_id

# ─── GEMINI TEXT ─────────────────────────────────────────────
async def gemini_text(prompt: str, system: str = '') -> str:
    """Вызывает Gemini Flash для текстовых запросов."""
    if not GEMINI_API_KEY:
        return ''
    url = GEMINI_URL.format(key=GEMINI_API_KEY)
    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'systemInstruction': {'parts': [{'text': system}]} if system else None,
        'generationConfig': {'temperature': 0.3, 'maxOutputTokens': 800},
    }
    if not system:
        del body['systemInstruction']
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body)
            if r.status_code != 200:
                logger.error(f'Gemini text HTTP {r.status_code}: {r.text[:500]}')
            r.raise_for_status()
            data = r.json()
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logger.error(f'Gemini text error: {e}')
        return ''

# ─── WHISPER: РАСПОЗНАВАНИЕ ГОЛОСА ───────────────────────────
async def whisper_transcribe(ogg_bytes: bytes) -> str:
    """
    Отправляет голосовое сообщение (OGG/Opus из Telegram) в Groq Whisper API
    (whisper-large-v3-turbo, бесплатный тариф до 2000 запросов/день)
    для транскрипции. Возвращает распознанный текст или '' при ошибке.
    """
    if not GROQ_API_KEY:
        return ''
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            files = {'file': ('voice.ogg', bytes(ogg_bytes), 'audio/ogg')}
            data  = {'model': 'whisper-large-v3-turbo', 'language': 'ru'}
            headers = {'Authorization': f'Bearer {GROQ_API_KEY}'}
            r = await client.post(WHISPER_URL, headers=headers, data=data, files=files)
            r.raise_for_status()
            result = r.json()
            return (result.get('text') or '').strip()
    except Exception as e:
        logger.error(f'Whisper transcribe error: {e}')
        return ''

# ─── HABITS SYSTEM ───────────────────────────────────────────

# Привычки которые требуют фото-доказательства
HABITS_NEED_PROOF = {
    'workout':   ('тренировка', ['потренировался', 'потренировалась', 'тренировка', 'зал', 'пожал', 'качалка', 'поднял', 'жим', 'приседания', 'кардио']),
    'content':   ('контент',    ['контент', 'посмотрел', 'посмотрела', 'видео', 'ютуб', 'youtube', 'рилс', 'шортс', 'тик ток']),
    'nmt':       ('НМТ',        ['нмт', 'подготовка', 'готовился', 'готовилась', 'учился', 'учился к нмт', 'история', 'украинский']),
    'steps':     ('10к шагов',  ['шаги', 'шагов', 'прошёл', 'прошла', 'ходил', 'ходила', '10к', '10000']),
    'lecture':   ('лекция',     ['лекция', 'лекцию', 'смотрел лекцию', 'посмотрел лекцию', 'база знаний', 'лекции']),
}

# Привычки без фото
HABITS_NO_PROOF = {
    'wakeup':    ('ранний подъём', ['проснулся', 'встал', 'подъём', 'поднялся', 'рано встал']),
    'nosugar':   ('без сахара',   ['без сахара', 'не ел сладкое', 'не ел сахар', 'не пил сладкое']),
    'devices':   ('устройства',   ['телефон отложил', 'отключил телефон', 'без телефона', 'устройства']),
    'water':     ('вода',         ['выпил воду', 'воду выпил', '2.5 литра', 'вода выпита', 'норму воды']),
    'sleep':     ('ранний отбой', ['лёг спать', 'лёг', 'отбой', 'заснул', 'уснул']),
    'protein':   ('протеин',      ['протеин', 'выпил протеин', 'протеин выпил']),
}

# Привычки которые отмечаются текстовым сообщением (не фото):
# — благодарности: само сообщение и есть запись благодарности
# — чтение: в сообщении есть номер страницы
HABITS_TEXT_PROOF = {
    'gratitude': ('благодарности', ['благодарности', 'благодарность', 'поблагодарил', 'благодарен']),
    'reading':   ('чтение книг',   ['почитал', 'прочитал', 'читал', 'книгу', 'страницы', 'страница', 'страниц']),
}

ALL_HABITS = {**HABITS_NEED_PROOF, **HABITS_NO_PROOF, **HABITS_TEXT_PROOF}

PROOF_PROMPTS = {
    'workout':   '📸 Отлично! Отправь фото из зала или скрин из фитнес-приложения',
    'content':   '📸 Пришли скрин что смотрел контент (лайк, просмотр и т.д.)',
    'nmt':       '📸 Пришли скрин результата или страницы с конспектом',
    'steps':     '📸 Пришли скрин из приложения шагов (Samsung Health, Apple Health и т.д.)',
    'lecture':   '📸 Отправь скрин прогресса просмотра лекции',
}

# Ждут фото: uid → habit_id
waiting_for_proof: dict[int, str] = {}
# Ждут номер страницы (привычка "чтение"): uid → True
waiting_for_page: dict[int, bool] = {}
# Ждут текст благодарности: uid → True
waiting_for_gratitude: dict[int, bool] = {}

def detect_habit(text: str) -> str | None:
    """Находит упоминание привычки в тексте, возвращает habit_id или None."""
    text_lower = text.lower()
    for habit_id, (name, keywords) in ALL_HABITS.items():
        if any(kw in text_lower for kw in keywords):
            return habit_id
    return None

def mark_habit_done(player: str, habit_id: str) -> bool:
    """Ставит галочку на привычку в Firebase за сегодня."""
    try:
        ref = db.reference('/sekta')
        state = ref.get() or {}

        today = datetime.now(PLAYER_TZ[player]).date()
        # Определяем номер дня в неделе (0=пн, ..., 6=вс)
        day_idx = today.weekday()  # 0=Monday

        # Найти нужную неделю в weekly
        weekly = state.get('weekly', {})

        # Ключ недели — сайт хранит дату ПОНЕДЕЛЬНИКА текущей недели
        # Формат: "2026-07-06" (YYYY-MM-DD понедельника)
        # weekday(): 0=Пн, 1=Вт, ..., 6=Вс
        monday = today - __import__('datetime').timedelta(days=today.weekday())
        week_key = monday.strftime("%Y-%m-%d")

        path = f"/sekta/weekly/{week_key}/{player}/{habit_id}"
        habit_ref = db.reference(path)
        arr = habit_ref.get()

        if not isinstance(arr, list):
            arr = [False] * 7
        while len(arr) < 7:
            arr.append(False)

        arr[day_idx] = True

        # Сохраняем время выполнения
        time_str = datetime.now(PLAYER_TZ[player]).strftime('%H:%M')
        habit_ref.set(arr)

        # Записываем время
        time_path = f"/sekta/habitTimes/{player}_{habit_id}_{day_idx}"
        db.reference(time_path).set(time_str)

        logger.info(f"✅ Marked {habit_id} done for {player} day {day_idx} week {week_key}")
        return True
    except Exception as e:
        logger.error(f"mark_habit_done error: {e}")
        return False

def _week_key_for(player: str) -> tuple[date, str]:
    today = now_for(player).date()
    monday = today - timedelta(days=today.weekday())
    return today, monday.strftime("%Y-%m-%d")

def save_reading_page(player: str, page: int) -> bool:
    """Сохраняет текущую страницу чтения в Firebase (общий прогресс + запись по дню)."""
    try:
        today, week_key = _week_key_for(player)
        day_idx = today.weekday()
        now_str = datetime.now(PLAYER_TZ[player]).isoformat()

        # Текущий прогресс (последняя отмеченная страница)
        db.reference(f'/sekta/readingProgress/{player}').set({
            'page': page,
            'updatedAt': now_str,
        })
        # История по дням недели (совместимо с habitPhotos/habitNotes)
        db.reference(f'/sekta/habitNotes/{week_key}/{player}/reading').set({
            'page': page,
            'day': day_idx,
            'time': datetime.now(PLAYER_TZ[player]).strftime('%H:%M'),
        })
        logger.info(f"📖 Reading page saved for {player}: page {page}")
        return True
    except Exception as e:
        logger.error(f"save_reading_page error: {e}")
        return False

def save_gratitude_text(player: str, text: str) -> bool:
    """Сохраняет текст благодарности в Firebase за сегодня."""
    try:
        today, week_key = _week_key_for(player)
        day_idx = today.weekday()
        db.reference(f'/sekta/habitNotes/{week_key}/{player}/gratitude').set({
            'text': text,
            'day': day_idx,
            'time': datetime.now(PLAYER_TZ[player]).strftime('%H:%M'),
        })
        logger.info(f"🙏 Gratitude text saved for {player}")
        return True
    except Exception as e:
        logger.error(f"save_gratitude_text error: {e}")
        return False

async def handle_habit_report(update: Update, uid: int, player: str, text: str) -> bool:
    """
    Проверяет текст на упоминание привычки.
    Если нашли → для привычек с фото просим доказательство,
                  для привычек с текстом (благодарности/чтение) обрабатываем текст,
                  для привычек без доказательств сразу ставим галочку.
    Возвращает True если обработали как привычку, False если нет.
    """
    habit_id = detect_habit(text)
    if not habit_id:
        return False

    name = ALL_HABITS[habit_id][0]

    if habit_id in HABITS_NEED_PROOF:
        # Просим доказательство
        waiting_for_proof[uid] = habit_id
        await update.message.reply_text(
            f'💪 *{name}* — молодец!\n\n{PROOF_PROMPTS.get(habit_id, "📸 Пришли доказательство")}',
            parse_mode='Markdown'
        )
        return True

    if habit_id == 'gratitude':
        # Само сообщение и есть запись благодарности — сохраняем текст и сразу ставим галочку
        ok = save_gratitude_text(player, text) and mark_habit_done(player, habit_id)
        if ok:
            await update.message.reply_text(
                '🙏 *Благодарности* — записано и отмечено!\nТекст сохранён на сайте.',
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text('⚠️ Не удалось сохранить благодарность. Попробуй позже.')
        return True

    if habit_id == 'reading':
        # Ищем номер страницы прямо в сообщении
        m = re.search(r'(\d+)', text)
        if m:
            page = int(m.group(1))
            ok = save_reading_page(player, page) and mark_habit_done(player, habit_id)
            if ok:
                await update.message.reply_text(
                    f'📖 *Чтение книг* — отмечено!\nСтраница {page} сохранена на сайте.',
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text('⚠️ Не удалось сохранить страницу. Попробуй позже.')
        else:
            waiting_for_page[uid] = True
            await update.message.reply_text('📖 На какой ты сейчас странице? Просто напиши число.')
        return True

    # Привычка без доказательств — ставим сразу
    ok = mark_habit_done(player, habit_id)
    if ok:
        await update.message.reply_text(
            f'✅ *{name}* — отмечено!\nГалочка поставлена в таблице привычек.',
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f'⚠️ Не удалось поставить галочку для *{name}*. Попробуй позже.', parse_mode='Markdown')
    return True

async def handle_proof_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фото как доказательство привычки."""
    uid = update.effective_user.id
    if uid not in USERS:
        return

    player = USERS[uid]

    # Если есть подпись к фото — проверяем не упоминание ли это новой привычки
    caption = (update.message.caption or '').strip()
    if caption and uid not in waiting_for_proof:
        habit_id = detect_habit(caption)
        if habit_id:
            waiting_for_proof[uid] = habit_id

    if uid not in waiting_for_proof:
        await update.message.reply_text('📎 Получил фото. Напиши какую привычку хочешь отметить — например «потренировался» или «прошёл 10к шагов».')
        return

    habit_id = waiting_for_proof.pop(uid)
    name     = ALL_HABITS[habit_id][0]

    # Сохраняем фото в Firebase
    try:
        photo     = update.message.photo[-1]
        file_obj  = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file_obj.download_as_bytearray()
        import base64
        b64 = 'data:image/jpeg;base64,' + base64.b64encode(bytes(img_bytes)).decode()

        today    = datetime.now(PLAYER_TZ[player]).date()
        week_num = today.isocalendar()[1]
        week_key = f"{today.year}-W{week_num}"

        photo_path = f"/sekta/habitPhotos/{week_key}/{player}/{habit_id}"
        db.reference(photo_path).set(b64)
        logger.info(f"📸 Photo saved for {player}/{habit_id}")
    except Exception as e:
        logger.warning(f"Photo save error (non-critical): {e}")

    ok = mark_habit_done(player, habit_id)
    if ok:
        await update.message.reply_text(
            f'✅ *{name}* — принято!\n\nГалочка поставлена, фото сохранено как доказательство 📎',
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text('⚠️ Фото получено, но не удалось поставить галочку. Попробуй снова.')

# ─── GEMINI: КЛАССИФИКАЦИЯ НАМЕРЕНИЯ ─────────────────────────
EVENT_TRIGGER_RE = re.compile(
    r'\b(поставь|добавь|создай|запланируй|запланировать|напомни|напомнить|'
    r'запиши|запиши событие|поставить|добавить|планир\w*|запланир\w*)\b',
    re.IGNORECASE
)

def heuristic_is_event(text: str) -> dict | None:
    """
    Строгий фолбэк без ИИ: считаем это событием ТОЛЬКО если в тексте
    явно есть слово-триггер (поставь/добавь/запланируй/напомни/запиши и т.п.).
    Просто упоминание даты/времени само по себе НЕ делает текст событием —
    это защищает от ложных срабатываний на обычные фразы и плохие транскрипты.
    """
    if not EVENT_TRIGGER_RE.search(text):
        return None
    return parse_event(text)

async def gemini_classify_intent(text: str, player: str) -> dict:
    """
    Единая точка входа для анализа И текста, И расшифрованной голосовухи.
    Сначала явно определяет намерение пользователя:
      - is_event=true  → пользователь просит запланировать/добавить событие в календарь
      - is_event=false → обычное сообщение/вопрос, разговор на тему дисциплины
    Только если is_event=true — извлекает title/date/time/duration и это
    передаётся в уже готовый код записи события (write_event_to_firebase),
    а не обрабатывается заново самой моделью.
    Возвращает {'is_event': bool, 'event': dict|None}.
    Если Gemini недоступна/не ответила/дала не-JSON — НЕ считаем это событием
    по умолчанию, а используем строгий heuristic с явными триггер-словами.
    """
    if not GEMINI_API_KEY:
        parsed = heuristic_is_event(text)
        return {'is_event': bool(parsed), 'event': parsed}

    today = now_for(player).date()
    system = (
        'Ты модуль классификации намерения в Telegram-боте дисциплинарного клуба. '
        'Твоя ЕДИНСТВЕННАЯ задача — определить, просит ли пользователь ЯВНО '
        'запланировать/добавить/поставить событие, встречу, задачу или напоминание в календарь. '
        'Если это обычный вопрос, реплика, приветствие, разговор о дисциплине, мотивации, жизни, '
        'бессвязный или плохо распознанный текст, или что угодно, не являющееся прямой '
        'просьбой добавить что-то в календарь — это НЕ событие. '
        'Не пытайся притянуть обычное сообщение к событию. В сомнительных случаях всегда выбирай false. '
        f'Сегодня {today.strftime("%d.%m.%Y")} ({today.strftime("%A")}). '
        'Примеры НЕ событий (is_event=false): "привет как дела", "спасибо", "алло здорово", '
        '"что думаешь про дисциплину", любые приветствия и бессвязные фразы. '
        'Примеры событий (is_event=true): "поставь завтра в 14 тренировку", '
        '"запланируй встречу на 03.07", "напомни мне позвонить в 18:00". '
        'Ответь СТРОГО JSON без markdown, формат: '
        '{"is_event": true/false, "title": "..." или null, '
        '"date": "YYYY-MM-DD" или null, "time": "HH:MM" или null, '
        '"duration_min": 60}. '
        'Если is_event=false — все остальные поля null. '
        'Если дата/время в тексте не указаны явно — null (не придумывай). '
        'Длительность по умолчанию 60 минут. Ничего кроме JSON не пиши.'
    )
    raw = await gemini_text(text, system)
    if not raw:
        # Gemini не ответила (сеть/лимит/фильтр) — НЕ считаем событием по умолчанию
        parsed = heuristic_is_event(text)
        return {'is_event': bool(parsed), 'event': parsed}
    try:
        raw = re.sub(r'^```json\s*|```$', '', raw.strip())
        data = json.loads(raw)
        if not data.get('is_event') or not data.get('title'):
            return {'is_event': False, 'event': None}
        date_obj = None
        if data.get('date'):
            try: date_obj = date.fromisoformat(data['date'])
            except ValueError: pass
        event = {
            'title':    data['title'].strip(),
            'date':     date_obj,
            'time':     data.get('time') or None,
            'duration': int(data.get('duration_min') or 60),
        }
        return {'is_event': True, 'event': event}
    except Exception as e:
        logger.warning(f'Gemini intent classify fallback: {e}')
        # JSON не распарсился — НЕ считаем событием по умолчанию
        parsed = heuristic_is_event(text)
        return {'is_event': bool(parsed), 'event': parsed}

# ─── GEMINI FREE CHAT ─────────────────────────────────────────
async def gemini_chat(text: str, player: str) -> str:
    """Свободный вопрос к Gemini — если это не команда и не событие."""
    name = PLAYER_NAMES[player]
    system = (
        f'Ты персональный ассистент для участника дисциплинарного клуба "Секта". '
        f'Тебя зовут Секта-бот. Ты общаешься с {name}. Никогда не начинай ответ с приветствия. '
        'Ты помогаешь с вопросами о дисциплине, планировании, мотивации и жизни. '
        'Отвечай коротко, по делу, на русском языке. Максимум 3-4 предложения. '
        'ВАЖНО: никогда не начинай ответ с приветствия — не пиши «Привет», «Привет, {name}», «Здравствуй» и подобное. '
        'Это не первый контакт — человек уже в диалоге. Отвечай сразу по делу, как в переписке с другом.'
    )
    return await gemini_text(text, system)

# ─── REMINDER SETTINGS IN FIREBASE ──────────────────────────
def get_reminder_settings(player: str) -> list[int]:
    try:
        ref = db.reference(f'/sekta/reminderSettings/{player}')
        val = ref.get()
        if isinstance(val, list) and all(isinstance(x, int) for x in val):
            _reminder_settings_cache[player] = val
            return val
    except Exception:
        pass
    cached = _reminder_settings_cache.get(player)
    return cached if cached is not None else [30]

def set_reminder_settings(player: str, minutes_list: list[int]):
    db.reference(f'/sekta/reminderSettings/{player}').set(sorted(minutes_list))
    _reminder_settings_cache[player] = sorted(minutes_list)

def reminder_settings_keyboard(player: str) -> InlineKeyboardMarkup:
    current = get_reminder_settings(player)
    rows = []
    for minutes, label in REMIND_OPTIONS:
        checked = minutes in current
        icon    = '✅' if checked else '⬜'
        rows.append([InlineKeyboardButton(
            f"{icon} {label}",
            callback_data=f"remtoggle:{player}:{minutes}"
        )])
    rows.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"remsave:{player}")])
    return InlineKeyboardMarkup(rows)

# ─── SMART REMINDER BACKGROUND LOOP ─────────────────────────
async def reminder_loop(app: Application):
    logger.info("⏰ Reminder loop started")
    while True:
        try:
            await check_and_send_reminders(app)
        except Exception as e:
            logger.error(f"Reminder loop error: {e}")
        await asyncio.sleep(60)

async def check_and_send_reminders(app: Application):
    ref        = db.reference('/sekta/calEvents')
    all_events = ref.get() or []
    if isinstance(all_events, dict):
        all_events = list(all_events.values())

    for player, tg_id in PLAYER_TG.items():
        now          = now_for(player)
        settings     = get_reminder_settings(player)
        today_str    = now.date().isoformat()
        tomorrow_str = (now.date() + timedelta(days=1)).isoformat()

        my_events = [
            e for e in all_events
            if isinstance(e, dict)
            and e.get('owner') == player
            and e.get('date') in (today_str, tomorrow_str)
            and e.get('remindersEnabled', True)
        ]

        for ev in my_events:
            ev_date = ev.get('date', '')
            ev_time = ev.get('time', '09:00')
            try:
                ev_dt = datetime(
                    *map(int, ev_date.split('-')),
                    *map(int, ev_time.split(':')),
                    tzinfo=PLAYER_TZ[player]
                )
            except Exception:
                continue

            diff_min = (ev_dt - now).total_seconds() / 60

            for remind_min in settings:
                key = (ev.get('id', ev['title']), remind_min)
                if key in sent_reminders:
                    continue
                if abs(diff_min - remind_min) < 1.5:
                    label = next((l for m, l in REMIND_OPTIONS if m == remind_min), f'{remind_min} мин')
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Понял", callback_data="remack:ok"),
                        InlineKeyboardButton("🔕 Откл для события", callback_data=f"remoff:{ev.get('id','')}"),
                    ]])
                    await app.bot.send_message(
                        chat_id=tg_id,
                        text=(
                            f"🔔 Напоминание!\n\n"
                            f"*{ev['title']}*\n"
                            f"📅 {ev_date} в {ev_time}\n"
                            f"⏰ Через {label}"
                        ),
                        parse_mode='Markdown',
                        reply_markup=keyboard,
                    )
                    sent_reminders.add(key)

# ─── HABIT REMINDERS THROUGHOUT THE DAY ──────────────────────
def get_habit_reminder_enabled(player: str) -> bool:
    try:
        val = db.reference(f'/sekta/habitReminderEnabled/{player}').get()
        if isinstance(val, bool):
            _habit_reminder_enabled_cache[player] = val
            return val
    except Exception:
        pass
    return _habit_reminder_enabled_cache.get(player, True)

def set_habit_reminder_enabled(player: str, enabled: bool):
    db.reference(f'/sekta/habitReminderEnabled/{player}').set(enabled)
    _habit_reminder_enabled_cache[player] = enabled

def get_today_habit_status(player: str) -> dict[str, bool]:
    """Возвращает {habit_id: сделано_ли_сегодня} для игрока."""
    today   = now_for(player).date()
    day_idx = today.weekday()
    monday  = today - timedelta(days=today.weekday())
    week_key = monday.strftime("%Y-%m-%d")

    weekly = db.reference(f'/sekta/weekly/{week_key}/{player}').get() or {}
    status = {}
    for habit_id in ALL_HABITS:
        arr = weekly.get(habit_id) if isinstance(weekly, dict) else None
        status[habit_id] = bool(isinstance(arr, list) and len(arr) > day_idx and arr[day_idx])
    return status

def habits_checklist_keyboard(player: str) -> InlineKeyboardMarkup:
    """Клавиатура-чеклист: тап по привычке отмечает её (или запускает нужный шаг: фото/страница/благодарность)."""
    status = get_today_habit_status(player)
    rows = []
    for habit_id, (name, _) in ALL_HABITS.items():
        done = status.get(habit_id, False)
        icon = '✅' if done else '⬜'
        cb   = 'habit_noop' if done else f'habitmark:{player}:{habit_id}'
        rows.append([InlineKeyboardButton(f'{icon} {name}', callback_data=cb)])
    return InlineKeyboardMarkup(rows)

async def habit_reminder_loop(app: Application):
    logger.info("⏰ Habit reminder loop started")
    while True:
        try:
            await check_and_send_habit_reminders(app)
        except Exception as e:
            logger.error(f"Habit reminder loop error: {e}")
        await asyncio.sleep(60)

async def check_and_send_habit_reminders(app: Application):
    for player, tg_id in PLAYER_TG.items():
        if not get_habit_reminder_enabled(player):
            continue

        now       = now_for(player)
        today_str = now.date().isoformat()

        for slot in HABIT_REMINDER_TIMES:
            sh, sm  = map(int, slot.split(':'))
            slot_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            diff_min = (now - slot_dt).total_seconds() / 60
            if not (0 <= diff_min < 1.5):
                continue

            key = (player, today_str, slot)
            if key in _habit_reminder_sent:
                continue
            _habit_reminder_sent.add(key)

            try:
                status = get_today_habit_status(player)
            except Exception as e:
                logger.error(f"habit status read error: {e}")
                continue

            not_done = [hid for hid, done in status.items() if not done]
            is_last  = slot == HABIT_REMINDER_TIMES[-1]

            if not not_done:
                if is_last:
                    try:
                        await app.bot.send_message(
                            chat_id=tg_id,
                            text='🎉 Все привычки на сегодня закрыты! Красавчик 💪',
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"Habit reminder (done) send error: {e}")
                continue

            names = [ALL_HABITS[h][0] for h in not_done]
            lines = '\n'.join(f'• {n}' for n in names)

            if is_last:
                header = '⏰ *Финальное напоминание!*'
                footer = 'День скоро закончится — успей закрыть 🔥'
            else:
                header = '⏰ *Не забудь про привычки*'
                footer = 'Напиши что сделал(а), и я поставлю галочку ✅'

            try:
                await app.bot.send_message(
                    chat_id=tg_id,
                    text=f'{header}\n\nЕщё не отмечено сегодня:\n{lines}\n\n{footer}',
                    parse_mode='Markdown'
                )
                logger.info(f"🔔 Habit reminder sent to {player} at {slot}: {not_done}")
            except Exception as e:
                logger.error(f"Habit reminder send error: {e}")

# ─── COMMANDS ────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке Секты.')
        return
    player = USERS[uid]
    name   = PLAYER_NAMES[player]
    gemini_status = '✅ Gemini подключён' if GEMINI_API_KEY else '⚠️ Gemini не настроен (только regex-парсинг)'
    await update.message.reply_text(
        f'👋 Привет, {name}!\n\n'
        f'Я бот Секты. Умею:\n'
        f'• Добавлять события в календарь текстом или голосом\n'
        f'• Отвечать на вопросы (Gemini AI)\n'
        f'• Напоминать о событиях\n\n'
        f'{gemini_status}\n\n'
        f'Просто напиши или отправь голосовое 🎤',
        parse_mode='Markdown'
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '📖 *Команды:*\n\n'
        '/list — все события\n'
        '/today — события сегодня\n'
        '/remind — настроить напоминания о событиях\n'
        '/habits — чек-лист привычек сегодня (жми кнопки, чтобы отметить)\n'
        '/habitremind — вкл/выкл напоминания о привычках в течение дня\n\n'
        '*Привычки:*\n'
        '• /habits — покажет все привычки с кнопками, тапни чтобы отметить\n'
        '• Или просто напиши что сделал(а) текстом/голосом\n'
        '• Тренировка/Контент/10к шагов/Лекция — попрошу фото-доказательство\n'
        '• Чтение книг — напиши номер страницы, я сохраню прогресс на сайте\n'
        '• Благодарности — просто напиши, за что благодарен — сохраню и отмечу\n\n'
        '*Создание событий:*\n'
        '• Текстом: `завтра в 14 тренировка`\n'
        '• Голосом: отправь голосовое сообщение 🎤\n\n'
        '*Gemini AI:*\n'
        'Задай любой вопрос — отвечу через AI',
        parse_mode='Markdown'
    )

async def list_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return
    player = USERS[uid]
    ref    = db.reference('/sekta/calEvents')
    events = ref.get() or []
    if isinstance(events, dict):
        events = list(events.values())

    my = [
        e for e in events
        if isinstance(e, dict) and e.get('owner') == player
    ]
    my.sort(key=lambda e: (e.get('date',''), e.get('time','')))

    if not my:
        await update.message.reply_text('📭 У тебя нет событий.')
        return

    lines = ['📅 *Твои события:*\n']
    for e in my[:15]:
        d = e.get('date','')
        t = e.get('time','')
        lines.append(f"• *{e['title']}* — {d} в {t}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def today_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return
    player    = USERS[uid]
    today_str = now_for(player).date().isoformat()
    ref       = db.reference('/sekta/calEvents')
    events    = ref.get() or []
    if isinstance(events, dict):
        events = list(events.values())

    today_ev = [
        e for e in events
        if isinstance(e, dict)
        and e.get('owner') == player
        and e.get('date') == today_str
    ]
    today_ev.sort(key=lambda e: e.get('time',''))

    if not today_ev:
        await update.message.reply_text('✅ Сегодня событий нет.')
        return

    lines = ['📅 *События сегодня:*\n']
    for e in today_ev:
        lines.append(f"• *{e['title']}* в {e.get('time','?')}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def remind_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return
    player = USERS[uid]
    await update.message.reply_text(
        '🔔 Когда напоминать о событиях?',
        reply_markup=reminder_settings_keyboard(player)
    )

async def habits_today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return
    player = USERS[uid]
    try:
        status = get_today_habit_status(player)
    except Exception as e:
        logger.error(f"habits_today_cmd error: {e}")
        await update.message.reply_text('⚠️ Не удалось получить статус привычек.')
        return

    lines = []
    for habit_id, (name, _) in ALL_HABITS.items():
        icon = '✅' if status.get(habit_id) else '⬜'
        lines.append(f'{icon} {name}')

    done_count  = sum(1 for v in status.values() if v)
    total_count = len(status)
    await update.message.reply_text(
        f'📋 *Привычки сегодня* ({done_count}/{total_count}):\n\n' + '\n'.join(lines) +
        '\n\nЖми на привычку ниже, чтобы отметить её 👇',
        parse_mode='Markdown',
        reply_markup=habits_checklist_keyboard(player)
    )

async def habitremind_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return
    player  = USERS[uid]
    enabled = get_habit_reminder_enabled(player)
    status_str = 'включены ✅' if enabled else 'выключены ⛔'
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            '🔕 Выключить' if enabled else '🔔 Включить',
            callback_data=f'habitremind_toggle:{player}'
        )
    ]])
    await update.message.reply_text(
        f'🔔 Напоминания о привычках в течение дня сейчас {status_str}\n\n'
        f'Проверяю в {", ".join(HABIT_REMINDER_TIMES)} — если что-то ещё не отмечено, пришлю список.',
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

# ─── VOICE HANDLER ───────────────────────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке Секты.')
        return

    player = USERS[uid]

    if not GROQ_API_KEY:
        await update.message.reply_text(
            '⚠️ Whisper API не настроен. Добавь GROQ_API_KEY в переменные окружения.'
        )
        return

    try:
        voice_file = await ctx.bot.get_file(update.message.voice.file_id)
        ogg_bytes  = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f'Voice download error: {e}')
        await update.message.reply_text('❌ Не удалось скачать голосовое.')
        return

    # Распознаём речь — без промежуточных сообщений
    transcript = await whisper_transcribe(bytes(ogg_bytes))

    if not transcript:
        await update.message.reply_text(
            '🤔 Не смог распознать речь. Попробуй ещё раз или напиши текстом.'
        )
        return

    # Сразу обрабатываем как текст — никаких «Распознано:»
    await process_user_text(update, uid, player, transcript)

# ─── TEXT MESSAGE HANDLER ────────────────────────────────────
async def handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return

    text   = update.message.text.strip()
    player = USERS[uid]

    # Ждём номер страницы для привычки "чтение"
    if uid in waiting_for_page:
        m = re.search(r'(\d+)', text)
        if not m:
            await update.message.reply_text('Не понял номер страницы. Напиши просто число, например: 145')
            return
        waiting_for_page.pop(uid, None)
        page = int(m.group(1))
        ok = save_reading_page(player, page) and mark_habit_done(player, 'reading')
        if ok:
            await update.message.reply_text(
                f'📖 *Чтение книг* — отмечено!\nСтраница {page} сохранена на сайте.',
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text('⚠️ Не удалось сохранить страницу. Попробуй позже.')
        return

    # Ждём текст благодарности
    if uid in waiting_for_gratitude:
        waiting_for_gratitude.pop(uid, None)
        ok = save_gratitude_text(player, text) and mark_habit_done(player, 'gratitude')
        if ok:
            await update.message.reply_text(
                '🙏 *Благодарности* — записано и отмечено!\nТекст сохранён на сайте.',
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text('⚠️ Не удалось сохранить благодарность. Попробуй позже.')
        return

    # Если есть незакрытый диалог (pending)
    if uid in pending:
        state = pending[uid]
        if isinstance(state, dict) and 'step' in state:
            if state['step'] == 'date':
                d = resolve_date(text)
                if not d:
                    await update.message.reply_text('Не понял дату. Напиши: завтра / 03.07 / сегодня')
                    return
                state['parsed']['date'] = d
                state['step'] = 'time'
                await update.message.reply_text(
                    f'⏰ В какое время {d.strftime("%d.%m")}?\nНапиши: `в 14` или `14:30`',
                    parse_mode='Markdown'
                )
                return
            if state['step'] == 'time':
                t = resolve_time(text)
                if not t:
                    await update.message.reply_text('Не понял время. Напиши: `в 14` или `14:30`', parse_mode='Markdown')
                    return
                state['parsed']['time'] = t
                del pending[uid]
                await ask_confirm(update, uid, state['parsed'])
                return
        else:
            # pending содержит подтверждение — продолжаем
            return await handle_message(update, ctx)

    await handle_message(update, ctx)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке пользователей Секты.')
        return

    text   = update.message.text.strip()
    player = USERS[uid]
    await process_user_text(update, uid, player, text)

async def process_user_text(update: Update, uid: int, player: str, text: str):
    """
    Общий пайплайн:
    1. Проверяем на упоминание привычки → ставим галочку / просим фото
    2. Проверяем намерение добавить событие в календарь
    3. Свободный разговор через Gemini
    """
    # 1. Привычки
    handled = await handle_habit_report(update, uid, player, text)
    if handled:
        return

    # 2. Событие в календарь
    result = await gemini_classify_intent(text, player)

    if not result.get('is_event') or not result.get('event'):
        # Не похоже на просьбу о событии — обычный разговор
        ai_reply = await gemini_chat(text, player)
        if ai_reply:
            await update.message.reply_text(f'🤖 {ai_reply}')
        else:
            await update.message.reply_text(
                '🤔 Не понял. Напиши например:\n`завтра в 14 тренировка`\n\nИли задай любой вопрос.',
                parse_mode='Markdown'
            )
        return

    parsed = result['event']

    if not parsed.get('date'):
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'date'}
        await update.message.reply_text(
            f'📅 На какую дату поставить *«{parsed["title"]}»*?\n'
            'Напиши: завтра / сегодня / послезавтра / 03.07',
            parse_mode='Markdown'
        )
        return

    if not parsed.get('time'):
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'time'}
        d = parsed['date'].strftime('%d.%m')
        await update.message.reply_text(
            f'⏰ В какое время {d}?\nНапиши например: `в 14` или `в 9:30`',
            parse_mode='Markdown'
        )
        return

    await ask_confirm(update, uid, parsed)

async def ask_confirm(update: Update, uid: int, parsed: dict):
    pending[uid] = parsed
    d       = parsed['date'].strftime('%d.%m.%Y')
    t       = parsed['time']
    dur     = parsed['duration']
    title   = parsed['title']
    dur_str = f"{dur//60}ч {dur%60}мин" if dur % 60 else f"{dur//60}ч" if dur >= 60 else f"{dur}мин"

    player    = USERS[uid]
    settings  = get_reminder_settings(player)
    rem_labels = [l for m, l in REMIND_OPTIONS if m in settings]
    rem_str   = ', '.join(rem_labels) if rem_labels else 'без напоминаний'

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Добавить", callback_data=f"confirm:{uid}"),
        InlineKeyboardButton("❌ Отмена",   callback_data=f"cancel:{uid}"),
    ]])

    await update.message.reply_text(
        f'📋 Добавить в календарь?\n\n'
        f'*{title}*\n'
        f'📅 {d} в {t}\n'
        f'⏱ {dur_str}\n'
        f'🔔 Напомнить: {rem_str}',
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

# ─── CALLBACK HANDLER ────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith('remtoggle:'):
        _, player, min_str = data.split(':')
        minutes = int(min_str)
        current = list(get_reminder_settings(player))
        if minutes in current: current.remove(minutes)
        else:                  current.append(minutes)
        set_reminder_settings(player, current)
        await query.edit_message_reply_markup(
            reply_markup=reminder_settings_keyboard(player)
        )
        return

    if data.startswith('remsave:'):
        player  = data.split(':')[1]
        current = get_reminder_settings(player)
        labels  = [l for m, l in REMIND_OPTIONS if m in current]
        cur_str = ', '.join(labels) if labels else 'не выбрано'
        await query.edit_message_text(
            f'✅ Сохранено!\n\n🔔 Напоминать: {cur_str}',
            parse_mode='Markdown'
        )
        return

    if data.startswith('remack:'):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data.startswith('remoff:'):
        ev_id = data.split(':')[1]
        try:
            ref    = db.reference('/sekta/calEvents')
            events = ref.get() or []
            if isinstance(events, dict):
                events = list(events.values())
            for i, e in enumerate(events):
                if isinstance(e, dict) and e.get('id') == ev_id:
                    events[i]['remindersEnabled'] = False
                    break
            ref.set(events)
        except Exception as exc:
            logger.error(f"remoff error: {exc}")
        await query.edit_message_text('🔕 Напоминание отключено.')
        return

    if data == 'habit_noop':
        await query.answer('Уже отмечено ✅')
        return

    if data.startswith('habitmark:'):
        _, player, habit_id = data.split(':', 2)
        uid = query.from_user.id
        if USERS.get(uid) != player:
            await query.answer('❌ Это не твой чек-лист')
            return

        status = get_today_habit_status(player)
        if status.get(habit_id):
            await query.answer('Уже отмечено ✅')
            return

        name = ALL_HABITS[habit_id][0]

        if habit_id in HABITS_NEED_PROOF:
            waiting_for_proof[uid] = habit_id
            await query.answer()
            await query.message.reply_text(
                f'💪 *{name}*\n\n{PROOF_PROMPTS.get(habit_id, "📸 Пришли доказательство")}',
                parse_mode='Markdown'
            )
            return

        if habit_id == 'reading':
            waiting_for_page[uid] = True
            await query.answer()
            await query.message.reply_text('📖 На какой ты сейчас странице? Просто напиши число.')
            return

        if habit_id == 'gratitude':
            waiting_for_gratitude[uid] = True
            await query.answer()
            await query.message.reply_text('🙏 Напиши, за что ты благодарен сегодня.')
            return

        # Привычка без доказательств — отмечаем сразу и обновляем чек-лист
        ok = mark_habit_done(player, habit_id)
        if ok:
            await query.answer('✅ Отмечено!')
            try:
                await query.edit_message_reply_markup(reply_markup=habits_checklist_keyboard(player))
            except Exception:
                pass
        else:
            await query.answer('⚠️ Ошибка, попробуй позже')
        return

    if data.startswith('habitremind_toggle:'):
        player  = data.split(':')[1]
        enabled = get_habit_reminder_enabled(player)
        set_habit_reminder_enabled(player, not enabled)
        new_status = 'включены ✅' if not enabled else 'выключены ⛔'
        await query.edit_message_text(f'🔔 Напоминания о привычках теперь {new_status}')
        return

    if data.startswith('dayoff_approve:'):
        do_id = data.split(':', 1)[1]
        await handle_dayoff_decision(query, 'approved', do_id)
        return

    if data.startswith('dayoff_reject:'):
        do_id = data.split(':', 1)[1]
        await handle_dayoff_decision(query, 'rejected', do_id)
        return

    if data.startswith('confirm:'):
        uid    = int(data.split(':')[1])
        parsed = pending.pop(uid, None)
        if not parsed:
            await query.edit_message_text('⚠️ Данные устарели, попробуй снова.')
            return
        player = USERS[uid]
        try:
            write_event_to_firebase(player, parsed)
            d          = parsed['date'].strftime('%d.%m.%Y')
            t          = parsed['time']
            settings   = get_reminder_settings(player)
            rem_labels = [l for m, l in REMIND_OPTIONS if m in settings]
            rem_str    = ', '.join(rem_labels) if rem_labels else 'без напоминаний'
            await query.edit_message_text(
                f'✅ Добавлено!\n\n*{parsed["title"]}*\n'
                f'📅 {d} в {t}\n🔔 Напомню: {rem_str}\n\n'
                f'Событие уже в календаре 🗓',
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Firebase write error: {e}")
            await query.edit_message_text(f'❌ Ошибка записи: {e}')
        return

    if data.startswith('cancel:'):
        uid = int(data.split(':')[1])
        pending.pop(uid, None)
        await query.edit_message_text('❌ Отменено.')
        return

# ─── NOTIFICATIONS: REVIEWS & DAYOFFS ────────────────────────
# Храним уже отправленные уведомления чтобы не спамить
_notified_reviews: set[str] = set()
_notified_dayoffs: set[str] = set()

async def notifications_loop(app: Application):
    """Фоновый цикл — проверяет Firebase каждые 15 секунд."""
    logger.info("🔔 Notifications loop started")
    await asyncio.sleep(10)  # ждём пока бот поднимется
    while True:
        try:
            await check_reviews_notify(app)
            await check_dayoffs_notify(app)
        except Exception as e:
            logger.error(f"Notifications loop error: {e}")
        await asyncio.sleep(15)

async def check_reviews_notify(app: Application):
    """
    Проверяет новые pending отчёты.
    Когда партнёр отправил отчёт на проверку — уведомляем второго участника.
    """
    try:
        ref     = db.reference('/sekta/reviews')
        reviews = ref.get() or []
        if isinstance(reviews, dict):
            reviews = list(reviews.values())
    except Exception as e:
        logger.error(f"check_reviews_notify read error: {e}")
        return

    for r in reviews:
        if not isinstance(r, dict):
            continue
        rev_id = r.get('id', '')
        status = r.get('status', '')
        sender = r.get('from', '')  # кто отправил отчёт

        if not rev_id or status != 'pending':
            continue
        if rev_id in _notified_reviews:
            continue

        # Уведомляем партнёра (не того кто отправил)
        reviewer = 'bogdan' if sender == 'artem' else 'artem'
        reviewer_tg = PLAYER_TG.get(reviewer)
        if not reviewer_tg:
            continue

        sender_name   = PLAYER_NAMES.get(sender, sender)
        reviewer_name = PLAYER_NAMES.get(reviewer, reviewer)
        rev_date      = r.get('date', '?')

        # Считаем сколько выполнено
        habits = r.get('habits', {})
        done_count  = sum(1 for v in habits.values() if v)
        total_count = len(habits)
        pct = round(done_count / total_count * 100) if total_count else 0

        try:
            await app.bot.send_message(
                chat_id=reviewer_tg,
                text=(
                    f'📋 *{sender_name} отправил отчёт на проверку!*\n\n'
                    f'📅 Дата: {rev_date}\n'
                    f'✅ Выполнено: {done_count}/{total_count} привычек ({pct}%)\n\n'
                    f'Открой сайт → раздел «Проверка» чтобы принять или отклонить.'
                ),
                parse_mode='Markdown'
            )
            _notified_reviews.add(rev_id)
            logger.info(f"📩 Review notification sent to {reviewer} for review {rev_id}")
        except Exception as e:
            logger.error(f"Review notify send error: {e}")

    # Уведомить отправителя о результате проверки
    for r in reviews:
        if not isinstance(r, dict):
            continue
        rev_id = r.get('id', '')
        status = r.get('status', '')
        sender = r.get('from', '')

        if not rev_id or status not in ('approved', 'rejected'):
            continue

        result_key = f"result_{rev_id}"
        if result_key in _notified_reviews:
            continue

        sender_tg = PLAYER_TG.get(sender)
        if not sender_tg:
            continue

        reviewer     = 'bogdan' if sender == 'artem' else 'artem'
        reviewer_name = PLAYER_NAMES.get(reviewer, reviewer)
        rev_date      = r.get('date', '?')
        comment       = r.get('reviewComment', '')

        if status == 'approved':
            emoji = '✅'
            status_text = 'принят'
        else:
            emoji = '❌'
            status_text = 'отклонён'

        text = (
            f'{emoji} *Твой отчёт {status_text}!*\n\n'
            f'📅 {rev_date} — {reviewer_name} проверил\n'
        )
        if comment:
            text += f'💬 Комментарий: {comment}'

        try:
            await app.bot.send_message(
                chat_id=sender_tg,
                text=text,
                parse_mode='Markdown'
            )
            _notified_reviews.add(result_key)
            logger.info(f"📩 Review result notification sent to {sender} — {status}")
        except Exception as e:
            logger.error(f"Review result notify error: {e}")

async def check_dayoffs_notify(app: Application):
    """
    Проверяет новые отгулы.
    Когда кто-то подал отгул — уведомляем партнёра с кнопками одобрить/отклонить прямо в TG.
    Когда партнёр принял решение — уведомляем подателя.
    """
    try:
        ref     = db.reference('/sekta/dayoffs')
        dayoffs = ref.get() or []
        if isinstance(dayoffs, dict):
            dayoffs = list(dayoffs.values())
    except Exception as e:
        logger.error(f"check_dayoffs_notify read error: {e}")
        return

    for d in dayoffs:
        if not isinstance(d, dict):
            continue
        do_id  = d.get('id', '')
        status = d.get('status', '')
        sender = d.get('from', '')

        if not do_id:
            continue

        # ── Новый pending отгул — уведомить партнёра ──────────
        if status == 'pending' and do_id not in _notified_dayoffs:
            reviewer    = 'bogdan' if sender == 'artem' else 'artem'
            reviewer_tg = PLAYER_TG.get(reviewer)
            if not reviewer_tg:
                continue

            sender_name = PLAYER_NAMES.get(sender, sender)
            do_date     = d.get('date', '?')
            do_reason   = d.get('reason', '—')
            do_type     = '🛡️ Форс-мажор' if d.get('type') == 'forcemajeure' else '🌙 Передышка'

            skip_habits = d.get('skipHabits', [])
            keep_habits = d.get('keepHabits', [])

            HABIT_NAMES = {
                'wakeup': 'Подъём', 'workout': 'Тренировка', 'nosugar': 'NO SUGAR',
                'devices': 'Устройства', 'content': 'Контент', 'lecture': 'Лекции',
                'gratitude': 'Благодарности', 'protein': 'Протеин', 'nmt': 'НМТ',
                'sleep': 'Отбой', 'water': 'Вода', 'reading': 'Чтение', 'steps': '10к шагов',
            }

            skip_str = ', '.join(HABIT_NAMES.get(h, h) for h in skip_habits) or '—'
            keep_str = ', '.join(HABIT_NAMES.get(h, h) for h in keep_habits) or 'все'

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ Одобрить',   callback_data=f'dayoff_approve:{do_id}'),
                InlineKeyboardButton('❌ Отклонить',  callback_data=f'dayoff_reject:{do_id}'),
            ]])

            try:
                await app.bot.send_message(
                    chat_id=reviewer_tg,
                    text=(
                        f'⚖️ *{sender_name} просит отгул!*\n\n'
                        f'{do_type}\n'
                        f'📅 Дата: {do_date}\n'
                        f'📝 Причина: {do_reason}\n'
                        f'✅ Выполнит: {keep_str}\n'
                        f'❌ Пропустит: {skip_str}\n\n'
                        f'Ответь прямо здесь или на сайте.'
                    ),
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )
                _notified_dayoffs.add(do_id)
                logger.info(f"📩 Dayoff notification sent to {reviewer} for {do_id}")
            except Exception as e:
                logger.error(f"Dayoff notify send error: {e}")

        # ── Решение по отгулу — уведомить подателя ────────────
        result_key = f"result_{do_id}"
        if status in ('approved', 'rejected', 'auto-rejected') and result_key not in _notified_dayoffs:
            sender_tg = PLAYER_TG.get(sender)
            if not sender_tg:
                continue

            reviewer      = 'bogdan' if sender == 'artem' else 'artem'
            reviewer_name = PLAYER_NAMES.get(reviewer, reviewer)
            do_date       = d.get('date', '?')
            comment       = d.get('reviewComment', '')

            if status == 'approved':
                emoji = '✅'
                text  = f'✅ *Отгул одобрен!*\n\n📅 {do_date} — {reviewer_name} одобрил\nЖизнь не снимается 🛡️'
            elif status == 'auto-rejected':
                emoji = '⏰'
                text  = f'⏰ *Отгул автоотклонён!*\n\n📅 {do_date}\n{reviewer_name} не успел ответить до 23:30\nЖизнь снимается по обычным правилам.'
            else:
                emoji = '❌'
                text  = f'❌ *Отгул отклонён*\n\n📅 {do_date} — {reviewer_name} отклонил'
                if comment:
                    text += f'\n💬 {comment}'

            try:
                await app.bot.send_message(
                    chat_id=sender_tg,
                    text=text,
                    parse_mode='Markdown'
                )
                _notified_dayoffs.add(result_key)
                logger.info(f"📩 Dayoff result notification sent to {sender} — {status}")
            except Exception as e:
                logger.error(f"Dayoff result notify error: {e}")

# ─── DAYOFF APPROVE/REJECT via Telegram ──────────────────────
async def handle_dayoff_decision(query, action: str, do_id: str):
    """Обрабатывает нажатие кнопок одобрить/отклонить отгул прямо в Telegram."""
    uid      = query.from_user.id
    reviewer = USERS.get(uid)
    if not reviewer:
        await query.answer('❌ Нет доступа')
        return

    try:
        ref     = db.reference('/sekta/dayoffs')
        dayoffs = ref.get() or []
        if isinstance(dayoffs, dict):
            dayoffs = list(dayoffs.values())

        target_idx = next((i for i, d in enumerate(dayoffs) if isinstance(d, dict) and d.get('id') == do_id), None)
        if target_idx is None:
            await query.answer('⚠️ Отгул не найден')
            return

        dayoff = dayoffs[target_idx]

        # Проверяем что это не свой отгул
        if dayoff.get('from') == reviewer:
            await query.answer('❌ Нельзя одобрить свой отгул')
            return

        if dayoff.get('status') != 'pending':
            await query.answer('⚠️ Этот отгул уже рассмотрен')
            await query.edit_message_reply_markup(reply_markup=None)
            return

        dayoffs[target_idx]['status']      = action
        dayoffs[target_idx]['reviewedAt']  = datetime.now(TZ_ARTEM).isoformat()
        dayoffs[target_idx]['reviewedBy']  = reviewer
        ref.set(dayoffs)

        sender_name   = PLAYER_NAMES.get(dayoff.get('from', ''), '?')
        reviewer_name = PLAYER_NAMES.get(reviewer, reviewer)
        do_date       = dayoff.get('date', '?')

        if action == 'approved':
            result_text = f'✅ Ты одобрил отгул {sender_name} на {do_date}'
        else:
            result_text = f'❌ Ты отклонил отгул {sender_name} на {do_date}'

        await query.edit_message_text(result_text, parse_mode='Markdown')
        await query.answer('Готово!')

        logger.info(f"⚖️ Dayoff {do_id} {action} by {reviewer} via Telegram")

    except Exception as e:
        logger.error(f"handle_dayoff_decision error: {e}")
        await query.answer(f'Ошибка: {e}')

# ─── MAIN ─────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start',  start))
    app.add_handler(CommandHandler('help',   help_cmd))
    app.add_handler(CommandHandler('list',   list_events))
    app.add_handler(CommandHandler('today',  today_events))
    app.add_handler(CommandHandler('remind', remind_settings))
    app.add_handler(CommandHandler('habits', habits_today_cmd))
    app.add_handler(CommandHandler('habitremind', habitremind_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Фото — доказательства привычек
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))
    # Голосовые сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending))

    async def post_init(application: Application):
        asyncio.create_task(reminder_loop(application))
        asyncio.create_task(notifications_loop(application))
        asyncio.create_task(habit_reminder_loop(application))

    app.post_init = post_init

    logger.info('🤖 Sekta bot started — Gemini intent + Whisper voice enabled')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

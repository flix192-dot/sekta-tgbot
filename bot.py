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

GEMINI_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-1.5-flash:generateContent?key={key}'
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

# ─── GEMINI: КЛАССИФИКАЦИЯ НАМЕРЕНИЯ (событие или просто разговор) ──
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
        f'Тебя зовут Секта-бот. Ты общаешься с {name}. '
        'Ты помогаешь с вопросами о дисциплине, планировании, мотивации и жизни. '
        'Отвечай коротко, по делу, на русском языке. Максимум 3-4 предложения.'
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
        '/remind — настроить напоминания\n\n'
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

# ─── VOICE HANDLER ───────────────────────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке Секты.')
        return

    player = USERS[uid]
    await update.message.reply_text('🎤 Слушаю...')

    if not GROQ_API_KEY:
        await update.message.reply_text(
            '⚠️ Whisper API не настроен. Добавь GROQ_API_KEY в переменные окружения.'
        )
        return

    try:
        # Скачиваем OGG файл от Telegram
        voice_file = await ctx.bot.get_file(update.message.voice.file_id)
        ogg_bytes  = await voice_file.download_as_bytearray()
    except Exception as e:
        logger.error(f'Voice download error: {e}')
        await update.message.reply_text('❌ Не удалось скачать голосовое.')
        return

    # Распознаём речь через Whisper API
    transcript = await whisper_transcribe(bytes(ogg_bytes))

    if not transcript:
        await update.message.reply_text(
            '🤔 Не смог распознать речь. Попробуй ещё раз или напиши текстом.'
        )
        return

    # Показываем транскрипт
    await update.message.reply_text(f'🗣 *Распознано:* {transcript}', parse_mode='Markdown')

    # Дальше — тот же пайплайн анализа намерения, что и для текста
    await process_user_text(update, uid, player, transcript)

# ─── TEXT MESSAGE HANDLER ────────────────────────────────────
async def handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        return

    text = update.message.text.strip()

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
    Общий пайплайн для текстовых сообщений и расшифрованных голосовых:
    1. ИИ анализирует намерение — это просьба запланировать событие или нет.
    2. Если да — данные передаются в уже готовый код (parse/write_event_to_firebase),
       модель сама календарь не трогает и не пишет в него напрямую.
    3. Если нет — бот просто общается на тему дисциплины через gemini_chat.
    """
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

# ─── MAIN ────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start',  start))
    app.add_handler(CommandHandler('help',   help_cmd))
    app.add_handler(CommandHandler('list',   list_events))
    app.add_handler(CommandHandler('today',  today_events))
    app.add_handler(CommandHandler('remind', remind_settings))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Голосовые сообщения
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    # Текстовые сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending))

    async def post_init(application: Application):
        asyncio.create_task(reminder_loop(application))

    app.post_init = post_init

    logger.info('🤖 Sekta bot started — Gemini intent + Whisper voice enabled')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

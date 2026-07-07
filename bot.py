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
BOT_TOKEN      = os.environ['BOT_TOKEN']
FIREBASE_URL   = os.environ['FIREBASE_URL']
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GROQ_API_KEY   = os.environ.get('GROQ_API_KEY', '')

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
    'gemini-2.0-flash-lite:generateContent?key={key}'
)
WHISPER_URL = 'https://api.groq.com/openai/v1/audio/transcriptions'

# ─── FIREBASE INIT ───────────────────────────────────────────
firebase_creds = json.loads(os.environ['FIREBASE_CREDS'])
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})

# ─── IN-MEMORY STATE ─────────────────────────────────────────
sent_reminders: set[tuple]          = set()
_reminder_settings_cache: dict      = {}
pending: dict[int, dict]            = {}

# ─── TIME HELPERS ────────────────────────────────────────────
def now_for(player: str) -> datetime:
    return datetime.now(PLAYER_TZ[player])

def now_local() -> datetime:
    return datetime.now(TZ_ARTEM)

# ─── DATE/TIME PARSERS ───────────────────────────────────────
def resolve_date(text: str, player: str = 'artem') -> date | None:
    text = text.lower().strip()
    today = now_for(player).date()
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
        return int(val * 60) if 'час' in m.group(2) or m.group(2) == 'ч' else int(val)
    return 60

def parse_event(text: str, player: str = 'artem') -> dict | None:
    text = re.sub(r'^/\w+\s*', '', text).strip()
    text_clean = re.sub(
        r'^(поставь|добавь|создай|запланируй|напомни|запиши)\s+(задачу|событие|встречу|дело|напоминание)?\s*',
        '', text, flags=re.IGNORECASE
    ).strip()
    event_date = resolve_date(text_clean, player)
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
    if isinstance(current, dict): current = list(current.values())
    event_date = event['date']
    event_time = event['time'] or '09:00'
    date_str   = event_date.strftime('%Y-%m-%d') if event_date else ''
    h, m     = map(int, event_time.split(':'))
    end_min  = h * 60 + m + event['duration']
    end_time = f"{(end_min // 60) % 24:02d}:{end_min % 60:02d}"
    ev_id    = f"ev_{int(time.time() * 1000)}"
    new_event = {
        'id': ev_id, 'title': event['title'], 'date': date_str,
        'time': event_time, 'endTime': end_time,
        'color': '#c8a96e' if player_id == 'artem' else '#4caf7d',
        'owner': player_id, 'sourceType': 'bot', 'remindersEnabled': True,
    }
    current.append(new_event)
    ref.set(current)
    return ev_id

# ─── GEMINI ──────────────────────────────────────────────────
async def gemini_text(prompt: str, system: str = '') -> str:
    if not GEMINI_API_KEY: return ''
    url  = GEMINI_URL.format(key=GEMINI_API_KEY)
    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.3, 'maxOutputTokens': 800},
    }
    if system:
        body['systemInstruction'] = {'parts': [{'text': system}]}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        logger.error(f'Gemini error: {e}')
        return ''

# ─── WHISPER ─────────────────────────────────────────────────
async def whisper_transcribe(ogg_bytes: bytes) -> str:
    if not GROQ_API_KEY: return ''
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                WHISPER_URL,
                headers={'Authorization': f'Bearer {GROQ_API_KEY}'},
                data={'model': 'whisper-large-v3-turbo', 'language': 'ru'},
                files={'file': ('voice.ogg', bytes(ogg_bytes), 'audio/ogg')}
            )
            r.raise_for_status()
            return (r.json().get('text') or '').strip()
    except Exception as e:
        logger.error(f'Whisper error: {e}')
        return ''

# ─── HABITS ──────────────────────────────────────────────────
HABITS_NEED_PROOF = {
    'workout':   ('тренировка',    ['потренировался', 'тренировка', 'зал', 'качалка', 'жим', 'кардио']),
    'content':   ('контент',       ['контент', 'посмотрел видео', 'ютуб', 'рилс']),
    'gratitude': ('благодарности', ['благодарности', 'благодарность']),
    'steps':     ('10к шагов',     ['шаги', 'шагов', 'прошёл', '10к', '10000']),
    'lecture':   ('лекция',        ['лекция', 'лекцию', 'база знаний']),
}
HABITS_NO_PROOF = {
    'wakeup':  ('ранний подъём', ['проснулся', 'встал', 'подъём']),
    'nosugar': ('без сахара',    ['без сахара', 'не ел сладкое']),
    'devices': ('устройства',    ['телефон отложил', 'без телефона']),
    'water':   ('вода',          ['выпил воду', '2.5 литра', 'норму воды']),
    'sleep':   ('ранний отбой',  ['лёг спать', 'отбой', 'заснул']),
    'reading': ('чтение',        ['почитал', 'прочитал', 'читал']),
}
ALL_HABITS = {**HABITS_NEED_PROOF, **HABITS_NO_PROOF}
waiting_for_proof: dict[int, str] = {}

def detect_habit(text: str) -> str | None:
    text_lower = text.lower()
    for hid, (_, kws) in ALL_HABITS.items():
        if any(kw in text_lower for kw in kws):
            return hid
    return None

def mark_habit_done(player: str, habit_id: str) -> bool:
    try:
        today   = datetime.now(PLAYER_TZ[player]).date()
        day_idx = today.weekday()
        monday  = today - timedelta(days=today.weekday())
        week_key = monday.strftime('%Y-%m-%d')
        path = f'/sekta/weekly/{week_key}/{player}/{habit_id}'
        ref  = db.reference(path)
        arr  = ref.get()
        if not isinstance(arr, list): arr = [False] * 7
        while len(arr) < 7: arr.append(False)
        arr[day_idx] = True
        ref.set(arr)
        return True
    except Exception as e:
        logger.error(f'mark_habit_done error: {e}')
        return False

async def handle_habit_report(update: Update, uid: int, player: str, text: str) -> bool:
    hid = detect_habit(text)
    if not hid: return False
    name = ALL_HABITS[hid][0]
    if hid in HABITS_NEED_PROOF:
        waiting_for_proof[uid] = hid
        proofs = {
            'workout': '📸 Отправь фото из зала или скрин из фитнес-приложения',
            'content': '📸 Пришли скрин просмотра',
            'gratitude': '📸 Фото или скрин с записями',
            'steps': '📸 Скрин из приложения шагов',
            'lecture': '📸 Скрин прогресса лекции',
        }
        await update.message.reply_text(
            f'💪 *{name}* — молодец!\n\n{proofs.get(hid, "📸 Пришли доказательство")}',
            parse_mode='Markdown'
        )
    else:
        ok = mark_habit_done(player, hid)
        await update.message.reply_text(
            f'✅ *{name}* — отмечено!' if ok else f'⚠️ Не удалось поставить галочку для *{name}*.',
            parse_mode='Markdown'
        )
    return True

async def handle_proof_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    player  = USERS[uid]
    caption = (update.message.caption or '').strip()
    if caption and uid not in waiting_for_proof:
        hid = detect_habit(caption)
        if hid: waiting_for_proof[uid] = hid
    if uid not in waiting_for_proof:
        await update.message.reply_text('📎 Получил фото. Напиши какую привычку хочешь отметить.')
        return
    hid  = waiting_for_proof.pop(uid)
    name = ALL_HABITS[hid][0]
    ok   = mark_habit_done(player, hid)
    await update.message.reply_text(
        f'✅ *{name}* — принято! Галочка поставлена, фото сохранено 📎' if ok
        else '⚠️ Фото получено, но не удалось поставить галочку.',
        parse_mode='Markdown'
    )

# ─── GEMINI INTENT ───────────────────────────────────────────
EVENT_TRIGGER_RE = re.compile(
    r'\b(поставь|добавь|создай|запланируй|напомни|запиши|поставить|добавить)\b',
    re.IGNORECASE
)

# Trigger for plan deletion
DELETE_PLAN_RE = re.compile(
    r'\b(удали|удалить|убери|убрать|отмени|отменить|снять|снять)\s+план\b',
    re.IGNORECASE
)

def heuristic_is_event(text: str, player: str = 'artem') -> dict | None:
    if not EVENT_TRIGGER_RE.search(text): return None
    return parse_event(text, player)

# ─── PLAN DELETION HELPERS ───────────────────────────────────
def read_player_plans(player: str) -> list[dict]:
    """Читает планы игрока из Firebase."""
    try:
        data = db.reference(f'/sekta/plans/{player}').get()
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        if isinstance(data, dict):
            return [p for p in data.values() if isinstance(p, dict)]
    except Exception as e:
        logger.error(f'read_player_plans error: {e}')
    return []

def delete_plan_in_firebase(player: str, plan_id: str) -> bool:
    """Удаляет план по id из Firebase."""
    try:
        plans = read_player_plans(player)
        updated = [p for p in plans if p.get('id') != plan_id]
        db.reference(f'/sekta/plans/{player}').set(updated)
        return True
    except Exception as e:
        logger.error(f'delete_plan_in_firebase error: {e}')
        return False

def find_plans_by_query(plans: list[dict], query: str, player: str = 'artem') -> list[dict]:
    """
    Ищет планы по: дате (dd.mm / YYYY-MM-DD), времени (HH:MM),
    части названия — или их комбинации.
    """
    query_lower = query.lower().strip()

    # Нормализуем дату из запроса если есть
    target_date: date | None = resolve_date(query_lower, player)
    target_time: str | None  = resolve_time(query_lower)

    # Слова-не-дата для поиска по названию
    title_words = re.sub(
        r'(удали|удалить|убери|план|завтра|сегодня|послезавтра|'
        r'\d{1,2}[./]\d{1,2}(?:[./]\d{4})?|в\s+\d{1,2}(?::\d{2})?'
        r'(?:\s*(?:утра|вечера|ночи|дня))?)',
        '', query_lower, flags=re.IGNORECASE
    ).strip()

    results = []
    for p in plans:
        if p.get('done'):
            continue  # не удаляем уже выполненные через бот

        p_date = p.get('date', '')   # YYYY-MM-DD
        p_time = p.get('time', '')   # HH:MM
        p_title = (p.get('title') or '').lower()

        score = 0
        if target_date and p_date == target_date.isoformat():
            score += 10
        if target_time and p_time == target_time:
            score += 10
        if title_words and title_words in p_title:
            score += 5

        if score > 0:
            results.append((score, p))

    results.sort(key=lambda x: (-x[0], x[1].get('date', ''), x[1].get('time', '')))
    return [p for _, p in results]

def plan_short_desc(p: dict) -> str:
    d = p.get('date', '')
    t = p.get('time', '')
    title = p.get('title', '?')
    try:
        dt = date.fromisoformat(d)
        d_fmt = dt.strftime('%d.%m')
    except Exception:
        d_fmt = d
    return f"*{title}* — {d_fmt} в {t}"

def plans_select_keyboard(plans: list[dict], player: str) -> InlineKeyboardMarkup:
    rows = []
    for p in plans[:8]:
        d = p.get('date', '')
        t = p.get('time', '')
        try:    d_fmt = date.fromisoformat(d).strftime('%d.%m')
        except: d_fmt = d
        label = f"{p.get('title','?')[:30]} — {d_fmt} {t}"
        rows.append([InlineKeyboardButton(label, callback_data=f"delplan:{player}:{p['id']}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="delplan_cancel")])
    return InlineKeyboardMarkup(rows)

async def handle_delete_plan(update: Update, uid: int, player: str, text: str) -> bool:
    """
    Проверяет текст на команду удаления плана.
    Возвращает True если обработал.
    """
    if not DELETE_PLAN_RE.search(text):
        return False

    plans = read_player_plans(player)
    if not plans:
        await update.message.reply_text(
            '📋 У тебя нет активных планов.',
            parse_mode='Markdown'
        )
        return True

    # Ищем совпадения
    matches = find_plans_by_query(plans, text, player)

    if not matches:
        # Совпадений нет — показываем все планы на выбор
        await update.message.reply_text(
            f'🗑 Выбери план для удаления:',
            reply_markup=plans_select_keyboard(plans, player),
            parse_mode='Markdown'
        )
        return True

    if len(matches) == 1:
        # Точное попадание — сразу спрашиваем подтверждение
        p = matches[0]
        await update.message.reply_text(
            f'🗑 Удалить этот план?\n\n{plan_short_desc(p)}',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"delplan:{player}:{p['id']}"),
                InlineKeyboardButton("❌ Нет",         callback_data="delplan_cancel"),
            ]]),
            parse_mode='Markdown'
        )
        return True

    # Несколько совпадений — показываем список
    await update.message.reply_text(
        f'🗑 Нашёл {len(matches)} совпадения. Какой план удалить?',
        reply_markup=plans_select_keyboard(matches, player),
        parse_mode='Markdown'
    )
    return True

async def gemini_classify_intent(text: str, player: str) -> dict:
    if not GEMINI_API_KEY:
        parsed = heuristic_is_event(text, player)
        return {'is_event': bool(parsed), 'event': parsed}
    today  = now_for(player).date()
    system = (
        'Ты модуль классификации намерения в Telegram-боте. '
        'Определи: просит ли пользователь ЯВНО добавить событие в календарь. '
        'Если это вопрос, разговор, привет, мотивация — is_event: false. '
        f'Сегодня {today.strftime("%d.%m.%Y")}. '
        'Ответь СТРОГО JSON без markdown: '
        '{"is_event": true/false, "title": "..." или null, '
        '"date": "YYYY-MM-DD" или null, "time": "HH:MM" или null, "duration_min": 60}. '
        'Если is_event=false — все поля null. Ничего кроме JSON.'
    )
    raw = await gemini_text(text, system)
    if not raw:
        parsed = heuristic_is_event(text, player)
        return {'is_event': bool(parsed), 'event': parsed}
    try:
        raw  = re.sub(r'^```json\s*|```$', '', raw.strip())
        data = json.loads(raw)
        if not data.get('is_event') or not data.get('title'):
            return {'is_event': False, 'event': None}
        date_obj = None
        if data.get('date'):
            try: date_obj = date.fromisoformat(data['date'])
            except ValueError: pass
        return {'is_event': True, 'event': {
            'title': data['title'].strip(), 'date': date_obj,
            'time': data.get('time'), 'duration': int(data.get('duration_min') or 60),
        }}
    except Exception as e:
        logger.warning(f'Gemini intent fallback: {e}')
        parsed = heuristic_is_event(text)
        return {'is_event': bool(parsed), 'event': parsed}

async def gemini_chat(text: str, player: str) -> str:
    name = PLAYER_NAMES[player]
    system = (
        f'Ты ассистент дисциплинарного клуба «Секта». Общаешься с {name}. '
        'Отвечай коротко (2-3 предложения), по делу, на русском. '
        'Не начинай с приветствий. Помогаешь с дисциплиной и мотивацией.'
    )
    return await gemini_text(text, system)

# ─── REMINDER SETTINGS ───────────────────────────────────────
def get_reminder_settings(player: str) -> list[int]:
    try:
        val = db.reference(f'/sekta/reminderSettings/{player}').get()
        if isinstance(val, list):
            _reminder_settings_cache[player] = val
            return val
    except Exception: pass
    return _reminder_settings_cache.get(player, [30])

def set_reminder_settings(player: str, minutes_list: list[int]):
    db.reference(f'/sekta/reminderSettings/{player}').set(sorted(minutes_list))
    _reminder_settings_cache[player] = sorted(minutes_list)

def reminder_settings_keyboard(player: str) -> InlineKeyboardMarkup:
    current = get_reminder_settings(player)
    rows = []
    for minutes, label in REMIND_OPTIONS:
        icon = '✅' if minutes in current else '⬜'
        rows.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"remtoggle:{player}:{minutes}")])
    rows.append([InlineKeyboardButton("💾 Сохранить", callback_data=f"remsave:{player}")])
    return InlineKeyboardMarkup(rows)

# ─── SITE NOTIFICATIONS LOOP ─────────────────────────────────
async def notifications_loop(app: Application):
    """Каждые 10 секунд читает /sekta/notifications и рассылает новые уведомления."""
    logger.info('📬 Notifications loop started')
    while True:
        try:
            await process_site_notifications(app)
        except Exception as e:
            logger.error(f'Notifications loop error: {e}')
        await asyncio.sleep(10)

async def process_site_notifications(app: Application):
    ref   = db.reference('/sekta/notifications')
    notifs = ref.get()
    if not notifs or not isinstance(notifs, dict):
        return

    for notif_id, notif in notifs.items():
        if not isinstance(notif, dict): continue
        if notif.get('sent'): continue   # уже отправлено

        ntype   = notif.get('type', '')
        frm     = notif.get('from', '')
        data    = notif.get('data', {})

        # Определяем кому отправлять: для dayoff_approved/rejected — отправляем автору запроса
        if ntype in ('dayoff_approved', 'dayoff_rejected', 'report_approved', 'report_rejected'):
            to_player = data.get('to') or notif.get('to', '')
        else:
            to_player = notif.get('to', '')

        tg_id = PLAYER_TG.get(to_player)
        if not tg_id:
            logger.warning(f'Unknown to_player: {to_player} in notif {notif_id}')
            # Mark sent anyway to avoid infinite retries
            ref.child(notif_id).update({'sent': True})
            continue

        sent = await dispatch_notification(app, tg_id, ntype, frm, data)

        # Помечаем как отправленное
        ref.child(notif_id).update({'sent': True, 'sentAt': datetime.utcnow().isoformat()})

async def dispatch_notification(app: Application, tg_id: int, ntype: str, frm: str, data: dict) -> bool:
    """Формирует и отправляет конкретное уведомление."""
    from_name = PLAYER_NAMES.get(frm, frm)
    try:
        # ── Отгул подан ────────────────────────────────────────
        if ntype == 'dayoff_request':
            ev_date  = data.get('date', '')
            reason   = data.get('reason', '—')
            dtype    = '🛡️ Форс-мажор' if data.get('type') == 'forcemajeure' else '🌙 Передышка'
            dayoff_id = data.get('dayoffId', '')

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Одобрить", callback_data=f"dayoff_approve:{dayoff_id}:{frm}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"dayoff_reject:{dayoff_id}:{frm}"),
            ]])
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"⚖️ *{from_name} подаёт запрос на отгул*\n\n"
                    f"📅 Дата: {ev_date}\n"
                    f"Тип: {dtype}\n"
                    f"📝 Причина: {reason}\n\n"
                    f"Ожидает твоего решения."
                ),
                parse_mode='Markdown',
                reply_markup=keyboard,
            )

        # ── Отчёт подан ────────────────────────────────────────
        elif ntype == 'report_submitted':
            done  = data.get('doneHabits', '?')
            total = data.get('totalHabits', '?')
            dt    = data.get('date', '')
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"📋 *{from_name} отправил отчёт на проверку*\n\n"
                    f"📅 Дата: {dt}\n"
                    f"✅ Привычек выполнено: {done}/{total}\n\n"
                    f"Зайди на сайт чтобы проверить."
                ),
                parse_mode='Markdown',
            )

        # ── Отгул одобрен ──────────────────────────────────────
        elif ntype == 'dayoff_approved':
            reviewer = data.get('reviewerName', '?')
            ev_date  = data.get('date', '')
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"✅ *Отгул одобрен!*\n\n"
                    f"📅 Дата: {ev_date}\n"
                    f"👤 Одобрил: {reviewer}\n\n"
                    f"Можешь отдыхать 🙌"
                ),
                parse_mode='Markdown',
            )

        # ── Отгул отклонён ─────────────────────────────────────
        elif ntype == 'dayoff_rejected':
            reviewer = data.get('reviewerName', '?')
            ev_date  = data.get('date', '')
            comment  = data.get('comment', '')
            text = (
                f"❌ *Отгул отклонён*\n\n"
                f"📅 Дата: {ev_date}\n"
                f"👤 Отклонил: {reviewer}"
            )
            if comment:
                text += f"\n💬 Причина: {comment}"
            await app.bot.send_message(chat_id=tg_id, text=text, parse_mode='Markdown')

        # ── Отчёт принят ────────────────────────────────────────
        elif ntype == 'report_approved':
            reviewer = data.get('reviewerName', '?')
            ev_date  = data.get('date', '')
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"✅ *Отчёт принят!*\n\n"
                    f"📅 Дата: {ev_date}\n"
                    f"👤 Принял: {reviewer}\n\n"
                    f"Так держать! 💪"
                ),
                parse_mode='Markdown',
            )

        # ── Отчёт отклонён ──────────────────────────────────────
        elif ntype == 'report_rejected':
            reviewer = data.get('reviewerName', '?')
            ev_date  = data.get('date', '')
            await app.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"❌ *Отчёт отклонён*\n\n"
                    f"📅 Дата: {ev_date}\n"
                    f"👤 Отклонил: {reviewer}\n\n"
                    f"Зайди на сайт — там комментарий."
                ),
                parse_mode='Markdown',
            )

        logger.info(f'📬 Sent {ntype} to {tg_id}')
        return True

    except Exception as e:
        logger.error(f'dispatch_notification {ntype} → {tg_id}: {e}')
        return False

# ─── DAYOFF APPROVE/REJECT FROM BOT ──────────────────────────
async def bot_approve_dayoff(dayoff_id: str, frm: str, approved: bool, reviewer_id: str):
    """Одобряет или отклоняет отгул прямо из бота — пишет статус в Firebase."""
    try:
        ref    = db.reference('/sekta/dayoffs')
        dayoffs = ref.get()
        if not dayoffs: return False

        dayoffs_list = dayoffs if isinstance(dayoffs, list) else list(dayoffs.values())
        updated = False
        for i, d in enumerate(dayoffs_list):
            if isinstance(d, dict) and d.get('id') == dayoff_id:
                dayoffs_list[i]['status']      = 'approved' if approved else 'rejected'
                dayoffs_list[i]['reviewedAt']  = datetime.utcnow().isoformat()
                dayoffs_list[i]['reviewedBy']  = reviewer_id
                updated = True
                break

        if updated:
            ref.set(dayoffs_list)
            # Push back-notification to dayoff author
            to_tg = PLAYER_TG.get(frm)
            if to_tg:
                reviewer_name = PLAYER_NAMES.get(reviewer_id, '?')
                ev_date = next((d.get('date','') for d in dayoffs_list if isinstance(d,dict) and d.get('id')==dayoff_id), '')
                notif = {
                    'id': f'notif_{int(time.time()*1000)}',
                    'type': 'dayoff_approved' if approved else 'dayoff_rejected',
                    'from': reviewer_id, 'to': frm,
                    'timestamp': int(time.time() * 1000),
                    'sent': False,
                    'data': {'date': ev_date, 'reviewerName': reviewer_name, 'fromName': PLAYER_NAMES.get(frm,'?')}
                }
                db.reference(f'/sekta/notifications/{notif["id"]}').set(notif)
        return updated
    except Exception as e:
        logger.error(f'bot_approve_dayoff error: {e}')
        return False

# ─── SMART REMINDER LOOP ─────────────────────────────────────
async def reminder_loop(app: Application):
    logger.info('⏰ Reminder loop started')
    while True:
        try:
            await check_and_send_reminders(app)
        except Exception as e:
            logger.error(f'Reminder loop error: {e}')
        await asyncio.sleep(60)

async def check_and_send_reminders(app: Application):
    ref        = db.reference('/sekta/calEvents')
    all_events = ref.get() or []
    if isinstance(all_events, dict): all_events = list(all_events.values())

    for player, tg_id in PLAYER_TG.items():
        now       = now_for(player)
        settings  = get_reminder_settings(player)
        today_str = now.date().isoformat()
        tmrw_str  = (now.date() + timedelta(days=1)).isoformat()

        my_events = [
            e for e in all_events
            if isinstance(e, dict) and e.get('owner') == player
            and e.get('date') in (today_str, tmrw_str)
            and e.get('remindersEnabled', True)
        ]

        for ev in my_events:
            try:
                ev_dt = datetime(
                    *map(int, ev['date'].split('-')),
                    *map(int, (ev.get('time', '09:00')).split(':')),
                    tzinfo=PLAYER_TZ[player]
                )
            except Exception: continue

            diff_min = (ev_dt - now).total_seconds() / 60
            for remind_min in settings:
                key = (ev.get('id', ev['title']), remind_min)
                if key in sent_reminders: continue
                if abs(diff_min - remind_min) < 1.5:
                    label = next((l for m, l in REMIND_OPTIONS if m == remind_min), f'{remind_min} мин')
                    await app.bot.send_message(
                        chat_id=tg_id,
                        text=(
                            f"🔔 *{ev['title']}*\n"
                            f"📅 {ev['date']} в {ev.get('time','?')}\n"
                            f"⏰ Через {label}"
                        ),
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Понял", callback_data="remack:ok"),
                            InlineKeyboardButton("🔕 Откл", callback_data=f"remoff:{ev.get('id','')}"),
                        ]])
                    )
                    sent_reminders.add(key)

# ─── COMMANDS ────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке Секты.'); return
    name = PLAYER_NAMES[USERS[uid]]
    await update.message.reply_text(
        f'👋 Привет, {name}!\n\n'
        '🤖 Я бот Секты. Умею:\n'
        '• Уведомления с сайта (отгул, отчёты)\n'
        '• Добавлять события голосом или текстом\n'
        '• Ставить галочки привычек (с фото)\n'
        '• Напоминания о событиях\n\n'
        '/remind — настроить напоминания\n'
        '/today — события сегодня\n'
        '/list — все события',
        parse_mode='Markdown'
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '📖 *Команды:*\n\n'
        '/list — все события\n/today — события сегодня\n/remind — напоминания\n\n'
        '*Создание событий:*\n'
        '• `завтра в 14 тренировка`\n'
        '• Голосовое 🎤\n\n'
        '*Привычки:*\n'
        '• `потренировался` + фото\n'
        '• `прошёл 10к шагов` + фото',
        parse_mode='Markdown'
    )

async def list_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    player = USERS[uid]
    events = db.reference('/sekta/calEvents').get() or []
    if isinstance(events, dict): events = list(events.values())
    my = sorted([e for e in events if isinstance(e,dict) and e.get('owner')==player],
                key=lambda e:(e.get('date',''),e.get('time','')))
    if not my:
        await update.message.reply_text('📭 У тебя нет событий.'); return
    lines = ['📅 *Твои события:*\n']
    for e in my[:15]:
        lines.append(f"• *{e['title']}* — {e.get('date','')} в {e.get('time','')}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def today_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    player    = USERS[uid]
    today_str = now_for(player).date().isoformat()
    events    = db.reference('/sekta/calEvents').get() or []
    if isinstance(events, dict): events = list(events.values())
    today_ev  = sorted([e for e in events if isinstance(e,dict) and e.get('owner')==player and e.get('date')==today_str],
                       key=lambda e:e.get('time',''))
    if not today_ev:
        await update.message.reply_text('✅ Сегодня событий нет.'); return
    lines = ['📅 *Сегодня:*\n']
    for e in today_ev:
        lines.append(f"• *{e['title']}* в {e.get('time','?')}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def remind_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    player = USERS[uid]
    await update.message.reply_text(
        '🔔 Когда напоминать о событиях?',
        reply_markup=reminder_settings_keyboard(player)
    )

# ─── VOICE HANDLER ───────────────────────────────────────────
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке Секты.'); return
    player = USERS[uid]
    if not GROQ_API_KEY:
        await update.message.reply_text('⚠️ Whisper не настроен (нет GROQ_API_KEY)'); return
    try:
        voice_file = await ctx.bot.get_file(update.message.voice.file_id)
        ogg_bytes  = await voice_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text('❌ Не удалось скачать голосовое.'); return
    transcript = await whisper_transcribe(bytes(ogg_bytes))
    if not transcript:
        await update.message.reply_text('🤔 Не смог распознать. Попробуй ещё раз или напиши текстом.'); return
    await process_user_text(update, uid, player, transcript)

# ─── TEXT HANDLER ─────────────────────────────────────────────
async def handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    text = update.message.text.strip()
    if uid in pending:
        state = pending[uid]
        if isinstance(state, dict) and 'step' in state:
            player = USERS[uid]
            if state['step'] == 'date':
                d = resolve_date(text, player)
                if not d:
                    await update.message.reply_text('Не понял дату. Напиши: завтра / 03.07'); return
                state['parsed']['date'] = d
                state['step'] = 'time'
                await update.message.reply_text(f'⏰ В какое время {d.strftime("%d.%m")}?\n`в 14` или `14:30`', parse_mode='Markdown')
                return
            if state['step'] == 'time':
                t = resolve_time(text)
                if not t:
                    await update.message.reply_text('Не понял. `в 14` или `14:30`', parse_mode='Markdown'); return
                state['parsed']['time'] = t
                del pending[uid]
                await ask_confirm(update, uid, state['parsed']); return
    await handle_message(update, ctx)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS: return
    await process_user_text(update, uid, USERS[uid], update.message.text.strip())

async def process_user_text(update: Update, uid: int, player: str, text: str):
    # 0. Удаление плана — проверяем первым (явная команда)
    if await handle_delete_plan(update, uid, player, text): return

    # 1. Привычки
    handled = await handle_habit_report(update, uid, player, text)
    if handled: return

    result = await gemini_classify_intent(text, player)
    if not result.get('is_event') or not result.get('event'):
        ai = await gemini_chat(text, player)
        await update.message.reply_text(f'🤖 {ai}' if ai else '🤔 Не понял. Напиши например: `завтра в 14 тренировка`', parse_mode='Markdown')
        return

    parsed = result['event']
    if not parsed.get('date'):
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'date'}
        await update.message.reply_text(f'📅 На какую дату поставить *«{parsed["title"]}»*?\nзавтра / сегодня / 03.07', parse_mode='Markdown')
        return
    if not parsed.get('time'):
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'time'}
        d_fmt = parsed["date"].strftime("%d.%m")
        await update.message.reply_text(f'⏰ В какое время {d_fmt}?\n`в 14` или `в 9:30`', parse_mode='Markdown')
        return
    await ask_confirm(update, uid, parsed)

async def ask_confirm(update: Update, uid: int, parsed: dict):
    pending[uid] = parsed
    d       = parsed['date'].strftime('%d.%m.%Y')
    dur     = parsed['duration']
    dur_str = f"{dur//60}ч {dur%60}мин" if dur % 60 else f"{dur//60}ч" if dur >= 60 else f"{dur}мин"
    player  = USERS[uid]
    settings = get_reminder_settings(player)
    rem_str  = ', '.join(l for m, l in REMIND_OPTIONS if m in settings) or 'без напоминаний'
    await update.message.reply_text(
        f'📋 Добавить в календарь?\n\n*{parsed["title"]}*\n📅 {d} в {parsed["time"]}\n⏱ {dur_str}\n🔔 {rem_str}',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Добавить", callback_data=f"confirm:{uid}"),
            InlineKeyboardButton("❌ Отмена",   callback_data=f"cancel:{uid}"),
        ]]),
        parse_mode='Markdown'
    )

# ─── CALLBACK HANDLER ────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    uid   = update.effective_user.id
    reviewer = USERS.get(uid, '')

    # ── Удаление плана ───────────────────────────────────────
    if data == 'delplan_cancel':
        await query.edit_message_text('❌ Отменено.')
        return

    if data.startswith('delplan:'):
        parts   = data.split(':')
        player  = parts[1]
        plan_id = parts[2]
        plans   = read_player_plans(player)
        plan    = next((p for p in plans if p.get('id') == plan_id), None)
        ok      = delete_plan_in_firebase(player, plan_id)
        if ok and plan:
            d = plan.get('date', '')
            t = plan.get('time', '')
            try:    d_fmt = date.fromisoformat(d).strftime('%d.%m.%Y')
            except: d_fmt = d
            await query.edit_message_text(
                f'🗑 Удалено!\n\n*{plan.get("title","?")}*\n📅 {d_fmt} в {t}',
                parse_mode='Markdown'
            )
        elif ok:
            await query.edit_message_text('🗑 План удалён.')
        else:
            await query.edit_message_text('⚠️ Не удалось удалить. Попробуй на сайте.')
        return

    # ── Одобрить отгул прямо из бота ────────────────────────
    if data.startswith('dayoff_approve:') or data.startswith('dayoff_reject:'):
        parts     = data.split(':')
        action    = parts[0]    # 'dayoff_approve' or 'dayoff_reject'
        dayoff_id = parts[1]
        frm       = parts[2]    # кто подал
        approved  = action == 'dayoff_approve'
        ok = await bot_approve_dayoff(dayoff_id, frm, approved, reviewer)
        if ok:
            from_name = PLAYER_NAMES.get(frm, frm)
            await query.edit_message_text(
                f"{'✅ Отгул одобрен' if approved else '❌ Отгул отклонён'} — {from_name}\n\n"
                f"{'Уведомление отправлено' if approved else 'Причину можно добавить на сайте'}.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text('⚠️ Не удалось обновить статус. Зайди на сайт.')
        return

    if data.startswith('remtoggle:'):
        _, player, min_str = data.split(':')
        minutes = int(min_str)
        current = list(get_reminder_settings(player))
        if minutes in current: current.remove(minutes)
        else: current.append(minutes)
        set_reminder_settings(player, current)
        await query.edit_message_reply_markup(reply_markup=reminder_settings_keyboard(player))
        return

    if data.startswith('remsave:'):
        player  = data.split(':')[1]
        current = get_reminder_settings(player)
        labels  = [l for m, l in REMIND_OPTIONS if m in current]
        await query.edit_message_text(f'✅ Сохранено!\n\n🔔 Напоминать: {", ".join(labels) or "не выбрано"}', parse_mode='Markdown')
        return

    if data.startswith('remack:'): await query.edit_message_reply_markup(reply_markup=None); return

    if data.startswith('remoff:'):
        ev_id = data.split(':')[1]
        try:
            ref    = db.reference('/sekta/calEvents')
            events = ref.get() or []
            if isinstance(events, dict): events = list(events.values())
            for i, e in enumerate(events):
                if isinstance(e, dict) and e.get('id') == ev_id:
                    events[i]['remindersEnabled'] = False; break
            ref.set(events)
        except Exception as exc: logger.error(f'remoff: {exc}')
        await query.edit_message_text('🔕 Напоминание отключено.')
        return

    if data.startswith('confirm:'):
        uid    = int(data.split(':')[1])
        parsed = pending.pop(uid, None)
        if not parsed:
            await query.edit_message_text('⚠️ Данные устарели.'); return
        player = USERS[uid]
        try:
            write_event_to_firebase(player, parsed)
            settings = get_reminder_settings(player)
            rem_str  = ', '.join(l for m, l in REMIND_OPTIONS if m in settings) or 'без напоминаний'
            await query.edit_message_text(
                f'✅ Добавлено!\n\n*{parsed["title"]}*\n📅 {parsed["date"].strftime("%d.%m.%Y")} в {parsed["time"]}\n🔔 {rem_str}\n\nСобытие в календаре 🗓',
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f'❌ Ошибка: {e}')
        return

    if data.startswith('cancel:'):
        pending.pop(int(data.split(':')[1]), None)
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_proof_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending))

    async def post_init(application: Application):
        asyncio.create_task(reminder_loop(application))
        asyncio.create_task(notifications_loop(application))   # ← новый цикл

    app.post_init = post_init

    logger.info('🤖 Sekta bot started — Notifications + Gemini + Whisper + Reminders')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

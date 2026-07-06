import os
import re
import json
import time
import logging
import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

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
BOT_TOKEN   = os.environ['BOT_TOKEN']
FIREBASE_URL = os.environ['FIREBASE_URL']       # https://sekta-4c877-default-rtdb.europe-west1.firebasedatabase.app
TZ          = ZoneInfo('Europe/Kyiv')           # Временная зона

# Telegram ID → sekta player id
USERS = {
    int(os.environ['TG_ARTEM']):  'artem',
    int(os.environ['TG_BOGDAN']): 'bogdan',
}

PLAYER_NAMES = {'artem': 'Артём', 'bogdan': 'Богдан'}

# ─── FIREBASE INIT ───────────────────────────────────────────
firebase_creds = json.loads(os.environ['FIREBASE_CREDS'])
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})

# ─── HELPERS ────────────────────────────────────────────────
def now_local() -> datetime:
    return datetime.now(TZ)

def resolve_date(text: str) -> date | None:
    """Парсит дату из текста: завтра, сегодня, послезавтра, ДД.ММ, ДД.ММ.ГГГГ"""
    text = text.lower().strip()
    today = now_local().date()

    if 'послезавтра' in text:
        return today + timedelta(days=2)
    if 'завтра' in text:
        return today + timedelta(days=1)
    if 'сегодня' in text:
        return today

    # ДД.ММ или ДД.ММ.ГГГГ
    m = re.search(r'(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?', text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None

def resolve_time(text: str) -> str | None:
    """Парсит время: 14:30, в 14, в 9 утра, в 21:00"""
    text = text.lower()

    # ЧЧ:ММ
    m = re.search(r'\b(\d{1,2}):(\d{2})\b', text)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"

    # "в 9", "в 14", "в 9 утра", "в 9 вечера"
    m = re.search(r'в\s+(\d{1,2})(?:\s*(утра|вечера|ночи|дня))?', text)
    if m:
        h = int(m.group(1))
        suffix = m.group(2)
        if suffix in ('вечера', 'ночи') and h < 12:
            h += 12
        if suffix == 'дня' and h < 12:
            h += 12
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    return None

def resolve_duration(text: str) -> int:
    """Парсит длительность: 1 час, 30 минут, 1.5 часа → минуты"""
    text = text.lower()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(час|ч\b|минут|мин\b)', text)
    if m:
        val = float(m.group(1).replace(',', '.'))
        unit = m.group(2)
        if 'час' in unit or unit == 'ч':
            return int(val * 60)
        return int(val)
    return 60  # default 1 час

def parse_event(text: str) -> dict | None:
    """
    Парсит сообщение вида:
      поставь задачу завтра в 14:00 встреча с врачом
      добавь событие сегодня в 9 тренировка 1 час
      напомни послезавтра в 21 позвонить маме
    """
    # Убираем команду если есть
    text = re.sub(r'^/\w+\s*', '', text).strip()

    # Ключевые слова-триггеры (необязательны)
    text_clean = re.sub(
        r'^(поставь|добавь|создай|запланируй|напомни|запиши)\s+(задачу|событие|встречу|дело|напоминание)?\s*',
        '', text, flags=re.IGNORECASE
    ).strip()

    event_date = resolve_date(text_clean)
    event_time = resolve_time(text_clean)
    duration   = resolve_duration(text_clean)

    # Название = текст без ключевых дат/времён
    title = text_clean
    title = re.sub(r'\b(завтра|сегодня|послезавтра)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\d{1,2}[./]\d{1,2}(?:[./]\d{4})?', '', title)
    title = re.sub(r'\bв\s+\d{1,2}(?::\d{2})?\s*(?:утра|вечера|ночи|дня)?\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\b\d+\s*(?:час|ч\b|минут|мин\b)\w*\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s{2,}', ' ', title).strip(' ,.-')

    if not title:
        return None

    return {
        'date':     event_date,
        'time':     event_time,
        'duration': duration,
        'title':    title,
    }

def write_event_to_firebase(player_id: str, event: dict) -> str:
    """Пишет событие в S.calEvents Firebase и возвращает ID события"""
    ref = db.reference('/sekta/calEvents')

    # Читаем текущий массив
    current = ref.get() or []
    if not isinstance(current, list):
        # Firebase может хранить как dict {0: ..., 1: ...}
        current = list(current.values()) if isinstance(current, dict) else []

    # Формируем событие
    event_date = event['date']
    event_time = event['time'] or '09:00'
    date_str   = event_date.strftime('%Y-%m-%d') if event_date else ''

    # Считаем endTime
    h, m = map(int, event_time.split(':'))
    end_minutes = h * 60 + m + event['duration']
    end_h = (end_minutes // 60) % 24
    end_m = end_minutes % 60
    end_time = f"{end_h:02d}:{end_m:02d}"

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
    }

    current.append(new_event)
    ref.set(current)
    return ev_id

# ─── PENDING CONFIRMS ────────────────────────────────────────
# chat_id → parsed event dict waiting for confirmation
pending: dict[int, dict] = {}

# ─── HANDLERS ───────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке пользователей Секты.')
        return
    name = PLAYER_NAMES[USERS[uid]]
    await update.message.reply_text(
        f'Привет, {name}! 👋\n\n'
        'Я добавляю события в календарь Секты.\n\n'
        '*Примеры команд:*\n'
        '• `поставь задачу завтра в 14 тренировка`\n'
        '• `добавь встречу сегодня в 9:30 звонок врачу`\n'
        '• `послезавтра в 20 ужин с друзьями 2 часа`\n'
        '• `03.07 в 15:00 подготовка к НМТ`\n\n'
        '*/help* — подробная справка',
        parse_mode='Markdown'
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '*📋 Как пользоваться ботом*\n\n'
        'Просто напиши в свободной форме что и когда:\n\n'
        '*Дата:* завтра / сегодня / послезавтра / 03.07 / 03.07.2026\n'
        '*Время:* в 14 / в 14:30 / в 9 утра / в 9 вечера\n'
        '*Длительность:* 1 час / 30 минут / 1.5 часа (по умолчанию 1 час)\n\n'
        '*Примеры:*\n'
        '`завтра в 8 ранний подъём`\n'
        '`поставь задачу послезавтра в 19:00 тренировка 1.5 часа`\n'
        '`сегодня в 22 читать книгу 30 минут`\n'
        '`05.07 в 10 встреча с куратором`\n\n'
        '*/list* — события на эту неделю\n'
        '*/today* — события на сегодня',
        parse_mode='Markdown'
    )

async def list_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Нет доступа.'); return

    player = USERS[uid]
    today = now_local().date()
    week_end = today + timedelta(days=7)

    ref = db.reference('/sekta/calEvents')
    all_events = ref.get() or []
    if isinstance(all_events, dict):
        all_events = list(all_events.values())

    my_events = [
        e for e in all_events
        if isinstance(e, dict) and e.get('owner') == player
        and e.get('date') and today <= date.fromisoformat(e['date']) <= week_end
    ]
    my_events.sort(key=lambda e: (e.get('date',''), e.get('time','')))

    if not my_events:
        await update.message.reply_text('📅 На эту неделю событий нет.')
        return

    lines = ['*📅 Твои события на неделю:*\n']
    cur_date = None
    for e in my_events:
        d = date.fromisoformat(e['date'])
        if d != cur_date:
            cur_date = d
            day_name = ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][d.weekday()]
            lines.append(f"\n*{day_name} {d.strftime('%d.%m')}*")
        t = e.get('time', '')
        lines.append(f"  {t} — {e.get('title','?')}")

    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def today_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Нет доступа.'); return

    player = USERS[uid]
    today_str = now_local().date().isoformat()

    ref = db.reference('/sekta/calEvents')
    all_events = ref.get() or []
    if isinstance(all_events, dict):
        all_events = list(all_events.values())

    my_events = [
        e for e in all_events
        if isinstance(e, dict) and e.get('owner') == player and e.get('date') == today_str
    ]
    my_events.sort(key=lambda e: e.get('time', ''))

    if not my_events:
        await update.message.reply_text('📅 Сегодня событий нет.')
        return

    lines = ['*📅 Сегодня:*\n']
    for e in my_events:
        lines.append(f"  {e.get('time','')} — {e.get('title','?')}")

    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USERS:
        await update.message.reply_text('❌ Ты не в списке пользователей Секты.')
        return

    text = update.message.text.strip()
    parsed = parse_event(text)

    if not parsed or not parsed['title']:
        await update.message.reply_text(
            '🤔 Не понял. Напиши например:\n`завтра в 14 тренировка`',
            parse_mode='Markdown'
        )
        return

    # Если дата не распознана — спросить
    if not parsed['date']:
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'date'}
        await update.message.reply_text(
            f'📅 На какую дату поставить *«{parsed["title"]}»*?\n'
            'Напиши: завтра / сегодня / послезавтра / 03.07',
            parse_mode='Markdown'
        )
        return

    # Если время не распознано — спросить
    if not parsed['time']:
        pending[uid] = {'text': text, 'parsed': parsed, 'step': 'time'}
        d = parsed['date'].strftime('%d.%m')
        await update.message.reply_text(
            f'⏰ В какое время {d}?\nНапиши например: `в 14` или `в 9:30`',
            parse_mode='Markdown'
        )
        return

    # Всё есть — показываем подтверждение
    await ask_confirm(update, uid, parsed)

async def handle_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответы на вопросы о дате/времени"""
    uid = update.effective_user.id
    if uid not in USERS or uid not in pending:
        return await handle_message(update, ctx)

    state = pending[uid]
    text  = update.message.text.strip()

    if state['step'] == 'date':
        d = resolve_date(text)
        if not d:
            await update.message.reply_text('Не понял дату. Напиши: завтра / 03.07 / сегодня')
            return
        state['parsed']['date'] = d
        state['step'] = 'time'
        d_str = d.strftime('%d.%m')
        await update.message.reply_text(
            f'⏰ В какое время {d_str}?\nНапиши: `в 14` или `14:30`',
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

async def ask_confirm(update: Update, uid: int, parsed: dict):
    """Показывает подтверждение перед добавлением события"""
    pending[uid] = parsed  # сохраняем для callback

    d = parsed['date'].strftime('%d.%m.%Y')
    t = parsed['time']
    dur = parsed['duration']
    title = parsed['title']
    dur_str = f"{dur//60}ч {dur%60}мин" if dur % 60 else f"{dur//60}ч" if dur >= 60 else f"{dur}мин"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Добавить", callback_data=f"confirm:{uid}"),
            InlineKeyboardButton("❌ Отмена",   callback_data=f"cancel:{uid}"),
        ]
    ])

    await update.message.reply_text(
        f'📋 Добавить в календарь?\n\n'
        f'*{title}*\n'
        f'📅 {d} в {t}\n'
        f'⏱ {dur_str}',
        reply_markup=keyboard,
        parse_mode='Markdown'
    )

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, uid_str = query.data.split(':', 1)
    uid = int(uid_str)

    if action == 'cancel':
        pending.pop(uid, None)
        await query.edit_message_text('❌ Отменено.')
        return

    if action == 'confirm':
        parsed = pending.pop(uid, None)
        if not parsed:
            await query.edit_message_text('⚠️ Данные устарели, попробуй снова.')
            return

        player = USERS[uid]
        try:
            write_event_to_firebase(player, parsed)
            d = parsed['date'].strftime('%d.%m.%Y')
            t = parsed['time']
            await query.edit_message_text(
                f'✅ Добавлено!\n\n'
                f'*{parsed["title"]}*\n'
                f'📅 {d} в {t}\n'
                f'Открой сайт — событие уже в календаре 🗓',
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Firebase write error: {e}")
            await query.edit_message_text(f'❌ Ошибка записи: {e}')

# ─── MAIN ────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help',  help_cmd))
    app.add_handler(CommandHandler('list',  list_events))
    app.add_handler(CommandHandler('today', today_events))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending))

    logger.info('🤖 Sekta bot started')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

# 🤖 Sekta Calendar Bot

Telegram бот для добавления событий в календарь сайта Секты.

## Переменные окружения (Railway)

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | Токен от @BotFather |
| `FIREBASE_URL` | `https://sekta-4c877-default-rtdb.europe-west1.firebasedatabase.app` |
| `TG_ARTEM` | Telegram ID Артёма |
| `TG_BOGDAN` | Telegram ID Богдана |
| `FIREBASE_CREDS` | Весь JSON файла service account (одной строкой) |

## Деплой на Railway

1. Зайди на [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Загрузи эти файлы в GitHub репозиторий (или через Railway CLI)
3. В настройках проекта добавь переменные окружения выше
4. Railway сам запустит бота

## Команды бота

- `/start` — приветствие
- `/help` — справка
- `/today` — события на сегодня
- `/list` — события на неделю

## Примеры сообщений

```
завтра в 14 тренировка
поставь задачу послезавтра в 19:00 встреча с куратором 1.5 часа
сегодня в 22 читать книгу 30 минут
03.07 в 10 подготовка к НМТ
```

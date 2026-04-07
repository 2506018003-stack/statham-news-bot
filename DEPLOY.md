# Statham News Bot — Инструкция по деплою на Render.com

## Что изменилось (vs PythonAnywhere)

| PythonAnywhere | Render.com |
|----------------|------------|
| ❌ Блокирует исходящие соединения | ✅ Нет ограничений |
| ❌ RSS не работает | ✅ RSS работает |
| ❌ Требует fcntl (Linux-only) | ✅ Без fcntl, кроссплатформенно |
| ❌ $5/мес для Hacker плана | ✅ Бесплатно |

## Новые функции

- ✅ **CryptoPanic** — крипто-новости EN+RU
- ✅ **RSS** — 10 источников (CoinDesk, Reuters и др.)
- ✅ **Twitter** — парсинг твитов с ключевыми словами
- ✅ **Telegram каналы** — репост важных сообщений

## Пошаговая инструкция

### 1. Создай аккаунт на Render

1. Зайди на https://render.com
2. Нажми "Get Started for Free"
3. Залогинься через GitHub или email

### 2. Создай новый Web Service

1. На дашборде нажми **"New +"** → **"Web Service"**
2. Выбери **"Build and deploy from a Git repository"**
3. Подключи свой GitHub репозиторий с кодом бота
4. Или выбери **"Deploy from image"** и загрузи файлы вручную

### 3. Настрой сервис

| Параметр | Значение |
|----------|----------|
| **Name** | `statham-news-bot` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn bot:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120` |
| **Plan** | Free |

### 4. Добавь Environment Variables

В разделе **Environment** добавь:

```
BOT_TOKEN_NEWS=8795447612:AAGMehnhtdW6Mfc3fekLwjNfPeJdJR0NHT0
CHAT_ID=-1003867089540
CRYPTOPANIC_TOKEN=db62f80e72919ae2e1617b6af4a08e287acb336d
TWITTER_BEARER_TOKEN=твой_токен_опционально
```

**Где взять TWITTER_BEARER_TOKEN:**
1. Зайди на https://developer.twitter.com
2. Создай проект и приложение
3. В разделе Keys and Tokens → Bearer Token

### 5. Добавь Disk (для сохранения данных)

1. Перейди в раздел **Disks**
2. Нажми **"Add Disk"**
3. **Name:** `data`
4. **Mount Path:** `/data`
5. **Size:** 1 GB
6. Нажми **Create**

### 6. Задеплой

Нажми **"Create Web Service"**

Через 2-3 минуты бот будет доступен по URL типа:
```
https://statham-news-bot.onrender.com
```

### 7. Проверь работу

Открой в браузере:
```
https://statham-news-bot.onrender.com/
```

Должен показать:
```json
{
  "status": "ok",
  "server": "news_bot_render",
  "features": ["cryptopanic", "rss", "twitter", "telegram_channels"]
}
```

### 8. Настрой Telegram Webhook

Открой:
```
https://statham-news-bot.onrender.com/setup
```

Или в Telegram отправь боту команду — webhook настроится автоматически.

## Управление через команды

| Команда | Описание |
|---------|----------|
| `/help` | Справка |
| `/news_status` | Статус всех источников |
| `/check_news` | Запустить парсинг вручную |
| `/clear_cache` | Очистить кэш |
| `/debug` | Посмотреть лог (через URL) |

## Важно знать

### Render Free Tier ограничения:
- **Spin-down:** После 15 мин без запросов сервер засыпает
- **Запрос разбудит:** Первый запрос после сна — 30 сек задержка
- **Решение:** Для бота это нормально — webhook от Telegram разбудит сервер

### Чтобы сервер не засыпал:
Можно настроить UptimeRobot или cron-job.org для ping каждые 10 мин:
```
https://statham-news-bot.onrender.com/
```

## Структура файлов

```
news-bot/
├── bot.py              # Основной код бота
├── requirements.txt    # Зависимости
├── render.yaml         # Конфиг для Render (опционально)
└── DEPLOY.md           # Эта инструкция
```

## Устранение проблем

### Бот не отвечает
1. Проверь логи в Render Dashboard → Logs
2. Убедись что `BOT_TOKEN_NEWS` правильный
3. Проверь `/setup` для webhook

### Нет новостей
1. Проверь `/debug` — есть ли ошибки?
2. Убедись что `CRYPTOPANIC_TOKEN` установлен
3. Проверь `/news_status` — кэш не переполнен?

### Twitter не работает
1. Проверь что `TWITTER_BEARER_TOKEN` добавлен
2. Twitter API Free Tier имеет ограничения (100 запросов/15 мин)

## Переезд с PythonAnywhere

1. Сохрани старые файлы с PA:
   - `rss_seen.json` — кэш новостей
   - `cp_ratelimit.json` — rate limit статус

2. Скопируй их на Render диск в `/data/`

3. Или начни с чистого кэша — `/clear_cache`

## Контакты и помощь

Если что-то не работает — проверь логи:
```
https://statham-news-bot.onrender.com/debug
```

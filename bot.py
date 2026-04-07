"""
Telegram Bot — Render.com: Новости из CryptoPanic, Twitter, Telegram
"""
from __future__ import annotations
import json, os, datetime, threading, time, hashlib, re
from urllib.parse import quote as url_quote
from flask import Flask, request, jsonify
import requests
import telebot
import feedparser
import redis
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── REDIS КЭШ (для Render Free Tier - файлы сбрасываются!) ───────
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is None and REDIS_URL:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        except Exception as e:
            write_debug_log(f"REDIS_ERR | {e}")
    return _redis_client

def redis_get(key: str, default=None):
    try:
        r = get_redis()
        if r:
            val = r.get(key)
            if val:
                return json.loads(val)
    except Exception as e:
        write_debug_log(f"REDIS_GET_ERR | {e}")
    return default

def redis_set(key: str, data, ex=2592000):  # 30 days TTL
    try:
        r = get_redis()
        if r:
            r.set(key, json.dumps(data), ex=ex)
            return True
    except Exception as e:
        write_debug_log(f"REDIS_SET_ERR | {e}")
    return False

# ── КОНФИГУРАЦИЯ ─────────────────────────────────────────────────
_raw_token = os.environ.get("BOT_TOKEN_NEWS", "")
if not _raw_token or ":" not in _raw_token:
    raise RuntimeError(f"BOT_TOKEN_NEWS неверный или не установлен: {_raw_token!r}")
TOKEN     = _raw_token
CHAT_ID   = os.environ.get("CHAT_ID", "-1003867089540")

NEWS_TOPIC_ID = "9505"

# Render free tier - используем текущую директорию (без диска)
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
os.makedirs(BASE_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN, threaded=False)

# ── CRYPTOPANIC ──────────────────────────────────────────────────
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN", "")
CRYPTOPANIC_URL   = "https://cryptopanic.com/api/developer/v2/posts/"
RSS_MAX_PER_RUN   = 3

_cp_lock        = threading.Lock()
_cp_last_req_ts = 0.0
CP_MIN_INTERVAL = 60.0

# ── TWITTER ──────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN", "")
TWITTER_USERS = [
    "1437032895216824322",  # @wu_blockchain
    "1333467482",           # @loomdart
    "1467183928872722438",  # @whale_alert
    "1307386582665342976",  # @tier10k
    "25073877",             # @realDonaldTrump (Трамп)
    "44196397",             # @elonmusk (Илон Маск)
    "119248032",            # @federalreserve (ФРС)
]
_twitter_lock = threading.Lock()
_twitter_last_req_ts = 0.0
TWITTER_MIN_INTERVAL = 120.0

# ── TELEGRAM ИСТОЧНИКИ ───────────────────────────────────────────
TELEGRAM_CHANNELS = [
    "-1001754252633",  # Канал 1
    "-1001967505770",  # Канал 2
    "-1002208172141",  # Канал 3
    "-1002196771546",  # Канал 4
]
_telegram_lock = threading.Lock()
_tg_last_check_ts = 0.0
TG_MIN_INTERVAL = 300.0  # 5 минут

# ── КЛЮЧЕВЫЕ СЛОВА ───────────────────────────────────────────────
RSS_KEYWORDS = [
    "etf","hack","exploit","listing","delist","sec","regulation","ban",
    "crash","lawsuit","arrest","halving","liquidat","rug","scam","sanction",
    "depeg","blackrock","coinbase","binance","usdt","usdc","stablecoin",
    "bitcoin","btc","ethereum","eth","ripple","xrp","solana","sol","bnb",
    "cardano","ada","dogecoin","doge","shib","avax","matic","polygon",
    "crypto","blockchain","defi","nft","airdrop","fork","mainnet","testnet",
    "exchange","wallet","custody","institutional","fidelity",
    "биткоин","крипто","блокчейн","регулирование","запрет","листинг",
    "делистинг","хакер","взлом","мошенничество","санкции","арест","суд",
    "эфириум","рипл","стейблкоин","биржа","кошелёк","халвинг",
    "рынок","акции","индекс","фьючерс","облигаци","доллар","евро","юань",
    "курс","инфляц","ставка","цб","центробанк","фрс","федеральная резервная",
    "ввп","дефицит","бюджет","долг","инвестиц","портфель","дивиденд",
    "ipo","спред","волатильность","коррекц","ралли","распродажа",
    "нефть","газ","опек","нефтяной","баррель","brent","wti","urals",
    "нефтегаз","газпром","роснефть","лукойл","новатэк","сжиженный",
    "нефтепровод","экспорт нефти","добыча нефти","сырьё",
    "золото","серебро","платина","палладий","медь","алюминий","никель",
    "сталь","железо","металл","gold","silver","metal",
    "норникель","северсталь","нлмк","магнитогорск",
    "сбербанк","сбер","яндекс","вк","мтс",
    "алроса","полюс","полиметалл","ozon","тинькофф","т-банк","wildberries",
    "ммвб","московская биржа","торги",
    "искусственный интеллект","нейросеть","нейронная сеть","языковая модель",
    "chatgpt","gpt","claude","gemini","llm","openai","anthropic","deepseek",
    "midjourney","stable diffusion","генеративный","автоматизация","ии",
    "машинное обучение","deep learning","computer vision",
    "artificial intelligence","neural network",
    "эмбарго","геополитика","военный","конфликт","мирный",
    "переговоры","соглашение","договор","саммит","g7","g20","брикс","нато",
    "евросоюз","сша","китай","путин","байден","трамп","президент",
    "экономическая война","торговая война","пошлин","тариф",
    # --- ГЕОПОЛИТИКА / ВОЕННЫЕ ДЕЙСТВИЯ ---
    "иран","саудовская аравия","эль-джубайль","персидский залив","израиль",
    "атака","удар","пожар","взрыв","ракет","дрон","бомбардировк","войск",
    "эскалация","конфликт","война","войны","военный","бое","боевые действия",
    "радиологический","чернобыль","катастрофа","ядерный","радиация",
    "гладков","белгородская","отставка","губернатор","власти","правительство",
    "мид рф","мид","россия","украина",
    # --- МАКРОЭКОНОМИКА ---
    "m2","денежная масса","ликвидность","триллион","$","доллар",
    "выступление","отчет","данные","статистика","публикация","заседание",
    "председатель","пауэлл","фomc","комитет",
    # --- ИИ СТАРТАПЫ ---
    "стартап","founder","создатель","один человек","одиночка","соло",
    # --- ПРОГНОЗЫ / СТАВКИ ---
    "polymarket","прогноз","ставка","вероятность","шанс",
]

# ── RSS ИСТОЧНИКИ (теперь работают на Render!) ─────────────────
RSS_SOURCES = [
    {"name": "CoinDesk",       "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",     "flag": "🪙"},
    {"name": "CoinTelegraph",  "url": "https://cointelegraph.com/rss",                       "flag": "📡"},
    {"name": "Decrypt",        "url": "https://decrypt.co/feed",                             "flag": "🔓"},
    {"name": "BeInCrypto",     "url": "https://beincrypto.com/feed/",                        "flag": "📊"},
    {"name": "The Block",      "url": "https://www.theblock.co/rss.xml",                     "flag": "🧱"},
    {"name": "FT Markets",     "url": "https://www.ft.com/rss/home/uk",                      "flag": "💹"},
    {"name": "TechCrunch AI",  "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "flag": "🤖"},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/",           "flag": "💡"},
]

# ── GOOGLE TRANSLATE ──────────────────────────────────────────────
def _is_russian(text: str) -> bool:
    return any('\u0400' <= ch <= '\u04FF' for ch in text)

def translate_to_ru(text: str) -> str:
    if not text or _is_russian(text):
        return text
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text},
            timeout=5,
        )
        if resp.status_code == 200:
            translated = "".join(p[0] for p in resp.json()[0] if p[0])
            if translated and translated != text and _is_russian(translated):
                return translated
    except Exception as e:
        write_debug_log(f"TRANSLATE_ERR | {e}")
    return text

# ── DEBUG LOG ─────────────────────────────────────────────────────
def write_debug_log(entry: str):
    log_path = os.path.join(BASE_DIR, "webhook_debug.log")
    try:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {entry}\n")
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 500:
            with open(log_path, "w", encoding="utf-8") as f:
                f.writelines(lines[-500:])
    except Exception:
        pass

# ── REDIS ХЕЛПЕРЫ (замена файловому кэшу) ────────────────────────
def load_json(key: str, default):
    return redis_get(key, default)

def save_json(key: str, data):
    redis_set(key, data)

def load_rss_seen():
    return redis_get("rss_seen", {})

def save_rss_seen(d):
    redis_set("rss_seen", d)

def _cp_get_retry_after() -> float:
    return redis_get("cp_retry_after", 0.0)

def _cp_set_retry_after(until_ts: float):
    redis_set("cp_retry_after", until_ts, ex=3600)

def _twitter_get_retry_after() -> float:
    return redis_get("twitter_retry_after", 0.0)

def _twitter_set_retry_after(until_ts: float):
    redis_set("twitter_retry_after", until_ts, ex=3600)

# ── TELEGRAM SEND ─────────────────────────────────────────────────
def send_tg(text: str, chat_id=None, thread_id=None) -> dict:
    if chat_id is None:
        chat_id = CHAT_ID
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if thread_id is not None:
        try:
            payload["message_thread_id"] = int(thread_id)
        except (ValueError, TypeError):
            pass
    resp = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json=payload,
        timeout=8.0
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"TG error: {data.get('description')}")
    return data["result"]

def send_news(text: str) -> dict:
    return send_tg(text, thread_id=NEWS_TOPIC_ID)

# ── WEBHOOK SETUP ─────────────────────────────────────────────────
_webhook_ok = False

def _do_register_webhook() -> bool:
    global _webhook_ok
    try:
        domain = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
        if not domain:
            write_debug_log("WEBHOOK_SETUP_ERR | RENDER_EXTERNAL_HOSTNAME not set")
            return False
        webhook_url = f"https://{domain}/{TOKEN}"
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            json={"url": webhook_url, "drop_pending_updates": False},
            timeout=10
        )
        result = r.json()
        write_debug_log(f"WEBHOOK_SETUP | {result}")
        _webhook_ok = result.get("ok", False)
        return _webhook_ok
    except Exception as e:
        write_debug_log(f"WEBHOOK_SETUP_ERR | {e}")
        return False

# ── HELPERS ───────────────────────────────────────────────────────
def _reply(message, text: str):
    cid = message.chat.id
    tid = getattr(message, "message_thread_id", None)
    payload: dict = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if tid:
        try:
            payload["message_thread_id"] = int(tid)
        except (ValueError, TypeError):
            pass
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=8.0
        )
        data = resp.json()
        if not data.get("ok"):
            write_debug_log(f"REPLY_ERR | chat={cid} | {data.get('description')}")
    except Exception as e:
        write_debug_log(f"REPLY_EXCEPTION | chat={cid} | {e}")

def is_bot_admin(user_id: int) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/getChatAdministrators",
            json={"chat_id": CHAT_ID},
            timeout=8.0
        )
        data = resp.json()
        if data.get("ok"):
            return user_id in [a["user"]["id"] for a in data.get("result", [])]
    except Exception as e:
        write_debug_log(f"IS_ADMIN_ERR | user={user_id} | {e}")
    return False

# ── FLASK ROUTES ──────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({
        "status": "ok",
        "server": "news_bot_render",
        "webhook_ok": _webhook_ok,
        "cached_news": len(load_rss_seen()),
        "news_topic": NEWS_TOPIC_ID,
        "features": ["cryptopanic", "rss", "twitter", "telegram_channels"]
    })

@app.route("/setup")
def setup_webhook():
    ok = _do_register_webhook()
    return jsonify({"status": "ok" if ok else "error", "webhook_ok": _webhook_ok})

@app.route("/debug")
def debug_log_route():
    log_path = os.path.join(BASE_DIR, "webhook_debug.log")
    if not os.path.exists(log_path):
        return jsonify({"log": "Лог пуст"})
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-50:]), "total_lines": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/" + TOKEN, methods=["POST"])
def telegram_webhook():
    global _webhook_ok
    if not _webhook_ok:
        threading.Thread(target=_do_register_webhook, daemon=True).start()
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        write_debug_log(f"TG_WEBHOOK_ERR | {e}")
    return "!", 200

# ── TELEGRAM КОМАНДЫ ──────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(message):
    _reply(message, "👋 Привет! Это <b>Statham News Bot</b>.\nДля справки — /help")

@bot.message_handler(commands=["help"])
def cmd_help(message):
    _reply(message, """📰 <b>Statham News Bot — Все команды</b>

<b>📋 Общие:</b>
• /start — Приветствие
• /help — Эта справка

<b>🔧 Только для Admin:</b>
• /news [текст] — Опубликовать новость вручную в топик
• /check_news — Запустить парсинг прямо сейчас
• /news_status — Статус бота, кэш, список источников
• /clear_cache — Очистить кэш отправленных новостей

<b>🌐 Открыть в браузере:</b>
• /debug — Последние 50 строк лога
• /setup — Переустановить webhook""")

@bot.message_handler(commands=["news"])
def cmd_news(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        _reply(message, "❌ Использование: <code>/news Текст новости</code>")
        return
    try:
        send_news(f"📰 <b>Новость</b>\n\n{parts[1].strip()}")
        _reply(message, "✅ Новость опубликована в тред новостей")
    except Exception as e:
        write_debug_log(f"CMD_NEWS_ERR | {e}")
        _reply(message, f"❌ Ошибка: {e}")

@bot.message_handler(commands=["check_news"])
def cmd_check_news(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    _reply(message, "🔄 Запускаю парсинг...")
    def _run():
        try:
            _check_news()
            _check_rss_all()
            _check_telegram_channels()
            _reply(message, "✅ Готово. Подробности: /debug")
        except Exception as e:
            _reply(message, f"❌ Ошибка: {e}")
    threading.Thread(target=_run, daemon=True).start()

@bot.message_handler(commands=["news_status"])
def cmd_news_status(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    seen = load_rss_seen()
    tg_seen = redis_get("telegram_seen", [])
    tok_ok = "✅ Установлен" if CRYPTOPANIC_TOKEN else "⚠️ Не установлен"
    rss_list = "\n".join(f"  {s['flag']} {s['name']}" for s in RSS_SOURCES)
    _reply(message, f"""📰 <b>Статус новостей</b>

🔑 CryptoPanic токен: {tok_ok}
 Кэш новостей: {len(seen)}
📚 Кэш Telegram: {len(tg_seen)} сообщений
⏱ Парсинг: каждые 30 мин
📬 Топик: #{NEWS_TOPIC_ID}

<b>✅ Работает:</b>
🌍 CryptoPanic EN+RU — крипто-новости
📡 RSS — {len(RSS_SOURCES)} источников
💬 Telegram — {len(TELEGRAM_CHANNELS)} каналов

<b>📂 Источники RSS:</b>
{rss_list}""")

@bot.message_handler(commands=["clear_cache"])
def cmd_clear_cache(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    save_rss_seen({})
    redis_set("twitter_seen", [])
    redis_set("telegram_seen", [])
    _reply(message, "✅ Кэш очищен. Следующий запуск пришлёт свежие новости.")

# ── CRYPTOPANIC NEWS ──────────────────────────────────────────────
def _check_news():
    global _cp_last_req_ts
    with _cp_lock:
        now = time.time()
        retry_after_ts = _cp_get_retry_after()
        if now < retry_after_ts:
            wait = retry_after_ts - now + 1.0
            write_debug_log(f"CRYPTOPANIC | ждём {wait:.0f}s (Retry-After)")
            time.sleep(wait)
        elapsed = time.time() - _cp_last_req_ts
        if elapsed < CP_MIN_INTERVAL:
            wait = CP_MIN_INTERVAL - elapsed
            write_debug_log(f"CRYPTOPANIC | ждём {wait:.0f}s (min-interval)")
            time.sleep(wait)
        _cp_last_req_ts = time.time()
        seen = load_rss_seen()
        if isinstance(seen, list):
            seen = {url: 0 for url in seen}
        now_ts = int(time.time())
        seen = {u: ts for u, ts in seen.items() if ts > now_ts - 30 * 86400}
        new_seen = dict(seen)
        sent_count = 0
        try:
            resp = requests.get(
                CRYPTOPANIC_URL,
                params={
                    "auth_token": CRYPTOPANIC_TOKEN,
                    "kind": "news",
                    "regions": "en,ru",
                    "public": "true"
                },
                timeout=12,
            )
            if resp.status_code == 401:
                write_debug_log("CRYPTOPANIC | 401 Unauthorized")
                return
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 300))
                _cp_set_retry_after(time.time() + retry_after)
                write_debug_log(f"CRYPTOPANIC | 429 — retry after {retry_after}s")
                return
            if resp.status_code != 200:
                write_debug_log(f"CRYPTOPANIC | HTTP {resp.status_code}")
                return
            results = resp.json().get("results", [])
            write_debug_log(f"CRYPTOPANIC | results={len(results)}")
            for item in results[:30]:
                if sent_count >= RSS_MAX_PER_RUN:
                    break
                url = (item.get("original_url") or item.get("url") or
                       (f"https://cryptopanic.com/news/{item['slug']}/"
                        if item.get("slug") else ""))
                title = item.get("title", "")
                if not title:
                    continue
                cache_key = url or str(item.get("id", "")) or hashlib.md5(title.encode()).hexdigest()
                if cache_key in seen:
                    continue
                if not any(kw in title.lower() for kw in RSS_KEYWORDS):
                    continue
                source = item.get("source", {}).get("title", "")
                domain = item.get("source", {}).get("domain", "")
                pub_raw = item.get("published_at", "")
                pub_str = pub_raw[:16].replace("T", " ") + " UTC" if pub_raw else ""
                panic = item.get("panic_score")
                try:
                    is_ru = _is_russian(title)
                    title_out = title if is_ru else translate_to_ru(title)
                    if not is_ru and not _is_russian(title_out):
                        write_debug_log(f"CRYPTOPANIC | SKIP (no translation): {title[:60]}")
                        continue
                    flag = "🇷🇺" if is_ru else "🌍"
                    meta = []
                    if pub_str:
                        meta.append(f"🕐 {pub_str}")
                    src = source or domain
                    if src:
                        meta.append(f"📡 {src}")
                    if panic and panic >= 60:
                        meta.append(f"🔥 Panic: {panic}")
                    parts = [f"📰 {flag} <b>Крипто-новость</b>"]
                    if meta:
                        parts.append("  |  ".join(meta))
                    parts.append("")
                    parts.append(f"<b>{title_out}</b>")
                    if not is_ru and title_out != title:
                        parts.append(f"<i>{title}</i>")
                    if url:
                        parts.append(f"🔗 {url}")
                    send_news("\n".join(parts))
                    new_seen[cache_key] = now_ts
                    sent_count += 1
                    time.sleep(3)
                except Exception as e:
                    write_debug_log(f"NEWS_SEND_ERR | {e}")
        except requests.exceptions.ConnectionError as e:
            write_debug_log(f"CRYPTOPANIC_BLOCKED | {e}")
        except requests.exceptions.Timeout:
            write_debug_log("CRYPTOPANIC_TIMEOUT")
        except Exception as e:
            write_debug_log(f"CRYPTOPANIC_ERR | {e}")
        if new_seen != seen:
            save_rss_seen(new_seen)
        write_debug_log(f"CRYPTOPANIC | done | sent={sent_count} | cached={len(new_seen)}")

# ── TWITTER PARSER ───────────────────────────────────────────────
def _twitter_get_retry_after() -> float:
    try:
        if os.path.exists(TWITTER_RATELIMIT_FILE):
            with open(TWITTER_RATELIMIT_FILE, "r") as f:
                return float(json.load(f).get("retry_after", 0))
    except Exception:
        pass
    return 0.0

def _twitter_set_retry_after(until_ts: float):
    try:
        with open(TWITTER_RATELIMIT_FILE, "w") as f:
            json.dump({"retry_after": until_ts}, f)
    except Exception:
        pass

def _check_twitter():
    global _twitter_last_req_ts
    if not TWITTER_BEARER_TOKEN:
        write_debug_log("TWITTER | пропуск: токен не установлен")
        return
    with _twitter_lock:
        now = time.time()
        retry_after_ts = _twitter_get_retry_after()
        if now < retry_after_ts:
            wait = retry_after_ts - now + 1.0
            write_debug_log(f"TWITTER | ждём {wait:.0f}s (Retry-After)")
            time.sleep(wait)
        elapsed = time.time() - _twitter_last_req_ts
        if elapsed < TWITTER_MIN_INTERVAL:
            wait = TWITTER_MIN_INTERVAL - elapsed
            write_debug_log(f"TWITTER | ждём {wait:.0f}s (min-interval)")
            time.sleep(wait)
        _twitter_last_req_ts = time.time()
        seen = redis_get("twitter_seen", [])
        now_ts = int(time.time())
        new_seen = list(seen)
        sent_count = 0
        try:
            headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
            user_ids = ",".join(TWITTER_USERS)
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                headers=headers,
                params={
                    "query": f"from:({user_ids.replace(',', ' OR from:')}) -is:retweet",
                    "max_results": 20,
                    "tweet.fields": "created_at,author_id,public_metrics",
                    "expansions": "author_id",
                    "user.fields": "username"
                },
                timeout=15
            )
            if resp.status_code == 401:
                write_debug_log("TWITTER | 401 Unauthorized")
                return
            if resp.status_code == 429:
                reset_ts = int(resp.headers.get("x-rate-limit-reset", time.time() + 900))
                _twitter_set_retry_after(reset_ts)
                write_debug_log(f"TWITTER | 429 — retry after {reset_ts - time.time():.0f}s")
                return
            if resp.status_code != 200:
                write_debug_log(f"TWITTER | HTTP {resp.status_code}")
                return
            data = resp.json()
            tweets = data.get("data", [])
            users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
            write_debug_log(f"TWITTER | tweets={len(tweets)}")
            for tweet in tweets:
                if sent_count >= RSS_MAX_PER_RUN:
                    break
                tweet_id = tweet.get("id")
                if tweet_id in seen:
                    continue
                text = tweet.get("text", "")
                if not text:
                    continue
                if not any(kw in text.lower() for kw in RSS_KEYWORDS):
                    continue
                try:
                    author_id = tweet.get("author_id", "")
                    username = users.get(author_id, "unknown")
                    created = tweet.get("created_at", "")[:16].replace("T", " ")
                    metrics = tweet.get("public_metrics", {})
                    likes = metrics.get("like_count", 0)
                    retweets = metrics.get("retweet_count", 0)
                    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"
                    is_ru = _is_russian(text)
                    text_out = text if is_ru else translate_to_ru(text)
                    if not is_ru and not _is_russian(text_out):
                        write_debug_log(f"TWITTER | SKIP (no translation): {text[:60]}")
                        continue
                    parts = [f"🐦 <b>Twitter / @{username}</b>"]
                    meta = []
                    if created:
                        meta.append(f"🕐 {created} UTC")
                    if likes or retweets:
                        meta.append(f"❤️ {likes} | 🔄 {retweets}")
                    if meta:
                        parts.append("  |  ".join(meta))
                    parts.append("")
                    text_clean = re.sub(r'http\S+', '', text_out).strip()
                    parts.append(f"<b>{text_clean[:280]}</b>")
                    if not is_ru and text_out != text:
                        orig_clean = re.sub(r'http\S+', '', text).strip()
                        parts.append(f"<i>{orig_clean[:200]}</i>")
                    parts.append(f"🔗 {tweet_url}")
                    send_news("\n".join(parts))
                    new_seen.append(tweet_id)
                    sent_count += 1
                    time.sleep(3)
                except Exception as e:
                    write_debug_log(f"TWITTER_SEND_ERR | {e}")
        except requests.exceptions.ConnectionError as e:
            write_debug_log(f"TWITTER_CONN_ERR | {e}")
        except requests.exceptions.Timeout:
            write_debug_log("TWITTER_TIMEOUT")
        except Exception as e:
            write_debug_log(f"TWITTER_ERR | {e}")
        if new_seen != seen:
            redis_set("twitter_seen", new_seen[-500:])
        write_debug_log(f"TWITTER | done | sent={sent_count} | cached={len(new_seen)}")

# ── TELEGRAM CHANNELS PARSER ─────────────────────────────────────
def _check_telegram_channels():
    """
    Парсит сообщения из указанных Telegram-каналов.
    Использует Bot API для получения обновлений.
    """
    global _tg_last_check_ts
    with _telegram_lock:
        now = time.time()
        if now - _tg_last_check_ts < TG_MIN_INTERVAL:
            write_debug_log("TELEGRAM | пропуск: слишком часто")
            return
        _tg_last_check_ts = now
        seen = redis_get("telegram_seen", [])
        now_ts = int(time.time())
        new_seen = list(seen)
        sent_count = 0
        for channel_id in TELEGRAM_CHANNELS:
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                    json={"limit": 100},
                    timeout=10
                )
                if resp.status_code != 200:
                    continue
                updates = resp.json().get("result", [])
                for update in updates:
                    msg = update.get("channel_post") or update.get("message")
                    if not msg:
                        continue
                    chat = msg.get("chat", {})
                    if str(chat.get("id")) != channel_id:
                        continue
                    msg_id = str(msg.get("message_id"))
                    cache_key = f"{channel_id}:{msg_id}"
                    if cache_key in seen or cache_key in new_seen:
                        continue
                    text = msg.get("text") or msg.get("caption", "")
                    if not text:
                        continue
                    if not any(kw in text.lower() for kw in RSS_KEYWORDS):
                        continue
                    try:
                        date = msg.get("date", 0)
                        date_str = datetime.datetime.fromtimestamp(date).strftime("%Y-%m-%d %H:%M") if date else ""
                        is_ru = _is_russian(text)
                        text_out = text if is_ru else translate_to_ru(text)
                        if not is_ru and not _is_russian(text_out):
                            continue
                        channel_title = chat.get("title", "Unknown")
                        parts = [f"💬 <b>Telegram / {channel_title}</b>"]
                        if date_str:
                            parts.append(f"🕐 {date_str}")
                        parts.append("")
                        text_clean = re.sub(r'http\S+', '', text_out).strip()[:400]
                        parts.append(f"<b>{text_clean}</b>")
                        if not is_ru and text_out != text:
                            orig_clean = re.sub(r'http\S+', '', text).strip()[:300]
                            parts.append(f"<i>{orig_clean}</i>")
                        send_news("\n".join(parts))
                        new_seen.append(cache_key)
                        sent_count += 1
                        time.sleep(3)
                    except Exception as e:
                        write_debug_log(f"TG_CHANNEL_SEND_ERR | {e}")
            except Exception as e:
                write_debug_log(f"TG_CHANNEL_ERR | {channel_id} | {e}")
        if new_seen != seen:
            redis_set("telegram_seen", new_seen[-500:])
        write_debug_log(f"TELEGRAM | done | sent={sent_count} | cached={len(new_seen)}")

# ── RSS-ПАРСЕР (через feedparser напрямую) ────────────────────────
_rss_lock = threading.Lock()

def _fetch_rss_items(url: str, name: str) -> list:
    try:
        feed = feedparser.parse(url)
        if feed.bozo and feed.status != 200:
            write_debug_log(f"RSS | {name} | feedparser error: {feed.bozo_exception}")
            return []
        entries = []
        for entry in feed.entries[:30]:
            item = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "pubDate": entry.get("published", entry.get("updated", ""))
            }
            entries.append(item)
        write_debug_log(f"RSS | {name} | feedparser OK | items={len(entries)}")
        return entries
    except Exception as e:
        write_debug_log(f"RSS | {name} | feedparser ERR: {e}")
        return []

def _check_rss_source(source: dict, seen: dict, now_ts: int) -> tuple:
    new_seen = dict(seen)
    sent_count = 0
    items = _fetch_rss_items(source["url"], source["name"])
    if not items:
        write_debug_log(f"RSS | {source['name']} | sent=0 (no items)")
        return new_seen, 0
    for item in items[:30]:
        if sent_count >= RSS_MAX_PER_RUN:
            break
        title = (item.get("title") or "").strip()
        if not title:
            continue
        url = (item.get("link") or "").strip()
        pub_str = (item.get("pubDate") or "")[:16]
        cache_key = url or hashlib.md5(title.encode()).hexdigest()
        if cache_key in new_seen:  # Проверяем против обновлённого кэша
            continue
        if not any(kw in title.lower() for kw in RSS_KEYWORDS):
            continue
        try:
            is_ru = _is_russian(title)
            title_out = title
            if not is_ru:
                title_out = translate_to_ru(title)
                if not _is_russian(title_out):
                    write_debug_log(f"RSS | {source['name']} | SKIP (no translation): {title[:60]}")
                    continue
            parts = [f"📰 <b>{source['flag']} {source['name']}</b>"]
            if pub_str:
                parts.append(f"🕐 {pub_str}")
            parts.append("")
            parts.append(f"<b>{title_out}</b>")
            if not is_ru and title_out != title:
                parts.append(f"<i>{title}</i>")
            send_news("\n".join(parts))
            new_seen[cache_key] = now_ts
            sent_count += 1
            time.sleep(3)
        except Exception as e:
            write_debug_log(f"RSS_SEND_ERR | {source['name']} | {e}")
    write_debug_log(f"RSS | {source['name']} | sent={sent_count}")
    return new_seen, sent_count

def _check_rss_all():
    with _rss_lock:
        seen = load_rss_seen()
        if isinstance(seen, list):
            seen = {url: 0 for url in seen}
        now_ts = int(time.time())
        seen = {k: v for k, v in seen.items() if v > now_ts - 30 * 86400}
        for source in RSS_SOURCES:
            seen, _ = _check_rss_source(source, seen, now_ts)
        save_rss_seen(seen)

# ── ПЛАНИРОВЩИК ───────────────────────────────────────────────────
_last_news_ts = 0
_last_twitter_ts = 0
_last_telegram_ts = 0

def _scheduler():
    global _last_news_ts, _last_telegram_ts
    while True:
        try:
            now_ts = int(time.time())
            # CryptoPanic каждые 30 мин
            if now_ts - _last_news_ts >= 1800:
                _last_news_ts = now_ts
                threading.Thread(target=_check_news, daemon=True).start()
                threading.Thread(target=_check_rss_all, daemon=True).start()
            # Telegram каналы каждые 15 мин
            if now_ts - _last_telegram_ts >= 900:
                _last_telegram_ts = now_ts
                threading.Thread(target=_check_telegram_channels, daemon=True).start()
        except Exception as e:
            write_debug_log(f"SCHEDULER_ERR | {e}")
        time.sleep(60)

# ── ЗАПУСК ────────────────────────────────────────────────────────
threading.Thread(target=_scheduler, daemon=True).start()
threading.Thread(target=lambda: (time.sleep(10), _do_register_webhook()), daemon=True).start()

# Для gunicorn
application = app

"""
Telegram Bot — Render.com: Новости из CryptoPanic, Nitter RSS, Telegram (RSSHub)
"""
from __future__ import annotations
import json, os, datetime, threading, time, hashlib, re
from flask import Flask, request, jsonify
import requests
import telebot
import feedparser
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── REDIS КЭШ (Upstash REST API через HTTP) ────────────────────────
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

class UpstashRedisClient:
    """Простой клиент для Upstash Redis REST API через HTTP."""
    def __init__(self, url: str, token: str):
        self.url = url.rstrip('/')
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}
    
    def _request(self, command: list):
        """Отправляет команду Redis через REST API."""
        try:
            resp = requests.post(
                f"{self.url}/pipeline",
                headers=self.headers,
                json=command,
                timeout=5
            )
            if resp.status_code == 200:
                result = resp.json()
                if isinstance(result, list) and len(result) > 0:
                    return result[0].get("result")
            return None
        except Exception as e:
            _write_log_file(f"UPSTASH_HTTP_ERR | {e}")
            return None
    
    def get(self, key: str):
        """GET key"""
        result = self._request(["GET", key])
        return result
    
    def set(self, key: str, value: str, ex: int = None):
        """SET key value [EX seconds]"""
        if ex:
            return self._request(["SET", key, value, "EX", str(ex)])
        return self._request(["SET", key, value])
    
    def delete(self, key: str):
        """DEL key"""
        return self._request(["DEL", key])
    
    def lpush(self, key: str, value: str):
        """LPUSH key value"""
        return self._request(["LPUSH", key, value])
    
    def ltrim(self, key: str, start: int, stop: int):
        """LTRIM key start stop"""
        return self._request(["LTRIM", key, str(start), str(stop)])
    
    def lrange(self, key: str, start: int, stop: int):
        """LRANGE key start stop"""
        return self._request(["LRANGE", key, str(start), str(stop)])
    
    def ping(self):
        """PING"""
        return self._request(["PING"])

_redis_client = None

def get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        try:
            _redis_client = UpstashRedisClient(UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN)
            # Тестируем соединение
            if _redis_client.ping():
                _write_log_file("REDIS_OK | Подключено к Upstash REST API (HTTP)")
                return _redis_client
        except Exception as e:
            _write_log_file(f"UPSTASH_INIT_ERR | {e}")
    
    _redis_client = None
    return None

def redis_get(key: str, default=None):
    try:
        r = get_redis()
        if r:
            val = r.get(key)
            if val:
                return json.loads(val)
    except Exception as e:
        _write_log_file(f"REDIS_GET_ERR | {e}")
    return default

def redis_set(key: str, data, ex=2592000):
    try:
        r = get_redis()
        if r:
            r.set(key, json.dumps(data), ex=ex)
            return True
    except Exception as e:
        _write_log_file(f"REDIS_SET_ERR | {e}")
    return False

# ── КОНФИГУРАЦИЯ ─────────────────────────────────────────────────
_raw_token = os.environ.get("BOT_TOKEN_NEWS", "")
if not _raw_token or ":" not in _raw_token:
    raise RuntimeError(f"BOT_TOKEN_NEWS неверный или не установлен: {_raw_token!r}")
TOKEN   = _raw_token
CHAT_ID = os.environ.get("CHAT_ID", "-1003867089540")

NEWS_TOPIC_ID = os.environ.get("NEWS_TOPIC_ID", "9505")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(BASE_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN, threaded=False)

# ── CRYPTOPANIC ──────────────────────────────────────────────────
CRYPTOPANIC_TOKEN = os.environ.get("CRYPTOPANIC_TOKEN", "")
CRYPTOPANIC_URL   = "https://cryptopanic.com/api/developer/v2/posts/"
RSS_MAX_PER_RUN   = 3

_cp_lock        = threading.Lock()
_cp_last_req_ts = 0.0
CP_MIN_INTERVAL = 60.0

# ── NITTER (бесплатная замена Twitter API) ───────────────────────
# Nitter — open-source фронтенд Twitter с RSS, без токенов и оплаты.
# Если один инстанс недоступен — бот автоматически пробует следующий.
NITTER_INSTANCES = [
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.1d4.us",
    "nitter.kavin.rocks",
    "nitter.cz",
    "nitter.net",
    "nitter.it",
    "nitter.moomoo.me",
    "nitter.fdn.fr",
    "nitter.nixnet.services",
]

# Аккаунты для мониторинга (username без @)
NITTER_ACCOUNTS = [
    {"name": "Wu Blockchain",   "username": "Wu_Blockchain",   "flag": "🐋"},
    {"name": "Whale Alert",     "username": "whale_alert",      "flag": "🚨"},
    {"name": "Tier10k",         "username": "tier10k",          "flag": "📊"},
    {"name": "Trump",           "username": "realDonaldTrump",  "flag": "🇺🇸"},
    {"name": "Elon Musk",       "username": "elonmusk",         "flag": "🚀"},
    {"name": "Federal Reserve", "username": "federalreserve",   "flag": "🏦"},
]

_nitter_rss_lock = threading.Lock()

def _get_nitter_instance() -> str | None:
    """Ищет рабочий Nitter-инстанс, кэширует в Redis на 30 мин."""
    cached = redis_get("nitter_instance_cache")
    if cached:
        return cached
    for inst in NITTER_INSTANCES:
        try:
            r = requests.get(f"https://{inst}/Wu_Blockchain/rss", timeout=8)
            if r.status_code == 200 and "<rss" in r.text[:300]:
                redis_set("nitter_instance_cache", inst, ex=1800)
                write_debug_log(f"NITTER | рабочий инстанс: {inst}")
                return inst
        except Exception:
            continue
    write_debug_log("NITTER | все инстансы недоступны")
    return None

# ── TELEGRAM КАНАЛЫ ЧЕРЕЗ RSSHUB ────────────────────────────────
# Для чтения ПУБЛИЧНЫХ чужих каналов используем RSSHub.
# Бот НЕ нужно добавлять в эти каналы — нужен только публичный username.
#
# Как найти username канала:
#   Telegram → открой канал → три точки → Поделиться → в ссылке t.me/XXXX
#   XXXX и есть username (только для публичных каналов)
#
# ЗАПОЛНИ ЭТОТ СПИСОК:
TELEGRAM_RSS_CHANNELS = [
    # {"name": "Durov",       "username": "durov",       "flag": "✈️"},
    # {"name": "Cbonds News", "username": "cbondsnews",  "flag": "📈"},
    # {"name": "RBK Крипто",  "username": "rbc_crypto",  "flag": "💹"},
]

# RSSHub публичный инстанс (можно заменить на self-hosted):
RSSHUB_BASE = "https://rsshub.app"

_tg_rsshub_lock   = threading.Lock()
_tg_rsshub_last_ts = 0.0

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
    "иран","саудовская аравия","персидский залив","израиль",
    "атака","удар","пожар","взрыв","ракет","дрон","бомбардировк","войск",
    "эскалация","война","войны","бое","боевые действия",
    "радиологический","ядерный","радиация","катастрофа",
    "гладков","белгородская","отставка","губернатор","власти","правительство",
    "мид рф","мид","россия","украина",
    "m2","денежная масса","ликвидность","триллион",
    "выступление","отчет","данные","статистика","публикация","заседание",
    "председатель","пауэлл","fomc","комитет",
    "стартап","founder","создатель",
    "polymarket","прогноз","вероятность","шанс",
]

# ── RSS ИСТОЧНИКИ ─────────────────────────────────────────────────
RSS_SOURCES = [
    {"name": "CoinDesk",       "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",     "flag": "🪙"},
    {"name": "CoinTelegraph",  "url": "https://cointelegraph.com/rss",                       "flag": "📡"},
    {"name": "Decrypt",        "url": "https://decrypt.co/feed",                             "flag": "🔓"},
    {"name": "BeInCrypto",     "url": "https://beincrypto.com/feed/",                        "flag": "📊"},
    {"name": "The Block",      "url": "https://www.theblock.co/rss.xml",                     "flag": "🧱"},
    {"name": "FT Markets",     "url": "https://www.ft.com/rss/home/uk",                      "flag": "💹"},
    {"name": "TechCrunch AI",  "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "flag": "🤖"},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/",           "flag": "💡"},
    # Reuters — напрямую блокируют feedparser, используем через RSSHub
    {"name": "Reuters Biz",    "url": "https://rsshub.app/reuters/businessNews",             "flag": "🗞️"},
    {"name": "Reuters Mkts",   "url": "https://rsshub.app/reuters/marketsNews",              "flag": "📈"},
]

# ── GOOGLE TRANSLATE ─────────────────────────────────────────────
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

# ── DEBUG LOG ────────────────────────────────────────────────────
def _write_log_file(entry: str):
    """Запись только в файл (без Redis, чтобы не было рекурсии)."""
    try:
        log_path = os.path.join(BASE_DIR, "webhook_debug.log")
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

def write_debug_log(entry: str):
    """Пишет в файл И дублирует в Redis — лог не теряется при рестарте Render."""
    _write_log_file(entry)
    try:
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {entry}"
        r = get_redis()
        if r:
            r.lpush("debug_log", line)
            r.ltrim("debug_log", 0, 199)  # хранить последние 200 строк
    except Exception:
        pass

# ── REDIS ХЕЛПЕРЫ ────────────────────────────────────────────────
def load_rss_seen():
    return redis_get("rss_seen", {})

def save_rss_seen(d):
    redis_set("rss_seen", d)

def _cp_get_retry_after() -> float:
    return redis_get("cp_retry_after", 0.0)

def _cp_set_retry_after(until_ts: float):
    redis_set("cp_retry_after", until_ts, ex=3600)

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
    nitter_inst = redis_get("nitter_instance_cache", "—")
    return jsonify({
        "status": "ok",
        "server": "news_bot_render",
        "webhook_ok": _webhook_ok,
        "cached_news": len(load_rss_seen()),
        "news_topic": NEWS_TOPIC_ID,
        "nitter_instance": nitter_inst,
        "features": ["cryptopanic", "rss", "nitter_rss", "telegram_rsshub"]
    })

@app.route("/setup")
def setup_webhook():
    ok = _do_register_webhook()
    return jsonify({"status": "ok" if ok else "error", "webhook_ok": _webhook_ok})

@app.route("/debug")
def debug_log_route():
    # Сначала пробуем Redis (переживает рестарты Render)
    try:
        r = get_redis()
        if r:
            lines = r.lrange("debug_log", 0, 49)
            if lines:
                return jsonify({"log": "\n".join(reversed(lines)), "source": "redis", "total": len(lines)})
    except Exception:
        pass
    # Fallback — локальный файл
    log_path = os.path.join(BASE_DIR, "webhook_debug.log")
    if not os.path.exists(log_path):
        return jsonify({"log": "Лог пуст"})
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"log": "".join(lines[-50:]), "source": "file", "total_lines": len(lines)})
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

# ── TELEGRAM КОМАНДЫ ─────────────────────────────────────────────
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
• /news [текст] — Опубликовать новость вручную
• /check_news — Запустить парсинг прямо сейчас
• /news_status — Статус бота, кэш, источники
• /clear_cache — Очистить кэш новостей
• /nitter_check — Проверить доступность Nitter

<b>🌐 Открыть в браузере:</b>
• /debug — Лог (из Redis, не теряется при рестарте)
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
            _check_nitter_all()
            _check_telegram_rsshub()
            _reply(message, "✅ Готово. Подробности: /debug")
        except Exception as e:
            _reply(message, f"❌ Ошибка: {e}")
    threading.Thread(target=_run, daemon=True).start()

@bot.message_handler(commands=["nitter_check"])
def cmd_nitter_check(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    # Сбросить кэш и перепроверить
    try:
        r = get_redis()
        if r:
            r.delete("nitter_instance_cache")
    except Exception:
        pass
    inst = _get_nitter_instance()
    if inst:
        _reply(message, f"✅ Nitter работает: <code>{inst}</code>")
    else:
        _reply(message, "❌ Все Nitter-инстансы недоступны.\nПроверь список NITTER_INSTANCES в коде.")

@bot.message_handler(commands=["news_status"])
def cmd_news_status(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    seen       = load_rss_seen()
    nitter_seen = redis_get("nitter_seen", {})
    tg_seen    = redis_get("telegram_rsshub_seen", {})
    tok_ok     = "✅ Установлен" if CRYPTOPANIC_TOKEN else "⚠️ Не установлен"
    nitter_inst = redis_get("nitter_instance_cache", "не определён")
    rss_list    = "\n".join(f"  {s['flag']} {s['name']}" for s in RSS_SOURCES)
    nitter_list = "\n".join(f"  {a['flag']} @{a['username']}" for a in NITTER_ACCOUNTS)
    tg_list     = "\n".join(f"  💬 {ch['name']}" for ch in TELEGRAM_RSS_CHANNELS) or "  (не настроено)"
    _reply(message, f"""📰 <b>Статус новостей</b>

🔑 CryptoPanic: {tok_ok}
📦 Кэш RSS: {len(seen)} | Nitter: {len(nitter_seen)} | TG: {len(tg_seen)}
⏱ RSS+Nitter: каждые 30 мин | TG: каждые 15 мин
📬 Топик: #{NEWS_TOPIC_ID}
🐦 Nitter-инстанс: {nitter_inst}

<b>📂 RSS источники ({len(RSS_SOURCES)}):</b>
{rss_list}

<b>🐦 Nitter аккаунты ({len(NITTER_ACCOUNTS)}):</b>
{nitter_list}

<b>💬 Telegram каналы ({len(TELEGRAM_RSS_CHANNELS)}):</b>
{tg_list}""")

@bot.message_handler(commands=["clear_cache"])
def cmd_clear_cache(message):
    if not is_bot_admin(message.from_user.id):
        _reply(message, "❌ Только для администраторов")
        return
    save_rss_seen({})
    redis_set("nitter_seen", {})
    redis_set("telegram_rsshub_seen", {})
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
            write_debug_log("CRYPTOPANIC | пропуск: слишком рано")
            return
        _cp_last_req_ts = time.time()
        seen = load_rss_seen()
        if isinstance(seen, list):
            seen = {url: 0 for url in seen}
        now_ts = int(time.time())
        seen = {k: v for k, v in seen.items() if v > now_ts - 30 * 86400}
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
                source  = item.get("source", {}).get("title", "")
                domain  = item.get("source", {}).get("domain", "")
                pub_raw = item.get("published_at", "")
                pub_str = pub_raw[:16].replace("T", " ") + " UTC" if pub_raw else ""
                panic   = item.get("panic_score")
                try:
                    is_ru     = _is_russian(title)
                    title_out = title if is_ru else translate_to_ru(title)
                    if not is_ru and not _is_russian(title_out):
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

# ── RSS ПАРСЕР ────────────────────────────────────────────────────
_rss_lock = threading.Lock()

def _fetch_rss_items(url: str, name: str) -> list:
    try:
        feed = feedparser.parse(url)
        # ФИКС: используем getattr — Reuters и другие иногда не возвращают .status
        status = getattr(feed, "status", 200)
        if feed.bozo and status != 200:
            write_debug_log(f"RSS | {name} | feedparser error: {feed.bozo_exception}")
            return []
        entries = []
        for entry in feed.entries[:30]:
            item = {
                "title":   entry.get("title", ""),
                "link":    entry.get("link", ""),
                "pubDate": entry.get("published", entry.get("updated", ""))
            }
            entries.append(item)
        write_debug_log(f"RSS | {name} | feedparser OK | items={len(entries)}")
        return entries
    except Exception as e:
        write_debug_log(f"RSS | {name} | feedparser ERR: {e}")
        return []

def _check_rss_source(source: dict, seen: dict, now_ts: int) -> tuple:
    new_seen   = dict(seen)
    sent_count = 0
    items = _fetch_rss_items(source["url"], source["name"])
    if not items:
        write_debug_log(f"RSS | {source['name']} | sent=0 (no items)")
        return new_seen, 0
    for item in items[:30]:
        if sent_count >= RSS_MAX_PER_RUN:
            break
        title   = (item.get("title") or "").strip()
        if not title:
            continue
        url     = (item.get("link") or "").strip()
        pub_str = (item.get("pubDate") or "")[:16]
        cache_key = url or hashlib.md5(title.encode()).hexdigest()
        if cache_key in new_seen:
            continue
        if not any(kw in title.lower() for kw in RSS_KEYWORDS):
            continue
        try:
            is_ru     = _is_russian(title)
            title_out = title if is_ru else translate_to_ru(title)
            if not is_ru and not _is_russian(title_out):
                write_debug_log(f"RSS | {source['name']} | SKIP: {title[:60]}")
                continue
            parts = [f"📰 <b>{source['flag']} {source['name']}</b>"]
            if pub_str:
                parts.append(f"🕐 {pub_str}")
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

# ── NITTER RSS (замена Twitter API, бесплатно) ────────────────────
def _check_nitter_all():
    """
    Парсит RSS-ленты Twitter-аккаунтов через Nitter.
    Полностью бесплатно, не требует API-ключей.
    Автоматически ищет рабочий Nitter-инстанс из списка NITTER_INSTANCES.
    """
    with _nitter_rss_lock:
        inst = _get_nitter_instance()
        if not inst:
            write_debug_log("NITTER | пропуск: нет рабочих инстансов")
            return
        seen = redis_get("nitter_seen", {})
        if isinstance(seen, list):
            seen = {k: 0 for k in seen}
        now_ts = int(time.time())
        seen = {k: v for k, v in seen.items() if v > now_ts - 30 * 86400}
        new_seen = dict(seen)
        total_sent = 0
        for account in NITTER_ACCOUNTS:
            username = account["username"]
            name     = account["name"]
            flag     = account["flag"]
            rss_url  = f"https://{inst}/{username}/rss"
            try:
                feed   = feedparser.parse(rss_url)
                status = getattr(feed, "status", 200)
                if feed.bozo and status != 200:
                    write_debug_log(f"NITTER | @{username} | ошибка: {feed.bozo_exception}")
                    # Сбрасываем кэш инстанса — попробуем другой в следующем цикле
                    try:
                        get_redis().delete("nitter_instance_cache")
                    except Exception:
                        pass
                    continue
                write_debug_log(f"NITTER | @{username} | items={len(feed.entries)}")
                sent_count = 0
                for entry in feed.entries[:20]:
                    if sent_count >= RSS_MAX_PER_RUN:
                        break
                    title   = (entry.get("title") or "").strip()
                    link    = (entry.get("link") or "").strip()
                    pub_str = (entry.get("published", ""))[:16]
                    if not title:
                        continue
                    cache_key = link or hashlib.md5(title.encode()).hexdigest()
                    if cache_key in new_seen:
                        continue
                    # Полный текст — из summary (убираем HTML теги)
                    summary    = entry.get("summary", title)
                    text_clean = re.sub(r'<[^>]+>', '', summary).strip()
                    text_clean = re.sub(r'http\S+', '', text_clean).strip()
                    if not any(kw in text_clean.lower() for kw in RSS_KEYWORDS):
                        new_seen[cache_key] = now_ts
                        continue
                    try:
                        is_ru    = _is_russian(text_clean)
                        text_out = text_clean if is_ru else translate_to_ru(text_clean[:400])
                        if not is_ru and not _is_russian(text_out):
                            new_seen[cache_key] = now_ts
                            continue
                        parts = [f"🐦 <b>{flag} {name} (@{username})</b>"]
                        if pub_str:
                            parts.append(f"🕐 {pub_str}")
                        parts.append("")
                        parts.append(f"<b>{text_out[:400]}</b>")
                        if not is_ru and text_out != text_clean:
                            parts.append(f"<i>{text_clean[:200]}</i>")
                        # Конвертируем ссылку из Nitter → оригинальный Twitter
                        tw_link = re.sub(rf"https?://{re.escape(inst)}", "https://twitter.com", link)
                        if tw_link:
                            parts.append(f"🔗 {tw_link}")
                        send_news("\n".join(parts))
                        new_seen[cache_key] = now_ts
                        sent_count += 1
                        total_sent += 1
                        time.sleep(3)
                    except Exception as e:
                        write_debug_log(f"NITTER_SEND_ERR | @{username} | {e}")
                write_debug_log(f"NITTER | @{username} | sent={sent_count}")
            except Exception as e:
                write_debug_log(f"NITTER_ERR | @{username} | {e}")
        redis_set("nitter_seen", new_seen)
        write_debug_log(f"NITTER | total sent={total_sent} | cached={len(new_seen)}")

# ── TELEGRAM КАНАЛЫ ЧЕРЕЗ RSSHUB ─────────────────────────────────
def _check_telegram_rsshub():
    """
    Читает публичные Telegram-каналы через RSSHub.
    Бот НЕ нужен в этих каналах — только публичный username.
    Заполни TELEGRAM_RSS_CHANNELS в начале файла.
    """
    global _tg_rsshub_last_ts
    with _tg_rsshub_lock:
        now = time.time()
        if now - _tg_rsshub_last_ts < 300:
            return
        _tg_rsshub_last_ts = now
        if not TELEGRAM_RSS_CHANNELS:
            write_debug_log("TG_RSSHUB | каналы не настроены")
            return
        seen = redis_get("telegram_rsshub_seen", {})
        if isinstance(seen, list):
            seen = {k: 0 for k in seen}
        now_ts = int(time.time())
        seen = {k: v for k, v in seen.items() if v > now_ts - 30 * 86400}
        new_seen   = dict(seen)
        total_sent = 0
        for ch in TELEGRAM_RSS_CHANNELS:
            username = ch["username"]
            name     = ch["name"]
            flag     = ch.get("flag", "💬")
            rss_url  = f"{RSSHUB_BASE}/telegram/channel/{username}"
            try:
                feed   = feedparser.parse(rss_url)
                status = getattr(feed, "status", 200)
                if feed.bozo and status != 200:
                    write_debug_log(f"TG_RSSHUB | @{username} | ERR: {feed.bozo_exception}")
                    continue
                write_debug_log(f"TG_RSSHUB | @{username} | items={len(feed.entries)}")
                sent_count = 0
                for entry in feed.entries[:20]:
                    if sent_count >= RSS_MAX_PER_RUN:
                        break
                    link    = (entry.get("link") or "").strip()
                    summary = entry.get("summary", entry.get("title", ""))
                    text    = re.sub(r'<[^>]+>', '', summary).strip()
                    text    = re.sub(r'http\S+', '', text).strip()
                    if not text:
                        continue
                    cache_key = link or hashlib.md5(text.encode()).hexdigest()
                    if cache_key in new_seen:
                        continue
                    if not any(kw in text.lower() for kw in RSS_KEYWORDS):
                        new_seen[cache_key] = now_ts
                        continue
                    pub_str = (entry.get("published", ""))[:16]
                    try:
                        is_ru    = _is_russian(text)
                        text_out = text if is_ru else translate_to_ru(text[:400])
                        if not is_ru and not _is_russian(text_out):
                            new_seen[cache_key] = now_ts
                            continue
                        parts = [f"{flag} <b>Telegram / {name}</b>"]
                        if pub_str:
                            parts.append(f"🕐 {pub_str}")
                        parts.append("")
                        parts.append(f"<b>{text_out[:400]}</b>")
                        if not is_ru and text_out != text:
                            parts.append(f"<i>{text[:200]}</i>")
                        if link:
                            parts.append(f"🔗 {link}")
                        send_news("\n".join(parts))
                        new_seen[cache_key] = now_ts
                        sent_count += 1
                        total_sent += 1
                        time.sleep(3)
                    except Exception as e:
                        write_debug_log(f"TG_RSSHUB_SEND_ERR | @{username} | {e}")
                write_debug_log(f"TG_RSSHUB | @{username} | sent={sent_count}")
            except Exception as e:
                write_debug_log(f"TG_RSSHUB_ERR | @{username} | {e}")
        redis_set("telegram_rsshub_seen", new_seen)
        write_debug_log(f"TG_RSSHUB | total sent={total_sent} | cached={len(new_seen)}")

# ── ПЛАНИРОВЩИК ───────────────────────────────────────────────────
_last_news_ts   = 0
_last_nitter_ts = 300   # сдвиг 5 мин от RSS чтобы не перегружать
_last_tg_ts     = 0

def _scheduler():
    global _last_news_ts, _last_nitter_ts, _last_tg_ts
    # Ищем рабочий Nitter при старте
    threading.Thread(target=_get_nitter_instance, daemon=True).start()
    while True:
        try:
            now_ts = int(time.time())
            # CryptoPanic + RSS каждые 30 мин
            if now_ts - _last_news_ts >= 1800:
                _last_news_ts = now_ts
                threading.Thread(target=_check_news,    daemon=True).start()
                threading.Thread(target=_check_rss_all, daemon=True).start()
            # Nitter RSS каждые 30 мин (со сдвигом)
            if now_ts - _last_nitter_ts >= 1800:
                _last_nitter_ts = now_ts
                threading.Thread(target=_check_nitter_all, daemon=True).start()
            # Telegram каналы каждые 15 мин
            if now_ts - _last_tg_ts >= 900:
                _last_tg_ts = now_ts
                threading.Thread(target=_check_telegram_rsshub, daemon=True).start()
        except Exception as e:
            write_debug_log(f"SCHEDULER_ERR | {e}")
        time.sleep(60)

# ── ЗАПУСК ────────────────────────────────────────────────────────
threading.Thread(target=_scheduler, daemon=True).start()
threading.Thread(target=lambda: (time.sleep(10), _do_register_webhook()), daemon=True).start()

# Для gunicorn
application = app

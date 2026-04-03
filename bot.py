"""
PolyBot — Polymarket Prediction Probability Telegram Bot
With Telegram Stars subscription system (3 tiers)
Powered by Groq (FREE AI) + Polymarket API
"""

import os
import json
import logging
import sqlite3
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    ContextTypes, filters
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY",   "YOUR_GROQ_API_KEY")
GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.3-70b-versatile"
GAMMA_API      = "https://gamma-api.polymarket.com"
CLOB_API       = "https://clob.polymarket.com"
DB_PATH        = "polybot.db"

TIERS = {
    "basic": {"name": "🥉 Basic", "stars": 500, "daily": 10, "days": 30, "label": "Basic — 10 analyses/day", "perks": "10 analyses per day"},
    "pro":   {"name": "🥈 Pro",   "stars": 1000,"daily": 50, "days": 30, "label": "Pro — 50 analyses/day",   "perks": "50 analyses per day"},
    "whale": {"name": "🐳 Whale", "stars": 2500,"daily": 999999,"days": 30,"label": "Whale — Unlimited",    "perks": "Unlimited analyses"},
}
FREE_ANALYSES = 3

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT,
        tier TEXT DEFAULT 'free', expires_at TEXT DEFAULT NULL,
        daily_used INTEGER DEFAULT 0, last_reset TEXT DEFAULT NULL,
        total_used INTEGER DEFAULT 0, free_used INTEGER DEFAULT 0)""")
    con.commit()
    con.close()

def get_user(user_id):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return dict(row) if row else None

def upsert_user(user_id, username):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username = excluded.username", (user_id, username or ""))
    con.commit()
    con.close()

def apply_subscription(user_id, tier_key):
    tier = TIERS[tier_key]
    expires = (datetime.utcnow() + timedelta(days=tier["days"])).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET tier=?, expires_at=?, daily_used=0, last_reset=? WHERE user_id=?",
                (tier_key, expires, datetime.utcnow().date().isoformat(), user_id))
    con.commit()
    con.close()

def check_and_reset_daily(user):
    today = datetime.utcnow().date().isoformat()
    if user["last_reset"] != today:
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE users SET daily_used=0, last_reset=? WHERE user_id=?", (today, user["user_id"]))
        con.commit()
        con.close()
        user["daily_used"] = 0
        user["last_reset"] = today
    return user

def is_subscription_active(user):
    if user["tier"] == "free" or not user["expires_at"]:
        return False
    return datetime.utcnow() < datetime.fromisoformat(user["expires_at"])

def can_analyze(user):
    user = check_and_reset_daily(user)
    if user["tier"] == "free" or not is_subscription_active(user):
        if user["free_used"] < FREE_ANALYSES:
            return True, "free"
        return False, "no_sub"
    tier = TIERS.get(user["tier"], {})
    if user["daily_used"] >= tier.get("daily", 0):
        return False, "daily_limit"
    return True, "ok"

def record_usage(user_id, is_free=False):
    con = sqlite3.connect(DB_PATH)
    if is_free:
        con.execute("UPDATE users SET free_used=free_used+1, total_used=total_used+1 WHERE user_id=?", (user_id,))
    else:
        con.execute("UPDATE users SET daily_used=daily_used+1, total_used=total_used+1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()def search_markets(query, limit=5):
    try:
        r = requests.get(f"{GAMMA_API}/markets",
            params={"q": query, "limit": limit, "active": "true", "closed": "false"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("markets", [])
    except Exception as e:
        log.error(f"search_markets: {e}")
        return []

def fetch_trending_markets(limit=5):
    try:
        r = requests.get(f"{GAMMA_API}/markets",
            params={"limit": limit, "active": "true", "closed": "false",
                    "order": "volume", "ascending": "false"}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("markets", [])
    except Exception as e:
        log.error(f"fetch_trending: {e}")
        return []

def get_market_detail(condition_id):
    try:
        r = requests.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        if r.ok:
            return r.json()
    except Exception:
        pass
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={"conditionId": condition_id}, timeout=10)
        if r.ok:
            data = r.json()
            return data[0] if data else None
    except Exception as e:
        log.error(f"get_market_detail: {e}")
    return None

def parse_outcomes(market):
    outcomes = []
    if "outcomes" in market and "outcomePrices" in market:
        names  = json.loads(market["outcomes"])      if isinstance(market["outcomes"],      str) else market["outcomes"]
        prices = json.loads(market["outcomePrices"]) if isinstance(market["outcomePrices"], str) else market["outcomePrices"]
        for name, price in zip(names, prices):
            try:
                outcomes.append({"name": name, "probability": round(float(price) * 100, 1)})
            except Exception:
                pass
    elif "tokens" in market:
        for tok in market["tokens"]:
            outcomes.append({"name": tok.get("outcome", "?"),
                             "probability": round(float(tok.get("price", 0)) * 100, 1)})
    return outcomes

def ai_analyze(market, outcomes):
    question    = market.get("question", market.get("title", "Unknown"))
    volume      = market.get("volume",    market.get("volumeNum",    "N/A"))
    liquidity   = market.get("liquidity", market.get("liquidityNum", "N/A"))
    end_date    = market.get("endDate",   market.get("endDateIso",   "N/A"))
    description = market.get("description", "")[:500]
    outcomes_text = "\n".join(f"  - {o['name']}: {o['probability']}%" for o in outcomes)
    prompt = f"""You are PolyBot, an expert prediction market analyst.
MARKET: {question}
DESCRIPTION: {description}
VOLUME: ${volume}
LIQUIDITY: ${liquidity}
CLOSES: {end_date}
PROBABILITIES:\n{outcomes_text}

Respond in this format (max 280 words):
VERDICT
[most likely outcome and why]
BREAKDOWN
[one line per outcome]
KEY FACTORS
- [factor 1]
- [factor 2]
- [factor 3]
CONFIDENCE SIGNAL
[HIGH/MEDIUM/LOW and why]
RISK NOTE
[biggest tail risk]"""
    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        body = {"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": 700, "temperature": 0.7}
        r = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Groq error: {e}")
        return "AI analysis unavailable right now."

def bar(prob, width=12):
    filled = round(prob / 100 * width)
    return "█" * filled + "░" * (width - filled)

def format_market_card(market, outcomes):
    question = market.get("question", market.get("title", "Unknown"))
    volume   = market.get("volume",   market.get("volumeNum", "—"))
    end_date = market.get("endDate",  market.get("endDateIso", "—"))
    if end_date and end_date not in ("—", "N/A"):
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            end_date = dt.strftime("%b %d, %Y")
        except Exception:
            pass
    lines = [f"📈 *{question}*", f"💰 Volume: ${volume}  |  📅 Closes: {end_date}", "", "*Current Odds:*"]
    for o in sorted(outcomes, key=lambda x: x["probability"], reverse=True):
        emoji = "🟢" if o["probability"] >= 50 else ("🔴" if o["probability"] < 20 else "🟡")
        lines.append(f"{emoji} `{bar(o['probability'])}` *{o['name']}* — {o['probability']}%")
    return "\n".join(lines)

def tier_badge(tier_key):
    return TIERS.get(tier_key, {}).get("name", "🆓 Free")async def cmd_start(update, ctx):
    user = update.effective_user
    upsert_user(user.id, user.username)
    u = get_user(user.id)
    free_left = max(0, FREE_ANALYSES - u["free_used"])
    text = (f"👋 *Welcome to PolyBot!*\n\n"
            f"I analyze Polymarket prediction markets using live AI.\n\n"
            f"🎁 *Free trial:* {free_left} analyses remaining\n\n"
            f"📌 *Commands:*\n"
            f"• /search `<query>` — Search markets\n"
            f"• /trending — Top markets by volume\n"
            f"• /subscribe — View plans\n"
            f"• /status — Your current plan\n\n"
            f"💡 Just type anything to search!")
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update, ctx):
    await cmd_start(update, ctx)

async def cmd_status(update, ctx):
    user = update.effective_user
    upsert_user(user.id, user.username)
    u = check_and_reset_daily(get_user(user.id))
    active = is_subscription_active(u)
    if active:
        tier = TIERS[u["tier"]]
        expires = datetime.fromisoformat(u["expires_at"])
        days_left = (expires - datetime.utcnow()).days
        daily_left = max(0, tier["daily"] - u["daily_used"]) if tier["daily"] < 999999 else "∞"
        text = f"*Your Plan: {tier_badge(u['tier'])}*\n\n📅 Expires in: *{days_left} days*\n⚡ Analyses left today: *{daily_left}*\n📊 Total done: *{u['total_used']}*\n\nUse /subscribe to renew or upgrade."
    else:
        free_left = max(0, FREE_ANALYSES - u["free_used"])
        text = f"*Your Plan: 🆓 Free Trial*\n\n🎁 Free analyses left: *{free_left}/{FREE_ANALYSES}*\n📊 Total done: *{u['total_used']}*\n\nUse /subscribe to unlock full access."
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_subscribe(update, ctx):
    upsert_user(update.effective_user.id, update.effective_user.username)
    keyboard = [
        [InlineKeyboardButton("🥉 Basic — 500 ⭐/month",  callback_data="buy:basic")],
        [InlineKeyboardButton("🥈 Pro — 1000 ⭐/month",   callback_data="buy:pro")],
        [InlineKeyboardButton("🐳 Whale — 2500 ⭐/month", callback_data="buy:whale")],
    ]
    text = ("🔥 *PolyBot Subscription Plans*\n\n"
            "🥉 *Basic* — 500 Stars/month (~$7)\n  └ 10 analyses/day\n\n"
            "🥈 *Pro* — 1000 Stars/month (~$13)\n  └ 50 analyses/day\n\n"
            "🐳 *Whale* — 2500 Stars/month (~$33)\n  └ Unlimited analyses\n\n"
            "Subscription lasts 30 days from payment.")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_buy_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    _, tier_key = query.data.split(":", 1)
    tier = TIERS.get(tier_key)
    if not tier:
        return
    ctx.user_data["pending_tier"] = tier_key
    await ctx.bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"PolyBot {tier['name']}",
        description=f"30-day access · {tier['perks']}",
        payload=f"sub_{tier_key}_{query.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(tier["label"], tier["stars"])],
        provider_token="",
    )

async def handle_precheckout(update, ctx):
    await update.pre_checkout_query.answer(ok=True)

async def handle_successful_payment(update, ctx):
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id
    parts = payload.split("_")
    tier_key = parts[1] if len(parts) >= 2 else "basic"
    upsert_user(user_id, update.effective_user.username)
    apply_subscription(user_id, tier_key)
    tier = TIERS[tier_key]
    await update.message.reply_text(f"✅ *Payment confirmed!*\n\nWelcome to {tier['name']} 🎉\n\n• {tier['perks']}\n• Valid for 30 days\n\nUse /status to check your plan.", parse_mode="Markdown")

async def cmd_search(update, ctx):
    query = " ".join(ctx.args) if ctx.args else ""
    if not query:
        await update.message.reply_text("Usage: `/search <query>`", parse_mode="Markdown")
        return
    await update.message.reply_text(f"🔍 Searching: *{query}*...", parse_mode="Markdown")
    markets = search_markets(query, limit=5)
    if not markets:
        await update.message.reply_text("❌ No markets found. Try different keywords.")
        return
    keyboard = []
    lines = [f"🎯 *Results for \"{query}\":*\n"]
    ctx.user_data.setdefault("market_cache", {})
    for i, m in enumerate(markets, 1):
        q   = m.get("question", m.get("title", "Unknown"))[:65]
        vol = m.get("volume", m.get("volumeNum", "—"))
        mid = m.get("conditionId", m.get("id", ""))
        lines.append(f"{i}. {q}\n   💰 Vol: ${vol}")
        keyboard.append([InlineKeyboardButton(f"📊 Analyze #{i}", callback_data=f"analyze:{mid}")])
        ctx.user_data["market_cache"][mid] = m
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_trending(update, ctx):
    await update.message.reply_text("🔍 Fetching trending markets...")
    markets = fetch_trending_markets(limit=5)
    if not markets:
        await update.message.reply_text("⚠️ Couldn't fetch trending markets.")
        return
    keyboard = []
    lines = ["🔥 *Trending Markets Right Now:*\n"]
    for i, m in enumerate(markets, 1):
        q   = m.get("question", m.get("title", "Unknown"))[:60]
        vol = m.get("volume", m.get("volumeNum", "—"))
        mid = m.get("conditionId", m.get("id", ""))
        lines.append(f"{i}. {q}\n   💰 Vol: ${vol}")
        keyboard.append([InlineKeyboardButton(f"📊 Analyze #{i}", callback_data=f"analyze:{mid}")])
    ctx.user_data["trending_markets"] = {m.get("conditionId", m.get("id", "")): m for m in markets}
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_analyze_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    _, condition_id = query.data.split(":", 1)
    user = update.effective_user
    upsert_user(user.id, user.username)
    u = get_user(user.id)
    allowed, reason = can_analyze(u)
    if not allowed:
        if reason == "no_sub":
            keyboard = [[InlineKeyboardButton("💳 View Plans", callback_data="show_plans")]]
            await query.edit_message_text(f"🔒 *You've used all {FREE_ANALYSES} free analyses.*\n\nSubscribe to keep going!\n\nUse /subscribe to pick a plan.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        elif reason == "daily_limit":
            tier = TIERS.get(u["tier"], {})
            await query.edit_message_text(f"⏳ *Daily limit reached.*\n\nYour {tier_badge(u['tier'])} plan resets at midnight UTC.\n\nUpgrade with /subscribe.", parse_mode="Markdown")
        return
    await query.edit_message_text("⚙️ Fetching data + running AI analysis... 🧠")
    market = (ctx.user_data.get("market_cache", {}).get(condition_id) or
              ctx.user_data.get("trending_markets", {}).get(condition_id) or
              get_market_detail(condition_id))
    if not market:
        await query.edit_message_text("❌ Could not fetch market data.")
        return
    outcomes = parse_outcomes(market)
    if not outcomes:
        await query.edit_message_text("⚠️ No outcome data available.")
        return
    is_free = reason == "free"
    record_usage(user.id, is_free=is_free)
    u = check_and_reset_daily(get_user(user.id))
    card = format_market_card(market, outcomes)
    analysis = ai_analyze(market, outcomes)
    if is_free:
        free_left = max(0, FREE_ANALYSES - u["free_used"])
        footer = f"\n\n_🎁 Free analyses left: {free_left}. Use /subscribe to unlock more._"
    elif u["tier"] != "whale":
        tier = TIERS[u["tier"]]
        day_left = max(0, tier["daily"] - u["daily_used"])
        footer = f"\n\n_{tier_badge(u['tier'])} · {day_left} analyses left today_"
    else:
        footer = "\n\n_🐳 Whale plan · Unlimited_"
    full_msg = f"{card}\n\n{'─' * 30}\n\n{analysis}{footer}"
    if len(full_msg) > 4096:
        full_msg = full_msg[:4090] + "…"
    await query.edit_message_text(full_msg, parse_mode="Markdown")

async def handle_show_plans_callback(update, ctx):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("🥉 Basic — 500 ⭐/month",  callback_data="buy:basic")],
        [InlineKeyboardButton("🥈 Pro — 1000 ⭐/month",   callback_data="buy:pro")],
        [InlineKeyboardButton("🐳 Whale — 2500 ⭐/month", callback_data="buy:whale")],
    ]
    await query.edit_message_text("🔥 *PolyBot Plans*\n\n🥉 Basic — 500 ⭐/month (~$7)\n  └ 10/day\n\n🥈 Pro — 1000 ⭐/month (~$13)\n  └ 50/day\n\n🐳 Whale — 2500 ⭐/month (~$33)\n  └ Unlimited", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text(update, ctx):
    text = update.message.text.strip()
    if len(text) < 3:
        return
    ctx.args = text.split()
    await cmd_search(update, ctx)

async def main():
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print("Set TELEGRAM_TOKEN env variable.")
        return
    if GROQ_API_KEY == "YOUR_GROQ_API_KEY":
        print("Set GROQ_API_KEY env variable.")
        return
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("trending",  cmd_trending))
    app.add_handler(CommandHandler("search",    cmd_search))
    app.add_handler(CallbackQueryHandler(handle_buy_callback,        pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(handle_analyze_callback,    pattern=r"^analyze:"))
    app.add_handler(CallbackQueryHandler(handle_show_plans_callback, pattern=r"^show_plans$"))
    app.add_handler(PreCheckoutQueryHandler(handle_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("PolyBot running with Stars subscriptions...")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from learner import score_coin, learn_pump, learn_dump, record_signal, update_signal_result, get_stats, get_daily_report, is_duplicate
from github_sync import sync_to_github, restore_from_github

BOT_TOKEN = "8799728887:AAEk1R_H7ApAotjaM1B_XvhUScVAyyHhjtU"
CHAT_ID = "5461546008"

PUMP_MULTIPLIER = 3.0
SCAN_INTERVAL = 120
MIN_LIQUIDITY = 3000
MIN_VOLUME = 500
MIN_MCAP = 5000
MAX_MCAP = 1000000
MIN_AGE_SECONDS = 300
MAX_AGE_SECONDS = 1800
HISTORY_MAX_AGE = 86400
GITHUB_SYNC_INTERVAL = 21600

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pump_coins = {}
dump_coins = {}
tracked_coins = {}
alerted_coins = set()
signal_tracking = {}
bot_active = True
current_threshold = 0.35

def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 স্ট্যাটাস"), KeyboardButton("📈 পারফরম্যান্স")],
        [KeyboardButton("🏆 ট্রেন"), KeyboardButton("⚙️ সেটিংস")],
        [KeyboardButton("✅ অন"), KeyboardButton("❌ অফ")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def format_number(n):
    try:
        n = float(n)
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        elif n >= 1_000:
            return f"${n/1_000:.1f}K"
        else:
            return f"${n:.2f}"
    except:
        return "$0"

def get_pair_age(pair):
    try:
        created_at = pair.get("pairCreatedAt")
        if created_at:
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            return (now_ms - int(created_at)) / 1000
    except:
        pass
    return None

def check_rug_risk(pair):
    risks = []
    try:
        price_change_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        txns = pair.get("txns", {}).get("m5", {})
        sells = int(txns.get("sells", 0))
        buys = int(txns.get("buys", 0))
        if price_change_5m < -30:
            risks.append("⚠️ দাম ৩০%+ পড়েছে")
        if liquidity < 2000:
            risks.append("⚠️ লিকুইডিটি খুব কম")
        if sells > buys * 3 and sells > 10:
            risks.append("⚠️ Sell pressure বেশি")
    except:
        pass
    return risks

def passes_realtime_filter(pair):
    try:
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume = float(pair.get("volume", {}).get("h24", 0) or 0)
        mcap = float(pair.get("fdv", 0) or 0)
        age = get_pair_age(pair)
        if liquidity < MIN_LIQUIDITY:
            return False, "লিকুইডিটি কম"
        if volume < MIN_VOLUME:
            return False, "ভলিউম কম"
        if mcap < MIN_MCAP or mcap > MAX_MCAP:
            return False, "MCap রেঞ্জের বাইরে"
        if age is None:
            return False, "বয়স জানা যায়নি"
        if age < MIN_AGE_SECONDS:
            return False, f"খুব নতুন ({int(age)}s)"
        if age > MAX_AGE_SECONDS:
            return False, f"খুব পুরনো ({int(age/60)}m)"
        return True, "ঠিক আছে"
    except:
        return False, "এরর"

def passes_history_filter(pair):
    try:
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        mcap = float(pair.get("fdv", 0) or 0)
        age = get_pair_age(pair)
        if liquidity < 3000:
            return False
        if mcap < 5000:
            return False
        if age is None or age > HISTORY_MAX_AGE:
            return False
        return True
    except:
        return False

async def fetch_new_solana_pairs(session):
    try:
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [p for p in data if p.get("chainId") == "solana"]
    except Exception as e:
        logger.error(f"Fetch error: {e}")
    return []

async def fetch_pair_data(session, token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                    return pairs[0]
    except Exception as e:
        logger.error(f"Pair error: {e}")
    return None

async def fetch_boosted_pairs(session):
    try:
        url = "https://api.dexscreener.com/token-boosts/latest/v1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [p for p in data if p.get("chainId") == "solana"]
    except Exception as e:
        logger.error(f"Boosted error: {e}")
    return []

async def send_msg(bot, text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Send error: {e}")

async def history_scan_loop(bot, session):
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(60)
                continue
            logger.info("📚 হিস্ট্রি স্ক্যান শুরু...")
            boosted = await fetch_boosted_pairs(session)
            new_tokens = await fetch_new_solana_pairs(session)
            all_tokens = {}
            for t in boosted + new_tokens:
                addr = t.get("tokenAddress") or t.get("address")
                if addr:
                    all_tokens[addr] = t
            learned_pump = 0
            learned_dump = 0
            for addr in list(all_tokens.keys())[:40]:
                if is_duplicate(addr):
                    continue
                await asyncio.sleep(2)
                pair = await fetch_pair_data(session, addr)
                if not pair or not passes_history_filter(pair):
                    continue
                age = get_pair_age(pair)
                price_change_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
                price_change_6h = float(pair.get("priceChange", {}).get("h6", 0) or 0)
                price_change_24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                best_multi = max(
                    1 + price_change_1h / 100,
                    1 + price_change_6h / 100,
                    1 + price_change_24h / 100
                )
                coin_info = {
                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                }
                if best_multi >= PUMP_MULTIPLIER:
                    ok, msg = learn_pump(coin_info, pair, best_multi, addr, manual=False)
                    if ok:
                        learned_pump += 1
                        logger.info(f"📚 পাম্প শেখা: {coin_info['symbol']} {best_multi:.1f}x")
                elif age and age > 3600 and best_multi < 1.5:
                    ok, msg = learn_dump(coin_info, pair, addr, manual=False)
                    if ok:
                        learned_dump += 1
            if learned_pump > 0 or learned_dump > 0:
                sync_to_github(f"হিস্ট্রি: পাম্প {learned_pump} ডাম্প {learned_dump}")
                logger.info(f"হিস্ট্রি শেষ: পাম্প {learned_pump} ডাম্প {learned_dump}")
        except Exception as e:
            logger.error(f"হিস্ট্রি এরর: {e}")
        await asyncio.sleep(3600)

async def realtime_scan_loop(bot, session):
    sync_counter = 0
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(30)
                continue
            new_tokens = await fetch_new_solana_pairs(session)
            added = 0
            for t in new_tokens:
                if added >= 15:
                    break
                if len(tracked_coins) >= 200:
                    oldest = min(tracked_coins, key=lambda x: tracked_coins[x].get("first_seen", 0))
                    del tracked_coins[oldest]
                addr = t.get("tokenAddress") or t.get("address")
                if not addr or addr in tracked_coins:
                    continue
                await asyncio.sleep(1.5)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                ok, reason = passes_realtime_filter(pair)
                if not ok:
                    continue
                price = float(pair.get("priceUsd", 0) or 0)
                if price > 0:
                    tracked_coins[addr] = {
                        "initial_price": price,
                        "name": pair.get("baseToken", {}).get("name", "Unknown"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                        "pair_created_at": pair.get("pairCreatedAt"),
                        "first_seen": datetime.now(timezone.utc).timestamp()
                    }
                    added += 1

            for addr, coin_info in list(tracked_coins.items()):
                if addr in pump_coins or addr in dump_coins:
                    continue
                await asyncio.sleep(1.5)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                age = get_pair_age(pair)
                current_price = float(pair.get("priceUsd", 0) or 0)
                initial_price = coin_info.get("initial_price", 0)
                if initial_price <= 0 or current_price <= 0:
                    continue
                multiplier = current_price / initial_price
                mcap = float(pair.get("fdv", 0) or 0)
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                dex_url = pair.get("url", f"https://dexscreener.com/solana/{addr}")
                name = coin_info.get("name", "Unknown")
                symbol = coin_info.get("symbol", "???")

                if age and age > MAX_AGE_SECONDS:
                    if multiplier >= PUMP_MULTIPLIER:
                        pump_coins[addr] = {"symbol": symbol, "multiplier": multiplier}
                        learn_pump(coin_info, pair, multiplier, addr, manual=False)
                        await send_msg(bot,
                            f"🚀 <b>পাম্প কয়েন!</b>\n━━━━━━━━━━━━━━━━\n"
                            f"🏷️ <b>{name}</b> (${symbol})\n"
                            f"📈 পাম্প: <b>{multiplier:.2f}x</b>\n"
                            f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                            f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                            f"🧠 <i>বট শিখেছে!</i>\n"
                            f"🔗 <a href='{dex_url}'>DexScreener</a>"
                        )
                        sync_to_github(f"পাম্প: {symbol} {multiplier:.1f}x")
                    else:
                        dump_coins[addr] = coin_info
                        learn_dump(coin_info, pair, addr, manual=False)
                    continue

                if addr not in alerted_coins and 1.1 <= multiplier <= 2.5:
                    ai_score, reason = score_coin(pair, coin_info)
                    if ai_score >= current_threshold:
                        risks = check_rug_risk(pair)
                        confidence_pct = int(ai_score * 100)
                        confidence_bar = "🟢" * int(confidence_pct/20) + "⚪" * (5 - int(confidence_pct/20))
                        risk_text = "\n".join(risks) if risks else "✅ রিস্ক নেই"
                        await send_msg(bot,
                            f"⚡ <b>আর্লি সিগনাল!</b>\n━━━━━━━━━━━━━━━━\n"
                            f"🏷️ <b>{name}</b> (${symbol})\n"
                            f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
                            f"🧠 কারণ: <i>{reason}</i>\n"
                            f"📈 মাল্টি: <b>{multiplier:.2f}x</b>\n"
                            f"💵 দাম: <b>{current_price:.8f}</b>\n"
                            f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                            f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                            f"⏱️ বয়স: <b>{int((age or 0)/60)}m</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"🛡️ রিস্ক: {risk_text}\n"
                            f"⚠️ <i>DYOR করুন!</i>\n"
                            f"🔗 <a href='{dex_url}'>DexScreener</a>"
                        )
                        record_signal(addr, symbol, ai_score, current_price, mcap)
                        signal_tracking[addr] = {
                            "symbol": symbol,
                            "price_at_signal": current_price,
                            "signal_time": datetime.now(timezone.utc).timestamp(),
                            "checked": False
                        }
                        alerted_coins.add(addr)

            await check_signal_results(session, bot)
            sync_counter += 1
            if sync_counter >= GITHUB_SYNC_INTERVAL // SCAN_INTERVAL:
                sync_to_github()
                sync_counter = 0
            logger.info(f"ট্র্যাক: {len(tracked_coins)} | পাম্প: {len(pump_coins)} | ডাম্প: {len(dump_coins)}")
        except Exception as e:
            logger.error(f"রিয়েলটাইম এরর: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

async def check_signal_results(session, bot):
    for addr, sig_info in list(signal_tracking.items()):
        if sig_info.get("checked"):
            continue
        age = datetime.now(timezone.utc).timestamp() - sig_info["signal_time"]
        if age < 1800:
            continue
        pair = await fetch_pair_data(session, addr)
        if not pair:
            continue
        current_price = float(pair.get("priceUsd", 0) or 0)
        if current_price <= 0:
            continue
        update_signal_result(addr, current_price)
        multiplier = current_price / sig_info["price_at_signal"] if sig_info["price_at_signal"] > 0 else 0
        emoji = "✅" if multiplier >= 2.0 else "❌"
        await send_msg(bot,
            f"{emoji} <b>সিগনাল ফলাফল!</b>\n"
            f"🏷️ ${sig_info['symbol']}\n"
            f"📈 ফলাফল: <b>{multiplier:.2f}x</b>\n"
            f"⏱️ ৩০ মিনিট পরের রেজাল্ট"
        )
        signal_tracking[addr]["checked"] = True
        await asyncio.sleep(1)

async def scan_loop(bot):
    async with aiohttp.ClientSession() as session:
        restore_from_github()
        await asyncio.gather(
            history_scan_loop(bot, session),
            realtime_scan_loop(bot, session)
        )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Bappis Trade Bot চালু!</b>\n"
        "AI-powered Solana মেমে কয়েন ট্র্যাকার\n\n"
        "📚 কমান্ড:\n"
        "/pump ADDRESS — পাম্প শেখান\n"
        "/dump ADDRESS — ডাম্প শেখান\n"
        "/threshold 50 — থ্রেশোল্ড সেট করুন (১-১০০)",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_threshold
    if not context.args:
        await update.message.reply_text(
            f"⚙️ বর্তমান থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>\n"
            f"পরিবর্তন করতে: /threshold 50",
            parse_mode="HTML"
        )
        return
    try:
        val = int(context.args[0])
        if not 1 <= val <= 100:
            await update.message.reply_text("❌ ১ থেকে ১০০ এর মধ্যে দিন।")
            return
        current_threshold = val / 100
        await update.message.reply_text(
            f"✅ থ্রেশোল্ড আপডেট: <b>{val}%</b>\n"
            f"এখন থেকে {val}%+ কনফিডেন্সে সিগনাল দেবে।",
            parse_mode="HTML"
        )
        logger.info(f"থ্রেশোল্ড পরিবর্তন: {val}%")
    except:
        await update.message.reply_text("❌ সংখ্যা দিন। যেমন: /threshold 50")

async def cmd_pump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ ব্যবহার: /pump TOKEN_ADDRESS")
        return
    address = context.args[0].strip()
    if is_duplicate(address):
        await update.message.reply_text("⚠️ এই কয়েন ইতিমধ্যে শেখা আছে! ডুপ্লিকেট।")
        return
    await update.message.reply_text("⏳ ডেটা আনছি...")
    async with aiohttp.ClientSession() as session:
        pair = await fetch_pair_data(session, address)
    if not pair:
        await update.message.reply_text("❌ কয়েনের ডেটা পাওয়া যায়নি।")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    coin_info = {"name": name, "symbol": symbol}
    price_change_24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)
    multiplier = max(1 + (price_change_24h / 100), PUMP_MULTIPLIER)
    ok, msg = learn_pump(coin_info, pair, multiplier, address, manual=True)
    if ok:
        sync_to_github(f"ম্যানুয়াল পাম্প: {symbol}")
    await update.message.reply_text(
        f"{'✅' if ok else '❌'} <b>{name}</b> (${symbol})\n{msg}",
        parse_mode="HTML"
    )

async def cmd_dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ ব্যবহার: /dump TOKEN_ADDRESS")
        return
    address = context.args[0].strip()
    if is_duplicate(address):
        await update.message.reply_text("⚠️ এই কয়েন ইতিমধ্যে শেখা আছে! ডুপ্লিকেট।")
        return
    await update.message.reply_text("⏳ ডেটা আনছি...")
    async with aiohttp.ClientSession() as session:
        pair = await fetch_pair_data(session, address)
    if not pair:
        await update.message.reply_text("❌ কয়েনের ডেটা পাওয়া যায়নি।")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    coin_info = {"name": name, "symbol": symbol}
    ok, msg = learn_dump(coin_info, pair, address, manual=True)
    if ok:
        sync_to_github(f"ম্যানুয়াল ডাম্প: {symbol}")
    await update.message.reply_text(
        f"{'✅' if ok else '❌'} <b>{name}</b> (${symbol})\n{msg}",
        parse_mode="HTML"
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    text = update.message.text
    if text == "📊 স্ট্যাটাস":
        stats = get_stats()
        status_text = "🟢 চালু" if bot_active else "🔴 বন্ধ"
        await update.message.reply_text(
            f"📊 <b>বটের অবস্থা: {status_text}</b>\n"
            f"🔍 ট্র্যাক: <b>{len(tracked_coins)}</b>\n"
            f"🚀 পাম্প: <b>{len(pump_coins)}</b>\n"
            f"🔴 ডাম্প: <b>{len(dump_coins)}</b>\n"
            f"⚡ সিগনাল: <b>{len(alerted_coins)}</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b> (ম্যানুয়াল: {stats['manual_pumps']})\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b> (ম্যানুয়াল: {stats['manual_dumps']})\n"
            f"🎯 থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>\n"
            f"🔐 ট্রেইনড: <b>{stats['trained_addresses']}</b>",
            parse_mode="HTML"
        )
    elif text == "📈 পারফরম্যান্স":
        stats = get_stats()
        await update.message.reply_text(
            f"📈 <b>পারফরম্যান্স</b>\n"
            f"⚡ মোট সিগনাল: <b>{stats['total_signals']}</b>\n"
            f"✅ চেক হয়েছে: <b>{stats['checked_signals']}</b>\n"
            f"🏆 সফল: <b>{stats['successful_signals']}</b>\n"
            f"🎯 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"⏰ সেরা সময়: <b>{stats['best_hour']}:00 UTC</b>\n"
            f"💰 গড় পাম্প MCap: <b>{format_number(stats['avg_pump_mcap'])}</b>",
            parse_mode="HTML"
        )
    elif text == "🏆 ট্রেন":
        stats = get_stats()
        await update.message.reply_text(
            f"🏆 <b>লার্নিং স্ট্যাটাস</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b>\n"
            f"✍️ ম্যানুয়াল পাম্প: <b>{stats['manual_pumps']}</b>\n"
            f"✍️ ম্যানুয়াল ডাম্প: <b>{stats['manual_dumps']}</b>\n"
            f"🎯 থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>\n"
            f"📊 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"{'✅ মডেল রেডি!' if stats['pump_patterns'] >= 5 else '⏳ আরো ডেটা দরকার...'}\n\n"
            f"📚 কমান্ড:\n/pump ADDRESS\n/dump ADDRESS\n/threshold 50",
            parse_mode="HTML"
        )
    elif text == "⚙️ সেটিংস":
        await update.message.reply_text(
            f"⚙️ <b>সেটিংস</b>\n"
            f"📈 পাম্প থ্রেশোল্ড: {PUMP_MULTIPLIER}x\n"
            f"🎯 AI থ্রেশোল্ড: {int(current_threshold*100)}%\n"
            f"⏱️ রিয়েলটাইম স্ক্যান: প্রতি {SCAN_INTERVAL}s\n"
            f"📚 হিস্ট্রি স্ক্যান: প্রতি ১ ঘণ্টা\n"
            f"⏳ রিয়েলটাইম এইজ: {MIN_AGE_SECONDS//60}m - {MAX_AGE_SECONDS//60}m\n"
            f"📅 হিস্ট্রি এইজ: ২৪ ঘণ্টার মধ্যে\n"
            f"💧 মিন লিকুইডিটি: {format_number(MIN_LIQUIDITY)}\n"
            f"💰 MCap: {format_number(MIN_MCAP)} - {format_number(MAX_MCAP)}",
            parse_mode="HTML"
        )
    elif text == "✅ অন":
        bot_active = True
        await update.message.reply_text("✅ বট চালু হয়েছে! স্ক্যান শুরু হবে।")
        logger.info("বট চালু করা হয়েছে")
    elif text == "❌ অফ":
        bot_active = False
        await update.message.reply_text("❌ বট বন্ধ হয়েছে। স্ক্যান বন্ধ আছে।")
        logger.info("বট বন্ধ করা হয়েছে")

async def send_daily_report(bot):
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 18 and now.minute < 2:
            report = get_daily_report()
            best = report.get("best_signal")
            best_text = f"${best['symbol']} → {best.get('result_multiplier', 0)}x" if best else "N/A"
            await send_msg(bot,
                f"📋 <b>দৈনিক রিপোর্ট</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📅 তারিখ: <b>{report['date']}</b>\n"
                f"⚡ সিগনাল: <b>{report['signals_sent']}</b>\n"
                f"🚀 পাম্প শেখা: <b>{report['pumps_learned']}</b>\n"
                f"✅ সফল: <b>{report['successful']}/{report['checked']}</b>\n"
                f"🏆 সেরা: <b>{best_text}</b>"
            )
            sync_to_github("দৈনিক রিপোর্ট")
        await asyncio.sleep(60)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pump", cmd_pump))
    app.add_handler(CommandHandler("dump", cmd_dump))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    loop = asyncio.get_event_loop()
    loop.create_task(scan_loop(app.bot))
    loop.create_task(send_daily_report(app.bot))
    app.run_polling()

if __name__ == "__main__":
    main()

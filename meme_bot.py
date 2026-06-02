import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from learner import score_coin, learn_pump, learn_dump, record_signal, update_signal_result, get_stats, get_daily_report
from github_sync import sync_to_github, restore_from_github

BOT_TOKEN = "8799728887:AAEk1R_H7ApAotjaM1B_XvhUScVAyyHhjtU"
CHAT_ID = "5461546008"

PUMP_MULTIPLIER = 3.0
SCAN_INTERVAL = 120
MIN_LIQUIDITY = 5000
MIN_VOLUME = 1000
MIN_MCAP = 10000
MAX_MCAP = 500000
MIN_AGE_SECONDS = 300
MAX_AGE_SECONDS = 900
GITHUB_SYNC_INTERVAL = 21600

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pump_coins = {}
dump_coins = {}
tracked_coins = {}
alerted_coins = set()
signal_tracking = {}

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
            risks.append("⚠️ দাম হঠাৎ ৩০%+ পড়েছে")
        if liquidity < 3000:
            risks.append("⚠️ লিকুইডিটি খুব কম")
        if sells > buys * 3 and sells > 10:
            risks.append("⚠️ Sell pressure বেশি")
    except:
        pass
    return risks

def passes_basic_filter(pair):
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

async def send_msg(bot, text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Send error: {e}")

async def send_signal(bot, coin, score, reason, risks):
    confidence_pct = int(score * 100)
    confidence_bar = "🟢" * int(confidence_pct / 20) + "⚪" * (5 - int(confidence_pct / 20))
    risk_text = "\n".join(risks) if risks else "✅ কোনো রিস্ক নেই"
    msg = (
        f"⚡ <b>আর্লি সিগনাল!</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>{coin['name']}</b> (${coin['symbol']})\n"
        f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
        f"🧠 কারণ: <i>{reason}</i>\n"
        f"📈 মাল্টি: <b>{coin['multiplier']:.2f}x</b>\n"
        f"💵 দাম: <b>{coin['current_price']:.8f}</b>\n"
        f"💰 MCap: <b>{format_number(coin['mcap'])}</b>\n"
        f"💧 লিকুইডিটি: <b>{format_number(coin['liquidity'])}</b>\n"
        f"⏱️ বয়স: <b>{int(coin.get('age', 0) / 60)} মিনিট</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🛡️ রিস্ক: {risk_text}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>DYOR করুন!</i>\n"
        f"🔗 <a href='{coin['dex_url']}'>DexScreener</a>"
    )
    await send_msg(bot, msg)

async def send_pump_alert(bot, coin):
    msg = (
        f"🚀 <b>পাম্প কয়েন লোড!</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>{coin['name']}</b> (${coin['symbol']})\n"
        f"⭐ স্কোর: <b>100/100</b>\n"
        f"📈 পাম্প: <b>{coin['multiplier']:.2f}x</b>\n"
        f"💵 দাম: <b>{coin['current_price']:.8f}</b>\n"
        f"💰 MCap: <b>{format_number(coin['mcap'])}</b>\n"
        f"💧 লিকুইডিটি: <b>{format_number(coin['liquidity'])}</b>\n"
        f"⏱️ বয়স: <b>{int(coin.get('age', 0) / 60)} মিনিট</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🧠 <i>বট এই প্যাটার্ন থেকে শিখছে...</i>\n"
        f"🔗 <a href='{coin['dex_url']}'>DexScreener</a>"
    )
    await send_msg(bot, msg)

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
            f"⏱️ সিগনালের ৩০ মিনিট পর"
        )
        signal_tracking[addr]["checked"] = True
        await asyncio.sleep(1)

async def scan_loop(bot):
    sync_counter = 0
    async with aiohttp.ClientSession() as session:
        restore_from_github()
        while True:
            try:
                new_tokens = await fetch_new_solana_pairs(session)
                added = 0
                for t in new_tokens:
                    if added >= 10:
                        break
                    if len(tracked_coins) >= 150:
                        oldest = min(tracked_coins, key=lambda x: tracked_coins[x].get("first_seen", 0))
                        del tracked_coins[oldest]
                    addr = t.get("tokenAddress") or t.get("address")
                    if not addr or addr in tracked_coins:
                        continue
                    await asyncio.sleep(2)
                    pair = await fetch_pair_data(session, addr)
                    if not pair:
                        continue
                    ok, reason = passes_basic_filter(pair)
                    if not ok:
                        continue
                    price = float(pair.get("priceUsd", 0) or 0)
                    if price > 0:
                        age = get_pair_age(pair)
                        tracked_coins[addr] = {
                            "initial_price": price,
                            "name": pair.get("baseToken", {}).get("name", "Unknown"),
                            "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                            "pair_created_at": pair.get("pairCreatedAt"),
                            "first_seen": datetime.now(timezone.utc).timestamp()
                        }
                        added += 1
                        logger.info(f"✅ ট্র্যাক: {tracked_coins[addr]['symbol']} | বয়স: {int((age or 0)/60)}m")

                for addr, coin_info in list(tracked_coins.items()):
                    if addr in pump_coins or addr in dump_coins:
                        continue
                    await asyncio.sleep(2)
                    pair = await fetch_pair_data(session, addr)
                    if not pair:
                        continue
                    age = get_pair_age(pair)
                    if age and age > MAX_AGE_SECONDS:
                        learn_dump(coin_info, pair)
                        dump_coins[addr] = coin_info
                        logger.info(f"⏰ ডাম্প শেখা: {coin_info.get('symbol')}")
                        continue
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
                    coin_data = {
                        "address": addr, "name": name, "symbol": symbol,
                        "multiplier": multiplier, "current_price": current_price,
                        "mcap": mcap, "liquidity": liquidity,
                        "dex_url": dex_url, "age": age or 0
                    }
                    if multiplier >= PUMP_MULTIPLIER:
                        pump_coins[addr] = coin_data
                        learn_pump(coin_info, pair, multiplier)
                        await send_pump_alert(bot, coin_data)
                        sync_to_github(f"পাম্প শেখা: {symbol} {multiplier:.1f}x")
                        logger.info(f"🚀 পাম্প: {symbol} {multiplier:.2f}x")
                    elif addr not in alerted_coins and 1.1 <= multiplier <= 2.5:
                        ai_score, reason = score_coin(pair, coin_info)
                        risks = check_rug_risk(pair)
                        stats = get_stats()
                        threshold = stats.get("threshold", 0.4)
                        if ai_score >= threshold:
                            await send_signal(bot, coin_data, ai_score, reason, risks)
                            record_signal(addr, symbol, ai_score, current_price, mcap)
                            signal_tracking[addr] = {
                                "symbol": symbol,
                                "price_at_signal": current_price,
                                "signal_time": datetime.now(timezone.utc).timestamp(),
                                "checked": False
                            }
                            alerted_coins.add(addr)
                            logger.info(f"⚡ সিগনাল: {symbol} স্কোর: {ai_score}")

                await check_signal_results(session, bot)
                sync_counter += 1
                if sync_counter >= GITHUB_SYNC_INTERVAL // SCAN_INTERVAL:
                    sync_to_github()
                    sync_counter = 0
                logger.info(f"ট্র্যাক: {len(tracked_coins)} | পাম্প: {len(pump_coins)} | ডাম্প: {len(dump_coins)}")
            except Exception as e:
                logger.error(f"স্ক্যান এরর: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

async def send_daily_report(bot):
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 18 and now.minute < 2:
            report = get_daily_report()
            best = report.get("best_signal")
            best_text = f"${best['symbol']} → {best.get('result_multiplier', 0)}x" if best else "N/A"
            msg = (
                f"📋 <b>দৈনিক রিপোর্ট</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📅 তারিখ: <b>{report['date']}</b>\n"
                f"⚡ সিগনাল: <b>{report['signals_sent']}</b>\n"
                f"🚀 পাম্প শেখা: <b>{report['pumps_learned']}</b>\n"
                f"✅ সফল সিগনাল: <b>{report['successful']}/{report['checked']}</b>\n"
                f"🏆 সেরা সিগনাল: <b>{best_text}</b>\n"
                f"━━━━━━━━━━━━━━━━"
            )
            await send_msg(bot, msg)
            sync_to_github("দৈনিক রিপোর্ট")
        await asyncio.sleep(60)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Bappis Trade Bot চালু!</b>\nAI-powered Solana মেমে কয়েন ট্র্যাকার",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 স্ট্যাটাস":
        stats = get_stats()
        await update.message.reply_text(
            f"📊 <b>বটের অবস্থা</b>\n"
            f"🔍 ট্র্যাক: <b>{len(tracked_coins)}</b>\n"
            f"🚀 পাম্প: <b>{len(pump_coins)}</b>\n"
            f"🔴 ডাম্প: <b>{len(dump_coins)}</b>\n"
            f"⚡ সিগনাল: <b>{len(alerted_coins)}</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b>\n"
            f"🎯 AI থ্রেশোল্ড: <b>{int(stats['threshold']*100)}%</b>\n"
            f"⏱️ এইজ উইন্ডো: <b>৫ - ১৫ মিনিট</b>",
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
            f"🎯 AI থ্রেশোল্ড: <b>{int(stats['threshold']*100)}%</b>\n"
            f"📊 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"{'✅ মডেল রেডি!' if stats['pump_patterns'] >= 3 else '⏳ আরো ডেটা দরকার...'}",
            parse_mode="HTML"
        )
    elif text == "⚙️ সেটিংস":
        await update.message.reply_text(
            f"⚙️ <b>সেটিংস</b>\n"
            f"📈 পাম্প থ্রেশোল্ড: {PUMP_MULTIPLIER}x\n"
            f"⏱️ স্ক্যান: {SCAN_INTERVAL}s\n"
            f"⏳ এইজ: {MIN_AGE_SECONDS//60}m - {MAX_AGE_SECONDS//60}m\n"
            f"💧 মিন লিকুইডিটি: {format_number(MIN_LIQUIDITY)}\n"
            f"💰 MCap: {format_number(MIN_MCAP)} - {format_number(MAX_MCAP)}",
            parse_mode="HTML"
        )
    elif text == "✅ অন":
        await update.message.reply_text("✅ বট চালু আছে, স্ক্যান হচ্ছে!")
    elif text == "❌ অফ":
        await update.message.reply_text("❌ বন্ধ করতে Termux-এ Ctrl+C চাপুন।")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    loop = asyncio.get_event_loop()
    loop.create_task(scan_loop(app.bot))
    loop.create_task(send_daily_report(app.bot))
    app.run_polling()

if __name__ == "__main__":
    main()

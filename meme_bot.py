import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = "8799728887:AAEk1R_H7ApAotjaM1B_XvhUScVAyyHhjtU"
CHAT_ID = "5461546008"

PUMP_MULTIPLIER = 3.0
SCAN_INTERVAL = 120
MIN_LIQUIDITY = 5000
MIN_VOLUME = 1000
MIN_MCAP = 10000
MAX_MCAP = 500000
MAX_TRACK = 100
MIN_AGE_SECONDS = 300
MAX_AGE_SECONDS = 900

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pump_coins = {}
dump_coins = {}
tracked_coins = {}
alerted_coins = set()

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

def get_pair_age_seconds(pair):
    try:
        created_at = pair.get("pairCreatedAt")
        if created_at:
            created_ms = int(created_at)
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            return (now_ms - created_ms) / 1000
    except:
        pass
    return None

def passes_filter(pair):
    try:
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume = float(pair.get("volume", {}).get("h24", 0) or 0)
        mcap = float(pair.get("fdv", 0) or 0)
        age = get_pair_age_seconds(pair)
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
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)

async def scan_loop(bot):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                new_tokens = await fetch_new_solana_pairs(session)
                added = 0
                for t in new_tokens:
                    if added >= 10:
                        break
                    if len(tracked_coins) >= MAX_TRACK:
                        break
                    addr = t.get("tokenAddress") or t.get("address")
                    if not addr or addr in tracked_coins:
                        continue
                    await asyncio.sleep(2)
                    pair = await fetch_pair_data(session, addr)
                    if not pair:
                        continue
                    ok, reason = passes_filter(pair)
                    if not ok:
                        logger.info(f"বাদ: {addr[:8]}... কারণ: {reason}")
                        continue
                    price = float(pair.get("priceUsd", 0) or 0)
                    if price > 0:
                        age = get_pair_age_seconds(pair)
                        tracked_coins[addr] = {
                            "initial_price": price,
                            "name": pair.get("baseToken", {}).get("name", "Unknown"),
                            "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                            "pair_created_at": pair.get("pairCreatedAt"),
                            "first_seen": datetime.now(timezone.utc).timestamp()
                        }
                        added += 1
                        logger.info(f"✅ ট্র্যাক: {tracked_coins[addr]['symbol']} | বয়স: {int(age/60)}m | MCap: {format_number(pair.get('fdv',0))}")

                for addr, coin_info in list(tracked_coins.items()):
                    if addr in pump_coins or addr in dump_coins:
                        continue
                    await asyncio.sleep(2)
                    pair = await fetch_pair_data(session, addr)
                    if not pair:
                        continue

                    age = get_pair_age_seconds(pair)
                    if age and age > MAX_AGE_SECONDS:
                        dump_coins[addr] = {"symbol": coin_info.get("symbol"), "reason": "টাইম এক্সপায়ার"}
                        logger.info(f"⏰ এক্সপায়ার: {coin_info.get('symbol')}")
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
                    result = {
                        "address": addr, "name": name, "symbol": symbol,
                        "multiplier": multiplier, "current_price": current_price,
                        "mcap": mcap, "liquidity": liquidity,
                        "dex_url": dex_url, "is_pump": multiplier >= PUMP_MULTIPLIER
                    }
                    if multiplier >= PUMP_MULTIPLIER:
                        pump_coins[addr] = result
                        msg = (
                            f"🚀 <b>পাম্প কয়েন লোড!</b>\n━━━━━━━━━━━━━━━━\n"
                            f"🏷️ <b>{name}</b> (${symbol})\n"
                            f"⭐ স্কোর: <b>100/100</b>\n"
                            f"📈 পাম্প: <b>{multiplier:.2f}x</b>\n"
                            f"💵 দাম: <b>{current_price:.8f}</b>\n"
                            f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                            f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                            f"⏱️ বয়স: <b>{int((age or 0)/60)} মিনিট</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"🔗 <a href='{dex_url}'>DexScreener</a>"
                        )
                        await send_msg(bot, msg)
                    if addr not in alerted_coins and 1.2 <= multiplier <= 2.0:
                        matched = [p for p in pump_coins.values() if p.get("mcap", 0) > 0 and mcap > 0 and 0.1 <= mcap / p["mcap"] <= 5.0]
                        if matched:
                            names = ", ".join([f"${p['symbol']}" for p in matched[:3]])
                            msg = (
                                f"⚡ <b>আর্লি সিগনাল!</b>\n━━━━━━━━━━━━━━━━\n"
                                f"🏷️ <b>{name}</b> (${symbol})\n"
                                f"🎯 মিল: <b>{names}</b>\n"
                                f"📈 মাল্টি: <b>{multiplier:.2f}x</b>\n"
                                f"💵 দাম: <b>{current_price:.8f}</b>\n"
                                f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                                f"⏱️ বয়স: <b>{int((age or 0)/60)} মিনিট</b>\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"⚠️ <i>DYOR করুন!</i>\n"
                                f"🔗 <a href='{dex_url}'>DexScreener</a>"
                            )
                            await send_msg(bot, msg)
                            alerted_coins.add(addr)

                logger.info(f"ট্র্যাক: {len(tracked_coins)} | পাম্প: {len(pump_coins)} | ডাম্প: {len(dump_coins)}")
            except Exception as e:
                logger.error(f"স্ক্যান এরর: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Bappis Trade Bot চালু!</b>\nSolana কয়েন ট্র্যাক হচ্ছে...",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 স্ট্যাটাস":
        await update.message.reply_text(
            f"📊 <b>বটের অবস্থা</b>\n"
            f"🔍 ট্র্যাক: <b>{len(tracked_coins)}</b>\n"
            f"🚀 পাম্প: <b>{len(pump_coins)}</b>\n"
            f"🔴 ডাম্প: <b>{len(dump_coins)}</b>\n"
            f"⚡ সিগনাল: <b>{len(alerted_coins)}</b>\n"
            f"⏱️ এইজ উইন্ডো: <b>৫ - ১৫ মিনিট</b>",
            parse_mode="HTML"
        )
    elif text == "📈 পারফরম্যান্স":
        total = len(pump_coins) + len(dump_coins)
        acc = (len(pump_coins) / total * 100) if total > 0 else 0
        await update.message.reply_text(
            f"📈 <b>পারফরম্যান্স</b>\n🏆 পাম্প: {len(pump_coins)}\n🔴 ডাম্প: {len(dump_coins)}\n🎯 একুরেসি: {acc:.1f}%",
            parse_mode="HTML"
        )
    elif text == "🏆 ট্রেন":
        await update.message.reply_text(f"🏆 পাম্প ডেটা: {len(pump_coins)}টি কয়েন।")
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
        await update.message.reply_text("✅ বট চালু আছে!")
    elif text == "❌ অফ":
        await update.message.reply_text("❌ বন্ধ করতে Termux-এ Ctrl+C চাপুন।")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    loop = asyncio.get_event_loop()
    loop.create_task(scan_loop(app.bot))
    app.run_polling()

if __name__ == "__main__":
    main()

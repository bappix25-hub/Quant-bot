import asyncio
import aiohttp
import logging
import json
import os
import websockets
from datetime import datetime, timezone
from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from learner import score_coin, learn_pump, learn_dump, record_signal, update_signal_result, get_stats, get_daily_report, is_duplicate, get_signal_age_window, verify_pump, get_launch_age, score_launch, extract_launch_pattern, learn_pump_with_launch
from github_sync import sync_to_github, restore_from_github

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
RUGCHECK_URL = os.getenv("RUGCHECK_URL", "https://api.rugcheck.xyz/v1")
PUMPPORTAL_WS = os.getenv("PUMPPORTAL_WS", "wss://pumpportal.fun/api/data")

PUMP_MULTIPLIER = 3.0
SCAN_INTERVAL = 120
MIN_LIQUIDITY = 2000
MIN_VOLUME = 300
MIN_MCAP = 1000
MAX_MCAP = 2000000
HISTORY_MAX_AGE = 86400
GITHUB_SYNC_INTERVAL = 21600

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

pump_coins = {}
dump_coins = {}
tracked_coins = {}
alerted_coins = set()
signal_tracking = {}
blacklisted = set()
launch_tracking = {}
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

def gmgn_link(address):
    return f"https://gmgn.ai/sol/token/{address}"

async def check_rugcheck(session, address, symbol="?"):
    """শুধু Freeze/Mint Authority চেক"""
    try:
        url = f"{RUGCHECK_URL}/tokens/{address}/report/summary"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                lp_locked = data.get("lpLockedPct", 0)
                risk_names = [r.get("name", "") for r in risks if isinstance(r, dict)]
                logger.info(f"RUGCHECK {symbol} | score={score} | risks={risk_names[:3]}")
                is_risky = any(r in [
                    "Freeze Authority still enabled",
                    "Mint Authority still enabled"
                ] for r in risk_names)
                return {
                    "score": score,
                    "risks": risk_names,
                    "lp_locked": lp_locked,
                    "is_risky": is_risky
                }
    except Exception as e:
        logger.error(f"Rugcheck error: {e}")
    return None

async def get_launch_transactions(session, address):
    """Helius দিয়ে লঞ্চের প্রথম transactions আনা"""
    try:
        url = f"https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={HELIUS_API_KEY}&limit=20&type=SWAP"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.error(f"Helius tx error: {e}")
    return []

async def get_holder_count(session, address):
    """Helius দিয়ে হোল্ডার সংখ্যা"""
    try:
        url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts", "params": {"mint": address, "limit": 1}}
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("result", {}).get("total", 0)
    except Exception as e:
        logger.error(f"Helius holder error: {e}")
    return None

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

async def process_new_token(bot, session, address, coin_info):
    """নতুন কয়েন প্রসেস"""
    if address in blacklisted or address in tracked_coins:
        return
    symbol = coin_info.get("symbol", "???")
    await asyncio.sleep(3)
    rug = await check_rugcheck(session, address, symbol)
    if rug and rug["is_risky"]:
        blacklisted.add(address)
        logger.info(f"🚫 ব্ল্যাকলিস্ট: {symbol} | {rug['risks'][:2]}")
        return
    holders = await get_holder_count(session, address)
    if holders is not None and holders < 3:
        logger.info(f"⚠️ হোল্ডার কম: {symbol} ({holders})")
        return
    price = float(coin_info.get("initialBuy", 0) or 0)
    launch_tracking[address] = {
        "name": coin_info.get("name", "Unknown"),
        "symbol": symbol,
        "first_seen": datetime.now(timezone.utc).timestamp(),
        "buy_count": 1,
        "sell_count": 0,
        "unique_wallets": set(),
        "volume": price,
        "holders": holders,
        "lp_locked": rug["lp_locked"] if rug else 0
    }
    logger.info(f"🆕 লঞ্চ ট্র্যাক: {symbol}")

async def pumpportal_loop(bot, session):
    """PumpPortal WebSocket"""
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(30)
                continue
            logger.info("🔌 PumpPortal সংযুক্ত হচ্ছে...")
            async with websockets.connect(PUMPPORTAL_WS) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("✅ PumpPortal সংযুক্ত!")
                async for message in ws:
                    if not bot_active:
                        break
                    try:
                        data = json.loads(message)
                        if "mint" in data and "name" in data:
                            address = data.get("mint")
                            coin_info = {
                                "name": data.get("name", "Unknown"),
                                "symbol": data.get("symbol", "???"),
                                "initialBuy": data.get("initialBuy", 0)
                            }
                            asyncio.create_task(process_new_token(bot, session, address, coin_info))
                        elif data.get("txType") == "migrate":
                            address = data.get("mint")
                            symbol = data.get("symbol", "???")
                            if address and address in launch_tracking:
                                info = launch_tracking[address]
                                logger.info(f"🚀 Migration: {symbol}")
                                asyncio.create_task(handle_migration(bot, session, address, info))
                        elif "mint" in data and "txType" in data:
                            address = data.get("mint")
                            if address in launch_tracking:
                                tx_type = data.get("txType", "")
                                if tx_type == "buy":
                                    launch_tracking[address]["buy_count"] = launch_tracking[address].get("buy_count", 0) + 1
                                    wallet = data.get("traderPublicKey", "")
                                    if wallet:
                                        launch_tracking[address]["unique_wallets"].add(wallet)
                                elif tx_type == "sell":
                                    launch_tracking[address]["sell_count"] = launch_tracking[address].get("sell_count", 0) + 1
                                asyncio.create_task(check_pre_migration_signal(bot, address))
                    except Exception as e:
                        logger.error(f"WS msg error: {e}")
        except Exception as e:
            logger.error(f"PumpPortal error: {e}")
        await asyncio.sleep(10)

async def check_pre_migration_signal(bot, address):
    """মাইগ্রেশনের আগে সিগনাল চেক"""
    if address in alerted_coins or address not in launch_tracking:
        return
    info = launch_tracking[address]
    age = datetime.now(timezone.utc).timestamp() - info.get("first_seen", 0)
    if age < 30:
        return
    buy_count = info.get("buy_count", 0)
    sell_count = info.get("sell_count", 0)
    unique_wallets = len(info.get("unique_wallets", set()))
    volume = info.get("volume", 0)
    buy_sell_ratio = buy_count / max(sell_count, 1)
    launch_data = {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "unique_wallets": unique_wallets,
        "volume": volume,
        "buy_sell_ratio": buy_sell_ratio
    }
    ai_score, reason = score_launch(launch_data)
    if ai_score >= current_threshold:
        symbol = info.get("symbol", "???")
        name = info.get("name", "Unknown")
        confidence_pct = int(ai_score * 100)
        confidence_bar = "🟢" * int(confidence_pct/20) + "⚪" * (5 - int(confidence_pct/20))
        link = gmgn_link(address)
        await send_msg(bot,
            f"⚡ <b>প্রি-মাইগ্রেশন সিগনাল!</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>{name}</b> (${symbol})\n"
            f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
            f"🧠 <i>{reason}</i>\n"
            f"📊 Buy: <b>{buy_count}</b> | Sell: <b>{sell_count}</b>\n"
            f"👥 Unique wallets: <b>{unique_wallets}</b>\n"
            f"⏱️ বয়স: <b>{int(age)}s</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>মাইগ্রেশনের আগে! DYOR করুন!</i>\n"
            f"🔗 <a href='{link}'>GMGN</a>"
        )
        alerted_coins.add(address)
        logger.info(f"⚡ প্রি-মাইগ্রেশন সিগনাল: {symbol} স্কোর: {ai_score}")

async def handle_migration(bot, session, address, info):
    """মাইগ্রেশন হলে DexScreener থেকে ডেটা নিয়ে ট্র্যাক করা"""
    await asyncio.sleep(10)
    pair = await fetch_pair_data(session, address)
    if not pair:
        return
    price = float(pair.get("priceUsd", 0) or 0)
    if price > 0 and address not in tracked_coins:
        tracked_coins[address] = {
            "initial_price": price,
            "name": info.get("name", "Unknown"),
            "symbol": info.get("symbol", "???"),
            "first_seen": datetime.now(timezone.utc).timestamp(),
            "holders": info.get("holders", 0),
            "lp_locked": info.get("lp_locked", 0)
        }
        logger.info(f"✅ মাইগ্রেশন ট্র্যাক: {info.get('symbol')}")

async def realtime_scan_loop(bot, session):
    """DexScreener রিয়েলটাইম স্ক্যান"""
    sync_counter = 0
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(30)
                continue
            new_tokens = await fetch_new_solana_pairs(session)
            for t in new_tokens[:15]:
                addr = t.get("tokenAddress") or t.get("address")
                if not addr or addr in tracked_coins or addr in blacklisted:
                    continue
                await asyncio.sleep(1)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                mcap = float(pair.get("fdv", 0) or 0)
                if liquidity < MIN_LIQUIDITY or mcap < MIN_MCAP or mcap > MAX_MCAP:
                    continue
                price = float(pair.get("priceUsd", 0) or 0)
                if price > 0:
                    tracked_coins[addr] = {
                        "initial_price": price,
                        "name": pair.get("baseToken", {}).get("name", "Unknown"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                        "first_seen": datetime.now(timezone.utc).timestamp()
                    }

            for addr, coin_info in list(tracked_coins.items()):
                if addr in pump_coins or addr in dump_coins or addr in blacklisted:
                    continue
                await asyncio.sleep(1)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                age = get_launch_age(pair)
                if age is None:
                    continue
                current_price = float(pair.get("priceUsd", 0) or 0)
                initial_price = coin_info.get("initial_price", 0)
                if initial_price <= 0 or current_price <= 0:
                    continue
                multiplier = current_price / initial_price
                mcap = float(pair.get("fdv", 0) or 0)
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                name = coin_info.get("name", "Unknown")
                symbol = coin_info.get("symbol", "???")
                link = gmgn_link(addr)

                if liquidity < 300:
                    blacklisted.add(addr)
                    logger.info(f"🚫 লিকুইডিটি pull: {symbol}")
                    continue

                if age > HISTORY_MAX_AGE:
                    verified, actual_multi = verify_pump(pair)
                    if verified:
                        pump_coins[addr] = coin_info
                        txs = await get_launch_transactions(session, addr)
                        launch_pat = extract_launch_pattern(txs) if txs else None
                        learn_pump_with_launch(coin_info, pair, actual_multi, launch_pat, addr, manual=False)
                        holders = coin_info.get("holders", "?")
                        lp = coin_info.get("lp_locked", 0)
                        await send_msg(bot,
                            f"🚀 <b>পাম্প কয়েন!</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"🏷️ <b>{name}</b> (${symbol})\n"
                            f"📈 পাম্প: <b>{actual_multi}x</b>\n"
                            f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                            f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                            f"👥 হোল্ডার: <b>{holders}</b>\n"
                            f"🔒 LP লক: <b>{lp}%</b>\n"
                            f"🧠 <i>লঞ্চ প্যাটার্ন শেখা হয়েছে!</i>\n"
                            f"🔗 <a href='{link}'>GMGN</a>"
                        )
                        sync_to_github(f"পাম্প: {symbol} {actual_multi}x")
                    else:
                        dump_coins[addr] = coin_info
                        learn_dump(coin_info, pair, addr, manual=False)
                    continue

                if addr not in alerted_coins:
                    ai_score, reason = score_coin(pair, coin_info, age)
                    if ai_score >= current_threshold:
                        holders = coin_info.get("holders", "?")
                        lp = coin_info.get("lp_locked", 0)
                        confidence_pct = int(ai_score * 100)
                        confidence_bar = "🟢" * int(confidence_pct/20) + "⚪" * (5 - int(confidence_pct/20))
                        await send_msg(bot,
                            f"⚡ <b>আর্লি সিগনাল!</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"🏷️ <b>{name}</b> (${symbol})\n"
                            f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
                            f"🧠 <i>{reason}</i>\n"
                            f"💵 দাম: <b>{current_price:.8f}</b>\n"
                            f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                            f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                            f"👥 হোল্ডার: <b>{holders}</b>\n"
                            f"🔒 LP লক: <b>{lp}%</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"🔗 <a href='{link}'>GMGN</a>"
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
            logger.info(f"ট্র্যাক: {len(tracked_coins)} | লঞ্চ: {len(launch_tracking)} | পাম্প: {len(pump_coins)} | সিগনাল: {len(alerted_coins)} | ব্ল্যাকলিস্ট: {len(blacklisted)}")
        except Exception as e:
            logger.error(f"রিয়েলটাইম এরর: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

async def history_scan_loop(bot, session):
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(60)
                continue
            logger.info("📚 হিস্ট্রি স্ক্যান...")
            boosted = await fetch_boosted_pairs(session)
            new_tokens = await fetch_new_solana_pairs(session)
            all_addrs = {}
            for t in boosted + new_tokens:
                addr = t.get("tokenAddress") or t.get("address")
                if addr:
                    all_addrs[addr] = t
            learned_pump = 0
            learned_dump = 0
            for addr in list(all_addrs.keys())[:40]:
                if is_duplicate(addr):
                    continue
                await asyncio.sleep(2)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                age = get_launch_age(pair)
                if liquidity < 1000 or age is None or age > HISTORY_MAX_AGE:
                    continue
                verified, actual_multi = verify_pump(pair)
                coin_info = {
                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                }
                if verified:
                    txs = await get_launch_transactions(session, addr)
                    launch_pat = extract_launch_pattern(txs) if txs else None
                    ok, msg = learn_pump_with_launch(coin_info, pair, actual_multi, launch_pat, addr, manual=False)
                    if ok:
                        learned_pump += 1
                        link = gmgn_link(addr)
                        launch_info = f" | লঞ্চ ডেটা: ✅" if launch_pat else ""
                        await send_msg(bot,
                            f"📚 <b>পাম্প শেখা!</b>\n"
                            f"🏷️ <b>{coin_info['name']}</b> (${coin_info['symbol']})\n"
                            f"📈 <b>{actual_multi}x</b> | ⏱️ {int((age or 0)/60)}m{launch_info}\n"
                            f"💰 {format_number(pair.get('fdv', 0))}\n"
                            f"🔗 <a href='{link}'>GMGN</a>"
                        )
                elif age and age > 3600:
                    h24 = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                    if h24 < 100:
                        ok, msg = learn_dump(coin_info, pair, addr, manual=False)
                        if ok:
                            learned_dump += 1
            if learned_pump > 0 or learned_dump > 0:
                sync_to_github(f"হিস্ট্রি: পাম্প {learned_pump} ডাম্প {learned_dump}")
        except Exception as e:
            logger.error(f"হিস্ট্রি এরর: {e}")
        await asyncio.sleep(3600)

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
            f"📈 ফলাফল: <b>{multiplier:.2f}x</b>"
        )
        signal_tracking[addr]["checked"] = True
        await asyncio.sleep(1)

async def scan_loop(bot):
    async with aiohttp.ClientSession() as session:
        restore_from_github()
        await asyncio.gather(
            pumpportal_loop(bot, session),
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
        "/threshold 50 — থ্রেশোল্ড সেট (১-১০০)",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_threshold
    if not context.args:
        await update.message.reply_text(f"⚙️ বর্তমান থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>", parse_mode="HTML")
        return
    try:
        val = int(context.args[0])
        if not 1 <= val <= 100:
            await update.message.reply_text("❌ ১-১০০ এর মধ্যে দিন।")
            return
        current_threshold = val / 100
        await update.message.reply_text(f"✅ থ্রেশোল্ড: <b>{val}%</b>", parse_mode="HTML")
    except:
        await update.message.reply_text("❌ যেমন: /threshold 50")

async def cmd_pump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /pump TOKEN_ADDRESS")
        return
    address = context.args[0].strip()
    if is_duplicate(address):
        await update.message.reply_text("⚠️ ডুপ্লিকেট!")
        return
    await update.message.reply_text("⏳ ডেটা আনছি...")
    async with aiohttp.ClientSession() as session:
        pair = await fetch_pair_data(session, address)
    if not pair:
        await update.message.reply_text("❌ ডেটা পাওয়া যায়নি।")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    coin_info = {"name": name, "symbol": symbol}
    verified, actual_multi = verify_pump(pair)
    if not verified:
        await update.message.reply_text(
            f"⚠️ ৩x ভেরিফাই হয়নি ({actual_multi}x)\n/forcepump {address}",
            parse_mode="HTML"
        )
        return
    ok, msg = learn_pump(coin_info, pair, actual_multi, address, manual=True)
    if ok:
        sync_to_github(f"ম্যানুয়াল পাম্প: {symbol}")
    await update.message.reply_text(f"{'✅' if ok else '❌'} <b>{name}</b>\n{msg}", parse_mode="HTML")

async def cmd_forcepump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /forcepump TOKEN_ADDRESS")
        return
    address = context.args[0].strip()
    if is_duplicate(address):
        await update.message.reply_text("⚠️ ডুপ্লিকেট!")
        return
    await update.message.reply_text("⏳ ডেটা আনছি...")
    async with aiohttp.ClientSession() as session:
        pair = await fetch_pair_data(session, address)
    if not pair:
        await update.message.reply_text("❌ ডেটা পাওয়া যায়নি।")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    coin_info = {"name": name, "symbol": symbol}
    ok, msg = learn_pump(coin_info, pair, 3.0, address, manual=True)
    if ok:
        sync_to_github(f"ফোর্স পাম্প: {symbol}")
    await update.message.reply_text(f"{'✅' if ok else '❌'} <b>{name}</b>\n{msg}", parse_mode="HTML")

async def cmd_dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /dump TOKEN_ADDRESS")
        return
    address = context.args[0].strip()
    if is_duplicate(address):
        await update.message.reply_text("⚠️ ডুপ্লিকেট!")
        return
    await update.message.reply_text("⏳ ডেটা আনছি...")
    async with aiohttp.ClientSession() as session:
        pair = await fetch_pair_data(session, address)
    if not pair:
        await update.message.reply_text("❌ ডেটা পাওয়া যায়নি।")
        return
    name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "???")
    coin_info = {"name": name, "symbol": symbol}
    ok, msg = learn_dump(coin_info, pair, address, manual=True)
    if ok:
        sync_to_github(f"ম্যানুয়াল ডাম্প: {symbol}")
    await update.message.reply_text(f"{'✅' if ok else '❌'} <b>{name}</b>\n{msg}", parse_mode="HTML")

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    text = update.message.text
    if text == "📊 স্ট্যাটাস":
        stats = get_stats()
        status_text = "🟢 চালু" if bot_active else "🔴 বন্ধ"
        await update.message.reply_text(
            f"📊 <b>বটের অবস্থা: {status_text}</b>\n"
            f"🆕 লঞ্চ ট্র্যাক: <b>{len(launch_tracking)}</b>\n"
            f"🔍 মাইগ্রেশন ট্র্যাক: <b>{len(tracked_coins)}</b>\n"
            f"🚫 ব্ল্যাকলিস্ট: <b>{len(blacklisted)}</b>\n"
            f"🚀 পাম্প: <b>{len(pump_coins)}</b>\n"
            f"⚡ সিগনাল: <b>{len(alerted_coins)}</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"📚 লঞ্চ প্যাটার্ন: <b>{stats['launch_patterns']}</b>\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b>\n"
            f"🎯 থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>",
            parse_mode="HTML"
        )
    elif text == "📈 পারফরম্যান্স":
        stats = get_stats()
        await update.message.reply_text(
            f"📈 <b>পারফরম্যান্স</b>\n"
            f"⚡ মোট সিগনাল: <b>{stats['total_signals']}</b>\n"
            f"✅ চেক হয়েছে: <b>{stats['checked_signals']}</b>\n"
            f"🏆 সফল (2x+): <b>{stats['successful_signals']}</b>\n"
            f"🎯 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"⏰ সেরা সময়: <b>{stats['best_hour']}:00 UTC</b>",
            parse_mode="HTML"
        )
    elif text == "🏆 ট্রেন":
        stats = get_stats()
        await update.message.reply_text(
            f"🏆 <b>লার্নিং স্ট্যাটাস</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"📚 লঞ্চ প্যাটার্ন: <b>{stats['launch_patterns']}</b>\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b>\n"
            f"✍️ ম্যানুয়াল পাম্প: <b>{stats['manual_pumps']}</b>\n"
            f"🎯 থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>\n"
            f"📊 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"/pump ADDRESS\n/dump ADDRESS\n/threshold 50",
            parse_mode="HTML"
        )
    elif text == "⚙️ সেটিংস":
        stats = get_stats()
        await update.message.reply_text(
            f"⚙️ <b>সেটিংস</b>\n"
            f"📈 পাম্প থ্রেশোল্ড: {PUMP_MULTIPLIER}x\n"
            f"🎯 AI থ্রেশোল্ড: {int(current_threshold*100)}%\n"
            f"🔌 PumpPortal: রিয়েলটাইম WebSocket\n"
            f"📚 হিস্ট্রি স্ক্যান: প্রতি ১ ঘণ্টা\n"
            f"🛡️ Rugcheck: Freeze/Mint Authority\n"
            f"👥 Helius: লঞ্চ ট্রানজেকশন\n"
            f"💧 মিন লিকুইডিটি: {format_number(MIN_LIQUIDITY)}\n"
            f"💰 MCap: {format_number(MIN_MCAP)} - {format_number(MAX_MCAP)}",
            parse_mode="HTML"
        )
    elif text == "✅ অন":
        bot_active = True
        await update.message.reply_text("✅ বট চালু!")
    elif text == "❌ অফ":
        bot_active = False
        await update.message.reply_text("❌ বট বন্ধ!")

async def send_daily_report(bot):
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 18 and now.minute < 2:
            report = get_daily_report()
            best = report.get("best_signal")
            best_text = f"${best['symbol']} → {best.get('result_multiplier', 0)}x" if best else "N/A"
            await send_msg(bot,
                f"📋 <b>দৈনিক রিপোর্ট</b>\n"
                f"📅 {report['date']}\n"
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
    app.add_handler(CommandHandler("forcepump", cmd_forcepump))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    loop = asyncio.get_event_loop()
    loop.create_task(scan_loop(app.bot))
    loop.create_task(send_daily_report(app.bot))
    app.run_polling()

if __name__ == "__main__":
    main()

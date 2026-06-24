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
from learner import score_coin, learn_pump, learn_dump, record_signal, update_signal_result, get_stats, get_daily_report, is_duplicate, verify_pump, get_launch_age
from github_sync import sync_to_github, restore_from_github

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
RUGCHECK_URL = "https://api.rugcheck.xyz/v1"
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

PUMP_MULTIPLIER = 3.0
SCAN_INTERVAL = 120
MIN_LIQUIDITY = 5000
MIN_MCAP = 10000
MAX_MCAP = 500000
PAPER_TRADE_SOL = 0.1
SIGNAL_CHECK_HOURS = 6
GITHUB_SYNC_INTERVAL = 21600

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

tracked_coins = {}
pump_coins = {}
dump_coins = {}
alerted_coins = set()
signal_tracking = {}
blacklisted = set()
launch_tracking = {}
paper_trades = {}
bot_active = True
current_threshold = 0.35

def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 স্ট্যাটাস"), KeyboardButton("📈 পারফরম্যান্স")],
        [KeyboardButton("🏆 ট্রেন"), KeyboardButton("💰 পেপার ট্রেড")],
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

def calculate_tp(pump_patterns):
    if not pump_patterns:
        return 2.0
    avg = sum(p.get("final_multiplier", 2.0) for p in pump_patterns[-20:]) / len(pump_patterns[-20:])
    return round(min(avg * 0.6, 5.0), 2)

async def check_honeypot(session, address):
    try:
        url = f"{RUGCHECK_URL}/tokens/{address}/report/summary"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                risks = data.get("risks", [])
                lp_locked = data.get("lpLockedPct", 0)
                risk_names = [r.get("name", "") for r in risks if isinstance(r, dict)]
                is_honeypot = any(r in [
                    "Freeze Authority still enabled",
                    "Mint Authority still enabled"
                ] for r in risk_names)
                return {"is_honeypot": is_honeypot, "lp_locked": lp_locked, "risks": risk_names}
    except Exception as e:
        logger.error(f"Rugcheck error: {e}")
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
    except:
        pass
    return None

async def fetch_new_solana_pairs(session):
    try:
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [p for p in data if p.get("chainId") == "solana"]
    except:
        pass
    return []

async def fetch_boosted_pairs(session):
    try:
        url = "https://api.dexscreener.com/token-boosts/latest/v1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return [p for p in data if p.get("chainId") == "solana"]
    except:
        pass
    return []

async def send_msg(bot, text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Send error: {e}")

def open_paper_trade(address, symbol, price, tp_multiplier):
    if address in paper_trades:
        return
    paper_trades[address] = {
        "symbol": symbol,
        "buy_price": price,
        "buy_sol": PAPER_TRADE_SOL,
        "tp_price": price * tp_multiplier,
        "tp_multiplier": tp_multiplier,
        "open_time": datetime.now(timezone.utc).timestamp(),
        "closed": False,
        "result": None
    }
    logger.info(f"📝 Paper buy: {symbol} @ {price:.8f} TP: {tp_multiplier}x")

async def check_paper_trades(session, bot):
    from learner import load_data
    data = load_data()
    tp = calculate_tp(data.get("pump_patterns", []))
    for addr, trade in list(paper_trades.items()):
        if trade["closed"]:
            continue
        age = datetime.now(timezone.utc).timestamp() - trade["open_time"]
        if age < 300:
            continue
        pair = await fetch_pair_data(session, addr)
        if not pair:
            continue
        current_price = float(pair.get("priceUsd", 0) or 0)
        if current_price <= 0:
            continue
        multiplier = current_price / trade["buy_price"] if trade["buy_price"] > 0 else 0
        symbol = trade["symbol"]
        tp_hit = current_price >= trade["tp_price"]
        sl_hit = multiplier < 0.5
        time_exit = age > SIGNAL_CHECK_HOURS * 3600
        if tp_hit or sl_hit or time_exit:
            pnl_sol = (multiplier - 1) * PAPER_TRADE_SOL
            pnl_pct = (multiplier - 1) * 100
            emoji = "✅" if multiplier >= 1.5 else "❌"
            reason = "TP ✅" if tp_hit else ("SL ❌" if sl_hit else "সময় শেষ ⏰")
            await send_msg(bot,
                f"{emoji} <b>পেপার ট্রেড বন্ধ!</b>\n"
                f"🏷️ ${symbol}\n"
                f"📈 ফলাফল: <b>{multiplier:.2f}x</b>\n"
                f"💰 P&L: <b>{pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)</b>\n"
                f"🎯 কারণ: {reason}"
            )
            paper_trades[addr]["closed"] = True
            paper_trades[addr]["result"] = multiplier

async def pumpportal_loop(bot, session):
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
                            if address and address not in blacklisted:
                                symbol = data.get("symbol", "???")
                                name = data.get("name", "Unknown")
                                market_cap = float(data.get("marketCapSol", 0) or 0)
                                if address not in launch_tracking:
                                    launch_tracking[address] = {
                                        "name": name,
                                        "symbol": symbol,
                                        "market_cap_sol": market_cap,
                                        "buy_count": 0,
                                        "sell_count": 0,
                                        "unique_wallets": set(),
                                        "first_seen": datetime.now(timezone.utc).timestamp(),
                                        "signaled": False
                                    }
                        elif "txType" in data:
                            address = data.get("mint")
                            tx_type = data.get("txType", "")
                            if address and address in launch_tracking:
                                if tx_type == "buy":
                                    launch_tracking[address]["buy_count"] += 1
                                    wallet = data.get("traderPublicKey", "")
                                    if wallet:
                                        launch_tracking[address]["unique_wallets"].add(wallet)
                                    bonding_curve = float(data.get("bondingCurveKey", 0) or 0)
                                    progress = float(data.get("progress", 0) or 0)
                                    if 20 <= progress <= 80 and not launch_tracking[address].get("signaled"):
                                        asyncio.create_task(check_early_signal(bot, session, address))
                                elif tx_type == "sell":
                                    launch_tracking[address]["sell_count"] += 1
                                elif tx_type == "migrate":
                                    asyncio.create_task(handle_migration(bot, session, address))
                    except Exception as e:
                        logger.error(f"WS error: {e}")
        except Exception as e:
            logger.error(f"PumpPortal error: {e}")
        await asyncio.sleep(10)

async def check_early_signal(bot, session, address):
    if address not in launch_tracking:
        return
    info = launch_tracking[address]
    if info.get("signaled") or address in alerted_coins:
        return
    symbol = info.get("symbol", "???")
    name = info.get("name", "Unknown")
    buy_count = info.get("buy_count", 0)
    sell_count = info.get("sell_count", 0)
    unique_wallets = len(info.get("unique_wallets", set()))
    age = datetime.now(timezone.utc).timestamp() - info.get("first_seen", 0)
    buy_sell_ratio = buy_count / max(sell_count, 1)
    if buy_count < 5 or unique_wallets < 3:
        return
    rug = await check_honeypot(session, address)
    if rug and rug["is_honeypot"]:
        blacklisted.add(address)
        launch_tracking[address]["signaled"] = True
        logger.info(f"🚫 Honeypot: {symbol}")
        return
    from learner import load_data
    data = load_data()
    pump_patterns = data.get("pump_patterns", [])
    tp = calculate_tp(pump_patterns)
    score = 0.0
    reasons = []
    if buy_count >= 10:
        score += 0.3
        reasons.append("Buy count ✅")
    if unique_wallets >= 5:
        score += 0.25
        reasons.append("Unique wallets ✅")
    if buy_sell_ratio >= 3:
        score += 0.25
        reasons.append("Buy pressure ✅")
    elif buy_sell_ratio >= 2:
        score += 0.1
    if len(pump_patterns) >= 5:
        avg_buys = sum(p.get("buys_m5", 0) for p in pump_patterns[-10:]) / len(pump_patterns[-10:])
        if buy_count >= avg_buys * 0.5:
            score += 0.2
            reasons.append("Pattern match ✅")
    lp = rug["lp_locked"] if rug else 0
    if score >= current_threshold:
        confidence_pct = int(score * 100)
        confidence_bar = "🟢" * int(confidence_pct/20) + "⚪" * (5 - int(confidence_pct/20))
        link = gmgn_link(address)
        await send_msg(bot,
            f"🚀 <b>আর্লি সিগনাল! (Pre-Migration)</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏷️ <b>{name}</b> (${symbol})\n"
            f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
            f"🧠 <i>{' | '.join(reasons)}</i>\n"
            f"📊 Buy: <b>{buy_count}</b> | Sell: <b>{sell_count}</b>\n"
            f"👥 Wallets: <b>{unique_wallets}</b>\n"
            f"⏱️ বয়স: <b>{int(age)}s</b>\n"
            f"🔒 LP: <b>{lp}%</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📝 Paper TP: <b>{tp}x</b>\n"
            f"⚠️ <i>মাইগ্রেশনের আগে! DYOR!</i>\n"
            f"🔗 <a href='{link}'>GMGN</a>"
        )
        record_signal(address, symbol, score, 0, 0)
        launch_tracking[address]["signaled"] = True
        alerted_coins.add(address)
        logger.info(f"🚀 আর্লি সিগনাল: {symbol} score={score}")

async def handle_migration(bot, session, address):
    if address not in launch_tracking and address in blacklisted:
        return
    info = launch_tracking.get(address, {})
    symbol = info.get("symbol", "???")
    await asyncio.sleep(8)
    pair = await fetch_pair_data(session, address)
    if not pair:
        return
    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    mcap = float(pair.get("fdv", 0) or 0)
    price = float(pair.get("priceUsd", 0) or 0)
    if liquidity < MIN_LIQUIDITY or mcap < MIN_MCAP or price <= 0:
        return
    if address not in tracked_coins:
        tracked_coins[address] = {
            "initial_price": price,
            "name": info.get("name", pair.get("baseToken", {}).get("name", "Unknown")),
            "symbol": symbol or pair.get("baseToken", {}).get("symbol", "???"),
            "first_seen": datetime.now(timezone.utc).timestamp(),
            "source": "migration"
        }
    if address not in alerted_coins:
        from learner import load_data
        data = load_data()
        tp = calculate_tp(data.get("pump_patterns", []))
        rug = await check_honeypot(session, address)
        if rug and rug["is_honeypot"]:
            blacklisted.add(address)
            return
        coin_info = tracked_coins[address]
        ai_score, reason = score_coin(pair, coin_info, 0)
        if ai_score >= current_threshold:
            lp = rug["lp_locked"] if rug else 0
            link = gmgn_link(address)
            name = coin_info["name"]
            sym = coin_info["symbol"]
            confidence_pct = int(ai_score * 100)
            confidence_bar = "🟢" * int(confidence_pct/20) + "⚪" * (5 - int(confidence_pct/20))
            await send_msg(bot,
                f"⚡ <b>মাইগ্রেশন সিগনাল!</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🏷️ <b>{name}</b> (${sym})\n"
                f"🎯 কনফিডেন্স: {confidence_bar} <b>{confidence_pct}%</b>\n"
                f"🧠 <i>{reason}</i>\n"
                f"💵 দাম: <b>{price:.8f}</b>\n"
                f"💰 MCap: <b>{format_number(mcap)}</b>\n"
                f"💧 লিকুইডিটি: <b>{format_number(liquidity)}</b>\n"
                f"🔒 LP: <b>{lp}%</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📝 Paper TP: <b>{tp}x</b>\n"
                f"🔗 <a href='{link}'>GMGN</a>"
            )
            record_signal(address, sym, ai_score, price, mcap)
            signal_tracking[address] = {
                "symbol": sym,
                "price_at_signal": price,
                "signal_time": datetime.now(timezone.utc).timestamp(),
                "checked": False
            }
            open_paper_trade(address, sym, price, tp)
            alerted_coins.add(address)

async def realtime_scan_loop(bot, session):
    sync_counter = 0
    while True:
        try:
            if not bot_active:
                await asyncio.sleep(30)
                continue
            new_tokens = await fetch_new_solana_pairs(session)
            for t in new_tokens[:10]:
                addr = t.get("tokenAddress") or t.get("address")
                if not addr or addr in tracked_coins or addr in blacklisted:
                    continue
                await asyncio.sleep(1)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                mcap = float(pair.get("fdv", 0) or 0)
                age = get_launch_age(pair)
                if liquidity < MIN_LIQUIDITY or mcap < MIN_MCAP or mcap > MAX_MCAP:
                    continue
                if age and age > 7200:
                    continue
                price = float(pair.get("priceUsd", 0) or 0)
                if price > 0:
                    tracked_coins[addr] = {
                        "initial_price": price,
                        "name": pair.get("baseToken", {}).get("name", "Unknown"),
                        "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                        "first_seen": datetime.now(timezone.utc).timestamp(),
                        "source": "dexscreener"
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
                mcap = float(pair.get("fdv", 0) or 0)
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                name = coin_info.get("name", "Unknown")
                symbol = coin_info.get("symbol", "???")
                link = gmgn_link(addr)
                if liquidity < 300:
                    blacklisted.add(addr)
                    continue
                verified, actual_multi = verify_pump(pair)
                if verified:
                    pump_coins[addr] = coin_info
                    learn_pump(coin_info, pair, actual_multi, addr, manual=False)
                    from learner import load_data
                    d = load_data()
                    tp = calculate_tp(d.get("pump_patterns", []))
                    await send_msg(bot,
                        f"🚀 <b>পাম্প কয়েন শেখা!</b>\n"
                        f"🏷️ <b>{name}</b> (${symbol})\n"
                        f"📈 <b>{actual_multi}x</b> | ⏱️ {int((age or 0)/60)}m\n"
                        f"💰 {format_number(mcap)}\n"
                        f"🎯 নতুন TP: <b>{tp}x</b>\n"
                        f"🔗 <a href='{link}'>GMGN</a>"
                    )
                    sync_to_github(f"পাম্প: {symbol} {actual_multi}x")
                    continue
                if age > 86400:
                    dump_coins[addr] = coin_info
                    learn_dump(coin_info, pair, addr, manual=False)
                    continue
                if addr not in alerted_coins and age < 7200:
                    ai_score, reason = score_coin(pair, coin_info, age)
                    if ai_score >= current_threshold:
                        from learner import load_data
                        d = load_data()
                        tp = calculate_tp(d.get("pump_patterns", []))
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
                            f"⏱️ বয়স: <b>{int((age or 0)/60)}m</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"📝 Paper TP: <b>{tp}x</b>\n"
                            f"🔗 <a href='{link}'>GMGN</a>"
                        )
                        record_signal(addr, symbol, ai_score, current_price, mcap)
                        signal_tracking[addr] = {
                            "symbol": symbol,
                            "price_at_signal": current_price,
                            "signal_time": datetime.now(timezone.utc).timestamp(),
                            "checked": False
                        }
                        open_paper_trade(addr, symbol, current_price, tp)
                        alerted_coins.add(addr)
            await check_paper_trades(session, bot)
            await check_signal_results(session, bot)
            sync_counter += 1
            if sync_counter >= GITHUB_SYNC_INTERVAL // SCAN_INTERVAL:
                sync_to_github()
                sync_counter = 0
            logger.info(f"ট্র্যাক: {len(tracked_coins)} | লঞ্চ: {len(launch_tracking)} | পাম্প: {len(pump_coins)} | সিগনাল: {len(alerted_coins)}")
        except Exception as e:
            logger.error(f"স্ক্যান এরর: {e}")
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
            for addr in list(all_addrs.keys())[:30]:
                if is_duplicate(addr):
                    continue
                await asyncio.sleep(2)
                pair = await fetch_pair_data(session, addr)
                if not pair:
                    continue
                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                age = get_launch_age(pair)
                if liquidity < 3000 or age is None or age > 86400:
                    continue
                rug = await check_honeypot(session, addr)
                if rug and rug["is_honeypot"]:
                    continue
                verified, actual_multi = verify_pump(pair)
                coin_info = {
                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "symbol": pair.get("baseToken", {}).get("symbol", "???"),
                }
                if verified:
                    ok, msg = learn_pump(coin_info, pair, actual_multi, addr, manual=False)
                    if ok:
                        learned_pump += 1
                        from learner import load_data
                        d = load_data()
                        tp = calculate_tp(d.get("pump_patterns", []))
                        link = gmgn_link(addr)
                        await send_msg(bot,
                            f"📚 <b>পাম্প শেখা!</b>\n"
                            f"🏷️ <b>{coin_info['name']}</b> (${coin_info['symbol']})\n"
                            f"📈 <b>{actual_multi}x</b> | ⏱️ {int((age or 0)/60)}m\n"
                            f"💰 {format_number(pair.get('fdv', 0))}\n"
                            f"🎯 নতুন TP: <b>{tp}x</b>\n"
                            f"🔗 <a href='{link}'>GMGN</a>"
                        )
            if learned_pump > 0:
                sync_to_github(f"হিস্ট্রি: {learned_pump} পাম্প")
        except Exception as e:
            logger.error(f"হিস্ট্রি এরর: {e}")
        await asyncio.sleep(3600)

async def check_signal_results(session, bot):
    for addr, sig_info in list(signal_tracking.items()):
        if sig_info.get("checked"):
            continue
        age = datetime.now(timezone.utc).timestamp() - sig_info["signal_time"]
        if age < SIGNAL_CHECK_HOURS * 3600:
            continue
        pair = await fetch_pair_data(session, addr)
        if not pair:
            sig_info["checked"] = True
            continue
        current_price = float(pair.get("priceUsd", 0) or 0)
        if current_price <= 0:
            continue
        update_signal_result(addr, current_price)
        multiplier = current_price / sig_info["price_at_signal"] if sig_info["price_at_signal"] > 0 else 0
        emoji = "✅" if multiplier >= 1.5 else "❌"
        await send_msg(bot,
            f"{emoji} <b>সিগনাল ফলাফল! ({SIGNAL_CHECK_HOURS}h)</b>\n"
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
        "/threshold 50 — থ্রেশোল্ড (১-১০০)",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_threshold
    if not context.args:
        await update.message.reply_text(f"⚙️ থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>", parse_mode="HTML")
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
        await update.message.reply_text(f"⚠️ ৩x ভেরিফাই হয়নি ({actual_multi}x)\n/forcepump {address}")
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
        open_trades = sum(1 for t in paper_trades.values() if not t["closed"])
        await update.message.reply_text(
            f"📊 <b>বটের অবস্থা: {status_text}</b>\n"
            f"🆕 লঞ্চ ট্র্যাক: <b>{len(launch_tracking)}</b>\n"
            f"🔍 মাইগ্রেশন ট্র্যাক: <b>{len(tracked_coins)}</b>\n"
            f"🚀 পাম্প: <b>{len(pump_coins)}</b>\n"
            f"⚡ সিগনাল: <b>{len(alerted_coins)}</b>\n"
            f"🚫 ব্ল্যাকলিস্ট: <b>{len(blacklisted)}</b>\n"
            f"📝 Paper trades (open): <b>{open_trades}</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"🎯 থ্রেশোল্ড: <b>{int(current_threshold*100)}%</b>",
            parse_mode="HTML"
        )
    elif text == "📈 পারফরম্যান্স":
        stats = get_stats()
        await update.message.reply_text(
            f"📈 <b>পারফরম্যান্স</b>\n"
            f"⚡ মোট সিগনাল: <b>{stats['total_signals']}</b>\n"
            f"✅ চেক হয়েছে: <b>{stats['checked_signals']}</b>\n"
            f"🏆 সফল (1.5x+): <b>{stats['successful_signals']}</b>\n"
            f"🎯 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"⏰ সেরা সময়: <b>{stats['best_hour']}:00 UTC</b>",
            parse_mode="HTML"
        )
    elif text == "🏆 ট্রেন":
        stats = get_stats()
        from learner import load_data
        d = load_data()
        tp = calculate_tp(d.get("pump_patterns", []))
        await update.message.reply_text(
            f"🏆 <b>লার্নিং স্ট্যাটাস</b>\n"
            f"🧠 পাম্প প্যাটার্ন: <b>{stats['pump_patterns']}</b>\n"
            f"📉 ডাম্প প্যাটার্ন: <b>{stats['dump_patterns']}</b>\n"
            f"🎯 AI TP: <b>{tp}x</b>\n"
            f"📊 একুরেসি: <b>{stats['accuracy']}%</b>\n"
            f"{'✅ মডেল রেডি!' if stats['pump_patterns'] >= 5 else '⏳ আরো ডেটা দরকার...'}\n\n"
            f"/pump ADDRESS\n/dump ADDRESS\n/threshold 50",
            parse_mode="HTML"
        )
    elif text == "💰 পেপার ট্রেড":
        closed = [t for t in paper_trades.values() if t["closed"]]
        open_t = [t for t in paper_trades.values() if not t["closed"]]
        total_pnl = sum((t.get("result", 1) - 1) * PAPER_TRADE_SOL for t in closed if t.get("result"))
        wins = sum(1 for t in closed if (t.get("result") or 0) >= 1.5)
        await update.message.reply_text(
            f"💰 <b>পেপার ট্রেড</b>\n"
            f"📂 Open: <b>{len(open_t)}</b>\n"
            f"✅ Closed: <b>{len(closed)}</b>\n"
            f"🏆 Wins: <b>{wins}/{len(closed)}</b>\n"
            f"💵 Total P&L: <b>{total_pnl:+.4f} SOL</b>\n"
            f"📊 Win rate: <b>{int(wins/max(len(closed),1)*100)}%</b>",
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
            closed = [t for t in paper_trades.values() if t["closed"]]
            total_pnl = sum((t.get("result", 1) - 1) * PAPER_TRADE_SOL for t in closed if t.get("result"))
            best = report.get("best_signal")
            best_text = f"${best['symbol']} → {best.get('result_multiplier', 0)}x" if best else "N/A"
            await send_msg(bot,
                f"📋 <b>দৈনিক রিপোর্ট</b>\n"
                f"📅 {report['date']}\n"
                f"⚡ সিগনাল: <b>{report['signals_sent']}</b>\n"
                f"🚀 পাম্প শেখা: <b>{report['pumps_learned']}</b>\n"
                f"✅ সফল: <b>{report['successful']}/{report['checked']}</b>\n"
                f"💰 Paper P&L: <b>{total_pnl:+.4f} SOL</b>\n"
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

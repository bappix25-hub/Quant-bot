import json
import os
import hashlib
from datetime import datetime, timezone

DATA_FILE = os.path.expanduser("~/bot_data.json")

DEFAULT_DATA = {
    "pump_patterns": [],
    "dump_patterns": [],
    "launch_patterns": [],
    "trained_addresses": [],
    "signals": [],
    "model": {
        "avg_pump_mcap": 0,
        "avg_pump_liquidity": 0,
        "avg_pump_volume_h1": 0,
        "avg_pump_buys_h1": 0,
        "best_hours": {},
        "total_signals": 0,
        "correct_signals": 0,
        "accuracy": 0.0,
        "threshold": 0.35
    }
}

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
                for key in DEFAULT_DATA:
                    if key not in data:
                        data[key] = DEFAULT_DATA[key]
                for key in DEFAULT_DATA["model"]:
                    if key not in data["model"]:
                        data["model"][key] = DEFAULT_DATA["model"][key]
                return data
        except:
            pass
    return DEFAULT_DATA.copy()

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _hash_address(address):
    return hashlib.md5(address.lower().encode()).hexdigest()

def is_duplicate(address):
    data = load_data()
    return _hash_address(address) in data.get("trained_addresses", [])

def _mark_trained(data, address):
    h = _hash_address(address)
    if h not in data["trained_addresses"]:
        data["trained_addresses"].append(h)

def get_launch_age(pair):
    try:
        created_at = pair.get("pairCreatedAt")
        if created_at:
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            return (now_ms - int(created_at)) / 1000
    except:
        pass
    return None

def verify_pump(pair):
    try:
        h1 = float(pair.get("priceChange", {}).get("h1", 0) or 0)
        h6 = float(pair.get("priceChange", {}).get("h6", 0) or 0)
        h24 = float(pair.get("priceChange", {}).get("h24", 0) or 0)
        best = max(h1, h6, h24)
        multiplier = 1 + best / 100
        return multiplier >= 3.0, round(multiplier, 2)
    except:
        return False, 0

def extract_pattern(pair, age_seconds=None):
    try:
        if age_seconds is None:
            age_seconds = get_launch_age(pair) or 0
        return {
            "mcap": float(pair.get("fdv", 0) or 0),
            "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "volume_h1": float(pair.get("volume", {}).get("h1", 0) or 0),
            "volume_m5": float(pair.get("volume", {}).get("m5", 0) or 0),
            "age_seconds": age_seconds,
            "price_change_5m": float(pair.get("priceChange", {}).get("m5", 0) or 0),
            "price_change_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
            "hour_of_day": datetime.now(timezone.utc).hour,
            "buys_m5": int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0),
            "sells_m5": int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0),
            "buys_h1": int(pair.get("txns", {}).get("h1", {}).get("buys", 0) or 0),
            "sells_h1": int(pair.get("txns", {}).get("h1", {}).get("sells", 0) or 0),
        }
    except:
        return None

def learn_pump(coin_info, pair, final_multiplier, address=None, manual=False):
    data = load_data()
    if address and is_duplicate(address):
        return False, "ডুপ্লিকেট!"
    if not manual:
        verified, actual_multi = verify_pump(pair)
        if not verified:
            return False, f"৩x ভেরিফাই হয়নি ({actual_multi}x)"
        final_multiplier = actual_multi
    age = get_launch_age(pair)
    pattern = extract_pattern(pair, age)
    if not pattern:
        return False, "ডেটা নেই"
    pattern["symbol"] = coin_info.get("symbol", "???")
    pattern["name"] = coin_info.get("name", "Unknown")
    pattern["address"] = address or ""
    pattern["final_multiplier"] = final_multiplier
    pattern["manual"] = manual
    pattern["timestamp"] = datetime.now(timezone.utc).isoformat()
    data["pump_patterns"].append(pattern)
    data["pump_patterns"] = data["pump_patterns"][-500:]
    if address:
        _mark_trained(data, address)
    _update_model(data)
    save_data(data)
    return True, f"✅ পাম্প শেখা! {final_multiplier}x | মোট: {len(data['pump_patterns'])}"

def learn_dump(coin_info, pair, address=None, manual=False):
    data = load_data()
    if address and is_duplicate(address):
        return False, "ডুপ্লিকেট!"
    age = get_launch_age(pair)
    pattern = extract_pattern(pair, age)
    if not pattern:
        return False, "ডেটা নেই"
    pattern["symbol"] = coin_info.get("symbol", "???")
    pattern["address"] = address or ""
    pattern["final_multiplier"] = 0
    pattern["manual"] = manual
    pattern["timestamp"] = datetime.now(timezone.utc).isoformat()
    data["dump_patterns"].append(pattern)
    data["dump_patterns"] = data["dump_patterns"][-500:]
    if address:
        _mark_trained(data, address)
    save_data(data)
    return True, f"✅ ডাম্প শেখা! মোট: {len(data['dump_patterns'])}"

def _update_model(data):
    pumps = data["pump_patterns"]
    if not pumps:
        return
    model = data["model"]
    model["avg_pump_mcap"] = sum(p["mcap"] for p in pumps) / len(pumps)
    model["avg_pump_liquidity"] = sum(p["liquidity"] for p in pumps) / len(pumps)
    model["avg_pump_volume_h1"] = sum(p.get("volume_h1", 0) for p in pumps) / len(pumps)
    model["avg_pump_buys_h1"] = sum(p.get("buys_h1", 0) for p in pumps) / len(pumps)
    hour_counts = {}
    for p in pumps:
        h = str(p.get("hour_of_day", 0))
        hour_counts[h] = hour_counts.get(h, 0) + 1
    model["best_hours"] = hour_counts
    total = model["total_signals"]
    correct = model["correct_signals"]
    if total >= 5:
        model["accuracy"] = round(correct / total * 100, 1)
        if model["accuracy"] < 40:
            model["threshold"] = min(0.7, model["threshold"] + 0.05)
        elif model["accuracy"] > 70:
            model["threshold"] = max(0.2, model["threshold"] - 0.05)
    data["model"] = model

def score_coin(pair, coin_info, age_seconds=None):
    data = load_data()
    model = data["model"]
    pumps = data["pump_patterns"]
    dumps = data["dump_patterns"]
    if age_seconds is None:
        age_seconds = get_launch_age(pair) or 0
    pattern = extract_pattern(pair, age_seconds)
    if not pattern:
        return 0.0, "ডেটা নেই"
    if len(pumps) < 3:
        score = 0.0
        reasons = []
        if pattern["price_change_5m"] > 10:
            score += 0.3
            reasons.append("৫m মোমেন্টাম ✅")
        buys = pattern["buys_m5"]
        sells = pattern["sells_m5"]
        if buys + sells > 0 and buys / (buys + sells) > 0.65:
            score += 0.3
            reasons.append("Buy pressure ✅")
        if pattern["volume_m5"] > 500:
            score += 0.2
            reasons.append("Volume spike ✅")
        if pattern["liquidity"] > 8000:
            score += 0.2
            reasons.append("লিকুইডিটি ✅")
        return round(min(score, 1.0), 2), "⏳ শিখছি | " + " | ".join(reasons)
    score = 0.0
    reasons = []
    avg_mcap = model["avg_pump_mcap"]
    if avg_mcap > 0:
        ratio = pattern["mcap"] / avg_mcap
        if 0.1 <= ratio <= 5.0:
            score += 0.2
            reasons.append("MCap ✅")
        else:
            score -= 0.1
    avg_liq = model["avg_pump_liquidity"]
    if avg_liq > 0 and pattern["liquidity"] / avg_liq >= 0.2:
        score += 0.15
        reasons.append("লিকুইডিটি ✅")
    avg_vol = model["avg_pump_volume_h1"]
    if avg_vol > 0 and pattern["volume_h1"] / avg_vol >= 0.2:
        score += 0.15
        reasons.append("ভলিউম ✅")
    if pattern["price_change_5m"] > 10:
        score += 0.2
        reasons.append("৫m মোমেন্টাম ✅")
    elif pattern["price_change_5m"] < -20:
        score -= 0.2
    buys = pattern["buys_m5"]
    sells = pattern["sells_m5"]
    if buys + sells > 0:
        buy_ratio = buys / (buys + sells)
        if buy_ratio > 0.65:
            score += 0.15
            reasons.append("Buy pressure ✅")
        elif buy_ratio < 0.3:
            score -= 0.15
    hour = str(pattern["hour_of_day"])
    best_hours = model.get("best_hours", {})
    if best_hours:
        max_count = max(best_hours.values())
        if hour in best_hours and best_hours[hour] >= max_count * 0.6:
            score += 0.1
            reasons.append("সেরা সময় ✅")
    dump_matches = sum(1 for dp in dumps[-100:]
        if dp["mcap"] > 0 and pattern["mcap"] > 0
        and abs(dp["mcap"] - pattern["mcap"]) / pattern["mcap"] < 0.2)
    if dump_matches > 8:
        score -= 0.3
        reasons.append("⚠️ ডাম্প প্যাটার্ন")
    score = max(0.0, min(1.0, score))
    return round(score, 2), " | ".join(reasons) if reasons else "প্যাটার্ন দুর্বল"

def record_signal(address, symbol, score, price_at_signal, mcap_at_signal):
    data = load_data()
    data["signals"].append({
        "address": address,
        "symbol": symbol,
        "score": score,
        "price_at_signal": price_at_signal,
        "mcap_at_signal": mcap_at_signal,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result_multiplier": None,
        "result_checked": False
    })
    data["signals"] = data["signals"][-500:]
    data["model"]["total_signals"] = data["model"].get("total_signals", 0) + 1
    save_data(data)

def update_signal_result(address, current_price):
    data = load_data()
    updated = False
    for sig in data["signals"]:
        if sig["address"] == address and not sig["result_checked"]:
            if sig["price_at_signal"] > 0:
                multiplier = current_price / sig["price_at_signal"]
                sig["result_multiplier"] = round(multiplier, 2)
                sig["result_checked"] = True
                if multiplier >= 2.0:
                    data["model"]["correct_signals"] = data["model"].get("correct_signals", 0) + 1
                updated = True
    if updated:
        _update_model(data)
        save_data(data)

def get_stats():
    data = load_data()
    model = data["model"]
    checked = [s for s in data["signals"] if s["result_checked"]]
    successful = [s for s in checked if (s["result_multiplier"] or 0) >= 2.0]
    best_hours = model.get("best_hours", {})
    best_hour = max(best_hours, key=best_hours.get) if best_hours else "N/A"
    manual_pumps = sum(1 for p in data["pump_patterns"] if p.get("manual"))
    manual_dumps = sum(1 for p in data["dump_patterns"] if p.get("manual"))
    return {
        "pump_patterns": len(data["pump_patterns"]),
        "dump_patterns": len(data["dump_patterns"]),
        "launch_patterns": len(data.get("launch_patterns", [])),
        "manual_pumps": manual_pumps,
        "manual_dumps": manual_dumps,
        "total_signals": len(data["signals"]),
        "checked_signals": len(checked),
        "successful_signals": len(successful),
        "accuracy": model.get("accuracy", 0.0),
        "threshold": model.get("threshold", 0.35),
        "best_hour": best_hour,
        "trained_addresses": len(data.get("trained_addresses", []))
    }

def get_daily_report():
    data = load_data()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_signals = [s for s in data["signals"] if s["timestamp"].startswith(today)]
    today_pumps = [p for p in data["pump_patterns"] if p["timestamp"].startswith(today)]
    checked = [s for s in today_signals if s["result_checked"]]
    successful = [s for s in checked if (s["result_multiplier"] or 0) >= 2.0]
    best = max(checked, key=lambda x: x.get("result_multiplier") or 0) if checked else None
    return {
        "date": today,
        "signals_sent": len(today_signals),
        "pumps_learned": len(today_pumps),
        "checked": len(checked),
        "successful": len(successful),
        "best_signal": best
    }

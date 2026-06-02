import json
import os
import math
from datetime import datetime, timezone

DATA_FILE = os.path.expanduser("~/bot_data.json")

DEFAULT_DATA = {
    "pump_patterns": [],
    "dump_patterns": [],
    "signals": [],
    "daily_stats": {},
    "model": {
        "avg_pump_mcap": 0,
        "avg_pump_liquidity": 0,
        "avg_pump_volume": 0,
        "avg_pump_age": 0,
        "avg_pump_price_velocity": 0,
        "best_hours": {},
        "total_signals": 0,
        "correct_signals": 0,
        "accuracy": 0.0,
        "threshold": 0.4
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
                return data
        except:
            pass
    return DEFAULT_DATA.copy()

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def extract_pattern(coin_info, pair):
    try:
        age = 0
        created_at = pair.get("pairCreatedAt")
        if created_at:
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            age = (now_ms - int(created_at)) / 1000
        price_change_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        price_change_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
        return {
            "mcap": float(pair.get("fdv", 0) or 0),
            "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
            "volume_h24": float(pair.get("volume", {}).get("h24", 0) or 0),
            "volume_h1": float(pair.get("volume", {}).get("h1", 0) or 0),
            "volume_m5": float(pair.get("volume", {}).get("m5", 0) or 0),
            "age_seconds": age,
            "price_change_5m": price_change_5m,
            "price_change_1h": price_change_1h,
            "hour_of_day": datetime.now(timezone.utc).hour,
            "txns_m5": pair.get("txns", {}).get("m5", {}).get("buys", 0),
            "buys_m5": pair.get("txns", {}).get("m5", {}).get("buys", 0),
            "sells_m5": pair.get("txns", {}).get("m5", {}).get("sells", 0),
        }
    except:
        return None

def learn_pump(coin_info, pair, final_multiplier):
    data = load_data()
    pattern = extract_pattern(coin_info, pair)
    if not pattern:
        return
    pattern["symbol"] = coin_info.get("symbol", "???")
    pattern["name"] = coin_info.get("name", "Unknown")
    pattern["final_multiplier"] = final_multiplier
    pattern["timestamp"] = datetime.now(timezone.utc).isoformat()
    data["pump_patterns"].append(pattern)
    data["pump_patterns"] = data["pump_patterns"][-200:]
    _update_model(data)
    save_data(data)

def learn_dump(coin_info, pair):
    data = load_data()
    pattern = extract_pattern(coin_info, pair)
    if not pattern:
        return
    pattern["symbol"] = coin_info.get("symbol", "???")
    pattern["final_multiplier"] = 0
    pattern["timestamp"] = datetime.now(timezone.utc).isoformat()
    data["dump_patterns"].append(pattern)
    data["dump_patterns"] = data["dump_patterns"][-200:]
    save_data(data)

def _update_model(data):
    pumps = data["pump_patterns"]
    if len(pumps) < 3:
        return
    model = data["model"]
    model["avg_pump_mcap"] = sum(p["mcap"] for p in pumps) / len(pumps)
    model["avg_pump_liquidity"] = sum(p["liquidity"] for p in pumps) / len(pumps)
    model["avg_pump_volume"] = sum(p["volume_h24"] for p in pumps) / len(pumps)
    model["avg_pump_age"] = sum(p["age_seconds"] for p in pumps) / len(pumps)
    hour_counts = {}
    for p in pumps:
        h = str(p.get("hour_of_day", 0))
        hour_counts[h] = hour_counts.get(h, 0) + 1
    model["best_hours"] = hour_counts
    total = model["total_signals"]
    correct = model["correct_signals"]
    if total > 0:
        model["accuracy"] = round(correct / total * 100, 1)
        if model["accuracy"] < 40:
            model["threshold"] = min(0.7, model["threshold"] + 0.05)
        elif model["accuracy"] > 70:
            model["threshold"] = max(0.2, model["threshold"] - 0.05)
    data["model"] = model

def score_coin(pair, coin_info):
    data = load_data()
    model = data["model"]
    pumps = data["pump_patterns"]
    dumps = data["dump_patterns"]
    if len(pumps) < 3:
        return 0.0, "ডেটা কম, শিখছি..."
    pattern = extract_pattern(coin_info, pair)
    if not pattern:
        return 0.0, "ডেটা নেই"
    score = 0.0
    reasons = []
    avg_mcap = model["avg_pump_mcap"]
    if avg_mcap > 0:
        ratio = pattern["mcap"] / avg_mcap
        if 0.2 <= ratio <= 3.0:
            score += 0.25
            reasons.append("MCap মিলেছে")
        else:
            score -= 0.1
    avg_liq = model["avg_pump_liquidity"]
    if avg_liq > 0:
        ratio = pattern["liquidity"] / avg_liq
        if 0.2 <= ratio <= 3.0:
            score += 0.2
            reasons.append("লিকুইডিটি মিলেছে")
    if pattern["price_change_5m"] > 10:
        score += 0.2
        reasons.append("৫m মোমেন্টাম আছে")
    elif pattern["price_change_5m"] < -10:
        score -= 0.15
    buys = pattern["buys_m5"]
    sells = pattern["sells_m5"]
    if buys > 0 and sells >= 0:
        buy_ratio = buys / max(buys + sells, 1)
        if buy_ratio > 0.6:
            score += 0.15
            reasons.append("Buy pressure বেশি")
    hour = str(pattern["hour_of_day"])
    best_hours = model.get("best_hours", {})
    if best_hours and hour in best_hours:
        max_hour = max(best_hours.values())
        if best_hours[hour] >= max_hour * 0.7:
            score += 0.1
            reasons.append("ভালো সময়")
    if pattern["volume_m5"] > 500:
        score += 0.1
        reasons.append("Volume spike")
    dump_matches = 0
    for dp in dumps[-50:]:
        if dp["mcap"] > 0 and pattern["mcap"] > 0:
            if abs(dp["mcap"] - pattern["mcap"]) / pattern["mcap"] < 0.3:
                dump_matches += 1
    if dump_matches > 5:
        score -= 0.2
        reasons.append("ডাম্প প্যাটার্নে মিল")
    score = max(0.0, min(1.0, score))
    reason_text = " | ".join(reasons) if reasons else "প্যাটার্ন মিলছে না"
    return round(score, 2), reason_text

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
    pump_count = len(data["pump_patterns"])
    dump_count = len(data["dump_patterns"])
    signal_count = len(data["signals"])
    checked = [s for s in data["signals"] if s["result_checked"]]
    successful = [s for s in checked if (s["result_multiplier"] or 0) >= 2.0]
    best_hours = model.get("best_hours", {})
    best_hour = max(best_hours, key=best_hours.get) if best_hours else "N/A"
    return {
        "pump_patterns": pump_count,
        "dump_patterns": dump_count,
        "total_signals": signal_count,
        "checked_signals": len(checked),
        "successful_signals": len(successful),
        "accuracy": model.get("accuracy", 0.0),
        "threshold": model.get("threshold", 0.4),
        "best_hour": best_hour,
        "avg_pump_mcap": model.get("avg_pump_mcap", 0),
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

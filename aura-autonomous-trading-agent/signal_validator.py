def evaluate_signal(df):
    if len(df) < 55:
        return {"STATUS": "INSUFFICIENT_DATA", "SIGNAL_SCORE": 0,
                "REASON": "Need at least 55 bars."}

    row = df.iloc[-1]
    score = 0
    reasons = []

    if bool(row["cross_3_8"]):
        score += 15
        reasons.append("EMA3 crossed above EMA8")
    if row["ema_3"] > row["ema_8"]:
        score += 5
        reasons.append("EMA3 remains above EMA8")
    if row["ema_8"] > row["ema_21"]:
        score += 15
        reasons.append("EMA8 > EMA21")
    if row["ema_21"] > row["ema_50"]:
        score += 10
        reasons.append("EMA21 > EMA50")
    if row["macd"] > row["macd_signal"]:
        score += 10
        reasons.append("MACD bullish")
    if 50 <= row["rsi_14"] <= 70:
        score += 10
        reasons.append("RSI constructive")
    elif row["rsi_14"] > 75:
        score -= 15
        reasons.append("RSI extremely extended")
    if row["rel_volume"] >= 1.20:
        score += 15
        reasons.append("Relative volume >= 1.2x")
    if row["price_acceleration"] > 0:
        score += 5
        reasons.append("Positive acceleration")

    persistence = int((df["ema_3"].tail(3) > df["ema_8"].tail(3)).sum())
    if persistence >= 3:
        score += 5
        reasons.append("EMA3/8 persisted 3 bars")

    recent = df["close"].tail(4)
    if len(recent) == 4:
        prev_move = recent.iloc[-2] / recent.iloc[-3] - 1
        now_move = recent.iloc[-1] / recent.iloc[-2] - 1
        if prev_move > 0.01 and now_move < -0.005:
            score -= 20
            reasons.append("Possible momentum reversal / whipsaw")

    score = max(0, min(100, score))
    if bool(row["cross_3_8"]) and persistence <= 1:
        status = "EARLY"
    elif score >= 70 and persistence >= 2:
        status = "CONFIRMED"
    elif score >= 45:
        status = "CONFIRMING"
    else:
        status = "FAILED"

    return {
        "STATUS": status,
        "SIGNAL_SCORE": score,
        "PERSISTENCE_BARS": persistence,
        "RSI_14": round(float(row["rsi_14"]), 2),
        "REL_VOLUME": round(float(row["rel_volume"]), 2),
        "ATR_PCT": round(float(row["atr_pct"]), 2),
        "PRICE_ACCELERATION": round(float(row["price_acceleration"]), 5),
        "REASONS": " | ".join(reasons),
        "ORDER_PLACED": False
    }

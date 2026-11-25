import os
import json
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz

# ═══════════════════════════════════════════════════════════════════
# 1. КОНФИГУРАЦИЯ TELEGRAM И ЧАСОВОГО ПОЯСА
# ═══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9040646501:AAHyg3y78jRDX8D_SVuWdKxp0ZGXCzqwedo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7037914269")

# Часовой пояс Ташкента (UTC+5)
TIMEZONE_TASHKENT = pytz.timezone("Asia/Tashkent")

INTERVAL = "5m"
PERIOD = "5d"
POLL_INTERVAL_SEC = 20
STATE_FILE = "last_circle.json"

# ═══════════════════════════════════════════════════════════════════
# 2. СПИСОК ИНСТРУМЕНТОВ И ИХ ПАРАМЕТРЫ
# ═══════════════════════════════════════════════════════════════════
INSTRUMENTS = {
    "XAUUSD": {
        "ticker": "GC=F",
        "name": "Золото (XAUUSD)",
        "pipSize": 0.1,
        "exitPips": 80,
        "slBuf": 0.5,
        "swingK": 4.0,
        "atrLen": 20,
        "cooldown": 15,
        "entryN": 8,
        "adxLen": 14,
        "adxMin": 20.0,
        "useAdx": True,
        "useDi": True
    },
    "BTCUSD": {
        "ticker": "BTC-USD",
        "name": "Биткоин (BTCUSD)",
        "pipSize": 1.0,
        "exitPips": 500,
        "slBuf": 0.5,
        "swingK": 4.0,
        "atrLen": 20,
        "cooldown": 15,
        "entryN": 8,
        "adxLen": 14,
        "adxMin": 20.0,
        "useAdx": True,
        "useDi": True
    },
    "US30": {
        "ticker": "YM=F",
        "name": "Индекс US30 (Dow Jones)",
        "pipSize": 1.0,
        "exitPips": 80,
        "slBuf": 0.5,
        "swingK": 4.0,
        "atrLen": 20,
        "cooldown": 15,
        "entryN": 8,
        "adxLen": 14,
        "adxMin": 20.0,
        "useAdx": True,
        "useDi": True
    }
}


# ═══════════════════════════════════════════════════════════════════
# 3. МИНИ-ВЕБ-СЕРВЕР ДЛЯ РАБОТЫ НА RENDER FREE
# ═══════════════════════════════════════════════════════════════════
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Gold Swing v7 Immediate Signal Monitor is Active 24/7!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════════
# 4. СОСТОЯНИЕ И ОТПРАВКА В TELEGRAM
# ═══════════════════════════════════════════════════════════════════
def load_all_states() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_symbol_state(symbol_key: str, bar_ts: str):
    states = load_all_states()
    states[symbol_key] = bar_ts
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(states, f)
    except Exception as e:
        print(f"Ошибка сохранения {STATE_FILE}:", e)


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("Ошибка отправки Telegram:", e)


# ═══════════════════════════════════════════════════════════════════
# 5. ИНДИКАТОРЫ (RMA УАЙЛДЕРА)
# ═══════════════════════════════════════════════════════════════════
def compute_indicators(df: pd.DataFrame, atr_len: int, adx_len: int):
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / atr_len, adjust=False).mean()

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_rma = tr.ewm(alpha=1.0 / adx_len, adjust=False).mean()
    plus_dm_rma = pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / adx_len, adjust=False).mean()
    minus_dm_rma = pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / adx_len, adjust=False).mean()

    plus_di = 100.0 * (plus_dm_rma / tr_rma.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_rma / tr_rma.replace(0, np.nan))

    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1.0 / adx_len, adjust=False).mean()

    return atr, plus_di, minus_di, adx


# ═══════════════════════════════════════════════════════════════════
# 6. ЛОГИКА СИГНАЛА В МОМЕНТ РАЗВОРОТА (fBuy / fSell)
# ═══════════════════════════════════════════════════════════════════
def detect_immediate_signals(df: pd.DataFrame, cfg: dict):
    """
    Отслеживает фиксацию разворота fBuy / fSell (alertcondition fBuy/fSell)
    и отправляет сигнал СРАЗУ, в момент подтверждения разворота (без 40-минутной задержки).
    """
    atr, di_plus, di_minus, adx = compute_indicators(df, cfg["atrLen"], cfg["adxLen"])

    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    dip_arr = di_plus.to_numpy(dtype=float)
    dim_arr = di_minus.to_numpy(dtype=float)
    adx_arr = adx.to_numpy(dtype=float)

    n = len(df)
    if n < max(cfg["atrLen"], cfg["adxLen"]) + 10:
        return []

    dir_zz = 1
    ext_hi = highs[0]
    ext_lo = lows[0]
    ext_hi_b = 0
    ext_lo_b = 0

    last_sig_idx = -100000
    signals = []

    for i in range(1, n):
        h, l, c = highs[i], lows[i], closes[i]
        cur_atr = atr_arr[i]
        if np.isnan(cur_atr) or cur_atr <= 0:
            continue

        thr = cfg["swingK"] * cur_atr
        sell_sig = False
        buy_sig = False
        sig_price = np.nan
        sig_bar = None

        if dir_zz == 1:
            if h > ext_hi:
                ext_hi = h
                ext_hi_b = i
            if l <= ext_hi - thr:
                sell_sig = True
                sig_price = ext_hi
                sig_bar = ext_hi_b
                dir_zz = -1
                ext_lo = l
                ext_lo_b = i
        else:
            if l < ext_lo:
                ext_lo = l
                ext_lo_b = i
            if h >= ext_lo + thr:
                buy_sig = True
                sig_price = ext_lo
                sig_bar = ext_lo_b
                dir_zz = 1
                ext_hi = h
                ext_hi_b = i

        adx_ok = (not cfg["useAdx"]) or (not np.isnan(adx_arr[i]) and adx_arr[i] >= cfg["adxMin"])
        spaced = (i - last_sig_idx) >= cfg["cooldown"]
        di_buy = (not cfg["useDi"]) or (dip_arr[i] > dim_arr[i])
        di_sell = (not cfg["useDi"]) or (dim_arr[i] > dip_arr[i])

        f_sell = sell_sig and adx_ok and spaced and di_sell
        f_buy = buy_sig and adx_ok and spaced and di_buy

        if f_sell or f_buy:
            last_sig_idx = i

            # Расчёт SL и TP в момент сигнала
            sl_price = (sig_price - cfg["slBuf"] * cur_atr) if f_buy else (sig_price + cfg["slBuf"] * cur_atr)
            direction = 1 if f_buy else -1
            tp_price = c + direction * cfg["exitPips"] * cfg["pipSize"]

            # Точное ташкентское время (UTC+5)
            signal_ts = df.index[i].tz_convert(TIMEZONE_TASHKENT).strftime("%Y-%m-%d %H:%M")
            pivot_ts = df.index[sig_bar].tz_convert(TIMEZONE_TASHKENT).strftime("%H:%M") if sig_bar is not None else ""

            signals.append({
                "signal_ts": signal_ts,
                "pivot_ts": pivot_ts,
                "side": "🟢 КРУГ BUY (Разворот ВВЕРХ)" if f_buy else "🔴 КРУГ SELL (Разворот ВНИЗ)",
                "cur_price": c,
                "pivot": sig_price,
                "sl": sl_price,
                "tp": tp_price
            })

    return signals


# ═══════════════════════════════════════════════════════════════════
# 7. ПОЛУЧЕНИЕ СВЕЧЕЙ (ТОЛЬКО ЗАКРЫТЫЕ)
# ═══════════════════════════════════════════════════════════════════
def fetch_closed_candles(ticker: str) -> pd.DataFrame | None:
    try:
        raw = yf.download(tickers=ticker, period=PERIOD, interval=INTERVAL, progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.dropna(subset=["Open", "High", "Low", "Close"])
        if len(raw) < 60:
            return None
        return raw.iloc[:-1].copy()
    except Exception as e:
        print(f"Ошибка загрузки данных {ticker}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# 8. ЦИКЛ МОНИТОРИНГА
# ═══════════════════════════════════════════════════════════════════
def monitor_loop():
    print("Запуск мгновенного мониторинга Gold Swing v7...")
    start_msg = (
        "⚡ <b>Монитор Gold Swing v7 обновлен: Мгновенные сигналы!</b>\n\n"
        "🔔 <b>Режим оповещения:</b> Сигнал отправляется СРАЗУ в момент фиксации разворота (без 40-минутного ожидания entryN)!\n"
        "📊 <b>Пары (M5):</b> XAUUSD | BTCUSD | US30\n"
        "🕒 <b>Время:</b> Ташкент (UTC+5)"
    )
    send_telegram(start_msg)

    saved_states = load_all_states()

    # Фиксируем последние сигналы при старте
    for sym_key, cfg in INSTRUMENTS.items():
        if sym_key not in saved_states:
            df = fetch_closed_candles(cfg["ticker"])
            if df is not None:
                sigs = detect_immediate_signals(df, cfg)
                if sigs:
                    saved_states[sym_key] = sigs[-1]["signal_ts"]
                    save_symbol_state(sym_key, sigs[-1]["signal_ts"])
                    print(f"[{sym_key}] Предыдущий сигнал зафиксирован: {saved_states[sym_key]}")

    while True:
        for sym_key, cfg in INSTRUMENTS.items():
            try:
                df = fetch_closed_candles(cfg["ticker"])
                if df is not None:
                    sigs = detect_immediate_signals(df, cfg)
                    if sigs:
                        latest = sigs[-1]
                        last_sent = saved_states.get(sym_key)
                        if latest["signal_ts"] != last_sent:
                            saved_states[sym_key] = latest["signal_ts"]
                            save_symbol_state(sym_key, latest["signal_ts"])

                            msg = (
                                f"{latest['side']}\n"
                                f"<b>Инструмент:</b> {cfg['name']} (M5)\n"
                                f"<b>Время сигнала (Ташкент):</b> <code>{latest['signal_ts']}</code>\n"
                                f"<b>Текущая цена (Close):</b> <code>{latest['cur_price']:.2f}</code>\n"
                                f"<b>Экстремум разворота ({latest['pivot_ts']}):</b> <code>{latest['pivot']:.2f}</code>\n"
                                f"<b>Ориентир SL:</b> <code>{latest['sl']:.2f}</code> | <b>TP:</b> <code>{latest['tp']:.2f}</code>"
                            )
                            print(f"\n[МОМЕНТАЛЬНЫЙ СИГНАЛ] {cfg['name']}:")
                            print(msg)
                            send_telegram(msg)
            except Exception as e:
                print(f"Ошибка проверки {sym_key}: {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=monitor_loop, daemon=True)
    bot_thread.start()

    run_web_server()

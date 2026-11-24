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

# ═══════════════════════════════════════════════════════════════════
# 1. КОНФИГУРАЦИЯ TELEGRAM И ДАННЫХ
# ═══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9040646501:AAHyg3y78jRDX8D_SVuWdKxp0ZGXCzqwedo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7037914269")

SYMBOL = "GC=F"
INTERVAL = "5m"
PERIOD = "5d"
POLL_INTERVAL_SEC = 20
STATE_FILE = "last_circle.json"

# Параметры индикатора Gold Swing v7
ATR_LEN = 20
SWING_K = 4.0
COOLDOWN = 15
USE_ADX = True
ADX_LEN = 14
ADX_MIN = 20.0
USE_DI = True
ENTRY_N = 8
SL_BUF = 0.5
PIP_SIZE = 0.1
EXIT_PIPS = 80


# ═══════════════════════════════════════════════════════════════════
# 2. МИНИ-ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО ТАРИФА (WEB SERVICE)
# ═══════════════════════════════════════════════════════════════════
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Gold Swing v7 Monitor is Running 24/7!")

    def log_message(self, format, *args):
        pass  # Отключаем спам в логи от пингов


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════════
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════
def load_last_sent_timestamp() -> str | None:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("last_bar_ts")
        except Exception:
            return None
    return None


def save_last_sent_timestamp(bar_ts: str):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_bar_ts": bar_ts}, f)
    except Exception as e:
        print("Ошибка сохранения состояния:", e)


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("Ошибка отправки Telegram:", e)


def compute_indicators(df: pd.DataFrame):
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / ATR_LEN, adjust=False).mean()

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_rma = tr.ewm(alpha=1.0 / ADX_LEN, adjust=False).mean()
    plus_dm_rma = pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / ADX_LEN, adjust=False).mean()
    minus_dm_rma = pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / ADX_LEN, adjust=False).mean()

    plus_di = 100.0 * (plus_dm_rma / tr_rma.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_rma / tr_rma.replace(0, np.nan))

    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1.0 / ADX_LEN, adjust=False).mean()

    return atr, plus_di, minus_di, adx


def detect_circles(df: pd.DataFrame):
    atr, di_plus, di_minus, adx = compute_indicators(df)

    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)
    dip_arr = di_plus.to_numpy(dtype=float)
    dim_arr = di_minus.to_numpy(dtype=float)
    adx_arr = adx.to_numpy(dtype=float)

    n = len(df)
    if n < max(ATR_LEN, ADX_LEN) + 10:
        return []

    dir_zz = 1
    ext_hi = highs[0]
    ext_lo = lows[0]

    last_sig_idx = -100000
    t_dir = 0
    t_sl = np.nan
    t_tp = np.nan

    p_dir = 0
    p_idx = None
    p_sl = np.nan
    p_piv = np.nan

    circles = []

    for i in range(1, n):
        h, l, c = highs[i], lows[i], closes[i]
        cur_atr = atr_arr[i]
        if np.isnan(cur_atr) or cur_atr <= 0:
            continue

        thr = SWING_K * cur_atr
        sell_sig = False
        buy_sig = False
        sig_price = np.nan

        if dir_zz == 1:
            if h > ext_hi:
                ext_hi = h
            if l <= ext_hi - thr:
                sell_sig = True
                sig_price = ext_hi
                dir_zz = -1
                ext_lo = l
        else:
            if l < ext_lo:
                ext_lo = l
            if h >= ext_lo + thr:
                buy_sig = True
                sig_price = ext_lo
                dir_zz = 1
                ext_hi = h

        adx_ok = (not USE_ADX) or (not np.isnan(adx_arr[i]) and adx_arr[i] >= ADX_MIN)
        spaced = (i - last_sig_idx) >= COOLDOWN
        di_buy = (not USE_DI) or (dip_arr[i] > dim_arr[i])
        di_sell = (not USE_DI) or (dim_arr[i] > dip_arr[i])

        f_sell = sell_sig and adx_ok and spaced and di_sell
        f_buy = buy_sig and adx_ok and spaced and di_buy

        if f_sell or f_buy:
            last_sig_idx = i

        if p_dir == 1 and l < p_piv:
            p_dir = 0
        if p_dir == -1 and h > p_piv:
            p_dir = 0

        hit_sl = t_dir != 0 and ((t_dir == 1 and l <= t_sl) or (t_dir == -1 and h >= t_sl))
        hit_tp = t_dir != 0 and (not hit_sl) and ((t_dir == 1 and h >= t_tp) or (t_dir == -1 and l <= t_tp))
        hit_rev = t_dir != 0 and (not hit_sl) and (not hit_tp) and ((t_dir == 1 and f_sell) or (t_dir == -1 and f_buy))
        if hit_sl or hit_tp or hit_rev:
            t_dir = 0

        if f_buy or f_sell:
            p_dir = 1 if f_buy else -1
            p_idx = i + ENTRY_N
            p_piv = sig_price
            p_sl = (sig_price - SL_BUF * cur_atr) if f_buy else (sig_price + SL_BUF * cur_atr)

        if p_dir != 0 and i == p_idx:
            if t_dir == 0:
                t_dir = p_dir
                t_sl = p_sl
                t_tp = c + p_dir * EXIT_PIPS * PIP_SIZE
                circles.append({
                    "bar_ts": str(df.index[i]),
                    "side": "🟢 КРУГ BUY" if p_dir == 1 else "🔴 КРУГ SELL",
                    "entry": c,
                    "pivot": p_piv,
                    "sl": t_sl,
                    "tp": t_tp,
                })
            p_dir = 0

    return circles


def fetch_closed_candles() -> pd.DataFrame | None:
    try:
        raw = yf.download(tickers=SYMBOL, period=PERIOD, interval=INTERVAL, progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.dropna(subset=["Open", "High", "Low", "Close"])
        if len(raw) < 60:
            return None
        return raw.iloc[:-1].copy()
    except Exception as e:
        print(f"Ошибка загрузки данных: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# 4. ЦИКЛ МОНИТОРИНГА
# ═══════════════════════════════════════════════════════════════════
def monitor_loop():
    print("Запуск мониторинга Gold Swing v7...")
    start_msg = (
        f"🚀 <b>Монитор Gold Swing v7 запущен 24/7!</b>\n"
        f"<b>Инструмент:</b> Золото ({SYMBOL}) M5\n"
        f"<b>Режим:</b> Облачный (Render Free)"
    )
    send_telegram(start_msg)

    last_sent_ts = load_last_sent_timestamp()
    initial_df = fetch_closed_candles()
    if initial_df is not None and last_sent_ts is None:
        initial_circles = detect_circles(initial_df)
        if initial_circles:
            last_sent_ts = initial_circles[-1]["bar_ts"]
            save_last_sent_timestamp(last_sent_ts)

    while True:
        try:
            df = fetch_closed_candles()
            if df is not None:
                circles = detect_circles(df)
                if circles:
                    latest = circles[-1]
                    if latest["bar_ts"] != last_sent_ts:
                        last_sent_ts = latest["bar_ts"]
                        save_last_sent_timestamp(last_sent_ts)

                        msg = (
                            f"{latest['side']}\n"
                            f"<b>Инструмент:</b> XAUUSD (M5)\n"
                            f"<b>Время закрытия:</b> <code>{latest['bar_ts']}</code>\n"
                            f"<b>Вход (Close):</b> <code>{latest['entry']:.2f}</code>\n"
                            f"<b>Круг (Пивот):</b> <code>{latest['pivot']:.2f}</code>\n"
                            f"<b>Ориентир SL:</b> <code>{latest['sl']:.2f}</code> | <b>TP:</b> <code>{latest['tp']:.2f}</code>"
                        )
                        send_telegram(msg)
        except Exception as e:
            print("Ошибка цикла:", e)

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=monitor_loop, daemon=True)
    bot_thread.start()

    run_web_server()

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

# Ташкентский часовой пояс (UTC+5)
TIMEZONE_TASHKENT = pytz.timezone("Asia/Tashkent")

INTERVAL = "5m"
PERIOD = "5d"
POLL_INTERVAL_SEC = 20
STATE_FILE = "last_circle.json"

# ═══════════════════════════════════════════════════════════════════
# 2. СПИСОК ИНСТРУМЕНТОВ И ИХ ПАРАМЕТРЫ (КАК В PINE SCRIPT v6)
# ═══════════════════════════════════════════════════════════════════
# Золото (GC=F), Биткоин (BTC-USD), Индекс Доу-Джонса / US30 (YM=F)
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
        self.wfile.write(b"Gold Swing v7 Multi-Pair Monitor (XAUUSD, BTCUSD, US30) is Active 24/7!")

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


# ═══════════════════════════════════════════════════════════════════
# 4. СОХРАНЕНИЕ И ОТПРАВКА СОСТОЯНИЯ
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
# 5. РАСЧЕТ ИНДИКАТОРОВ (СТРОГО КАК В PINE SCRIPT v6)
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

    # Wilder's RMA (Pine Script ta.atr и ta.rma)
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
# 6. ЛОГИКА СИГНАЛА «КРУГ» (GOLD SWING v7 — 1 В 1)
# ═══════════════════════════════════════════════════════════════════
def detect_circles(df: pd.DataFrame, cfg: dict):
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
    t_dir = 0
    t_sl = np.nan
    t_tp = np.nan

    p_dir = 0
    p_bar = None
    p_sl = np.nan
    p_piv = np.nan
    p_piv_b = None

    circles = []

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

        # ───── ZigZag ─────
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

        # ───── Фильтры ─────
        adx_ok = (not cfg["useAdx"]) or (not np.isnan(adx_arr[i]) and adx_arr[i] >= cfg["adxMin"])
        spaced = (i - last_sig_idx) >= cfg["cooldown"]
        di_buy = (not cfg["useDi"]) or (dip_arr[i] > dim_arr[i])
        di_sell = (not cfg["useDi"]) or (dim_arr[i] > dip_arr[i])

        f_sell = sell_sig and adx_ok and spaced and di_sell
        f_buy = buy_sig and adx_ok and spaced and di_buy

        if f_sell or f_buy:
            last_sig_idx = i

        # 0) Отмена ожидания: до входа цена пробила точку разворота
        if p_dir == 1 and l < p_piv:
            p_dir = 0
        if p_dir == -1 and h > p_piv:
            p_dir = 0

        # 1) Выход: стоп, тейк или новый принятый разворот
        hit_sl = t_dir != 0 and ((t_dir == 1 and l <= t_sl) or (t_dir == -1 and h >= t_sl))
        hit_tp = t_dir != 0 and (not hit_sl) and ((t_dir == 1 and h >= t_tp) or (t_dir == -1 and l <= t_tp))
        hit_rev = t_dir != 0 and (not hit_sl) and (not hit_tp) and ((t_dir == 1 and f_sell) or (t_dir == -1 and f_buy))
        if hit_sl or hit_tp or hit_rev:
            t_dir = 0

        # 2) Принятый разворот ставит ожидание входа через entryN баров
        if f_buy or f_sell:
            p_dir = 1 if f_buy else -1
            p_bar = i + cfg["entryN"]
            p_piv = sig_price
            p_piv_b = sig_bar
            p_sl = (sig_price - cfg["slBuf"] * cur_atr) if f_buy else (sig_price + cfg["slBuf"] * cur_atr)

        # 3) Вход на бар p_bar: только если ожидание не отменено и позиции нет
        if p_dir != 0 and i == p_bar:
            if t_dir == 0:
                t_dir = p_dir
                t_sl = p_sl
                t_tp = c + p_dir * cfg["exitPips"] * cfg["pipSize"]

                # Переводим время бара входа и бара пивота в Ташкент (UTC+5)
                entry_ts = df.index[i].tz_convert(TIMEZONE_TASHKENT).strftime("%Y-%m-%d %H:%M")
                pivot_ts = df.index[p_piv_b].tz_convert(TIMEZONE_TASHKENT).strftime("%H:%M") if p_piv_b is not None else ""

                circles.append({
                    "entry_ts": entry_ts,
                    "pivot_ts": pivot_ts,
                    "side": "🟢 КРУГ BUY" if p_dir == 1 else "🔴 КРУГ SELL",
                    "entry": c,
                    "pivot": p_piv,
                    "sl": t_sl,
                    "tp": t_tp,
                })
            p_dir = 0

    return circles


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
# 8. ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА 3-Х ПАР
# ═══════════════════════════════════════════════════════════════════
def monitor_loop():
    print("Запуск мультипарного мониторинга Gold Swing v7...")
    start_msg = (
        "🚀 <b>Мультипарный монитор Gold Swing v7 запущен!</b>\n\n"
        "📊 <b>Отслеживаемые инструменты (M5):</b>\n"
        "• 🟡 <b>Золото:</b> XAUUSD (GC=F)\n"
        "• 🟠 <b>Биткоин:</b> BTCUSD\n"
        "• 🔵 <b>Индекс:</b> US30 (Dow Jones)\n\n"
        "🕒 <b>Часовой пояс:</b> Ташкент (UTC+5)\n"
        "🎯 <b>Логика:</b> Точно по Pine Script v6 (сигнал КРУГ на баре входа)"
    )
    send_telegram(start_msg)

    saved_states = load_all_states()

    # Фиксируем последние исторические круги при старте для каждой пары
    for sym_key, cfg in INSTRUMENTS.items():
        if sym_key not in saved_states:
            df = fetch_closed_candles(cfg["ticker"])
            if df is not None:
                circles = detect_circles(df, cfg)
                if circles:
                    saved_states[sym_key] = circles[-1]["entry_ts"]
                    save_symbol_state(sym_key, circles[-1]["entry_ts"])
                    print(f"[{sym_key}] Зафиксирован прошлый сигнал: {saved_states[sym_key]}")

    while True:
        for sym_key, cfg in INSTRUMENTS.items():
            try:
                df = fetch_closed_candles(cfg["ticker"])
                if df is not None:
                    circles = detect_circles(df, cfg)
                    if circles:
                        latest = circles[-1]
                        last_sent = saved_states.get(sym_key)
                        if latest["entry_ts"] != last_sent:
                            saved_states[sym_key] = latest["entry_ts"]
                            save_symbol_state(sym_key, latest["entry_ts"])

                            msg = (
                                f"{latest['side']}\n"
                                f"<b>Инструмент:</b> {cfg['name']} (M5)\n"
                                f"<b>Время сигнала (Ташкент):</b> <code>{latest['entry_ts']}</code>\n"
                                f"<b>Вход (Close):</b> <code>{latest['entry']:.2f}</code>\n"
                                f"<b>Кружок на пивоте ({latest['pivot_ts']}):</b> <code>{latest['pivot']:.2f}</code>\n"
                                f"<b>Ориентир SL:</b> <code>{latest['sl']:.2f}</code> | <b>TP:</b> <code>{latest['tp']:.2f}</code>"
                            )
                            print(f"\n[СИГНАЛ] {cfg['name']}:")
                            print(msg)
                            send_telegram(msg)
            except Exception as e:
                print(f"Ошибка проверки {sym_key}: {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=monitor_loop, daemon=True)
    bot_thread.start()

    run_web_server()

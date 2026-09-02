import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import requests
import json
import os
import time
from io import BytesIO

# ============================================================
# PAGE CONFIG & RESPONSIVE DARK THEME
# ============================================================

st.set_page_config(
    page_title="BNF Swing Radar Pro & Nifty Tactical Desk",
    layout="wide",
    page_icon="⚡"
)

st.markdown("""
<style>
    div[data-testid="stVerticalBlockBorderWrapper"] {
        min-height: 420px !important;
        display: flex !important;
        flex-direction: column !important;
        justify-content: space-between !important;
        background-color: #111827 !important;
        border: 1px solid #1f2937 !important;
        border-radius: 12px !important;
        padding: 16px !important;
    }
    .stock-title {
        font-size: 1.15rem;
        font-weight: 700;
        color: #f9fafb;
        height: 46px;
        display: flex;
        align-items: center;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        margin-bottom: 2px;
    }
    .metric-container {
        background-color: #1f2937;
        border-radius: 8px;
        padding: 10px;
        margin: 8px 0;
        font-size: 0.88rem;
    }
    .scenario-card {
        background-color: #1e293b;
        border-left: 4px solid #3b82f6;
        border-radius: 8px;
        padding: 14px;
        margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# ============================================================
# TELEGRAM INTEGRATION (SAFE SECRETS FALLBACK)
# ============================================================

try:
    TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "8851317745:AAGMTSvVgpNPyKXQDd2dyvnAJtJdy0wVQJY")
    TELEGRAM_CHAT_ID = st.secrets.get("TELEGRAM_CHAT_ID", "8399631067")
except Exception:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8851317745:AAGMTSvVgpNPyKXQDd2dyvnAJtJdy0wVQJY")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8399631067")

def send_telegram_msg(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False

# ============================================================
# PORTFOLIO PERSISTENCE
# ============================================================

PORTFOLIO_FILE = "portfolio.json"

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_portfolio(data):
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

if "portfolio" not in st.session_state:
    st.session_state.portfolio = load_portfolio()

# ============================================================
# MATHEMATICALLY EXACT WILDER'S RSI
# ============================================================

def calc_wilder_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    avg_gain = gain.ewm(alpha=1.0/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, adjust=False).mean()
    
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ============================================================
# NSE UNIVERSE ENGINE (SEGMENTED NIFTY 500)
# ============================================================

NSE_BASE_URL = "https://archives.nseindia.com/content/indices/"
NSE_FILES = {
    "Large Cap": "ind_nifty100list.csv",
    "Mid Cap": "ind_niftymidcap150list.csv",
    "Small Cap": "ind_niftysmallcap250list.csv"
}

@st.cache_data(ttl=86400)
def load_nifty500_universe():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    segment_lists = {}

    for cap_type, filename in NSE_FILES.items():
        try:
            url = NSE_BASE_URL + filename
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(BytesIO(resp.content))
            if "Symbol" not in df.columns:
                segment_lists[cap_type] = []
                continue
            symbols = df["Symbol"].dropna().astype(str).str.strip().tolist()
            segment_lists[cap_type] = [f"{s}.NS" for s in list(dict.fromkeys(symbols))]
        except Exception:
            segment_lists[cap_type] = []

    large_caps = segment_lists.get("Large Cap", [])
    mid_caps = segment_lists.get("Mid Cap", [])
    small_caps = segment_lists.get("Small Cap", [])

    if len(large_caps) < 95 or len(mid_caps) < 140 or len(small_caps) < 240:
        try:
            fallback_url = "https://raw.githubusercontent.com/datasets-stocks/nifty500/main/nifty500.csv"
            fallback_resp = requests.get(fallback_url, timeout=12)
            fallback_resp.raise_for_status()
            fallback_df = pd.read_csv(BytesIO(fallback_resp.content))
            if "Symbol" in fallback_df.columns:
                all_symbols = fallback_df["Symbol"].dropna().astype(str).str.strip().tolist()
                all_tickers = [f"{s}.NS" for s in list(dict.fromkeys(all_symbols))]
                classified = set(large_caps + mid_caps + small_caps)
                unclassified = [t for t in all_tickers if t not in classified]
                if not large_caps and not mid_caps and not small_caps:
                    unclassified = all_tickers
            else:
                unclassified = []
        except Exception:
            unclassified = []
    else:
        unclassified = []

    return (
        list(dict.fromkeys(large_caps)),
        list(dict.fromkeys(mid_caps)),
        list(dict.fromkeys(small_caps)),
        list(dict.fromkeys(unclassified))
    )

# ============================================================
# TAB 1: BNF SCANNER ENGINE (WILDER RSI + CORRIDORS)
# ============================================================

@st.cache_data(ttl=600)
def scan_classical_bnf(ticker_list, cap_type):
    if not ticker_list:
        return pd.DataFrame()

    if cap_type == "Large Cap":
        min_disp, max_disp, extreme_disp = -7.0, -20.0, -12.0
    elif cap_type == "Mid Cap":
        min_disp, max_disp, extreme_disp = -10.0, -25.0, -16.0
    elif cap_type == "Small Cap":
        min_disp, max_disp, extreme_disp = -15.0, -35.0, -25.0
    else:
        min_disp, max_disp, extreme_disp = -10.0, -25.0, -16.0

    records = []
    chunk_size = 50

    for i in range(0, len(ticker_list), chunk_size):
        chunk = ticker_list[i:i + chunk_size]
        try:
            raw = yf.download(chunk, period="6mo", interval="1d", group_by="ticker", progress=False, threads=True, auto_adjust=False)
        except Exception:
            continue

        for ticker in chunk:
            try:
                if len(chunk) > 1:
                    if not isinstance(raw.columns, pd.MultiIndex) or ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker].copy()
                else:
                    df = raw.copy()
                    if isinstance(df.columns, pd.MultiIndex):
                        try:
                            df.columns = df.columns.get_level_values(-1)
                        except Exception:
                            pass

                if df is None or df.empty or not all(c in df.columns for c in ["Close", "Volume"]):
                    continue

                df = df[["Close", "Volume"]].dropna(subset=["Close", "Volume"]).copy()
                if len(df) < 30:
                    continue

                close = float(df["Close"].iloc[-1])
                if close <= 0:
                    continue

                ema_25 = float(df["Close"].ewm(span=25, adjust=False).mean().iloc[-1])
                if ema_25 <= 0:
                    continue

                disparity = ((close - ema_25) / ema_25) * 100
                if not (max_disp <= disparity <= min_disp):
                    continue

                rsi_series = calc_wilder_rsi(df["Close"], period=14)
                rsi = float(rsi_series.iloc[-1])
                if np.isnan(rsi) or rsi > 35:
                    continue

                vol_avg = float(df["Volume"].rolling(20).mean().iloc[-1])
                vol_now = float(df["Volume"].iloc[-1])
                if vol_avg <= 0:
                    continue

                vol_spike = vol_now / vol_avg
                score = 50
                if disparity <= extreme_disp: score += 25
                if rsi <= 25: score += 15
                if vol_spike >= 1.3: score += 10

                records.append({
                    "Symbol": ticker.replace(".NS", ""),
                    "Raw_Ticker": ticker,
                    "Cap": cap_type,
                    "Price (₹)": round(close, 2),
                    "EMA-25 (₹)": round(ema_25, 2),
                    "Disparity %": round(disparity, 2),
                    "RSI": round(rsi, 1),
                    "Vol Spike": f"{round(vol_spike, 1)}x",
                    "BNF Score": score,
                    "Target (+5%)": round(close * 1.05, 2),
                    "SL (-2.5%)": round(close * 0.975, 2),
                    "Horizon": "2-4 Trading Days"
                })
            except Exception:
                continue

    if records:
        return pd.DataFrame(records).sort_values(by=["BNF Score", "Disparity %"], ascending=[False, True]).reset_index(drop=True)
    return pd.DataFrame()

def trading_days_held(buy_date, current_date):
    if buy_date >= current_date:
        return 0
    business_days = pd.bdate_range(start=buy_date, end=current_date)
    return max(0, len(business_days) - 1)

# ============================================================
# TAB 3: RIGOROUS NIFTY TECHNICAL & PRICE ACTION MATRIX
# ============================================================

@st.cache_data(ttl=300)
def fetch_nifty_technicals():
    df = None
    for ticker in ["^NSEI", "NIFTYBEES.NS"]:
        try:
            raw = yf.download(ticker, period="2y", interval="1d", progress=False, auto_adjust=False)
            if raw is None or raw.empty:
                continue
            
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
                
            temp_df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
            if len(temp_df) >= 100:
                df = temp_df
                break
        except Exception:
            continue

    if df is None or len(df) < 50:
        return None

    try:
        prev_h = float(df["High"].iloc[-1])
        prev_l = float(df["Low"].iloc[-1])
        prev_c = float(df["Close"].iloc[-1])
        prev_o = float(df["Open"].iloc[-1])
        prev_prev_c = float(df["Close"].iloc[-2])

        candle_range = max(prev_h - prev_l, 0.01)
        body_size = abs(prev_c - prev_o)
        body_pct = (body_size / candle_range * 100)
        upper_wick = prev_h - max(prev_o, prev_c)
        lower_wick = min(prev_o, prev_c) - prev_l
        upper_wick_pct = (upper_wick / candle_range * 100)
        lower_wick_pct = (lower_wick / candle_range * 100)
        close_pos_pct = ((prev_c - prev_l) / candle_range * 100)
        gap_pct = ((prev_o - prev_prev_c) / prev_prev_c * 100)

        vol_latest = float(df["Volume"].iloc[-1])
        vol_sma20 = float(df["Volume"].rolling(20, min_periods=5).mean().iloc[-1])
        vol_ratio = (vol_latest / vol_sma20) if vol_sma20 > 0 else 1.0

        sma_20 = float(df["Close"].rolling(20, min_periods=10).mean().iloc[-1])
        sma_50 = float(df["Close"].rolling(50, min_periods=20).mean().iloc[-1])
        sma_100 = float(df["Close"].rolling(100, min_periods=30).mean().iloc[-1])
        
        if len(df) >= 200:
            sma_200 = float(df["Close"].rolling(200).mean().iloc[-1])
        else:
            sma_200 = float(df["Close"].rolling(len(df)).mean().iloc[-1])

        ema_20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema_50 = float(df["Close"].ewm(span=50, adjust=False).mean().iloc[-1])

        sma_20_prev = float(df["Close"].rolling(20, min_periods=5).mean().iloc[-5])
        sma_50_prev = float(df["Close"].rolling(50, min_periods=5).mean().iloc[-5])
        sma_20_slope = "↗ Bullish" if sma_20 >= sma_20_prev else "↘ Bearish"
        sma_50_slope = "↗ Bullish" if sma_50 >= sma_50_prev else "↘ Bearish"

        rsi_series = calc_wilder_rsi(df["Close"], period=14)
        rsi = float(rsi_series.iloc[-1])

        ema_12 = df["Close"].ewm(span=12, adjust=False).mean()
        ema_26 = df["Close"].ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        sig_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_val = float(macd_line.iloc[-1])
        sig_val = float(sig_line.iloc[-1])
        hist_curr = macd_val - sig_val
        hist_prev = float((macd_line - sig_line).iloc[-2])
        macd_expansion = hist_curr > hist_prev

        tr1 = df["High"] - df["Low"]
        tr2 = (df["High"] - df["Close"].shift(1)).abs()
        tr3 = (df["Low"] - df["Close"].shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = float(tr.ewm(alpha=1.0/14, adjust=False).mean().iloc[-1])

        up_move = df["High"].diff()
        down_move = -df["Low"].diff()
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        tr_smooth = tr.ewm(alpha=1.0/14, adjust=False).mean()
        plus_di = 100 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1.0/14, adjust=False).mean() / tr_smooth.replace(0, np.nan))
        minus_di = 100 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1.0/14, adjust=False).mean() / tr_smooth.replace(0, np.nan))
        
        sum_di = (plus_di + minus_di).replace(0, np.nan)
        dx = 100 * ((plus_di - minus_di).abs() / sum_di)
        adx_series = dx.ewm(alpha=1.0/14, adjust=False).mean()
        adx = float(adx_series.iloc[-1]) if not np.isnan(adx_series.iloc[-1]) else 20.0
        plus_di_val = float(plus_di.iloc[-1]) if not np.isnan(plus_di.iloc[-1]) else 25.0
        minus_di_val = float(minus_di.iloc[-1]) if not np.isnan(minus_di.iloc[-1]) else 25.0

        pivot = (prev_h + prev_l + prev_c) / 3.0
        bc = (prev_h + prev_l) / 2.0
        tc = (pivot - bc) + pivot
        r1 = (2 * pivot) - prev_l
        s1 = (2 * pivot) - prev_h
        r2 = pivot + (prev_h - prev_l)
        s2 = pivot - (prev_h - prev_l)

        return {
            "Open": prev_o, "High": prev_h, "Low": prev_l, "Close": prev_c,
            "PDH": prev_h, "PDL": prev_l, "PDC": prev_c, "Gap_Pct": gap_pct,
            "Candle_Range": candle_range, "Body_Pct": body_pct,
            "Upper_Wick_Pct": upper_wick_pct, "Lower_Wick_Pct": lower_wick_pct,
            "Close_Pos_Pct": close_pos_pct,
            "Volume": vol_latest, "Vol_Ratio": vol_ratio,
            "SMA_20": sma_20, "SMA_50": sma_50, "SMA_100": sma_100, "SMA_200": sma_200,
            "EMA_20": ema_20, "EMA_50": ema_50,
            "SMA_20_Slope": sma_20_slope, "SMA_50_Slope": sma_50_slope,
            "RSI": rsi, "MACD": macd_val, "Signal": sig_val, "Hist": hist_curr,
            "MACD_Expansion": macd_expansion,
            "ADX": adx, "Plus_DI": plus_di_val, "Minus_DI": minus_di_val, "ATR": atr,
            "Pivot": pivot, "CPR_TC": max(tc, bc), "CPR_BC": min(tc, bc),
            "R1": r1, "S1": s1, "R2": r2, "S2": s2
        }
    except Exception:
        return None

# ============================================================
# LIVE NSE DERIVATIVES INGESTION (DIAGNOSTIC & RESILIENT ENGINE)
# ============================================================

@st.cache_data(ttl=300)
def fetch_nse_option_chain_live():
    # Diagnostic state tracking
    diag_messages = []
    
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive"
    }

    warmup_headers = dict(base_headers)
    warmup_headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1"
    })

    api_headers = dict(base_headers)
    api_headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/option-chain",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    })

    for attempt in range(1, 3):
        session = requests.Session()
        session.headers.update(warmup_headers)
        
        try:
            # STEP 1: Visit homepage to initiate Akamai cookies
            r1 = session.get("https://www.nseindia.com", timeout=10)
            if r1.status_code != 200:
                diag_messages.append(f"Attempt {attempt}: Homepage returned HTTP {r1.status_code}")
                session.close()
                time.sleep(1.0)
                continue
                
            time.sleep(0.5)

            # STEP 2: Visit option-chain page to obtain route permissions
            r2 = session.get("https://www.nseindia.com/option-chain", headers=warmup_headers, timeout=10)
            if r2.status_code != 200:
                diag_messages.append(f"Attempt {attempt}: Option-chain landing returned HTTP {r2.status_code}")
                session.close()
                time.sleep(1.0)
                continue

            cookie_count = len(session.cookies)
            if cookie_count == 0:
                diag_messages.append(f"Attempt {attempt}: Zero cookies received after warmup")
                session.close()
                time.sleep(1.0)
                continue

            time.sleep(0.8)

            # STEP 3: Request live option chain JSON
            resp = session.get(url, headers=api_headers, timeout=12)
            final_code = resp.status_code
            content_type = resp.headers.get("Content-Type", "")

            if final_code != 200:
                diag_messages.append(f"Attempt {attempt}: API returned HTTP {final_code} ({resp.reason})")
                session.close()
                time.sleep(1.0)
                continue

            # Verify JSON payload
            try:
                payload = resp.json()
            except Exception as e:
                snippet = resp.text[:120].strip().replace("\n", " ")
                diag_messages.append(f"Attempt {attempt}: JSON parse failed. Content-Type: {content_type}. Snippet: {snippet}")
                session.close()
                time.sleep(1.0)
                continue

            if not isinstance(payload, dict):
                diag_messages.append(f"Attempt {attempt}: Root payload is not a JSON object")
                session.close()
                continue

            records_obj = payload.get("records")
            if not isinstance(records_obj, dict):
                diag_messages.append(f"Attempt {attempt}: Key 'records' missing from NSE JSON")
                session.close()
                continue

            records = records_obj.get("data", [])
            expiry_list = records_obj.get("expiryDates", [])
            spot = records_obj.get("underlyingValue")

            if not records:
                diag_messages.append(f"Attempt {attempt}: 'records.data' is empty")
                session.close()
                continue

            if not expiry_list:
                diag_messages.append(f"Attempt {attempt}: 'records.expiryDates' is empty")
                session.close()
                continue

            # Spot resolution fallback
            if spot is None or float(spot or 0) <= 0:
                for item in records:
                    if isinstance(item, dict):
                        ce = item.get("CE") or {}
                        pe = item.get("PE") or {}
                        cand_spot = ce.get("underlyingValue") or pe.get("underlyingValue") or item.get("underlyingValue")
                        if cand_spot and float(cand_spot) > 0:
                            spot = cand_spot
                            break

            # Nearest active expiry resolution
            today_dt = datetime.date.today()
            valid_expiries = []
            for exp_str in expiry_list:
                try:
                    exp_dt = datetime.datetime.strptime(exp_str, "%d-%b-%Y").date()
                    if exp_dt >= today_dt:
                        valid_expiries.append((exp_dt, exp_str))
                except Exception:
                    pass

            if valid_expiries:
                valid_expiries.sort(key=lambda x: x[0])
                near_expiry = valid_expiries[0][1]
            else:
                near_expiry = expiry_list[0]

            # Parse strikes for selected expiry
            chain = []
            call_oi_map, put_oi_map = {}, {}
            tot_ce_oi = tot_pe_oi = 0
            tot_ce_chg = tot_pe_chg = 0

            for item in records:
                if not isinstance(item, dict) or item.get("expiryDate") != near_expiry:
                    continue

                strike = item.get("strikePrice")
                if strike is None:
                    continue

                ce = item.get("CE") or {}
                pe = item.get("PE") or {}

                c_oi = float(ce.get("openInterest") or 0)
                p_oi = float(pe.get("openInterest") or 0)
                c_chg = float(ce.get("changeinOpenInterest") or 0)
                p_chg = float(pe.get("changeinOpenInterest") or 0)
                c_price_chg = float(ce.get("change") or 0)
                p_price_chg = float(pe.get("change") or 0)

                call_oi_map[strike] = c_oi
                put_oi_map[strike] = p_oi
                tot_ce_oi += c_oi
                tot_pe_oi += p_oi
                tot_ce_chg += c_chg
                tot_pe_chg += p_chg

                chain.append({
                    "Strike": strike,
                    "CE_OI": c_oi,
                    "CE_DeltaOI": c_chg,
                    "CE_PriceChange": c_price_chg,
                    "PE_OI": p_oi,
                    "PE_DeltaOI": p_chg,
                    "PE_PriceChange": p_price_chg
                })

            if not chain or tot_ce_oi <= 0:
                diag_messages.append(f"Attempt {attempt}: Zero call open interest found for expiry {near_expiry}")
                session.close()
                continue

            chain_df = pd.DataFrame(chain).sort_values("Strike").reset_index(drop=True)
            pcr = round(tot_pe_oi / tot_ce_oi, 2)
            max_c_strike = max(call_oi_map, key=call_oi_map.get) if call_oi_map else 0
            max_p_strike = max(put_oi_map, key=put_oi_map.get) if put_oi_map else 0

            # Max Pain calculation
            loss_map = {}
            strikes = sorted(set(call_oi_map) | set(put_oi_map))
            for target_s in strikes:
                total_loss = 0.0
                for strike, oi in call_oi_map.items():
                    if strike < target_s:
                        total_loss += (target_s - strike) * oi
                for strike, oi in put_oi_map.items():
                    if strike > target_s:
                        total_loss += (strike - target_s) * oi
                loss_map[target_s] = total_loss
            max_pain = min(loss_map, key=loss_map.get) if loss_map else 0

            # Spot corridor & Writing analysis (±5%)
            if spot is None or float(spot) <= 0:
                spot = float(chain_df["Strike"].median())
            spot = float(spot)
            
            window = chain_df[(chain_df["Strike"] >= spot * 0.95) & (chain_df["Strike"] <= spot * 1.05)].copy()
            if window.empty:
                window = chain_df.copy()

            call_writing = window[(window["CE_DeltaOI"] > 0) & (window["CE_PriceChange"] <= 0)].copy()
            put_writing = window[(window["PE_DeltaOI"] > 0) & (window["PE_PriceChange"] <= 0)].copy()

            call_write_oi = float(call_writing["CE_DeltaOI"].clip(lower=0).sum())
            put_write_oi = float(put_writing["PE_DeltaOI"].clip(lower=0).sum())

            strongest_call_write = int(call_writing.loc[call_writing["CE_DeltaOI"].idxmax(), "Strike"]) if not call_writing.empty else 0
            strongest_put_write = int(put_writing.loc[put_writing["PE_DeltaOI"].idxmax(), "Strike"]) if not put_writing.empty else 0

            session.close()
            st.session_state.nse_diag_status = "Connected ✅ (Live NSE Feed)"
            
            return {
                "Available": True,
                "Spot": spot,
                "PCR": pcr,
                "Highest_Call_OI": max_c_strike,
                "Highest_Put_OI": max_p_strike,
                "Max_Pain": max_pain,
                "Total_Call_Chg": tot_ce_chg,
                "Total_Put_Chg": tot_pe_chg,
                "Call_Writing_OI": call_write_oi,
                "Put_Writing_OI": put_write_oi,
                "Strongest_Call_Writing": strongest_call_write,
                "Strongest_Put_Writing": strongest_put_write,
                "Expiry": near_expiry
            }

        except Exception as e:
            diag_messages.append(f"Attempt {attempt} Exception: {str(e)}")
            session.close()
            time.sleep(1.0)
            continue

    # Record diagnosis message in session state for UI inspection
    st.session_state.nse_diag_status = " | ".join(diag_messages) if diag_messages else "Unknown NSE Connection Failure"
    return None

# ============================================================
# MASTER APPLICATION INTERFACE
# ============================================================

st.title("⚡ BNF Terminal & Nifty Strategic Desk")

(large_list, mid_list, small_list, unclass_list) = load_nifty500_universe()
scannable_universe = len(large_list) + len(mid_list) + len(small_list)

st.caption(
    f"Scannable Universe: **{scannable_universe} Stocks** | "
    f"Large: **{len(large_list)}** | Mid: **{len(mid_list)}** | Small: **{len(small_list)}** | "
    f"Unclassified: **{len(unclass_list)}** | Telegram Engine: **Active ✅**"
)

main_tab1, main_tab2, main_tab3 = st.tabs([
    "🔍 BNF Stock Scanner", 
    "💼 Active Tracked Portfolio", 
    "📊 Nifty Next-Day Strategic Analyzer"
])

# ------------------------------------------------------------
# TAB 1: BNF SCANNER
# ------------------------------------------------------------
with main_tab1:
    c1, c2, c3 = st.columns([3, 4, 3])
    with c1:
        seg_choice = st.selectbox(
            "Select Market Segment:",
            ["All (Large + Mid + Small)", "Small Cap (-15% to -35% Disparity)", "Mid Cap (-10% to -25% Disparity)", "Large Cap (-7% to -20% Disparity)"]
        )
    with c2:
        max_price = st.slider("Max Stock Price Filter (₹):", 10, 5000, 3500, 10)
    with c3:
        st.write("")
        st.write("")
        trigger_scan = st.button("🚀 Run BNF Deep Scan", type="primary", use_container_width=True)

    if trigger_scan:
        with st.spinner("Analyzing Nifty 500 with Falling-Knife Corridors & Wilder RSI..."):
            collected = []
            if seg_choice in ["All (Large + Mid + Small)", "Large Cap (-7% to -20% Disparity)"]:
                dfl = scan_classical_bnf(large_list, "Large Cap")
                if not dfl.empty: collected.append(dfl)
            if seg_choice in ["All (Large + Mid + Small)", "Mid Cap (-10% to -25% Disparity)"]:
                dfm = scan_classical_bnf(mid_list, "Mid Cap")
                if not dfm.empty: collected.append(dfm)
            if seg_choice in ["All (Large + Mid + Small)", "Small Cap (-15% to -35% Disparity)"]:
                dfs = scan_classical_bnf(small_list, "Small Cap")
                if not dfs.empty: collected.append(dfs)

            st.session_state.bnf_results = pd.concat(collected, ignore_index=True) if collected else pd.DataFrame()

    if "bnf_results" in st.session_state and not st.session_state.bnf_results.empty:
        df_disp = st.session_state.bnf_results.copy()
        df_disp = df_disp[df_disp["Price (₹)"] <= max_price].copy()
        st.subheader(f"🔥 Classical Reversal Setups Detected ({len(df_disp)})")

        top_picks = df_disp.head(5)
        if len(top_picks) > 0:
            cols = st.columns(len(top_picks))
            for idx, (_, row) in enumerate(top_picks.iterrows()):
                with cols[idx]:
                    with st.container(border=True):
                        st.markdown(f"<div class='stock-title'>#{idx + 1} {row['Symbol']}</div>", unsafe_allow_html=True)
                        st.caption(f"Segment: {row['Cap']}")
                        st.metric("Price", f"₹{row['Price (₹)']}", f"{row['Disparity %']}% vs EMA", delta_color="inverse")
                        st.markdown(f"""
                        <div class='metric-container'>
                            <b>EMA-25:</b> ₹{row['EMA-25 (₹)']}<br>
                            <b>Wilder RSI:</b> {row['RSI']} &nbsp;|&nbsp; <b>Vol:</b> {row['Vol Spike']}<br>
                            🎯 <b>Target:</b> ₹{row['Target (+5%)']}<br>
                            🛡️ <b>SL:</b> ₹{row['SL (-2.5%)']}<br>
                            ⭐ <b>Score:</b> {row['BNF Score']}/100
                        </div>
                        """, unsafe_allow_html=True)

                        if st.button("📥 Mark Bought", key=f"btn_{row['Symbol']}", use_container_width=True):
                            if not any(item["Symbol"] == row["Symbol"] for item in st.session_state.portfolio):
                                entry = {
                                    "Symbol": row["Symbol"], "Raw_Ticker": row["Raw_Ticker"],
                                    "Buy_Price": row["Price (₹)"], "Buy_Date": str(datetime.date.today()),
                                    "Target": row["Target (+5%)"], "StopLoss": row["SL (-2.5%)"], "Max_Days": 4
                                }
                                st.session_state.portfolio.append(entry)
                                save_portfolio(st.session_state.portfolio)
                                alert = (
                                    "🟢 *BNF ENTRY TRIGGER*\n\n"
                                    f"📌 *Stock:* `{row['Symbol']}` ({row['Cap']})\n"
                                    f"💰 *Entry:* ₹{row['Price (₹)']}\n"
                                    f"🎯 *Target (+5%):* ₹{row['Target (+5%)']}\n"
                                    f"🛡️ *SL (-2.5%):* ₹{row['SL (-2.5%)']}\n"
                                    f"📉 *Disparity:* {row['Disparity %']}% | *RSI:* {row['RSI']}\n"
                                    "⏳ *Time Stop:* 4 Trading Days"
                                )
                                send_telegram_msg(alert)
                                st.toast(f"{row['Symbol']} added & alerted!", icon="✅")
                                st.rerun()

        st.markdown("---")
        st.dataframe(df_disp.drop(columns=["Raw_Ticker"], errors="ignore"), use_container_width=True, hide_index=True)
    elif "bnf_results" in st.session_state:
        st.warning("⚠️ No stock satisfies classical BNF panic conditions within the safe corridor.")

# ------------------------------------------------------------
# TAB 2: PORTFOLIO TRACKER
# ------------------------------------------------------------
with main_tab2:
    st.subheader("🛡️ Active Position Watch & Risk Guard")
    if not st.session_state.portfolio:
        st.info("No active positions tracked.")
    else:
        active_list = []
        for item in st.session_state.portfolio:
            try:
                curr_data = yf.download(item["Raw_Ticker"], period="5d", interval="1d", progress=False, auto_adjust=False)
                if isinstance(curr_data.columns, pd.MultiIndex):
                    curr_data.columns = curr_data.columns.get_level_values(-1)
                curr_price = round(float(curr_data["Close"].dropna().iloc[-1]), 2)
            except Exception:
                curr_price = item["Buy_Price"]

            pnl_pct = round(((curr_price - item["Buy_Price"]) / item["Buy_Price"]) * 100, 2)
            try:
                buy_dt = datetime.datetime.strptime(item["Buy_Date"], "%Y-%m-%d").date()
            except Exception:
                buy_dt = datetime.date.today()

            days_held = trading_days_held(buy_dt, datetime.date.today())

            if curr_price <= item["StopLoss"]: status = "🚨 SL BREACHED (EXIT)"
            elif curr_price >= item["Target"]: status = "🎯 TARGET HIT (PROFIT)"
            elif days_held >= item["Max_Days"]: status = "⏳ TIME STOP (EXIT & ROTATE)"
            else: status = "🟢 HOLDING SAFE"

            active_list.append({
                "Symbol": item["Symbol"], "Buy Price": f"₹{item['Buy_Price']}", "Live Price": f"₹{curr_price}",
                "P&L %": f"{pnl_pct}%", "SL / Target": f"₹{item['StopLoss']} / ₹{item['Target']}",
                "Trading Days": f"{days_held} / {item['Max_Days']}d", "Signal": status
            })

        st.dataframe(pd.DataFrame(active_list), use_container_width=True, hide_index=True)

        dcol1, _ = st.columns([4, 6])
        with dcol1:
            syms = [item["Symbol"] for item in st.session_state.portfolio]
            to_exit = st.selectbox("Select position to close:", syms)
            if st.button("❌ Close Position", use_container_width=True):
                st.session_state.portfolio = [i for i in st.session_state.portfolio if i["Symbol"] != to_exit]
                save_portfolio(st.session_state.portfolio)
                send_telegram_msg(f"⚪ *BNF POSITION CLOSED*\n\n📌 `{to_exit}` removed from tracking.")
                st.toast(f"{to_exit} closed.", icon="ℹ️")
                st.rerun()

# ------------------------------------------------------------
# TAB 3: RIGOROUS NIFTY NEXT-DAY STRATEGIC ANALYZER
# ------------------------------------------------------------
with main_tab3:
    st.subheader("🎯 Nifty 50 Next-Day Tactical Decision Engine")
    
    with st.spinner("Synthesizing Multi-Timeframe Technicals & Live Derivatives..."):
        tech = fetch_nifty_technicals()
        opt = fetch_nse_option_chain_live()

    if tech:
        score = 50.0
        evidence = []

        # 1. Technical Trend (±30)
        if tech["Close"] > tech["EMA_20"]:
            score += 8
            evidence.append(f"Price (₹{round(tech['Close'])}) > 20-EMA [Short-Term Bullish]")
        else:
            score -= 8
            evidence.append("Price below 20-EMA [Short-Term Bearish]")

        if tech["Close"] > tech["SMA_50"]:
            score += 8
            evidence.append(f"Price > 50-SMA (₹{round(tech['SMA_50'])}) [Medium-Term Bullish]")
        else:
            score -= 8
            evidence.append("Price below 50-SMA [Medium-Term Bearish]")

        if tech["Close"] > tech["SMA_200"]:
            score += 6
            evidence.append("Above 200-SMA Structural Bull Regime")
        else:
            score -= 6
            evidence.append("Below 200-SMA Structural Bear Regime")

        if tech["SMA_20_Slope"] == "↗ Bullish":
            score += 4
            evidence.append("20-SMA slope bullish")
        else:
            score -= 4
            evidence.append("20-SMA slope bearish")

        # 2. Price Action (±20)
        if tech["Close_Pos_Pct"] >= 70:
            score += 6
            evidence.append(f"Strong close in top 30% of range ({round(tech['Close_Pos_Pct'])}%)")
        elif tech["Close_Pos_Pct"] <= 30:
            score -= 6
            evidence.append(f"Weak close in bottom 30% of range ({round(tech['Close_Pos_Pct'])}%)")

        if tech["Lower_Wick_Pct"] > 35:
            score += 5
            evidence.append(f"Lower-wick demand absorption ({round(tech['Lower_Wick_Pct'])}%)")
        if tech["Upper_Wick_Pct"] > 35:
            score -= 5
            evidence.append(f"Upper-wick supply rejection ({round(tech['Upper_Wick_Pct'])}%)")

        if tech["Vol_Ratio"] >= 1.15:
            if tech["Close"] > tech["Open"]:
                score += 4
                evidence.append(f"Volume expansion on green day ({round(tech['Vol_Ratio'], 2)}x)")
            elif tech["Close"] < tech["Open"]:
                score -= 4
                evidence.append(f"Volume expansion on red day ({round(tech['Vol_Ratio'], 2)}x)")

        # 3. Momentum & Trend Strength (±25)
        if 50 <= tech["RSI"] <= 65:
            score += 7
            evidence.append(f"Wilder RSI {round(tech['RSI'], 1)} = healthy bullish momentum")
        elif tech["RSI"] > 70:
            score -= 3
            evidence.append(f"RSI {round(tech['RSI'], 1)} = overbought risk")
        elif tech["RSI"] >= 65:
            score += 3
            evidence.append(f"RSI {round(tech['RSI'], 1)} = positive but extended")
        elif tech["RSI"] < 40:
            score -= 7
            evidence.append(f"RSI {round(tech['RSI'], 1)} = bearish weakness")
        else:
            score -= 2
            evidence.append(f"RSI {round(tech['RSI'], 1)} = neutral/weak momentum")

        if tech["Hist"] > 0 and tech["MACD_Expansion"]:
            score += 7
            evidence.append("MACD histogram positive & expanding")
        elif tech["Hist"] > 0:
            score += 3
            evidence.append("MACD positive but slowing")
        elif tech["Hist"] < 0 and tech["MACD_Expansion"]:
            score -= 3
            evidence.append("MACD negative but improving")
        else:
            score -= 7
            evidence.append("MACD histogram negative & weakening")

        if tech["ADX"] >= 20:
            if tech["Plus_DI"] > tech["Minus_DI"]:
                score += 6
                evidence.append(f"ADX {round(tech['ADX'], 1)} with +DI dominance")
            else:
                score -= 6
                evidence.append(f"ADX {round(tech['ADX'], 1)} with -DI dominance")
        else:
            evidence.append(f"ADX {round(tech['ADX'], 1)} = trend strength weak/range")

        # 4. Derivatives (±25) with Dynamic Normalization
        has_derivatives = opt is not None
        if has_derivatives:
            if opt["PCR"] >= 1.2:
                score += 7
                evidence.append(f"PCR {opt['PCR']} = put-side support bias")
            elif opt["PCR"] <= 0.8:
                score -= 7
                evidence.append(f"PCR {opt['PCR']} = call-heavy resistance bias")
            else:
                evidence.append(f"PCR {opt['PCR']} = neutral derivative zone")

            if opt["Put_Writing_OI"] > opt["Call_Writing_OI"] * 1.10:
                score += 7
                evidence.append("Strike-wise put writing pressure exceeds call writing")
            elif opt["Call_Writing_OI"] > opt["Put_Writing_OI"] * 1.10:
                score -= 7
                evidence.append("Strike-wise call writing pressure exceeds put writing")
            else:
                evidence.append("CE/PE writing pressure broadly balanced")

            if tech["Close"] >= opt["Max_Pain"]:
                score += 3
                evidence.append(f"Spot above/at Max Pain ₹{opt['Max_Pain']} [supporting context]")
            else:
                score -= 3
                evidence.append(f"Spot below Max Pain ₹{opt['Max_Pain']} [supporting context]")

            if opt["Strongest_Put_Writing"] and opt["Strongest_Call_Writing"]:
                evidence.append(f"Near-expiry writing zones: Put ₹{opt['Strongest_Put_Writing']} / Call ₹{opt['Strongest_Call_Writing']}")
        else:
            evidence.append("⚠️ Live NSE option chain unavailable. Score shown is Technical + Price Action + Momentum only; no re-scaling applied.")

        score = int(round(max(0.0, min(100.0, score))))

        if score >= 80: bias_label, bias_color = "🟢 STRONG BULLISH", "#22c55e"
        elif score >= 65: bias_label, bias_color = "🟢 BULLISH", "#16a34a"
        elif score >= 55: bias_label, bias_color = "🟢 MILD BULLISH", "#4ade80"
        elif score >= 45: bias_label, bias_color = "⚪ NEUTRAL / RANGEBOUND", "#94a3b8"
        elif score >= 35: bias_label, bias_color = "🔴 MILD BEARISH", "#f87171"
        else: bias_label, bias_color = "🔴 BEARISH", "#ef4444"

        st.caption("Post-market / completed-session analysis • Near-expiry option-chain context when NSE is available")

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Nifty Spot Close", f"₹{round(tech['Close'], 2)}", f"ATR: ±{round(tech['ATR'])} pts")
        k2.metric("Confluence Score", f"{score}/100", bias_label)
        if has_derivatives:
            k3.metric("Derivative PCR", f"{opt['PCR']}", f"Pain: ₹{opt['Max_Pain']}")
            k4.metric("OI Corridor", f"₹{opt['Strongest_Put_Writing'] or opt['Highest_Put_OI']} - ₹{opt['Strongest_Call_Writing'] or opt['Highest_Call_OI']}", f"Near Expiry: {opt['Expiry']}")
        else:
            status_diag = getattr(st.session_state, "nse_diag_status", "NSE Non-Responsive")
            k3.metric("Derivative Status", "Unavailable", status_diag[:28])
            k4.metric("Technical Range", f"₹{round(tech['S1'])} - ₹{round(tech['R1'])}", "Floor Pivots")

        # Diagnostics expandable section if NSE is unreachable
        if not has_derivatives and hasattr(st.session_state, "nse_diag_status"):
            with st.expander("🔍 NSE Option Chain Diagnostic Telemetry", expanded=True):
                st.caption("Detailed connection logs from last live probe:")
                st.code(st.session_state.nse_diag_status)

        st.markdown("---")

        col_left, col_right = st.columns([5, 5])
        with col_left:
            st.subheader("📐 Next-Day Forward Tactical Boundaries")
            boundaries = [
                {"Level": "Next-Day Floor R2", "Price": f"₹{round(tech['R2'])}", "Significance": "Secondary Intraday Extension Target"},
                {"Level": "Next-Day Floor R1", "Price": f"₹{round(tech['R1'])}", "Significance": "Primary Supply Zone"},
                {"Level": "Previous Day High (PDH)", "Price": f"₹{round(tech['PDH'])}", "Significance": "Key Structural Breakout Level"},
                {"Level": "Forward CPR (TC - BC)", "Price": f"₹{round(tech['CPR_BC'])} - ₹{round(tech['CPR_TC'])}", "Significance": "Intraday Equilibrium Band"},
                {"Level": "Previous Day Low (PDL)", "Price": f"₹{round(tech['PDL'])}", "Significance": "Key Structural Breakdown Level"},
                {"Level": "Next-Day Floor S1", "Price": f"₹{round(tech['S1'])}", "Significance": "Primary Dip Demand Base"},
                {"Level": "Next-Day Floor S2", "Price": f"₹{round(tech['S2'])}", "Significance": "Secondary Panic Demand Zone"}
            ]
            if has_derivatives:
                boundaries.insert(1, {"Level": "Major Call OI Resistance", "Price": f"₹{opt['Highest_Call_OI']}", "Significance": "Strongest Call Writer Wall"})
                boundaries.append({"Level": "Major Put OI Support", "Price": f"₹{opt['Highest_Put_OI']}", "Significance": "Strongest Put Writer Floor"})

            st.dataframe(pd.DataFrame(boundaries), use_container_width=True, hide_index=True)

        with col_right:
            st.subheader("🔍 Institutional Evidence Breakdown")
            for ev in evidence:
                st.write(f"• {ev}")

        st.markdown("---")

        st.subheader("📋 Tomorrow's Actionable Playbook (Intraday Scenarios)")
        p1, p2 = st.columns(2)
        with p1:
            st.markdown(f"""
            <div class='scenario-card'>
                <h4>Scenario A: Gap-Up (&gt; ₹{round(tech['PDH'] + 40)})</h4>
                <p>Do not chase early momentum. Look for an initial pullback towards <b>₹{round(tech['PDH'])}</b> or <b>₹{round(tech['CPR_TC'])}</b>. Enter longs only if 15-min rejection wicks appear from below.</p>
            </div>
            <div class='scenario-card' style='border-left-color: #10b981;'>
                <h4>Scenario B: Flat Open (Within CPR ₹{round(tech['CPR_BC'])} - ₹{round(tech['CPR_TC'])})</h4>
                <p>Equilibrium test. If price breaks and sustains above <b>₹{round(tech['CPR_TC'])}</b>, target <b>₹{round(tech['PDH'])}</b>. If rejected at CPR, expect rotational chopping.</p>
            </div>
            """, unsafe_allow_html=True)

        with p2:
            st.markdown(f"""
            <div class='scenario-card' style='border-left-color: #f59e0b;'>
                <h4>Scenario C: Gap-Down (&lt; ₹{round(tech['PDL'] - 40)})</h4>
                <p>Watch for panic exhaustion near Floor S1 <b>₹{round(tech['S1'])}</b>{" or Put Writing Zone ₹" + str(opt['Strongest_Put_Writing'] or opt['Highest_Put_OI']) if has_derivatives else ""}. Avoid shorting into major demand zones without breakdown confirmation.</p>
            </div>
            <div class='scenario-card' style='border-left-color: #ef4444;'>
                <h4>Scenario D: Breakdown Invalidation (&lt; ₹{round(tech['PDL'])})</h4>
                <p>Structural bear trigger. If a 15-minute candle closes below <b>₹{round(tech['PDL'])}</b> with expanding volume, abort long bias and look for extension toward <b>₹{round(tech['S2'])}</b>.</p>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("---")
        if st.button("📲 Send Daily Briefing to Telegram", type="primary"):
            deriv_line = f"• PCR: `{opt['PCR']}` | Max Pain: `₹{opt['Max_Pain']}`\n• Writing Zones: `₹{opt['Strongest_Put_Writing'] or opt['Highest_Put_OI']} - ₹{opt['Strongest_Call_Writing'] or opt['Highest_Call_OI']}`" if has_derivatives else "• Derivatives: `Live NSE Feed Off-Session / Unreachable`"
            telegram_intel = (
                f"📊 *NIFTY 50 NEXT-DAY STRATEGIC BRIEFING*\n\n"
                f"🎯 *Overall Bias:* {bias_label} ({score}/100)\n"
                f"📌 *Spot Close:* ₹{round(tech['Close'], 1)}\n"
                f"⚡ *Forward CPR:* ₹{round(tech['CPR_BC'])} - ₹{round(tech['CPR_TC'])}\n"
                f"📐 *Key Range:* ₹{round(tech['PDL'])} (PDL) — ₹{round(tech['PDH'])} (PDH)\n\n"
                f"🔍 *Derivative Context:*\n{deriv_line}\n\n"
                f"💡 *Strategy:* {'Buy Dips / Breakout Retest' if score >= 65 else ('Sell Rallies / Breakdown Confirmation' if score <= 35 else 'Wait for CPR/PDH-PDL confirmation; avoid overtrading')}"
            )
            if send_telegram_msg(telegram_intel):
                st.toast("Strategic Briefing sent to Telegram!", icon="✅")
            else:
                st.error("Telegram delivery failed. Check bot credentials.")
    else:
        st.error("Unable to load index technicals from Yahoo Finance. Please retry.")
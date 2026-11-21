#!/usr/bin/env python3
"""
Daytrade-scanner voor aandelen (Yahoo Finance) en crypto (Bitvavo via ccxt).

Scant een watchlist op intraday setups (ORB, breakout door high/low van gisteren,
VWAP-pullback), filtert op volatiliteit en relatief volume, en rekent per kans
stop, doel en positiegrootte uit volgens vaste risicoregels:
  - max 1% van het account riskeren per trade
  - stop op het invalidatiepunt van de setup
  - minimaal 1:2 reward:risk tot het eerstvolgende level
  - max 3 open posities

Met --backtest speelt hij elke setup na op de afgelopen weken en slaat per setup
het slagingspercentage op. Elke scan toont dat percentage daarna bij elk signaal.

Dit is een scanner, geen handelsbot: hij plaatst geen orders. Geen financieel advies.

Voorbeelden:
  python daytrade_scanner.py --backtest                 # eerst: slagingspercentages berekenen
  python daytrade_scanner.py                            # aandelen + crypto, één keer
  python daytrade_scanner.py --market crypto --loop 5
  python daytrade_scanner.py --telegram-setup <TOKEN>   # Telegram koppelen
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.getenv("SCANNER_STATE_DIR", HERE)        # op de server: aparte map die bewaard blijft
STATS_FILE = os.path.join(HERE, "setup_stats.json")     # naast het script (wordt op GitHub gecommit)
TELEGRAM_FILE = os.path.join(HERE, "telegram_config.json")
EU_SUFFIXES = (".AS", ".DE", ".PA", ".BR", ".MI", ".MC", ".L", ".SW", ".CO", ".ST", ".HE", ".OL", ".LS")
SESSIONS = {"us": ("America/New_York", (9, 30), (16, 0)), "eu": ("Europe/Amsterdam", (9, 0), (17, 30))}


def market_open(ticker: str, now: dt.datetime | None = None) -> bool:
    """Grove check op beurstijden (feestdagen niet meegenomen; oude data wordt later alsnog weggefilterd)."""
    tz, (oh, om), (ch, cm) = SESSIONS["eu" if ticker.upper().endswith(EU_SUFFIXES) else "us"]
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return oh * 60 + om <= minutes <= ch * 60 + cm + 5

# --------------------------------------------------------------------------- #
# Instellingen
# --------------------------------------------------------------------------- #

DEFAULT_STOCKS = [
    # US: liquide namen en ETF's die veel intraday bewegen
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN",
    "GOOGL", "NFLX", "AVGO", "PLTR", "COIN", "MSTR", "SMCI", "MU", "INTC", "BA",
    "UBER", "SHOP", "SOFI", "HOOD", "MARA", "RIOT", "ARM", "CRWD", "SNOW", "RIVN",
    # Euronext Amsterdam
    "ASML.AS", "ADYEN.AS", "BESI.AS", "INGA.AS", "PRX.AS", "ASM.AS",
]
STABLECOINS = {"USDT", "USDC", "EURC", "DAI", "EURT", "TUSD", "FDUSD", "PYUSD", "EURS"}
JUDGE_RANK = {"te weinig data": 0, "laag": 1, "gemiddeld": 2, "hoog": 3}


@dataclass
class Config:
    account: float = 10_000.0
    risk_pct: float = 1.0          # % van account per trade
    min_rr: float = 2.0            # minimale reward:risk
    max_positions: int = 3
    min_rvol: float = 1.5          # relatief volume t.o.v. zelfde tijdstip vorige dagen
    min_atr_pct: float = 2.0       # dagelijkse ATR als % van de prijs (genoeg beweging?)
    max_stop_pct: float = 4.0      # stops verder dan dit (% van prijs) worden overgeslagen
    or_bars: int = 3               # opening range = eerste 3 candles van 5 min (15 min)
    max_extension_atr: float = 1.5 # niet najagen: max afstand tot triggerlevel in 5m-ATR
    rsi_max: float = 75.0          # long niet als 5m-RSI hoger is
    rsi_min: float = 25.0          # short niet als 5m-RSI lager is
    shorts_stocks: bool = True
    shorts_crypto: bool = False    # spot crypto kun je niet shorten
    min_trades_for_judgement: int = 20


# --------------------------------------------------------------------------- #
# Indicatoren
# --------------------------------------------------------------------------- #

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def build_features(intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Berekent per 5m-candle alles wat de regels nodig hebben, met alleen data tot en met die candle.
    Zo gebruiken de live scan (laatste candle) en de backtest (elke candle) exact dezelfde logica."""
    f = intra.copy()
    f["d"] = np.array(f.index.date, dtype="datetime64[D]")
    f["i"] = np.arange(len(f))
    tp = (f["high"] + f["low"] + f["close"]) / 3
    f["_pv"] = tp * f["volume"]
    g = f.groupby("d", sort=False)
    f["k"] = g.cumcount()                                   # candle-nummer binnen de sessie
    f["cumvol"] = g["volume"].cumsum()
    f["vwap"] = (g["_pv"].cumsum() / f["cumvol"].replace(0, np.nan)).fillna(tp)
    f["e9"], f["e20"] = ema(f["close"], 9), ema(f["close"], 20)
    f["rsi"], f["atr5"] = rsi(f["close"]), atr(f)
    first = f[f["k"] < cfg.or_bars].groupby("d")
    f["orh"] = f["d"].map(first["high"].max())
    f["orl"] = f["d"].map(first["low"].min())
    f["hod"], f["lod"] = g["high"].cummax(), g["low"].cummin()

    # RVOL: volume tot nu toe vs. gemiddelde over dezelfde candles op de 5 vorige sessies
    piv = f.pivot_table(index="d", columns="k", values="cumvol", aggfunc="last")
    base = piv.shift(1).rolling(5, min_periods=1).mean()
    rv = (piv / base.replace(0, np.nan)).to_numpy()
    f["rvol"] = rv[piv.index.get_indexer(f["d"]), piv.columns.get_indexer(f["k"])]

    # Daglevels: steeds van de laatste volledige dag vóór de sessie
    dd = daily.copy()
    dd["datr"] = atr(dd)
    dd["hi5"], dd["lo5"] = dd["high"].rolling(5).max(), dd["low"].rolling(5).min()
    dd.loc[np.arange(len(dd)) < 14, "datr"] = np.nan        # te weinig dagen voor ATR
    ddates = np.array(dd.index.date, dtype="datetime64[D]")
    sess = np.unique(f["d"].to_numpy())
    pos = np.searchsorted(ddates, sess, side="left") - 1
    lv = {}
    for s, p in zip(sess, pos):
        r = dd.iloc[p] if p >= 0 else None
        lv[s] = (r["high"], r["low"], r["close"], r["hi5"], r["lo5"], r["datr"]) if r is not None else (np.nan,) * 6
    arr = np.array([lv[d] for d in f["d"].to_numpy()], dtype=float)
    f["pdh"], f["pdl"], f["pdc"], f["hi5"], f["lo5"], f["datr"] = arr.T
    f["gap_pct"] = (f["d"].map(g["open"].first()) / f["pdc"] - 1) * 100
    return f.drop(columns="_pv")


# --------------------------------------------------------------------------- #
# Regels
# --------------------------------------------------------------------------- #

def size_position(entry: float, stop: float, cfg: Config, fractional: bool):
    risk_amount = cfg.account * cfg.risk_pct / 100
    per_unit = abs(entry - stop)
    qty = risk_amount / per_unit
    note = ""
    if qty * entry > cfg.account:  # geen hefboom
        qty = cfg.account / entry
        note = "beperkt door accountgrootte"
    qty = round(qty, 6) if fractional else math.floor(qty)
    return qty, qty * entry, qty * per_unit, note


def rules(row: dict, cfg: Config, market: str) -> list[dict]:
    """Past de setups toe op één candle (row uit build_features)."""
    if row["i"] < 30 or row["k"] < cfg.or_bars + 1:
        return []
    if any(pd.isna(row[x]) for x in ("pdh", "datr", "rvol", "orh", "atr5")):
        return []
    price, vw, r, atr5 = row["close"], row["vwap"], row["rsi"], row["atr5"]
    atr_pct, rvol = row["datr"] / price * 100, row["rvol"]
    if atr_pct < cfg.min_atr_pct or not rvol >= cfg.min_rvol:
        return []

    orh, orl = row["orh"], row["orl"]
    ormid = (orh + orl) / 2
    pdh, pdl, e9, e20 = row["pdh"], row["pdl"], row["e9"], row["e20"]
    ext = cfg.max_extension_atr * atr5
    allow_short = cfg.shorts_crypto if market == "crypto" else cfg.shorts_stocks
    up = price > vw and e9 > e20
    down = price < vw and e9 < e20
    cands = []  # (setup, richting, stop)

    if up and r <= cfg.rsi_max:
        if price > orh and price - orh <= ext:
            cands.append(("ORB", "long", ormid))
        if price > pdh and row["lod"] < pdh and price - pdh <= ext:
            cands.append(("Breakout high gisteren", "long", pdh - 0.5 * atr5))
        if row["low"] <= max(vw, e20) * 1.001 and price > vw and r >= 40:
            cands.append(("VWAP-pullback", "long", min(row["low"], vw) - 0.25 * atr5))

    if down and r >= cfg.rsi_min and allow_short:
        if price < orl and orl - price <= ext:
            cands.append(("ORB", "short", ormid))
        if price < pdl and row["hod"] > pdl and pdl - price <= ext:
            cands.append(("Breakdown low gisteren", "short", pdl + 0.5 * atr5))
        if row["high"] >= min(vw, e20) * 0.999 and price < vw and r <= 60:
            cands.append(("VWAP-afwijzing", "short", max(row["high"], vw) + 0.25 * atr5))

    out = []
    for setup, side, stop in cands:
        entry = price
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0 or risk / entry * 100 > cfg.max_stop_pct:
            continue
        if side == "long":
            levels = [lv for lv in (pdh, row["hi5"], row["hod"]) if lv > entry * 1.001]
            level = min(levels) if levels else None
            target = entry + cfg.min_rr * risk
        else:
            levels = [lv for lv in (pdl, row["lo5"], row["lod"]) if lv < entry * 0.999]
            level = max(levels) if levels else None
            target = entry - cfg.min_rr * risk
        rr_level = abs(level - entry) / risk if level is not None else None
        rr_ok = rr_level is None or rr_level >= cfg.min_rr
        qty, value, risk_eur, size_note = size_position(entry, stop, cfg, fractional=(market == "crypto"))
        notes = [n for n in (size_note,
                             "geen level in de weg (nieuwe high/low)" if level is None else "",
                             f"R:R tot level < {cfg.min_rr:g}" if not rr_ok else "") if n]
        score = min(rvol, 5) * 2 + min(atr_pct, 8) + (min(rr_level, 5) if rr_level is not None else 3)
        out.append({
            "markt": market, "setup": setup, "richting": side,
            "prijs": entry, "stop": stop, "doel_2R": target, "volgend_level": level,
            "rr_tot_level": rr_level, "rr_ok": rr_ok, "rvol": rvol, "atr_pct": atr_pct,
            "gap_pct": row["gap_pct"] if market != "crypto" else float("nan"),
            "rsi_5m": r, "vwap": vw, "aantal": qty, "positiewaarde": value,
            "risico": risk_eur, "score": score, "opmerking": "; ".join(notes),
        })
    return out


def analyze(symbol: str, market: str, intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config) -> list[dict]:
    f = build_features(intra, daily, cfg)
    sigs = rules(f.iloc[-1].to_dict(), cfg, market)
    for s in sigs:
        s["symbool"], s["data_tijd"] = symbol, str(f.index[-1])
    return sigs


# --------------------------------------------------------------------------- #
# Backtest en slagingspercentage
# --------------------------------------------------------------------------- #

def backtest_symbol(symbol: str, market: str, intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config) -> list[dict]:
    """Speelt elke setup na: instap op de slotkoers van de signaalcandle, daarna wint hij als
    het 2R-doel eerder geraakt wordt dan de stop. Raakt een candle beide, dan telt het als verlies
    (voorzichtig). Niets geraakt aan het eind van de sessie: uitstap op de slotkoers.
    Per symbool/dag/setup/richting telt alleen het eerste signaal."""
    f = build_features(intra, daily, cfg)
    last_day = f["d"].iloc[-1]
    H, L, C = f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy()
    end = (f["i"] + f.groupby("d").cumcount(ascending=False)).to_numpy()  # laatste candle van de sessie
    seen, trades = set(), []
    for row in f.to_dict("records"):
        if row["d"] == last_day:      # lopende sessie overslaan: uitkomst nog onbekend
            break
        for s in rules(row, cfg, market):
            key = (row["d"], s["setup"], s["richting"])
            if not s["rr_ok"] or key in seen:
                continue
            seen.add(key)
            i, e = row["i"], end[row["i"]]
            if i >= e:
                continue
            entry, stop, target = s["prijs"], s["stop"], s["doel_2R"]
            risk = abs(entry - stop)
            long = s["richting"] == "long"
            result, r_mult = "tijd", None
            for j in range(i + 1, e + 1):
                hit_stop = L[j] <= stop if long else H[j] >= stop
                hit_tgt = H[j] >= target if long else L[j] <= target
                if hit_stop:
                    result, r_mult = "verlies", -1.0
                    break
                if hit_tgt:
                    result, r_mult = "winst", cfg.min_rr
                    break
            if r_mult is None:
                r_mult = ((C[e] - entry) if long else (entry - C[e])) / risk
            trades.append({"markt": market, "symbool": symbol, "setup": s["setup"], "richting": s["richting"],
                           "datum": str(row["d"]), "resultaat": result, "R": r_mult})
    return trades


def summarize(trades: pd.DataFrame) -> dict:
    stats = {}
    for (m, s, r), g in trades.groupby(["markt", "setup", "richting"]):
        stats[f"{m}|{s}|{r}"] = {"n": int(len(g)), "winrate": float((g["resultaat"] == "winst").mean()),
                                 "avg_r": float(g["R"].mean())}
    return stats


def judge(st: dict | None, cfg: Config) -> str:
    """Oordeel op basis van de verwachte R per trade (winrate alleen zegt weinig bij een 2R-doel:
    vanaf ~33% winst speel je al quitte)."""
    if not st or st["n"] < cfg.min_trades_for_judgement:
        return "te weinig data"
    if st["avg_r"] <= 0:
        return "laag"
    return "hoog" if st["avg_r"] >= 0.25 else "gemiddeld"


def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {}
    with open(STATS_FILE) as fh:
        data = json.load(fh)
    age = (dt.date.today() - dt.date.fromisoformat(data["gemaakt"][:10])).days
    if age > 14:
        print(f"Let op: slagingspercentages zijn {age} dagen oud. Draai --backtest opnieuw.")
    return data.get("stats", {})


def add_stats(df: pd.DataFrame, stats: dict, cfg: Config) -> pd.DataFrame:
    def col(row):
        st = stats.get(f"{row['markt']}|{row['setup']}|{row['richting']}")
        return pd.Series({
            "slagingskans": f"{st['winrate']*100:.0f}% (n={st['n']})" if st else "-",
            "verw_R": st["avg_r"] if st else float("nan"),
            "oordeel": judge(st, cfg),
        })
    return pd.concat([df, df.apply(col, axis=1)], axis=1)


# --------------------------------------------------------------------------- #
# Data ophalen
# --------------------------------------------------------------------------- #

def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    return df.dropna(subset=["open", "high", "low", "close"])


def stock_data(t: str, intraday_period: str, daily_period: str):
    import yfinance as yf
    tk = yf.Ticker(t)
    return (_norm(tk.history(period=intraday_period, interval="5m", prepost=False)),
            _norm(tk.history(period=daily_period, interval="1d")))


def _ohlcv_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.sort_index()


def crypto_exchange(exchange_id: str):
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    ex.load_markets()
    return ex


def crypto_symbols(ex, quote: str, top_n: int, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip() for s in symbols.split(",")]
    tickers = ex.fetch_tickers()
    pairs = [(s, tk.get("quoteVolume") or 0) for s, tk in tickers.items()
             if s.endswith("/" + quote) and s.split("/")[0] not in STABLECOINS]
    return [s for s, _ in sorted(pairs, key=lambda x: x[1], reverse=True)[:top_n]]


def crypto_data(ex, sym: str, days: int):
    since = ex.milliseconds() - days * 24 * 3600 * 1000
    rows = []
    for _ in range(100):  # pagineren
        batch = ex.fetch_ohlcv(sym, "5m", since=since, limit=1000)
        if not batch:
            break
        rows += batch
        if batch[-1][0] >= ex.milliseconds() - 10 * 60 * 1000 or batch[-1][0] + 1 <= since:
            break
        since = batch[-1][0] + 1
    return _ohlcv_df(rows), _ohlcv_df(ex.fetch_ohlcv(sym, "1d", limit=max(60, days + 30)))


def _parallel(fn, items, workers=8):
    def safe(x):
        try:
            return fn(x)
        except Exception as e:  # één kapot symbool mag de scan niet stoppen
            print(f"  ! {x}: {e}", file=sys.stderr)
            return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for res in pool.map(safe, items) for r in res]


def scan_stocks(tickers: list[str], cfg: Config, include_closed: bool) -> list[dict]:
    def one(t):
        intra, daily = stock_data(t, "5d", "3mo")
        if intra.empty or daily.empty:
            return []
        stale = dt.datetime.now(dt.timezone.utc) - intra.index[-1].tz_convert("UTC") > dt.timedelta(minutes=30)
        if stale and not include_closed:
            return []
        sigs = analyze(t, "aandeel", intra, daily, cfg)
        for s in sigs:
            if stale:
                s["opmerking"] = "; ".join(x for x in (s["opmerking"], "MARKT DICHT – oude data") if x)
        return sigs
    return _parallel(one, tickers, workers=4)  # rustig aan i.v.m. rate limits van Yahoo


def scan_crypto(cfg: Config, args) -> list[dict]:
    ex = crypto_exchange(args.exchange)
    syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols)
    def one(sym):
        intra, daily = crypto_data(ex, sym, 3)
        return analyze(sym, "crypto", intra, daily, cfg) if len(intra) and len(daily) else []
    return _parallel(one, syms, workers=1)  # sequentieel i.v.m. rate limits


def run_backtest(args, tickers: list[str], cfg: Config):
    trades = []
    if args.market in ("stocks", "all"):
        print(f"Backtest aandelen ({len(tickers)}, laatste ~60 dagen; dat is het maximum voor 5m-data bij Yahoo)...")
        trades += _parallel(lambda t: backtest_symbol(t, "aandeel", *stock_data(t, "60d", "1y"), cfg), tickers,
                            workers=4)
    if args.market in ("crypto", "all"):
        ex = crypto_exchange(args.exchange)
        syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols)
        print(f"Backtest crypto ({len(syms)} paren, laatste {args.bt_days} dagen)...")
        trades += _parallel(lambda s: backtest_symbol(s, "crypto", *crypto_data(ex, s, args.bt_days), cfg),
                            syms, workers=1)
    if not trades:
        print("Geen trades gevonden in de backtest.")
        return
    tdf = pd.DataFrame(trades)
    tdf.to_csv(os.path.join(STATE_DIR, "backtest_trades.csv"), index=False)
    stats = summarize(tdf)
    with open(STATS_FILE, "w") as fh:
        json.dump({"gemaakt": dt.datetime.now().isoformat(timespec="seconds"), "stats": stats}, fh, indent=2)
    rows = [{"setup": k.replace("|", " / "), "trades": v["n"], "winrate": f"{v['winrate']*100:.0f}%",
             "verw. R": f"{v['avg_r']:+.2f}", "oordeel": judge(v, cfg)} for k, v in stats.items()]
    print(pd.DataFrame(rows).sort_values("setup").to_string(index=False))
    print(f"\nBreak-even bij een {cfg.min_rr:g}R-doel ligt rond {100/(1+cfg.min_rr):.0f}% winrate. "
          "Kosten en slippage zitten er niet in.")
    print(f"Opgeslagen in {STATS_FILE}; alle trades in backtest_trades.csv")

    token, chat = telegram_creds()
    if token and chat:
        icon = {"hoog": "🟢", "gemiddeld": "🟡", "laag": "🔴", "te weinig data": "⚪"}
        lines = [f"{icon[r['oordeel']]} {r['setup']}: {r['winrate']} win, {r['verw. R']}R (n={r['trades']})"
                 for r in sorted(rows, key=lambda r: r["setup"])]
        telegram_send(token, chat, "📊 Backtest bijgewerkt\n" + "\n".join(lines))


# --------------------------------------------------------------------------- #
# Uitvoer: tabel, log, Telegram
# --------------------------------------------------------------------------- #

def fmt_price(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.6g}"


def print_table(df: pd.DataFrame, top: int):
    if df.empty:
        print("Geen kansen die aan alle filters voldoen.")
        return
    view = df.head(top).copy()
    for col in ("prijs", "stop", "doel_2R", "volgend_level", "positiewaarde", "risico"):
        view[col] = view[col].map(fmt_price)
    view["rr_tot_level"] = view["rr_tot_level"].map(lambda v: "open" if v is None or pd.isna(v) else f"{v:.1f}")
    view["rvol"] = view["rvol"].map(lambda v: f"{v:.1f}x")
    view["atr_pct"] = view["atr_pct"].map(lambda v: f"{v:.1f}%")
    view["gap_pct"] = view["gap_pct"].map(lambda v: "-" if pd.isna(v) else f"{v:+.1f}%")
    view["rsi_5m"] = view["rsi_5m"].map(lambda v: f"{v:.0f}")
    view["aantal"] = view["aantal"].map(lambda v: f"{v:g}")
    view["verw_R"] = view["verw_R"].map(lambda v: "-" if pd.isna(v) else f"{v:+.2f}")
    cols = ["symbool", "setup", "richting", "prijs", "stop", "doel_2R", "volgend_level", "rr_tot_level",
            "slagingskans", "verw_R", "oordeel", "rvol", "atr_pct", "gap_pct", "rsi_5m", "aantal",
            "positiewaarde", "risico", "opmerking"]
    print(view[cols].to_string(index=False))


def log_signals(df: pd.DataFrame, path: str) -> pd.DataFrame:
    """Voegt nieuwe signalen toe aan een CSV (handig als papertrade-logboek). Geeft de nieuwe terug."""
    if df.empty:
        return df
    df = df.copy()
    df["datum"] = dt.date.today().isoformat()
    df["gescand_om"] = dt.datetime.now().strftime("%H:%M:%S")
    key = lambda d: d["datum"] + "|" + d["symbool"] + "|" + d["setup"] + "|" + d["richting"]
    if os.path.exists(path):
        old = pd.read_csv(path, usecols=["datum", "symbool", "setup", "richting"], dtype=str)
        new = df[~key(df).isin(set(key(old)))]
    else:
        new = df
    if not new.empty:
        new = new.assign(uitkomst="", notities="")  # in te vullen bij papertraden
        new.to_csv(path, mode="a", header=not os.path.exists(path), index=False)
    return new


def telegram_creds():
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if (not token or not chat) and os.path.exists(TELEGRAM_FILE):
        with open(TELEGRAM_FILE) as fh:
            c = json.load(fh)
        token, chat = token or c.get("token"), chat or c.get("chat_id")
    return token, chat


def telegram_send(token: str, chat: str, text: str) -> bool:
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
        return True
    except Exception as e:
        print(f"  ! Telegram: {e}", file=sys.stderr)
        return False


def telegram_setup(token: str):
    """Zoekt je chat-id op (stuur eerst een bericht naar je bot), slaat alles op en stuurt een test."""
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
            updates = json.load(r).get("result", [])
    except Exception as e:
        sys.exit(f"Kon Telegram niet bereiken of token is ongeldig: {e}")
    chats = [u["message"]["chat"]["id"] for u in updates if "message" in u]
    if not chats:
        sys.exit("Nog geen chat gevonden. Open je bot in Telegram, stuur hem een bericht (bv. 'hoi') en probeer opnieuw.")
    chat = str(chats[-1])
    with open(TELEGRAM_FILE, "w") as fh:
        json.dump({"token": token, "chat_id": chat}, fh)
    ok = telegram_send(token, chat, "✅ Daytrade-scanner gekoppeld. Nieuwe signalen komen hier binnen.")
    print(f"Opgeslagen in {TELEGRAM_FILE} (chat-id {chat}). Testbericht {'verstuurd' if ok else 'MISLUKT'}.")


def send_telegram(df: pd.DataFrame, minimum: str):
    token, chat = telegram_creds()
    if not token or not chat or df.empty:
        return
    df = df[df["oordeel"].map(JUDGE_RANK) >= JUDGE_RANK.get(minimum, 0)] if minimum != "alle" else df
    icon = {"hoog": "🟢", "gemiddeld": "🟡", "laag": "🔴", "te weinig data": "⚪"}
    for _, s in df.iterrows():
        pijl = "⬆️" if s["richting"] == "long" else "⬇️"
        text = (f"{pijl} {s['symbool']} – {s['setup']} ({s['richting']})\n"
                f"Prijs {fmt_price(s['prijs'])} | stop {fmt_price(s['stop'])} | doel 2R {fmt_price(s['doel_2R'])}\n"
                f"Aantal {s['aantal']:g} (risico {fmt_price(s['risico'])})\n"
                f"{icon.get(s['oordeel'], '')} Slagingskans {s['slagingskans']} – {s['oordeel']}\n"
                f"RVOL {s['rvol']:.1f}x | ATR {s['atr_pct']:.1f}%"
                + (f"\n⚠️ {s['opmerking']}" if s["opmerking"] else ""))
        telegram_send(token, chat, text)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run_once(args, cfg: Config, tickers: list[str], stats: dict):
    print(f"\n=== Scan {dt.datetime.now():%Y-%m-%d %H:%M:%S} ===")
    if args.open_positions >= cfg.max_positions:
        print(f"Let op: je hebt {args.open_positions} open posities (max {cfg.max_positions}). "
              "Volgens je regels geen nieuwe trades; de scan is alleen ter info.")
    sigs = []
    if args.market in ("stocks", "all"):
        open_t = tickers if args.include_closed else [t for t in tickers if market_open(t)]
        if open_t:
            print(f"Aandelen scannen ({len(open_t)} met open beurs)...")
            sigs += scan_stocks(open_t, cfg, args.include_closed)
        else:
            print("Aandelenbeurzen zijn dicht, aandelen overgeslagen.")
    if args.market in ("crypto", "all"):
        print(f"Crypto scannen op {args.exchange} (top {args.crypto_top} {args.quote}-paren)...")
        sigs += scan_crypto(cfg, args)
    df = pd.DataFrame(sigs)
    if not df.empty:
        if not args.show_all:
            df = df[df["rr_ok"]]
        df = add_stats(df, stats, cfg) if not df.empty else df
    if not df.empty:
        df["_rank"] = df["oordeel"].map(JUDGE_RANK)
        df = df.sort_values(["_rank", "score"], ascending=False).drop(columns="_rank").reset_index(drop=True)
    print_table(df, args.top)
    new = log_signals(df, args.log)
    if not new.empty:
        print(f"{len(new)} nieuwe signalen gelogd in {args.log}")
    send_telegram(new, args.telegram_min)


def main():
    p = argparse.ArgumentParser(description="Daytrade-scanner voor aandelen en crypto")
    p.add_argument("--market", choices=["stocks", "crypto", "all"], default="all")
    p.add_argument("--account", type=float, default=10_000, help="accountgrootte (default 10000)")
    p.add_argument("--risk-pct", type=float, default=1.0, help="risico per trade in %% (default 1)")
    p.add_argument("--open-positions", type=int, default=0, help="aantal posities dat je nu open hebt")
    p.add_argument("--min-rvol", type=float, default=1.5)
    p.add_argument("--min-atr-pct", type=float, default=2.0)
    p.add_argument("--stocks-file", help="tekstbestand met één ticker per regel")
    p.add_argument("--exchange", default="bitvavo", help="ccxt-exchange-id (default bitvavo)")
    p.add_argument("--quote", default="EUR", help="quote-valuta voor crypto (default EUR)")
    p.add_argument("--crypto-top", type=int, default=30, help="aantal crypto-paren op volume")
    p.add_argument("--crypto-symbols", help="vaste lijst, bv. 'BTC/EUR,ETH/EUR,SOL/EUR'")
    p.add_argument("--no-stock-shorts", action="store_true", help="geen short-setups voor aandelen")
    p.add_argument("--crypto-shorts", action="store_true", help="ook short-setups voor crypto tonen")
    p.add_argument("--include-closed", action="store_true", help="aandelen ook scannen als de markt dicht is")
    p.add_argument("--show-all", action="store_true", help="ook setups met R:R onder het minimum tonen")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--loop", type=float, default=0, help="elke N minuten opnieuw scannen")
    p.add_argument("--log", default=os.path.join(STATE_DIR, "signals_log.csv"))
    p.add_argument("--backtest", action="store_true", help="slagingspercentages per setup berekenen")
    p.add_argument("--bt-days", type=int, default=45, help="aantal dagen crypto-backtest (default 45)")
    p.add_argument("--telegram-setup", metavar="TOKEN", help="koppel je Telegram-bot en stuur een testbericht")
    p.add_argument("--telegram-min", choices=["alle", "gemiddeld", "hoog"], default="alle",
                   help="alleen Telegram-meldingen vanaf dit oordeel (default alle)")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)

    if args.telegram_setup:
        telegram_setup(args.telegram_setup)
        return
    if args.risk_pct > 1.0:
        print("Waarschuwing: je regel is max 1% risico per trade.")
    cfg = Config(account=args.account, risk_pct=args.risk_pct, min_rvol=args.min_rvol,
                 min_atr_pct=args.min_atr_pct, shorts_stocks=not args.no_stock_shorts,
                 shorts_crypto=args.crypto_shorts)
    tickers = DEFAULT_STOCKS
    if args.stocks_file:
        with open(args.stocks_file) as fh:
            tickers = [l.strip().upper() for l in fh if l.strip() and not l.startswith("#")]

    if args.backtest:
        run_backtest(args, tickers, cfg)
        return

    stats = load_stats()
    if not stats:
        print("Nog geen slagingspercentages. Draai eerst: python daytrade_scanner.py --backtest")
    print("Scanner, geen advies. Papertrade een setup 30+ keer voordat je hem live gebruikt.")
    while True:
        run_once(args, cfg, tickers, stats)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()

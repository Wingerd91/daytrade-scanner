#!/usr/bin/env python3
"""
Trade-scanner voor aandelen (Yahoo Finance) en crypto (Bitvavo via ccxt).

Twee soorten signalen, strikt gescheiden:

  ⚡ DAYTRADE  openen én sluiten op dezelfde dag. Vaste stop en vast 2R-doel.
              Setups: ORB, breakout/breakdown high/low van gisteren, VWAP-pullback/-afwijzing.
              Wordt automatisch gepapertraded (max 3 tegelijk open).
  🟣 SWING     dagen tot maanden vasthouden. Uitstap op je eigen red zone (Fib, zelf tekenen).
              Setups: koers boven de EMA Ribbon (dag), WaveTrend-omslag in oversold (week).
              Eén keer per dag gescand. Géén automatische papertrade.

Vaste risicoregels: max 1% risico per trade, stop op invalidatie, minimaal 1:2, max 3 posities.
Dit is een scanner, geen handelsbot: hij plaatst geen orders. Geen financieel advies.

Voorbeelden:
  python daytrade_scanner.py --backtest          # slagingspercentages daytrade-setups (na kosten)
  python daytrade_scanner.py                     # één scan (+ swing en dagoverzicht na 22:15)
  python daytrade_scanner.py --market crypto --loop 5
  python daytrade_scanner.py --swing-now         # swing-scan + dagoverzicht direct uitvoeren
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
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.getenv("SCANNER_STATE_DIR", HERE)        # op de server: aparte map die bewaard blijft
STATS_FILE = os.path.join(HERE, "setup_stats.json")     # naast het script (wordt op GitHub gecommit)
TELEGRAM_FILE = os.path.join(HERE, "telegram_config.json")
PAPER_FILE = os.path.join(STATE_DIR, "paper_trades.csv")
SWING_LOG = os.path.join(STATE_DIR, "swing_log.csv")
DAY_STATE = os.path.join(STATE_DIR, "dag_status.json")
CHAT_ID_FILE = os.path.join(STATE_DIR, "telegram_chat_id.txt")
AMS = ZoneInfo("Europe/Amsterdam")

DAY = "⚡ DAYTRADE"
SWING = "🟣 SWING"
DAY_UITLEG = "vandaag openen én vandaag sluiten"
SWING_UITLEG = "dagen tot maanden vasthouden – GEEN daytrade"

DEFAULT_STOCKS = [
    # US: liquide namen en ETF's die veel intraday bewegen
    "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN",
    "GOOGL", "NFLX", "AVGO", "PLTR", "COIN", "MSTR", "SMCI", "MU", "INTC", "BA",
    "UBER", "SHOP", "SOFI", "HOOD", "MARA", "RIOT", "ARM", "CRWD", "SNOW", "RIVN",
    # Euronext Amsterdam
    "ASML.AS", "ADYEN.AS", "BESI.AS", "INGA.AS", "PRX.AS", "ASM.AS",
]
STABLECOINS = {"USDT", "USDC", "EURC", "DAI", "EURT", "TUSD", "FDUSD", "PYUSD", "EURS"}
EU_SUFFIXES = (".AS", ".DE", ".PA", ".BR", ".MI", ".MC", ".L", ".SW", ".CO", ".ST", ".HE", ".OL", ".LS")
SESSIONS = {"us": ("America/New_York", (9, 30), (16, 0)), "eu": ("Europe/Amsterdam", (9, 0), (17, 30))}
JUDGE_RANK = {"te weinig data": 0, "laag": 1, "gemiddeld": 2, "hoog": 3}
ICON = {"hoog": "🟢", "gemiddeld": "🟡", "laag": "🔴", "te weinig data": "⚪"}
PAPER_COLS = ["id", "type", "markt", "symbool", "setup", "richting", "open_tijd", "prijs", "stop", "doel",
              "fee", "be_r", "oordeel", "status", "sluit_tijd", "uitstap", "R"]


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
    fee_stocks: float = 0.0005     # kosten per kant (aan- of verkoop), 0,05%: spread + commissie
    fee_crypto: float = 0.0025     # Bitvavo taker-fee per kant, 0,25%
    regime_drop_crypto: float = 1.0  # BTC "hard dalend": onder VWAP, EMA9<EMA20 én ≥1% gedaald vandaag
    regime_drop_index: float = 0.5   # SPY/AEX idem bij ≥0,5%
    summary_time: tuple = (22, 15)   # dagoverzicht + swing-scan (Nederlandse tijd)
    wt_oversold: float = -53.0       # VuManChu B standaard oversold-niveau
    # Filters tegen "stop wordt steeds geraakt" (de backtest kiest de beste combinatie per markt):
    min_stop_atr: float = 1.0        # invalidatie moet ≥ zoveel 5m-ATR weg liggen; dichterbij = ruis → overslaan
    max_cost_r: float = 0.25         # kosten mogen max. dit deel van je risico zijn, anders overslaan
    trend_filter: bool = True        # long alleen als gisteren boven de dag-EMA20 sloot (short omgekeerd)
    breakeven_r: float = 0.0         # >0: stop naar instapprijs zodra de koers zoveel R in je voordeel staat
    active_hours: tuple | None = None  # crypto: alleen signalen tussen deze UTC-uren, bv. (13, 21)

    def fee(self, market: str) -> float:
        return self.fee_crypto if market == "crypto" else self.fee_stocks


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def market_open(ticker: str, now: dt.datetime | None = None) -> bool:
    """Grove check op beurstijden (feestdagen niet meegenomen; oude data wordt later alsnog weggefilterd)."""
    tz, (oh, om), (ch, cm) = SESSIONS["eu" if ticker.upper().endswith(EU_SUFFIXES) else "us"]
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return oh * 60 + om <= minutes <= ch * 60 + cm + 5


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


def wavetrend(df: pd.DataFrame, channel: int = 9, average: int = 12, ma: int = 3):
    """WaveTrend zoals in VuManChu Cipher B (standaardinstellingen 9/12/3)."""
    ap = (df["high"] + df["low"] + df["close"]) / 3
    esa = ema(ap, channel)
    d = ema((ap - esa).abs(), channel)
    ci = (ap - esa) / (0.015 * d.replace(0, np.nan))
    wt1 = ema(ci.fillna(0), average)
    return wt1, wt1.rolling(ma).mean()


def closed_only(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Haalt de laatste candle weg als die nog niet afgesloten is (anders 'verdwijnen' signalen weer)."""
    if df.empty:
        return df
    end = df.index[-1] + pd.Timedelta(minutes=minutes)
    return df.iloc[:-1] if end > now_utc() else df


def _dates(idx) -> np.ndarray:
    return np.array(idx.date, dtype="datetime64[D]")


def build_features(intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Berekent per 5m-candle alles wat de regels nodig hebben, met alleen data tot en met die candle.
    Zo gebruiken de live scan (laatste candle) en de backtest (elke candle) exact dezelfde logica."""
    f = intra.copy()
    f["d"] = _dates(f.index)
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
    dd["ema20"] = ema(dd["close"], 20)
    dd.loc[np.arange(len(dd)) < 14, "datr"] = np.nan        # te weinig dagen voor ATR
    dd.loc[np.arange(len(dd)) < 20, "ema20"] = np.nan
    ddates = _dates(dd.index)
    sess = np.unique(f["d"].to_numpy())
    pos = np.searchsorted(ddates, sess, side="left") - 1
    lv = {}
    for s, p in zip(sess, pos):
        r = dd.iloc[p] if p >= 0 else None
        lv[s] = (r["high"], r["low"], r["close"], r["hi5"], r["lo5"], r["datr"], r["ema20"]) if r is not None else (np.nan,) * 7
    arr = np.array([lv[d] for d in f["d"].to_numpy()], dtype=float)
    f["pdh"], f["pdl"], f["pdc"], f["hi5"], f["lo5"], f["datr"], f["d_ema20"] = arr.T
    f["hour"] = f.index.tz_convert("UTC").hour
    f["gap_pct"] = (f["d"].map(g["open"].first()) / f["pdc"] - 1) * 100
    return f.drop(columns="_pv")


def regime_series(intra: pd.DataFrame, drop_pct: float) -> pd.Series:
    """Marktrichting per candle: -1 = hard dalend, +1 = hard stijgend, 0 = neutraal.
    Hard dalend = onder VWAP, EMA9 < EMA20 en minstens drop_pct% lager dan de opening van de dag."""
    if intra is None or intra.empty:
        return pd.Series(dtype=float)
    f = intra.copy()
    f["d"] = _dates(f.index)
    tp = (f["high"] + f["low"] + f["close"]) / 3
    f["_pv"] = tp * f["volume"]
    g = f.groupby("d", sort=False)
    vw = (g["_pv"].cumsum() / g["volume"].cumsum().replace(0, np.nan)).fillna(tp)
    e9, e20 = ema(f["close"], 9), ema(f["close"], 20)
    chg = (f["close"] / g["open"].transform("first") - 1) * 100
    s = pd.Series(0.0, index=f.index)
    s[(f["close"] < vw) & (e9 < e20) & (chg <= -drop_pct)] = -1.0
    s[(f["close"] > vw) & (e9 > e20) & (chg >= drop_pct)] = 1.0
    s.index = s.index.tz_convert("UTC")
    return s[~s.index.duplicated()].sort_index()


def align_regime(reg: pd.Series | None, index: pd.DatetimeIndex) -> np.ndarray:
    if reg is None or reg.empty:
        return np.zeros(len(index))
    return reg.reindex(index.tz_convert("UTC"), method="ffill").fillna(0).to_numpy()


# --------------------------------------------------------------------------- #
# Daytrade-regels
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


def rules(row: dict, cfg: Config, market: str, regime: float = 0.0) -> list[dict]:
    """Past de daytrade-setups toe op één afgesloten candle (row uit build_features)."""
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
    up = price > vw and e9 > e20 and regime > -1      # geen longs als de markt hard daalt
    down = price < vw and e9 < e20 and regime < 1     # geen shorts als de markt hard stijgt
    trend_up = row["pdc"] > row["d_ema20"] if not pd.isna(row["d_ema20"]) else False
    trend_down = row["pdc"] < row["d_ema20"] if not pd.isna(row["d_ema20"]) else False
    if cfg.trend_filter:
        up, down = up and trend_up, down and trend_down
    if market == "crypto" and cfg.active_hours and not (cfg.active_hours[0] <= row["hour"] < cfg.active_hours[1]):
        return []
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
    fee = cfg.fee(market)
    for setup, side, stop in cands:
        entry = price
        risk = entry - stop if side == "long" else stop - entry
        if risk <= 0 or risk / entry * 100 > cfg.max_stop_pct:
            continue
        if risk < cfg.min_stop_atr * atr5:      # invalidatie ligt binnen de normale ruis
            continue
        if fee * (2 * entry) / risk > cfg.max_cost_r:   # kosten eten te veel van het risico op
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
        kosten_r = fee * (entry + target) / risk     # kosten heen en terug, uitgedrukt in R
        notes = [n for n in (size_note,
                             "geen level in de weg (nieuwe high/low)" if level is None else "",
                             f"R:R tot level < {cfg.min_rr:g}" if not rr_ok else "") if n]
        score = min(rvol, 5) * 2 + min(atr_pct, 8) + (min(rr_level, 5) if rr_level is not None else 3) - kosten_r * 3
        out.append({
            "type": "daytrade", "markt": market, "setup": setup, "richting": side,
            "prijs": entry, "stop": stop, "doel_2R": target, "volgend_level": level,
            "rr_tot_level": rr_level, "rr_ok": rr_ok, "rvol": rvol, "atr_pct": atr_pct,
            "gap_pct": row["gap_pct"] if market != "crypto" else float("nan"),
            "rsi_5m": r, "vwap": vw, "aantal": qty, "positiewaarde": value,
            "risico": risk_eur, "kosten_R": kosten_r, "score": score, "opmerking": "; ".join(notes),
            "risk_atr": risk / atr5, "trend_ok": trend_up if side == "long" else trend_down, "hour": row["hour"],
        })
    return out


def analyze(symbol: str, market: str, intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config,
            regime: float = 0.0) -> list[dict]:
    f = build_features(intra, daily, cfg)
    sigs = rules(f.iloc[-1].to_dict(), cfg, market, regime)
    for s in sigs:
        s["symbool"], s["data_tijd"] = symbol, f.index[-1].tz_convert("UTC").isoformat()
    return sigs


def simulate(H, L, C, start: int, end: int, entry: float, stop: float, target: float, long: bool,
             be_r: float = 0.0):
    """Loopt candles start..end af. Stop en doel in dezelfde candle telt als verlies (voorzichtig).
    be_r > 0: zodra de koers be_r × risico in je voordeel staat, gaat de stop vanaf de vólgende candle
    naar de instapprijs. Geeft (resultaat, uitstapprijs, index) of None als nog niets geraakt is."""
    risk = abs(entry - stop)
    trigger = (entry + be_r * risk) if long else (entry - be_r * risk)
    moved = False
    for j in range(start, end + 1):
        if (L[j] <= stop) if long else (H[j] >= stop):
            return ("break-even" if moved else "verlies"), stop, j
        if (H[j] >= target) if long else (L[j] <= target):
            return "winst", target, j
        if be_r > 0 and not moved and ((H[j] >= trigger) if long else (L[j] <= trigger)):
            stop, moved = entry, True
    return None


def net_r(entry: float, exit_: float, stop: float, long: bool, fee: float) -> float:
    risk = abs(entry - stop)
    gross = (exit_ - entry) if long else (entry - exit_)
    return (gross - fee * (entry + exit_)) / risk


# --------------------------------------------------------------------------- #
# Backtest en slagingspercentage
# --------------------------------------------------------------------------- #

def candidates_symbol(symbol: str, market: str, intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config,
                      regime: pd.Series | None = None) -> list[dict]:
    """Alle mogelijke daytrade-signalen met de ruimste filters, met de uitkomst met en zonder break-even.
    Daarna kan elke filtercombinatie snel worden doorgerekend (zie apply_variant)."""
    loose = replace(cfg, min_stop_atr=0.0, max_cost_r=1e9, trend_filter=False, active_hours=None)
    f = build_features(intra, daily, loose)
    regs = align_regime(regime, f.index)
    last_day = f["d"].iloc[-1]
    H, L, C = f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy()
    end = (f["i"] + f.groupby("d").cumcount(ascending=False)).to_numpy()  # laatste candle van de sessie
    fee = cfg.fee(market)
    out = []
    for n, row in enumerate(f.to_dict("records")):
        if row["d"] == last_day:      # lopende sessie overslaan: uitkomst nog onbekend
            break
        for s in rules(row, loose, market, regs[n]):
            i, e = row["i"], end[row["i"]]
            if not s["rr_ok"] or i >= e:
                continue
            long = s["richting"] == "long"
            rec = {"markt": market, "symbool": symbol, "setup": s["setup"], "richting": s["richting"],
                   "datum": str(row["d"]), "i": i, "hour": s["hour"], "risk_atr": s["risk_atr"],
                   "kosten_R": fee * 2 * s["prijs"] / abs(s["prijs"] - s["stop"]), "trend_ok": bool(s["trend_ok"])}
            for be in (0.0, 1.0):
                hit = simulate(H, L, C, i + 1, e, s["prijs"], s["stop"], s["doel_2R"], long, be)
                res, exit_ = (hit[0], hit[1]) if hit else ("tijd", C[e])
                rec[f"res{be:g}"], rec[f"R{be:g}"] = res, net_r(s["prijs"], exit_, s["stop"], long, fee)
            out.append(rec)
    return out


def apply_variant(cands: pd.DataFrame, v: dict, market: str) -> pd.DataFrame:
    """Past één filtercombinatie toe op de kandidaten. Per symbool/dag/setup/richting telt het eerste signaal."""
    if cands.empty:
        return cands.assign(resultaat=[], R=[])
    c = cands[(cands["risk_atr"] >= v["min_stop_atr"]) & (cands["kosten_R"] <= v["max_cost_r"])]
    if v["trend_filter"]:
        c = c[c["trend_ok"]]
    if market == "crypto" and v.get("active_hours"):
        a, b = v["active_hours"]
        c = c[(c["hour"] >= a) & (c["hour"] < b)]
    c = c.sort_values(["symbool", "i"]).drop_duplicates(["symbool", "datum", "setup", "richting"], keep="first")
    be = "1" if v["breakeven_r"] > 0 else "0"
    return c.assign(resultaat=c[f"res{be}"], R=c[f"R{be}"])


def variant_grid(market: str, cfg: Config) -> list[dict]:
    grid = []
    for msa in (0.5, 1.0, 1.5):
        for tf in (False, True):
            for be in (0.0, 1.0):
                for hours in ((None, (13, 21)) if market == "crypto" else (None,)):
                    grid.append({"min_stop_atr": msa, "trend_filter": tf, "breakeven_r": be,
                                 "active_hours": hours, "max_cost_r": cfg.max_cost_r})
    return grid


def choose_variant(cands: pd.DataFrame, market: str, cfg: Config):
    """Kiest de beste filtercombinatie op de eerste 70% van de dagen en controleert die op de laatste 30%
    (data die bij het kiezen niet gebruikt is). Alleen 'actief' als hij ook daar winst geeft."""
    days = sorted(cands["datum"].unique()) if len(cands) else []
    if len(days) < 10:
        return None, []
    split = days[int(len(days) * 0.7)]
    train, test = cands[cands["datum"] < split], cands[cands["datum"] >= split]
    rows = []
    for v in variant_grid(market, cfg):
        tr, te = apply_variant(train, v, market), apply_variant(test, v, market)
        rows.append({"v": v, "n_train": len(tr), "R_train": tr["R"].mean() if len(tr) else np.nan,
                     "n_test": len(te), "R_test": te["R"].mean() if len(te) else np.nan})
    ok = [r for r in rows if r["n_train"] >= 30 and not np.isnan(r["R_train"])]
    if not ok:
        return None, rows
    best = max(ok, key=lambda r: (r["R_train"], r["n_train"]))
    best = dict(best, actief=bool(best["n_test"] >= 10 and best["R_test"] > 0 and best["R_train"] > 0))
    return best, rows


def backtest_symbol(symbol: str, market: str, intra: pd.DataFrame, daily: pd.DataFrame, cfg: Config,
                    regime: pd.Series | None = None) -> list[dict]:
    """Backtest met de huidige instellingen van cfg (handig voor losse tests)."""
    cands = pd.DataFrame(candidates_symbol(symbol, market, intra, daily, cfg, regime))
    v = {"min_stop_atr": cfg.min_stop_atr, "trend_filter": cfg.trend_filter, "breakeven_r": cfg.breakeven_r,
         "active_hours": cfg.active_hours, "max_cost_r": cfg.max_cost_r}
    t = apply_variant(cands, v, market)
    return t[["markt", "symbool", "setup", "richting", "datum", "resultaat", "R"]].to_dict("records") if len(t) else []


def summarize(trades: pd.DataFrame) -> dict:
    stats = {}
    for (m, s, r), g in trades.groupby(["markt", "setup", "richting"]):
        stats[f"{m}|{s}|{r}"] = {"n": int(len(g)), "winrate": float((g["resultaat"] == "winst").mean()),
                                 "avg_r": float(g["R"].mean())}
    return stats


def judge(st: dict | None, cfg: Config) -> str:
    """Oordeel op basis van de verwachte R per trade na kosten (winrate alleen zegt weinig bij een 2R-doel)."""
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
    if not data.get("na_kosten"):
        print("Let op: slagingspercentages zijn nog zonder kosten berekend. Draai --backtest opnieuw.")
    return data


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


def stock_intraday(t: str, period: str = "5d") -> pd.DataFrame:
    import yfinance as yf
    return closed_only(_norm(yf.Ticker(t).history(period=period, interval="5m", prepost=False)), 5)


def stock_daily(t: str, period: str) -> pd.DataFrame:
    import yfinance as yf
    return _norm(yf.Ticker(t).history(period=period, interval="1d"))


def _ohlcv_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.sort_index()


def crypto_exchange(exchange_id: str):
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    ex.load_markets()
    return ex


def crypto_symbols(ex, quote: str, top_n: int, symbols: str | None, movers: int = 10) -> list[str]:
    """Top N op volume, plus de grootste stijgers/dalers van vandaag met genoeg volume."""
    if symbols:
        return [s.strip() for s in symbols.split(",")]
    tickers = ex.fetch_tickers()
    rows = [(s, tk.get("quoteVolume") or 0, tk.get("percentage") or 0) for s, tk in tickers.items()
            if s.endswith("/" + quote) and s.split("/")[0] not in STABLECOINS]
    by_vol = [s for s, _, _ in sorted(rows, key=lambda x: x[1], reverse=True)[:top_n]]
    liquid = [r for r in rows if r[1] >= 250_000 and r[0] not in by_vol]
    by_move = [s for s, _, _ in sorted(liquid, key=lambda x: abs(x[2]), reverse=True)[:movers]]
    return by_vol + by_move


def crypto_intraday(ex, sym: str, days: float) -> pd.DataFrame:
    since = ex.milliseconds() - int(days * 24 * 3600 * 1000)
    rows = []
    for _ in range(100):  # pagineren
        batch = ex.fetch_ohlcv(sym, "5m", since=since, limit=1000)
        if not batch:
            break
        rows += batch
        if batch[-1][0] >= ex.milliseconds() - 10 * 60 * 1000 or batch[-1][0] + 1 <= since:
            break
        since = batch[-1][0] + 1
    return closed_only(_ohlcv_df(rows), 5)


def crypto_daily(ex, sym: str, limit: int) -> pd.DataFrame:
    return _ohlcv_df(ex.fetch_ohlcv(sym, "1d", limit=limit))


def _parallel(fn, items, workers=8):
    def safe(x):
        try:
            return fn(x)
        except Exception as e:  # één kapot symbool mag de scan niet stoppen
            print(f"  ! {x}: {e}", file=sys.stderr)
            return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for res in pool.map(safe, items) for r in res]


def benchmark_for(t: str) -> str:
    return "^AEX" if t.upper().endswith(EU_SUFFIXES) else "SPY"


def stock_regimes(cfg: Config, period: str = "5d") -> dict[str, pd.Series]:
    out = {}
    for b in ("SPY", "^AEX"):
        try:
            out[b] = regime_series(stock_intraday(b, period), cfg.regime_drop_index)
        except Exception as e:
            print(f"  ! marktfilter {b}: {e}", file=sys.stderr)
            out[b] = pd.Series(dtype=float)
    return out


def last_regime(reg: pd.Series | None) -> float:
    return float(reg.iloc[-1]) if reg is not None and len(reg) else 0.0


def dynamic_stocks(state: dict) -> list[str]:
    """Actieve US-aandelen van vandaag (grootste stijgers/dalers en meest verhandeld), elk half uur ververst."""
    cache = state.get("dyn")
    if cache and (now_utc() - pd.Timestamp(cache["t"])) < pd.Timedelta(minutes=30):
        return cache["tickers"]
    syms = []
    try:
        import yfinance as yf
        for q in ("day_gainers", "day_losers", "most_actives"):
            res = yf.screen(q, count=25)
            for qd in (res.get("quotes", []) if isinstance(res, dict) else []):
                price = qd.get("regularMarketPrice") or 0
                vol = qd.get("averageDailyVolume3Month") or qd.get("regularMarketVolume") or 0
                if qd.get("symbol") and price >= 5 and vol >= 1_000_000:
                    syms.append(qd["symbol"])
    except Exception as e:
        print(f"  ! dynamische watchlist: {e}", file=sys.stderr)
    syms = list(dict.fromkeys(syms))
    state["dyn"] = {"t": now_utc().isoformat(), "tickers": syms}
    return syms


def earnings_soon(t: str, state: dict) -> bool:
    """True als het bedrijf vandaag of de volgende handelsdag cijfers publiceert (per dag gecachet)."""
    today = dt.date.today()
    cache = state.setdefault("cijfers", {})
    if cache.get("_datum") != today.isoformat():
        cache.clear()
        cache["_datum"] = today.isoformat()
    if t in cache:
        return cache[t]
    soon = False
    try:
        import yfinance as yf
        df = yf.Ticker(t).get_earnings_dates(limit=12)
        horizon = today + dt.timedelta(days=3 if today.weekday() == 4 else 1)
        soon = any(today <= ts.date() <= horizon for ts in (df.index if df is not None else []))
    except Exception as e:
        print(f"  ! cijfersdatum {t}: {e}", file=sys.stderr)
    cache[t] = bool(soon)
    return bool(soon)


def scan_stocks(tickers: list[str], cfg: Config, include_closed: bool, regimes: dict) -> list[dict]:
    def one(t):
        intra, daily = stock_intraday(t), stock_daily(t, "3mo")
        if intra.empty or daily.empty:
            return []
        stale = now_utc() - intra.index[-1].tz_convert("UTC") > pd.Timedelta(minutes=30)
        if stale and not include_closed:
            return []
        sigs = analyze(t, "aandeel", intra, daily, cfg, last_regime(regimes.get(benchmark_for(t))))
        for s in sigs:
            if stale:
                s["opmerking"] = "; ".join(x for x in (s["opmerking"], "MARKT DICHT – oude data") if x)
        return sigs
    return _parallel(one, tickers, workers=4)  # rustig aan i.v.m. rate limits van Yahoo


def scan_crypto(cfg: Config, args, ex=None) -> list[dict]:
    ex = ex or crypto_exchange(args.exchange)
    syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols)
    btc = f"BTC/{args.quote}"
    try:
        reg = last_regime(regime_series(crypto_intraday(ex, btc, 1.2), cfg.regime_drop_crypto))
    except Exception as e:
        print(f"  ! marktfilter {btc}: {e}", file=sys.stderr)
        reg = 0.0
    if reg < 0:
        print(f"Marktfilter: {btc} daalt hard, geen crypto-longs.")

    def one(sym):
        intra, daily = crypto_intraday(ex, sym, 3), crypto_daily(ex, sym, 60)
        return analyze(sym, "crypto", intra, daily, cfg, reg) if len(intra) and len(daily) else []
    return _parallel(one, syms, workers=1)  # sequentieel i.v.m. rate limits


def variant_text(v: dict | None) -> str:
    if not v:
        return "-"
    parts = [f"stop ≥ {v['min_stop_atr']:g}× ATR", "trendfilter aan" if v["trend_filter"] else "trendfilter uit",
             "stop naar instap na +1R" if v["breakeven_r"] > 0 else "geen break-even"]
    if v.get("active_hours"):
        a, b = v["active_hours"]
        parts.append(f"alleen {a + 1}:00–{b + 1}:00 NL")
    return ", ".join(parts)


def run_backtest(args, tickers: list[str], cfg: Config):
    cands = []
    if args.market in ("stocks", "all"):
        print(f"Backtest aandelen ({len(tickers)}, laatste ~60 dagen; dat is het maximum voor 5m-data bij Yahoo)...")
        regs = stock_regimes(cfg, "60d")
        cands += _parallel(lambda t: candidates_symbol(t, "aandeel", stock_intraday(t, "60d"), stock_daily(t, "1y"),
                                                       cfg, regs.get(benchmark_for(t))), tickers, workers=4)
    if args.market in ("crypto", "all"):
        ex = crypto_exchange(args.exchange)
        syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols, movers=0)
        print(f"Backtest crypto ({len(syms)} paren, laatste {args.bt_days} dagen)...")
        btc_reg = regime_series(crypto_intraday(ex, f"BTC/{args.quote}", args.bt_days), cfg.regime_drop_crypto)
        cands += _parallel(lambda s: candidates_symbol(s, "crypto", crypto_intraday(ex, s, args.bt_days),
                                                       crypto_daily(ex, s, args.bt_days + 60), cfg, btc_reg),
                           syms, workers=1)
    swing_st = run_swing_backtest(args, tickers, cfg)
    cdf = pd.DataFrame(cands) if cands else pd.DataFrame(columns=["markt"])
    trades, settings, report = [], {}, []
    tested = [m for m, a in (("aandeel", ("stocks", "all")), ("crypto", ("crypto", "all"))) if args.market in a]
    for market in tested:
        mc = cdf[cdf["markt"] == market]
        if mc.empty:
            settings[market] = {"actief": False}
            report.append((market, None))
            continue
        best, rows = choose_variant(mc, market, cfg)
        print(f"\n{market}: {len(rows)} filtercombinaties getest")
        for r in sorted(rows, key=lambda r: -(r["R_train"] if not np.isnan(r["R_train"]) else -9))[:5]:
            print(f"  {variant_text(r['v'])}: train {r['R_train']:+.2f}R (n={r['n_train']}), "
                  f"test {r['R_test']:+.2f}R (n={r['n_test']})")
        if best is None:
            settings[market] = {"actief": False}
            report.append((market, None))
            continue
        v = best["v"]
        settings[market] = {"actief": best["actief"], **{k: (list(x) if isinstance(x, tuple) else x) for k, x in v.items()},
                            "R_train": best["R_train"], "R_test": best["R_test"],
                            "n_train": best["n_train"], "n_test": best["n_test"]}
        report.append((market, settings[market]))
        t = apply_variant(mc, v, market)
        trades.append(t)
    tdf = pd.concat(trades) if trades else pd.DataFrame(columns=["markt", "setup", "richting", "resultaat", "R"])
    tdf.to_csv(os.path.join(STATE_DIR, "backtest_trades.csv"), index=False)
    stats = summarize(tdf) if len(tdf) else {}
    with open(STATS_FILE, "w") as fh:
        json.dump({"gemaakt": dt.datetime.now().isoformat(timespec="seconds"), "na_kosten": True,
                   "kosten_per_kant": {"aandeel": cfg.fee_stocks, "crypto": cfg.fee_crypto},
                   "instellingen": settings, "stats": stats, "swing_stats": swing_st}, fh, indent=2)
    rows = [{"setup": k.replace("|", " / "), "trades": v["n"], "winrate": f"{v['winrate']*100:.0f}%",
             "verw. R": f"{v['avg_r']:+.2f}", "oordeel": judge(v, cfg)} for k, v in stats.items()]
    if rows:
        print("\n" + pd.DataFrame(rows).sort_values("setup").to_string(index=False))
    print(f"\nNa kosten ({cfg.fee_stocks*100:.2f}% aandelen, {cfg.fee_crypto*100:.2f}% crypto per kant).")
    token, chat = telegram_creds()
    if token and chat:
        lines = []
        for market, titel in (("aandeel", "📈 <b>Aandelen</b>"), ("crypto", "🪙 <b>Crypto</b>")):
            st = settings.get(market)
            if st is None:
                continue
            lines.append(titel)
            if not st.get("actief"):
                lines.append("⛔ <b>Daytrade-signalen UIT</b>: geen instelling gevonden die ook op de controledata "
                             "winst gaf na kosten.")
                if "R_test" in st:
                    lines.append(f"   (beste: {variant_text(st)}; controle {st['R_test']:+.2f}R)")
                continue
            lines.append(f"✅ Actief met: {variant_text(st)}")
            lines.append(f"   kiesperiode {st['R_train']:+.2f}R/trade (n={st['n_train']}), "
                         f"controleperiode <b>{st['R_test']:+.2f}R</b>/trade (n={st['n_test']})")
            for k, v in sorted(stats.items()):
                if k.startswith(market + "|"):
                    _, setup, richting = k.split("|")
                    o = judge(v, cfg)
                    lines.append(f"{ICON[o]} {esc(setup)} ({richting}): {v['winrate']*100:.0f}% win, "
                                 f"<b>{v['avg_r']:+.2f}R</b> · {o} (n={v['n']})")
        sw = []
        for k, v in sorted(swing_st.items()):
            m, setup = k.split("|")
            o = judge_swing(v, cfg)
            ctrl = f", controle {v['avg_r_test']:+.2f}R" if v.get("avg_r_test") is not None else ""
            sw.append(f"{ICON[o]} {'🪙' if m == 'crypto' else '📈'} {esc(setup)}: {v['winrate']*100:.0f}% win, "
                      f"<b>{v['avg_r']:+.2f}R</b>{ctrl} · {o} (n={v['n']}, ± {v['dagen']:.0f} dagen)")
        telegram_send(token, chat,
                      "📊 <b>STATISTIEK – dit is GEEN signaal</b>\n"
                      "Wekelijkse test op echte koersen, na kosten.\n"
                      f"{LIJN}\n🟣 <b>SWING</b> (doel 2R als benadering van je red zone, max. 6 maanden)\n"
                      + ("\n".join(sw) if sw else "• geen data") +
                      f"\n{LIJN}\n⚡ <b>DAYTRADE</b>\n" + "\n".join(lines) + f"\n{LIJN}\n"
                      "🟢 hoog ≥ +0,25R · 🟡 gemiddeld · 🔴 laag (verliesgevend) · ⚪ te weinig data\n"
                      "Ook goede setups verliezen vaak: bij een 2R-doel is ±40% winst al winstgevend.")


# --------------------------------------------------------------------------- #
# Papertrading (daytrade en swing)
# --------------------------------------------------------------------------- #

def load_paper() -> pd.DataFrame:
    if os.path.exists(PAPER_FILE):
        df = pd.read_csv(PAPER_FILE, dtype={"id": int, "sluit_tijd": object, "oordeel": object, "status": object})
        if "type" not in df.columns:
            df["type"] = "daytrade"
        return df
    return pd.DataFrame(columns=PAPER_COLS)


def save_paper(df: pd.DataFrame):
    df.to_csv(PAPER_FILE, index=False)


def open_paper(paper: pd.DataFrame, s, cfg: Config):
    """Opent een papertrade als dat volgens de regels mag. Geeft (paper, id of None, uitleg)."""
    open_ = paper[paper["status"] == "open"]
    if (open_["symbool"] == s["symbool"]).any():
        return paper, None, "al een open papertrade in dit symbool"
    if len(open_) >= cfg.max_positions:
        return paper, None, f"al {cfg.max_positions} open (je regel: max {cfg.max_positions})"
    pid = int(paper["id"].max()) + 1 if len(paper) else 1
    row = {"id": pid, "type": s.get("type", "daytrade"), "markt": s["markt"], "symbool": s["symbool"], "setup": s["setup"], "richting": s["richting"],
           "open_tijd": s["data_tijd"], "prijs": s["prijs"], "stop": s["stop"], "doel": s["doel_2R"],
           "fee": cfg.fee(s["markt"]), "be_r": cfg.breakeven_r if s.get("type", "daytrade") == "daytrade" else 0.0, "oordeel": s.get("oordeel", ""), "status": "open",
           "sluit_tijd": "", "uitstap": np.nan, "R": np.nan}
    return pd.concat([paper, pd.DataFrame([row])], ignore_index=True), pid, "geopend"


def _session_date(ts: pd.Timestamp, market: str, symbol: str):
    tz = "UTC" if market == "crypto" else SESSIONS["eu" if symbol.upper().endswith(EU_SUFFIXES) else "us"][0]
    return ts.tz_convert(tz).date(), tz


def evaluate_paper(trade: dict, bars: pd.DataFrame, now: pd.Timestamp | None = None):
    """Bepaalt of een open papertrade gesloten is. bars = afgesloten 5m-candles. Geeft None of
    (status, uitstap, sluit_tijd, R)."""
    now = now or now_utc()
    opened = pd.Timestamp(trade["open_tijd"])
    day, tz = _session_date(opened, trade["markt"], trade["symbool"])
    after = bars[bars.index > opened]
    same = after[np.array(after.index.tz_convert(tz).date) == day]
    long = trade["richting"] == "long"
    if len(same):
        be = trade.get("be_r", 0.0)
        be = 0.0 if be is None or pd.isna(be) else float(be)
        hit = simulate(same["high"].to_numpy(), same["low"].to_numpy(), same["close"].to_numpy(),
                       0, len(same) - 1, trade["prijs"], trade["stop"], trade["doel"], long, be)
        if hit:
            return hit[0], hit[1], str(same.index[hit[2]]), net_r(trade["prijs"], hit[1], trade["stop"], long, trade["fee"])
    later_bars = len(after) > len(same)
    local_now = now.tz_convert(tz)
    if trade["markt"] == "crypto":
        over = local_now.date() > day
    else:
        _, _, (ch, cm) = SESSIONS["eu" if trade["symbool"].upper().endswith(EU_SUFFIXES) else "us"]
        over = later_bars or local_now.date() > day or (local_now.hour * 60 + local_now.minute) >= ch * 60 + cm + 10
    if not over:
        return None
    exit_ = float(same["close"].iloc[-1]) if len(same) else float(trade["prijs"])
    return "tijd", exit_, str(same.index[-1]) if len(same) else now.isoformat(), \
        net_r(trade["prijs"], exit_, trade["stop"], long, trade["fee"])


def evaluate_swing_paper(trade: dict, daily: pd.DataFrame):
    """Swing-papertrade: stop of 2R-doel op dagcandles na de signaaldag, anders uitstap na ± 6 maanden."""
    opened = pd.Timestamp(trade["open_tijd"])
    after = daily[np.array(daily.index.date) > opened.date()]
    if after.empty:
        return None
    n = min(len(after), SWING_MAX_DAYS)
    hit = simulate(after["high"].to_numpy(), after["low"].to_numpy(), after["close"].to_numpy(),
                   0, n - 1, trade["prijs"], trade["stop"], trade["doel"], True)
    if hit:
        return hit[0], hit[1], str(after.index[hit[2]]), net_r(trade["prijs"], hit[1], trade["stop"], True, trade["fee"])
    if len(after) >= SWING_MAX_DAYS:
        exit_ = float(after["close"].iloc[n - 1])
        return "tijd", exit_, str(after.index[n - 1]), net_r(trade["prijs"], exit_, trade["stop"], True, trade["fee"])
    return None


def update_paper(paper: pd.DataFrame, args, ex_getter) -> tuple[pd.DataFrame, list[dict]]:
    closed = []
    for idx in paper.index[paper["status"] == "open"]:
        t = paper.loc[idx].to_dict()
        try:
            if t.get("type") == "swing":
                d = crypto_daily(ex_getter(), t["symbool"], 400) if t["markt"] == "crypto" else stock_daily(t["symbool"], "1y")
                res = evaluate_swing_paper(t, d)
                if res:
                    paper.loc[idx, ["status", "uitstap", "sluit_tijd", "R"]] = [res[0], res[1], res[2], res[3]]
                    closed.append(paper.loc[idx].to_dict())
                continue
            if t["markt"] == "crypto":
                ex = ex_getter()
                bars = closed_only(_ohlcv_df(ex.fetch_ohlcv(
                    t["symbool"], "5m", since=int(pd.Timestamp(t["open_tijd"]).timestamp() * 1000), limit=1000)), 5)
            else:
                bars = stock_intraday(t["symbool"])
            res = evaluate_paper(t, bars)
        except Exception as e:
            print(f"  ! papertrade {t['symbool']}: {e}", file=sys.stderr)
            continue
        if res:
            paper.loc[idx, ["status", "uitstap", "sluit_tijd", "R"]] = [res[0], res[1], res[2], res[3]]
            closed.append(paper.loc[idx].to_dict())
    return paper, closed


def paper_progress(paper: pd.DataFrame) -> list[str]:
    done = paper[paper["status"] != "open"]
    out = []
    for (m, s, r), g in done.groupby(["markt", "setup", "richting"]):
        out.append(f"{m} {s} ({r}): {len(g)}/30, gem. {g['R'].astype(float).mean():+.2f}R")
    return out


# --------------------------------------------------------------------------- #
# Swing-scanner (één keer per dag)
# --------------------------------------------------------------------------- #

def to_weekly(daily: pd.DataFrame, rule: str) -> pd.DataFrame:
    w = daily.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    w = w.dropna(subset=["close"])
    if len(w) and w.index[-1].date() > daily.index[-1].date():   # lopende week weglaten
        w = w.iloc[:-1]
    return w


SWING_MAX_DAYS = 126          # ± 6 maanden aan handelsdagen (je regel: 1–6 maanden vasthouden)
SWING_RELATIE = {
    "Boven EMA Ribbon (dag)": "lijkt op de EMA Ribbon-voorwaarde van je 🟢 daily-alert (de MONTHLY-voorwaarde controleert de bot niet)",
    "WaveTrend-omslag in oversold (week)": "lijkt op je 🟢 weekly-alert (VuManChu), maar is NIET identiek: check je indicator",
}
SWING_STOP_UITLEG = {
    "Boven EMA Ribbon (dag)": "onderkant van de EMA Ribbon op de signaaldag",
    "WaveTrend-omslag in oversold (week)": "laagste low van de laatste 8 weken",
}


def swing_events(daily: pd.DataFrame, market: str, cfg: Config) -> list[tuple[int, str, float]]:
    """Alle swing-signalen in een reeks afgesloten dagcandles: (positie van de signaaldag, setup, stop).
    De live scan kijkt alleen naar de laatste dag, de backtest naar alle dagen: dezelfde logica."""
    out = []
    if len(daily) < 80:
        return out
    c = daily["close"]
    ribbon = pd.concat([ema(c, n) for n in range(20, 60, 5)], axis=1)   # EMA 20,25,...,55
    top, bot = ribbon.max(axis=1).to_numpy(), ribbon.min(axis=1).to_numpy()
    above = c.to_numpy() > top
    for pos in np.where(above[1:] & ~above[:-1])[0] + 1:
        if pos >= 79:          # zelfde minimum (80 dagen) als de live scan
            out.append((int(pos), "Boven EMA Ribbon (dag)", float(bot[pos])))
    weekly = to_weekly(daily, "W-SUN" if market == "crypto" else "W-FRI")
    if len(weekly) >= 30:
        wt1, wt2 = (x.to_numpy() for x in wavetrend(weekly))
        ddates = _dates(daily.index)
        lows = weekly["low"].to_numpy()
        for k in range(30, len(weekly)):
            if wt1[k - 1] <= wt2[k - 1] and wt1[k] > wt2[k] and wt1[k] < cfg.wt_oversold:
                pos = int(np.searchsorted(ddates, np.datetime64(weekly.index[k].date(), "D"), side="right") - 1)
                if 0 <= pos < len(daily):
                    out.append((pos, "WaveTrend-omslag in oversold (week)", float(lows[k - 7:k + 1].min())))
    return sorted(out)


def swing_rules(symbol: str, market: str, daily: pd.DataFrame, cfg: Config) -> list[dict]:
    """Swing-signalen op de laatste afgesloten dagcandle."""
    sigs = []
    last = len(daily) - 1
    for pos, setup, stop in swing_events(daily, market, cfg):
        if pos != last:
            continue
        entry = float(daily["close"].iloc[pos])
        risk = entry - stop
        if risk <= 0:
            continue
        qty, value, risk_eur, note = size_position(entry, stop, cfg, fractional=(market == "crypto"))
        sigs.append({"type": "swing", "markt": market, "symbool": symbol, "setup": setup, "richting": "long",
                     "prijs": entry, "stop": stop, "stop_uitleg": SWING_STOP_UITLEG[setup],
                     "min_red_zone": entry + cfg.min_rr * risk, "doel_2R": entry + cfg.min_rr * risk,
                     "stop_pct": risk / entry * 100, "aantal": qty, "positiewaarde": value, "risico": risk_eur,
                     "relatie": SWING_RELATIE[setup], "opmerking": note, "datum": str(daily.index[pos].date()),
                     "data_tijd": daily.index[pos].tz_convert("UTC").isoformat() if daily.index.tz else str(daily.index[pos])})
    return sigs


def swing_backtest_symbol(symbol: str, market: str, daily: pd.DataFrame, cfg: Config) -> list[dict]:
    """Speelt elk swing-signaal na: instap op het dagslot, stop zoals in het signaal, doel 2R (en ter vergelijking 3R)
    als benadering van je red zone, uitstap na max. ± 6 maanden. Kosten afgetrokken. Lopende trades tellen niet mee."""
    H, L, C = daily["high"].to_numpy(), daily["low"].to_numpy(), daily["close"].to_numpy()
    fee, n = cfg.fee(market), len(daily)
    out = []
    for pos, setup, stop in swing_events(daily, market, cfg):
        entry = C[pos]
        risk = entry - stop
        if risk <= 0 or risk / entry > 0.30:
            continue
        rec = {"markt": market, "symbool": symbol, "setup": setup, "datum": str(daily.index[pos].date())}
        for tgt in (2, 3):
            end = min(pos + SWING_MAX_DAYS, n - 1)
            hit = simulate(H, L, C, pos + 1, end, entry, stop, entry + tgt * risk, True)
            if hit is None and pos + SWING_MAX_DAYS > n - 1:
                rec = None        # nog lopend: uitkomst onbekend
                break
            res, exit_ = (hit[0], hit[1]) if hit else ("tijd", C[end])
            rec[f"res{tgt}"], rec[f"R{tgt}"] = res, net_r(entry, exit_, stop, True, fee)
            rec[f"dagen{tgt}"] = (hit[2] if hit else end) - pos
        if rec:
            out.append(rec)
    return out


def summarize_swing(tr: pd.DataFrame) -> dict:
    """Per markt/setup: resultaat met 2R-doel, ter vergelijking 3R, en een controle op de laatste 30% van de tijd."""
    out = {}
    for (m, s), g in tr.groupby(["markt", "setup"]):
        g = g.sort_values("datum")
        split = g["datum"].iloc[int(len(g) * 0.7)] if len(g) >= 10 else None
        test = g[g["datum"] >= split] if split else g.iloc[0:0]
        out[f"{m}|{s}"] = {"n": int(len(g)), "winrate": float((g["res2"] == "winst").mean()),
                           "avg_r": float(g["R2"].mean()), "avg_r3": float(g["R3"].mean()),
                           "winrate3": float((g["res3"] == "winst").mean()),
                           "dagen": float(g["dagen2"].median()),
                           "n_test": int(len(test)), "avg_r_test": float(test["R2"].mean()) if len(test) else None}
    return out


def judge_swing(st: dict | None, cfg: Config) -> str:
    o = judge(st, cfg)
    if o in ("hoog", "gemiddeld") and st.get("n_test", 0) >= 10 and (st.get("avg_r_test") or 0) <= 0:
        return "laag"           # winst verdween in de controleperiode
    return o


def run_swing_backtest(args, tickers: list[str], cfg: Config) -> dict:
    tr = []
    if args.market in ("stocks", "all"):
        print(f"\nSwing-backtest aandelen ({len(tickers)}, 10 jaar dagdata)...")
        tr += _parallel(lambda t: swing_backtest_symbol(t, "aandeel", stock_daily(t, "10y"), cfg), tickers, workers=4)
    if args.market in ("crypto", "all"):
        ex = crypto_exchange(args.exchange)
        syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols, movers=0)
        print(f"Swing-backtest crypto ({len(syms)} paren, max. ± 4 jaar dagdata)...")
        tr += _parallel(lambda s: swing_backtest_symbol(s, "crypto", crypto_daily(ex, s, 1440), cfg), syms, workers=1)
    if not tr:
        return {}
    tdf = pd.DataFrame(tr)
    tdf.to_csv(os.path.join(STATE_DIR, "swing_backtest_trades.csv"), index=False)
    st = summarize_swing(tdf)
    for k, v in st.items():
        print(f"  {k.replace('|', ' / ')}: n={v['n']}, 2R: {v['winrate']*100:.0f}% win {v['avg_r']:+.2f}R, "
              f"3R: {v['avg_r3']:+.2f}R, controle {v['avg_r_test'] if v['avg_r_test'] is None else round(v['avg_r_test'], 2)}R, "
              f"mediaan {v['dagen']:.0f} dagen → {judge_swing(v, cfg)}")
    return st


def swing_scan(cfg: Config, args, tickers: list[str]) -> list[dict]:
    sigs = []
    today_utc = now_utc().date()
    if args.market in ("stocks", "all"):
        def one(t):
            d = stock_daily(t, "2y")
            if len(d) and d.index[-1].date() >= dt.datetime.now(d.index.tz).date() and market_open(t):
                d = d.iloc[:-1]     # dagcandle van vandaag is nog niet af
            return swing_rules(t, "aandeel", d, cfg)
        sigs += _parallel(one, tickers, workers=4)
    if args.market in ("crypto", "all"):
        ex = crypto_exchange(args.exchange)
        syms = crypto_symbols(ex, args.quote, args.crypto_top, args.crypto_symbols, movers=0)
        def one_c(s):
            d = crypto_daily(ex, s, 500)
            if len(d) and d.index[-1].date() >= today_utc:
                d = d.iloc[:-1]
            return swing_rules(s, "crypto", d, cfg)
        sigs += _parallel(one_c, syms, workers=1)
    return sigs


def log_swing(sigs: list[dict]) -> list[dict]:
    if not sigs:
        return []
    df = pd.DataFrame(sigs)
    key = lambda d: d["datum"].astype(str) + "|" + d["symbool"] + "|" + d["setup"]
    if os.path.exists(SWING_LOG):
        old = pd.read_csv(SWING_LOG, usecols=["datum", "symbool", "setup"], dtype=str)
        df = df[~key(df).isin(set(key(old)))]
    if len(df):
        df.to_csv(SWING_LOG, mode="a", header=not os.path.exists(SWING_LOG), index=False)
    return df.to_dict("records")


# --------------------------------------------------------------------------- #
# Uitvoer: tabel, log, Telegram
# --------------------------------------------------------------------------- #

def fmt_price(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.6g}"


def print_table(df: pd.DataFrame, top: int):
    if df.empty:
        print(f"{DAY}: geen kansen die aan alle filters voldoen.")
        return
    print(f"{DAY} ({DAY_UITLEG}):")
    view = df.head(top).copy()
    for col in ("prijs", "stop", "doel_2R", "volgend_level", "positiewaarde", "risico"):
        view[col] = view[col].map(fmt_price)
    view["rr_tot_level"] = view["rr_tot_level"].map(lambda v: "open" if v is None or pd.isna(v) else f"{v:.1f}")
    view["rvol"] = view["rvol"].map(lambda v: f"{v:.1f}x")
    view["atr_pct"] = view["atr_pct"].map(lambda v: f"{v:.1f}%")
    view["gap_pct"] = view["gap_pct"].map(lambda v: "-" if pd.isna(v) else f"{v:+.1f}%")
    view["rsi_5m"] = view["rsi_5m"].map(lambda v: f"{v:.0f}")
    view["aantal"] = view["aantal"].map(lambda v: f"{v:g}")
    view["kosten_R"] = view["kosten_R"].map(lambda v: f"{v:.2f}")
    view["verw_R"] = view["verw_R"].map(lambda v: "-" if pd.isna(v) else f"{v:+.2f}")
    cols = ["symbool", "setup", "richting", "prijs", "stop", "doel_2R", "volgend_level", "rr_tot_level",
            "slagingskans", "verw_R", "oordeel", "kosten_R", "rvol", "atr_pct", "gap_pct", "rsi_5m", "aantal",
            "positiewaarde", "risico", "opmerking"]
    print(view[cols].to_string(index=False))


def log_signals(df: pd.DataFrame, path: str) -> pd.DataFrame:
    """Voegt nieuwe signalen toe aan een CSV. Geeft de nieuwe terug (per dag/symbool/setup één keer)."""
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
        new.to_csv(path, mode="a", header=not os.path.exists(path), index=False)
    return new


def discover_chat_id(token: str) -> str | None:
    """Zoekt de chat-id zelf op via de laatste berichten aan de bot en onthoudt hem."""
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE) as fh:
            return fh.read().strip() or None
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10) as r:
            updates = json.load(r).get("result", [])
    except Exception as e:
        print(f"  ! Telegram: kon chat-id niet ophalen ({e})", file=sys.stderr)
        return None
    chats = [u[k]["chat"]["id"] for u in updates for k in ("message", "my_chat_member") if k in u]
    if not chats:
        print("  ! Telegram: geen chat gevonden. Stuur je bot een bericht (bv. 'hoi').", file=sys.stderr)
        return None
    chat = str(chats[-1])
    with open(CHAT_ID_FILE, "w") as fh:
        fh.write(chat)
    telegram_send(token, chat, "✅ <b>Scanner gekoppeld.</b> Nieuwe signalen komen hier binnen.")
    return chat


def telegram_creds():
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if (not token or not chat) and os.path.exists(TELEGRAM_FILE):
        with open(TELEGRAM_FILE) as fh:
            c = json.load(fh)
        token, chat = token or c.get("token"), chat or c.get("chat_id")
    token = (token or "").strip() or None
    if token and not chat:
        chat = discover_chat_id(token)
    return token, chat


def telegram_send(token: str | None, chat: str | None, text: str) -> bool:
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
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
    ok = telegram_send(token, chat, "✅ <b>Scanner gekoppeld.</b> Nieuwe signalen komen hier binnen.")
    print(f"Opgeslagen in {TELEGRAM_FILE} (chat-id {chat}). Testbericht {'verstuurd' if ok else 'MISLUKT'}.")


LIJN = "━━━━━━━━━━━━━━━━"
SETUP_UITLEG = {
    "ORB": "uitbraak uit de eerste 15 minuten van de dag",
    "Breakout high gisteren": "koers breekt door de hoogste koers van gisteren",
    "Breakdown low gisteren": "koers zakt door de laagste koers van gisteren",
    "VWAP-pullback": "terugval naar de gemiddelde dagkoers (VWAP) die standhoudt",
    "VWAP-afwijzing": "koers stuit op de gemiddelde dagkoers (VWAP) en draait omlaag",
    "Boven EMA Ribbon (dag)": "dagslot voor het eerst boven alle 8 EMA's (20 t/m 55)",
    "WaveTrend-omslag in oversold (week)": "WaveTrend draait omhoog vanuit oversold op de weekkaart",
}


def esc(x) -> str:
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cur(symbol: str, market: str) -> str:
    if market == "crypto":
        return {"EUR": "€", "USD": "$", "USDT": "$", "USDC": "$"}.get(symbol.split("/")[-1], "")
    return "€" if symbol.upper().endswith(EU_SUFFIXES) else "$"


def money(x, symbol: str, market: str) -> str:
    return f"{cur(symbol, market)}{fmt_price(float(x))}"


def pct(a, b) -> str:
    return f"{(float(b) / float(a) - 1) * 100:+.1f}%".replace(".", ",")


def chart_link(symbol: str, market: str) -> str:
    if market == "crypto":
        tv = "BITVAVO:" + symbol.replace("/", "")
    elif symbol.upper().endswith(".AS"):
        tv = "EURONEXT:" + symbol.split(".")[0]
    else:
        tv = symbol
    return f'<a href="https://www.tradingview.com/chart/?symbol={urllib.parse.quote(tv)}">📈 Open grafiek op TradingView</a>'


def kop(symbol: str, market: str, richting: str) -> str:
    markt = "crypto" if market == "crypto" else "aandeel"
    actie = "🟢 KOPEN (long)" if richting == "long" else "🔻 SHORTEN (short)"
    return f"<b>{esc(symbol)}</b>  ·  {markt}\n{actie}"


def msg_daytrade(s, paper_note: str) -> str:
    sym, m = s["symbool"], s["markt"]
    o = s.get("oordeel", "te weinig data")
    waarschuwing = ""
    if o == "laag":
        waarschuwing = "\n⛔ <b>Deze setup was verliesgevend in de backtest.</b> Alleen papertraden."
    return (f"⚡ <b>DAYTRADE</b> · vandaag in én vandaag uit\n{LIJN}\n"
            f"{kop(sym, m, s['richting'])}\n"
            f"Setup: <b>{esc(s['setup'])}</b> – {SETUP_UITLEG.get(s['setup'], '')}\n{LIJN}\n"
            f"💶 Instap: <b>{money(s['prijs'], sym, m)}</b>\n"
            f"🛑 Stop: <b>{money(s['stop'], sym, m)}</b> ({pct(s['prijs'], s['stop'])})\n"
            f"🎯 Doel: <b>{money(s['doel_2R'], sym, m)}</b> ({pct(s['prijs'], s['doel_2R'])})\n"
            f"📦 Aantal: {s['aantal']:g} · inzet {money(s['positiewaarde'], sym, m)} · risico {money(s['risico'], sym, m)}\n"
            f"{LIJN}\n"
            f"{ICON.get(o, '')} Slagingskans: <b>{esc(s['slagingskans'])} · {o}</b> (na kosten)\n"
            f"💸 Kosten ≈ {s['kosten_R']:.2f}R  ·  RVOL {s['rvol']:.1f}x"
            + (f"\n⚠️ {esc(s['opmerking'])}" if s["opmerking"] else "")
            + waarschuwing
            + f"\n📝 Papertrade: {esc(paper_note)}\n"
            f"⏰ Vandaag niet gevuld of nog open bij slot? Dan sluiten/vervallen.\n"
            f"{chart_link(sym, m)}")


def msg_paper_closed(t: dict) -> str:
    swing = t.get("type") == "swing"
    icon = {"winst": "✅", "verlies": "❌", "tijd": "⏱", "break-even": "➖"}[t["status"]]
    uitleg = {"winst": "doel geraakt" + (" (2R, benadering red zone)" if swing else ""), "verlies": "stop geraakt",
              "break-even": "gesloten op instapprijs (break-even)",
              "tijd": "6 maanden voorbij, gesloten op slotkoers" if swing else "einde dag, gesloten op slotkoers"}[t["status"]]
    sym, m = t["symbool"], t["markt"]
    soort = "🟣 swing" if swing else "⚡ daytrade"
    return (f"{icon} <b>PAPERTRADE #{int(t['id'])} GESLOTEN</b> · {soort}\n{LIJN}\n"
            f"<b>{esc(sym)}</b> · {esc(t['setup'])} · {t['richting']}\n"
            f"{uitleg.capitalize()}\n"
            f"In {money(t['prijs'], sym, m)} → uit {money(float(t['uitstap']), sym, m)}\n"
            f"Resultaat: <b>{float(t['R']):+.2f}R</b> na kosten")


def msg_swing(s: dict, st: dict | None = None, paper_note: str = "", cfg: Config | None = None) -> str:
    sym, m = s["symbool"], s["markt"]
    cfg = cfg or Config()
    o = judge_swing(st, cfg)
    if st:
        ctrl = f" · controleperiode {st['avg_r_test']:+.2f}R" if st.get("avg_r_test") is not None else ""
        kans = (f"{ICON[o]} Slagingskans: <b>{st['winrate']*100:.0f}% (n={st['n']}) · {o}</b>\n"
                f"     gem. <b>{st['avg_r']:+.2f}R</b> bij 2R-doel{ctrl}; bij 3R-doel {st['avg_r3']:+.2f}R\n"
                f"     doorlooptijd ± {st['dagen']:.0f} handelsdagen")
    else:
        kans = "⚪ Slagingskans: nog geen backtest"
    waarschuwing = "\n⛔ <b>Deze setup was verliesgevend in de backtest.</b> Alleen papertraden." if o == "laag" else ""
    return (f"🟣 <b>SWING</b> · dagen tot maanden vasthouden\n"
            f"<i>Dit is GEEN daytrade.</i>\n{LIJN}\n"
            f"{kop(sym, m, 'long')}\n"
            f"Setup: <b>{esc(s['setup'])}</b> – {SETUP_UITLEG.get(s['setup'], '')}\n{LIJN}\n"
            f"💶 Slotkoers: <b>{money(s['prijs'], sym, m)}</b>\n"
            f"🛑 Stop: <b>{money(s['stop'], sym, m)}</b> ({pct(s['prijs'], s['stop'])}) – {esc(s['stop_uitleg'])}\n"
            f"🎯 Doel: <b>jouw red zone</b> (Fib, zelf tekenen)\n"
            f"     ➜ alleen nemen als die boven <b>{money(s['min_red_zone'], sym, m)}</b> ligt (1:2)\n"
            f"📦 Aantal: {s['aantal']:g} · inzet {money(s['positiewaarde'], sym, m)} · risico {money(s['risico'], sym, m)}\n"
            f"{LIJN}\n"
            f"{kans}{waarschuwing}\n"
            f"ℹ️ {esc(s['relatie'])}.\n"
            + (f"📝 Papertrade: {esc(paper_note)}\n" if paper_note else "") +
            f"⚠️ Eerst checken op je chart.\n"
            f"{chart_link(sym, m)}")


def msg_summary(day_df: pd.DataFrame, paper: pd.DataFrame, swing_today: list[dict], uit: list[str] = ()) -> str:
    today = dt.datetime.now(AMS).date()
    lines = [f"📋 <b>DAGOVERZICHT {today:%d-%m-%Y}</b>", LIJN, "⚡ <b>Daytrade-signalen vandaag</b>"]
    for m in uit:
        lines.append(f"⛔ {m}: daytrade staat uit (geen winstgevende instelling na kosten in de backtest)")
    if len(day_df):
        for _, s in day_df.iterrows():
            o = s.get("oordeel", "")
            act = "kopen" if s["richting"] == "long" else "shorten"
            lines.append(f"{ICON.get(o, '•')} <b>{esc(s['symbool'])}</b> – {act} – {esc(s['setup'])}")
    else:
        lines.append("• geen")
    done = paper[paper["status"] != "open"]
    done = done[pd.to_datetime(done["sluit_tijd"], utc=True, errors="coerce").dt.tz_convert(AMS).dt.date == today]
    lines += ["", "📝 <b>Papertrades vandaag gesloten</b>"]
    if len(done):
        for _, t in done.iterrows():
            icon = {"winst": "✅", "verlies": "❌", "tijd": "⏱", "break-even": "➖"}.get(t["status"], "•")
            lines.append(f"{icon} #{int(t['id'])} <b>{esc(t['symbool'])}</b> {esc(t['setup'])}: <b>{float(t['R']):+.2f}R</b>")
        lines.append(f"Totaal vandaag: <b>{done['R'].astype(float).sum():+.2f}R</b>")
    else:
        lines.append("• geen")
    open_ = paper[paper["status"] == "open"]
    lines += ["", f"📂 <b>Papertrades nog open: {len(open_)}/3</b>"]
    lines += [f"{'🟣' if t.get('type') == 'swing' else '⚡'} #{int(t['id'])} <b>{esc(t['symbool'])}</b> {esc(t['setup'])}"
              for _, t in open_.iterrows()]
    prog = paper_progress(paper)
    if prog:
        lines += ["", "📈 <b>Op weg naar 30 papertrades per setup</b>"] + [f"• {esc(p)}" for p in prog]
    lines += ["", LIJN, "🟣 <b>Swing-signalen</b> (dagen tot maanden)"]
    lines += [f"• <b>{esc(s['symbool'])}</b> – {esc(s['setup'])}" for s in swing_today] or ["• geen nieuwe"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if os.path.exists(DAY_STATE):
        with open(DAY_STATE) as fh:
            return json.load(fh)
    return {}


def save_state(state: dict):
    with open(DAY_STATE, "w") as fh:
        json.dump(state, fh)


def summary_due(state: dict, cfg: Config) -> bool:
    now = dt.datetime.now(AMS)
    h, m = cfg.summary_time
    return (now.hour, now.minute) >= (h, m) and state.get("laatste_overzicht") != now.date().isoformat()


def market_cfg(cfg: Config, data: dict, market: str) -> tuple[Config, bool]:
    """Past de door de backtest gekozen instellingen toe. Geeft (config, actief)."""
    st = (data or {}).get("instellingen", {}).get(market)
    if not st:
        return cfg, True          # nog geen keuze gemaakt: standaardinstellingen
    keys = ("min_stop_atr", "trend_filter", "breakeven_r", "active_hours", "max_cost_r")
    upd = {k: (tuple(st[k]) if k == "active_hours" and st.get(k) else st.get(k)) for k in keys if k in st}
    return replace(cfg, **upd), bool(st.get("actief", False))


def run_once(args, cfg: Config, tickers: list[str], data: dict):
    print(f"\n=== Scan {dt.datetime.now(AMS):%Y-%m-%d %H:%M} (NL) ===")
    stats = (data or {}).get("stats", {})
    cfg_s, actief_s = market_cfg(cfg, data, "aandeel")
    cfg_c, actief_c = market_cfg(cfg, data, "crypto")
    if args.force_active:
        actief_s = actief_c = True
    state = load_state()
    token, chat = telegram_creds()
    ex_cache = {}
    get_ex = lambda: ex_cache.setdefault("ex", crypto_exchange(args.exchange))

    # 1. Open papertrades bijwerken
    paper = load_paper()
    paper, closed = update_paper(paper, args, get_ex)
    for t in closed:
        print(f"Papertrade #{int(t['id'])} {t['symbool']} gesloten: {t['status']} {float(t['R']):+.2f}R")
        telegram_send(token, chat, msg_paper_closed(t))

    # 2. Daytrade-scan
    sigs = []
    if args.market in ("stocks", "all") and not actief_s:
        print("Daytrade aandelen staat UIT: de backtest vond geen instelling die na kosten winst gaf.")
    if args.market in ("stocks", "all") and actief_s:
        all_t = list(dict.fromkeys(tickers + ([] if args.no_dynamic else
                                              (dynamic_stocks(state) if market_open("SPY") else []))))
        open_t = all_t if args.include_closed else [t for t in all_t if market_open(t)]
        if open_t:
            print(f"Aandelen scannen ({len(open_t)} met open beurs, waarvan {len(open_t) - len([t for t in open_t if t in tickers])} uit de dynamische watchlist)...")
            regs = stock_regimes(cfg)
            for b, r in regs.items():
                if last_regime(r) < 0:
                    print(f"Marktfilter: {b} daalt hard, geen longs in die markt.")
            sigs += scan_stocks(open_t, cfg_s, args.include_closed, regs)
        else:
            print("Aandelenbeurzen zijn dicht, aandelen overgeslagen.")
    if args.market in ("crypto", "all") and not actief_c:
        print("Daytrade crypto staat UIT: de backtest vond geen instelling die na kosten winst gaf.")
    if args.market in ("crypto", "all") and actief_c:
        print(f"Crypto scannen op {args.exchange} (top {args.crypto_top} {args.quote}-paren + grootste bewegers)...")
        sigs += scan_crypto(cfg_c, args, get_ex())
    df = pd.DataFrame(sigs)
    if not df.empty:
        if not args.show_all:
            df = df[df["rr_ok"]]
        if not df.empty and not args.no_earnings:
            keep = [not (r["markt"] == "aandeel" and earnings_soon(r["symbool"], state)) for _, r in df.iterrows()]
            skipped = df[[not k for k in keep]]
            for _, r in skipped.iterrows():
                print(f"Overgeslagen: {r['symbool']} publiceert vandaag/morgen cijfers.")
            df = df[keep]
        df = add_stats(df, stats, cfg) if not df.empty else df
    if not df.empty:
        df["_rank"] = df["oordeel"].map(JUDGE_RANK)
        df = df.sort_values(["_rank", "score"], ascending=False).drop(columns="_rank").reset_index(drop=True)
    print_table(df, args.top)
    new = log_signals(df, args.log)
    if not new.empty:
        print(f"{len(new)} nieuwe signalen gelogd in {args.log}")
        send = new if args.telegram_min == "alle" else new[new["oordeel"].map(JUDGE_RANK) >= JUDGE_RANK[args.telegram_min]]
        for _, s in send.iterrows():
            paper, pid, why = open_paper(paper, s, cfg_c if s["markt"] == "crypto" else cfg_s)
            note = f"#{pid} geopend – de bot houdt bij of stop of doel geraakt wordt" if pid else f"niet geopend: {why}"
            telegram_send(token, chat, msg_daytrade(s, note))
    save_paper(paper)

    # 3. Eén keer per dag: swing-scan + dagoverzicht
    if args.swing_now or summary_due(state, cfg):
        print(f"\n{SWING} ({SWING_UITLEG}) scannen...")
        swing_new = log_swing(swing_scan(cfg, args, tickers))
        swing_stats = (data or {}).get("swing_stats", {})
        for s in swing_new:
            print(f"  {s['symbool']}: {s['setup']} (stop {fmt_price(s['stop'])}, red zone moet boven {fmt_price(s['min_red_zone'])})")
            st = swing_stats.get(f"{s['markt']}|{s['setup']}")
            s["oordeel"] = judge_swing(st, cfg)
            paper, pid, why = open_paper(paper, s, cfg)
            note = (f"#{pid} geopend – de bot volgt stop en 2R-doel (max. 6 maanden)" if pid else f"niet geopend: {why}")
            telegram_send(token, chat, msg_swing(s, st, note, cfg))
        save_paper(paper)
        if not swing_new:
            print("  geen nieuwe swing-signalen")
        today_log = pd.read_csv(args.log) if os.path.exists(args.log) else pd.DataFrame()
        if len(today_log):
            today_log = today_log[today_log["datum"].astype(str) == dt.date.today().isoformat()]
        uit = [m for m, a in (("Aandelen", actief_s), ("Crypto", actief_c)) if not a]
        telegram_send(token, chat, msg_summary(today_log, paper, swing_new, uit))
        state["laatste_overzicht"] = dt.datetime.now(AMS).date().isoformat()
    save_state(state)


def main():
    p = argparse.ArgumentParser(description="Trade-scanner (daytrade + swing) voor aandelen en crypto")
    p.add_argument("--market", choices=["stocks", "crypto", "all"], default="all")
    p.add_argument("--account", type=float, default=10_000, help="accountgrootte (default 10000)")
    p.add_argument("--risk-pct", type=float, default=1.0, help="risico per trade in %% (default 1)")
    p.add_argument("--min-rvol", type=float, default=1.5)
    p.add_argument("--min-atr-pct", type=float, default=2.0)
    p.add_argument("--fee-stocks", type=float, default=0.05, help="kosten per kant in %% voor aandelen (default 0.05)")
    p.add_argument("--fee-crypto", type=float, default=0.25, help="kosten per kant in %% voor crypto (default 0.25)")
    p.add_argument("--stocks-file", help="tekstbestand met één ticker per regel")
    p.add_argument("--no-dynamic", action="store_true", help="geen dynamische watchlist (alleen de vaste lijst)")
    p.add_argument("--no-earnings", action="store_true", help="aandelen met cijfers vandaag/morgen niet overslaan")
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
    p.add_argument("--backtest", action="store_true", help="slagingspercentages daytrade-setups berekenen")
    p.add_argument("--bt-days", type=int, default=45, help="aantal dagen crypto-backtest (default 45)")
    p.add_argument("--swing-now", action="store_true", help="swing-scan + dagoverzicht nu uitvoeren")
    p.add_argument("--telegram-setup", metavar="TOKEN", help="koppel je Telegram-bot en stuur een testbericht")
    p.add_argument("--telegram-min", choices=["alle", "gemiddeld", "hoog"], default="gemiddeld",
                   help="alleen daytrade-meldingen (en papertrades) vanaf dit oordeel (default gemiddeld)")
    p.add_argument("--force-active", action="store_true",
                   help="ook daytrade-signalen in markten die de backtest heeft uitgezet")
    args = p.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)

    if args.telegram_setup:
        telegram_setup(args.telegram_setup)
        return
    if args.risk_pct > 1.0:
        print("Waarschuwing: je regel is max 1% risico per trade.")
    cfg = Config(account=args.account, risk_pct=args.risk_pct, min_rvol=args.min_rvol,
                 min_atr_pct=args.min_atr_pct, shorts_stocks=not args.no_stock_shorts,
                 shorts_crypto=args.crypto_shorts, fee_stocks=args.fee_stocks / 100, fee_crypto=args.fee_crypto / 100)
    tickers = DEFAULT_STOCKS
    if args.stocks_file:
        with open(args.stocks_file) as fh:
            tickers = [l.strip().upper() for l in fh if l.strip() and not l.startswith("#")]

    if args.backtest:
        run_backtest(args, tickers, cfg)
        return

    stats = load_stats()
    if not stats.get("stats"):
        print("Nog geen slagingspercentages. Draai eerst: python daytrade_scanner.py --backtest")
    print("Scanner, geen advies. Papertrade een setup 30+ keer voordat je hem live gebruikt.")
    token, chat = telegram_creds()  # koppelt Telegram meteen (ook als er geen signalen zijn)
    if token:
        print(f"Telegram: {'gekoppeld' if chat else 'nog niet gekoppeld – stuur je bot een bericht'}")
    while True:
        run_once(args, cfg, tickers, stats)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()

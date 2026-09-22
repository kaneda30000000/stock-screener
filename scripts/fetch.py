"""株価と決算データを集めて、手元の保管ファイルに貯めていく部分。

初回だけ過去5年分をまとめて取りに行きます（20〜30分かかります）。
2回目以降は「前回から増えた日の分だけ」なので数十秒で終わります。
"""

import os
import sys
import time
import datetime as dt

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from jq import JQuants  # noqa: E402

CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), "..", "cache"))
PRICES = os.path.join(CACHE_DIR, "prices.parquet")
STATEMENTS = os.path.join(CACHE_DIR, "statements.parquet")

PRICE_DAYS = 130        # 株価は直近130営業日ぶんを保持（3か月判定に余裕を持たせる）
STATEMENT_YEARS = 5     # 決算はライトプランの上限いっぱい5年分

PRICE_COLS = [
    "Date", "Code", "Close", "AdjustmentClose", "AdjustmentHigh",
    "AdjustmentLow", "AdjustmentVolume", "TurnoverValue",
]


def _log(msg):
    print(f"[fetch] {msg}", flush=True)


def _load(path):
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception as e:
            _log(f"保管ファイルが読めなかったので作り直します: {e}")
    return None


def _save(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_parquet(path, index=False)


def business_days(api, start, end):
    """営業日の一覧を新しい順で返す。"""
    try:
        cal = api.trading_calendar(start.isoformat(), end.isoformat())
        days = [c["Date"] for c in cal if str(c.get("HolidayDivision")) in ("1", "2")]
        if days:
            return sorted(days, reverse=True)
    except Exception as e:
        _log(f"取引カレンダーが取れなかったので暦日で代用します: {e}")
    days, d = [], end
    while d >= start:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return days


# ---------------------------------------------------------------- 株価
def update_prices(api, today=None):
    today = today or dt.date.today()
    old = _load(PRICES)
    have = set(old["Date"].astype(str)) if old is not None else set()

    # 130営業日ぶんを確保するために、暦日で約200日さかのぼる
    days = business_days(api, today - dt.timedelta(days=200), today)[: PRICE_DAYS + 5]
    need = [d for d in days if d not in have]

    if need:
        _log(f"株価を取りに行きます: {len(need)}日分")
    frames = [old] if old is not None else []
    for i, d in enumerate(need, 1):
        rows = api.daily_quotes_by_date(d)
        if rows:
            df = pd.DataFrame(rows)
            keep = [c for c in PRICE_COLS if c in df.columns]
            frames.append(df[keep])
        if i % 20 == 0:
            _log(f"  {i}/{len(need)}日")
        time.sleep(0.12)

    if not frames:
        raise RuntimeError("株価が1日分も取得できませんでした")

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.drop_duplicates(subset=["Date", "Code"], keep="last")
    # 古い日付を捨てる
    keep_days = sorted(all_df["Date"].astype(str).unique(), reverse=True)[:PRICE_DAYS]
    all_df = all_df[all_df["Date"].astype(str).isin(set(keep_days))]
    for c in ["Close", "AdjustmentClose", "AdjustmentHigh", "AdjustmentLow",
              "AdjustmentVolume", "TurnoverValue"]:
        if c in all_df.columns:
            all_df[c] = pd.to_numeric(all_df[c], errors="coerce")
    _save(all_df, PRICES)
    _log(f"株価の保管ファイル: {len(all_df):,}行 / {len(keep_days)}営業日")
    return all_df


# ---------------------------------------------------------------- 決算
STMT_COLS = [
    "LocalCode", "DisclosedDate", "TypeOfCurrentPeriod", "TypeOfDocument",
    "CurrentFiscalYearEndDate", "CurrentPeriodEndDate",
    "NetSales", "OperatingProfit", "OrdinaryProfit", "Profit",
    "EarningsPerShare", "BookValuePerShare",
    "TotalAssets", "Equity", "EquityToAssetRatio",
    "ForecastNetSales", "ForecastOperatingProfit", "ForecastProfit",
    "ForecastEarningsPerShare", "ForecastDividendPerShareAnnual",
    "ResultDividendPerShareAnnual",
    "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
    "NumberOfTreasuryStockAtTheEndOfFiscalYear",
]


def update_statements(api, today=None):
    today = today or dt.date.today()
    old = _load(STATEMENTS)
    have = set(old["DisclosedDate"].astype(str)) if old is not None else set()

    start = today - dt.timedelta(days=365 * STATEMENT_YEARS + 10)
    days = business_days(api, start, today)
    need = [d for d in days if d not in have]

    if len(need) > 30:
        _log(f"決算データの初回取り込みです。{len(need)}日分を取りに行きます（20〜30分かかります）")
    elif need:
        _log(f"決算を取りに行きます: {len(need)}日分")

    frames = [old] if old is not None else []
    got_dates = []
    for i, d in enumerate(need, 1):
        rows = api.statements_by_date(d)
        got_dates.append(d)
        if rows:
            df = pd.DataFrame(rows)
            keep = [c for c in STMT_COLS if c in df.columns]
            frames.append(df[keep])
        if i % 100 == 0:
            _log(f"  {i}/{len(need)}日")
        time.sleep(0.12)

    # 取りに行った日は「開示ゼロ」でも記録しておく（毎回取り直さないため）
    if got_dates:
        frames.append(pd.DataFrame({"DisclosedDate": got_dates, "LocalCode": [None] * len(got_dates)}))

    if not frames:
        raise RuntimeError("決算データが取得できませんでした")

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[all_df["DisclosedDate"].astype(str) >= start.isoformat()]
    all_df = all_df.drop_duplicates(
        subset=["LocalCode", "DisclosedDate", "TypeOfCurrentPeriod", "TypeOfDocument"],
        keep="last",
    )
    _save(all_df, STATEMENTS)
    real = all_df["LocalCode"].notna().sum()
    _log(f"決算の保管ファイル: {real:,}件の開示")
    return all_df


def load_cached():
    """保管ファイルだけを読む（日中の軽い更新用）。"""
    return _load(PRICES), _load(STATEMENTS)

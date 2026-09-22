"""株価・指標・決算を集めて、手元の保管ファイルに貯めていく部分。

初回だけ過去5年分をまとめて取りに行きます（20〜30分かかります）。
2回目以降は「前回から増えた日の分だけ」なので数十秒で終わります。
"""

import datetime as dt
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from jq import JQuants  # noqa: E402

CACHE_DIR = os.environ.get(
    "CACHE_DIR", os.path.join(os.path.dirname(__file__), "..", "cache")
)
PRICES = os.path.join(CACHE_DIR, "prices.parquet")
SUMMARY = os.path.join(CACHE_DIR, "summary.parquet")

PRICE_DAYS = 130        # 株価は直近130営業日ぶんを保持
STATEMENT_YEARS = 5     # 決算はライトプランの上限いっぱい5年分

PRICE_COLS = ["Date", "Code", "C", "AdjC", "AdjH", "AdjL", "AdjVo", "Va"]
NUMERIC_PRICE = ["C", "AdjC", "AdjH", "AdjL", "AdjVo", "Va"]

SUM_COLS = [
    "Code", "DiscDate", "DocType", "CurPerType", "CurFYEn", "CurPerEn",
    "Sales", "OP", "NP", "EPS", "BPS", "TA", "Eq", "EqAR",
    "FSales", "FOP", "FEPS", "FDivAnn", "DivAnn",
]

VAL_COLS = ["Code", "Date", "EPS", "FwdEPS", "BPS", "PER", "FwdPER", "PBR",
            "ROE", "FwdROE", "MktCap"]


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


def _keep(df, cols):
    return df[[c for c in cols if c in df.columns]]


def business_days(api, start, end):
    """営業日の一覧を新しい順で返す。"""
    try:
        cal = api.calendar(start.isoformat(), end.isoformat())
        days = [c["Date"] for c in cal if str(c.get("HolDiv")) in ("1", "2")]
        days = [d for d in days if start.isoformat() <= d <= end.isoformat()]
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

    days = business_days(api, today - dt.timedelta(days=220), today)[: PRICE_DAYS + 5]
    need = [d for d in days if d not in have]

    if need:
        _log(f"株価を取りに行きます: {len(need)}日分")
    frames = [old] if old is not None else []
    for i, d in enumerate(need, 1):
        rows = api.bars_by_date(d)
        if rows:
            frames.append(_keep(pd.DataFrame(rows), PRICE_COLS))
        if i % 20 == 0:
            _log(f"  {i}/{len(need)}日")
        time.sleep(0.12)

    if not frames:
        raise RuntimeError("株価が1日分も取得できませんでした")

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.drop_duplicates(subset=["Date", "Code"], keep="last")
    keep_days = sorted(all_df["Date"].astype(str).unique(), reverse=True)[:PRICE_DAYS]
    all_df = all_df[all_df["Date"].astype(str).isin(set(keep_days))]
    for c in NUMERIC_PRICE:
        if c in all_df.columns:
            all_df[c] = pd.to_numeric(all_df[c], errors="coerce")
    _save(all_df, PRICES)
    _log(f"株価の保管ファイル: {len(all_df):,}行 / {len(keep_days)}営業日")
    return all_df


# ---------------------------------------------------------------- 指標
def fetch_valuation(api, date):
    """指定日の全銘柄のPER・PBR・EPSなど（最新日だけあれば足りる）。"""
    rows = api.valuation_by_date(date)
    if not rows:
        return pd.DataFrame(columns=VAL_COLS)
    df = _keep(pd.DataFrame(rows), VAL_COLS)
    for c in VAL_COLS:
        if c in df.columns and c not in ("Code", "Date"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    _log(f"バリュエーション指標: {len(df):,}銘柄（{date}）")
    return df


# ---------------------------------------------------------------- 決算
def update_summary(api, today=None):
    today = today or dt.date.today()
    old = _load(SUMMARY)
    have = set(old["DiscDate"].astype(str)) if old is not None else set()

    start = today - dt.timedelta(days=365 * STATEMENT_YEARS + 5)
    days = business_days(api, start, today)
    need = [d for d in days if d not in have]

    if len(need) > 30:
        _log(f"決算データの初回取り込みです。{len(need)}日分（20〜30分かかります）")
    elif need:
        _log(f"決算を取りに行きます: {len(need)}日分")

    frames = [old] if old is not None else []
    for i, d in enumerate(need, 1):
        rows = api.summary_by_date(d)
        if rows:
            frames.append(_keep(pd.DataFrame(rows), SUM_COLS))
        if i % 100 == 0:
            _log(f"  {i}/{len(need)}日")
        time.sleep(0.1)

    # 取りに行った日は「開示ゼロ」でも記録しておく（毎回取り直さないため）
    if need:
        frames.append(pd.DataFrame({"DiscDate": need, "Code": [None] * len(need)}))

    if not frames:
        raise RuntimeError("決算データが取得できませんでした")

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[all_df["DiscDate"].astype(str) >= start.isoformat()]
    subset = [c for c in ["Code", "DiscDate", "CurPerType", "DocType"]
              if c in all_df.columns]
    all_df = all_df.drop_duplicates(subset=subset, keep="last")
    _save(all_df, SUMMARY)
    _log(f"決算の保管ファイル: {int(all_df['Code'].notna().sum()):,}件の開示")
    return all_df

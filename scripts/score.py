"""必須条件の判定と、ポイントの計算。

ここが仕組みの心臓部です。config.yml の数字を読んで動きます。
"""

import numpy as np
import pandas as pd


# ============================================================ 決算まわり
FY_DOC_PREFIXES = ("FYFinancialStatements",)


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def build_fundamentals(stmt: pd.DataFrame, consecutive_years: int) -> pd.DataFrame:
    """銘柄ごとに「業績の並び」と「最新の1株あたり情報」をまとめる。"""
    df = stmt[stmt["LocalCode"].notna()].copy()
    df["DisclosedDate"] = df["DisclosedDate"].astype(str)
    df["code4"] = df["LocalCode"].astype(str).str[:4]

    # ---- 本決算（通期実績）の並び ----
    for col in ("TypeOfCurrentPeriod", "TypeOfDocument", "CurrentFiscalYearEndDate",
                "NetSales", "OperatingProfit"):
        if col not in df.columns:
            df[col] = None
    is_fy = (df["TypeOfCurrentPeriod"].astype(str) == "FY") & df[
        "TypeOfDocument"
    ].astype(str).str.startswith(FY_DOC_PREFIXES)
    fy = df[is_fy].copy()
    fy["NetSales"] = _num(fy["NetSales"])
    fy["OperatingProfit"] = _num(fy["OperatingProfit"])
    fy = fy.dropna(subset=["CurrentFiscalYearEndDate"])
    fy = fy.sort_values(["code4", "CurrentFiscalYearEndDate", "DisclosedDate"])
    fy = fy.drop_duplicates(subset=["code4", "CurrentFiscalYearEndDate"], keep="last")

    # ---- 最新の値（項目ごとに「いちばん新しい空でない値」を拾う） ----
    latest_fields = [
        "ForecastEarningsPerShare", "EarningsPerShare",
        "ForecastDividendPerShareAnnual", "ResultDividendPerShareAnnual",
        "BookValuePerShare", "EquityToAssetRatio", "Equity", "TotalAssets",
        "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
        "NumberOfTreasuryStockAtTheEndOfFiscalYear",
    ]
    d = df.sort_values("DisclosedDate")
    latest = {}
    for f in latest_fields:
        if f in d.columns:
            s = d[["code4", f]].copy()
            s[f] = _num(s[f])
            s = s.dropna(subset=[f])
            latest[f] = s.groupby("code4")[f].last()
        else:
            latest[f] = pd.Series(dtype=float)
    latest = pd.DataFrame(latest)

    # ---- 銘柄ごとに集計 ----
    rows = []
    for code, g in fy.groupby("code4"):
        g = g.sort_values("CurrentFiscalYearEndDate")
        sales = g["NetSales"].to_numpy(dtype=float)
        op = g["OperatingProfit"].to_numpy(dtype=float)
        years = g["CurrentFiscalYearEndDate"].tolist()
        ok = ~(np.isnan(sales) | np.isnan(op))
        sales, op = sales[ok], op[ok]
        years = [y for y, k in zip(years, ok) if k]
        n = len(sales)

        # 直近から何年連続で増収増益か
        streak = 0
        for i in range(n - 1, 0, -1):
            if sales[i] > sales[i - 1] and op[i] > op[i - 1]:
                streak += 1
            else:
                break

        # 営業利益の年平均成長率
        cagr = np.nan
        if n >= 2 and op[0] > 0 and op[-1] > 0:
            cagr = ((op[-1] / op[0]) ** (1 / (n - 1)) - 1) * 100

        rows.append({
            "code": code,
            "fy_count": n,
            "growth_streak": streak,
            "latest_fy": years[-1] if years else None,
            "latest_sales": sales[-1] if n else np.nan,
            "latest_op": op[-1] if n else np.nan,
            "op_margin": (op[-1] / sales[-1] * 100) if n and sales[-1] else np.nan,
            "profit_cagr": cagr,
            "sales_series": [float(x) for x in sales[-6:]],
            "op_series": [float(x) for x in op[-6:]],
            "fy_labels": [str(y)[:7] for y in years[-6:]],
        })

    if not rows:
        raise RuntimeError(
            "本決算（通期）のデータが1件も取れませんでした。"
            "J-Quantsのプランがライト以上になっているか確認してください。"
        )
    out = pd.DataFrame(rows).set_index("code")
    out = out.join(latest, how="left")
    for c in latest_fields:
        if c not in out.columns:
            out[c] = np.nan

    # 1株あたり純資産（載っていなければ純資産÷株数で計算）
    shares_col = "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock"
    bps = out.get("BookValuePerShare")
    calc = out["Equity"] / (out[shares_col] - out["NumberOfTreasuryStockAtTheEndOfFiscalYear"].fillna(0))
    out["bps"] = bps.where(bps.notna() & (bps > 0), calc)

    out["eps"] = out["ForecastEarningsPerShare"].where(
        out["ForecastEarningsPerShare"].notna() & (out["ForecastEarningsPerShare"] > 0),
        out["EarningsPerShare"],
    )
    out["dps"] = out["ForecastDividendPerShareAnnual"].where(
        out["ForecastDividendPerShareAnnual"].notna(),
        out["ResultDividendPerShareAnnual"],
    )
    out["equity_ratio"] = out["EquityToAssetRatio"] * 100
    out.loc[out["equity_ratio"].isna(), "equity_ratio"] = (
        out["Equity"] / out["TotalAssets"] * 100
    )
    return out


# ============================================================ 株価まわり
def build_technicals(prices: pd.DataFrame, req: dict) -> pd.DataFrame:
    """銘柄ごとに高値・下落率・レンジの状態を計算する。"""
    p = prices.copy()
    p["code4"] = p["Code"].astype(str).str[:4]
    p["Date"] = p["Date"].astype(str)
    p = p.sort_values(["code4", "Date"])

    look = int(req["high_lookback_days"])
    win = int(req["range_window_days"])
    width_max = float(req["range_width_max_pct"])

    rows = []
    for code, g in p.groupby("code4", sort=False):
        g = g.tail(look + 60)
        close = g["AdjustmentClose"].to_numpy(dtype=float)
        high = g["AdjustmentHigh"].to_numpy(dtype=float)
        low = g["AdjustmentLow"].to_numpy(dtype=float)
        raw_close = g["Close"].to_numpy(dtype=float)
        turnover = g["TurnoverValue"].to_numpy(dtype=float)
        if len(close) < win + 5 or np.isnan(close[-1]):
            continue

        h_series = pd.Series(high)
        l_series = pd.Series(low)
        c_series = pd.Series(close)

        # 直近 look 日の高値
        high_n = np.nanmax(high[-look:])
        last_adj = close[-1]
        dd = (high_n - last_adj) / high_n * 100 if high_n > 0 else np.nan

        # 各日について「直近 win 日の値幅 ÷ 平均株価」を出す
        roll_h = h_series.rolling(win).max()
        roll_l = l_series.rolling(win).min()
        roll_m = c_series.rolling(win).mean()
        width = (roll_h - roll_l) / roll_m * 100

        in_range = (width <= width_max).to_numpy()
        # 直近から何日連続でレンジ状態か
        rdays = 0
        for i in range(len(in_range) - 1, -1, -1):
            if in_range[i]:
                rdays += 1
            else:
                break

        recent_high = np.nanmax(high[-win:])
        rows.append({
            "code": code,
            "date": g["Date"].iloc[-1],
            "price": raw_close[-1] if not np.isnan(raw_close[-1]) else last_adj,
            "adj_price": last_adj,
            "high_n": high_n,
            "drawdown": dd,
            "range_width": float(width.iloc[-1]) if not np.isnan(width.iloc[-1]) else np.nan,
            "range_days": rdays,
            "not_recovered": bool(recent_high < high_n * 0.995),
            "avg_turnover": float(np.nanmean(turnover[-win:])),
            "range_low": float(np.nanmin(low[-win:])),
            "range_high": float(recent_high),
            "spark": [float(x) for x in close[-look:]],
        })
    if not rows:
        raise RuntimeError("株価データが1銘柄も揃いませんでした")
    return pd.DataFrame(rows).set_index("code")


# ============================================================ 採点
def _tier(value, rules):
    """config の配点表から、当てはまる段階を1つ返す。"""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0, None
    for r in rules:
        lo = r.get("min", -1e18)
        hi = r.get("max", 1e18)
        if lo <= value <= hi:
            return int(r["points"]), r.get("label")
    return 0, None


def screen(fund, tech, info, cfg, price_override=None):
    """必須条件でふるいにかけ、通った銘柄に点数をつける。"""
    req = cfg["required"]
    sc = cfg["scoring"]

    df = tech.join(fund, how="inner").join(info, how="inner")

    # 日中の最新株価で上書き（あれば）
    if price_override:
        ov = pd.Series(price_override, dtype=float)
        ov.index = ov.index.astype(str)
        hit = df.index.intersection(ov.index)
        ratio = ov[hit] / df.loc[hit, "price"]
        df.loc[hit, "price"] = ov[hit]
        df.loc[hit, "adj_price"] = df.loc[hit, "adj_price"] * ratio
        df.loc[hit, "drawdown"] = (
            (df.loc[hit, "high_n"] - df.loc[hit, "adj_price"]) / df.loc[hit, "high_n"] * 100
        )

    df["per"] = df["price"] / df["eps"]
    df["pbr"] = df["price"] / df["bps"]
    df["div_yield"] = df["dps"] / df["price"] * 100
    df["payout"] = df["dps"] / df["eps"] * 100

    # ---------------- 必須条件 ----------------
    c_growth = df["growth_streak"] >= int(req["consecutive_growth_years"])
    c_per = df["per"].between(float(req["per_min"]) + 1e-9, float(req["per_max"]))
    c_dd = df["drawdown"] >= float(req["drawdown_min_pct"])
    c_range = (df["range_width"] <= float(req["range_width_max_pct"])) & df["not_recovered"]
    c_liq = df["avg_turnover"] >= float(req["min_avg_turnover_yen"])

    df["ok_growth"], df["ok_per"] = c_growth, c_per
    df["ok_drawdown"], df["ok_range"], df["ok_liquidity"] = c_dd, c_range, c_liq
    passed = df[c_growth & c_per & c_dd & c_range & c_liq].copy()

    # ---------------- 採点 ----------------
    metric_map = [
        ("dividend_yield", "div_yield"),
        ("payout_ratio", "payout"),
        ("per", "per"),
        ("pbr", "pbr"),
        ("drawdown", "drawdown"),
        ("range_days", "range_days"),
        ("equity_ratio", "equity_ratio"),
        ("operating_margin", "op_margin"),
        ("profit_cagr", "profit_cagr"),
    ]
    scores, details = [], []
    for _, row in passed.iterrows():
        total, det = 0, []
        for key, col in metric_map:
            pts, label = _tier(row.get(col), sc.get(key, []))
            if pts:
                total += pts
                det.append({"label": label, "points": pts})
        scores.append(total)
        details.append(det)
    passed["score"] = scores
    passed["score_detail"] = details

    max_score = sum(max((r["points"] for r in rules), default=0) for rules in sc.values())
    passed["score_max"] = max_score
    passed = passed.sort_values(["score", "div_yield"], ascending=[False, False])
    return passed, df

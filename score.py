"""必須条件の判定と、ポイントの計算。

ここが仕組みの心臓部です。config.yml の数字を読んで動きます。
"""

import numpy as np
import pandas as pd

FY_DOC_PREFIX = "FYFinancialStatements"


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _code4(s):
    return s.astype(str).str[:4]


# ============================================================ 決算・指標
def build_fundamentals(summary: pd.DataFrame, val: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとに「業績の並び」と「1株あたり指標」をまとめる。"""
    df = summary[summary["Code"].notna()].copy()
    for col in ("DocType", "CurPerType", "CurFYEn", "Sales", "OP", "EqAR",
                "FDivAnn", "DivAnn", "DiscDate"):
        if col not in df.columns:
            df[col] = None
    df["DiscDate"] = df["DiscDate"].astype(str)
    df["code"] = _code4(df["Code"])

    # ---- 本決算（通期実績）の並び ----
    is_fy = (df["CurPerType"].astype(str) == "FY") & df["DocType"].astype(
        str
    ).str.startswith(FY_DOC_PREFIX)
    fy = df[is_fy].copy()
    fy["Sales"], fy["OP"] = _num(fy["Sales"]), _num(fy["OP"])
    fy["CurFYEn"] = fy["CurFYEn"].astype(str)
    fy = fy[fy["CurFYEn"].str.len() >= 7]
    fy = fy.sort_values(["code", "CurFYEn", "DiscDate"])
    fy = fy.drop_duplicates(subset=["code", "CurFYEn"], keep="last")

    rows = []
    for code, g in fy.groupby("code", sort=False):
        sales = g["Sales"].to_numpy(dtype=float)
        op = g["OP"].to_numpy(dtype=float)
        years = g["CurFYEn"].tolist()
        ok = ~(np.isnan(sales) | np.isnan(op))
        sales, op = sales[ok], op[ok]
        years = [y for y, k in zip(years, ok) if k]
        n = len(sales)
        if n == 0:
            continue

        streak = 0
        for i in range(n - 1, 0, -1):
            if sales[i] > sales[i - 1] and op[i] > op[i - 1]:
                streak += 1
            else:
                break

        cagr = np.nan
        if n >= 2 and op[0] > 0 and op[-1] > 0:
            cagr = ((op[-1] / op[0]) ** (1 / (n - 1)) - 1) * 100

        rows.append({
            "code": code,
            "fy_count": n,
            "growth_streak": streak,
            "latest_fy": years[-1],
            "op_margin": (op[-1] / sales[-1] * 100) if sales[-1] else np.nan,
            "profit_cagr": cagr,
        })

    if not rows:
        raise RuntimeError(
            "本決算（通期）のデータが1件も取れませんでした。"
            "ご契約のプランを確認してください。"
        )
    out = pd.DataFrame(rows).set_index("code")

    # ---- 項目ごとに「いちばん新しい空でない値」を拾う ----
    d = df.sort_values("DiscDate")
    for field, name in [("EqAR", "equity_ratio"), ("FDivAnn", "fdiv"),
                        ("DivAnn", "rdiv")]:
        s = d[["code", field]].copy()
        s[field] = _num(s[field])
        s = s.dropna(subset=[field])
        out[name] = s.groupby("code")[field].last()

    # 自己資本比率が小数（0.45）でも百分率（45）でも正しく扱う
    med = out["equity_ratio"].median()
    if pd.notna(med) and med <= 1.5:
        out["equity_ratio"] = out["equity_ratio"] * 100
    out["dps"] = out["fdiv"].where(out["fdiv"].notna(), out["rdiv"])

    # ---- バリュエーション指標を重ねる ----
    if val is not None and len(val):
        v = val.copy()
        v["code"] = _code4(v["Code"])
        v = v.drop_duplicates(subset=["code"], keep="last").set_index("code")
        out["eps"] = v["FwdEPS"].where(
            v.get("FwdEPS", pd.Series(dtype=float)).notna() & (v["FwdEPS"] > 0),
            v.get("EPS"),
        )
        out["bps"] = v.get("BPS")
        out["mkt_cap"] = v.get("MktCap")
    for c in ("eps", "bps", "mkt_cap"):
        if c not in out.columns:
            out[c] = np.nan
    return out


# ============================================================ 株価まわり
def build_technicals(prices: pd.DataFrame, req: dict) -> pd.DataFrame:
    """銘柄ごとに高値・下落率・レンジの状態を計算する。"""
    p = prices.copy()
    p["code"] = _code4(p["Code"])
    p["Date"] = p["Date"].astype(str)
    p = p.sort_values(["code", "Date"])

    look = int(req["high_lookback_days"])
    win = int(req["range_window_days"])
    width_max = float(req["range_width_max_pct"])

    rows = []
    for code, g in p.groupby("code", sort=False):
        g = g.tail(look + 60)
        close = g["AdjC"].to_numpy(dtype=float)
        high = g["AdjH"].to_numpy(dtype=float)
        low = g["AdjL"].to_numpy(dtype=float)
        raw_close = g["C"].to_numpy(dtype=float)
        turnover = g["Va"].to_numpy(dtype=float)
        if len(close) < win + 5 or np.isnan(close[-1]):
            continue

        high_n = np.nanmax(high[-look:])
        last_adj = close[-1]
        dd = (high_n - last_adj) / high_n * 100 if high_n > 0 else np.nan

        roll_h = pd.Series(high).rolling(win).max()
        roll_l = pd.Series(low).rolling(win).min()
        roll_m = pd.Series(close).rolling(win).mean()
        width = (roll_h - roll_l) / roll_m * 100
        in_range = (width <= width_max).to_numpy()

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
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return 0, None
    for r in rules:
        if r.get("min", -1e18) <= value <= r.get("max", 1e18):
            return int(r["points"]), r.get("label")
    return 0, None


def screen(fund, tech, info, cfg, price_override=None):
    """必須条件でふるいにかけ、通った銘柄に点数をつける。"""
    req, sc = cfg["required"], cfg["scoring"]
    df = tech.join(fund, how="inner").join(info, how="inner")

    if price_override:
        ov = pd.Series(price_override, dtype=float)
        ov.index = ov.index.astype(str)
        hit = df.index.intersection(ov.index)
        if len(hit):
            ratio = ov[hit] / df.loc[hit, "price"]
            df.loc[hit, "price"] = ov[hit]
            df.loc[hit, "adj_price"] = df.loc[hit, "adj_price"] * ratio
            df.loc[hit, "drawdown"] = (
                (df.loc[hit, "high_n"] - df.loc[hit, "adj_price"])
                / df.loc[hit, "high_n"] * 100
            )

    df["per"] = df["price"] / df["eps"]
    df["pbr"] = df["price"] / df["bps"]
    df["div_yield"] = df["dps"] / df["price"] * 100
    df["payout"] = df["dps"] / df["eps"] * 100

    c_growth = df["growth_streak"] >= int(req["consecutive_growth_years"])
    c_per = df["per"].between(float(req["per_min"]) + 1e-9, float(req["per_max"]))
    c_dd = df["drawdown"] >= float(req["drawdown_min_pct"])
    c_range = (df["range_width"] <= float(req["range_width_max_pct"])) & df["not_recovered"]
    c_liq = df["avg_turnover"] >= float(req["min_avg_turnover_yen"])

    df["ok_growth"], df["ok_per"] = c_growth, c_per
    df["ok_drawdown"], df["ok_range"], df["ok_liquidity"] = c_dd, c_range, c_liq
    passed = df[c_growth & c_per & c_dd & c_range & c_liq].copy()

    metric_map = [
        ("dividend_yield", "div_yield"), ("payout_ratio", "payout"),
        ("per", "per"), ("pbr", "pbr"), ("drawdown", "drawdown"),
        ("range_days", "range_days"), ("equity_ratio", "equity_ratio"),
        ("operating_margin", "op_margin"), ("profit_cagr", "profit_cagr"),
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
    passed["score_max"] = sum(
        max((r["points"] for r in rules), default=0) for rules in sc.values()
    )
    passed = passed.sort_values(["score", "div_yield"], ascending=[False, False])
    return passed, df

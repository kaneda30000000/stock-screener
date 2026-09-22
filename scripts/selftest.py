"""ダミーデータで計算とページ生成が正しく動くかを確かめる（ネット接続なし）。"""

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import render  # noqa: E402
import score as scoring  # noqa: E402

rng = np.random.default_rng(7)
N = 60
DAYS = 130

codes = [f"{1000 + i * 7}" for i in range(N)]
dates = [(dt.date(2026, 9, 19) - dt.timedelta(days=d)).isoformat()
         for d in range(DAYS * 7 // 5, 0, -1)]
dates = [d for d in dates if dt.date.fromisoformat(d).weekday() < 5][-DAYS:]


def make_path(kind, base=2000.0):
    n = len(dates)
    if kind == "range_after_drop":
        peak = int(n * 0.55)
        up = np.linspace(base * 0.8, base * 1.25, peak)
        down = np.linspace(base * 1.25, base * 0.98, int(n * 0.2))
        flat = base * 0.98 + rng.normal(0, base * 0.008, n - peak - len(down))
        return np.concatenate([up, down, flat])[:n]
    if kind == "trending_up":
        return np.linspace(base * 0.7, base * 1.3, n) + rng.normal(0, base * 0.01, n)
    return base + rng.normal(0, base * 0.05, n).cumsum() / 3


price_rows, stmt_rows, info_rows = [], [], []
for i, c in enumerate(codes):
    kind = ["range_after_drop", "trending_up", "noisy"][i % 3]
    base = float(rng.uniform(600, 4000))
    path = make_path(kind, base)
    for d, px in zip(dates, path):
        price_rows.append({
            "Date": d, "Code": c + "0", "Close": round(px, 1),
            "AdjustmentClose": round(px, 1),
            "AdjustmentHigh": round(px * 1.008, 1),
            "AdjustmentLow": round(px * 0.992, 1),
            "AdjustmentVolume": 100000,
            "TurnoverValue": float(px * 100000),
        })

    growing = i % 4 != 3
    sales = 100000.0
    op = 9000.0
    for y in range(5):
        fy_end = f"{2022 + y}-03-31"
        if growing:
            sales *= 1.08
            op *= 1.12
        else:
            sales *= 1.08 if y != 2 else 0.95
            op *= 1.12 if y != 2 else 0.8
        eps = float(rng.uniform(60, 400)) if y == 4 else 100.0
        stmt_rows.append({
            "LocalCode": c + "0",
            "DisclosedDate": f"{2022 + y}-05-12",
            "TypeOfCurrentPeriod": "FY",
            "TypeOfDocument": "FYFinancialStatements_Consolidated_JP",
            "CurrentFiscalYearEndDate": fy_end,
            "CurrentPeriodEndDate": fy_end,
            "NetSales": sales, "OperatingProfit": op,
            "Profit": op * 0.7,
            "EarningsPerShare": eps,
            "BookValuePerShare": eps * float(rng.uniform(6, 20)),
            "TotalAssets": sales * 1.5, "Equity": sales * 1.5 * float(rng.uniform(0.3, 0.75)),
            "EquityToAssetRatio": float(rng.uniform(0.3, 0.75)),
            "ForecastEarningsPerShare": base / float(rng.uniform(7, 30)),
            "ForecastDividendPerShareAnnual": base / float(rng.uniform(30, 300)),
            "ResultDividendPerShareAnnual": None,
            "NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock": 1e7,
            "NumberOfTreasuryStockAtTheEndOfFiscalYear": 1e5,
        })
    info_rows.append({"code": c, "name": f"テスト商事{i:02d}", "sector": "卸売業",
                      "market": "プライム"})

prices = pd.DataFrame(price_rows)
stmts = pd.DataFrame(stmt_rows)
info = pd.DataFrame(info_rows).set_index("code")

with open(os.path.join(ROOT, "config.yml"), encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

fund = scoring.build_fundamentals(stmts, cfg["required"]["consecutive_growth_years"])
tech = scoring.build_technicals(prices, cfg["required"])
print(f"決算 {len(fund)} / 株価 {len(tech)}")
print("連続増収増益の分布:", fund["growth_streak"].value_counts().to_dict())

passed, allrows = scoring.screen(fund, tech, info, cfg)
print(f"通過 {len(passed)} / 調査 {len(allrows)}  満点={int(allrows.get('score_max', pd.Series([0])).max() or 0) if len(passed) else '—'}")
for col in ["ok_growth", "ok_per", "ok_drawdown", "ok_range", "ok_liquidity"]:
    print(f"  {col}: {int(allrows[col].sum())}")
if len(passed):
    print(passed[["score", "per", "pbr", "div_yield", "payout", "drawdown",
                  "range_days", "op_margin"]].head(8).round(2).to_string())

meta = {"updated_at": "test", "mode": "full", "universe": len(allrows),
        "price_date": dates[-1], "price_note": "（テストデータ）"}
out = os.path.join(ROOT, "docs", "index.html")
os.makedirs(os.path.dirname(out), exist_ok=True)
render.render(passed, allrows, cfg, meta, out)
render.dump_json(passed, meta, os.path.join(ROOT, "docs", "latest.json"))
print("ページ出力:", out, os.path.getsize(out), "バイト")

# 日中更新（株価を5%下げて再計算）も試す
ov = {c: float(tech.loc[c, "price"]) * 0.95 for c in list(tech.index)[:20]}
p2, _ = scoring.screen(fund, tech, info, cfg, price_override=ov)
print(f"日中更新テスト: 通過 {len(p2)}")

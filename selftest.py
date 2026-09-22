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
N, DAYS = 60, 130

codes = [f"{1000 + i * 7}" for i in range(N)]
dates = [(dt.date(2026, 9, 18) - dt.timedelta(days=d)).isoformat()
         for d in range(DAYS * 7 // 5, 0, -1)]
dates = [d for d in dates if dt.date.fromisoformat(d).weekday() < 5][-DAYS:]


def make_path(kind, base):
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


price_rows, sum_rows, val_rows, info_rows = [], [], [], []
for i, c in enumerate(codes):
    kind = ["range_after_drop", "trending_up", "noisy"][i % 3]
    base = float(rng.uniform(600, 4000))
    path = make_path(kind, base)
    for d, px in zip(dates, path):
        price_rows.append({
            "Date": d, "Code": c + "0", "C": round(px, 1), "AdjC": round(px, 1),
            "AdjH": round(px * 1.008, 1), "AdjL": round(px * 0.992, 1),
            "AdjVo": 100000.0, "Va": float(px * 100000),
        })

    growing = i % 4 != 3
    sales, op = 100000.0, 9000.0
    for y in range(5):
        if growing:
            sales, op = sales * 1.08, op * 1.12
        else:
            sales *= 1.08 if y != 2 else 0.95
            op *= 1.12 if y != 2 else 0.80
        sum_rows.append({
            "Code": c + "0", "DiscDate": f"{2022 + y}-05-12",
            "DocType": "FYFinancialStatements_Consolidated_JP",
            "CurPerType": "FY", "CurFYEn": f"{2022 + y}-03-31",
            "CurPerEn": f"{2022 + y}-03-31",
            "Sales": sales, "OP": op, "NP": op * 0.7,
            "EPS": 100.0, "BPS": 900.0, "TA": sales * 1.5,
            "Eq": sales * 0.8, "EqAR": float(rng.uniform(0.30, 0.75)),
            "FSales": sales * 1.05, "FOP": op * 1.05,
            "FEPS": 110.0, "FDivAnn": base / float(rng.uniform(30, 300)),
            "DivAnn": None,
        })

    eps = base / float(rng.uniform(7, 30))
    val_rows.append({
        "Code": c + "0", "Date": dates[-1], "EPS": eps * 0.95, "FwdEPS": eps,
        "BPS": eps * float(rng.uniform(6, 20)), "PER": np.nan, "FwdPER": np.nan,
        "PBR": np.nan, "ROE": 0.1, "FwdROE": 0.1, "MktCap": 50000.0,
    })
    info_rows.append({"code": c, "name": f"テスト商事{i:02d}",
                      "sector": "卸売業", "market": "プライム"})

prices = pd.DataFrame(price_rows)
summary = pd.DataFrame(sum_rows)
val = pd.DataFrame(val_rows)
info = pd.DataFrame(info_rows).set_index("code")

with open(os.path.join(ROOT, "config.yml"), encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

fund = scoring.build_fundamentals(summary, val)
tech = scoring.build_technicals(prices, cfg["required"])
print(f"決算 {len(fund)} / 株価 {len(tech)}")
print("連続増収増益の分布:", fund["growth_streak"].value_counts().to_dict())
print("自己資本比率の中央値:", round(float(fund['equity_ratio'].median()), 1))

passed, allrows = scoring.screen(fund, tech, info, cfg)
print(f"通過 {len(passed)} / 調査 {len(allrows)}")
for col in ["ok_growth", "ok_per", "ok_drawdown", "ok_range", "ok_liquidity"]:
    print(f"  {col}: {int(allrows[col].sum())}")
if len(passed):
    print(passed[["score", "per", "pbr", "div_yield", "payout", "drawdown",
                  "range_days", "op_margin", "equity_ratio"]].head(8).round(2).to_string())

meta = {"updated_at": "test", "mode": "full", "universe": len(allrows),
        "price_date": dates[-1], "price_note": "（テストデータ）"}
out = os.path.join(ROOT, "docs", "index.html")
os.makedirs(os.path.dirname(out), exist_ok=True)
render.render(passed, allrows, cfg, meta, out)
render.dump_json(passed, meta, os.path.join(ROOT, "docs", "latest.json"))
print("ページ出力:", out, os.path.getsize(out), "バイト")

ov = {c: float(tech.loc[c, "price"]) * 0.95 for c in list(tech.index)[:20]}
p2, _ = scoring.screen(fund, tech, info, cfg, price_override=ov)
print(f"日中更新テスト: 通過 {len(p2)}")

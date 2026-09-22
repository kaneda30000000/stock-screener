"""実行の入り口。

  python scripts/run.py full      … 全銘柄を一から調べ直す（深夜に1回）
  python scripts/run.py intraday  … 候補の株価だけ更新する（前場引け後と大引け後）
"""

import datetime as dt
import json
import os
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fetch  # noqa: E402
import quote  # noqa: E402
import render  # noqa: E402
import score as scoring  # noqa: E402
from jq import JQuants  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(ROOT, "data")


def load_config():
    with open(os.path.join(ROOT, "config.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_universe(api, cfg):
    info = pd.DataFrame(api.master())
    if info.empty:
        raise RuntimeError("上場銘柄一覧が取得できませんでした")
    info["code"] = info["Code"].astype(str).str[:4]
    u = cfg["universe"]
    info = info[info["Mkt"].astype(str).isin(u["market_codes"])]
    info = info[~info["S33"].astype(str).isin(u["exclude_sector33"])]
    info = info.sort_values("Code").drop_duplicates(subset=["code"], keep="last")
    info = info.set_index("code")
    return pd.DataFrame({
        "name": info["CoName"],
        "sector": info.get("S33Nm", pd.Series(dtype=str)),
        "market": info.get("MktNm", pd.Series(dtype=str)),
    })


def is_trading_day(api, today):
    try:
        cal = api.calendar(today.isoformat(), today.isoformat())
        if cal:
            return str(cal[0].get("HolDiv")) in ("1", "2")
    except Exception:
        pass
    return today.weekday() < 5


def run(mode):
    cfg = load_config()
    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)

    api = JQuants().login()
    today = dt.datetime.now(JST).date()

    if mode == "intraday" and not is_trading_day(api, today):
        print("[run] 本日は非営業日のため日中更新は行いません")
        return

    prices = fetch.update_prices(api, today)
    summary = fetch.update_summary(api, today)
    price_date = str(prices["Date"].max())
    val = fetch.fetch_valuation(api, price_date)

    info = build_universe(api, cfg)
    print(f"[run] 対象銘柄: {len(info):,}")
    prices = prices[prices["Code"].astype(str).str[:4].isin(info.index)]

    fund = scoring.build_fundamentals(summary, val)
    tech = scoring.build_technicals(prices, cfg["required"])
    print(f"[run] 決算あり {len(fund):,} / 株価あり {len(tech):,}")

    override, note = None, ""
    if mode == "intraday":
        req = cfg["required"]
        pre = tech.join(fund, how="inner").join(info, how="inner")
        pre["per_pre"] = pre["price"] / pre["eps"]
        watch = pre[
            (pre["growth_streak"] >= req["consecutive_growth_years"])
            & (pre["per_pre"] > 0)
            & (pre["per_pre"] <= req["per_max"] * 1.25)
            & (pre["drawdown"] >= req["drawdown_min_pct"] - 6)
            & (pre["avg_turnover"] >= req["min_avg_turnover_yen"])
        ].sort_values("avg_turnover", ascending=False).head(500)
        print(f"[run] 日中に株価を取り直す銘柄: {len(watch):,}")
        override = quote.fetch_latest(list(watch.index))
        note = (f" ＋ {dt.datetime.now(JST).strftime('%H:%M')}時点の遅延株価で更新"
                if override else "（日中株価が取得できず前日終値のまま）")

    passed, allrows = scoring.screen(fund, tech, info, cfg, price_override=override)
    print(f"[run] 必須条件を通過: {len(passed):,}銘柄")

    meta = {
        "updated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "mode": mode,
        "universe": int(len(allrows)),
        "price_date": price_date,
        "price_note": note,
    }
    render.render(passed, allrows, cfg, meta, os.path.join(DOCS, "index.html"))
    render.dump_json(passed, meta, os.path.join(DOCS, "latest.json"))
    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "passed": int(len(passed))}, f, ensure_ascii=False, indent=1)
    print("[run] ページを書き出しました: docs/index.html")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "full")

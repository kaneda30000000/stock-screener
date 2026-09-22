"""買い時スクリーニング — これ1ファイルで全部動きます。

  python scripts/run.py full      … 全銘柄を一から調べ直す（深夜に1回）
  python scripts/run.py intraday  … 候補の株価だけ更新する（前場引け後と大引け後）
"""

import datetime as dt
import html
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import requests
import yaml

JST = dt.timezone(dt.timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(ROOT, "data")



# ====================================================================
# J-Quants API（v2）との通信
# ====================================================================
BASE = "https://api.jquants.com/v2"


class JQuantsError(RuntimeError):
    pass


class JQuants:
    def __init__(self, api_key=None):
        # JQ_API_KEY が無ければ、旧設定の JQ_PASSWORD に入れたキーも受け付ける
        self.api_key = (
            api_key
            or os.environ.get("JQ_API_KEY")
            or os.environ.get("JQ_PASSWORD")
            or ""
        ).strip()
        if not self.api_key:
            raise JQuantsError(
                "APIキーが設定されていません。"
                "GitHubのSecretsに JQ_API_KEY（または JQ_PASSWORD）として"
                "APIキーを登録してください。"
            )
        if "@" in self.api_key:
            raise JQuantsError(
                "APIキーではなくメールアドレスが入っているようです。"
                "J-Quants管理画面の[API Keys]で発行したキーを登録してください。"
            )
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self.api_key})

    def login(self):
        """疎通確認。ここで失敗すれば原因がはっきりする。"""
        r = self.session.get(f"{BASE}/markets/calendar", timeout=30)
        if r.status_code == 403:
            raise JQuantsError(
                "APIキーが拒否されました（403）。"
                "管理画面の[API Keys]で発行したキーを、余分な空白なしで "
                "JQ_API_KEY に登録しているか確認してください。"
            )
        if r.status_code != 200:
            raise JQuantsError(f"接続確認に失敗（{r.status_code}）: {r.text[:300]}")
        return self

    # ---------- 共通の取得処理（ページ送り・再試行つき） ----------
    def get(self, path, params=None, retries=4):
        params = dict(params or {})
        out = []
        while True:
            for attempt in range(retries):
                r = self.session.get(f"{BASE}{path}", params=params, timeout=90)
                if r.status_code == 200:
                    break
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt + 1)
                    continue
                if r.status_code == 403:
                    raise JQuantsError(
                        f"{path} へのアクセスが拒否されました（403）。"
                        "ご契約のプランでこのデータが使えるか確認してください。"
                    )
                raise JQuantsError(f"{path} の取得に失敗（{r.status_code}）: {r.text[:300]}")
            else:
                raise JQuantsError(f"{path} の取得に失敗（再試行しても回復せず）")

            body = r.json()
            key = "data" if "data" in body else next(
                (k for k in body if k != "pagination_key"), None
            )
            if key:
                out.extend(body.get(key) or [])
            pk = body.get("pagination_key")
            if not pk:
                return out
            params["pagination_key"] = pk

    # ---------- 個別のデータ ----------
    def master(self, date=None):
        """上場銘柄一覧"""
        return self.get("/equities/master", {"date": date} if date else None)

    def calendar(self, frm, to):
        """取引カレンダー"""
        return self.get("/markets/calendar", {"from": frm, "to": to})

    def bars_by_date(self, date):
        """ある1日の全銘柄の株価四本値"""
        return self.get("/equities/bars/daily", {"date": date})

    def valuation_by_date(self, date):
        """ある1日の全銘柄のバリュエーション指標（PER・PBR・EPSなど）"""
        return self.get("/equities/valuation", {"date": date})

    def summary_by_date(self, date):
        """ある1日に開示された全社の決算情報"""
        return self.get("/fins/summary", {"date": date})


# ====================================================================
# 日中の最新株価（無料・20分遅れ）
# ====================================================================
def fetch_latest(codes, chunk=120, retries=2):
    """4桁コードのリストを渡すと {コード: 最新値} を返す。"""
    try:
        import yfinance as yf
    except ImportError:
        print("[quote] yfinance が入っていないので日中更新は省略します", flush=True)
        return {}

    out = {}
    codes = [str(c) for c in codes]
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        tickers = [f"{c}.T" for c in part]
        for attempt in range(retries + 1):
            try:
                df = yf.download(
                    tickers, period="1d", interval="5m",
                    progress=False, threads=True, auto_adjust=False,
                )
                if df is None or df.empty:
                    raise ValueError("空のデータ")
                close = df["Close"] if "Close" in df.columns.get_level_values(0) else df
                if hasattr(close, "columns"):
                    for t in close.columns:
                        s = close[t].dropna()
                        if len(s):
                            out[str(t).replace(".T", "")] = float(s.iloc[-1])
                else:
                    s = close.dropna()
                    if len(s):
                        out[part[0]] = float(s.iloc[-1])
                break
            except Exception as e:
                if attempt == retries:
                    print(f"[quote] {part[0]}〜 の取得に失敗: {e}", flush=True)
                else:
                    time.sleep(3)
        time.sleep(1)
    print(f"[quote] 最新株価を {len(out)}/{len(codes)} 銘柄ぶん取得", flush=True)
    return out


# ====================================================================
# 条件判定とポイント計算
# ====================================================================
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


# ====================================================================
# HTMLページの生成
# ====================================================================
def _f(v, digits=1, suffix="", dash="—"):
    if v is None:
        return dash
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return dash
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return dash


def _sparkline(values, w=132, h=34):
    vals = [v for v in (values or []) if v is not None and not math.isnan(v)]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = (w - 4) / (len(vals) - 1)
    pts = " ".join(
        f"{2 + i * step:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}"
        for i, v in enumerate(vals)
    )
    last_x = 2 + (len(vals) - 1) * step
    last_y = h - 3 - (vals[-1] - lo) / rng * (h - 6)
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'aria-hidden="true"><polyline points="{pts}" fill="none" '
        f'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" '
        f'stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.6" fill="currentColor"/></svg>'
    )


def _card(row, rank):
    code = html.escape(str(row.name))
    name = html.escape(str(row.get("name") or ""))
    sector = html.escape(str(row.get("sector") or ""))
    score = int(row.get("score") or 0)
    smax = int(row.get("score_max") or 100)
    pct = max(4, min(100, round(score / smax * 100)))

    chips = "".join(
        f'<span class="chip">{html.escape(str(d["label"]))}'
        f'<b>+{int(d["points"])}</b></span>'
        for d in (row.get("score_detail") or []) if d.get("label")
    )

    def cell(label, value):
        return f'<div class="cell"><span>{label}</span><b>{value}</b></div>'

    metrics = "".join([
        cell("株価", _f(row.get("price"), 0, "円")),
        cell("PER", _f(row.get("per"), 1, "倍")),
        cell("PBR", _f(row.get("pbr"), 2, "倍")),
        cell("配当利回り", _f(row.get("div_yield"), 2, "%")),
        cell("配当性向", _f(row.get("payout"), 0, "%")),
        cell("自己資本比率", _f(row.get("equity_ratio"), 0, "%")),
        cell("営業利益率", _f(row.get("op_margin"), 1, "%")),
        cell("高値から", "−" + _f(row.get("drawdown"), 1, "%")),
        cell("レンジ日数", _f(row.get("range_days"), 0, "日")),
    ])

    streak = int(row.get("growth_streak") or 0)
    cagr = _f(row.get("profit_cagr"), 1, "%")
    lo, hi = _f(row.get("range_low"), 0), _f(row.get("range_high"), 0)

    return f"""
<article class="card">
  <header>
    <div class="ident">
      <span class="rank">{rank}</span>
      <div>
        <h2><span class="code">{code}</span> {name}</h2>
        <p class="sector">{sector}</p>
      </div>
    </div>
    <div class="score">
      <div class="score-num">{score}<span>/{smax}</span></div>
      <div class="bar"><i style="width:{pct}%"></i></div>
    </div>
  </header>
  <div class="grid">{metrics}</div>
  <div class="foot">
    <div class="note">{streak}期連続 増収増益 ・ 営業利益 年{cagr}成長<br>
      レンジ {lo}〜{hi}円</div>
    <div class="sparkwrap">{_sparkline(row.get("spark"))}<span>直近3か月</span></div>
  </div>
  <div class="chips">{chips}</div>
</article>"""


CSS = """
:root{
  --bg:#f6f7f9; --surface:#ffffff; --line:#e3e6ea;
  --ink:#16191d; --ink2:#5a616b; --ink3:#878e99;
  --accent:#1f6feb; --accent-soft:#e8f0fe;
  --good:#1a7f5a; --warn:#b06b00;
  --radius:14px;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0f1216; --surface:#171b21; --line:#282e37;
    --ink:#e9edf2; --ink2:#a3acb9; --ink3:#767f8c;
    --accent:#5a9bff; --accent-soft:#1b2940;
    --good:#4ec08d; --warn:#e0a23c;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1216; --surface:#171b21; --line:#282e37;
  --ink:#e9edf2; --ink2:#a3acb9; --ink3:#767f8c;
  --accent:#5a9bff; --accent-soft:#1b2940;
  --good:#4ec08d; --warn:#e0a23c;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",
    "Yu Gothic UI",sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-text-size-adjust:100%;
}
.wrap{max-width:1180px; margin:0 auto; padding:20px 16px 64px}
.top{margin-bottom:18px}
.top h1{font-size:19px; margin:0 0 4px; letter-spacing:.01em}
.top .sub{color:var(--ink2); font-size:13px; margin:0}
.stats{
  display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:14px 0 6px;
  max-width:520px;
}
.stat{
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:8px 10px;
}
.stat span{display:block; font-size:10.5px; color:var(--ink3); white-space:nowrap}
.stat b{font-size:17px; font-variant-numeric:tabular-nums}
.cards{display:grid; gap:12px; grid-template-columns:1fr}
@media(min-width:760px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(min-width:1140px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{
  background:var(--surface); border:1px solid var(--line);
  border-radius:var(--radius); padding:14px 14px 12px;
}
.card header{display:flex; justify-content:space-between; gap:12px; align-items:flex-start}
.ident{display:flex; gap:9px; min-width:0}
.rank{
  flex:none; width:22px; height:22px; border-radius:6px; margin-top:2px;
  background:var(--accent-soft); color:var(--accent);
  font-size:11px; font-weight:700; display:grid; place-items:center;
  font-variant-numeric:tabular-nums;
}
.card h2{font-size:14.5px; margin:0; font-weight:650; line-height:1.35}
.code{
  font-variant-numeric:tabular-nums; color:var(--ink2);
  font-size:12.5px; margin-right:4px;
}
.sector{margin:1px 0 0; font-size:11.5px; color:var(--ink3)}
.score{flex:none; width:92px; text-align:right}
.score-num{font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1.1}
.score-num span{font-size:11px; color:var(--ink3); font-weight:500}
.bar{height:5px; border-radius:3px; background:var(--line); margin-top:5px; overflow:hidden}
.bar i{display:block; height:100%; border-radius:3px; background:var(--accent)}
.grid{
  display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:10px;
  overflow:hidden; margin:12px 0 10px;
}
.cell{background:var(--surface); padding:6px 8px}
.cell span{display:block; font-size:10.5px; color:var(--ink3)}
.cell b{font-size:13.5px; font-weight:600; font-variant-numeric:tabular-nums}
.foot{display:flex; justify-content:space-between; align-items:flex-end; gap:10px}
.note{font-size:11.5px; color:var(--ink2)}
.sparkwrap{flex:none; text-align:right; color:var(--accent)}
.sparkwrap span{display:block; font-size:10px; color:var(--ink3); margin-top:-2px}
.chips{display:flex; flex-wrap:wrap; gap:5px; margin-top:10px}
.chip{
  font-size:10.5px; color:var(--ink2); background:var(--bg);
  border:1px solid var(--line); border-radius:999px; padding:2px 8px;
}
.chip b{color:var(--accent); margin-left:4px; font-variant-numeric:tabular-nums}
.empty{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:28px 18px; text-align:center; color:var(--ink2);
}
details.about{margin-top:26px; font-size:12.5px; color:var(--ink2)}
details.about summary{cursor:pointer; color:var(--ink); font-weight:600}
details.about table{border-collapse:collapse; margin-top:10px; width:100%}
details.about th,details.about td{
  border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; font-weight:400;
}
details.about th{color:var(--ink3); font-size:11px}
footer{margin-top:26px; font-size:11px; color:var(--ink3); line-height:1.7}
"""


def render_page(passed, allrows, cfg, meta, out_path):
    rows = passed.head(int(cfg["display"]["max_rows"]))
    cards = "\n".join(_card(r, i + 1) for i, (_, r) in enumerate(rows.iterrows()))
    if not len(rows):
        cards = ('<div class="empty">今日は必須条件をすべて満たす銘柄がありませんでした。'
                 '<br>条件をゆるめたい場合は config.yml の数字を調整してください。</div>')

    fails = [
        ("業績（連続増収増益）", int(allrows["ok_growth"].sum())),
        ("PER15倍以下", int(allrows["ok_per"].sum())),
        ("高値から下落", int(allrows["ok_drawdown"].sum())),
        ("レンジ形成", int(allrows["ok_range"].sum())),
        ("売買代金", int(allrows["ok_liquidity"].sum())),
    ]
    fail_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v:,} 銘柄</td></tr>" for k, v in fails
    )

    req = cfg["required"]
    cond_rows = "".join([
        f"<tr><td>業績</td><td>直近{req['consecutive_growth_years']}期連続で増収かつ増益</td></tr>",
        f"<tr><td>PER</td><td>{req['per_max']}倍以下（会社予想EPS基準）</td></tr>",
        f"<tr><td>下落</td><td>直近{req['high_lookback_days']}営業日の高値から{req['drawdown_min_pct']}%以上下落</td></tr>",
        f"<tr><td>レンジ</td><td>直近{req['range_window_days']}営業日の値幅が平均株価の{req['range_width_max_pct']}%以内、かつ高値を更新していない</td></tr>",
        f"<tr><td>流動性</td><td>平均売買代金 {int(req['min_avg_turnover_yen']):,}円以上</td></tr>",
    ])

    now = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    return_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>買い時スクリーニング</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>買い時スクリーニング</h1>
    <p class="sub">最終更新 {now}（日本時間）・株価基準 {html.escape(str(meta.get('price_date','—')))}{html.escape(meta.get('price_note',''))}</p>
    <div class="stats">
      <div class="stat"><span>調べた銘柄</span><b>{meta.get('universe',0):,}</b></div>
      <div class="stat"><span>条件通過</span><b>{len(passed):,}</b></div>
      <div class="stat"><span>表示</span><b>{len(rows):,}</b></div>
      <div class="stat"><span>最高点</span><b>{int(passed['score'].max()) if len(passed) else 0}</b></div>
    </div>
  </div>

  <div class="cards">
{cards}
  </div>

  <details class="about">
    <summary>条件と配点について</summary>
    <table>
      <tr><th>必須条件</th><th>内容</th></tr>
      {cond_rows}
    </table>
    <table>
      <tr><th>各条件を単独で満たした銘柄数</th><th></th></tr>
      {fail_rows}
    </table>
  </details>

  <footer>
    このページは自動生成です。数値はJPX公式のJ-Quants APIと、日中は遅延株価をもとに機械的に計算しています。<br>
    決算の実数値・会社予想は必ずご自身で確認してください。投資判断の責任は利用者にあります。
  </footer>
</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(return_html)
    return out_path


def dump_json(passed, meta, path):
    recs = []
    for code, r in passed.iterrows():
        recs.append({
            "code": str(code),
            "name": r.get("name"),
            "score": int(r.get("score") or 0),
            "price": None if r.get("price") is None or np.isnan(r.get("price")) else float(r["price"]),
            "per": None if np.isnan(r.get("per", np.nan)) else float(r["per"]),
            "div_yield": None if np.isnan(r.get("div_yield", np.nan)) else float(r["div_yield"]),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "stocks": recs}, f, ensure_ascii=False, indent=1)


# ====================================================================
# データの取得と保管
# ====================================================================
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


# ====================================================================
# 実行の流れ
# ====================================================================
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

    prices = update_prices(api, today)
    summary = update_summary(api, today)
    price_date = str(prices["Date"].max())
    val = fetch_valuation(api, price_date)

    info = build_universe(api, cfg)
    print(f"[run] 対象銘柄: {len(info):,}")
    prices = prices[prices["Code"].astype(str).str[:4].isin(info.index)]

    fund = build_fundamentals(summary, val)
    tech = build_technicals(prices, cfg["required"])
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
        override = fetch_latest(list(watch.index))
        note = (f" ＋ {dt.datetime.now(JST).strftime('%H:%M')}時点の遅延株価で更新"
                if override else "（日中株価が取得できず前日終値のまま）")

    passed, allrows = screen(fund, tech, info, cfg, price_override=override)
    print(f"[run] 必須条件を通過: {len(passed):,}銘柄")

    meta = {
        "updated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "mode": mode,
        "universe": int(len(allrows)),
        "price_date": price_date,
        "price_note": note,
    }
    render_page(passed, allrows, cfg, meta, os.path.join(DOCS, "index.html"))
    dump_json(passed, meta, os.path.join(DOCS, "latest.json"))
    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "passed": int(len(passed))}, f, ensure_ascii=False, indent=1)
    print("[run] ページを書き出しました: docs/index.html")

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "full")

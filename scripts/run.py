"""株の自動まとめ — これ1ファイルで全部動きます。

  python scripts/run.py full      … 全銘柄を一から調べ直す（深夜に1回）
  python scripts/run.py intraday  … 候補の株価だけ更新する（前場引け後と大引け後）
"""

import datetime as dt
import html
import json
import math
import os
import re
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
    def get(self, path, params=None, retries=6):
        params = dict(params or {})
        out = []
        while True:
            last = ""
            for attempt in range(retries):
                try:
                    r = self.session.get(f"{BASE}{path}", params=params, timeout=90)
                except requests.RequestException as e:
                    last = f"通信エラー: {e}"
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code == 200:
                    break
                last = f"{r.status_code} {r.text[:200]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt * 2, 60))
                    continue
                if r.status_code == 403:
                    raise JQuantsError(
                        f"{path} へのアクセスが拒否されました（403）。"
                        "ご契約のプランでこのデータが使えるか確認してください。"
                    )
                raise JQuantsError(f"{path} の取得に失敗（{last}）")
            else:
                raise JQuantsError(
                    f"{path} の取得に失敗（{retries}回試してだめでした）。最後の応答: {last}"
                )

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

    def covered_from(self):
        """契約で遡れる最も古い日付を調べる（範囲外を要求して教えてもらう）。"""
        try:
            r = self.session.get(
                f"{BASE}/markets/calendar",
                params={"from": "2000-01-01", "to": "2000-01-31"},
                timeout=30,
            )
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*~", r.text)
            if m:
                d = dt.date.fromisoformat(m.group(1)) + dt.timedelta(days=1)
                print(f"[jq] 契約で遡れるのは {d.isoformat()} 以降です", flush=True)
                return d
        except Exception as e:
            print(f"[jq] 遡及開始日の確認に失敗（無視して進めます）: {e}", flush=True)
        return None

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

# 四半期の段階（1Q=1 …… 本決算=4）。日本の決算短信は各期とも「期初からの累計」
STAGE = {"1Q": 1, "2Q": 2, "3Q": 3, "4Q": 4, "FY": 4}


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _code4(s):
    return s.astype(str).str[:4]


# ============================================================ 決算・指標
def _pct(now, before):
    """前年同期比（%）。前年が0以下なら比率に意味がないので出さない。"""
    if now is None or before is None:
        return np.nan
    try:
        now, before = float(now), float(before)
    except (TypeError, ValueError):
        return np.nan
    if np.isnan(now) or np.isnan(before) or before <= 0:
        return np.nan
    return (now / before - 1) * 100


def _quarterly(df: pd.DataFrame) -> pd.DataFrame:
    """直近の四半期決算について、前年同期比と会社予想に対する進捗率を出す。

    日本の決算短信の数字は「期初からの累計」なので、
      ・前年同期比 … 同じ段階（2Qなら2Q）の累計どうしを比べる
      ・進捗率 …… 累計 ÷（通期予想 × 段階/4）
    本決算の会社予想は翌期のものなので、進捗率にはその期の途中に
    出ていた予想（修正があれば修正後）を使う。
    """
    d = df.copy()
    for c in ("Sales", "OP", "FSales", "FOP"):
        d[c] = _num(d[c]) if c in d.columns else np.nan
    d["CurFYEn"] = d["CurFYEn"].astype(str)
    d = d[d["CurFYEn"].str.len() >= 7]
    d["stage"] = d["CurPerType"].astype(str).map(STAGE)
    d["is_fs"] = d["DocType"].astype(str).str.contains("FinancialStatements")

    rows = []
    for code, g in d.groupby("code", sort=False):
        g = g.sort_values(["CurFYEn", "DiscDate"])
        fs = g[g["is_fs"] & g["stage"].notna()]
        if not len(fs):
            continue
        fs = fs.sort_values(["CurFYEn", "stage", "DiscDate"])
        fs = fs.drop_duplicates(subset=["CurFYEn", "stage"], keep="last")
        last = fs.iloc[-1]
        stage = int(last["stage"])
        fy_end = last["CurFYEn"]

        # 前年の同じ段階
        same = fs[fs["stage"] == stage]
        prev = same.iloc[-2] if len(same) >= 2 else None
        s_yoy = _pct(last["Sales"], prev["Sales"]) if prev is not None else np.nan
        o_yoy = _pct(last["OP"], prev["OP"]) if prev is not None else np.nan

        # 進捗率に使う通期予想
        pool = g[g["CurFYEn"] == fy_end]
        pool = (pool[pool["DiscDate"] < last["DiscDate"]] if stage >= 4
                else pool[pool["DiscDate"] <= last["DiscDate"]])
        def latest(col):
            s = pool[col].dropna()
            return float(s.iloc[-1]) if len(s) else np.nan
        fsales, fop = latest("FSales"), latest("FOP")

        f = stage / 4.0
        s_prog = (last["Sales"] / (fsales * f) * 100
                  if fsales and fsales > 0 and not np.isnan(last["Sales"]) else np.nan)
        o_prog = (last["OP"] / (fop * f) * 100
                  if fop and fop > 0 and not np.isnan(last["OP"]) else np.nan)
        both = [x for x in (s_prog, o_prog) if not (x is None or np.isnan(x))]
        prog = min(both) if both else np.nan

        rows.append({
            "code": code,
            "q_label": f"{str(fy_end)[:7]} {last['CurPerType']}",
            "q_sales_yoy": s_yoy,
            "q_op_yoy": o_yoy,
            "q_progress": prog,
            "q_sales_progress": s_prog,
            "q_op_progress": o_prog,
        })
    if not rows:
        return pd.DataFrame(columns=["q_label", "q_sales_yoy", "q_op_yoy",
                                     "q_progress", "q_sales_progress",
                                     "q_op_progress"]).rename_axis("code")
    return pd.DataFrame(rows).set_index("code")


def build_fundamentals(summary: pd.DataFrame, val: pd.DataFrame,
                       dividend_years: int = 5) -> pd.DataFrame:
    """銘柄ごとに「業績の並び」と「1株あたり指標」をまとめる。"""
    df = summary[summary["Code"].notna()].copy()
    for col in ("DocType", "CurPerType", "CurFYEn", "Sales", "OP", "EqAR",
                "FDivAnn", "DivAnn", "DiscDate", "NP"):
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
    fy["DivAnn"] = _num(fy["DivAnn"])
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

        # 配当の並び（直近 dividend_years 期分）を見て、減配があったか
        divs = g["DivAnn"].to_numpy(dtype=float)
        divs = divs[~np.isnan(divs)][-dividend_years:]
        cut = False
        for i in range(1, len(divs)):
            if divs[i] < divs[i - 1] - 1e-9:
                cut = True
                break

        rows.append({
            "code": code,
            "fy_count": n,
            "growth_streak": streak,
            "latest_fy": years[-1],
            "op_margin": (op[-1] / sales[-1] * 100) if sales[-1] else np.nan,
            "profit_cagr": cagr,
            "div_years": len(divs),
            "div_cut": cut,
            "div_series": [float(x) for x in divs],
        })

    if not rows:
        raise RuntimeError(
            "本決算（通期）のデータが1件も取れませんでした。"
            "ご契約のプランを確認してください。"
        )
    out = pd.DataFrame(rows).set_index("code")

    out = out.join(_quarterly(df), how="left")

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
        prev1 = float(close[-2]) if len(close) >= 2 else np.nan
        prev3 = float(close[-4]) if len(close) >= 4 else np.nan
        rows.append({
            "code": code,
            "date": g["Date"].iloc[-1],
            "price": raw_close[-1] if not np.isnan(raw_close[-1]) else last_adj,
            "adj_price": last_adj,
            "prev1": prev1,
            "prev3": prev3,
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
# (設定キー, 計算列, 表示名, 単位, 小数桁, 大きいほど良いか)
METRIC_INFO = [
    ("per",              "per",          "PER",        "倍", 1, False),
    ("q_sales_yoy",      "q_sales_yoy",  "四半期増収率", "%", 1, True),
    ("q_op_yoy",         "q_op_yoy",     "四半期増益率", "%", 1, True),
    ("q_sales_progress", "q_sales_progress", "売上進捗率", "%", 0, True),
    ("q_op_progress",    "q_op_progress",    "利益進捗率", "%", 0, True),
    ("drawdown",         "drawdown",     "下落率",     "%",  1, True),
    ("range_days",       "range_days",   "レンジ日数", "日", 0, True),
    ("operating_margin", "op_margin",    "営業利益率", "%",  1, True),
    ("profit_cagr",      "profit_cagr",  "利益成長",   "%",  1, True),
    ("dividend_yield",   "div_yield",    "配当利回り", "%",  2, True),
    ("payout_ratio",     "payout",       "配当性向",   "%",  0, True),
    ("pbr",              "pbr",          "PBR",        "倍", 2, False),
    ("equity_ratio",     "equity_ratio", "自己資本比率", "%", 0, True),
]
METRICS = [(k, c) for k, c, *_ in METRIC_INFO]


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
    # 前日比と直近3営業日の騰落率（株価は分割調整済みで比較）
    df["chg_1d"] = (df["adj_price"] / df["prev1"] - 1) * 100
    df["chg_3d"] = (df["adj_price"] / df["prev3"] - 1) * 100

    c_growth = df["growth_streak"] >= int(req["consecutive_growth_years"])
    c_per = df["per"].between(float(req["per_min"]) + 1e-9, float(req["per_max"]))
    c_liq = df["avg_turnover"] >= float(req["min_avg_turnover_yen"])
    c_div = (~df["div_cut"].fillna(False)) if req.get("no_dividend_cut", True) \
        else pd.Series(True, index=df.index)

    df["ok_growth"], df["ok_per"] = c_growth, c_per
    df["ok_liquidity"], df["ok_dividend"] = c_liq, c_div
    passed = df[c_growth & c_per & c_liq & c_div].copy()

    scores, details = [], []
    for _, row in passed.iterrows():
        total, det = 0, {}
        for key, col in METRICS:
            pts, label = _tier(row.get(col), sc.get(key, []))
            det[key] = {"points": pts, "label": label}
            total += pts
        scores.append(total)
        details.append(det)
    passed["score"] = scores
    passed["score_detail"] = details
    passed["score_max"] = sum(
        max((r["points"] for r in rules), default=0) for rules in sc.values()
    )

    # 急落の印（前日比、または直近3営業日の下げ）
    al = cfg.get("alerts", {})
    d1 = float(al.get("drop_1d_pct", -10.0))
    d3 = float(al.get("drop_3d_pct", -15.0))
    passed["alert"] = (passed["chg_1d"] <= d1) | (passed["chg_3d"] <= d3)

    passed = passed.sort_values(["score", "per"], ascending=[False, True])
    return passed, df


# ====================================================================
# HTMLページの生成
# ====================================================================
def _f(v, digits=1, suffix="", dash="—"):
    try:
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return dash
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return dash


def _sortval(v, higher_is_better):
    """並べ替え用の数値。欠損は必ず最後に来るようにする。"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            raise ValueError
    except (TypeError, ValueError):
        return -1e18
    return f if higher_is_better else -f


def _sparkline(values, w=96, h=26):
    vals = [v for v in (values or []) if v is not None and not math.isnan(v)]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = (w - 4) / (len(vals) - 1)
    pts = " ".join(f"{2 + i * step:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'aria-hidden="true"><polyline points="{pts}" fill="none" '
            f'stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>')


def _row(r, rank, smax):
    code = html.escape(str(r.name))
    name = html.escape(str(r.get("name") or ""))
    sector = html.escape(str(r.get("sector") or ""))
    score = int(r.get("score") or 0)
    det = r.get("score_detail") or {}

    cells, attrs = [], [
        f'data-s-score="{score}"', f'data-v-score="{score}"',
    ]
    for key, col, _label, unit, digits, hib in METRIC_INFO:
        pts = int((det.get(key) or {}).get("points") or 0)
        val = r.get(col)
        attrs.append(f'data-s-{key}="{pts}"')
        attrs.append(f'data-v-{key}="{_sortval(val, hib):.6f}"')
        cls = " has" if pts else ""
        cells.append(
            f'<td class="m{cls}"><span class="val">{_f(val, digits, unit)}</span>'
            f'<span class="pt">{"+" + str(pts) if pts else "・"}</span></td>'
        )

    pct = max(3, min(100, round(score / smax * 100))) if smax else 0
    streak = int(r.get("growth_streak") or 0)
    dy = int(r.get("div_years") or 0)
    tag = '<span class="tag">急落</span>' if bool(r.get("alert")) else ""
    attrs.append(f'data-r-chg1="{_raw(r.get("chg_1d"))}"')
    attrs.append(f'data-r-chg3="{_raw(r.get("chg_3d"))}"')

    short = _short(r.get("name"))
    return f"""<tr {' '.join(attrs)}>
<th class="name" scope="row" title="{name}"><div class="nb"><span class="rank">{rank}</span>
  {_link(r.name, html.escape(short) + tag, "ident " + _name_cls(short))}</div></th>
<td class="code">{code}</td>
<td class="sector">{sector}</td>
<td class="total"><b>{score}</b><span class="track"><i style="width:{pct}%"></i></span></td>
{''.join(cells)}
<td class="{_chg_cls(r.get('chg_1d'))}">{_f(r.get("chg_1d"), 1, "%")}</td>
<td class="{_chg_cls(r.get('chg_3d'))}">{_f(r.get("chg_3d"), 1, "%")}</td>
<td class="px">{_f(r.get("price"), 0, "円")}</td>
<td class="note">{streak}期連続増収増益<br>配当{dy}期<br>{html.escape(str(r.get("q_label") or "四半期なし"))}</td>
<td class="sp">{_sparkline(r.get("spark"))}</td>
</tr>"""


def _raw(v):
    """昇順で並べるときの値。欠損は必ず最後に来るようにする。"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            raise ValueError
        return f"{f:.6f}"
    except (TypeError, ValueError):
        return "1e18"


ABBREV = [
    ("ホールディングス", "HD"), ("ホールディングズ", "HD"), ("ホールディング", "HD"),
    ("インターナショナル", "IN"), ("コンサルティング", "CS"),
    ("エンジニアリング", "EG"), ("コーポレーション", "CO"),
    ("ソリューションズ", "SL"), ("ソリューション", "SL"),
    ("テクノロジーズ", "TC"), ("テクノロジー", "TC"),
    ("インベストメント", "IV"), ("マネジメント", "MG"),
    ("パートナーズ", "PT"), ("プロダクツ", "PD"), ("グループ", "G"),
]


def _short(name):
    """長い社名を詰める。よくある後ろの語を2文字までの略称にする。"""
    s = str(name or "")
    for a, b in (("株式会社", ""), ("（株）", ""), ("(株)", "")):
        s = s.replace(a, b)
    for a, b in ABBREV:
        s = s.replace(a, b)
    return s.replace("・", "").strip()


LINK_TMPL = [""]      # config.yml の display.link_url をここに入れて使う


def _link(code, inner, cls=""):
    """銘柄名をクリックしたら外部サイトに飛ぶようにする。"""
    if not LINK_TMPL[0]:
        return f'<span class="{cls}">{inner}</span>' if cls else inner
    url = html.escape(LINK_TMPL[0].replace("{code}", str(code)), quote=True)
    c = f' class="lnk {cls}"' if cls else ' class="lnk"'
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer"{c}>{inner}</a>'


def _name_cls(s):
    n = len(s)
    if n >= 13:
        return "n3"
    if n >= 9:
        return "n2"
    return "n1"


def _chg_cls(v):
    try:
        return "chg down" if float(v) < 0 else "chg"
    except (TypeError, ValueError):
        return "chg"


def _alert_block(passed, smax):
    """急落した銘柄だけを一覧の上に別枠で出す。"""
    if "alert" not in passed.columns:
        return ""
    hit = passed[passed["alert"].fillna(False)]
    if not len(hit):
        return ""
    hit = hit.sort_values("chg_1d")
    rows = ""
    for code, r in hit.iterrows():
        rows += (
            f'<tr><th scope="row">'
            + _link(code, "<b>" + html.escape(str(r.get("name") or "")) + "</b>")
            + f'<small>{html.escape(str(code))} ・ {html.escape(str(r.get("sector") or ""))}</small></th>'
            f'<td class="chg down">{_f(r.get("chg_1d"), 1, "%")}</td>'
            f'<td class="chg down">{_f(r.get("chg_3d"), 1, "%")}</td>'
            f'<td>{_f(r.get("per"), 1, "倍")}</td>'
            f'<td>{_f(r.get("div_yield"), 2, "%")}</td>'
            f'<td>{_f(r.get("price"), 0, "円")}</td>'
            f'<td class="sc">{int(r.get("score") or 0)}<span>/{smax}</span></td></tr>'
        )
    return f"""
  <section class="alert">
    <h2><span class="mark" aria-hidden="true">▼</span> 急落中 <em>{len(hit)}銘柄</em></h2>
    <p>条件を通った銘柄のうち、前日比−10%以下、または直近3営業日で−15%以下のもの。下の一覧にも同じ銘柄が入っています。</p>
    <div class="scroll">
      <table class="al">
        <thead><tr><th scope="col">銘柄</th><th scope="col">前日比</th>
        <th scope="col">3営業日</th><th scope="col">PER</th>
        <th scope="col">配当利回り</th><th scope="col">株価</th><th scope="col">合計点</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </section>"""


CSS = """
:root{
  --bg:#f6f7f9; --surface:#fff; --line:#e3e6ea; --line2:#eef0f3;
  --ink:#16191d; --ink2:#5a616b; --ink3:#8a919c;
  --accent:#1f6feb; --accent-soft:#e8f0fe;
  --warn:#b4341c; --warn-soft:#fdeeea; --warn-line:#f2c4b8;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f1216; --surface:#171b21; --line:#282e37; --line2:#1f242b;
  --ink:#e9edf2; --ink2:#a3acb9; --ink3:#767f8c;
  --accent:#5a9bff; --accent-soft:#1b2940;
  --warn:#ff8a6b; --warn-soft:#2a1a16; --warn-line:#4a2a20;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Yu Gothic UI",sans-serif;
  font-size:14px;line-height:1.5;-webkit-text-size-adjust:100%}
.wrap{max-width:none;margin:0 auto;padding:18px 14px 36px}
h1{font-size:18px;margin:0 0 3px}
.sub{color:var(--ink2);font-size:12px;margin:0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:13px 0 10px;max-width:520px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:7px 10px}
.stat span{display:block;font-size:10.5px;color:var(--ink3);white-space:nowrap}
.stat b{font-size:17px;font-variant-numeric:tabular-nums}
.hint{font-size:11.5px;color:var(--ink3);margin:0 0 8px}
.scroll{overflow:auto;-webkit-overflow-scrolling:touch;
  background:var(--surface);border:1px solid var(--line);border-radius:12px}
table{border-collapse:separate;border-spacing:0;width:100%;font-variant-numeric:tabular-nums}
th,td{white-space:nowrap;padding:7px 9px;border-bottom:1px solid var(--line2);text-align:right}
thead th{position:sticky;top:0;z-index:3;background:var(--surface);
  font-size:11px;font-weight:600;color:var(--ink2);border-bottom:1px solid var(--line);
  text-align:right;line-height:1.3}
thead th.s{cursor:pointer;user-select:none}
thead th.s:hover{color:var(--accent)}
thead th.s::after{content:"⌄";opacity:.35;margin-left:3px;font-size:10px}
thead th.s.up::after{content:"⌃"}
thead th.s.on{color:var(--accent)}
thead th.s.on::after{opacity:1}
th.name,thead th.name{position:sticky;left:0;z-index:4;background:var(--surface);
  text-align:left;min-width:170px;max-width:170px;white-space:normal;
  border-right:1px solid var(--line)}
td.code{color:var(--ink2);font-size:12.5px}
td.sector{color:var(--ink3);font-size:11.5px;text-align:left;min-width:86px}
tbody th.name{z-index:2;font-weight:400}
.nb{display:flex;gap:7px;align-items:flex-start}
.rank{flex:none;display:grid;place-items:center;width:19px;height:18px;border-radius:5px;
  background:var(--accent-soft);color:var(--accent);font-size:10px;font-weight:700;
  margin-top:1px}
.ident{min-width:0;font-weight:600;line-height:1.25;word-break:break-word}
a.lnk{color:inherit;text-decoration:none;display:inline-block}
a.lnk:hover,a.lnk:focus-visible{color:var(--accent);text-decoration:underline}
table.al a.lnk b{font-weight:600}
.ident.n1{font-size:13px}
.ident.n2{font-size:11.5px}
.ident.n3{font-size:10px}
td.total{min-width:80px}
td.total b{font-size:16px;font-weight:700}
td.total .track{display:block;height:3px;border-radius:2px;background:var(--line);margin-top:4px}
td.total i{display:block;height:100%;border-radius:2px;background:var(--accent)}
/* PCではページ全体を画面の高さに収め、表が残りを全部使う。
   こうすると横スクロールのバーが常に画面内に出る。 */
@media(min-width:760px){
  .wrap{height:100dvh;display:flex;flex-direction:column;padding-bottom:10px}
  .tabs{flex:none}
  .panel{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}
  #panel-brief{overflow:auto;padding-right:2px}
  #panel-screen > *{flex:none}
  .alert{margin-bottom:10px}
  .alert .scroll{max-height:24vh}
  .scroll.main{flex:1 1 auto;min-height:240px}
  details.about{margin-top:10px}
  footer{margin-top:8px}
}
@media(max-width:560px){
  th.name,thead th.name{min-width:66px;max-width:66px;padding-left:5px;padding-right:4px}
  .nb{gap:4px}
  .rank{width:14px;height:14px;font-size:8.5px;border-radius:4px}
  .ident.n1{font-size:11px}
  .ident.n2{font-size:9.5px}
  .ident.n3{font-size:8.5px}
  th,td{padding:6px 7px}
  td.m{min-width:64px}
  td.code{font-size:11px;min-width:auto}
  td.sector{font-size:10px;min-width:56px}
  td.total{min-width:66px}
  .tag{margin-left:0;font-size:8.5px;padding:0 3px}
}
td.m{min-width:72px}
td.m .val{display:block;font-size:13px}
td.m .pt{display:block;font-size:10.5px;color:var(--ink3)}
td.m.has .pt{color:var(--accent);font-weight:700}
td.px{color:var(--ink2)}
td.chg{min-width:62px;color:var(--ink2)}
td.chg.down{color:var(--warn);font-weight:600}
.tag{display:inline-block;margin-left:5px;padding:0 5px;border-radius:4px;
  background:var(--warn-soft);color:var(--warn);border:1px solid var(--warn-line);
  font-size:9.5px;font-weight:700;vertical-align:1px}
.alert{background:var(--warn-soft);border:1px solid var(--warn-line);
  border-radius:12px;padding:12px 12px 4px;margin:0 0 14px}
.alert h2{font-size:14px;margin:0 0 3px;color:var(--warn)}
.alert h2 em{font-style:normal;font-weight:600;font-size:12px}
.alert .mark{font-size:11px}
.alert p{margin:0 0 9px;font-size:11.5px;color:var(--ink2)}
.alert .scroll{border-color:var(--warn-line)}
table.al th[scope="row"],table.al thead th:first-child{position:sticky;left:0;z-index:3;
  background:var(--warn-soft);text-align:left;white-space:normal;
  min-width:150px;max-width:150px;border-right:1px solid var(--warn-line)}
table.al thead th{background:var(--warn-soft)}
table.al thead th:first-child{z-index:6}
table.al th,table.al td{border-bottom:1px solid var(--warn-line)}
table.al th[scope="row"] b{display:block;font-size:13px;font-weight:600}
table.al th[scope="row"] small{display:block;font-size:10.5px;color:var(--ink3)}
table.al td.sc span{font-size:10px;color:var(--ink3)}
td.note{font-size:10px;color:var(--ink3);line-height:1.35;text-align:left}
td.sp{color:var(--accent);padding-right:12px}
tbody tr:hover th.name,tbody tr:hover td{background:var(--line2)}
.empty{padding:30px 16px;text-align:center;color:var(--ink2)}
details.about{margin-top:22px;font-size:12.5px;color:var(--ink2)}
details.about summary{cursor:pointer;color:var(--ink);font-weight:600}
details.about table{margin-top:10px;width:auto;max-width:820px}
details.about th,details.about td{border-bottom:1px solid var(--line);padding:5px 10px;
  text-align:left;font-weight:400;white-space:normal}
details.about th{color:var(--ink3);font-size:11px}
footer{margin-top:22px;font-size:11px;color:var(--ink3);line-height:1.7}
"""

JS = """
(function(){
  var tbl=document.getElementById('main'); if(!tbl) return;
  var tb=tbl.querySelector('tbody'); if(!tb) return;
  var heads=tbl.querySelectorAll('thead th.s');
  function sort(key,th){
    var asc=th&&th.getAttribute('data-mode')==='asc';
    var rows=Array.prototype.slice.call(tb.querySelectorAll('tr'));
    rows.sort(asc?function(a,b){
      return (+a.getAttribute('data-r-'+key))-(+b.getAttribute('data-r-'+key));
    }:function(a,b){
      var pa=+a.getAttribute('data-s-'+key), pb=+b.getAttribute('data-s-'+key);
      if(pb!==pa) return pb-pa;
      var va=+a.getAttribute('data-v-'+key), vb=+b.getAttribute('data-v-'+key);
      return vb-va;
    });
    rows.forEach(function(r,i){
      tb.appendChild(r);
      var k=r.querySelector('.rank'); if(k) k.textContent=i+1;
    });
    heads.forEach(function(h){h.classList.toggle('on',h===th);});
    try{localStorage.setItem('sortKey',key);}catch(e){}
  }
  heads.forEach(function(th){
    th.addEventListener('click',function(){sort(th.dataset.key,th);});
  });
  var saved=null; try{saved=localStorage.getItem('sortKey');}catch(e){}
  if(saved){
    for(var i=0;i<heads.length;i++){
      if(heads[i].dataset.key===saved){sort(saved,heads[i]);break;}
    }
  }
})();
"""


BRIEF_CSS = """
/* ---------- タブ ---------- */
.tabs{display:flex;gap:6px;margin:0 0 14px;border-bottom:1px solid var(--line)}
.tab{
  appearance:none;background:none;border:0;border-bottom:2px solid transparent;
  font:inherit;font-size:14px;font-weight:600;color:var(--ink3);
  padding:8px 14px 9px;cursor:pointer;margin-bottom:-1px;white-space:nowrap;
}
.tab:hover{color:var(--ink2)}
.tab.on{color:var(--accent);border-bottom-color:var(--accent)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:6px}
.panel[hidden]{display:none}

/* ---------- 朝のブリーフ ---------- */
.bhead{margin-bottom:12px}
.brow{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.stamp{font-size:15px;font-weight:700;color:var(--accent);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.bmeta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:7px}
.chip{font-size:11px;padding:3px 9px;border-radius:999px;white-space:nowrap;
  border:1px solid var(--line);color:var(--ink2);background:var(--surface);
  font-variant-numeric:tabular-nums}
.chip.ok{color:#17788c;border-color:#17788c66}
.chip.warn{color:var(--warn);background:var(--warn-soft);border-color:var(--warn-line)}
select.picker{font:inherit;font-size:16px;padding:5px 10px;border-radius:8px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink);min-height:36px}
.bbody{max-width:760px}
.state{margin-top:8px;padding:22px 18px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;color:var(--ink2);font-size:14.5px;line-height:1.85}
.state strong{display:block;color:var(--ink);font-size:15.5px;margin-bottom:7px}
.snap{background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.band{font-size:11px;letter-spacing:.16em;font-weight:700;color:var(--ink3);
  background:var(--bg);padding:7px 15px;border-block:1px solid var(--line)}
.band:first-child{border-top:0}
.snap .row{padding:11px 15px;border-bottom:1px solid var(--line2)}
.snap .row:last-child{border-bottom:0}
.r-top{display:flex;align-items:baseline;gap:4px 8px;flex-wrap:wrap}
.lab{flex:1 1 auto;min-width:0;font-size:14px;font-weight:500;color:var(--ink);
  overflow-wrap:break-word}
.lab .n{font-size:11px;color:var(--ink3);font-weight:700;margin-right:7px;
  font-variant-numeric:tabular-nums}
.asof{margin-left:auto;font-size:11.5px;color:var(--ink3);white-space:nowrap}
.r-bot{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-top:3px}
.snap .val{font-size:20px;font-weight:700;color:var(--ink);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.snap .chg{font-size:13px;text-align:right;font-variant-numeric:tabular-nums}
/* 日本式：上げは赤、下げは青 */
.snap .chg.up{color:#c4342a}
.snap .chg.down{color:#17788c}
.snap .chg.flat{color:var(--ink3)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .snap .chg.up{color:#f0705f}
  :root:not([data-theme="light"]) .snap .chg.down{color:#4cbad0}}
.snap .row.bad .val{color:var(--warn)}
.gauge{padding:0 15px 12px;margin-top:-3px}
.gauge .track{height:6px;background:var(--bg);border-radius:3px;overflow:hidden}
.gauge .track i{display:block;height:100%;background:var(--accent);border-radius:3px}
.gauge .ends{display:flex;justify-content:space-between;font-size:10px;color:var(--ink3);margin-top:3px}
.btools{display:flex;justify-content:flex-end;margin-top:16px}
.btools button{font:inherit;font-size:13px;color:var(--ink2);background:none;
  border:1px solid var(--line);border-radius:999px;padding:7px 15px;cursor:pointer}
details.sec{margin-top:9px;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;overflow:hidden}
details.sec > summary{list-style:none;cursor:pointer;padding:14px 15px;
  display:flex;align-items:center;gap:10px;font-size:15px;font-weight:700;color:var(--ink)}
details.sec > summary::-webkit-details-marker{display:none}
.caret{flex:0 0 auto;width:8px;height:8px;border-right:2px solid var(--accent);
  border-bottom:2px solid var(--accent);transform:rotate(45deg);transition:transform .18s}
details[open] > summary .caret{transform:rotate(-135deg)}
.sec-body{padding:13px 15px 17px;border-top:1px solid var(--line)}
.prose{color:var(--ink2);font-size:15px;line-height:1.9}
.prose h3{font-size:12.5px;font-weight:700;margin:20px 0 8px;color:var(--accent);letter-spacing:.05em}
.prose h3:first-child{margin-top:0}
.prose p{margin:0 0 13px;overflow-wrap:anywhere}
.prose p.head{font-weight:700;color:var(--ink);margin:18px 0 6px}
.prose p.head:first-child{margin-top:0}
.prose p.impact{color:var(--ink3);font-size:14px;background:var(--bg);
  border-left:3px solid var(--line);border-radius:0 8px 8px 0;padding:10px 12px;margin:0 0 16px}
.prose ul{margin:0 0 13px;padding-left:1.2em}
.prose li{margin-bottom:6px}
.prose a{color:var(--accent);text-underline-offset:3px}
.prose .listing{background:var(--bg);border-radius:10px;padding:5px 3px;margin:0 0 16px}
.prose .listing div{padding:9px 11px;font-size:14.5px;border-bottom:1px solid var(--line)}
.prose .listing div:last-child{border-bottom:0}
.bsources{margin-top:22px;padding-top:14px;border-top:1px solid var(--line)}
.bsources .t{font-size:11px;letter-spacing:.14em;color:var(--ink3);font-weight:700;margin-bottom:7px}
.bsources ul{list-style:none;padding:0;margin:0;font-size:14px}
.bsources a{color:var(--ink2);text-decoration:none;display:inline-block;padding:5px 0}
.bfoot{margin-top:20px;font-size:12px;color:var(--ink3);line-height:1.8;padding-bottom:20px}
@media(max-width:560px){
  .tab{padding:8px 10px 9px;font-size:13px}
  .prose{font-size:14.5px}
}
"""


BRIEF_JS = r"""
(function(){
  var $=function(id){return document.getElementById(id);};
  function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}

  /* ---------- タブ ---------- */
  var tabs=document.querySelectorAll(".tab");
  function showTab(name){
    tabs.forEach(function(b){b.classList.toggle("on",b.dataset.tab===name);});
    var b=$("panel-brief"), s=$("panel-screen");
    if(b) b.hidden = name!=="brief";
    if(s) s.hidden = name!=="screen";
    try{localStorage.setItem("tab",name);}catch(e){}
    if(location.hash!=="#"+name){ try{history.replaceState(null,"","#"+name);}catch(e){} }
    if(name==="brief") loadBrief();
  }
  tabs.forEach(function(b){b.addEventListener("click",function(){showTab(b.dataset.tab);});});

  var want=(location.hash||"").replace("#","");
  if(want!=="brief"&&want!=="screen"){
    try{want=localStorage.getItem("tab")||"";}catch(e){want="";}
  }
  if(want!=="brief"&&want!=="screen"){
    var hh=new Date(Date.now()+(new Date().getTimezoneOffset()+540)*60000).getHours();
    want = hh<12 ? "brief" : "screen";
  }
  showTab(want);

  /* ---------- かんたんなMarkdown ---------- */
  function inline(s){
    s=esc(s);
    s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
    s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
    s=s.replace(/`([^`]+)`/g,"<code>$1</code>");
    return s;
  }
  function md(src){
    if(!src) return "";
    var lines=String(src).split(/\r?\n/),out=[],list=null,box=false;
    function cl(){if(list){out.push("</"+list+">");list=null;}}
    function cb(){if(box){out.push("</div>");box=false;}}
    for(var i=0;i<lines.length;i++){
      var t=lines[i].trim();
      if(!t){cl();cb();continue;}
      if(/^###\s/.test(t)){cl();cb();out.push("<h3>"+inline(t.replace(/^###\s*/,""))+"</h3>");continue;}
      if(/^##\s/.test(t)){cl();cb();out.push("<h3>"+inline(t.replace(/^##\s*/,"").replace(/^■\s*/,""))+"</h3>");continue;}
      if(/^〈.+〉$/.test(t)){cl();cb();out.push("<h3>"+inline(t)+"</h3>");out.push('<div class="listing">');box=true;continue;}
      if(box){
        if(/^\d+\./.test(t)){out.push("<div>"+inline(t)+"</div>");continue;}
        if(/^[-*・]\s*/.test(t)){out.push("<div>"+inline(t.replace(/^[-*・]\s*/,""))+"</div>");continue;}
        cb();
      }
      if(/^[-*]\s+/.test(t)){cb();
        if(list!=="ul"){cl();out.push("<ul>");list="ul";}
        out.push("<li>"+inline(t.replace(/^[-*]\s+/,""))+"</li>");continue;}
      cl();
      if(/^\*\*.+\*\*$/.test(t)){out.push('<p class="head">'+inline(t)+"</p>");continue;}
      if(/^(影響|補足)[：:]/.test(t)){out.push('<p class="impact">'+inline(t)+"</p>");continue;}
      out.push("<p>"+inline(t)+"</p>");
    }
    cl();cb();
    return out.join("");
  }
  function sections(src){
    if(!src) return [];
    var lines=String(src).split(/\r?\n/),secs=[],cur=null;
    for(var i=0;i<lines.length;i++){
      var t=lines[i];
      if(/^##\s+/.test(t.trim())){
        if(cur) secs.push(cur);
        cur={title:t.trim().replace(/^##\s*/,"").replace(/^■\s*/,"").trim(),lines:[]};
      } else if(cur){cur.lines.push(t);}
      else if(t.trim()){cur={title:"",lines:[t]};}
    }
    if(cur) secs.push(cur);
    return secs.map(function(s){return {title:s.title,body:s.lines.join("\n").trim()};})
               .filter(function(s){return s.title||s.body;});
  }
  function opensBy(t){return /押さえ|注目予定|スナップショット|取得に失敗|保存/.test(t||"");}

  /* ---------- スナップショット ---------- */
  function dir(c){
    var s=String(c||"");
    if(/▼/.test(s)) return "down";
    if(/▲/.test(s)) return "up";
    if(/[-−]\s*\d/.test(s)&&!/\+/.test(s)) return "down";
    if(/\+/.test(s)) return "up";
    return "flat";
  }
  function arrow(d){return d==="up"?"▲":d==="down"?"▼":"—";}
  function trimChange(c){return String(c||"").replace(/^(前営業日終値比|前営業日比|前日終値比|前日比)\s*/,"");}
  function splitVal(v){
    var m=String(v||"").match(/^(.*?)（(.+)）\s*$/);
    return m?{main:m[1],asof:m[2]}:{main:String(v||""),asof:""};
  }
  var BANDS={1:"日本株",5:"米国株",7:"為替・金利",10:"半導体・商品",12:"市場心理"};
  function snapshot(items){
    var h='<div class="snap">';
    items.forEach(function(it,i){
      var n=i+1;
      if(BANDS[n]) h+='<div class="band">'+esc(BANDS[n])+"</div>";
      var sv=splitVal(it.value),d=dir(it.change),ok=it.ok!==false;
      h+='<div class="row'+(ok?"":" bad")+'"><div class="r-top">'
       + '<span class="lab"><span class="n">'+n+"</span>"+esc(it.label)+"</span>"
       + (sv.asof?'<span class="asof">'+esc(sv.asof)+"</span>":"")
       + '</div><div class="r-bot"><span class="val">'+esc(sv.main)+"</span>"
       + '<span class="chg '+d+'">'+arrow(d)+" "+esc(trimChange(it.change))+"</span></div></div>";
      if(/Fear|Greed/i.test(String(it.label)+String(it.change))){
        var v=parseFloat(String(it.value).replace(/[^\d.]/g,""));
        if(isFinite(v)){
          h+='<div class="gauge"><div class="track"><i style="width:'+Math.max(2,Math.min(100,v))+'%"></i></div>'
           + '<div class="ends"><span>0 極度の恐怖</span><span>100 極度の強気</span></div></div>';
        }
      }
    });
    return h+"</div>";
  }

  /* ---------- 描画 ---------- */
  var DATES=[],CACHE={},LOADED=false;
  function empty(msg,detail){
    $("b-main").innerHTML='<div class="state"><strong>'+esc(msg)+"</strong>"+detail+"</div>";
  }
  function render(doc){
    $("b-stamp").textContent=doc.date||"";
    var m=[];
    if(doc.period) m.push('<span class="chip">'+esc(doc.period)+"</span>");
    if(doc.ok_count!=null&&doc.total!=null){
      m.push('<span class="chip '+(doc.ok_count===doc.total?"ok":"warn")+'">指標 '+doc.ok_count+"/"+doc.total+"</span>");
    }
    if(DATES.length>1){
      m.push('<select class="picker" id="b-pick" aria-label="日付を選ぶ">'
        +DATES.map(function(d){return '<option value="'+esc(d)+'"'+(d===doc.date?" selected":"")+">"+esc(d)+"</option>";}).join("")
        +"</select>");
    }
    $("b-meta").innerHTML=m.join("");
    var pick=$("b-pick");
    if(pick) pick.addEventListener("change",function(){show(pick.value);});

    var h="";
    if(doc.snapshot_items&&doc.snapshot_items.length) h+=snapshot(doc.snapshot_items);
    var stream=[doc.note,doc.lead,doc.markdown]
      .concat((doc.parts||[]).map(function(p){return typeof p==="string"?p:(p&&p.markdown);}))
      .concat([doc.outlook])
      .filter(function(x){return x&&String(x).trim();}).join("\n\n");
    var secs=sections(stream);
    if(secs.length){
      h+='<div class="btools"><button type="button" id="b-toggle">すべて開く</button></div>';
      secs.forEach(function(s){
        h+='<details class="sec"'+(opensBy(s.title)?" open":"")+">"
         + '<summary><span class="caret"></span><span>'+esc(s.title||"本文")+"</span></summary>"
         + '<div class="sec-body"><div class="prose">'+md(s.body)+"</div></div></details>";
      });
    }
    if(doc.sources_markdown){
      var links=[];
      String(doc.sources_markdown).replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,function(_,tt,u){
        links.push('<li><a href="'+esc(u)+'" target="_blank" rel="noopener">'+esc(tt)+"</a></li>");return "";});
      if(links.length) h+='<div class="bsources"><div class="t">出典</div><ul>'+links.join("")+"</ul></div>";
    }
    h+='<div class="bfoot">数値はGitHub Actionsが平日朝に自動取得したもの、本文は定期タスクが保存したものです。</div>';
    $("b-main").innerHTML=h;

    var btn=$("b-toggle");
    if(btn) btn.addEventListener("click",function(){
      var all=$("b-main").querySelectorAll("details.sec");
      var open=btn.textContent.indexOf("開く")>=0;
      for(var i=0;i<all.length;i++) all[i].open=open;
      btn.textContent=open?"すべて閉じる":"すべて開く";
    });
  }
  function show(date){
    if(CACHE[date]) return render(CACHE[date]);
    fetch("briefs/"+date+".json",{cache:"no-cache"}).then(function(r){
      if(!r.ok) throw new Error(r.status); return r.json();
    }).then(function(d){
      if(!d.date) d.date=date;
      CACHE[date]=d; render(d);
    })["catch"](function(){ empty("その日のブリーフを読めませんでした","日付を選び直してください。"); });
  }
  /* 日付一覧のファイルは持たず、ファイルの有無を直接確かめる。
     こうすると毎朝の書き込みは「その日のJSONを1本置く」だけで済む。 */
  function jstYmd(back){
    var t=Date.now()+(new Date().getTimezoneOffset()*60000)+9*3600000-back*86400000;
    var x=new Date(t), p=function(n){return (n<10?"0":"")+n;};
    return {s:x.getFullYear()+"-"+p(x.getMonth()+1)+"-"+p(x.getDate()), wd:x.getDay()};
  }
  function candidates(){
    var out=[];
    for(var i=0;i<60&&out.length<30;i++){
      var d=jstYmd(i);
      if(d.wd===0||d.wd===6) continue;
      out.push(d.s);
    }
    return out;
  }
  function loadBrief(){
    if(LOADED) return;
    LOADED=true;
    var list=candidates();
    Promise.all(list.map(function(d){
      return fetch("briefs/"+d+".json",{method:"HEAD",cache:"no-cache"})
        .then(function(r){return r.ok?d:null;})["catch"](function(){return null;});
    })).then(function(res){
      DATES=res.filter(Boolean).sort().reverse();
      if(!DATES.length){
        LOADED=false;
        empty("まだブリーフが保存されていません",
          "平日朝の定期タスクが <code>docs/briefs/</code> に最初の1本を置くと、ここに表示されます。");
        return;
      }
      show(DATES[0]);
    })["catch"](function(){
      LOADED=false;
      empty("ブリーフを読み込めませんでした","時間をおいて開き直してください。");
    });
  }
})();
"""


def render_page(passed, allrows, cfg, meta, out_path):
    LINK_TMPL[0] = str(cfg["display"].get("link_url") or "")
    rows = passed.head(int(cfg["display"]["max_rows"]))
    smax = int(passed["score_max"].max()) if len(passed) else 0

    heads = ['<th class="name" scope="col">銘柄</th>',
             '<th scope="col">コード</th>', '<th scope="col">業種</th>',
             '<th class="s on" data-key="score" scope="col">合計<br>/' + str(smax) + '</th>']
    for key, _col, label, _u, _d, _h in METRIC_INFO:
        heads.append(f'<th class="s" data-key="{key}" scope="col">{label}</th>')
    heads += ['<th class="s up" data-key="chg1" data-mode="asc" scope="col">前日比</th>',
              '<th class="s up" data-key="chg3" data-mode="asc" scope="col">3営業日</th>',
              '<th scope="col">株価</th>', '<th scope="col">業績</th>',
              '<th scope="col">3か月</th>']

    body = "\n".join(_row(r, i + 1, smax) for i, (_, r) in enumerate(rows.iterrows()))
    if not len(rows):
        table = ('<div class="empty">今日は必須条件をすべて満たす銘柄がありませんでした。'
                 '<br>条件をゆるめたい場合は config.yml の数字を調整してください。</div>')
    else:
        table = ('<div class="scroll main"><table id="main"><thead><tr>' + "".join(heads)
                 + '</tr></thead><tbody>' + body + '</tbody></table></div>')

    alert_block = _alert_block(passed, smax)

    req = cfg["required"]
    tech = cfg.get("technical", {})
    cond_rows = "".join([
        f"<tr><td>業績</td><td>直近{req['consecutive_growth_years']}期連続で増収かつ増益</td>"
        f"<td>{int(allrows['ok_growth'].sum()):,}</td></tr>",
        f"<tr><td>PER</td><td>{req['per_max']}倍以下（会社予想の1株利益）</td>"
        f"<td>{int(allrows['ok_per'].sum()):,}</td></tr>",
        f"<tr><td>流動性</td><td>平均売買代金 {int(req['min_avg_turnover_yen']):,}円以上</td>"
        f"<td>{int(allrows['ok_liquidity'].sum()):,}</td></tr>",
        f"<tr><td>配当</td><td>直近{req.get('dividend_years', 5)}期で減配なし（無配continuedも可）</td>"
        f"<td>{int(allrows['ok_dividend'].sum()):,}</td></tr>",
    ]).replace("無配continued", "ずっと無配")

    sc_rows = ""
    for key, _col, label, _u, _d, _h in METRIC_INFO:
        tiers = " ／ ".join(
            f"{html.escape(str(t.get('label', '')))} <b>+{int(t['points'])}</b>"
            for t in cfg["scoring"].get(key, []))
        sc_rows += f"<tr><td>{label}</td><td>{tiers or '—'}</td></tr>"

    now = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    doc = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>株の自動まとめ</title>
<style>{CSS}{BRIEF_CSS}</style>
</head>
<body>
<div class="wrap">
  <nav class="tabs" role="tablist">
    <button type="button" class="tab" data-tab="brief" role="tab">朝のブリーフ</button>
    <button type="button" class="tab" data-tab="screen" role="tab">スクリーニング</button>
  </nav>

  <section class="panel" id="panel-brief" role="tabpanel" hidden>
    <header class="bhead">
      <div class="brow"><h1>朝のマーケット・ブリーフ</h1><span class="stamp" id="b-stamp">—</span></div>
      <div class="bmeta" id="b-meta"></div>
    </header>
    <div class="bbody" id="b-main">
      <div class="state"><strong>読み込み中</strong>保存済みのブリーフを探しています。</div>
    </div>
  </section>

  <section class="panel" id="panel-screen" role="tabpanel">
  <h1>買い時スクリーニング</h1>
  <p class="sub">最終更新 {now}（日本時間）・株価基準 {html.escape(str(meta.get('price_date','—')))}{html.escape(meta.get('price_note',''))}</p>
  <div class="stats">
    <div class="stat"><span>調べた銘柄</span><b>{meta.get('universe',0):,}</b></div>
    <div class="stat"><span>条件通過</span><b>{len(passed):,}</b></div>
    <div class="stat"><span>表示</span><b>{len(rows):,}</b></div>
    <div class="stat"><span>最高点</span><b>{int(passed['score'].max()) if len(passed) else 0}</b></div>
  </div>
{alert_block}
  <p class="hint">項目名をタップすると並べ替わります。⌄の付いた項目は点数の高い順（同点なら中身の良いほうが上）、⌃の付いた「前日比」「3営業日」は下げの大きい順です。横にスクロールできます。</p>

{table}

  <details class="about">
    <summary>条件と配点について</summary>
    <table>
      <tr><th>必須条件</th><th>内容</th><th>単独で満たした数</th></tr>
      {cond_rows}
    </table>
    <table>
      <tr><th>加点項目（合計{smax}点満点）</th><th>段階</th></tr>
      {sc_rows}
    </table>
  </details>

  <footer>
    このページは自動生成です。数値はJPX公式のJ-Quants APIをもとに機械的に計算しています。<br>
    株式分割があった銘柄は、1株あたり配当が見かけ上減って「減配」と判定されることがあります。<br>
    決算の実数値・会社予想は必ずご自身で確認してください。投資判断の責任は利用者にあります。
  </footer>
  </section>
</div>
<script>{JS}</script>
<script>{BRIEF_JS}</script>
</body>
</html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


def dump_json(passed, meta, path):
    recs = []
    for code, r in passed.iterrows():
        det = r.get("score_detail") or {}
        recs.append({
            "code": str(code),
            "name": r.get("name"),
            "score": int(r.get("score") or 0),
            "price": None if r.get("price") is None or np.isnan(r.get("price")) else float(r["price"]),
            "per": None if np.isnan(r.get("per", np.nan)) else float(r["per"]),
            "div_yield": None if np.isnan(r.get("div_yield", np.nan)) else float(r["div_yield"]),
            "points": {k: int(v.get("points") or 0) for k, v in det.items()},
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


_covered = []


def covered_from(api):
    """契約で遡れる最も古い日付（1度だけ調べて使い回す）。"""
    if not _covered:
        _covered.append(api.covered_from())
    return _covered[0]


def business_days(api, start, end):
    """営業日の一覧を新しい順で返す。契約範囲の外は自動で切り詰める。"""
    lim = covered_from(api)
    if lim and start < lim:
        start = lim
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
        time.sleep(0.2)

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
        time.sleep(10)   # 直前の連続アクセスから少し間を空ける
    elif need:
        _log(f"決算を取りに行きます: {len(need)}日分")

    frames = [old] if old is not None else []
    done, fails = [], 0

    def flush():
        """途中経過を保存しておく（次回は続きから）。"""
        if not done:
            return
        parts = list(frames)
        parts.append(pd.DataFrame({"DiscDate": done, "Code": [None] * len(done)}))
        df = pd.concat(parts, ignore_index=True)
        df = df.drop_duplicates(
            subset=[c for c in ["Code", "DiscDate", "CurPerType", "DocType"]
                    if c in df.columns], keep="last")
        _save(df, SUMMARY)

    for i, d in enumerate(need, 1):
        try:
            rows = api.summary_by_date(d)
            fails = 0
        except Exception as e:
            msg = str(e)
            if "400" in msg:          # 契約範囲外などは取れないものとして記録
                done.append(d)
                continue
            fails += 1
            _log(f"  {d} の取得に失敗（{fails}回連続）: {msg[:160]}")
            if fails >= 10:
                flush()
                raise RuntimeError(
                    f"決算データの取得が {fails} 回連続で失敗したので中断しました。"
                    f"ここまでの分は保存済みなので、もう一度実行すると続きから再開します。"
                    f"最後のエラー: {msg[:200]}"
                )
            time.sleep(20)
            continue

        done.append(d)
        if rows:
            frames.append(_keep(pd.DataFrame(rows), SUM_COLS))
        if i % 100 == 0:
            _log(f"  {i}/{len(need)}日")
            flush()
        time.sleep(0.2)

    if need:
        frames.append(pd.DataFrame({"DiscDate": done, "Code": [None] * len(done)}))

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

    fund = build_fundamentals(
        summary, val, int(cfg["required"].get("dividend_years", 5)))
    tech = build_technicals(prices, cfg["technical"])
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
            & (pre["avg_turnover"] >= req["min_avg_turnover_yen"])
            & (~pre["div_cut"].fillna(False))
        ].sort_values("avg_turnover", ascending=False).head(600)
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

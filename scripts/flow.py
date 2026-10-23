"""「資金の流れ」タブ用の数値を集めて data/flow.json に保存する。

  python scripts/flow.py

取得の順番はプロジェクトのルールどおり「まず J-Quants、無ければ他から」。
1つの取得先が失敗しても他は続け、結果は status に「ok / 失敗理由 / プラン制限」で残す。

出力
  data/flow.json       … 最新のまとめ（定期タスクとページが読む）
  data/flow_hist.json  … 日付ごとの記録（JPX は最新の数日分しか置かないので自前でためる。答え合わせにも使う）
"""

import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
import traceback

import requests

JST = dt.timezone(dt.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flow.json")
HIST = os.path.join(ROOT, "data", "flow_hist.json")
JQ = "https://api.jquants.com/v2"
DAYS = 26            # 業種の計算に使う営業日数（20日平均＋5日）
HIST_KEEP = 400      # 記録を残す日数

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
})
STATUS = {}
SEC17 = {}          # 4桁コード → 17業種コード（J-Quants の上場銘柄一覧から）
SEC17_NAMES = {}


# ====================================================================
# 共通
# ====================================================================
def http(url, **kw):
    last = None
    for i in range(4):
        try:
            r = S.get(url, timeout=40, **kw)
            if r.status_code == 200 and r.content:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url[:90]} … {last}")


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace(",", "").replace("▲", "-").replace("△", "-").replace("%", "")
    if s in ("", "-", "*", "－"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def rnd(x, n=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, n)


def step(name):
    """取得処理を1つずつ包み、失敗しても全体を止めない。"""
    def deco(fn):
        def run(*a, **kw):
            t = time.time()
            try:
                res = fn(*a, **kw)
                if name not in STATUS:
                    STATUS[name] = "ok"
                print(f"[ok] {name}（{time.time() - t:.0f}秒）", flush=True)
                return res
            except PlanLimit as e:
                STATUS[name] = f"プラン制限: {e}"
            except Exception as e:
                STATUS[name] = f"失敗: {e}"[:300]
                traceback.print_exc()
            print(f"[--] {name}: {STATUS[name]}", flush=True)
            return None
        return run
    return deco


class PlanLimit(Exception):
    pass


# ====================================================================
# J-Quants
# ====================================================================
class JQuants:
    def __init__(self):
        k = (os.environ.get("JQ_API_KEY") or os.environ.get("JQ_PASSWORD") or "").strip()
        if not k:
            raise RuntimeError("JQ_API_KEY が設定されていません")
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": k})

    def get(self, path, params=None, retries=6):
        params = dict(params or {})
        out = []
        while True:
            last = ""
            for i in range(retries):
                try:
                    r = self.s.get(f"{JQ}{path}", params=params, timeout=90)
                except requests.RequestException as e:
                    last = str(e)
                    time.sleep(2 ** i)
                    continue
                if r.status_code == 200:
                    break
                last = f"{r.status_code} {r.text[:160]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** i * 2, 60))
                    continue
                if r.status_code in (401, 403):
                    raise PlanLimit(f"{path} は今の契約で使えません（{r.status_code}）")
                raise RuntimeError(f"{path}: {last}")
            else:
                raise RuntimeError(f"{path}: {retries}回失敗（{last}）")
            body = r.json()
            key = "data" if "data" in body else next((k for k in body if k != "pagination_key"), None)
            if key:
                out.extend(body.get(key) or [])
            pk = body.get("pagination_key")
            if not pk:
                return out
            params["pagination_key"] = pk


def trading_days(jq, n):
    today = dt.datetime.now(JST).date()
    cal = jq.get("/markets/calendar", {"from": (today - dt.timedelta(days=n * 2 + 20)).isoformat(),
                                       "to": today.isoformat()})
    days = sorted(c["Date"] for c in cal if str(c.get("HolDiv")) in ("1", "2"))
    # 当日の四本値は夕方に出るので、まだ無ければ前日までを使う（呼び出し側で空なら捨てる）
    return days[-n:]


ETF17 = {  # NEXT FUNDS TOPIX-17業種ETF（17業種コード順）
    "1617": "1", "1618": "2", "1619": "3", "1620": "4", "1621": "5", "1622": "6",
    "1623": "7", "1624": "8", "1625": "9", "1626": "10", "1627": "11", "1628": "12",
    "1629": "13", "1630": "14", "1631": "15", "1632": "16", "1633": "17",
}


@step("J-Quants：業種別の売買代金・騰落（全銘柄の日足）")
def jq_sectors(jq, hist):
    import pandas as pd

    master = pd.DataFrame(jq.get("/equities/master"))
    master["c4"] = master["Code"].astype(str).str[:4]
    stocks = master[master["S17"].astype(str).isin([str(i) for i in range(1, 18)])]
    s17 = dict(zip(stocks["c4"], stocks["S17"].astype(str)))
    s33 = dict(zip(stocks["c4"], stocks["S33"].astype(str)))
    n17 = dict(zip(stocks["S17"].astype(str), stocks["S17Nm"]))
    n33 = dict(zip(stocks["S33"].astype(str), stocks["S33Nm"]))
    m33to17 = dict(zip(stocks["S33"].astype(str), stocks["S17"].astype(str)))   # 33業種→17業種の対応（空売り比率の集計に使う）

    frames = []
    for d in trading_days(jq, DAYS):
        rows = jq.get("/equities/bars/daily", {"date": d})
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(0.2)
    if not frames:
        raise RuntimeError("日足が0件")
    bars = pd.concat(frames, ignore_index=True)
    bars["c4"] = bars["Code"].astype(str).str[:4]
    for c in ("AdjC", "C", "Va", "MktCap"):
        if c in bars:
            bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars["px"] = bars["AdjC"] if "AdjC" in bars else bars["C"]
    bars = bars.sort_values(["c4", "Date"])
    bars["ret"] = bars.groupby("c4")["px"].pct_change()
    dates = sorted(bars["Date"].unique())
    last = dates[-1]

    SEC17.update(s17)
    SEC17_NAMES.update(n17)

    # ---- 規模の重み：最新日のバリュエーションの時価総額（無ければ20日平均売買代金）----
    w = None
    try:
        val = pd.DataFrame(jq.get("/equities/valuation", {"date": last}))
        val["c4"] = val["Code"].astype(str).str[:4]
        w = dict(zip(val["c4"], pd.to_numeric(val["MktCap"], errors="coerce")))
    except Exception as ex:
        STATUS["J-Quants：時価総額（重み）"] = f"失敗のため売買代金で代用: {ex}"[:200]
    if not w:
        w = bars.groupby("c4")["Va"].mean().to_dict()

    # ---- 17業種ETFの資金の出入り：時価総額÷終値＝口数。口数の増減×終値＝資金の出入り ----
    etf = None
    try:
        close = {(r.c4, r.Date): r.C for r in bars[bars["c4"].isin(ETF17)][["c4", "Date", "C"]].itertuples()}
        pts = {}
        for back in (0, 5, 20):
            if len(dates) <= back:
                continue
            d = dates[-1 - back]
            v = val if back == 0 and "val" in dir() else pd.DataFrame(jq.get("/equities/valuation", {"date": d}))
            v = v.copy()
            v["c4"] = v["Code"].astype(str).str[:4]
            v = v[v["c4"].isin(ETF17)]
            for r in v.itertuples():
                mc, c = num(getattr(r, "MktCap", None)), close.get((r.c4, d))
                if mc and c:
                    pts.setdefault(r.c4, {})[back] = (mc / c, c, mc)
        if pts:
            etf = []
            for code, p in pts.items():
                if 0 not in p:
                    continue
                u0, c0, mc0 = p[0]
                o = {"code": code, "s17": ETF17[code], "sector": n17.get(ETF17[code]), "aum": rnd(mc0 / 1e8, 0)}
                for back, key in ((5, "flow5"), (20, "flow20")):
                    if back in p:
                        o[key] = rnd((u0 - p[back][0]) * c0 / 1e8, 1)    # 億円
                etf.append(o)
        else:
            STATUS["J-Quants：17業種ETFの口数"] = "バリュエーション指標に17業種ETFの時価総額が無いため計算できず"
    except Exception as ex:
        STATUS["J-Quants：17業種ETFの口数"] = f"失敗: {ex}"[:200]

    b = bars[bars["c4"].isin(s17)].copy()
    b["w"] = b["c4"].map(w).fillna(0)
    b["up"] = (b["ret"] > 0).astype(float)

    def per_sector(key, names):
        b["sec"] = b["c4"].map(key)
        daily = []
        for d, g in b.groupby("Date"):
            tot = g["Va"].sum()
            wsum = g["w"][g["ret"].notna()].sum()
            if not wsum:
                continue        # 1日目（前日比が出ない日）は飛ばす
            mkt = (g["ret"] * g["w"]).sum() / wsum
            for sec, h in g.groupby("sec"):
                ww = h["w"][h["ret"].notna()].sum()
                daily.append({"date": d, "sec": sec,
                              "ret": (h["ret"] * h["w"]).sum() / ww if ww else None,
                              "mkt": mkt,
                              "va": h["Va"].sum(), "share": h["Va"].sum() / tot * 100 if tot else None,
                              "upva": (h["Va"] * h["up"]).sum(),
                              "sflow": (h["Va"] * h["ret"].apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0)).sum()})
        dd = pd.DataFrame(daily).sort_values("date")
        out = []
        for sec, g in dd.groupby("sec"):
            g = g.dropna(subset=["ret"])
            if len(g) < 6:
                continue
            r = g["ret"].fillna(0)
            m = g["mkt"].fillna(0)
            cum = lambda s, n: (1 + s.tail(n)).prod() - 1
            out.append({
                "code": sec, "name": names.get(sec, sec), "s17": m33to17.get(sec) if key is s33 else sec,
                "ret1": rnd(r.iloc[-1] * 100), "ret5": rnd(cum(r, 5) * 100), "ret20": rnd(cum(r, 20) * 100),
                "rel5": rnd((cum(r, 5) - cum(m, 5)) * 100), "rel20": rnd((cum(r, 20) - cum(m, 20)) * 100),
                "share": rnd(g["share"].iloc[-1]),
                "share_chg": rnd(g["share"].tail(5).mean() - g["share"].tail(20).mean()),  # 5日平均−20日平均（ポイント）
                "up_ratio5": rnd(g["upva"].tail(5).sum() / g["va"].tail(5).sum() * 100, 1),  # 上昇銘柄の売買代金の割合
                "sflow5": rnd(g["sflow"].tail(5).sum() / 1e8, 0),   # 上昇日＋・下落日−の売買代金（億円）
                "va1": rnd(g["va"].iloc[-1] / 1e8, 0),
            })
        return out

    res = {"date": last, "days": len(dates), "s17": per_sector(s17, n17), "s33": per_sector(s33, n33), "etf17": etf}
    h = hist.setdefault(last, {})
    h["jp_s17"] = {x["code"]: [x["ret1"], x["share"], x["rel5"]] for x in res["s17"]}
    h["jp_s33"] = {x["code"]: [x["ret1"], x["share"], x["rel5"]] for x in res["s33"]}
    return res


@step("J-Quants：投資部門別（週次）")
def jq_investor_types(jq):
    today = dt.datetime.now(JST).date()
    rows = jq.get("/equities/investor-types", {"from": (today - dt.timedelta(days=120)).isoformat(),
                                               "to": today.isoformat()})
    keys = {"Frgn": "海外投資家", "Ind": "個人", "TrstBnk": "信託銀行", "InvTr": "投資信託",
            "BusCo": "事業法人", "InsCo": "生保・損保", "Bank": "都銀・地銀等", "OthFin": "その他金融機関",
            "OthCo": "その他法人", "SecCo": "証券会社", "Prop": "自己（証券会社の自己売買）"}
    out = {}
    for r in rows:
        sec = r.get("Section")
        if sec not in ("TSEPrime", "TSEStandard", "TSEGrowth", "TokyoNagoya"):
            continue
        out.setdefault(sec, []).append({
            "pub": r.get("PubDate"), "from": r.get("StDate"), "to": r.get("EnDate"),
            # 差引（買−売）を億円に（元は千円）
            **{k: rnd(num(r.get(f"{k}Bal")) / 1e5, 0) if num(r.get(f"{k}Bal")) is not None else None for k in keys},
        })
    for v in out.values():
        v.sort(key=lambda x: x["to"] or "")
        del v[:-12]
    if not out:
        raise RuntimeError("データ0件")
    return {"labels": keys, "unit": "億円（買い−売り）", "sections": out}


@step("J-Quants：スタンダード以上のデータの確認")
def jq_plan_check(jq):
    """信用残・空売りが今の契約で取れるかを毎回確かめる（取れるようになったら J-Quants を優先して使う）。"""
    res = {}
    today = dt.datetime.now(JST).date().isoformat()
    for path, label in (("/markets/short-ratio", "業種別空売り比率"), ("/markets/margin-interest", "信用取引週末残高"),
                        ("/markets/margin-alert", "日々公表信用取引残高")):
        try:
            jq.get(path, {"date": today}, retries=2)
            res[label] = "取得可"
        except PlanLimit:
            res[label] = "プラン制限（ライトでは不可）"
        except Exception as e:
            res[label] = f"確認失敗: {e}"[:120]
    return res


# ====================================================================
# JPX のサイト（J-Quants ではプラン制限のもの）
# ====================================================================
JPX = "https://www.jpx.co.jp"


def jpx_links(page, pattern):
    html = http(JPX + page).text
    out = []
    for href in re.findall(r'href="([^"]+)"', html):
        if re.search(pattern, href):
            out.append(href if href.startswith("http") else JPX + href)
    return list(dict.fromkeys(out))


def pdf_text(content):
    import pypdf
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(content)).pages)


S33_NAMES = ["水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙", "化学", "医薬品",
             "石油・石炭製品", "ゴム製品", "ガラス・土石製品", "鉄鋼", "非鉄金属", "金属製品", "機械",
             "電気機器", "輸送用機器", "精密機器", "その他製品", "電気・ガス業", "陸運業", "海運業", "空運業",
             "倉庫・運輸関連業", "情報・通信業", "卸売業", "小売業", "銀行業", "証券、商品先物取引業",
             "証券・商品先物取引業", "保険業", "その他金融業", "不動産業", "サービス業"]


@step("JPX：業種別空売り比率（毎日）")
def jpx_short_ratio(hist):
    links = jpx_links("/markets/statistics-equities/short-selling/index.html", r"\d{6}-g\.pdf$")
    if not links:
        raise RuntimeError("業種別のPDFのリンクが見つからない")
    got = []
    for url in links[:5]:
        ymd = re.search(r"(\d{6})-g\.pdf", url).group(1)
        day = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}"
        if "short" in hist.get(day, {}):
            got.append(day)
            continue
        text = pdf_text(http(url).content)
        secs = {}
        for line in text.splitlines():
            line = line.replace("　", " ").strip()
            m = re.match(r"^(\S+?)\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)$", line)
            if not m:
                continue
            name = m.group(1)
            a, b, c, d = (num(m.group(i)) for i in (2, 4, 6, 8))
            secs[name] = {"short": rnd((b + c) / d * 100, 1) if d else None, "va": d}   # 空売り比率％、売買代金（百万円）
        if len(secs) < 20:
            raise RuntimeError(f"{day}: 業種の行が{len(secs)}行しか読めない（PDFの形が変わった可能性）")
        tot_d = sum(v["va"] for v in secs.values())
        tot_s = sum(v["va"] * v["short"] / 100 for v in secs.values() if v["short"] is not None)
        hist.setdefault(day, {})["short"] = {"total": rnd(tot_s / tot_d * 100, 1), "sec": secs}
        got.append(day)
    days = sorted(d for d in hist if "short" in hist[d])
    last = days[-1]
    cur = hist[last]["short"]
    prev = [hist[d]["short"] for d in days[-21:-1]]
    rows = []
    for name, v in cur["sec"].items():
        p = [x["sec"][name]["short"] for x in prev if name in x["sec"] and x["sec"][name]["short"] is not None]
        rows.append({"name": name, "short": v["short"],
                     "avg20": rnd(sum(p) / len(p), 1) if p else None,
                     "chg": rnd(v["short"] - sum(p) / len(p), 1) if p and v["short"] is not None else None})
    return {"date": last, "total": cur["total"], "history_days": len(days), "sectors": rows,
            "note": "空売り比率＝空売り（価格規制あり＋なし）÷売買代金。信用の新規売りを含む"}


def read_xlsx_rows(content, limit=80):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    for r in ws.iter_rows(values_only=True):
        cells = [("" if c is None else str(c).strip()) for c in r]
        if any(cells):
            rows.append([c for c in cells])
        if len(rows) >= limit:
            break
    return rows


@step("JPX：信用取引現在高（週次・市場全体）")
def jpx_margin_weekly(hist):
    links = jpx_links("/markets/statistics-equities/margin/04.html", r"\d{8}_mtcurrent\.xlsx$")
    if not links:
        raise RuntimeError("信用取引現在高のExcelが見つからない")
    url = sorted(links)[-1]
    ymd = re.search(r"(\d{8})_mtcurrent", url).group(1)
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
    rows = read_xlsx_rows(http(url).content, 40)
    # 表の形（2026年9月からの新形式）：ラベル列のあとに「株数／金額」、続いて二市場の 売残・前週比・買残・前週比
    want = {"制度信用取引残高": "seido", "一般信用取引残高": "ippan", "信用取引残高合計": "total", "貸借取引残高": "jsf"}
    out, cur, acct = {}, None, ""
    for r in rows:
        lab = " ".join(r[1:3])
        if r[1].strip():        # 左端の見出しが変わったら、対象の区分も切り替える（社内対当・自己貸株などは読まない）
            cur = next((v for k, v in want.items() if k in r[1]), None)
            acct = ""
        if "委託" in lab:
            acct = "cust"
        elif "自己" in lab and "自己貸株" not in lab:
            acct = "prop"
        unit = r[3] if len(r) > 3 else ""
        if cur and unit.startswith(("株数", "金額")) and len(r) >= 8:
            key = cur if cur in ("total", "jsf") else f"{cur}_{acct}"
            u = "shs" if unit.startswith("株数") else "val"
            out.setdefault(key, {})[u] = {"sell": num(r[4]), "sell_chg": num(r[5]), "buy": num(r[6]), "buy_chg": num(r[7])}
    t = out.get("total")
    if not t or "shs" not in t:
        raise RuntimeError("信用取引残高合計の行が読めない（表の形が変わった可能性）")
    res = {"date": day, "url": url, "unit": "株数は千株、金額は百万円（二市場）", "table": out,
           "ratio": rnd(t["shs"]["buy"] / t["shs"]["sell"], 2) if t["shs"]["sell"] else None}
    hist.setdefault(day, {})["margin_weekly"] = {"buy_val": t.get("val", {}).get("buy"), "sell_val": t.get("val", {}).get("sell"),
                                                 "ratio": res["ratio"]}
    hist[day].pop("margin_weekly_rows", None)
    return res


@step("JPX：日々公表銘柄の信用残（毎日）")
def jpx_margin_daily():
    links = jpx_links("/markets/statistics-equities/margin/index.html", r"\d{8}_mtdaily(_\d+)?\.xlsx$")
    if not links:
        raise RuntimeError("日々公表のExcelが見つからない")
    # 同じ日に修正版（_01）があればそちらを使う
    best = {}
    for u in links:
        ymd = re.search(r"(\d{8})_mtdaily", u).group(1)
        if ymd not in best or "_mtdaily_" in u:
            best[ymd] = u
    ymd = sorted(best)[-1]
    rows = read_xlsx_rows(http(best[ymd]).content, 400)
    return {"date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "rows": rows[:250],
            "note": "日々公表銘柄（信用取引が過熱気味として取引所が毎日残高を出している銘柄）の一覧"}


@step("日証金：貸借取引の新規・返済・残高（毎日・業種別）")
def jsf_balance(hist):
    """日証金の銘柄別残高一覧（前営業日の申込分）。証券会社が日証金から借りた分なので、制度信用の動きの大部分を表す。
    融資＝信用の買い（個人の買いが中心）、貸株＝信用の売り。"""
    import pandas as pd
    r = http("https://www.taisyaku.jp/data/zandaka.csv")
    text = None
    for enc in ("cp932", "utf-8-sig"):
        try:
            text = r.content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    hi = next((i for i, l in enumerate(lines[:15]) if "銘柄コード" in l), 0)
    df = pd.read_csv(io.StringIO("\n".join(lines[hi:])), dtype=str)
    cols = ["融資新規金額", "融資返済金額", "融資残高金額", "貸株新規金額", "貸株返済金額", "貸株残高金額",
            "融資新規株数", "融資残高株数", "貸株新規株数", "貸株残高株数"]
    for c in cols:
        df[c] = pd.to_numeric(df[c].str.replace(",", ""), errors="coerce").fillna(0)
    day = str(df["申込日"].dropna().iloc[0]).replace("/", "-")
    df["c4"] = df["銘柄コード"].astype(str).str[:4]
    df["s17"] = df["c4"].map(SEC17)
    oku = lambda x: rnd(x / 1e8, 1)
    def summ(g):
        return {"buy_new": oku(g["融資新規金額"].sum()), "buy_repay": oku(g["融資返済金額"].sum()), "buy_bal": oku(g["融資残高金額"].sum()),
                "sell_new": oku(g["貸株新規金額"].sum()), "sell_repay": oku(g["貸株返済金額"].sum()), "sell_bal": oku(g["貸株残高金額"].sum())}
    total = summ(df[df["s17"].notna()]) if SEC17 else summ(df)
    sec = {k: {"name": SEC17_NAMES.get(k, k), **summ(g)} for k, g in df[df["s17"].notna()].groupby("s17")} if SEC17 else {}
    hist.setdefault(day, {})["jsf"] = {"total": total, "sec": {k: [v["buy_new"], v["buy_bal"], v["sell_new"], v["sell_bal"]] for k, v in sec.items()}}
    # 直近5日・20日の平均と比べる（記録がたまるまでは出ない）
    days = sorted(d for d in hist if "jsf" in hist[d] and d <= day)
    prev = [hist[d]["jsf"]["total"] for d in days[-21:-1]]
    if prev:
        total["buy_new_avg"] = rnd(sum(p["buy_new"] or 0 for p in prev) / len(prev), 1)
        total["sell_new_avg"] = rnd(sum(p["sell_new"] or 0 for p in prev) / len(prev), 1)
        total["buy_bal_chg"] = rnd(total["buy_bal"] - prev[-1]["buy_bal"], 1) if prev[-1].get("buy_bal") is not None else None
        total["sell_bal_chg"] = rnd(total["sell_bal"] - prev[-1]["sell_bal"], 1) if prev[-1].get("sell_bal") is not None else None
    if len(days) >= 6:
        for k, v in sec.items():
            p5 = [hist[d]["jsf"]["sec"].get(k) for d in days[-6:-1] if k in hist[d]["jsf"].get("sec", {})]
            if p5:
                v["buy_bal_chg5"] = rnd(v["buy_bal"] - p5[0][1], 1)
                v["sell_bal_chg5"] = rnd(v["sell_bal"] - p5[0][3], 1)
    return {"date": day, "unit": "億円（日証金の貸借取引。融資＝信用の買い、貸株＝信用の売り）", "total": total,
            "sectors": sec, "history_days": len(days), "issues": len(df)}


# ====================================================================
# 米国
# ====================================================================
US_SECTORS = {"XLK": "情報技術", "XLF": "金融", "XLV": "ヘルスケア", "XLY": "一般消費財", "XLP": "生活必需品",
              "XLE": "エネルギー", "XLI": "資本財", "XLB": "素材", "XLU": "公益", "XLRE": "不動産",
              "XLC": "コミュニケーション"}
SEMIS = {"ASML": "ASML（米国上場）", "ASML.AS": "ASML（アムステルダム）", "^SOX": "フィラデルフィア半導体指数",
         "SMH": "半導体ETF（SMH）", "2330.TW": "TSMC", "005930.KS": "サムスン電子", "000660.KS": "SKハイニックス",
         "8035.T": "東京エレクトロン", "6857.T": "アドバンテスト", "6920.T": "レーザーテック",
         "^KS11": "KOSPI", "^TWII": "台湾加権指数", "000300.SS": "CSI300（中国本土）", "^HSI": "ハンセン指数"}


def yf_hist(tickers, period="3mo"):
    import yfinance as yf
    df = yf.download(list(tickers), period=period, interval="1d", progress=False,
                     auto_adjust=True, group_by="ticker", threads=True)
    out = {}
    for t in tickers:
        try:
            g = df[t].dropna(subset=["Close"])
            if len(g) >= 6:
                out[t] = g
        except Exception:
            pass
    return out


def perf(g, base=None):
    c = g["Close"]
    r = lambda n: (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100 if len(c) > n else None
    dv = (g["Close"] * g["Volume"])
    o = {"date": str(g.index[-1].date()), "close": rnd(float(c.iloc[-1])),
         "ret1": rnd(r(1)), "ret5": rnd(r(5)), "ret20": rnd(r(20)),
         "vol_ratio": rnd(float(dv.tail(5).mean() / dv.tail(20).mean()), 2) if dv.tail(20).mean() else None}
    if base is not None:
        b = base["Close"]
        for n in (5, 20):
            if len(b) > n and o[f"ret{n}"] is not None:
                o[f"rel{n}"] = rnd(o[f"ret{n}"] - (b.iloc[-1] / b.iloc[-1 - n] - 1) * 100)
    return o


def ssga_units(tic):
    """SPDR の運用会社が出している日々の基準価額・口数の履歴（Excel）から口数を読む。"""
    import pandas as pd
    url = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/"
           f"navhist-us-en-{tic.lower()}.xlsx")
    raw = pd.read_excel(io.BytesIO(http(url).content), header=None)
    hi = next(i for i in range(min(15, len(raw)))
              if any("Shares Outstanding" in str(x) for x in raw.iloc[i].values))
    df = raw.iloc[hi + 1:].copy()
    df.columns = [str(x).strip() for x in raw.iloc[hi].values]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date")
    df["units"] = pd.to_numeric(df["Shares Outstanding"], errors="coerce")
    df["nav"] = pd.to_numeric(df["NAV"], errors="coerce")
    df = df.dropna(subset=["units", "nav"]).tail(30)
    df["flow"] = df["units"].diff() * df["nav"] / 1e6     # 百万ドル
    return {"date": str(df["Date"].iloc[-1].date()), "flow1": rnd(df["flow"].iloc[-1], 0),
            "flow5": rnd(df["flow"].tail(5).sum(), 0), "flow20": rnd(df["flow"].tail(20).sum(), 0),
            "aum": rnd(float(df["units"].iloc[-1] * df["nav"].iloc[-1] / 1e6), 0)}


@step("米国：11業種ETF（騰落・売買代金・資金の出入り）")
def us_sectors(hist):
    h = yf_hist(list(US_SECTORS) + ["SPY", "RSP"])
    base = h.get("SPY")
    rows, flow_err = [], []
    for t, name in US_SECTORS.items():
        if t not in h:
            continue
        o = {"tic": t, "name": name, **perf(h[t], base)}
        try:
            o.update(ssga_units(t))
        except Exception as e:
            flow_err.append(f"{t}: {e}"[:80])
        rows.append(o)
        time.sleep(0.5)
    if flow_err:
        STATUS["米国：ETFの口数（資金の出入り）"] = "一部失敗: " + " / ".join(flow_err[:4])
    if not rows:
        raise RuntimeError("1業種も取れない")
    res = {"date": rows[0]["date"], "sectors": rows, "spy": perf(base) if base is not None else None}
    if "RSP" in h and base is not None:
        res["rsp_vs_spy"] = {"rel5": perf(h["RSP"], base).get("rel5"), "rel20": perf(h["RSP"], base).get("rel20"),
                             "note": "均等加重−時価総額加重。プラスなら大型株以外にお金が広がっている"}
    hist.setdefault(res["date"], {})["us"] = {r["tic"]: [r["ret1"], r.get("flow1")] for r in rows}
    return res


@step("CFTC：建玉明細（週次・S&P500と日経平均の先物）")
def cftc():
    text = http("https://www.cftc.gov/dea/newcot/FinFutWk.txt").text
    import csv
    want = {"E-MINI S&P 500 -": "S&P500先物（E-mini）", "NIKKEI STOCK AVERAGE YEN DENOM": "日経平均先物（円建て・CME）",
            "NASDAQ MINI": "ナスダック100先物（E-mini）"}
    out = {}
    for f in csv.reader(io.StringIO(text)):
        name = f[0].strip()
        for k, label in want.items():
            if name.startswith(k) and label not in out:
                v = [num(x) for x in f]
                out[label] = {"date": f[2].strip(), "oi": v[7],
                              "asset_mgr_net": v[11] - v[12], "lev_money_net": v[14] - v[15],
                              "dealer_net": v[8] - v[9]}
    if not out:
        raise RuntimeError("対象の銘柄の行が見つからない")
    return {"markets": out, "note": "資産運用会社（年金・投信など）とレバレッジ勢（ヘッジファンドなど）の買い持ち−売り持ち（枚）"}


# ====================================================================
# 韓国・台湾・半導体
# ====================================================================
@step("韓国：KOSPIの投資主体別売買（NAVER）")
def korea():
    """NAVER のスマホ向けページが使うデータ（指数の総合情報に、投資主体別の売買が入っている）。"""
    errs = []
    for url in ("https://m.stock.naver.com/api/index/KOSPI/integration",
                "https://m.stock.naver.com/api/index/KOSPI/trend"):
        try:
            j = http(url, headers={"Referer": "https://m.stock.naver.com/"}).json()
        except Exception as e:
            errs.append(str(e)[:80])
            continue
        lst = None
        def walk(o):
            nonlocal lst
            if lst is not None:
                return
            if isinstance(o, list) and o and isinstance(o[0], dict) and any("foreign" in k.lower() for k in o[0]):
                lst = o
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(j)
        if not lst:
            errs.append(f"{url.rsplit('/', 1)[-1]}: 投資主体別の欄なし（キー: {list(j)[:8] if isinstance(j, dict) else type(j).__name__}）")
            continue
        def pick(d, *keys):
            for k in d:
                if any(x in k.lower() for x in keys):
                    return num(d[k])
            return None
        rows = []
        for d in lst:
            date = str(d.get("bizdate") or d.get("localTradedAt") or d.get("date") or "")
            date = re.sub(r"^(\d{4})(\d{2})(\d{2})$", r"\1-\2-\3", date[:10])
            rows.append({"date": date, "ind": pick(d, "personal", "individual"), "frgn": pick(d, "foreign"),
                         "inst": pick(d, "institution", "organ")})
        return {"unit": "億ウォン（買い−売り）", "rows": rows[:10], "raw_keys": list(lst[0])[:12]}
    raise RuntimeError(" / ".join(errs))


@step("台湾：三大法人の売買と信用取引（TWSE）")
def taiwan():
    res = {}
    for back in range(0, 6):
        d = dt.datetime.now(JST).date() - dt.timedelta(days=back)
        ymd = d.strftime("%Y%m%d")
        j = http("https://www.twse.com.tw/rwd/zh/fund/BFI82U",
                 params={"type": "day", "dayDate": ymd, "response": "json"}).json()
        if j.get("stat") == "OK" and j.get("data"):
            inst = {}
            for name, buy, sell, net in j["data"]:
                inst[name] = rnd(num(net) / 1e8, 1)     # 億台湾ドル
            res["date"] = d.isoformat()
            res["inst"] = inst
            break
        time.sleep(1)
    if "date" not in res:
        raise RuntimeError("直近6日に三大法人のデータが無い")
    ymd = res["date"].replace("-", "")
    try:
        j = http("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
                 params={"date": ymd, "selectType": "MS", "response": "json"}).json()
        t = next(t for t in j.get("tables", []) if t.get("data"))
        res["margin"] = {row[0]: {"prev": num(row[4]), "today": num(row[5])} for row in t["data"]}
    except Exception as e:
        STATUS["台湾：信用取引"] = f"失敗: {e}"[:150]
    try:
        j = http("https://www.twse.com.tw/rwd/zh/fund/T86",
                 params={"date": ymd, "selectType": "ALLBUT0999", "response": "json"}).json()
        f = j["fields"]
        ix = next(i for i, x in enumerate(f) if "外" in x and "買賣超" in x)
        for row in j["data"]:
            if row[0].strip() == "2330":
                res["tsmc_foreign_shares"] = num(row[ix])   # 株（外資の買い越し株数）
    except Exception as e:
        STATUS["台湾：TSMCへの外資の売買"] = f"失敗: {e}"[:150]
    return res


@step("半導体・アジア株（ASML・TSMC・サムスン・SKハイニックス・日本の製造装置）")
def semis():
    h = yf_hist(list(SEMIS) + ["SPY"])
    base = h.get("SPY")
    us_listed = lambda t: t in ("ASML", "^SOX", "SMH")
    return {t: {"name": n, **perf(h[t], base if us_listed(t) else None)}
            for t, n in SEMIS.items() if t in h}


# ====================================================================
def main():
    hist = {}
    if os.path.exists(HIST):
        try:
            hist = json.load(open(HIST, encoding="utf-8"))
        except Exception:
            hist = {}
    out = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"), "version": 1,
           "jp": {}, "us": None, "cftc": None, "kr": None, "tw": None, "semis": None}

    jq = None
    try:
        jq = JQuants()
    except Exception as e:
        STATUS["J-Quants"] = f"失敗: {e}"
    if jq:
        out["jp"]["sectors"] = jq_sectors(jq, hist)
        out["jp"]["investor_types"] = jq_investor_types(jq)
        out["jp"]["plan_check"] = jq_plan_check(jq)
    out["jp"]["short_ratio"] = jpx_short_ratio(hist)
    out["jp"]["margin_weekly"] = jpx_margin_weekly(hist)
    out["jp"]["margin_daily"] = jpx_margin_daily()
    out["jp"]["jsf"] = jsf_balance(hist)
    out["us"] = us_sectors(hist)
    out["cftc"] = cftc()
    out["kr"] = korea()
    out["tw"] = taiwan()
    out["semis"] = semis()
    out["status"] = STATUS
    out["ok_count"] = sum(1 for v in STATUS.values() if v == "ok")

    for k in sorted(hist)[:-HIST_KEEP]:
        del hist[k]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), default=str)
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(hist.items())), f, ensure_ascii=False, separators=(",", ":"), default=str)
    print("保存:", OUT, json.dumps(STATUS, ensure_ascii=False, indent=1), flush=True)
    if out["ok_count"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

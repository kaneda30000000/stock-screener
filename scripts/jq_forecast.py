"""朝の相場予想に使う、個別銘柄の四本値と出来高を J-Quants から取って保存する。

  python scripts/jq_forecast.py

出力: data/jq_forecast.json
  {"generated_at": "...", "latest_date": "YYYY-MM-DD", "errors": [...],
   "stocks": {"8306": {"name": "...", "rows": [{"date","open","high","low","close","volume","turnover"}, ...]}, ...}}
rows は古い順。株式分割などに備えて調整済みの値（AdjO など）を使う（直近日は調整前と同じ値になる）。
予想ページの計算用スクリプト（fc.py）は、この形をそのまま読む。
"""

import datetime as dt
import json
import os
import sys
import time

import requests

BASE = "https://api.jquants.com/v2"
JST = dt.timezone(dt.timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "jq_forecast.json")

# 予想の対象3銘柄 ＋ 指数の出来高の代わりに使うETF（日経225連動 1321、TOPIX連動 1306）
TARGETS = [
    ("8306", "三菱UFJフィナンシャル・グループ"),
    ("7203", "トヨタ自動車"),
    ("9984", "ソフトバンクグループ"),
    ("1321", "NEXT FUNDS 日経225連動型上場投信"),
    ("1306", "NEXT FUNDS TOPIX連動型上場投信"),
]
DAYS_BACK = 160      # 暦日で約160日（およそ100営業日）さかのぼる
STALE_DAYS = 5       # 最新日がこれより古ければ「データが遅れている」と記録する


def api_key():
    k = (os.environ.get("JQ_API_KEY") or os.environ.get("JQ_PASSWORD") or "").strip()
    if not k:
        sys.exit("JQ_API_KEY（または JQ_PASSWORD）が設定されていません")
    return k


def get(session, path, params, retries=6):
    params = dict(params)
    out = []
    while True:
        last = ""
        for i in range(retries):
            try:
                r = session.get(f"{BASE}{path}", params=params, timeout=60)
            except requests.RequestException as e:
                last = f"通信エラー: {e}"
                time.sleep(2 ** i)
                continue
            if r.status_code == 200:
                break
            last = f"{r.status_code} {r.text[:200]}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** i * 2, 60))
                continue
            raise RuntimeError(f"{path} {params.get('code', '')}: {last}")
        else:
            raise RuntimeError(f"{path} {params.get('code', '')}: {retries}回失敗（{last}）")
        body = r.json()
        key = "data" if "data" in body else next((k for k in body if k != "pagination_key"), None)
        if key:
            out.extend(body.get(key) or [])
        pk = body.get("pagination_key")
        if not pk:
            return out
        params["pagination_key"] = pk


def num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def pick(r, adj, raw):
    v = num(r.get(adj))
    return v if v is not None else num(r.get(raw))


def to_row(r):
    row = {
        "date": str(r.get("Date"))[:10],
        "open": pick(r, "AdjO", "O"),
        "high": pick(r, "AdjH", "H"),
        "low": pick(r, "AdjL", "L"),
        "close": pick(r, "AdjC", "C"),
        "volume": pick(r, "AdjVo", "Vo"),
        "turnover": num(r.get("Va")),
    }
    # 売買が成立しなかった日（四本値が空）は除く
    if None in (row["open"], row["high"], row["low"], row["close"]):
        return None
    return row


def main():
    s = requests.Session()
    s.headers.update({"x-api-key": api_key()})
    today = dt.datetime.now(JST).date()
    frm = (today - dt.timedelta(days=DAYS_BACK)).isoformat()
    out = {"generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
           "source": "J-Quants API v2 /equities/bars/daily", "errors": [], "stocks": {}}
    for code, name in TARGETS:
        try:
            raw = get(s, "/equities/bars/daily", {"code": code, "from": frm, "to": today.isoformat()})
            rows = sorted((x for x in map(to_row, raw) if x), key=lambda x: x["date"])
            if not rows:
                out["errors"].append(f"{code}: 四本値が0件でした")
                continue
            out["stocks"][code] = {"name": name, "rows": rows}
            print(f"{code} {name}: {len(rows)}日分（{rows[0]['date']}〜{rows[-1]['date']}）", flush=True)
        except Exception as e:
            out["errors"].append(f"{code}: {e}")
            print(f"{code} 失敗: {e}", flush=True)
        time.sleep(0.3)

    dates = [v["rows"][-1]["date"] for v in out["stocks"].values()]
    out["latest_date"] = max(dates) if dates else None
    if out["latest_date"] and (today - dt.date.fromisoformat(out["latest_date"])).days > STALE_DAYS:
        out["errors"].append(
            f"最新日が {out['latest_date']} で古い（契約プランの配信遅延の可能性）")

    if not out["stocks"] and os.path.exists(OUT):
        print("1銘柄も取れなかったので、前回のファイルを残します", flush=True)
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("保存:", OUT, "最新日:", out["latest_date"], "注意:", out["errors"], flush=True)


if __name__ == "__main__":
    main()

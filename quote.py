"""日中の最新株価（20分ほど遅れ）を無料のデータ源からまとめて取る部分。

ここが失敗しても仕組み全体は止まりません。取れなかったときは
前日終値のまま表示し、ページにその旨を出します。
"""

import time


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

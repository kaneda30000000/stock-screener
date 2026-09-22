"""J-Quants API（v2）との通信をまとめた部品。

v2ではメールアドレスとパスワードでのログインは使いません。
管理画面で発行した「APIキー」を毎回の通信に添えるだけです。
"""

import os
import time
import requests

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

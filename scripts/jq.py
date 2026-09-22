"""J-Quants API との通信をまとめた部品。

やっていること
  1. メールアドレスとパスワードで「リフレッシュトークン」をもらう
  2. それを使って「IDトークン」（24時間有効な通行証）をもらう
  3. 以降はその通行証を付けてデータを取りに行く
"""

import os
import time
import requests

BASE = "https://api.jquants.com/v1"


class JQuantsError(RuntimeError):
    pass


class JQuants:
    def __init__(self, mail=None, password=None, refresh_token=None):
        self.mail = mail or os.environ.get("JQ_MAIL")
        self.password = password or os.environ.get("JQ_PASSWORD")
        self.refresh_token = refresh_token or os.environ.get("JQ_REFRESH_TOKEN")
        self.id_token = None
        self.session = requests.Session()

    # ---------- 認証 ----------
    def login(self):
        if not self.refresh_token:
            if not (self.mail and self.password):
                raise JQuantsError(
                    "J-Quantsのメールアドレスとパスワードが設定されていません。"
                    "GitHubのSecretsに JQ_MAIL と JQ_PASSWORD を登録してください。"
                )
            r = self.session.post(
                f"{BASE}/token/auth_user",
                json={"mailaddress": self.mail, "password": self.password},
                timeout=30,
            )
            if r.status_code != 200:
                raise JQuantsError(
                    f"ログインに失敗しました（{r.status_code}）。"
                    f"メールアドレスとパスワードを確認してください。応答: {r.text[:300]}"
                )
            self.refresh_token = r.json()["refreshToken"]

        r = self.session.post(
            f"{BASE}/token/auth_refresh",
            params={"refreshtoken": self.refresh_token},
            timeout=30,
        )
        if r.status_code != 200:
            raise JQuantsError(
                f"IDトークンの取得に失敗しました（{r.status_code}）。応答: {r.text[:300]}"
            )
        self.id_token = r.json()["idToken"]
        return self

    @property
    def headers(self):
        if not self.id_token:
            self.login()
        return {"Authorization": f"Bearer {self.id_token}"}

    # ---------- 共通の取得処理（ページ送り・再試行つき） ----------
    def get(self, path, params=None, key=None, retries=4):
        """1ページずつ取得して全部つなげて返す。"""
        params = dict(params or {})
        out = []
        while True:
            for attempt in range(retries):
                r = self.session.get(
                    f"{BASE}{path}", headers=self.headers, params=params, timeout=60
                )
                if r.status_code == 200:
                    break
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt + 1)
                    continue
                if r.status_code in (401, 403):
                    # 通行証が切れた可能性があるので取り直して1回だけ再挑戦
                    self.id_token = None
                    self.login()
                    continue
                raise JQuantsError(f"{path} の取得に失敗（{r.status_code}）: {r.text[:300]}")
            else:
                raise JQuantsError(f"{path} の取得に失敗（再試行しても回復せず）")

            data = r.json()
            if key is None:
                key = next(k for k in data if k != "pagination_key")
            out.extend(data.get(key, []))
            pk = data.get("pagination_key")
            if not pk:
                return out
            params["pagination_key"] = pk

    # ---------- 個別のデータ ----------
    def listed_info(self):
        """上場銘柄一覧"""
        return self.get("/listed/info", key="info")

    def trading_calendar(self, frm, to):
        """取引カレンダー（営業日かどうか）"""
        return self.get(
            "/markets/trading_calendar",
            params={"from": frm, "to": to},
            key="trading_calendar",
        )

    def daily_quotes_by_date(self, date):
        """ある1日の全銘柄の株価四本値"""
        return self.get(
            "/prices/daily_quotes", params={"date": date}, key="daily_quotes"
        )

    def statements_by_date(self, date):
        """ある1日に開示された全社の決算情報"""
        return self.get("/fins/statements", params={"date": date}, key="statements")

    def statements_by_code(self, code):
        """1銘柄の決算情報（過去分まとめて）"""
        return self.get("/fins/statements", params={"code": code}, key="statements")

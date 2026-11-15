#!/usr/bin/env python3
"""Webull署名中継: 足データ取得専用（読み取りのみ・発注系は一切なし）

Vercel サーバーレス関数。環境変数:
  WEBULL_APP_KEY / WEBULL_APP_SECRET … Webull OpenAPI(日本) の鍵。
      Vercel の環境変数にだけ置く。コードやリポジトリには書かない。
  RELAY_TOKEN … 中継の呼び出しトークン（Bearer）。呼び出し側と共有する。
  WEBULL_ENDPOINT … 任意。既定 api.webull.co.jp

呼び出し:
  GET /api/bars?symbols=SPY,QQQ&timespan=M15&count=200
  Authorization: Bearer <RELAY_TOKEN>

署名は公式SDK（webull-openapi-python-sdk）が内部で行う。
銘柄ごとの取得パターン（カテゴリの試行順・レスポンス解釈）は
旧 v1 スキャナの実績コードに合わせた。
"""
import hmac
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

ALLOWED_SYMBOLS = {
    # 指数の代用ETF
    "SPY", "QQQ", "IWM", "DIA", "SOXX",
    # 大型株
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "AVGO", "NFLX", "PLTR", "COIN", "MSTR", "JPM",
}
ETF_FIRST = {"SPY", "QQQ", "IWM", "DIA", "SOXX"}
ALLOWED_TIMESPANS = {"M1", "M5", "M15", "M30", "M60", "M240", "D", "W"}
MAX_SYMBOLS = 20
MAX_COUNT = 1200

_client = None


def get_client():
    global _client
    if _client is None:
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        api = ApiClient(os.environ["WEBULL_APP_KEY"],
                        os.environ["WEBULL_APP_SECRET"], "jp")
        api.add_endpoint("jp", os.environ.get("WEBULL_ENDPOINT",
                                              "api.webull.co.jp"))
        _client = DataClient(api)
    return _client


def parse_bars(js):
    """レスポンスの形が違っても足のリストを取り出す（v1踏襲）"""
    if isinstance(js, dict):
        for k in ("bars", "result", "data", "items"):
            if k in js:
                return parse_bars(js[k])
        vals = [v for v in js.values() if isinstance(v, list)]
        return vals[0] if vals else []
    if isinstance(js, list):
        if js and isinstance(js[0], dict) and "bars" in js[0]:
            return js[0]["bars"]
        return js
    return []


def fetch_bars(symbol, timespan, count):
    cats = ["US_ETF", "US_STOCK"] if symbol in ETF_FIRST \
        else ["US_STOCK", "US_ETF"]
    last_status = None
    for cat in cats:
        try:
            res = get_client().market_data.get_history_bar(
                symbol, cat, timespan, count)
        except TypeError:
            res = get_client().market_data.get_history_bar(
                symbol, cat, timespan)
        last_status = getattr(res, "status_code", None)
        if last_status == 200:
            rows = parse_bars(res.json())
            if rows:
                return rows
    raise RuntimeError(f"データなし (HTTP {last_status}。"
                       "403なら市場データ購読が必要)")


def normalize(rows):
    out = []
    for r in rows:
        try:
            t = r.get("time", r.get("timestamp"))
            if isinstance(t, (int, float)):
                ts = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            bar = {"t": ts.isoformat(),
                   "o": float(r["open"]), "h": float(r["high"]),
                   "l": float(r["low"]), "c": float(r["close"])}
            if r.get("volume") is not None:
                bar["v"] = float(r["volume"])
            out.append(bar)
        except Exception:
            continue
    out.sort(key=lambda b: b["t"])
    return out


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # アクセスログにトークンを残さない
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        token = os.environ.get("RELAY_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(
                auth, f"Bearer {token}"):
            return self._send(401, {"error": "unauthorized"})
        q = parse_qs(urlparse(self.path).query)
        symbols = [s.strip().upper()
                   for s in q.get("symbols", [""])[0].split(",") if s.strip()]
        timespan = q.get("timespan", ["M15"])[0].upper()
        try:
            count = int(q.get("count", ["200"])[0])
        except ValueError:
            count = 200
        if not symbols or len(symbols) > MAX_SYMBOLS:
            return self._send(400, {"error": "symbols が不正"})
        bad = [s for s in symbols if s not in ALLOWED_SYMBOLS]
        if bad:
            return self._send(400, {"error": f"許可外の銘柄: {bad}"})
        if timespan not in ALLOWED_TIMESPANS:
            return self._send(400, {"error": "timespan が不正"})
        count = max(1, min(MAX_COUNT, count))
        result = {}
        for s in symbols:
            try:
                result[s] = {"bars": normalize(fetch_bars(s, timespan, count))}
            except Exception as e:
                result[s] = {"error": str(e)[:200]}
        return self._send(200, {"timespan": timespan, "count": count,
                                "data": result})

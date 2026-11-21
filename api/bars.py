#!/usr/bin/env python3
"""Webull署名中継 v2: 足データ取得専用（読み取りのみ・発注系は一切なし）

Vercel サーバーレス関数。環境変数:
  WEBULL_APP_KEY / WEBULL_APP_SECRET … Webull OpenAPI(日本) の鍵。
  RELAY_TOKEN … 中継の呼び出しトークン（Bearer）。
  WEBULL_ENDPOINT … 任意。既定 api.webull.co.jp
  KV_REST_API_URL / KV_REST_API_TOKEN … Vercel KV (Upstash) 接続時に
      Vercel が自動で入れる。アクセストークンの保管先として使う。

モード（クエリ mode）:
  bars（既定・無人経路）… 保管済みアクセストークンを set_token して
      SDK初期化（トークン作成＋確認ポーリング）を迂回する。
      トークンを新規作成することは絶対にしない。保管が無ければ
      kind=token_missing を返して即終了する。
  renew（立ち会い専用）… 通常のSDK初期化を実行し（アプリ確認プッシュが
      飛ぶ）、得たトークンを KV とローカルファイルに保管する。
      ポーリング上限は45秒（関数の60秒に収める）。
  check … 保管済みトークンの状態だけを check_token で問い合わせる。
      作成やプッシュの引き金にはならない。

応答には計測（timing）とトークン状態（token: source/hash/expires）を
含める。トークン文字列そのものは絶対に応答に含めない。

呼び出し:
  GET /api/bars?symbols=SPY,QQQ&timespan=M15&count=200
  GET /api/bars?mode=renew
  GET /api/bars?mode=check
  Authorization: Bearer <RELAY_TOKEN>
"""
import hashlib
import re
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

ALLOWED_SYMBOLS = {
    "SPY", "QQQ", "IWM", "DIA", "SOXX",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "AVGO", "NFLX", "PLTR", "COIN", "MSTR", "JPM",
}
ETF_FIRST = {"SPY", "QQQ", "IWM", "DIA", "SOXX"}
ALLOWED_TIMESPANS = {"M1", "M5", "M15", "M30", "M60", "M240", "D", "W"}
MAX_SYMBOLS = 20
MAX_COUNT = 1200
MAX_SNAPSHOT_SYMBOLS = 20
MAX_PAGES = 6
MAX_PAGE_SIZE = 1000

TOKEN_KEY = "webull_token_v1"
TOKEN_DIR = "/tmp/webull-token"
TOKEN_FILE = os.path.join(TOKEN_DIR, "token.txt")

_clients = {}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def sanitize(msg):
    # SDKのエラーメッセージにはマスク済みトークン断片が混ざることがあるため除去する
    s = re.sub(r"token[:=]\S+", "token:<redacted>", str(msg))
    return s[:200]


# ---------- トークン保管（KV / ローカルファイル） ----------

def _kv_request(path, body=None):
    url = os.environ.get("KV_REST_API_URL", "")
    tok = os.environ.get("KV_REST_API_TOKEN", "")
    if not url or not tok:
        return None
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=body.encode() if body is not None else None,
        headers={"Authorization": f"Bearer {tok}"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def kv_available():
    return bool(os.environ.get("KV_REST_API_URL")
                and os.environ.get("KV_REST_API_TOKEN"))


def load_stored_token():
    """(record, source) を返す。record は {token, expires, status}"""
    if kv_available():
        try:
            res = _kv_request(f"/get/{TOKEN_KEY}")
            raw = (res or {}).get("result")
            if raw:
                rec = json.loads(raw)
                if rec.get("token"):
                    return rec, "kv"
        except Exception:
            pass
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.readlines()]
        if lines and lines[0]:
            return {"token": lines[0],
                    "expires": int(lines[1]) if len(lines) > 1
                    and lines[1].isdigit() else 0,
                    "status": lines[2] if len(lines) > 2 else ""}, "file"
    except Exception:
        pass
    return None, None


def save_stored_token(record):
    stored = []
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(record["token"] + "\n")
            f.write(str(record.get("expires", 0)) + "\n")
            f.write(record.get("status", "") + "\n")
        stored.append("file")
    except Exception:
        pass
    if kv_available():
        try:
            _kv_request(f"/set/{TOKEN_KEY}", json.dumps(record))
            stored.append("kv")
        except Exception:
            pass
    return stored


# ---------- SDKクライアント構築 ----------

def _base_api(token_check_seconds):
    os.chdir("/tmp")
    os.environ["WEBULL_OPENAPI_TOKEN_DIR"] = TOKEN_DIR
    from webull.core.client import ApiClient
    api = ApiClient(os.environ["WEBULL_APP_KEY"],
                    os.environ["WEBULL_APP_SECRET"], "jp",
                    token_check_duration_seconds=token_check_seconds,
                    token_check_interval_seconds=5)
    api.add_endpoint("jp", os.environ.get("WEBULL_ENDPOINT",
                                          "api.webull.co.jp"))
    return api


def build_bypass_client(record):
    """保管トークンをセットし、SDK初期化を迂回して DataClient を作る"""
    key = "bypass:" + token_hash(record["token"])
    if key in _clients:
        return _clients[key]
    from webull.data.data_client import DataClient
    from webull.core.http.initializer.client_initializer import (
        ClientInitializer)
    api = _base_api(10)
    api.set_token(record["token"])
    original = ClientInitializer.initializer
    ClientInitializer.initializer = staticmethod(lambda api_client: None)
    try:
        client = DataClient(api)
    finally:
        ClientInitializer.initializer = original
    _clients[key] = client
    return client


def build_renew_client():
    """通常のSDK初期化（トークン作成＋確認ポーリング最大45秒）"""
    if "renew" in _clients:
        return _clients["renew"]
    from webull.data.data_client import DataClient
    api = _base_api(45)
    client = DataClient(api)  # ここで初期化が走る（プッシュ発生）
    _clients["renew"] = (client, api)
    return client, api


# ---------- 足データ ----------

def parse_bars(js):
    if isinstance(js, dict):
        for k in ("bars", "result", "data", "items"):
            if k in js:
                return parse_bars(js[k])
        vals = [v for v in js.values() if isinstance(v, list)]
        return vals[0] if vals else []
    if isinstance(js, list):
        if js and isinstance(js[0], dict) and "bars" in js[0]:
            return js[0]["bars"]
        if js and isinstance(js[0], dict) and "result" in js[0]:
            return parse_bars(js[0]["result"])
        return js
    return []


def fetch_bars(client, symbol, timespan, count):
    cats = ["US_ETF", "US_STOCK"] if symbol in ETF_FIRST \
        else ["US_STOCK", "US_ETF"]
    last_status = None
    for cat in cats:
        try:
            res = client.market_data.get_history_bar(
                symbol, cat, timespan, count)
        except TypeError:
            res = client.market_data.get_history_bar(
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


def fetch_contracts(client, underlying, start_date, end_date,
                    option_type, strike_gte, strike_lte, page_size):
    """契約一覧をページングしながら集める。生の行をそのまま返す"""
    items = []
    pagination_key = None
    pages = 0
    while pages < MAX_PAGES:
        kwargs = dict(category="US_OPTION",
                      underlying_symbols=underlying,
                      page_size=page_size)
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        if option_type:
            kwargs["option_type"] = option_type
        if strike_gte:
            kwargs["strike_price_gte"] = strike_gte
        if strike_lte:
            kwargs["strike_price_lte"] = strike_lte
        if pagination_key:
            kwargs["pagination_key"] = pagination_key
        res = client.instrument.list_option_contracts(**kwargs)
        status = getattr(res, "status_code", None)
        if status != 200:
            raise RuntimeError(f"契約一覧が取得できません (HTTP {status})")
        js = res.json()
        rows, pagination_key = parse_contract_page(js)
        items.extend(rows)
        pages += 1
        if not pagination_key:
            break
    return items, pages


def parse_contract_page(js):
    """(行リスト, 次ページキー) を取り出す。構造が未知なので柔軟に読む"""
    if isinstance(js, dict):
        key = js.get("pagination_key") or js.get("next_pagination_key")
        for k in ("items", "contracts", "data", "result", "list"):
            if isinstance(js.get(k), list):
                return js[k], key
        vals = [v for v in js.values() if isinstance(v, list)]
        return (vals[0] if vals else []), key
    if isinstance(js, list):
        return js, None
    return [], None


def fetch_snapshot(client, symbols):
    res = client.option_market_data.get_option_snapshot(
        ",".join(symbols), "US_OPTION")
    status = getattr(res, "status_code", None)
    if status != 200:
        raise RuntimeError(f"気配が取得できません (HTTP {status})")
    return res.json()


def classify_error(e):
    msg = str(e).lower()
    code = str(getattr(e, "error_code", "") or
               getattr(e, "_error_code", "") or "").lower()
    if "token" in msg or "token" in code:
        return "token_invalid"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    return "api_error"


# ---------- ハンドラ ----------

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        t0 = time.time()
        token = os.environ.get("RELAY_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(
                auth, f"Bearer {token}"):
            return self._send(401, {"error": "unauthorized"})
        q = parse_qs(urlparse(self.path).query)
        mode = q.get("mode", ["bars"])[0]
        if mode == "renew":
            return self._do_renew(t0)
        if mode == "check":
            return self._do_check(t0)
        if mode == "contracts":
            return self._do_contracts(q, t0)
        if mode == "snapshot":
            return self._do_snapshot(q, t0)
        return self._do_bars(q, t0)

    def _do_bars(self, q, t0):
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

        record, source = load_stored_token()
        if record is None:
            return self._send(200, {
                "error": "保管済みトークンがありません",
                "kind": "token_missing",
                "token": {"source": None},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        t_init = time.time()
        try:
            client = build_bypass_client(record)
        except Exception as e:
            return self._send(200, {
                "error": sanitize(e), "kind": classify_error(e),
                "token": {"source": source,
                          "hash": token_hash(record["token"])},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        init_ms = int((time.time() - t_init) * 1000)

        result = {}
        per_symbol = {}
        kinds = {}
        for s in symbols:
            ts = time.time()
            try:
                raw = fetch_bars(client, s, timespan, count)
                result[s] = {"bars": normalize(raw)}
            except Exception as e:
                result[s] = {"error": sanitize(e)}
                kinds[s] = classify_error(e)
            per_symbol[s] = int((time.time() - ts) * 1000)
        out = {"timespan": timespan, "count": count, "data": result,
               "token": {"source": source,
                         "hash": token_hash(record["token"]),
                         "expires": record.get("expires", 0)},
               "timing": {"init_ms": init_ms, "per_symbol_ms": per_symbol,
                          "total_ms": int((time.time() - t0) * 1000)}}
        if kinds:
            out["error_kinds"] = kinds
        return self._send(200, out)

    def _do_renew(self, t0):
        try:
            client, api = build_renew_client()
        except Exception as e:
            rec, _src = load_stored_token()
            return self._send(200, {
                "mode": "renew", "ok": False,
                "kind": "token_not_verified",
                "error": sanitize(e),
                "token": {"status": rec.get("status") if rec else None,
                          "expires": rec.get("expires") if rec else None},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        rec, _src = load_stored_token()
        if rec is None or not api.get_token():
            return self._send(200, {
                "mode": "renew", "ok": False, "kind": "token_missing",
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        rec["token"] = api.get_token()
        rec["saved_at"] = datetime.now(timezone.utc).isoformat()
        stored = save_stored_token(rec)
        return self._send(200, {
            "mode": "renew", "ok": True,
            "token": {"status": rec.get("status"),
                      "expires": rec.get("expires"),
                      "hash": token_hash(rec["token"])},
            "stored": stored,
            "timing": {"total_ms": int((time.time() - t0) * 1000)}})

    def _do_check(self, t0):
        record, source = load_stored_token()
        if record is None:
            return self._send(200, {
                "mode": "check", "ok": False, "kind": "token_missing",
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        try:
            from webull.core.http.initializer.token.token_operation import (
                TokenOperation)
            api = _base_api(10)
            resp = TokenOperation(api).check_token(record["token"])
            js = resp.json() or {}
            return self._send(200, {
                "mode": "check", "ok": True,
                "token": {"status": js.get("status"),
                          "expires": js.get("expires"),
                          "hash": token_hash(record["token"]),
                          "source": source,
                          "stored_expires": record.get("expires", 0)},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        except Exception as e:
            return self._send(200, {
                "mode": "check", "ok": False,
                "kind": classify_error(e), "error": sanitize(e),
                "token": {"hash": token_hash(record["token"]),
                          "source": source},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})

    def _client_or_error(self, t0):
        """(client, source, record, error_response) を返す"""
        record, source = load_stored_token()
        if record is None:
            return None, None, None, self._send(200, {
                "error": "保管済みトークンがありません",
                "kind": "token_missing",
                "token": {"source": None},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        try:
            client = build_bypass_client(record)
        except Exception as e:
            return None, None, None, self._send(200, {
                "error": sanitize(e), "kind": classify_error(e),
                "token": {"source": source,
                          "hash": token_hash(record["token"])},
                "timing": {"total_ms": int((time.time() - t0) * 1000)}})
        return client, source, record, None

    def _do_contracts(self, q, t0):
        underlying = q.get("underlying", [""])[0].strip().upper()
        if underlying not in ALLOWED_SYMBOLS:
            return self._send(400, {"error": "underlying が許可外"})
        start_date = q.get("start", [""])[0].strip() or None
        end_date = q.get("end", [""])[0].strip() or None
        option_type = q.get("type", [""])[0].strip().upper() or None
        if option_type not in (None, "CALL", "PUT"):
            return self._send(400, {"error": "type が不正"})
        strike_gte = q.get("strike_gte", [""])[0].strip() or None
        strike_lte = q.get("strike_lte", [""])[0].strip() or None
        try:
            page_size = int(q.get("page_size", ["200"])[0])
        except ValueError:
            page_size = 200
        page_size = max(1, min(MAX_PAGE_SIZE, page_size))
        client, source, record, err = self._client_or_error(t0)
        if err is not None:
            return err
        ts = time.time()
        try:
            items, pages = fetch_contracts(
                client, underlying, start_date, end_date, option_type,
                strike_gte, strike_lte, page_size)
            out = {"mode": "contracts", "underlying": underlying,
                   "count": len(items), "pages": pages, "data": items}
        except Exception as e:
            out = {"mode": "contracts", "error": sanitize(e),
                   "kind": classify_error(e)}
        out["token"] = {"source": source,
                        "hash": token_hash(record["token"]),
                        "expires": record.get("expires", 0)}
        out["timing"] = {"fetch_ms": int((time.time() - ts) * 1000),
                         "total_ms": int((time.time() - t0) * 1000)}
        return self._send(200, out)

    def _do_snapshot(self, q, t0):
        symbols = [s.strip().upper()
                   for s in q.get("symbols", [""])[0].split(",")
                   if s.strip()]
        if not symbols or len(symbols) > MAX_SNAPSHOT_SYMBOLS:
            return self._send(400, {"error": "symbols が不正"})
        client, source, record, err = self._client_or_error(t0)
        if err is not None:
            return err
        ts = time.time()
        try:
            data = fetch_snapshot(client, symbols)
            out = {"mode": "snapshot", "symbols": symbols, "data": data}
        except Exception as e:
            out = {"mode": "snapshot", "error": sanitize(e),
                   "kind": classify_error(e)}
        out["token"] = {"source": source,
                        "hash": token_hash(record["token"]),
                        "expires": record.get("expires", 0)}
        out["timing"] = {"fetch_ms": int((time.time() - ts) * 1000),
                         "total_ms": int((time.time() - t0) * 1000)}
        return self._send(200, out)

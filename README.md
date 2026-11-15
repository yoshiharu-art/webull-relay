# webull-relay

Webull OpenAPI（日本）の署名をサーバー側で行い、足データだけを返す読み取り専用の中継。
設計の背景と制約は [DESIGN.md](DESIGN.md) を参照。

## デプロイ手順

1. Vercel でこのリポジトリを Import してプロジェクトを作成
2. Settings → Environment Variables に以下を登録（値は Vercel の画面で直接入力する）:
   - `WEBULL_APP_KEY` … Webull OpenAPI の App Key
   - `WEBULL_APP_SECRET` … Webull OpenAPI の App Secret
   - `RELAY_TOKEN` … 中継の呼び出しトークン（長いランダム文字列を自分で決める）
3. デプロイし、発行された URL を控える

## 使い方

```
GET /api/bars?symbols=SPY,QQQ&timespan=M15&count=200
Authorization: Bearer <RELAY_TOKEN>
```

- `timespan`: M1 / M5 / M15 / M30 / M60 / M240 / D / W
- `count`: 最大 1200
- 許可銘柄は `api/bars.py` の `ALLOWED_SYMBOLS` のみ

鍵はこのリポジトリには含まれない。環境変数にのみ存在する。

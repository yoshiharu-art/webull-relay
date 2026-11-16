# Webull署名中継 設計書

作成: 2026-11-15 / 目的: べーた式スキャナのデータ元を Yahoo から Webull（日本OpenAPI）へ切り替えるための、鍵を持たない中継の設計。

## なぜ中継が要るか

- Webull OpenAPI は App Key + App Secret の署名方式。Muse は署名方式の鍵を預かれない（チャットに貼るのも不可）。
- Webull Cloud MCP は OAuth のエンドポイントが2ドメインに跨るため、Muse のコネクタ方式では接続不可（2026-11-15 実測）。
- だから「署名する小さなプログラムを自分のサーバーに置き、Muse はその出力だけを読む」。旧 spy0dte システムと同じ構図で、あれより遥かに軽い。

## 全体像

```
Muse（スキャナ群）
  │  HTTPS + Bearer トークン（中継専用・Secure Vaultで管理）
  ▼
中継（Vercel サーバーレス関数 / このリポジトリ）
  │  公式SDKが App Secret で署名（鍵はVercel環境変数のみ）
  ▼
Webull OpenAPI（api.webull.co.jp / 日本リージョン）
  → 米国株・ETFの足データ（M15 / M60 / D など）
```

- 鍵（App Key / App Secret）は **Vercel の環境変数にだけ** 置く。コード・GitHub・チャットには一切入れない。
- 中継のコードには取引系エンドポイントが存在しない（足データの取得のみ）。万一トークンが漏れても発注はできない。

## 中継の仕様（api/bars.py）

- エンドポイント: `GET /api/bars`
- 認証: `Authorization: Bearer <RELAY_TOKEN>`（環境変数、定数時間比較。不一致は 401）
- パラメータ:
  - `symbols`: カンマ区切り。許可リスト（下記）のみ、それ以外はエラー
  - `timespan`: `M15` / `M30` / `M60` / `M240` / `D` / `W`（SDKの表記に合わせる）
  - `count`: 1〜1200（既定 200）
- 動作: 銘柄ごとに `get_history_bar`（カテゴリは ETF 銘柄は US_ETF→US_STOCK、その他は US_STOCK→US_ETF の順に試行。旧 v1 スキャナの実績パターン）
- 応答: 銘柄ごとに正規化した足 `[{t, o, h, l, c, v}]`（t は UTC ISO）。銘柄単位のエラーは全体を落とさず `error` として返す
- 許可銘柄: SPY QQQ IWM DIA SOXX（指数の代用ETF）+ 大型株14（AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO NFLX PLTR COIN MSTR JPM）

## 無料枠の計算（前の失敗を繰り返さない）

- 旧システムが溢れた原因は 60 秒ポーリング（1 日 1,440 回超）＋ニュースのスケジューラー。
- 今回の呼び出し量: 定時スキャン（寄り二段階＋反転ロング）で 1 日あたり数十回、バックテストは手動時のみ。**旧構成の 1/50 以下**。Vercel 無料枠に十分収まる設計。
- 中継側でも銘柄数×本数の上限を絞り、暴走を防ぐ。

## 正直な制約・未確認点

1. **指数そのもの（^GSPC 等）の足は Webull では取れない見込み**。指数系は代用ETF（SPY 等）の足で判定する形に切り替える（換算倍率の仕組みは既にスキャナにある）。SOX は SOXX で代用。
2. **15 分足の深さ**: 1 回 1,200 本 ≒ 46 セッション分（Yahoo の 60 日≒42 セッションと同等〜微増）。米国 API で追加された範囲指定（start/end）が日本 API で使えるかは未確認。使えれば 180 日まで拡張する（第2段階）。
3. **市場データ購読**: 旧 v1 の注意書きに「米国株/ETF の履歴データには OpenAPI 用の市場データ購読が必要（403 ならこれが原因）」とある。API 自体は無料でも、履歴バーの権限が別なら初回呼び出しで 403 になる。**最初の実呼び出しがその判定を兼ねる**（403 → 購読の要否を確認してから判断）。
4. 足は「確定足のみ」をスキャナ側で扱う前提は今まで通り（中継は生データを返すだけ）。

## 配置手順（ユーザーの作業）

1. このリポジトリを GitHub に置く（Muse が作成・push 可能）
2. Vercel でそのリポジトリを Import して新規プロジェクト作成
3. Vercel の環境変数に 3 つを設定（入力は Vercel の画面で直接。チャットには貼らない）:
   - `WEBULL_APP_KEY`
   - `WEBULL_APP_SECRET`
   - `RELAY_TOKEN`（中継用トークン。長いランダム文字列を自分で決める）
4. デプロイして発行された URL（例: `https://webull-relay-xxx.vercel.app`）を Muse に伝える

## Muse 側の作業（URL を受け取った後）

1. RELAY_TOKEN を Secure Vault 経由で登録（api_key 方式・対象ホストは Vercel のドメインのみ）
2. カスタムコネクタとして `custom.webull_relay` のドキュメント＋ヘルパーを作成
3. 検証: SPY の M15 を 200 本取得し、Yahoo の同期間の足と突き合わせて差異を確認（403 の場合は購読問題として報告）
4. 問題なければスキャナのデータ取得を中継優先＋Yahoo フォールバックに切り替え、1 週間は両系の判定差分を朝の答え合わせで観察

## セキュリティまとめ

- App Secret は Vercel 環境変数の外に出ない。GitHub リポジトリは公開されても鍵を含まない。
- 中継トークンは Muse の Secure Vault にのみ保存。漏洩時は Vercel 側で差し替えれば即無効化できる。
- 中継は読み取り専用コードのみ。取引・口座系の呼び出しは実装しない。

## v2 追記（2026-11-17）

- 遅延の主因はSDKのトークン初期化（DataClient生成時に create_token→確認ポーリング最大300秒）が、関数の60秒上限と衝突することと判明。
- 中継v2: 無人経路（bars）は保管済みトークンを set_token して初期化を迂回し、トークンを作らない。renew（立ち会い専用）だけが初期化を実行し、得たトークンを Vercel KV（キー webull_token_v1）とローカルファイルに保管する。check は check_token 単体の状態確認。
- KVは新規作成せず、既存ストア spy-v64-news-free（Upstash Free）を webull-relay に接続して流用（新規作成は支払い方法の登録が必須だったため）。環境変数 KV_REST_API_URL / KV_REST_API_TOKEN はプレフィックスなしで追加された。
- 応答には timing と token（source/hash/expires）の計測を含める。トークン文字列は応答に出さない。

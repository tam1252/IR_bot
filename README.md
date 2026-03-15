# IR Bot

LR2IR（BMS 用オンラインランキングサービス）と連携した Discord Bot。
毎週のウィークリーイベントのアナウンス・ランキング集計・BPI 計算・マイページ表示などの機能を提供する。

---

## ファイル構成

```text
IR_bot/
├── main.py                  # Bot エントリーポイント・コマンド定義
├── Dockerfile               # コンテナイメージ定義
├── insane_scores.csv        # BPI 計算用スコアデータ（理論値・平均・トップスコア等）
├── requirements.txt         # 依存パッケージ一覧
├── .env                     # 環境変数（要作成、後述）
└── src/
    ├── common.py            # 共通ユーティリティ（Google Sheets 認証・Discord defer ヘルパー）
    ├── lr2ir.py             # LR2IR ランキングスクレイピング
    ├── mypage.py            # ユーザーデータ・成績シート参照ロジック
    ├── result.py            # LR2ID → Discord 表示名の変換ロジック
    ├── generate_table.py    # Bootstrap + DataTables の HTML テーブル生成
    └── web_server.py        # マイページ配信用 aiohttp Web サーバー
```

---

## 各ファイルの役割

### `main.py`

Bot のエントリーポイント。以下を担当する。

- Bot の初期化・起動・スラッシュコマンドの同期
- `/announce` コマンド（管理者専用）: イベント情報を入力するモーダルを表示し、告知チャンネルの作成と CourseData へのアップサートを行う
- `/result` コマンド（管理者専用）: LR2IR からランキングを取得し、BPI を計算してスプレッドシートに保存・Discord に表示する
- `/bpi` コマンド: 曲名とスコアを入力して BPI を計算・表示する
- BPI 計算ロジック（`pgf` / `calculate_bpi`）
- スプレッドシートへの書き込み関数（`upsert_course_row` / `write_round_result_to_sheet` / `fetch_course_id_by_round_sync`）
- `LR2Cog`: `/register`（LR2ID 登録）・`/mypage`（成績確認）コマンドを持つ Cog
- `Help` Cog: `/help`・`/changelog` コマンド

---

### `src/common.py`

プロジェクト全体で共有するユーティリティ。

| 関数 | 説明 |
| --- | --- |
| `_authorize_gc()` | GCP サービスアカウントで gspread クライアントを生成して返す |
| `safe_defer()` | Discord Interaction の defer を安全に呼び出す（応答済みの場合は何もしない） |

---

### `src/lr2ir.py`

LR2IR のランキングページをスクレイピングするモジュール。

| 関数 | 説明 |
| --- | --- |
| `fetch_lr2_ranking(course_id)` | 指定 CourseID のランキングを取得し、DataFrame（順位/LR2ID/プレイヤー/スコア/PG/GR）で返す |

---

### `src/mypage.py`

Google Sheets からユーザーデータ・成績データを読み込む同期関数群。
すべて同期関数であり、`asyncio.run_in_executor` 経由で呼び出すことを想定している。

| 関数 | 説明 |
| --- | --- |
| `load_course_meta_map_sync(sheet_id, ws_title)` | CourseData を読み込み、`{ '回': {title, diff} }` の辞書を返す |
| `_fetch_user_record_one_round_sync(sheet_id, round, lr2id)` | 指定回のシートから LR2ID 一致の1行と総人数を返す |
| `_fetch_user_records_all_rounds_sync(sheet_id, lr2id)` | 全回シートを走査してユーザーの全記録リストを返す |
| `_get_lr2id_by_discord_sync(sheet_id, ws_title, discord_id)` | UserData から Discord ID に対応する LR2ID を返す |

---

### `src/result.py`

Discord のメンバー情報と UserData を紐づけるモジュール。

| 関数 | 説明 |
| --- | --- |
| `build_id_to_name_from_sheet(guild)` | UserData を読み込み、`{ LR2ID: Discord表示名 }` の辞書を返す（非同期） |

---

### `src/generate_table.py`

pandas DataFrame を Bootstrap 5 + DataTables の HTML ページに変換するモジュール。
`/mypage all` の全記録表示時に使用する。

| 関数 | 説明 |
| --- | --- |
| `generate_bootstrap_html_table(df, title)` | DataFrame を検索・ページング対応の HTML テーブルに変換して文字列で返す |

---

### `src/web_server.py`

生成した HTML をインメモリに保持し、aiohttp で URL 配信するモジュール。
`/mypage all` 実行時に HTML を保存し、ユニークな URL を発行する。

| 関数 | 説明 |
| --- | --- |
| `store_page(html)` | HTML を保存してアクセス用トークンを返す（TTL: 24時間） |
| `start_web_server(host, port)` | aiohttp サーバーを起動して `AppRunner` を返す |

---

## 環境変数（`.env`）

```env
DISCORD_TOKEN=      # Discord Bot トークン
MAIN_ID=            # NebukawaIR スプレッドシートの ID（CourseData タブを含む）
SCORE_ID=           # NebukawaIR(result) スプレッドシートの ID（回ごとの成績タブを含む）
USERDATA_ID=        # UserData スプレッドシートの ID（省略時は MAIN_ID を使用）
USERDATA_WS=        # UserData タブ名（デフォルト: UserData）
COURSE_WS=          # CourseData タブ名（デフォルト: CourseData）
GCP_SA_JSON=        # GCP サービスアカウントの JSON（文字列）
ANNOUNCE_CHANNEL=   # @everyone 告知を投稿するチャンネル名（デフォルト: 一般）

# マイページ Web サーバー
WEB_HOST=           # バインドアドレス（デフォルト: 0.0.0.0）
WEB_PORT=           # ポート番号（デフォルト: 8080）
WEB_BASE_URL=       # 外部公開 URL（例: https://xxxx.code.run）
```

---

## スプレッドシート構成

| スプレッドシート | タブ名 | 内容 |
| --- | --- | --- |
| NebukawaIR（`MAIN_ID`） | `CourseData` | 回・難易度・曲名・CourseID |
| NebukawaIR（`MAIN_ID`） | `UserData` | DiscordID・LR2ID の対応表 |
| NebukawaIR(result)（`SCORE_ID`） | `1`, `2`, ... | 各回のランキング結果（Rank/LR2ID/PlayerName/Score/Score Rate/BPI） |

---

## コマンド一覧

| コマンド | 権限 | 説明 |
| --- | --- | --- |
| `/register [lr2id]` | 全員 | Discord ID と LR2ID を紐づけて登録する |
| `/mypage [回数\|all]` | 全員 | 指定回の成績を表示。`all` でマイページの URL を返す（24時間有効） |
| `/bpi [song] [score]` | 全員 | 曲名（オートコンプリート対応）とスコアから BPI を計算 |
| `/changelog` | 全員 | Bot の更新情報を表示（UPDATES.md 全セクション） |
| `/announce` | 管理者 | イベント情報をモーダルで入力し、告知チャンネルを作成 |
| `/result [回数]` | 管理者 | LR2IR からランキングを取得してスプレッドシートに保存・表示 |
| `/help` | 全員 | コマンド一覧を表示 |

---

## セットアップ

```bash
# 依存パッケージのインストール
pip install -r requirements.txt

# .env を作成して環境変数を設定（上記の環境変数セクションを参照）

# Bot 起動
python main.py
```

---

## 追加予定機能

（随時追記）

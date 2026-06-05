# MCP テストサーバー

injection-tool との接続テスト用 Python MCP サーバーです。

## セットアップ

```powershell
# 仮想環境を有効化
.venv\Scripts\Activate

# 依存パッケージがない場合はインストール
pip install -r requirements.txt
```

## サーバー起動

```powershell
# SSE モード（HTTP: http://localhost:8000/sse）
.venv\Scripts\python server.py

# または ポート指定
.venv\Scripts\mcp run server.py --port 8080
```

## テストクライアント実行

別ターミナルで:

```powershell
.venv\Scripts\python test_client.py
```

## injection-tool への登録

injection-tool の管理画面 → MCP管理 で以下を設定:

| 項目 | 値 |
|------|-----|
| 名前 | test-server |
| URL | http://localhost:8000/sse |
| 説明 | テスト用 MCP サーバー |

## 提供ツール

| ツール名 | 説明 | 引数 |
|----------|------|------|
| `echo` | メッセージをそのまま返す | `message: str` |
| `get_current_time` | 現在の日時を返す | なし |
| `calculate` | 数式を計算する | `expression: str` |
| `get_mock_weather` | モック天気情報を返す | `city: str` |
| `list_tools_info` | ツール一覧を返す | なし |

## MCP Inspector での確認

```powershell
# Node.js環境がある場合
npx @modelcontextprotocol/inspector .venv\Scripts\python server.py
```

## 処理フロー

通常の Web 検索と、サイトクロール構造化は別フローで動きます。

~~~mermaid
flowchart TD
    A[search_web呼び出し] --> B[DuckDuckGo HTML取得]
    B --> C[検索結果抽出]
    C --> D[日本語優先で並べ替え]
    D --> E[上位3件を選定]
    E --> F[各URLをfetch_urlで要約取得]
    F --> G[JSONで返却]

    H[crawl_site呼び出し] --> I[サイト内リンクを巡回]
    I --> J[ページ本文抽出]
    J --> K[ページ種別判定: content/list/form/navigation/system]
    K --> L[共通テキスト除去]
    L --> M[_build_blockで構造化]
    M --> N[_normalize_page_title適用]
    N --> O[role/label/priority付与]
    O --> P[blocks + metaをJSON返却]
~~~

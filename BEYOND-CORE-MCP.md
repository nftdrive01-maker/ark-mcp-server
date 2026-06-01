# MCP エージェント統合ガイド

BEYOND Core MCP サーバーを Claude Desktop や Cursor などの外部 AI エージェントから接続・テストするためのガイドです。

## 概要

| 項目 | 詳細 |
|------|------|
| **プロトコル** | JSON-RPC 2.0 over TCP |
| **ホスト** | `127.0.0.1` (ローカルホスト) |
| **ポート** | `8001` |
| **ツール数** | 11個 |
| **対応クライアント** | Claude Desktop, Cursor, その他 MCP クライアント |

---

## 前提条件

1. **BEYOND Core バックエンド起動**
   ```powershell
   cd e:\nftdrive-v3\beyond-core\backend
   .\venv\Scripts\Activate.ps1
   python run.py
   ```
   サーバーが `http://localhost:8000` で起動することを確認

2. **MCP サーバー起動確認**
   ```powershell
   curl.exe -s -X POST http://localhost:8000/api/mcp/start
   ```
   レスポンス例：
   ```json
   {"success":true,"message":"MCP Server started","pid":88680}
   ```

3. **ステータス確認**
   ```powershell
   curl.exe -s http://localhost:8000/api/mcp/status
   ```
   レスポンス：`running: true`

---

## Claude Desktop の設定

### 1. Claude Desktop インストール
- [Claude Desktop ダウンロード](https://claude.ai/download) から最新版をインストール

### 2. 設定ファイル編集
Claude Desktop の設定ファイルは以下の場所にあります：

**Windows:**
```
%APPDATA%\Claude\claude_desktop_config.json
```

**macOS:**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Linux:**
```
~/.config/Claude/claude_desktop_config.json
```

### 3. MCP サーバー設定を追加

`claude_desktop_config.json` を編集して、以下のように `mcpServers` セクションに追加：

```json
{
  "mcpServers": {
    "beyond-core": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-stdlib"
      ],
      "env": {
        "BEYOND_MCP_HOST": "127.0.0.1",
        "BEYOND_MCP_PORT": "8001"
      },
      "disabled": false
    },
    "beyond-tcp": {
      "command": "python",
      "args": [
        "-m",
        "mcp.client.stdio_client",
        "127.0.0.1:8001"
      ],
      "disabled": false
    }
  }
}
```

**または** TCP クライアント経由の設定（推奨）：

```json
{
  "mcpServers": {
    "beyond-core": {
      "disabled": false,
      "command": "python",
      "args": [
        "-c",
        "import socket, json, sys; s=socket.socket(); s.connect(('127.0.0.1', 8001)); print('Connected')"
      ]
    }
  }
}
```

### 4. Claude Desktop 再起動
編集後、Claude Desktop を完全に終了してから再起動します。

---

## Cursor の設定

### 1. Cursor インストール
- [Cursor ダウンロード](https://www.cursor.com/) から最新版をインストール

### 2. MCP サーバー設定

Cursor の設定ファイル：
```
%APPDATA%\Cursor\User\settings.json
```

以下を追加：

```json
{
  "mcpServers": {
    "beyond-core": {
      "command": "python",
      "args": [
        "-m",
        "mcp.client.stdio_client",
        "127.0.0.1:8001"
      ]
    }
  }
}
```

### 3. Cursor 再起動

---

## Python でのテスト接続

### 基本的な接続テスト

```python
import socket
import json

def test_mcp_connection():
    """MCP サーバーへの TCP 接続テスト"""
    try:
        # TCP ソケット作成
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        
        # サーバーに接続
        print("Connecting to MCP Server...")
        s.connect(("127.0.0.1", 8001))
        print("✓ Connected to 127.0.0.1:8001")
        
        # tools/list リクエスト送信
        request = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "id": 1
        }
        s.sendall(json.dumps(request).encode() + b'\n')
        print(f"✓ Sent: {request['method']}")
        
        # レスポンス受信
        response_raw = s.recv(4096).decode()
        response = json.loads(response_raw)
        
        print(f"✓ Response received:")
        print(f"  Tools count: {len(response.get('result', {}).get('tools', []))}")
        
        # ツール一覧表示
        tools = response.get('result', {}).get('tools', [])
        for tool in tools:
            print(f"    - {tool['name']}: {tool.get('description', 'N/A')}")
        
        s.close()
        return True
        
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False

if __name__ == "__main__":
    test_mcp_connection()
```

実行：
```bash
python test_mcp.py
```

### tools/call テスト（例：memory_list）

```python
import socket
import json

def test_mcp_tool_call():
    """MCP ツール呼び出しテスト"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", 8001))
        
        # memory_list を呼び出し
        request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "memory_list",
                "arguments": {}
            },
            "id": 2
        }
        
        s.sendall(json.dumps(request).encode() + b'\n')
        response_raw = s.recv(4096).decode()
        response = json.loads(response_raw)
        
        print("Memory List:")
        print(json.dumps(response, indent=2, ensure_ascii=False))
        
        s.close()
        return response
        
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    test_mcp_tool_call()
```

---

## 利用可能なツール

MCP サーバーが提供する 11 個のツール：

### 1. **chronicle_chat** - CHRONICLE との会話
```json
{
  "name": "chronicle_chat",
  "description": "CHRONICLE メモリーシステムと会話",
  "arguments": {
    "message": "string (required)",
    "mode": "string (optional: 'normal' or 'system')"
  }
}
```

### 2. **chronicle_session_chat** - セッション内チャット
### 3. **memory_list** - メモリ一覧取得
### 4. **memory_create** - メモリ作成
### 5. **memory_update** - メモリ更新
### 6. **memory_delete** - メモリ削除
### 7. **ai_queue_list_jobs** - AI キュージョブ一覧
### 8. **ai_queue_create_job** - AI キュージョブ作成
### 9. **system_settings_get** - システム設定取得
### 10. **system_settings_update** - システム設定更新
### 11. **network_status** - ネットワーク状態確認

---

## トラブルシューティング

### エラー：接続拒否（Connection Refused）
```
✗ Connection failed: [Errno 10061] ターゲット マシンによって接続が拒否されました
```

**原因と対策：**
1. FastAPI サーバーが起動していないか確認
   ```powershell
   curl.exe -s http://localhost:8000/api/mcp/status
   ```

2. MCP サーバーが起動していないか確認
   ```powershell
   curl.exe -s -X POST http://localhost:8000/api/mcp/start
   ```

3. ファイアウォール設定を確認（ローカルホスト接続なので通常は問題なし）

### エラー：タイムアウト（Timeout）
```
✗ Connection failed: timeout
```

**原因と対策：**
- MCP サーバーが応答していない場合、FastAPI ログを確認
- `curl.exe -s http://localhost:8000/api/mcp/status` で `running: true` か確認

### エラー：Invalid JSON
```
✗ Connection failed: json.decoder.JSONDecodeError
```

**原因と対策：**
- リクエスト形式が JSON-RPC 2.0 に準拠していることを確認
- `"jsonrpc": "2.0"` と `"id"` フィールドが必須

---

## テスト実行例

### 1. 接続テスト
```powershell
# PowerShell での接続確認
$s = New-Object System.Net.Sockets.TcpClient("127.0.0.1", 8001)
$w = New-Object System.IO.StreamWriter($s.GetStream())
$w.WriteLine('{"jsonrpc":"2.0","method":"tools/list","id":1}')
$w.Flush()
$r = New-Object System.IO.StreamReader($s.GetStream())
$r.ReadLine()
$s.Close()
```

### 2. Python テスト実行
```bash
# テストスクリプト実行
python e:\nftdrive-v3\test_mcp_connection.py
```

### 3. Claude Desktop でテスト
1. Claude Desktop 起動
2. チャットで：`@beyond-core`（またはサーバー名）と入力
3. "What tools are available?" と質問
4. MCP ツール一覧が表示されることを確認

---

## 統合チェックリスト

- [ ] FastAPI サーバー起動済み（port 8000）
- [ ] MCP サーバー起動済み（port 8001）
- [ ] API `/api/mcp/status` で `running: true` 確認
- [ ] TCP 接続テスト成功
- [ ] tools/list で 11 個のツール確認
- [ ] Claude Desktop 設定ファイル編集完了
- [ ] Claude Desktop 再起動完了
- [ ] エージェント接続テスト実施

---

## 次のステップ

1. **エージェント統合テスト**
   - Claude Desktop で "Use MCP tools" オプション確認
   - memory_list, chronicle_chat などのツール呼び出しテスト

2. **実運用連携**
   - カスタム プロンプト設定で MCP ツール利用を指示
   - 自動化ワークフロー構築

3. **本番環境設定**
   - ネットワーク範囲で MCP サーバーを提供する場合
   - ファイアウォール設定（port 8001 アクセス許可）
   - セキュリティ設定（認証など）

---

## 参考資料

- [Model Context Protocol 仕様](https://spec.modelcontextprotocol.io/)
- [Claude Desktop MCP 設定](https://claude.ai/docs/mcp)
- [Cursor MCP 統合](https://docs.cursor.com/advanced/mcp)


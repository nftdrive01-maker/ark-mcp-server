"""
MCP サーバーのテストクライアント（MCP Python SDK使用）
サーバーを起動した後にこのスクリプトを実行してください。

使い方:
  1. python server.py を別ターミナルで起動
  2. python test_client.py
"""

import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client


SERVER_URL = "http://localhost:8000/sse"


async def main():
    print("=" * 50)
    print("MCP サーバー テストクライアント")
    print("=" * 50)

    try:
        async with sse_client(SERVER_URL) as (read, write):
            async with ClientSession(read, write) as session:

                # 1. initialize
                print("\n[1] initialize...")
                result = await session.initialize()
                print(f"  サーバー名: {result.serverInfo.name}")
                print(f"  プロトコル: {result.protocolVersion}")

                # 2. tools/list
                print("\n[2] ツール一覧取得...")
                tools_result = await session.list_tools()
                for t in tools_result.tools:
                    print(f"  - {t.name}: {t.description or ''}")

                # 3. ツール呼び出しテスト
                print("\n[3] ツール呼び出しテスト...")

                tests = [
                    ("echo", {"message": "Hello, MCP!"}),
                    ("get_current_time", {}),
                    ("calculate", {"expression": "2 + 3 * 4"}),
                    ("get_mock_weather", {"city": "東京"}),
                ]

                for tool_name, args in tests:
                    try:
                        call_result = await session.call_tool(tool_name, args)
                        content = call_result.content
                        text = content[0].text if content else "(no output)"
                        print(f"  [{tool_name}] {text}")
                    except Exception as e:
                        print(f"  [{tool_name}] エラー: {e}")

                # 4. リソース取得
                print("\n[4] リソース一覧取得...")
                try:
                    resources_result = await session.list_resources()
                    for r in resources_result.resources:
                        print(f"  - {r.uri}: {r.name or ''}")

                    if resources_result.resources:
                        uri = resources_result.resources[0].uri
                        res_content = await session.read_resource(uri)
                        text = res_content.contents[0].text if res_content.contents else "(空)"
                        print(f"\n  [{uri}] の内容:\n  {text}")
                except Exception as e:
                    print(f"  エラー: {e}")

    except Exception as e:
        print(f"\n接続失敗: {e}")
        print("サーバーが起動していますか? python server.py を先に実行してください。")
        return

    print("\n" + "=" * 50)
    print("テスト完了")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())

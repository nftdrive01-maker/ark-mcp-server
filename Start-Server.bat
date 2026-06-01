@echo off
echo MCP テストサーバーを起動します...
echo URL: http://localhost:8000/sse
echo 停止: Ctrl+C
echo.
D:\mcp-server\.venv\Scripts\python.exe D:\mcp-server\server.py
pause

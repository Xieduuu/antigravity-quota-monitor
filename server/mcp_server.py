# -*- coding: utf-8 -*-
"""
Antigravity 额度与上下文监控插件 — 零依赖纯 Python MCP Apps Server。

实现特性：
1. 纯 Python 标准库实现 MCP stdio JSON-RPC 2.0 协议与 MCP Apps（SEP-1865）扩展，
   无需安装任何第三方 pip/npm 依赖。
2. 暴露 `open_monitor` 入口工具，挂载至 Antigravity 右侧辅助栏（aux_pane）与左侧全局页（global）。
3. 暴露 `visibility: ["app"]` 的纯前端工具（`app_get_dashboard`、`app_update_preferences`），对模型完全不可见，运行零额外 Token 消耗。
4. 支持 `--preview [port]` 本地浏览器预览模式，便于离线联调 UI 视觉效果。
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

# 确保同目录模块可直接导入
SERVER_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from data_engine import QuotaMonitorEngine


APP_RESOURCE_URI = "ui://quota-monitor/app.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"
SERVER_NAME = "antigravity-quota-monitor"
SERVER_VERSION = "0.1.1"


def load_app_html() -> str:
    """每次读取最新前端单文件 HTML，方便热更新调试。"""
    override = os.environ.get("QUOTA_MONITOR_APP_HTML")
    html_path = Path(override) if override else (PLUGIN_ROOT / "ui" / "app.html")
    return html_path.read_text(encoding="utf-8")


def build_tools_manifest() -> List[Dict[str, Any]]:
    """声明暴露给宿主与前端 iframe 的全部 MCP 工具及其 UI 元数据。"""
    return [
        {
            "name": "open_monitor",
            "title": "额度与上下文监控",
            "description": (
                "打开 Antigravity 额度与上下文监控面板，展示当前会话精确上下文占用、"
                "缓存命中率、历史每日用量日历及 Gemini / Claude & GPT / Argon 额度同步。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {
                        "type": "string",
                        "description": "可选，指定要聚焦查看的会话 ID。",
                    }
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "resourceUri": APP_RESOURCE_URI,
                },
                "ui/resourceUri": APP_RESOURCE_URI,
                "com.google.antigravity/ui": {
                    "entrypoints": [
                        {"type": "aux_pane", "title": "额度与上下文"},
                        {"type": "global", "title": "用量与额度"},
                    ]
                },
            },
        },
        {
            "name": "app_get_dashboard",
            "title": "刷新监控仪表盘",
            "description": "仅限前端 UI 调用：拉取最新会话上下文、额度与指定月份用量日历。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "string"},
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                    "forceQuotaRefresh": {
                        "type": "boolean",
                        "description": "立即向桌面端查询额度，跳过插件的短时缓存。",
                    },
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
        {
            "name": "app_update_preferences",
            "title": "更新面板偏好设置",
            "description": "仅限前端 UI 调用：保存数字单位、自动跟随会话与刷新间隔等偏好。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "unitMode": {"type": "string", "enum": ["compact", "exact"]},
                    "followActiveSession": {"type": "boolean"},
                    "autoRefreshSec": {"type": "integer"},
                    "customContextLimits": {"type": "object"},
                    "conversationId": {"type": "string"},
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
    ]


class McpQuotaServer:
    """基于 JSON-RPC 2.0 的标准 MCP Server 处理器。"""

    def __init__(self, engine: QuotaMonitorEngine) -> None:
        self.engine = engine

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理单条 JSON-RPC 请求；通知类消息返回 None。"""
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        # Notification（无 id）无需响应
        if req_id is None:
            return None

        try:
            if method == "initialize":
                client_ver = params.get("protocolVersion") or "2025-03-26"
                return self._ok(
                    req_id,
                    {
                        "protocolVersion": client_ver,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                )

            if method == "ping":
                return self._ok(req_id, {})

            if method == "tools/list":
                return self._ok(req_id, {"tools": build_tools_manifest()})

            if method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                result = self._call_tool(name, args)
                return self._ok(req_id, result)

            if method == "resources/list":
                return self._ok(
                    req_id,
                    {
                        "resources": [
                            {
                                "uri": APP_RESOURCE_URI,
                                "name": "Antigravity Quota & Context Monitor UI",
                                "description": "额度与上下文实时监控交互面板",
                                "mimeType": APP_MIME_TYPE,
                                "_meta": {
                                    "ui": {
                                        "prefersBorder": False,
                                    }
                                },
                            }
                        ]
                    },
                )

            if method == "resources/read":
                uri = str(params.get("uri") or "")
                if uri != APP_RESOURCE_URI:
                    return self._err(req_id, -32602, f"未知资源 URI: {uri}")
                html = load_app_html()
                return self._ok(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": APP_RESOURCE_URI,
                                "mimeType": APP_MIME_TYPE,
                                "text": html,
                                "_meta": {
                                    "ui": {
                                        "prefersBorder": False,
                                    }
                                },
                            }
                        ]
                    },
                )

            return self._err(req_id, -32601, f"不支持的方法: {method}")

        except Exception as exc:
            return self._err(req_id, -32603, f"服务器内部错误: {exc}")

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """执行具体工具调用并返回 MCP CallToolResult。"""
        cid = args.get("conversationId")
        year = args.get("year")
        month = args.get("month")

        if name == "open_monitor":
            dashboard = self.engine.build_dashboard(conversation_id=cid, year=year, month=month)
            summary_text = self._format_text_summary(dashboard)
            return {
                "content": [{"type": "text", "text": summary_text}],
                "structuredContent": dashboard,
                "isError": False,
            }

        if name == "app_get_dashboard":
            dashboard = self.engine.build_dashboard(
                conversation_id=cid, year=year, month=month,
                force_quota_refresh=args.get("forceQuotaRefresh") is True,
            )
            return {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": dashboard,
                "isError": False,
            }

        if name == "app_update_preferences":
            self.engine.update_preferences(args)
            dashboard = self.engine.build_dashboard(conversation_id=cid, year=year, month=month)
            return {
                "content": [{"type": "text", "text": "偏好已更新"}],
                "structuredContent": dashboard,
                "isError": False,
            }

        return {
            "content": [{"type": "text", "text": f"未知工具: {name}"}],
            "isError": True,
        }

    @staticmethod
    def _format_text_summary(dashboard: Dict[str, Any]) -> str:
        """为模型生成简洁的一行文字摘要，避免浪费对话上下文。"""
        sess = dashboard.get("session")
        usage = dashboard.get("usageSummary") or {}
        parts = ["已打开额度与上下文监控面板。"]
        if sess:
            ctx = sess["context"]
            tok = sess["tokens"]
            parts.append(
                f"当前会话（{sess['modelDisplay']}）上下文 {ctx['usedPct']}% "
                f"({ctx['usedTokens']:,}/{ctx['limitTokens']:,} tok)，缓存命中 {tok['cacheHitPct']}%。"
            )
        parts.append(f"今日累计用量 {usage.get('todayTokens', 0):,} tok。")
        return " ".join(parts)

    @staticmethod
    def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def run_stdio_server(engine: QuotaMonitorEngine) -> None:
    """运行标准 MCP stdio 服务循环（逐行 JSON-RPC 2.0）。"""
    server = McpQuotaServer(engine)
    # Windows 下确保 stdin/stdout 使用 UTF-8
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        resp = server.handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def run_preview_http_server(engine: QuotaMonitorEngine, port: int) -> None:
    """启动本地轻量 HTTP 预览服务，方便在浏览器直接调试 UI。"""

    class PreviewHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                body = load_app_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/dashboard":
                qs = urllib.parse.parse_qs(parsed.query)
                cid = (qs.get("conversationId") or [None])[0]
                year = int(qs["year"][0]) if "year" in qs and qs["year"][0] else None
                month = int(qs["month"][0]) if "month" in qs and qs["month"][0] else None
                force_quota_refresh = qs.get("forceQuotaRefresh") == ["true"]
                self._send_json(engine.build_dashboard(
                    conversation_id=cid, year=year, month=month, force_quota_refresh=force_quota_refresh,
                ))
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if self.path == "/api/preferences":
                engine.update_preferences(payload)
                self._send_json(
                    engine.build_dashboard(
                        conversation_id=payload.get("conversationId"),
                        year=payload.get("year"),
                        month=payload.get("month"),
                    )
                )
                return
            self.send_response(404)
            self.end_headers()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)
    sys.stderr.write(f"[Preview] http://127.0.0.1:{port}\n")
    sys.stderr.flush()
    httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity Quota & Context Monitor MCP Server")
    parser.add_argument("--data-dir", type=str, default=None, help="插件持久化数据目录（默认读取 PLUGIN_DATA）")
    parser.add_argument("--brain-dir", type=str, default=None, help="可选覆盖 Antigravity brain 目录")
    parser.add_argument("--preview", type=int, default=0, help="启动本地 HTTP 预览端口（例如 8765）")
    args = parser.parse_args()

    brain_dirs = [Path(args.brain_dir)] if args.brain_dir else None
    engine = QuotaMonitorEngine(
        data_dir=Path(args.data_dir) if args.data_dir else None,
        brain_dirs=brain_dirs,
    )

    if args.preview and args.preview > 0:
        run_preview_http_server(engine, args.preview)
    else:
        run_stdio_server(engine)


if __name__ == "__main__":
    main()

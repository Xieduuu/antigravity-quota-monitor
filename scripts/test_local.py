# -*- coding: utf-8 -*-
"""
Antigravity Quota & Context Monitor 自动化测试脚本。

覆盖验证：
1. 模型分组归类（Gemini / Claude & GPT / Argon）与上下文窗口默认值。
2. transcript.jsonl 解析准确性（上下文占用、缓存命中率、时间统计、分段守恒）。
3. 桌面端余量同步、短时缓存和外部额度文件回退。
4. hook_tracker.py 子进程 stdin/stdout 契约测试。
5. mcp_server.py 完整 stdio JSON-RPC 2.0 协议端到端联调（含 MCP Apps 元数据断言）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from data_engine import (  # noqa: E402
    QuotaMonitorEngine,
    classify_model_group,
    get_default_context_limit,
)
from live_quota import LiveQuotaSource, parse_quota_summary  # noqa: E402
from mcp_server import McpQuotaServer  # noqa: E402


def make_sample_transcript(path: Path, now: dt.datetime) -> None:
    """生成一份符合 Antigravity 真实格式的模拟 transcript.jsonl。"""
    t0 = (now - dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    t1 = (now - dt.timedelta(minutes=29, seconds=40)).isoformat().replace("+00:00", "Z")
    t2 = (now - dt.timedelta(minutes=29, seconds=10)).isoformat().replace("+00:00", "Z")
    t3 = (now - dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    t4 = (now - dt.timedelta(minutes=9, seconds=30)).isoformat().replace("+00:00", "Z")

    steps = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": t0,
            "content": "帮我检查这个项目的架构并做性能优化",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": t1,
            "model": "gemini-4-neon-high",
            "input_tokens": 18000,
            "cache_read_tokens": 0,
            "output_tokens": 600,
            "thinking": "先检查代码结构",
            "tool_calls": [{"name": "read_file"}, {"name": "run_command"}],
        },
        {
            "step_index": 2,
            "source": "SYSTEM",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": t2,
            "content": "tool output ok",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": t3,
            "model": "gemini-4-neon-high",
            "input_tokens": 4000,
            "cache_read_tokens": 18600,
            "output_tokens": 1400,
            "content": "已完成分析，下面是优化建议……",
        },
        {
            "step_index": 4,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": t4,
            "model": "claude-sonnet-5.5",
            "input_tokens": 2000,
            "cache_read_tokens": 22000,
            "output_tokens": 1000,
            "content": "补充 Claude 视角复核。",
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n", encoding="utf-8")


def test_classification() -> None:
    assert classify_model_group("gemini-4-neon-high") == "gemini"
    assert classify_model_group("gemini-4-argon-medium") == "argon"
    assert classify_model_group("claude-opus-5.5-medium") == "claude_gpt"
    assert classify_model_group("gpt-oss-120b-medium") == "claude_gpt"
    assert get_default_context_limit("gemini-4-neon-high") == 1_000_000
    assert get_default_context_limit("claude-sonnet-5.5") == 200_000
    print("[PASS] 模型分类与上下文默认上限")


@patch.object(LiveQuotaSource, "read", return_value={})
def test_engine_and_quota_window(_mock_live_quota) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        brain = tmp_path / "brain"
        data_dir = tmp_path / "data"
        conv_id = "11111111-2222-3333-4444-555555555555"
        tr_path = brain / conv_id / ".system_generated" / "logs" / "transcript.jsonl"

        now = dt.datetime.now(dt.timezone.utc)
        make_sample_transcript(tr_path, now)

        # 构造模拟 conversation_summaries.db，验证优先使用官方对话名称而非第一句话
        db_path = tmp_path / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO conversation_summaries (conversation_id, title) VALUES (?, ?)",
            (conv_id, "Antigravity 桌面端插件开发计划"),
        )
        conn.commit()
        conn.close()

        engine = QuotaMonitorEngine(data_dir=data_dir, brain_dirs=[brain])
        dash = engine.build_dashboard()

        assert dash["conversationCount"] == 1
        sess = dash["session"]
        assert sess is not None
        assert sess["title"] == "Antigravity 桌面端插件开发计划"
        # 最新一步为 step 4: 2000 + 22000 + 1000 = 25000
        assert sess["context"]["usedTokens"] == 25000
        bd = sess["context"]["breakdown"]
        assert bd["systemAndTools"] + bd["dialogue"] + bd["toolOutputs"] == 25000
        # 全会话累计: uncached=24000, cache=40600, output=3000
        assert sess["tokens"]["uncachedInput"] == 24000
        assert sess["tokens"]["cacheRead"] == 40600
        assert sess["tokens"]["output"] == 3000
        assert sess["stats"]["toolCallCount"] == 2
        # 严格 1:1 配对耗时：step 1 (600 tok / 30s) + step 3 (1400 tok / 30s) = 2000 / 60 = 33.3 tok/s，末轮 step 4 不虚增
        assert sess["stats"]["avgOutputTps"] == 33.3

        initial_bucket = next(b for b in dash["quotas"]["buckets"] if b["id"] == "gemini_5h")
        new_step_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)).isoformat()
        with tr_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "step_index": 5,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": new_step_at,
                        "model": "gemini-4-neon-high",
                        "input_tokens": 10000,
                        "cache_read_tokens": 0,
                        "output_tokens": 1000,
                    }
                )
                + "\n"
            )

        dash2 = engine.build_dashboard()
        g5 = next(b for b in dash2["quotas"]["buckets"] if b["id"] == "gemini_5h")
        assert g5["quota"]["remainingPct"] is None
        assert g5["windowUsage"]["totalTokens"] == initial_bucket["windowUsage"]["totalTokens"] + 11000
        assert g5["windowUsage"]["calls"] == initial_bucket["windowUsage"]["calls"] + 1
        assert dash2["session"]["context"]["usedTokens"] == 11000
        # 超过 120s 空窗与末轮未闭合步骤不应虚增平均输出速率
        assert dash2["session"]["stats"]["avgOutputTps"] == 33.3

        # 测试标准外部额度文件 external_quota.json 优先接入
        ext_file = data_dir / "external_quota.json"
        ext_file.write_text(
            json.dumps(
                {
                    "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "buckets": {
                        "gemini_5h": {
                            "remainingPct": 77.5,
                            "resetsAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).isoformat(),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        dash3 = engine.build_dashboard()
        assert dash3["quotas"]["source"] == "external"
        g5_ext = next(b for b in dash3["quotas"]["buckets"] if b["id"] == "gemini_5h")
        assert g5_ext["quota"]["source"] == "external"
        assert g5_ext["quota"]["remainingPct"] == 77.5
        print("[PASS] 日志增量解析、本机用量和外部额度文件")


def test_hook_tracker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["PLUGIN_DATA"] = tmp
        payload = {
            "conversationId": "hook-test-conv",
            "transcriptPath": str(Path(tmp) / "fake_transcript.jsonl"),
            "modelName": "gemini-4-neon-high",
            "hookEventName": "PostInvocation",
        }
        proc = subprocess.run(
            [sys.executable, str(ROOT / "server" / "hook_tracker.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        assert json.loads(proc.stdout.strip()) == {}
        active_file = Path(tmp) / "active_session.json"
        assert active_file.exists()
        saved = json.loads(active_file.read_text(encoding="utf-8"))
        assert saved["conversationId"] == "hook-test-conv"
        print("[PASS] hook_tracker.py 生命周期钩子契约")


def test_live_quota_precedence() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(hours=3)).isoformat()
    expired = (now - dt.timedelta(seconds=1)).isoformat()
    payload = {"response": {"groups": [{"buckets": [
        {"bucketId": "gemini-5h", "remainingFraction": 0, "resetTime": future},
        {"bucketId": "gemini-weekly", "remaining": {"remainingFraction": 0.44}, "resetTime": expired},
        {"bucketId": "gemini-argon-wtu-weekly", "remaining": {"case": "remainingFraction", "value": 0.5}},
        {"bucketId": "3p-5h", "remainingFraction": 0.8, "disabled": True},
        {"bucketId": "3p-weekly", "resetTime": future},
    ]}]}}
    buckets = parse_quota_summary(payload)
    assert set(buckets) == {"gemini_5h", "gemini_weekly", "argon_weekly"}

    with tempfile.TemporaryDirectory() as tmp:
        engine = QuotaMonitorEngine(data_dir=Path(tmp), brain_dirs=[])
        engine.state_file.write_text(json.dumps({
            "calibrations": {"claude_gpt_weekly": {"remainingPct": 72, "calibratedAt": now.isoformat()}},
            "preferences": {"unitMode": "exact"},
        }), encoding="utf-8")
        engine.external_quota_file.write_text(json.dumps({
            "updatedAt": now.isoformat(),
            "buckets": {
                "gemini_5h": {"remainingPct": 95},
                "claude_gpt_5h": {"remainingPct": 81},
                "claude_gpt_weekly": {"remainingPct": 34, "resetsAt": expired},
            },
        }), encoding="utf-8")

        with patch.object(engine.live_quota, "read", return_value=buckets):
            dashboard = engine.build_dashboard()
        quotas = dashboard["quotas"]
        by_id = {bucket["id"]: bucket["quota"] for bucket in quotas["buckets"]}
        assert dashboard["preferences"]["unitMode"] == "exact"
        assert quotas["source"] == "antigravity"
        assert by_id["gemini_5h"]["source"] == "antigravity"
        assert by_id["gemini_5h"]["remainingPct"] == 0
        assert by_id["gemini_5h"]["resetsInSec"] > 0
        assert by_id["gemini_weekly"]["remainingPct"] == 44
        assert by_id["gemini_weekly"]["resetsInSec"] is None
        assert by_id["argon_weekly"]["remainingPct"] == 50
        assert by_id["claude_gpt_5h"]["source"] == "external"
        assert by_id["claude_gpt_5h"]["remainingPct"] == 81
        assert by_id["claude_gpt_weekly"]["source"] == "unavailable"
        assert by_id["claude_gpt_weekly"]["remainingPct"] is None
    print("[PASS] 本机额度解析、实时来源优先和逐项回退")


def test_manual_sync_bypasses_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = QuotaMonitorEngine(data_dir=Path(tmp), brain_dirs=[])
        server = McpQuotaServer(engine)
        snapshots = [
            {"gemini_5h": {"remainingPct": 90}},
            {"gemini_5h": {"remainingPct": 68}},
            {},
        ]
        mock_read = patch.object(engine.live_quota, "_read_uncached", side_effect=snapshots)
        with patch("live_quota.time.monotonic", return_value=100), mock_read as read:
            for args, expected in [
                ({}, 90),
                ({}, 90),
                ({"forceQuotaRefresh": True}, 68),
                ({}, 68),
                ({"forceQuotaRefresh": True}, None),
            ]:
                reply = server.handle_request({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "app_get_dashboard", "arguments": args},
                })
                buckets = reply["result"]["structuredContent"]["quotas"]["buckets"]
                first = next(bucket for bucket in buckets if bucket["id"] == "gemini_5h")
                assert first["quota"]["remainingPct"] == expected
            assert len(read.call_args_list) == 3
            assert all(call.args == (True,) for call in read.call_args_list)
    print("[PASS] 手动同步跳过额度缓存，失败时返回不可用状态")


def test_mcp_stdio_protocol() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        brain = tmp_path / "brain"
        data_dir = tmp_path / "data"
        tr_path = brain / "conv-mcp-1" / ".system_generated" / "logs" / "transcript.jsonl"
        make_sample_transcript(tr_path, dt.datetime.now(dt.timezone.utc))

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://quota-monitor/app.html"}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "open_monitor", "arguments": {}}},
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "app_get_dashboard",
                    "arguments": {"forceQuotaRefresh": True},
                },
            },
        ]
        stdin_blob = "\n".join(json.dumps(r) for r in requests) + "\n"
        env = dict(os.environ, HOME=tmp, USERPROFILE=tmp)
        env.pop("ANTIGRAVITY_LS_ADDRESS", None)
        env.pop("ANTIGRAVITY_CSRF_TOKEN", None)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "server" / "mcp_server.py"),
                "--data-dir",
                str(data_dir),
                "--brain-dir",
                str(brain),
            ],
            input=stdin_blob,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        assert len(responses) == 6, f"期望 6 条响应，实际 {len(responses)}"

        # 校验 initialize
        assert responses[0]["result"]["capabilities"]["resources"] is not None

        # 校验 tools/list 元数据
        tools = {t["name"]: t for t in responses[1]["result"]["tools"]}
        open_meta = tools["open_monitor"]["_meta"]
        assert open_meta["ui"]["resourceUri"] == "ui://quota-monitor/app.html"
        ep_types = [e["type"] for e in open_meta["com.google.antigravity/ui"]["entrypoints"]]
        assert "aux_pane" in ep_types and "global" in ep_types
        assert tools["app_get_dashboard"]["_meta"]["ui"]["visibility"] == ["app"]
        assert tools["app_check_update"]["_meta"]["ui"]["visibility"] == ["app"]
        assert tools["app_apply_update"]["_meta"]["ui"]["visibility"] == ["app"]
        assert tools["app_get_dashboard"]["inputSchema"]["properties"]["forceQuotaRefresh"]["type"] == "boolean"
        assert set(tools) == {
            "open_monitor",
            "app_get_dashboard",
            "app_update_preferences",
            "app_check_update",
            "app_apply_update",
        }

        # 校验 resources/read 返回自包含 HTML 且小于 10 MiB
        contents = responses[3]["result"]["contents"]
        assert contents[0]["mimeType"] == "text/html;profile=mcp-app"
        html_text = contents[0]["text"]
        assert "<!DOCTYPE html>" in html_text and "ui/initialize" in html_text
        assert len(html_text.encode("utf-8")) < 10 * 1024 * 1024

        # 校验打开面板和手动同步在桌面端不可用时仍返回结构化数据
        open_res = responses[4]["result"]
        assert open_res["structuredContent"]["session"]["context"]["usedTokens"] == 25000
        synced = responses[5]["result"]["structuredContent"]
        assert synced["quotas"]["source"] == "unavailable"
        assert all(b["quota"]["remainingPct"] is None for b in synced["quotas"]["buckets"])
        print("[PASS] MCP stdio JSON-RPC 2.0 与 MCP Apps 协议端到端测试")


def test_updater_safe_apply() -> None:
    """测试版本比较、域名白名单、Zip Slip 防护、备份与前后端变更判定。"""
    import updater  # noqa: E402

    assert updater.is_newer_version("v0.2.0", "0.1.1") is True
    assert updater.is_newer_version("0.1.1", "0.1.1") is False
    assert updater.is_newer_version("0.1.0", "0.2.0") is False

    # 非白名单域名必须被拒绝
    bad_res = updater.apply_update(ROOT, Path(tempfile.gettempdir()), "https://evil.example.com/a.zip")
    assert bad_res["ok"] is False

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugin_dir = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        (plugin_dir / "server").mkdir(parents=True)
        (plugin_dir / "ui").mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "antigravity-quota-monitor", "version": "0.1.1"}), encoding="utf-8"
        )
        (plugin_dir / "server" / "mcp_server.py").write_text("# v1 backend\n", encoding="utf-8")
        (plugin_dir / "ui" / "app.html").write_text("<html>v1</html>", encoding="utf-8")

        # 1. 仅前端更新包（server/*.py 哈希不变 -> backendChanged 应为 False）
        zip_fe = tmp_path / "fe_update.zip"
        with zipfile.ZipFile(zip_fe, "w") as zf:
            zf.writestr(
                "repo-v0.2.0/plugin.json",
                json.dumps({"name": "antigravity-quota-monitor", "version": "0.2.0"}),
            )
            zf.writestr("repo-v0.2.0/server/mcp_server.py", "# v1 backend\n")
            zf.writestr("repo-v0.2.0/ui/app.html", "<html>v2 frontend</html>")

        res_fe = updater.apply_update_from_zip(plugin_dir, data_dir, zip_fe)
        assert res_fe["ok"] is True
        assert res_fe["updatedVersion"] == "0.2.0"
        assert res_fe["backendChanged"] is False
        assert (plugin_dir / "ui" / "app.html").read_text(encoding="utf-8") == "<html>v2 frontend</html>"
        assert Path(res_fe["backupDir"]).exists()

        # 2. 后端更新包（server/*.py 变化 -> backendChanged 应为 True）
        zip_be = tmp_path / "be_update.zip"
        with zipfile.ZipFile(zip_be, "w") as zf:
            zf.writestr(
                "repo-v0.2.1/plugin.json",
                json.dumps({"name": "antigravity-quota-monitor", "version": "0.2.1"}),
            )
            zf.writestr("repo-v0.2.1/server/mcp_server.py", "# v2 backend\n")
            zf.writestr("repo-v0.2.1/ui/app.html", "<html>v2 frontend</html>")

        res_be = updater.apply_update_from_zip(plugin_dir, data_dir, zip_be)
        assert res_be["ok"] is True
        assert res_be["backendChanged"] is True
        assert (plugin_dir / "server" / "mcp_server.py").read_text(encoding="utf-8") == "# v2 backend\n"

        # 3. Zip Slip 路径穿越必须被拦截
        zip_bad = tmp_path / "zip_slip.zip"
        with zipfile.ZipFile(zip_bad, "w") as zf:
            zf.writestr("../escape.txt", "bad")
        try:
            updater.apply_update_from_zip(plugin_dir, data_dir, zip_bad)
            raise AssertionError("应当拦截 Zip Slip 路径穿越")
        except ValueError:
            pass

    print("[PASS] 插件安全更新校验、备份与前后端热更新判定")


if __name__ == "__main__":
    test_classification()
    test_engine_and_quota_window()
    test_hook_tracker()
    test_live_quota_precedence()
    test_manual_sync_bypasses_cache()
    test_mcp_stdio_protocol()
    test_updater_safe_apply()
    print("\n全部测试通过！")

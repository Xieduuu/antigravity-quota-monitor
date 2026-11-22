# -*- coding: utf-8 -*-
"""
Antigravity 生命周期 Hook 追踪脚本。

由 hooks.json 在 SessionStart / PostInvocation / PostToolUse / Stop 事件触发。
仅记录当前活跃会话的 conversationId、transcriptPath 与 modelName 到
${PLUGIN_DATA}/active_session.json，随后立即输出 {} 退出，零阻塞且不向模型注入内容。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    if isinstance(payload, dict) and payload.get("conversationId"):
        data_dir_env = os.environ.get("PLUGIN_DATA")
        if data_dir_env:
            data_dir = Path(data_dir_env)
        else:
            data_dir = Path(__file__).resolve().parent.parent / ".local_data"

        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            target = data_dir / "active_session.json"
            record = {
                "conversationId": payload.get("conversationId"),
                "transcriptPath": payload.get("transcriptPath"),
                "modelName": payload.get("modelName"),
                "workspacePaths": payload.get("workspacePaths") or [],
                "lastEvent": payload.get("hookEventName"),
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            tmp = target.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except Exception:
            pass

    sys.stdout.write("{}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
Antigravity 额度与上下文监控插件 — 核心数据解析与额度引擎。

功能职责：
1. 扫描本机 Antigravity 会话日志（transcript.jsonl / transcript_full.jsonl），
   通过文件 (mtime, size) 内存缓存实现毫秒级增量刷新。
2. 计算当前会话的精确上下文占用（input_tokens + cache_read_tokens + output_tokens）、
   精确缓存命中率、模型/工具耗时，以及标注为估算值的上下文构成拆分。
3. 汇总全局今日 / 本月 / 累计 Token 用量与当月每日热力日历。
4. 对齐 Antigravity 原生 View Usage 的三大额度池（Gemini Models、Claude & GPT、
   Gemini 4 Argon），读取桌面端余量及重置时间。
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__:
    from .live_quota import LiveQuotaSource
else:
    from live_quota import LiveQuotaSource


# 三大额度池的 5 个标准桶定义（与 Antigravity 原生 View Usage 完全对齐）
QUOTA_BUCKETS: List[Dict[str, Any]] = [
    {
        "id": "gemini_5h",
        "group": "gemini",
        "groupLabel": "Gemini Models",
        "label": "5h 限额",
        "shortLabel": "5h",
        "windowHours": 5,
        "primary": True,
    },
    {
        "id": "gemini_weekly",
        "group": "gemini",
        "groupLabel": "Gemini Models",
        "label": "周限额",
        "shortLabel": "周额度",
        "windowHours": 24 * 7,
        "primary": True,
    },
    {
        "id": "claude_gpt_5h",
        "group": "claude_gpt",
        "groupLabel": "Claude & GPT",
        "label": "5h 限额",
        "shortLabel": "5h",
        "windowHours": 5,
        "primary": False,
    },
    {
        "id": "claude_gpt_weekly",
        "group": "claude_gpt",
        "groupLabel": "Claude & GPT",
        "label": "周限额",
        "shortLabel": "周额度",
        "windowHours": 24 * 7,
        "primary": False,
    },
    {
        "id": "argon_weekly",
        "group": "argon",
        "groupLabel": "Gemini 4 Argon",
        "label": "周限额",
        "shortLabel": "周额度",
        "windowHours": 24 * 7,
        "primary": False,
    },
]

# 单步耗时上限（秒），用于过滤用户长时间离开键盘造成的空窗间隔
MAX_STEP_GAP_SEC = 120.0


def utc_now() -> dt.datetime:
    """返回当前 UTC 带时区时间对象。"""
    return dt.datetime.now(dt.timezone.utc)


def parse_iso_utc(raw: Optional[str]) -> Optional[dt.datetime]:
    """解析 ISO-8601 时间字符串并归一化为 UTC datetime。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def classify_model_group(model_name: Optional[str]) -> str:
    """
    将模型名称归类到与 Antigravity 原生 View Usage 一致的三大额度池：
    - argon: Gemini 4 Argon 独立周额度池
    - claude_gpt: Claude 与 GPT 系列共享额度池
    - gemini: Gemini 系列（Neon / Hydrogen / Carbon / Flash / Pro 等）
    - other: 其他未识别模型
    """
    if not model_name:
        return "other"
    low = model_name.lower()
    if "argon" in low:
        return "argon"
    if any(k in low for k in ("claude", "sonnet", "opus", "haiku", "gpt", "openai", "o1-", "o3-", "o4-")):
        return "claude_gpt"
    if any(k in low for k in ("gemini", "neon", "hydrogen", "carbon", "flash", "pro", "orchestrator")):
        return "gemini"
    return "other"


def format_model_display_name(model_name: Optional[str]) -> str:
    """将内部模型标识符美化为易读展示名称。"""
    if not model_name:
        return "未知模型"
    if " " in model_name and "-" not in model_name:
        return model_name
    tokens = re.split(r"[-_]+", model_name.strip())
    pretty: List[str] = []
    for tok in tokens:
        low = tok.lower()
        if low == "gpt":
            pretty.append("GPT")
        elif low == "oss":
            pretty.append("OSS")
        elif low in ("120b", "70b", "32b", "1m", "200k"):
            pretty.append(tok.upper())
        elif re.match(r"^\d+(\.\d+)?$", tok):
            pretty.append(tok)
        else:
            pretty.append(tok.capitalize())
    return " ".join(pretty)


def get_default_context_limit(model_name: Optional[str], custom_limits: Optional[Dict[str, int]] = None) -> int:
    """根据模型名与用户自定义映射返回上下文窗口上限（Token 数）。"""
    if model_name and custom_limits:
        if model_name in custom_limits and int(custom_limits[model_name]) > 0:
            return int(custom_limits[model_name])
    if not model_name:
        return 1_000_000
    low = model_name.lower()
    if "1m" in low:
        return 1_000_000
    if any(k in low for k in ("gemini", "argon", "neon", "hydrogen", "carbon", "orchestrator")):
        return 1_000_000
    if any(k in low for k in ("claude", "sonnet", "opus", "haiku")):
        return 200_000
    if "gpt-oss" in low or "gpt-4o" in low:
        return 128_000
    if "gpt-5" in low or "gpt-4.1" in low:
        return 400_000
    return 200_000


def estimate_text_tokens(text: Any) -> int:
    """
    轻量级中英混合文本 Token 估算函数（仅用于无法从日志直接拆分的占比近似）。
    中文字符约 0.72 tok/字，ASCII 约 3.8 字符/tok。
    """
    if not text:
        return 0
    s = str(text)
    if not s:
        return 0
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    ascii_chars = len(s) - non_ascii
    return max(1, int(non_ascii * 0.72 + ascii_chars / 3.8))


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """原子写入 JSON 文件，避免并发读写导致配置损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def safe_read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    """安全读取 JSON 文件，不存在或损坏时返回默认值。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return dict(default)


@dataclass
class StepUsageEvent:
    """单次模型调用（PLANNER_RESPONSE）的精确用量事件。"""
    timestamp: dt.datetime
    model: str
    group: str
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    conversation_id: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.output_tokens


@dataclass
class ParsedConversation:
    """单个会话解析后的完整摘要与事件缓存。"""
    conversation_id: str
    transcript_path: str
    mtime_ns: int
    file_size: int
    title: str
    latest_model: str
    models_used: List[str]
    first_ts: Optional[dt.datetime]
    last_ts: Optional[dt.datetime]
    # 最新一步上下文（精确）
    context_tokens: int
    latest_input_tokens: int
    latest_cache_read_tokens: int
    latest_output_tokens: int
    # 上下文拆分（估算）
    est_system_tools_tokens: int
    est_dialogue_tokens: int
    est_tool_context_tokens: int
    # 全会话累计（精确）
    sum_uncached_input: int
    sum_cache_read: int
    sum_output: int
    turn_count: int
    planner_calls: int
    tool_call_count: int
    model_duration_sec: float
    tool_duration_sec: float
    # 分子分母严格 1:1 配对后的输出统计（仅统计紧接系统/工具步骤的模型回复，排除人工等待与末轮空窗）
    timed_output_tokens: int = 0
    timed_model_sec: float = 0.0
    events: List[StepUsageEvent] = field(default_factory=list)


class QuotaMonitorEngine:
    """本地日志扫描、额度同步与偏好设置。"""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        brain_dirs: Optional[List[Path]] = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else self._default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.active_session_file = self.data_dir / "active_session.json"
        self.external_quota_file = self.data_dir / "external_quota.json"
        self.live_quota = LiveQuotaSource()
        self.brain_dirs = brain_dirs if brain_dirs is not None else self._discover_brain_dirs()
        self._cache: Dict[str, ParsedConversation] = {}

    @staticmethod
    def _default_data_dir() -> Path:
        env_dir = os.environ.get("PLUGIN_DATA")
        if env_dir:
            return Path(env_dir)
        return Path(__file__).resolve().parent.parent / ".local_data"

    @staticmethod
    def _discover_brain_dirs() -> List[Path]:
        env_brain = os.environ.get("ANTIGRAVITY_BRAIN_DIR")
        if env_brain:
            return [Path(p.strip()) for p in env_brain.split(os.pathsep) if p.strip()]
        home = Path.home()
        candidates = [
            home / ".gemini" / "antigravity" / "brain",
            home / ".gemini" / "antigravity-ide" / "brain",
        ]
        return [p for p in candidates if p.exists()]

    def load_state(self) -> Dict[str, Any]:
        """加载持久化用户配置。"""
        default_state: Dict[str, Any] = {
            "preferences": {
                "unitMode": "compact",
                "followActiveSession": True,
                "autoRefreshSec": 15,
                "customContextLimits": {},
            },
        }
        state = safe_read_json(self.state_file, default_state)
        state.pop("calibrations", None)
        prefs = state.setdefault("preferences", {})
        prefs.setdefault("unitMode", "compact")
        prefs.setdefault("followActiveSession", True)
        prefs.setdefault("autoRefreshSec", 15)
        prefs.setdefault("customContextLimits", {})
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        """持久化保存状态文件。"""
        atomic_write_json(self.state_file, state)

    def load_active_session_hint(self) -> Dict[str, Any]:
        """读取由 hooks.json 实时写入的当前活跃会话提示信息。"""
        return safe_read_json(self.active_session_file, {})

    def load_external_quota(self) -> Dict[str, Any]:
        """
        读取可选的外部标准额度文件（${PLUGIN_DATA}/external_quota.json）。
        格式示例：
        {
          "updatedAt": "2026-11-22T06:30:00Z",
          "buckets": {
            "gemini_5h": {"remainingPct": 88, "resetsAt": "2026-11-22T10:48:00Z"}
          }
        }
        """
        data = safe_read_json(self.external_quota_file, {})
        if not isinstance(data.get("buckets"), dict):
            return {}
        return data

    def _load_official_titles(self) -> Dict[str, str]:
        """
        以只读方式从 Antigravity 本地 conversation_summaries.db 读取官方对话名称。
        若数据库不存在或被锁定则优雅降级，绝不阻塞主流程。
        """
        titles: Dict[str, str] = {}
        db_candidates: List[Path] = []
        for brain_dir in self.brain_dirs:
            cand = brain_dir.parent / "conversation_summaries.db"
            if cand not in db_candidates:
                db_candidates.append(cand)

        for db_path in db_candidates:
            try:
                if not db_path.exists():
                    continue
            except OSError:
                continue

            conn: Optional[sqlite3.Connection] = None
            try:
                # 优先普通只读打开（可读 WAL 最新写入）；若遇权限或锁冲突再回退 immutable 只读
                uri_ro = f"file:{db_path.as_posix()}?mode=ro"
                try:
                    conn = sqlite3.connect(uri_ro, uri=True, timeout=0.8)
                    rows = conn.execute(
                        "SELECT conversation_id, title FROM conversation_summaries WHERE title != ''"
                    ).fetchall()
                except sqlite3.Error:
                    if conn is not None:
                        conn.close()
                        conn = None
                    uri_imm = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
                    conn = sqlite3.connect(uri_imm, uri=True, timeout=0.8)
                    rows = conn.execute(
                        "SELECT conversation_id, title FROM conversation_summaries WHERE title != ''"
                    ).fetchall()

                for cid, title in rows:
                    if cid and title:
                        titles[str(cid)] = str(title).strip()
            except Exception:
                continue
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return titles

    def _find_transcript_files(self) -> List[Tuple[str, Path]]:
        """遍历所有可访问的 brain 目录，收集 (conversation_id, transcript_path)。"""
        found: Dict[str, Path] = {}

        # 1. 优先把 active_session_file 指向的当前会话纳入
        hint = self.load_active_session_hint()
        hint_path = hint.get("transcriptPath")
        hint_cid = hint.get("conversationId")
        if hint_path and isinstance(hint_path, str):
            p = Path(hint_path)
            try:
                if p.exists():
                    cid = str(hint_cid or p.parent.parent.parent.name)
                    found[cid] = p
            except OSError:
                pass

        # 2. 扫描标准 brain 目录下的所有会话日志
        for brain_dir in self.brain_dirs:
            try:
                if not brain_dir.exists():
                    continue
                for child in brain_dir.iterdir():
                    try:
                        if not child.is_dir():
                            continue
                        logs_dir = child / ".system_generated" / "logs"
                        compact = logs_dir / "transcript.jsonl"
                        full = logs_dir / "transcript_full.jsonl"
                        target: Optional[Path] = None
                        if compact.exists():
                            target = compact
                        elif full.exists():
                            target = full
                        if target is not None:
                            found.setdefault(child.name, target)
                    except OSError:
                        continue
            except OSError:
                continue

        return list(found.items())

    def _parse_transcript(self, conversation_id: str, path: Path) -> Optional[ParsedConversation]:
        """解析单个 transcript.jsonl，命中 mtime/size 缓存时直接复用。"""
        try:
            st = path.stat()
        except OSError:
            return None

        cache_key = str(path)
        cached = self._cache.get(cache_key)
        if cached and cached.mtime_ns == st.st_mtime_ns and cached.file_size == st.st_size:
            return cached

        title = ""
        latest_model = ""
        models_seen: List[str] = []
        first_ts: Optional[dt.datetime] = None
        last_ts: Optional[dt.datetime] = None
        prev_ts: Optional[dt.datetime] = None
        prev_stype: str = ""
        prev_output_tokens: int = 0

        latest_in = 0
        latest_cache = 0
        latest_out = 0
        first_planner_in = 0
        first_user_est = 0

        sum_in = 0
        sum_cache = 0
        sum_out = 0
        turn_count = 0
        planner_calls = 0
        tool_call_count = 0
        model_dur = 0.0
        tool_dur = 0.0
        timed_output_tokens = 0
        timed_model_sec = 0.0
        dialogue_est = 0
        events: List[StepUsageEvent] = []

        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        step = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(step, dict):
                        continue

                    stype = str(step.get("type") or "")
                    ts = parse_iso_utc(step.get("created_at"))
                    cur_out_tok = int(step.get("output_tokens") or 0) if stype == "PLANNER_RESPONSE" else 0
                    if ts is not None:
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                        if prev_ts is not None:
                            gap = (ts - prev_ts).total_seconds()
                            # created_at 记录步骤开始时间，因此时间差归属于前一步骤
                            if 0.0 <= gap <= MAX_STEP_GAP_SEC:
                                if prev_stype == "PLANNER_RESPONSE":
                                    model_dur += gap
                                    # 仅统计“模型回复后立即进入系统/工具步骤”的严格配对样本，排除人工思考间隔与整秒 0s 舍入误差
                                    if stype != "USER_INPUT" and prev_output_tokens > 0 and gap >= 1.0:
                                        timed_output_tokens += prev_output_tokens
                                        timed_model_sec += gap
                                elif prev_stype not in ("", "USER_INPUT"):
                                    tool_dur += gap
                        prev_ts = ts
                        prev_stype = stype
                        prev_output_tokens = cur_out_tok

                    if stype == "USER_INPUT":
                        turn_count += 1
                        content = str(step.get("content") or "")
                        # 清理 Antigravity 系统包裹标签，提取纯净用户输入
                        cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.S)
                        cleaned = re.sub(r"</?USER_REQUEST>", "", cleaned)
                        cleaned = re.sub(r"\s+", " ", cleaned).strip()
                        est_u = estimate_text_tokens(cleaned or content)
                        dialogue_est += est_u
                        if turn_count == 1:
                            first_user_est = est_u
                            if cleaned:
                                title = cleaned[:42] + ("…" if len(cleaned) > 42 else "")

                    elif stype == "PLANNER_RESPONSE":
                        planner_calls += 1
                        model = str(step.get("model") or latest_model or "")
                        if model:
                            latest_model = model
                            if model not in models_seen:
                                models_seen.append(model)

                        in_tok = int(step.get("input_tokens") or 0)
                        cache_tok = int(step.get("cache_read_tokens") or 0)
                        out_tok = int(step.get("output_tokens") or 0)

                        if in_tok > 0 or cache_tok > 0 or out_tok > 0:
                            if first_planner_in == 0:
                                first_planner_in = in_tok + cache_tok
                            latest_in = in_tok
                            latest_cache = cache_tok
                            latest_out = out_tok
                            sum_in += in_tok
                            sum_cache += cache_tok
                            sum_out += out_tok
                            if ts is not None:
                                events.append(
                                    StepUsageEvent(
                                        timestamp=ts,
                                        model=model or "unknown",
                                        group=classify_model_group(model),
                                        input_tokens=in_tok,
                                        cache_read_tokens=cache_tok,
                                        output_tokens=out_tok,
                                        conversation_id=conversation_id,
                                    )
                                )

                        tool_calls = step.get("tool_calls")
                        if isinstance(tool_calls, list):
                            tool_call_count += len(tool_calls)

                        dialogue_est += estimate_text_tokens(step.get("content"))
                        dialogue_est += estimate_text_tokens(step.get("thinking"))

        except OSError:
            return None

        context_tokens = latest_in + latest_cache + latest_out
        if context_tokens > 0:
            raw_sys = max(0, first_planner_in - first_user_est)
            est_sys = min(raw_sys, context_tokens)
            rem_after_sys = max(0, context_tokens - est_sys)
            est_dial = min(dialogue_est, rem_after_sys)
            est_tools = max(0, context_tokens - est_sys - est_dial)
        else:
            est_sys = 0
            est_dial = 0
            est_tools = 0

        parsed = ParsedConversation(
            conversation_id=conversation_id,
            transcript_path=str(path),
            mtime_ns=st.st_mtime_ns,
            file_size=st.st_size,
            title=title or f"会话 {conversation_id[:8]}",
            latest_model=latest_model or "unknown",
            models_used=models_seen,
            first_ts=first_ts,
            last_ts=last_ts,
            context_tokens=context_tokens,
            latest_input_tokens=latest_in,
            latest_cache_read_tokens=latest_cache,
            latest_output_tokens=latest_out,
            est_system_tools_tokens=est_sys,
            est_dialogue_tokens=est_dial,
            est_tool_context_tokens=est_tools,
            sum_uncached_input=sum_in,
            sum_cache_read=sum_cache,
            sum_output=sum_out,
            turn_count=turn_count,
            planner_calls=planner_calls,
            tool_call_count=tool_call_count,
            model_duration_sec=round(model_dur, 1),
            tool_duration_sec=round(tool_dur, 1),
            timed_output_tokens=timed_output_tokens,
            timed_model_sec=round(timed_model_sec, 1),
            events=events,
        )
        self._cache[cache_key] = parsed
        return parsed

    def scan_all_conversations(self) -> List[ParsedConversation]:
        """扫描并解析全部可见会话，合并官方对话名称后按最后更新时间倒序排列。"""
        official_titles = self._load_official_titles()
        results: List[ParsedConversation] = []
        for cid, path in self._find_transcript_files():
            parsed = self._parse_transcript(cid, path)
            if parsed is not None:
                if cid in official_titles and official_titles[cid]:
                    parsed.title = official_titles[cid]
                results.append(parsed)
        results.sort(
            key=lambda c: c.last_ts.timestamp() if c.last_ts else 0.0,
            reverse=True,
        )
        return results

    def _collect_all_events(self, conversations: List[ParsedConversation]) -> List[StepUsageEvent]:
        events: List[StepUsageEvent] = []
        for conv in conversations:
            events.extend(conv.events)
        events.sort(key=lambda e: e.timestamp)
        return events

    def _compute_bucket_metrics(
        self,
        bucket_def: Dict[str, Any],
        events: List[StepUsageEvent],
        live_entry: Any,
        external_entry: Any,
        now: dt.datetime,
    ) -> Dict[str, Any]:
        """计算单个额度桶的本地用量，并按可用来源显示余量。"""
        group = bucket_def["group"]
        window_hours = bucket_def["windowHours"]
        window_start = now - dt.timedelta(hours=window_hours)

        window_events = [e for e in events if e.group == group and e.timestamp >= window_start]

        win_in = sum(e.input_tokens for e in window_events)
        win_cache = sum(e.cache_read_tokens for e in window_events)
        win_out = sum(e.output_tokens for e in window_events)
        quota = {
            "source": "unavailable",
            "remainingPct": None,
            "resetsAt": None,
            "resetsInSec": None,
        }
        for source_name, entry in (("antigravity", live_entry), ("external", external_entry)):
            if not isinstance(entry, dict) or isinstance(entry.get("remainingPct"), bool):
                continue
            try:
                pct = float(entry.get("remainingPct"))
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(pct):
                continue
            reset_at = parse_iso_utc(entry.get("resetsAt"))
            if source_name == "external" and reset_at is not None and reset_at <= now:
                continue
            resets_in_sec = int((reset_at - now).total_seconds()) if reset_at is not None and reset_at > now else None
            quota = {
                "source": source_name,
                "remainingPct": round(max(0.0, min(100.0, pct)), 1),
                "resetsAt": reset_at.isoformat() if reset_at is not None else None,
                "resetsInSec": resets_in_sec,
            }
            break

        return {
            "id": bucket_def["id"],
            "group": group,
            "groupLabel": bucket_def["groupLabel"],
            "label": bucket_def["label"],
            "shortLabel": bucket_def["shortLabel"],
            "windowHours": window_hours,
            "primary": bucket_def["primary"],
            "windowUsage": {
                "totalTokens": win_in + win_cache + win_out,
                "uncachedInput": win_in,
                "cacheRead": win_cache,
                "output": win_out,
                "calls": len(window_events),
            },
            "quota": quota,
        }

    def update_preferences(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """更新并保存用户界面偏好设置。"""
        state = self.load_state()
        prefs = state.setdefault("preferences", {})
        if "unitMode" in updates and updates["unitMode"] in ("compact", "exact"):
            prefs["unitMode"] = updates["unitMode"]
        if "followActiveSession" in updates:
            prefs["followActiveSession"] = bool(updates["followActiveSession"])
        if "autoRefreshSec" in updates:
            sec = int(updates["autoRefreshSec"])
            prefs["autoRefreshSec"] = max(5, min(300, sec))
        if "customContextLimits" in updates and isinstance(updates["customContextLimits"], dict):
            cleaned: Dict[str, int] = {}
            for k, v in updates["customContextLimits"].items():
                try:
                    iv = int(v)
                    if iv >= 8192:
                        cleaned[str(k)] = iv
                except (TypeError, ValueError):
                    continue
            prefs["customContextLimits"] = cleaned
        self.save_state(state)
        return prefs

    def build_dashboard(
        self,
        conversation_id: Optional[str] = None,
        year: Optional[int] = None,
        month: Optional[int] = None,
        *,
        force_quota_refresh: bool = False,
    ) -> Dict[str, Any]:
        """构建前端面板所需的完整实时仪表盘快照。"""
        now_utc = utc_now()
        local_tz = dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
        now_local = now_utc.astimezone(local_tz)

        target_year = int(year) if year else now_local.year
        target_month = int(month) if month else now_local.month
        target_month = max(1, min(12, target_month))

        state = self.load_state()
        prefs = state["preferences"]
        custom_limits = prefs.get("customContextLimits") or {}

        conversations = self.scan_all_conversations()
        all_events = self._collect_all_events(conversations)
        active_hint = self.load_active_session_hint()

        selected_conv: Optional[ParsedConversation] = None
        if conversation_id:
            selected_conv = next((c for c in conversations if c.conversation_id == conversation_id), None)
        if selected_conv is None and prefs.get("followActiveSession", True):
            hint_cid = active_hint.get("conversationId")
            if hint_cid:
                selected_conv = next((c for c in conversations if c.conversation_id == hint_cid), None)
        if selected_conv is None and conversations:
            selected_conv = conversations[0]

        session_payload: Optional[Dict[str, Any]] = None
        if selected_conv is not None:
            ctx_limit = get_default_context_limit(selected_conv.latest_model, custom_limits)
            ctx_pct = min(100.0, (selected_conv.context_tokens / ctx_limit * 100.0) if ctx_limit > 0 else 0.0)
            total_in_all = selected_conv.sum_uncached_input + selected_conv.sum_cache_read
            cache_hit_pct = (selected_conv.sum_cache_read / total_in_all * 100.0) if total_in_all > 0 else 0.0
            avg_tps = (
                selected_conv.timed_output_tokens / selected_conv.timed_model_sec
                if selected_conv.timed_model_sec >= 2.0
                else 0.0
            )
            session_payload = {
                "conversationId": selected_conv.conversation_id,
                "title": selected_conv.title,
                "model": selected_conv.latest_model,
                "modelDisplay": format_model_display_name(selected_conv.latest_model),
                "modelGroup": classify_model_group(selected_conv.latest_model),
                "lastUpdated": selected_conv.last_ts.isoformat() if selected_conv.last_ts else None,
                "context": {
                    "usedTokens": selected_conv.context_tokens,
                    "limitTokens": ctx_limit,
                    "usedPct": round(ctx_pct, 1),
                    "latestInput": selected_conv.latest_input_tokens,
                    "latestCacheRead": selected_conv.latest_cache_read_tokens,
                    "latestOutput": selected_conv.latest_output_tokens,
                    "breakdownEstimated": True,
                    "breakdown": {
                        "systemAndTools": selected_conv.est_system_tools_tokens,
                        "dialogue": selected_conv.est_dialogue_tokens,
                        "toolOutputs": selected_conv.est_tool_context_tokens,
                    },
                },
                "stats": {
                    "modelDurationSec": selected_conv.model_duration_sec,
                    "toolDurationSec": selected_conv.tool_duration_sec,
                    "turnCount": selected_conv.turn_count,
                    "plannerCalls": selected_conv.planner_calls,
                    "toolCallCount": selected_conv.tool_call_count,
                    "avgOutputTps": round(avg_tps, 1),
                },
                "tokens": {
                    "uncachedInput": selected_conv.sum_uncached_input,
                    "cacheRead": selected_conv.sum_cache_read,
                    "output": selected_conv.sum_output,
                    "total": selected_conv.sum_uncached_input + selected_conv.sum_cache_read + selected_conv.sum_output,
                    "cacheHitPct": round(cache_hit_pct, 1),
                },
            }

        today_date = now_local.date()
        today_in = today_cache = today_out = today_calls = 0
        month_total = 0
        all_total = 0
        all_in = all_cache = all_out = 0

        _, days_in_month = calendar.monthrange(target_year, target_month)
        daily_map: Dict[int, Dict[str, int]] = {
            d: {"day": d, "total": 0, "uncachedInput": 0, "cacheRead": 0, "output": 0, "calls": 0}
            for d in range(1, days_in_month + 1)
        }
        model_totals: Dict[str, Dict[str, Any]] = {}

        for ev in all_events:
            ev_local = ev.timestamp.astimezone(local_tz)
            ev_tot = ev.total_tokens
            all_total += ev_tot
            all_in += ev.input_tokens
            all_cache += ev.cache_read_tokens
            all_out += ev.output_tokens

            if ev_local.date() == today_date:
                today_in += ev.input_tokens
                today_cache += ev.cache_read_tokens
                today_out += ev.output_tokens
                today_calls += 1

            if ev_local.year == now_local.year and ev_local.month == now_local.month:
                month_total += ev_tot

            if ev_local.year == target_year and ev_local.month == target_month:
                slot = daily_map[ev_local.day]
                slot["total"] += ev_tot
                slot["uncachedInput"] += ev.input_tokens
                slot["cacheRead"] += ev.cache_read_tokens
                slot["output"] += ev.output_tokens
                slot["calls"] += 1

            m_entry = model_totals.setdefault(
                ev.model,
                {
                    "model": ev.model,
                    "displayName": format_model_display_name(ev.model),
                    "group": ev.group,
                    "totalTokens": 0,
                    "uncachedInput": 0,
                    "cacheRead": 0,
                    "output": 0,
                    "calls": 0,
                },
            )
            m_entry["totalTokens"] += ev_tot
            m_entry["uncachedInput"] += ev.input_tokens
            m_entry["cacheRead"] += ev.cache_read_tokens
            m_entry["output"] += ev.output_tokens
            m_entry["calls"] += 1

        today_total = today_in + today_cache + today_out
        today_in_denom = today_in + today_cache
        today_cache_hit = round((today_cache / today_in_denom * 100.0), 1) if today_in_denom > 0 else None

        max_day_tok = max((d["total"] for d in daily_map.values()), default=0)
        calendar_days: List[Dict[str, Any]] = []
        for day_num in range(1, days_in_month + 1):
            item = daily_map[day_num]
            if item["total"] <= 0 or max_day_tok <= 0:
                level = 0
            else:
                ratio = item["total"] / max_day_tok
                if ratio < 0.2:
                    level = 1
                elif ratio < 0.45:
                    level = 2
                elif ratio < 0.75:
                    level = 3
                else:
                    level = 4
            calendar_days.append(
                {
                    **item,
                    "level": level,
                    "isToday": (
                        target_year == today_date.year
                        and target_month == today_date.month
                        and day_num == today_date.day
                    ),
                }
            )

        first_weekday = dt.date(target_year, target_month, 1).weekday()

        live_buckets = self.live_quota.read(force_refresh=force_quota_refresh)
        external_buckets = self.load_external_quota().get("buckets", {})
        quota_now = utc_now()
        buckets = [
            self._compute_bucket_metrics(
                bucket,
                all_events,
                live_buckets.get(bucket["id"]),
                external_buckets.get(bucket["id"]),
                quota_now,
            )
            for bucket in QUOTA_BUCKETS
        ]
        sources_used = {bucket["quota"]["source"] for bucket in buckets}
        quota_source = next((name for name in ("antigravity", "external") if name in sources_used), "unavailable")

        recent_sessions = [
            {
                "conversationId": c.conversation_id,
                "title": c.title,
                "modelDisplay": format_model_display_name(c.latest_model),
                "contextTokens": c.context_tokens,
                "lastUpdated": c.last_ts.isoformat() if c.last_ts else None,
            }
            for c in conversations[:25]
        ]

        models_list = sorted(model_totals.values(), key=lambda x: x["totalTokens"], reverse=True)

        return {
            "generatedAt": now_utc.isoformat(),
            "localTime": now_local.strftime("%m月%d日 %H:%M"),
            "conversationCount": len(conversations),
            "preferences": prefs,
            "activeSessionHintId": active_hint.get("conversationId"),
            "session": session_payload,
            "recentSessions": recent_sessions,
            "quotas": {
                "source": quota_source,
                "buckets": buckets,
            },
            "usageSummary": {
                "todayTokens": today_total,
                "todayCalls": today_calls,
                "todayCacheHitPct": today_cache_hit,
                "monthTokens": month_total,
                "allTimeTokens": all_total,
                "allTimeUncachedInput": all_in,
                "allTimeCacheRead": all_cache,
                "allTimeOutput": all_out,
            },
            "calendar": {
                "year": target_year,
                "month": target_month,
                "firstWeekday": first_weekday,
                "days": calendar_days,
                "monthSelectedTotal": sum(d["total"] for d in calendar_days),
            },
            "modelBreakdown": models_list,
        }

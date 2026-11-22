<div align="center">
  <img src="./assets/logo.svg" alt="Antigravity Quota & Context Monitor Logo" width="84" height="84" />
  <h1>Antigravity Quota &amp; Context Monitor</h1>
  <p>
    面向 <strong>Google Antigravity 2.0 桌面端</strong> 的零 Token 消耗本地监控插件<br />
    实时上下文占用环 · 精确缓存命中率 · 每日用量热力日历 · Gemini / Claude &amp; GPT / Argon 额度监控
  </p>
  <p>
    <img src="https://img.shields.io/badge/Antigravity-2.0%20MCP%20App-6366F1?style=flat-square" alt="Antigravity 2.0 MCP App" />
    <img src="https://img.shields.io/badge/Python-3.9%2B-38BDF8?style=flat-square" alt="Python 3.9+" />
    <img src="https://img.shields.io/badge/Dependencies-Zero-34D399?style=flat-square" alt="Zero Dependencies" />
    <img src="https://img.shields.io/badge/License-MIT-94A3B8?style=flat-square" alt="MIT License" />
  </p>
</div>

---

## 为什么做这个插件？

在日常高频使用 **Google Antigravity 2.0 桌面端** 时，经常需要同时关注两件事：
1. **当前对话上下文还剩多少、缓存命中高不高？**（防止上下文膨胀却浑然不知）
2. **Gemini 5h / 周额度、Claude & GPT 以及 Gemini 4 Argon 余量还剩多少？**（不用反复点开左下角菜单）

本插件基于 Antigravity 官方 **MCP Apps（SEP-1865）** 与生命周期 **Hooks** 构建，**完全在本地运行、不额外消耗任何模型 Token、无需安装第三方 Python/Node 依赖**，支持暗色 / 浅色主题原生融合。

---

## 核心特性

### 1. 零模型 Token 消耗 & 零外部依赖
- 所有刷新与同步工具均声明为 `visibility: ["app"]`，对大模型完全不可见，面板刷新不占用对话上下文或模型额度。
- **纯 Python 标准库后端 + 单文件自包含 HTML 前端**，无需 `pip install` 或 `npm install`。

### 2. 三大额度池实时监控
- 完整对齐 Antigravity 原生 `View Usage` 分组：
  - **外显常驻**：`Gemini Models` 的 **5h 限额** 与 **周限额**（极简进度环 + 剩余百分比 + 重置倒计时 + 本机窗口 Token 消耗）
  - **点击展开**：`Claude & GPT models`（5h / 周限额）、`Gemini 4 Argon`（周限额）
- 面板通过桌面端本机的 `RetrieveUserQuotaSummary` 接口读取剩余百分比与重置时间，支持点击「同步」跳过缓存立即刷新；同时支持外部额度文件 `external_quota.json` 作为补充来源。
- 额度解析实现参考了 [Antigravity Model Usage](https://github.com/DYY-Studio/antigravity-model-usage/blob/main/sidecars/usage/main.mjs) 与 [CodexBar](https://github.com/steipete/CodexBar/blob/b41715f3e3fb85d01d807b9bd7a64d9bf384c6f8/Sources/CodexBarCore/Providers/Antigravity/AntigravityQuotaSummaryParser.swift)。

### 3. 当前会话上下文精确监控
- 直接读取 Antigravity 本机 `transcript.jsonl` 与 `conversation_summaries.db`（只读），精确展示：
  - **上下文占用环**：当前已用 Token 与百分比（`input_tokens + cache_read_tokens + output_tokens`）
  - **上下文构成条**：系统提示词与工具定义 / 对话消息与思考内容 / 工具输出与历史上下文的可视化占比
  - **会话统计**：模型调用次数、对话轮数 / 工具调用次数、平均输出速率
  - **Token 用量明细**：缓存命中率（%）、未缓存输入、缓存读取、输出 Token
- 内置自定义暗色/浅色会话切换菜单，直接显示**官方对话名称**，默认自动跟随当前活跃会话，也可随时切换查看历史会话。

### 4. 全局用量统计 & 当月每日热力日历
- 聚合本机全部历史会话，展示 **今日 / 本月 / 累计** Token 用量、今日缓存命中率、按模型用量分布。
- 内置 5 级热力日历，支持翻月查看与点击任意日期查看当日精确明细。

### 5. 双入口自适应布局
- **右侧辅助栏（`aux_pane` → `额度与上下文`）**：紧凑单列布局，可常驻在对话右侧边聊边看。
- **左侧导航栏（`global` → `用量与额度`）**：宽屏双列仪表盘总览。

---

## 目录结构

```text
.
├── plugin.json               # Antigravity 插件清单
├── mcp_config.json           # MCP Server 启动配置（使用 ${PLUGIN_ROOT} / ${PLUGIN_DATA}）
├── hooks.json                # 生命周期钩子（实时追踪活跃会话）
├── assets/
│   └── logo.svg              # 插件矢量图标
├── server/
│   ├── data_engine.py        # transcript.jsonl 解析、汇总、日历与额度聚合
│   ├── live_quota.py         # 读取桌面端本机余量和重置时间
│   ├── hook_tracker.py       # Hook 轻量接收器（<50ms）
│   └── mcp_server.py         # 纯 Python 标准库 MCP Apps Server（支持 --preview 预览）
├── ui/
│   └── app.html              # 单文件自包含自适应交互界面
└── scripts/
    ├── install_plugin.ps1    # Windows 一键安装到 ~/.gemini/config/plugins/
    └── test_local.py         # 单元测试与 MCP 协议端到端测试
```

---

## 快速安装

**前置要求**：本机已安装 Python 3.9+（且 `python` 命令在 PATH 中可用）。无需安装任何第三方包。

### Windows 一键安装（推荐）

下载本仓库后，在项目根目录打开 PowerShell 运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_plugin.ps1
```

脚本仅会把轻量插件文件复制到 `~/.gemini/config/plugins/antigravity-quota-monitor/`，Antigravity 会自动发现并加载：
1. **右侧辅助栏**：展开右上角辅助栏 → 点击 **`+`** → 选择 **额度与上下文**
2. **左侧导航栏**：在左侧插件菜单点击 **用量与额度**

### 手动安装（macOS / Linux / Windows 通用）

将以下文件/目录复制到 `~/.gemini/config/plugins/antigravity-quota-monitor/`：
`plugin.json`、`mcp_config.json`、`hooks.json`、`assets/`、`server/`、`ui/`。

> **提示**：macOS / Linux 若系统仅提供 `python3`，请将 `mcp_config.json` 与 `hooks.json` 中的 `python` 改为 `python3`。

---

## 本地测试与浏览器预览

### 运行自动化测试

```bash
python scripts/test_local.py
```

### 本地 HTTP 预览 UI（无需重启 Antigravity）

```bash
python server/mcp_server.py --preview 8765
```

在浏览器访问 `http://127.0.0.1:8765` 即可直接预览本机真实数据与交互效果。

---

## 可选：外部标准额度文件（`external_quota.json`）

如需补充外部同步数据，可按以下标准 JSON 格式写入 `${PLUGIN_DATA}/external_quota.json`（当桌面端未返回对应额度项且未过期时自动采用）：

```json
{
  "updatedAt": "2026-11-22T06:30:00Z",
  "buckets": {
    "gemini_5h": { "remainingPct": 88, "resetsAt": "2026-11-22T10:48:00Z" },
    "gemini_weekly": { "remainingPct": 90, "resetsAt": "2026-11-28T09:30:00Z" },
    "claude_gpt_5h": { "remainingPct": 100 },
    "claude_gpt_weekly": { "remainingPct": 92, "resetsAt": "2026-11-28T06:30:00Z" },
    "argon_weekly": { "remainingPct": 100 }
  }
}
```

---

## 隐私与安全说明

- 所有日志读取与额度查询均仅在**本机回环（`127.0.0.1`）与本地磁盘**完成，不向任何第三方服务器发送数据。
- 会话令牌仅在内存中临时使用，不写入配置文件、不打印日志、不落盘。
- 面板偏好与活跃会话指针仅保存在 Antigravity 分配的 `${PLUGIN_DATA}` 目录中，不会写入代码仓库。

## License

本项目基于 [MIT License](./LICENSE) 开源。

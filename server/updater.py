# -*- coding: utf-8 -*-
"""
Antigravity 额度与上下文监控插件 — 纯手动触发的安全原地更新模块。

特性：
1. 仅在用户主动点击「检查更新」时请求 GitHub Releases 公开 API，平时零后台外联。
2. 下载地址严格限制在 GitHub 官方域名白名单（HTTPS），并防范 Zip Slip 路径穿越。
3. 更新前自动备份当前插件运行文件至 ${PLUGIN_DATA}/backup/，若写入失败自动回滚。
4. 自动比对 server/*.py 哈希：若仅更新了前端 ui/app.html，可立即在面板内热重载生效。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_REPO_OWNER = "Xieduuu"
DEFAULT_REPO_NAME = "antigravity-quota-monitor"
EXPECTED_PLUGIN_NAME = "antigravity-quota-monitor"

ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}

# 仅替换运行期必需文件，不覆盖 .git / scripts / 用户本地数据
RUNTIME_ROOT_FILES = ("plugin.json", "mcp_config.json", "hooks.json", "README.md")
RUNTIME_SUBDIRS = ("assets", "server", "ui")
SKIP_SUBPATHS = {
    ("assets", "screenshots"),
}


def parse_semver(raw: Optional[str]) -> Tuple[int, ...]:
    """解析语义化版本号（如 v0.2.0 / 0.1.1），用于比较新旧版本。"""
    if not raw:
        return (0, 0, 0)
    cleaned = str(raw).strip().lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", cleaned)]
    if not nums:
        return (0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:4])


def is_newer_version(latest: str, current: str) -> bool:
    """判断 latest 是否严格高于 current。"""
    return parse_semver(latest) > parse_semver(current)


def read_local_version(plugin_root: Path) -> str:
    """从 plugin.json 读取当前插件版本号。"""
    manifest_path = plugin_root / "plugin.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ver = str(data.get("version") or "0.0.0").strip()
        return ver.lstrip("vV") or "0.0.0"
    except Exception:
        return "0.0.0"


def _resolve_repo_slug(plugin_root: Path) -> Tuple[str, str]:
    """优先从 plugin.json 的 repository 字段解析 GitHub owner/repo，默认回退主仓库。"""
    manifest_path = plugin_root / "plugin.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        repo = data.get("repository")
        url = repo.get("url") if isinstance(repo, dict) else repo
        if isinstance(url, str):
            m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s.#?]+)", url)
            if m:
                owner = m.group(1)
                name = m.group(2).removesuffix(".git")
                if owner and name:
                    return owner, name
    except Exception:
        pass
    return DEFAULT_REPO_OWNER, DEFAULT_REPO_NAME


def _validate_download_url(url: str) -> None:
    """严格校验下载地址协议与域名白名单。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("仅允许使用 HTTPS 下载更新包。")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"不受信任的更新下载域名: {host}")


def _sha256_normalized(path: Path) -> str:
    """对文本源文件做换行符归一化（CRLF -> LF）后再哈希，避免 Windows 换行差异误判后端变更。"""
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _snapshot_backend_hashes(root: Path) -> Dict[str, str]:
    """计算 server/*.py 哈希映射，用于判断是否需要重启后端进程。"""
    server_dir = root / "server"
    hashes: Dict[str, str] = {}
    if not server_dir.exists():
        return hashes
    for py_file in sorted(server_dir.glob("*.py")):
        if py_file.is_file():
            hashes[py_file.name] = _sha256_normalized(py_file)
    return hashes


def _collect_runtime_files(root: Path) -> List[Path]:
    """收集需要备份或更新的相对路径列表（不含 __pycache__ 与 screenshots）。"""
    rel_paths: List[Path] = []
    for fname in RUNTIME_ROOT_FILES:
        p = root / fname
        if p.is_file():
            rel_paths.append(Path(fname))

    for sub in RUNTIME_SUBDIRS:
        sub_dir = root / sub
        if not sub_dir.is_dir():
            continue
        for item in sub_dir.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(root)
            parts = rel.parts
            if "__pycache__" in parts or item.suffix in (".pyc", ".pyo"):
                continue
            if len(parts) >= 2 and (parts[0], parts[1]) in SKIP_SUBPATHS:
                continue
            rel_paths.append(rel)
    return rel_paths


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """安全解压 ZIP，阻止任何路径穿越（Zip Slip）。"""
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            member_path = Path(info.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"更新包包含非法路径: {info.filename}")
            target = (dest_dir / member_path).resolve()
            if dest_resolved not in target.parents and target != dest_resolved:
                raise ValueError(f"更新包路径越界: {info.filename}")
        zf.extractall(dest_dir)


def _find_extracted_root(extract_dir: Path) -> Path:
    """定位解压后包含 plugin.json 的项目根目录。"""
    if (extract_dir / "plugin.json").is_file():
        return extract_dir
    children = [p for p in extract_dir.iterdir() if p.is_dir()]
    for child in children:
        if (child / "plugin.json").is_file():
            return child
    raise ValueError("更新包中未找到有效的 plugin.json。")


def _validate_extracted_package(pkg_root: Path) -> str:
    """校验解压后的更新包结构完整且属于本插件，返回包内版本号。"""
    manifest_file = pkg_root / "plugin.json"
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    if data.get("name") != EXPECTED_PLUGIN_NAME:
        raise ValueError(f"插件标识不匹配: {data.get('name')}")
    if not (pkg_root / "server" / "mcp_server.py").is_file():
        raise ValueError("更新包缺少 server/mcp_server.py。")
    if not (pkg_root / "ui" / "app.html").is_file():
        raise ValueError("更新包缺少 ui/app.html。")
    return str(data.get("version") or "0.0.0").strip().lstrip("vV")


def check_for_updates(plugin_root: Path, timeout: float = 10.0) -> Dict[str, Any]:
    """
    主动向 GitHub Releases API 查询最新版本信息。
    出错时返回结构化错误信息，不抛出未捕获异常。
    """
    current_ver = read_local_version(plugin_root)
    owner, repo = _resolve_repo_slug(plugin_root)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat()

    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{EXPECTED_PLUGIN_NAME}/{current_ver}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            msg = "仓库尚未发布正式 Release。"
        elif exc.code == 403:
            msg = "GitHub API 请求频率受限，请稍后再试。"
        else:
            msg = f"检查更新失败（HTTP {exc.code}）。"
        return {
            "ok": False,
            "currentVersion": current_ver,
            "hasUpdate": False,
            "error": msg,
            "checkedAt": checked_at,
        }
    except Exception as exc:
        return {
            "ok": False,
            "currentVersion": current_ver,
            "hasUpdate": False,
            "error": f"无法连接 GitHub（请检查网络或代理）：{exc}",
            "checkedAt": checked_at,
        }

    tag_name = str(payload.get("tag_name") or "").strip()
    latest_ver = tag_name.lstrip("vV") or current_ver
    has_update = is_newer_version(latest_ver, current_ver)
    zip_url = str(payload.get("zipball_url") or "").strip()
    if not zip_url and tag_name:
        zip_url = f"https://github.com/{owner}/{repo}/archive/refs/tags/{tag_name}.zip"

    return {
        "ok": True,
        "currentVersion": current_ver,
        "latestVersion": latest_ver,
        "tagName": tag_name or f"v{latest_ver}",
        "hasUpdate": has_update,
        "releaseTitle": str(payload.get("name") or tag_name or f"v{latest_ver}"),
        "releaseNotes": str(payload.get("body") or "").strip(),
        "releaseUrl": str(payload.get("html_url") or f"https://github.com/{owner}/{repo}/releases"),
        "publishedAt": payload.get("published_at"),
        "downloadUrl": zip_url,
        "checkedAt": checked_at,
    }


def apply_update_from_zip(
    plugin_root: Path,
    data_dir: Path,
    zip_path: Path,
) -> Dict[str, Any]:
    """
    从已下载的本地 ZIP 文件执行安全校验、备份、原地覆盖与失败回滚。
    拆分为独立函数便于离线单元测试。
    """
    prev_ver = read_local_version(plugin_root)
    old_backend_hashes = _snapshot_backend_hashes(plugin_root)

    with tempfile.TemporaryDirectory(prefix="agy_qm_extract_") as tmp:
        extract_dir = Path(tmp)
        _safe_extract_zip(zip_path, extract_dir)
        pkg_root = _find_extracted_root(extract_dir)
        new_ver = _validate_extracted_package(pkg_root)
        new_backend_hashes = _snapshot_backend_hashes(pkg_root)
        backend_changed = new_backend_hashes != old_backend_hashes

        files_to_install = _collect_runtime_files(pkg_root)
        existing_files = _collect_runtime_files(plugin_root)

        # 1. 备份当前版本到 ${PLUGIN_DATA}/backup/
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = data_dir / "backup" / f"v{prev_ver}_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for rel in existing_files:
            src = plugin_root / rel
            dst = backup_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # 2. 原地覆盖写入新文件（出错自动从备份回滚）
        try:
            for rel in files_to_install:
                src = pkg_root / rel
                dst = plugin_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except Exception as exc:
            for rel in existing_files:
                bsrc = backup_dir / rel
                bdst = plugin_root / rel
                if bsrc.is_file():
                    bdst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(bsrc, bdst)
            raise RuntimeError(f"写入更新文件失败，已自动回滚至 v{prev_ver}：{exc}") from exc

    if backend_changed:
        msg = f"已更新至 v{new_ver}！本次包含后端代码更新，请在插件管理页关开一次开关（或重启 Antigravity）生效。"
    else:
        msg = f"已热更新至 v{new_ver}，正在自动刷新界面…"

    return {
        "ok": True,
        "previousVersion": prev_ver,
        "updatedVersion": new_ver,
        "backendChanged": backend_changed,
        "updatedFilesCount": len(files_to_install),
        "backupDir": str(backup_dir),
        "message": msg,
    }


def apply_update(
    plugin_root: Path,
    data_dir: Path,
    download_url: str,
    timeout: float = 25.0,
) -> Dict[str, Any]:
    """下载 GitHub Release 压缩包并安全执行原地更新。"""
    current_ver = read_local_version(plugin_root)
    try:
        _validate_download_url(download_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": f"{EXPECTED_PLUGIN_NAME}/{current_ver}"},
    )

    try:
        with tempfile.TemporaryDirectory(prefix="agy_qm_dl_") as tmp_dl:
            zip_path = Path(tmp_dl) / "release.zip"
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # 二次校验重定向后的最终落点域名
                final_url = resp.geturl()
                _validate_download_url(final_url)
                content = resp.read()
                if len(content) > 20 * 1024 * 1024:
                    return {"ok": False, "error": "更新包体积超过 20 MB 安全上限，已中止。"}
                zip_path.write_bytes(content)

            return apply_update_from_zip(plugin_root, data_dir, zip_path)
    except Exception as exc:
        return {"ok": False, "error": f"更新失败：{exc}"}

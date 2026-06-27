"""
项目同步管理器 — 哈希增量刷入、状态比对、文件拉取。

通过 ``ProjectSyncManager`` 封装所有需要设备连接的项目级操作，
使用 MicroPython 实例与设备通信。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import ntpath
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from ..utils.config import HASH_CONFIG_FILE, _HASH_VERSION
from ..utils.flash import SET_EXECUTE, _strip_repl_trailer
from ..utils.log import get_logger
from ..utils.build import load_manifest
from ..utils.ui import log as output_log, print_json, safe_text

if TYPE_CHECKING:
    from ..utils.flash import MicroPython

log = get_logger(__name__)


def compute_file_hash(filepath: str) -> str:
    """计算文件的 SHA256 哈希值。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(1048576)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class ProjectSyncManager:
    """项目同步管理器 — 哈希增量刷入、状态比对、文件拉取。"""

    def __init__(self, mp: "MicroPython") -> None:
        self.mp = mp

    @staticmethod
    def _normalise_remote_prefix(remote_prefix: str) -> str:
        prefix = (remote_prefix or "/").replace("\\", "/")
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        stripped = prefix.strip("/")
        return f"/{stripped}" if stripped else "/"

    @classmethod
    def _remote_prefix_candidates(cls, remote_prefix: str) -> List[str]:
        prefix = cls._normalise_remote_prefix(remote_prefix)
        if prefix == "/":
            return ["/"]
        relative = prefix.lstrip("/")
        return [prefix, relative]

    @staticmethod
    def _windows_path_reason(path: str) -> Optional[str]:
        drive, _tail = ntpath.splitdrive(path)
        if not drive:
            return None
        if drive.startswith(("\\\\", "//")):
            return "windows_unc"
        return "windows_drive"

    @classmethod
    def _safe_local_target_for_device_path(
        cls,
        local_dir: str,
        remote_prefix: str,
        remote_path: str,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Return (local_path, relative_path, reason) for a discovered device path."""
        raw_remote = str(remote_path)
        if not raw_remote:
            return None, None, "empty_path"

        reason = cls._windows_path_reason(raw_remote)
        if reason is not None:
            return None, None, reason
        if raw_remote.startswith("\\"):
            return None, None, "host_absolute"

        remote = raw_remote.replace("\\", "/")
        reason = cls._windows_path_reason(remote)
        if reason is not None:
            return None, None, reason
        if remote.startswith("//"):
            return None, None, "windows_unc"

        prefixes = cls._remote_prefix_candidates(remote_prefix)
        if prefixes == ["/"]:
            rel = remote.lstrip("/")
        else:
            rel = None
            for prefix in prefixes:
                prefix_base = prefix.rstrip("/")
                if remote == prefix_base:
                    rel = ""
                    break
                if remote.startswith(prefix_base + "/"):
                    rel = remote[len(prefix_base) + 1:]
                    break
            if rel is None:
                rel = remote.lstrip("/")

        if not rel or rel.startswith(("/", "\\")):
            return None, None, "host_absolute"

        parts: List[str] = []
        for part in rel.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                return None, None, "parent_reference"
            reason = cls._windows_path_reason(part)
            if reason is not None:
                return None, None, reason
            parts.append(part)

        if not parts:
            return None, None, "empty_path"

        base = Path(local_dir).resolve()
        target = base.joinpath(*parts).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None, None, "outside_local_dir"

        rel_path = "/".join(parts)
        return str(target).replace("\\", "/"), rel_path, None

    @classmethod
    def _device_download_targets(
        cls,
        local_dir: str,
        remote_prefix: str,
        remote_files: List[str],
    ) -> Tuple[List[str], List[str], List[Dict[str, str]]]:
        safe_remote_files: List[str] = []
        local_paths: List[str] = []
        skipped: List[Dict[str, str]] = []
        for remote_path in remote_files:
            local_path, _rel_path, reason = cls._safe_local_target_for_device_path(
                local_dir,
                remote_prefix,
                remote_path,
            )
            if local_path is None:
                skipped.append({
                    "remote": remote_path,
                    "reason": reason or "unsafe_device_path",
                })
                continue
            safe_remote_files.append(remote_path)
            local_paths.append(local_path)
        return safe_remote_files, local_paths, skipped

    # ── 文件收集 ──────────────────────────────────────────────

    @staticmethod
    def _collect_project_files(
        local_dir: str,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """收集项目中可刷入的文件列表。"""
        if manifest_path:
            entries = load_manifest(
                manifest_path, active_tags or set(), base_dir=local_dir,
            )
        else:
            entries = []
            for root, _dirs, files in os.walk(local_dir):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    lp = os.path.join(root, fn)
                    rp = os.path.relpath(lp, local_dir).replace("\\", "/")
                    entries.append((lp, rp))
        return [
            (lp, rp) for lp, rp in entries
            if Path(rp).name != "manifest.py" and not lp.endswith(".pyi")
        ]

    # ── project scan ────────────────────────────────

    def scan(
        self,
        local_dir: str,
        hash_config_path: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
    ) -> str:
        """扫描项目，计算所有可刷入文件的 SHA256 哈希并保存到配置文件。"""
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        entries = self._collect_project_files(local_dir, active_tags, manifest_path)

        file_hashes: Dict[str, str] = {}
        for lp, _rp in entries:
            rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
            file_hashes[rel_path] = compute_file_hash(lp)

        config = {
            "version": _HASH_VERSION,
            "hash_algorithm": "sha256",
            "files": file_hashes,
        }
        with open(hash_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        log.info("项目文件哈希已保存: %s (%d 个文件)", hash_config_path, len(file_hashes))
        for rel_path in sorted(file_hashes):
            log.debug("  %s", rel_path)
        return hash_config_path

    # ── project flash ───────────────────────────────

    def flash(
        self,
        local_dir: str,
        remote_prefix: str,
        hash_config_path: Optional[str] = None,
        bytecode_ver: Optional[int] = None,
        arch: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
        dry_run: bool = False,
        changed_paths: Optional[Set[str]] = None,
    ) -> List[Tuple[str, str, bool]]:
        """根据哈希配置，仅刷入新增或已更改的文件。"""
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        has_hash_config = os.path.exists(hash_config_path)
        if has_hash_config:
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored_config = json.load(f)
            stored_hashes = stored_config.get("files", {})
        else:
            log.warning("未找到哈希配置文件，将全量刷入")
            stored_hashes = {}

        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            log.info("没有需要刷入的文件")
            return []

        changed_abs: Optional[Set[str]] = None
        if changed_paths and has_hash_config:
            changed_abs = {
                os.path.abspath(path)
                for path in changed_paths
                if path
            }

        changed: List[Tuple[str, str, str]] = []
        unchanged_count = 0
        current_hashes: Dict[str, str] = {}

        for lp, rp_part in entries:
            if changed_abs is not None and os.path.abspath(lp) not in changed_abs:
                continue
            rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
            cur_hash = compute_file_hash(lp)
            current_hashes[rel_path] = cur_hash
            remote_path = os.path.join(remote_prefix, rp_part).replace("\\", "/")

            stored = stored_hashes.get(rel_path)
            if stored is None:
                changed.append((lp, remote_path, "新增"))
            elif stored != cur_hash:
                changed.append((lp, remote_path, "已更改"))
            else:
                unchanged_count += 1

        removed = [] if changed_abs is not None else [
            k for k in stored_hashes if k not in current_hashes
        ]
        if removed:
            log.info("%d 个文件已从项目中移除（将从配置中清除）", len(removed))
            for rf in sorted(removed):
                log.debug("  - %s", rf)

        if not changed:
            if changed_abs is not None:
                log.info("监听到的文件未产生可刷入变更，无需刷入")
                return []
            log.info("所有文件均未更改 (%d 个文件)，无需刷入", unchanged_count)
            return [
                (lp, os.path.join(remote_prefix, rp_part).replace("\\", "/"), True)
                for lp, rp_part in entries
            ]

        log.info("需要刷入 %d 个文件:", len(changed))
        for lp, rp, reason in changed:
            log.info("  [%s] %s → %s", reason, os.path.relpath(lp, local_dir), rp)
        if unchanged_count:
            log.info("%d 个文件未更改，跳过", unchanged_count)

        if dry_run:
            log.info("[DRY-RUN] 以上 %d 个文件将被刷入（未实际执行）", len(changed))
            return []

        try:
            results = self.mp.flash_entries(
                [(lp, remote_path) for lp, remote_path, _reason in changed],
                bytecode_ver=bytecode_ver,
                arch=arch,
                active_tags=active_tags,
                dry_run=False,
            )
        except Exception as e:
            log.error("batch flash failed: %s", e)
            results = [
                (lp, remote_path, False)
                for lp, remote_path, _reason in changed
            ]

        ok = sum(1 for _lp, _rp, success in results if success)
        fail = sum(1 for _lp, _rp, success in results if not success)

        if ok > 0:
            updated: Dict[str, str] = (
                dict(stored_hashes) if changed_abs is not None else {}
            )
            for lp, _rp_part in entries:
                if changed_abs is not None and os.path.abspath(lp) not in changed_abs:
                    continue
                rel_path = os.path.relpath(lp, local_dir).replace("\\", "/")
                was_flashed_ok = any(
                    lp == flp and success
                    for flp, _frp, success in results
                )
                if was_flashed_ok:
                    updated[rel_path] = current_hashes[rel_path]
                elif rel_path in stored_hashes:
                    updated[rel_path] = stored_hashes[rel_path]
                elif rel_path in current_hashes:
                    updated[rel_path] = current_hashes[rel_path]

            with open(hash_config_path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": _HASH_VERSION,
                    "hash_algorithm": "sha256",
                    "files": updated,
                }, f, indent=2, ensure_ascii=False)
            log.info("哈希配置已更新: %s", hash_config_path)

        parts = []
        if ok:
            parts.append(f"{ok} 成功")
        if fail:
            parts.append(f"{fail} 失败")
        log.info("增量刷入完成: %s", ", ".join(parts))
        return results

    # ── 设备端辅助查询 ────────────────────────────────

    def _check_device_files(
        self, remote_paths: List[str],
    ) -> Dict[str, int]:
        """批量检查设备文件存在性和大小。"""
        if not remote_paths:
            return {}
        paths_repr = repr(remote_paths)
        script = (
            "import os\n"
            "r=[]\n"
            f"for p in {paths_repr}:\n"
            " try:\n"
            "  r.append(str(os.stat(p)[6]))\n"
            " except OSError:\n"
            "  r.append('-')\n"
            "print(','.join(r))\n"
        )
        out = self.mp.run(script)
        sizes = out.strip().split(",")
        result: Dict[str, int] = {}
        for i, rp in enumerate(remote_paths):
            if i < len(sizes) and sizes[i] != "-":
                result[rp] = int(sizes[i])
            else:
                result[rp] = -1
        return result

    def _discover_device_files(
        self, remote_prefix: str,
    ) -> List[Tuple[str, int]]:
        """递归发现设备上的所有文件。"""
        script = (
            "import os\n"
            "def _walk(d):\n"
            " for n in os.listdir(d):\n"
            "  fp=(d+'/'+n).replace('//','/')\n"
            "  try:s=os.stat(fp)\n"
            "  except:continue\n"
            "  if s[0]&0x4000:\n"
            "   _walk(fp)\n"
            "  else:\n"
            "   print(str(s[6])+'|'+fp)\n"
            f"_walk({remote_prefix!r})\n"
        )
        out = self.mp.run(script)
        files: List[Tuple[str, int]] = []
        for line in out.strip().splitlines():
            line = line.strip()
            if "|" in line:
                sz, _, fp = line.partition("|")
                if sz.isdigit():
                    files.append((fp, int(sz)))
        return files

    @staticmethod
    def _unified_diff(
        remote_path: str,
        rel_path: str,
        remote_data: bytes,
        local_data: bytes,
    ) -> str:
        """Build a line-level unified diff between device and local content."""
        remote_text = [
            safe_text(line, preserve_newlines=True)
            for line in remote_data.decode("utf-8", errors="replace").splitlines(keepends=True)
        ]
        local_text = [
            safe_text(line, preserve_newlines=True)
            for line in local_data.decode("utf-8", errors="replace").splitlines(keepends=True)
        ]
        return "".join(difflib.unified_diff(
            remote_text,
            local_text,
            fromfile=safe_text(remote_path, preserve_newlines=False),
            tofile=safe_text(rel_path, preserve_newlines=False),
            lineterm="",
        ))

    # ── project status ──────────────────────────────

    def status(
        self,
        local_dir: str,
        remote_prefix: str,
        hash_config_path: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
        fmt: str = "text",
        diff: bool = False,
    ) -> bool:
        """比对本地哈希和设备端文件，显示差异清单（不刷入）。"""
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        if os.path.exists(hash_config_path):
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored = json.load(f).get("files", {})
        else:
            stored = {}

        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            if fmt == "json":
                print_json({
                    "added": [], "changed": [], "removed": [], "ok_count": 0,
                })
                return False
            log.info("没有可刷入的文件")
            return False

        current_hashes: Dict[str, str] = {}
        remote_paths: List[str] = []
        local_map: Dict[str, str] = {}
        for lp, rp_part in entries:
            rel = os.path.relpath(lp, local_dir).replace("\\", "/")
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            current_hashes[remote] = compute_file_hash(lp)
            remote_paths.append(remote)
            local_map[remote] = rel

        dev_sizes = self._check_device_files(remote_paths)

        added: List[Tuple[str, str]] = []
        changed: List[Tuple[str, str, str]] = []
        removed_list: List[str] = []
        ok_count = 0

        for rp in remote_paths:
            rel = local_map[rp]
            local_path = os.path.join(local_dir, rel)
            cur_hash = current_hashes.get(rp)
            old_hash = stored.get(rel)
            dev_size = dev_sizes.get(rp, -1)
            local_size = os.path.getsize(local_path)
            if dev_size < 0:
                added.append((rel, rp))
                continue

            if not diff:
                if dev_size != local_size or old_hash is None or cur_hash != old_hash:
                    changed.append((rel, rp, ""))
                else:
                    ok_count += 1
                continue

            with open(local_path, "rb") as f:
                local_data = f.read()
            try:
                remote_data = self.mp._read_device_file(rp)
            except Exception as e:
                log.warning("读取设备文件失败，退回哈希判断: %s (%s)", rp, e)
                if old_hash is not None and cur_hash != old_hash:
                    changed.append((rel, rp, ""))
                else:
                    ok_count += 1
                continue

            if remote_data != local_data:
                changed.append((rel, rp, self._unified_diff(rp, rel, remote_data, local_data)))
            elif old_hash is None:
                ok_count += 1
            else:
                ok_count += 1

        for rel in stored:
            if rel not in [local_map[r] for r in remote_paths]:
                removed_list.append(rel)

        has_diff = bool(added or changed or removed_list)

        if fmt == "json":
            print_json({
                "added":   [{"local": r, "remote": rp} for r, rp in added],
                "changed": [
                    {"local": r, "remote": rp, "diff": diff}
                    for r, rp, diff in changed
                ],
                "removed": removed_list,
                "ok_count": ok_count,
            })
            return has_diff

        # 打印差异清单（用户可见表格）
        header = f"{'状态':6}  {'本地文件':40}  {'设备路径':40}"
        sep = f"{'──':6}  {'─' * 40}  {'─' * 40}"
        output_log(f"\n  {header}")
        output_log(f"  {sep}")

        for rel, rp in added:
            rel_out = safe_text(rel, preserve_newlines=False)
            rp_out = safe_text(rp, preserve_newlines=False)
            output_log(f"  \033[33m[ADD]\033[0m  {rel_out:<40}  {rp_out:<40}")
        for rel, rp, _diff in changed:
            rel_out = safe_text(rel, preserve_newlines=False)
            rp_out = safe_text(rp, preserve_newlines=False)
            output_log(f"  \033[33m[MOD]\033[0m  {rel_out:<40}  {rp_out:<40}")
        for rel in removed_list:
            rel_out = safe_text(rel, preserve_newlines=False)
            output_log(f"  \033[31m[DEL]\033[0m  {rel_out:<40}  {'(不在项目中)':40}")

        for _rel, _rp, diff in changed:
            if diff:
                output_log("")
                for line in diff.splitlines():
                    output_log(f"    {line}")

        if not has_diff:
            log.info("所有文件一致 (%d 个文件)", ok_count)
        else:
            log.info(
                "一致: %d  新增: %d  变更: %d  删除: %d",
                ok_count, len(added), len(changed), len(removed_list),
            )
        return has_diff

    # ── project pull ────────────────────────────────

    def pull(
        self,
        local_dir: str,
        remote_prefix: str,
        hash_config_path: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
        dry_run: bool = False,
        fmt: str = "text",
    ) -> bool:
        """从设备下载文件到本地（批量传输）。"""
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        pre_skipped: List[Dict[str, str]] = []
        if not entries:
            if fmt != "json":
                log.info("本地目录为空，从设备发现文件...")
            dev_files = self._discover_device_files(remote_prefix)
            if not dev_files:
                if fmt == "json":
                    if dry_run:
                        print_json({"preview": []})
                    else:
                        print_json({"downloaded": [], "skipped": [], "failed": []})
                else:
                    log.info("设备上未发现文件")
                return True
            entries = []
            for rp, _sz in dev_files:
                lp, _rel, reason = self._safe_local_target_for_device_path(
                    local_dir, remote_prefix, rp,
                )
                if reason is not None:
                    pre_skipped.append({"remote": rp, "reason": reason})
                    continue
                entries.append((lp, rp))

        remote_files: List[str] = []
        local_paths: List[str] = []
        for lp, rp_part in entries:
            if str(rp_part).startswith("/"):
                remote_files.append(str(rp_part).replace("\\", "/"))
                local_paths.append(lp)
                continue
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            remote_files.append(remote)
            local_paths.append(lp)

        return self._download_device_files(
            remote_files,
            local_paths,
            dry_run=dry_run,
            fmt=fmt,
            pre_skipped=pre_skipped,
        )

    # ── device backup / restore ─────────────────────

    def backup(
        self,
        local_dir: str,
        remote_prefix: str = "/",
        dry_run: bool = False,
        fmt: str = "text",
    ) -> bool:
        """Back up every file below a device path into a local directory."""
        dev_files = self._discover_device_files(remote_prefix)
        if not dev_files:
            if fmt == "json":
                if dry_run:
                    print_json({"preview": []})
                else:
                    print_json({"downloaded": [], "skipped": [], "failed": []})
            else:
                log.info("设备上未发现文件")
            return True

        remote_files: List[str] = []
        local_paths: List[str] = []
        pre_skipped: List[Dict[str, str]] = []
        for rp, _sz in dev_files:
            lp, _rel, reason = self._safe_local_target_for_device_path(
                local_dir, remote_prefix, rp,
            )
            if reason is not None:
                pre_skipped.append({"remote": rp, "reason": reason})
                continue
            remote_files.append(rp)
            local_paths.append(lp)
        return bool(self._download_device_files(
            remote_files,
            local_paths,
            dry_run=dry_run,
            fmt=fmt,
            pre_skipped=pre_skipped,
        ))

    def restore(
        self,
        local_dir: str,
        remote_prefix: str = "/",
        dry_run: bool = False,
        overwrite: bool = True,
    ) -> List[Tuple[str, str, bool]]:
        """Restore every local file below a directory onto the device."""
        if not os.path.isdir(local_dir):
            raise NotADirectoryError(f"不是有效目录: {local_dir}")

        entries: List[Tuple[str, str]] = []
        for root, _dirs, files in os.walk(local_dir):
            for fn in files:
                lp = os.path.join(root, fn)
                rel = os.path.relpath(lp, local_dir).replace("\\", "/")
                rp = os.path.join(remote_prefix, rel).replace("\\", "/")
                entries.append((lp, rp))

        if not entries:
            log.info("本地目录为空，无需恢复")
            return []

        dirs = sorted({
            os.path.dirname(rp)
            for _lp, rp in entries
            if os.path.dirname(rp)
        })
        if dirs and not dry_run:
            self._ensure_device_dirs(dirs)

        results: List[Tuple[str, str, bool]] = []
        for lp, rp in entries:
            if dry_run:
                log.info("[PREVIEW] 将恢复 %s → %s", lp, rp)
                results.append((lp, rp, True))
                continue
            if not overwrite:
                try:
                    size = self._check_device_files([rp]).get(rp, -1)
                    if size >= 0:
                        log.warning("[SKIP] %s 已存在", rp)
                        results.append((lp, rp, False))
                        continue
                except Exception:
                    pass
            try:
                self.mp.flash_file(lp, rp, compile=False)
                results.append((lp, rp, True))
            except Exception as e:
                log.error("恢复失败 %s → %s: %s", lp, rp, e)
                results.append((lp, rp, False))

        ok = sum(1 for _lp, _rp, success in results if success)
        fail = len(results) - ok
        parts = [f"{ok} 成功"]
        if fail:
            parts.append(f"{fail} 失败")
        log.info("恢复完成: %s", ", ".join(parts))
        return results

    def _ensure_device_dirs(self, dirs: List[str]) -> None:
        script = (
            "import os\n"
            f"dirs={dirs!r}\n"
            "for d in dirs:\n"
            " parts=[p for p in d.split('/') if p]\n"
            " cur=''\n"
            " for p in parts:\n"
            "  cur=cur+'/'+p\n"
            "  try:\n"
            "   os.mkdir(cur)\n"
            "  except OSError:\n"
            "   pass\n"
        )
        self.mp.run(script)

    def _download_device_files(
        self,
        remote_files: List[str],
        local_paths: List[str],
        dry_run: bool = False,
        fmt: str = "text",
        pre_skipped: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        """Download a known device file list using one raw byte stream."""
        skipped = list(pre_skipped or [])
        if dry_run:
            if fmt == "json":
                payload = {
                    "preview": [
                        {"remote": rp, "local": lp}
                        for rp, lp in zip(remote_files, local_paths)
                    ],
                }
                if skipped:
                    payload["skipped"] = skipped
                    payload["failed"] = []
                print_json(payload)
            else:
                for item in skipped:
                    log.warning(
                        "[SKIP] %s (%s)",
                        safe_text(item["remote"], preserve_newlines=False),
                        safe_text(item["reason"], preserve_newlines=False),
                    )
                log.info("[PREVIEW] 将下载 %d 个文件:", len(remote_files))
                for rp, lp in zip(remote_files, local_paths):
                    log.info(
                        "  %s → %s",
                        safe_text(rp, preserve_newlines=False),
                        safe_text(lp, preserve_newlines=False),
                    )
            return True

        if fmt != "json":
            for item in skipped:
                log.warning(
                    "[SKIP] %s (%s)",
                    safe_text(item["remote"], preserve_newlines=False),
                    safe_text(item["reason"], preserve_newlines=False),
                )

        if not remote_files:
            if fmt == "json":
                print_json({
                    "downloaded": [], "skipped": skipped, "failed": [],
                })
            else:
                log.info("download complete: 0 ok")
            return True

        self.mp._enter_raw_repl()
        script = (
            "import os,sys\n"
            "_out=sys.stdout.buffer\n"
            f"files={remote_files!r}\n"
            "sizes=[]\n"
            "for f in files:\n"
            " try:\n"
            "  sizes.append(os.stat(f)[6])\n"
            " except:\n"
            "  sizes.append(-1)\n"
            "_out.write(b'SZ:'+','.join(str(s) for s in sizes).encode()+b'\\n')\n"
            "for i,f in enumerate(files):\n"
            " if sizes[i]>=0:\n"
            "  with open(f,'rb') as fp:\n"
            "   while True:\n"
            "    c=fp.read(512)\n"
            "    if not c:break\n"
            "    _out.write(c)\n"
        )
        self.mp._write(script.encode() + SET_EXECUTE)
        time.sleep(0.3)

        buf = b""
        deadline = time.time() + max(30, len(remote_files) * 8)
        sizes: List[int] = []
        expected_total = -1
        raw_start = -1

        while time.time() < deadline:
            if self.mp.transport.in_waiting:
                buf += self.mp.transport.read(self.mp.transport.in_waiting)
                if expected_total >= 0 and len(buf) > expected_total + 131072:
                    break
                if expected_total < 0:
                    sz_marker = buf.find(b"SZ:")
                    if sz_marker >= 0:
                        nl = buf.find(b"\n", sz_marker)
                        if nl >= 0:
                            try:
                                sizes = [
                                    int(x)
                                    for x in buf[sz_marker + 3 : nl].decode().split(",")
                                ]
                                expected_total = sum(s for s in sizes if s >= 0)
                                raw_start = nl + 1
                            except Exception:
                                pass
                if expected_total >= 0:
                    raw_len = len(buf) - raw_start
                    raw = _strip_repl_trailer(buf[raw_start:])
                    if len(raw) >= expected_total:
                        time.sleep(0.05)
                        buf += self.mp.transport.read(
                            self.mp.transport.in_waiting,
                        )
                        break
            else:
                time.sleep(0.02)

        if expected_total < 0:
            return self._pull_transfer_error(
                fmt, "size_info_missing", "无法获取文件大小信息",
            )

        if len(sizes) != len(remote_files):
            return self._pull_transfer_error(
                fmt, "file_count_mismatch", "设备返回文件数量不匹配",
                expected=len(remote_files), received=len(sizes),
            )

        raw = _strip_repl_trailer(buf[raw_start:])
        if len(raw) < expected_total:
            return self._pull_transfer_error(
                fmt, "incomplete_data", "数据不完整",
                expected_bytes=expected_total, received_bytes=len(raw),
            )

        raw = raw[:expected_total]

        ok = 0
        fail = 0
        offset = 0
        downloaded = []
        failed = []
        for i, (lp, size) in enumerate(zip(local_paths, sizes)):
            if size < 0:
                log.warning(
                    "[SKIP] %s (设备上不存在)",
                    safe_text(remote_files[i], preserve_newlines=False),
                )
                skipped.append({"remote": remote_files[i], "reason": "missing"})
                fail += 1
                continue
            file_data = raw[offset : offset + size]
            offset += size
            try:
                os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
                with open(lp, "wb") as f:
                    f.write(file_data)
                log.info(
                    "✓ %s → %s (%d 字节)",
                    safe_text(remote_files[i], preserve_newlines=False),
                    safe_text(lp, preserve_newlines=False),
                    size,
                )
                downloaded.append({
                    "remote": remote_files[i], "local": lp, "size": size,
                })
                ok += 1
            except Exception as e:
                log.error(
                    "✗ %s → %s: %s",
                    safe_text(remote_files[i], preserve_newlines=False),
                    safe_text(lp, preserve_newlines=False),
                    safe_text(e, preserve_newlines=False),
                )
                failed.append({"remote": remote_files[i], "error": str(e)})
                fail += 1

        if fmt == "json":
            print_json({
                "downloaded": downloaded, "skipped": skipped, "failed": failed,
            })
        else:
            parts = [f"{ok} 成功"]
            if fail:
                parts.append(f"{fail} 失败")
            log.info("下载完成: %s", ", ".join(parts))
        return True

    def _pull_transfer_error(
        self, fmt: str, code: str, message: str, **details: int,
    ) -> bool:
        if fmt == "json":
            payload = {"error": code, "message": message}
            payload.update(details)
            print_json(payload)
        else:
            extra = ""
            if code == "file_count_mismatch":
                extra = (
                    f"（期望 {details.get('expected')}，"
                    f"收到 {details.get('received')}）"
                )
            elif code == "incomplete_data":
                extra = (
                    f": 期望 {details.get('expected_bytes')} 字节, "
                    f"收到 {details.get('received_bytes')} 字节"
                )
            log.error("%s%s", message, extra)
        return False

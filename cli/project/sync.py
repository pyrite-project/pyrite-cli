import os
import time
import json
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from ..utils.ansi import _GREEN, _YELLOW, _RED, _RESET
from ..utils.flash import _strip_repl_trailer, SET_EXECUTE
from ..utils.config import _HASH_VERSION, HASH_CONFIG_FILE
from ..utils.manifest_loader import load_manifest

if TYPE_CHECKING:
    from ..utils.flash import MicroPython


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
    """项目同步管理器 — 哈希增量刷入、状态比对、文件拉取。

    封装所有需要设备连接的项目级操作，通过 MicroPython 实例与设备通信。
    """

    def __init__(self, mp: "MicroPython") -> None:
        self.mp = mp

    # ── 文件收集 ──────────────────────────────────────────────

    @staticmethod
    def _collect_project_files(
        local_dir: str,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """收集项目中可刷入的文件列表。

        Returns:
            list of (local_abs_path, local_rel_remote)
        """
        if manifest_path:
            entries = load_manifest(manifest_path, active_tags or set(), base_dir=local_dir)
        else:
            entries = []
            for root, _dirs, files in os.walk(local_dir):
                for fn in files:
                    if not fn.endswith(".py"):
                        continue
                    lp = os.path.join(root, fn)
                    rp = os.path.relpath(lp, local_dir).replace("\\", "/")
                    entries.append((lp, rp))
        # 过滤 manifest.py 和 .pyi
        return [(lp, rp) for lp, rp in entries
                if Path(rp).name != "manifest.py" and not lp.endswith(".pyi")]

    # ── project scan ────────────────────────────────

    def scan(
        self, local_dir: str,
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

        print(f"  {_GREEN}项目文件哈希已保存:{_RESET} {hash_config_path}")
        print(f"  {_GREEN}共 {len(file_hashes)} 个文件{_RESET}")
        for rel_path in sorted(file_hashes):
            print(f"    {rel_path}")
        return hash_config_path

    # ── project flash ───────────────────────────────

    def flash(
        self, local_dir: str, remote_prefix: str,
        hash_config_path: Optional[str] = None,
        bytecode_ver: Optional[int] = None,
        arch: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
    ) -> List[Tuple[str, str, bool]]:
        """根据哈希配置，仅刷入新增或已更改的文件。"""
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        # 加载已有哈希配置
        if os.path.exists(hash_config_path):
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored_config = json.load(f)
            stored_hashes = stored_config.get("files", {})
        else:
            print(f"  {_YELLOW}[WARN]{_RESET} 未找到哈希配置文件，将全量刷入")
            stored_hashes = {}

        # 扫描当前项目文件
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            print("  没有需要刷入的文件。")
            return []

        # 计算当前哈希并比对
        changed: List[Tuple[str, str, str]] = []  # [(local_abs_path, remote_path, reason)]
        unchanged_count = 0
        current_hashes: Dict[str, str] = {}

        for lp, rp_part in entries:
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

        # 报告已删除文件
        removed = [k for k in stored_hashes if k not in current_hashes]
        if removed:
            print(f"  {_YELLOW}[INFO]{_RESET} {len(removed)} 个文件已从项目中移除（将从配置中清除）")
            for rf in sorted(removed):
                print(f"    - {rf}")

        if not changed:
            print(f"  {_GREEN}所有文件均未更改 ({unchanged_count} 个文件)，无需刷入{_RESET}")
            return [(lp, os.path.join(remote_prefix, rp_part).replace("\\", "/"), True)
                    for lp, rp_part in entries]

        print(f"  {_GREEN}需要刷入 {len(changed)} 个文件:{_RESET}")
        for lp, rp, reason in changed:
            print(f"    [{reason}] {os.path.relpath(lp, local_dir)} -> {rp}")
        if unchanged_count:
            print(f"  {_GREEN}{unchanged_count} 个文件未更改，跳过{_RESET}")

        if dry_run:
            print(f"  {_YELLOW}[DRY-RUN]{_RESET} 以上 {len(changed)} 个文件将被刷入（未实际执行）")
            return []

        # 逐个刷入变更文件
        results: List[Tuple[str, str, bool]] = []
        ok = 0
        fail = 0

        for lp, remote_path, _reason in changed:
            print("")
            try:
                self.mp.flash_file(
                    lp, remote_path,
                    compile=None,
                    bytecode_ver=bytecode_ver,
                    arch=arch,
                    active_tags=active_tags,
                )
                results.append((lp, remote_path, True))
                ok += 1
            except Exception as e:
                print(f"  {_RED}刷入失败: {e}{_RESET}")
                results.append((lp, remote_path, False))
                fail += 1

        # 更新哈希配置（仅成功刷入的文件）
        if ok > 0:
            updated: Dict[str, str] = {}
            for lp, rp_part in entries:
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

            print(f"\n  {_GREEN}哈希配置已更新:{_RESET} {hash_config_path}")

        parts = []
        if ok:
            parts.append(f"\033[32m{ok} 成功\033[0m")
        if fail:
            parts.append(f"\033[31m{fail} 失败\033[0m")
        print(f"\n增量刷入完成: {', '.join(parts)}")
        return results

    # ── 设备端辅助查询 ────────────────────────────────

    def _check_device_files(self, remote_paths: List[str]) -> Dict[str, int]:
        """批量检查设备文件存在性和大小。

        Returns:
            {remote_path: size}，不存在的文件 size 为 -1
        """
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
        sizes = out.strip().split(',')
        result: Dict[str, int] = {}
        for i, rp in enumerate(remote_paths):
            if i < len(sizes) and sizes[i] != '-':
                result[rp] = int(sizes[i])
            else:
                result[rp] = -1
        return result

    def _discover_device_files(self, remote_prefix: str) -> List[Tuple[str, int]]:
        """递归发现设备上的所有文件，返回 [(full_remote_path, size), ...]."""
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
            if '|' in line:
                sz, _, fp = line.partition('|')
                if sz.isdigit():
                    files.append((fp, int(sz)))
        return files

    # ── project status ──────────────────────────────

    def status(
        self, local_dir: str, remote_prefix: str,
        hash_config_path: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
    ) -> None:
        """比对本地哈希和设备端文件，显示差异清单（不刷入）。"""
        if hash_config_path is None:
            hash_config_path = os.path.join(local_dir, HASH_CONFIG_FILE)

        # 加载哈希配置
        if os.path.exists(hash_config_path):
            with open(hash_config_path, "r", encoding="utf-8") as f:
                stored = json.load(f).get("files", {})
        else:
            stored = {}

        # 扫描本地文件
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        if not entries:
            print("  没有可刷入的文件。")
            return

        current_hashes: Dict[str, str] = {}
        remote_paths: List[str] = []
        local_map: Dict[str, str] = {}  # {remote_path: local_rel_path}
        for lp, rp_part in entries:
            rel = os.path.relpath(lp, local_dir).replace("\\", "/")
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            current_hashes[remote] = compute_file_hash(lp)
            remote_paths.append(remote)
            local_map[remote] = rel

        # 查询设备端文件
        dev_sizes = self._check_device_files(remote_paths)

        # 构建差异列表
        added: List[Tuple[str, str]] = []     # 本地有，设备无
        changed: List[Tuple[str, str]] = []   # 哈希不同
        removed: List[str] = []               # 配置有，本地无
        ok_count = 0

        for rp in remote_paths:
            rel = local_map[rp]
            cur_hash = current_hashes.get(rp)
            old_hash = stored.get(rel)
            dev_size = dev_sizes.get(rp, -1)
            if dev_size < 0:
                added.append((rel, rp))
            elif old_hash is not None and cur_hash != old_hash:
                changed.append((rel, rp))
            elif old_hash is None:
                added.append((rel, rp))
            else:
                ok_count += 1

        for rel in stored:
            if rel not in [local_map[r] for r in remote_paths]:
                removed.append(rel)

        # 打印差异清单
        header = f"{'状态':6}  {'本地文件':40}  {'设备路径':40}"
        sep = f"{'──':6}  {'─'*40}  {'─'*40}"
        print(f"\n  {header}")
        print(f"  {sep}")

        for rel, rp in added:
            print(f"  {_YELLOW}[ADD]{_RESET}  {rel:<40}  {rp:<40}")
        for rel, rp in changed:
            print(f"  {_YELLOW}[MOD]{_RESET}  {rel:<40}  {rp:<40}")
        for rel in removed:
            print(f"  {_RED}[DEL]{_RESET}  {rel:<40}  {'(不在项目中)':40}")

        if not added and not changed and not removed:
            print(f"  {_GREEN}所有文件一致 ({ok_count} 个文件){_RESET}")
        else:
            print(f"  {_GREEN}一致: {ok_count}{_RESET}  "
                  f"{_YELLOW}新增: {len(added)}{_RESET}  "
                  f"{_YELLOW}变更: {len(changed)}{_RESET}  "
                  f"{_RED}删除: {len(removed)}{_RESET}")
        print()

    # ── project pull ────────────────────────────────

    def pull(
        self, local_dir: str, remote_prefix: str,
        hash_config_path: Optional[str] = None,
        active_tags: Optional[Set[str]] = None,
        manifest_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        """从设备下载文件到本地（批量传输）。"""
        # 尝试从本地项目收集文件清单
        entries = self._collect_project_files(local_dir, active_tags, manifest_path)
        from_device = False

        if not entries:
            print(f"  {_YELLOW}[INFO]{_RESET} 本地目录为空，从设备发现文件...")
            dev_files = self._discover_device_files(remote_prefix)
            if not dev_files:
                print(f"  {_YELLOW}[INFO]{_RESET} 设备上未发现文件。")
                return
            from_device = True
            entries = []
            for rp, sz in dev_files:
                rel = rp[len(remote_prefix):].lstrip('/') if rp.startswith(remote_prefix) else rp.lstrip('/')
                lp = os.path.join(local_dir, rel).replace("\\", "/")
                entries.append((lp, rel))

        # 构建远程文件路径列表
        remote_files: List[str] = []
        local_paths: List[str] = []
        for lp, rp_part in entries:
            remote = os.path.join(remote_prefix, rp_part).replace("\\", "/")
            remote_files.append(remote)
            local_paths.append(lp)

        if dry_run:
            print(f"  {_YELLOW}[PREVIEW]{_RESET} 将下载 {len(remote_files)} 个文件:")
            for rp, lp in zip(remote_files, local_paths):
                print(f"    {rp} -> {lp}")
            return

        # ── 批量获取 ──
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

        # ── 读取设备返回 ──
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
                                sizes = [int(x) for x in buf[sz_marker + 3:nl].decode().split(',')]
                                expected_total = sum(s for s in sizes if s >= 0)
                                raw_start = nl + 1
                            except Exception:
                                pass
                if expected_total >= 0:
                    raw_len = len(buf) - raw_start
                    raw = _strip_repl_trailer(buf[raw_start:])
                    if len(raw) >= expected_total:
                        time.sleep(0.05)
                        buf += self.mp.transport.read(self.mp.transport.in_waiting)
                        break
            else:
                time.sleep(0.02)

        if expected_total < 0:
            print(f"  {_RED}[ERROR]{_RESET} 无法获取文件大小信息")
            return

        if len(sizes) != len(remote_files):
            print(f"  {_RED}[ERROR]{_RESET} 设备返回文件数量不匹配"
                  f"（期望 {len(remote_files)}，收到 {len(sizes)}）")
            return

        # ── 解析原始数据 ──
        raw = _strip_repl_trailer(buf[raw_start:])

        if len(raw) < expected_total:
            print(f"  {_RED}[ERROR]{_RESET} 数据不完整:"
                  f" 期望 {expected_total} 字节, 收到 {len(raw)} 字节")
            return

        raw = raw[:expected_total]

        # ── 按大小分割并写入本地文件 ──
        ok = fail = 0
        offset = 0
        for i, (lp, size) in enumerate(zip(local_paths, sizes)):
            if size < 0:
                print(f"  {_YELLOW}[SKIP]{_RESET} {remote_files[i]} (设备上不存在)")
                fail += 1
                continue
            file_data = raw[offset:offset + size]
            offset += size
            try:
                os.makedirs(os.path.dirname(lp) or '.', exist_ok=True)
                with open(lp, "wb") as f:
                    f.write(file_data)
                print(f"  {_GREEN}✓{_RESET} {remote_files[i]} -> {lp} ({size} 字节)")
                ok += 1
            except Exception as e:
                print(f"  {_RED}✗{_RESET} {remote_files[i]} -> {lp}: {e}")
                fail += 1

        print(f"\n  {_GREEN}下载完成: {ok} 成功{_RESET}", end="")
        if fail:
            print(f"  {_RED}{fail} 失败{_RESET}", end="")
        print()

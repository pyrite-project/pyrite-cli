"""Host-side planning and parsing for device-resident test runs."""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence


DEFAULT_TEST_DIR = "test_device"
DEFAULT_REMOTE_DIR = "/.pyrite_tests/"
RESULT_PREFIX = "PYRITE_TEST"
PASS_STATUSES = {"pass"}


@dataclass(frozen=True)
class DeviceTestFile:
    local_path: Path
    relative_path: str
    remote_path: str


@dataclass(frozen=True)
class DeviceTestPlan:
    files: list[DeviceTestFile]
    remote_dir: str


@dataclass(frozen=True)
class CleanupPlan:
    remote_dir: str
    recursive: bool = True
    force: bool = True


@dataclass(frozen=True)
class DeviceTestResult:
    index: int
    status: str
    remote_path: str
    stdout: str
    error: str
    duration_ms: int

    @property
    def passed(self) -> bool:
        return self.status in PASS_STATUSES


@dataclass(frozen=True)
class DeviceTestSession:
    plan: DeviceTestPlan
    results: list[DeviceTestResult]
    raw_output: str

    @property
    def ok(self) -> bool:
        expected_paths = {item.remote_path for item in self.plan.files}
        result_paths = {result.remote_path for result in self.results}
        return len(self.results) == len(self.plan.files) and all(
            result.passed for result in self.results
        ) and result_paths == expected_paths


def _normalize_remote_dir(remote_dir: str) -> str:
    normalized = (remote_dir or DEFAULT_REMOTE_DIR).replace("\\", "/").strip()
    if not normalized:
        normalized = DEFAULT_REMOTE_DIR
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _remote_join(remote_dir: str, relative_path: str) -> str:
    rel = relative_path.replace("\\", "/").strip("/")
    if remote_dir == "/":
        return "/" + rel
    return remote_dir + "/" + rel


def _candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob("*.py"):
        if not candidate.is_file():
            continue
        rel_parts = candidate.relative_to(root).parts
        if "__pycache__" in rel_parts:
            continue
        if any(part.startswith(".") for part in rel_parts[:-1]):
            continue
        files.append(candidate)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def discover_device_tests(
    path: Optional[str | Path] = None,
    *,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    cwd: Optional[str | Path] = None,
) -> DeviceTestPlan:
    """Build the host upload plan for device-side tests."""
    base = Path.cwd() if cwd is None else Path(cwd)
    selected = Path(path) if path is not None else base / DEFAULT_TEST_DIR
    if not selected.is_absolute():
        selected = base / selected
    selected = selected.resolve()
    normalized_remote_dir = _normalize_remote_dir(remote_dir)

    if selected.is_file():
        if selected.suffix != ".py":
            raise ValueError(f"device test file must be .py: {selected}")
        candidates = [selected]
        rel_base = selected.parent
    elif selected.is_dir():
        candidates = _candidate_files(selected)
        rel_base = selected
    else:
        raise FileNotFoundError(f"device test path not found: {selected}")

    if not candidates:
        raise FileNotFoundError(f"no device test .py files found: {selected}")

    files: list[DeviceTestFile] = []
    for local_path in candidates:
        relative_path = local_path.relative_to(rel_base).as_posix()
        files.append(DeviceTestFile(
            local_path=local_path,
            relative_path=relative_path,
            remote_path=_remote_join(normalized_remote_dir, relative_path),
        ))

    return DeviceTestPlan(files=files, remote_dir=normalized_remote_dir)


def build_device_test_runner_script(
    remote_paths: Sequence[str],
    *,
    timeout: int | float = 10,
) -> str:
    """Generate the MicroPython script that executes uploaded tests."""
    paths = [path.replace("\\", "/") for path in remote_paths]
    path_dirs: set[str] = set()
    for remote_path in paths:
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        while parent and parent != "/":
            path_dirs.add(parent)
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    timeout_ms = max(0, int(float(timeout) * 1000))
    return f"""\
import sys,time
try:
 import ubinascii as _binascii
except Exception:
 import binascii as _binascii
_TESTS={paths!r}
_PATH_DIRS={sorted(path_dirs)!r}
_TIMEOUT_MS={timeout_ms}
_PREFIX={RESULT_PREFIX!r}
class _Capture:
 def __init__(self):
  self._parts=[]
 def write(self,data):
  if data is None:
   return 0
  text=str(data)
  self._parts.append(text)
  return len(text)
 def flush(self):
  pass
 def get(self):
  return ''.join(self._parts)
def _ticks_ms():
 try:
  return time.ticks_ms()
 except AttributeError:
  return int(time.time()*1000)
def _elapsed_ms(start):
 try:
  return time.ticks_diff(_ticks_ms(),start)
 except AttributeError:
  return _ticks_ms()-start
def _b64(value):
 if not isinstance(value,str):
  value=str(value)
 data=value.encode('utf-8')
 out=_binascii.b2a_base64(data)
 if isinstance(out,bytes):
  out=out.decode()
 return out.strip()
def _print_exc(exc,stream):
 try:
  sys.print_exception(exc,stream)
 except Exception:
  stream.write(exc.__class__.__name__+': '+str(exc)+'\\n')
def _emit(index,status,path,stdout,error,duration_ms):
 print(_PREFIX+'|'+str(index)+'|'+status+'|'+str(duration_ms)+'|'+_b64(path)+'|'+_b64(stdout)+'|'+_b64(error))
for _dir in _PATH_DIRS:
 if _dir not in sys.path:
  sys.path.insert(0,_dir)
_total_start=_ticks_ms()
for _idx,_path in enumerate(_TESTS):
 _cap=_Capture()
 _err=_Capture()
 _old_stdout=sys.stdout
 _start=_ticks_ms()
 _status='pass'
 try:
  sys.stdout=_cap
  _ns={{'__name__':'__main__','__file__':_path}}
  with open(_path,'r') as _file:
   _code=_file.read()
  exec(_code,_ns)
 except AssertionError as _exc:
  _status='fail'
  _print_exc(_exc,_err)
 except Exception as _exc:
  _status='error'
  _print_exc(_exc,_err)
 except BaseException as _exc:
  _status='error'
  _print_exc(_exc,_err)
 finally:
  sys.stdout=_old_stdout
 _duration=_elapsed_ms(_start)
 if _status=='pass' and _TIMEOUT_MS and _duration>_TIMEOUT_MS:
  _status='timeout'
  _err.write('Timeout: exceeded '+str(_TIMEOUT_MS)+' ms\\n')
 _emit(_idx,_status,_path,_cap.get(),_err.get(),_duration)
 if _TIMEOUT_MS and _elapsed_ms(_total_start)>_TIMEOUT_MS:
  break
"""


def _decode_field(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_device_test_output(output: str) -> list[DeviceTestResult]:
    """Parse result lines emitted by the device test runner."""
    results: list[DeviceTestResult] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith(RESULT_PREFIX + "|"):
            continue
        parts = line.split("|", 6)
        if len(parts) != 7:
            continue
        _prefix, index_s, status, duration_s, path_s, stdout_s, error_s = parts
        try:
            index = int(index_s)
        except ValueError:
            continue
        try:
            duration_ms = int(duration_s)
        except ValueError:
            duration_ms = 0
        results.append(DeviceTestResult(
            index=index,
            status=status,
            remote_path=_decode_field(path_s),
            stdout=_decode_field(stdout_s),
            error=_decode_field(error_s),
            duration_ms=duration_ms,
        ))
    return results


def build_cleanup_plan(plan: Any, *, keep_files: bool) -> Optional[CleanupPlan]:
    if keep_files:
        return None
    return CleanupPlan(remote_dir=_normalize_remote_dir(plan.remote_dir))


def run_device_test_plan(
    mp: Any,
    plan: DeviceTestPlan,
    *,
    timeout: int = 10,
    keep_files: bool = False,
) -> DeviceTestSession:
    """Upload, execute, parse, and optionally clean a device test plan."""
    if not plan.files:
        raise ValueError("device test plan has no files")

    cleanup = build_cleanup_plan(plan, keep_files=keep_files)
    try:
        for item in plan.files:
            mp.flash_file(str(item.local_path), item.remote_path, compile=False)
        script = build_device_test_runner_script(
            [item.remote_path for item in plan.files],
            timeout=timeout,
        )
        raw_output = mp.run(script, timeout=timeout)
        return DeviceTestSession(
            plan=plan,
            results=parse_device_test_output(raw_output),
            raw_output=raw_output,
        )
    finally:
        if cleanup is not None:
            original_error = sys.exc_info()[0]
            try:
                mp.fs_rm(
                    cleanup.remote_dir,
                    recursive=cleanup.recursive,
                    force=cleanup.force,
                )
            except Exception:
                if original_error is None:
                    raise

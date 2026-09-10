# -*- coding: utf-8 -*-
"""Locate qgis_process and run Processing algorithms."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence


LogFn = Callable[[str], None]
ProgressFn = Callable[[Dict[str, object]], None]

# qgis_process ConsoleFeedback: ...10...20...30...100 - done.
_PROGRESS_MILESTONE = re.compile(r"(?<!\d)(10|20|30|40|50|60|70|80|90|100)(?!\d)")
_PROGRESS_DONE = re.compile(r"100\s*-\s*done", re.I)
# Algorithm stage lines like: Placing points... 1451/205835 polygons (...)
_FRACTION_PROGRESS = re.compile(
    r"(?P<cur>\d+)\s*/\s*(?P<total>\d+)\s+(?P<unit>polygons?|tiles?|points?)\b",
    re.I,
)
_PROBE_DETAIL = re.compile(
    r"#(?P<idx>\d+):\s*(?P<probes>\d+)\s+probes,\s*(?P<hits>\d+)\s+hits,\s*(?P<secs>\d+)s",
    re.I,
)
_IMPORTANT_LOG = re.compile(
    r"(?i)(error|fail|exception|wrote|results|warning|===|\bok\b|status)"
)
_STAGE_HINT = re.compile(
    r"(?i)\b("
    r"loading|loaded|algorithm|providers?|plugins?|"
    r"opening|preparing|sampling|placing|writing|copying|"
    r"flatten|indexing|using|found|kept|patched|discover|"
    r"worker|tile|mesh|polygon|point|centroid"
    r")\b"
)

# Don't spam the console with every pushInfo; keep stage text fresh.
_LOG_MIN_INTERVAL_S = 2.5
# ConsoleFeedback often prints "0" / "...10..." with no newline before stage text.
_GLUED_PROGRESS_PREFIX = re.compile(
    r"^(?:(?:\.\.\.)?\d{1,3})+(?=[A-Za-z])"
)


@dataclass
class RunResult:
    ok: bool
    returncode: int
    command: List[str]
    stdout: str
    stderr: str


@dataclass
class QgisProcessConfig:
    bat_path: Optional[Path] = None
    extra_search: List[Path] = field(default_factory=list)


def _candidate_bats() -> List[Path]:
    candidates: List[Path] = []
    env = os.environ.get("QGIS_PROCESS_BAT") or os.environ.get("QGIS_PROCESS")
    if env:
        candidates.append(Path(env))
    which = shutil.which("qgis_process-qgis-ltr.bat") or shutil.which("qgis_process.bat")
    if which:
        candidates.append(Path(which))
    prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if prog.is_dir():
        for qgis_dir in sorted(prog.glob("QGIS*"), reverse=True):
            bin_dir = qgis_dir / "bin"
            for name in ("qgis_process-qgis-ltr.bat", "qgis_process-qgis.bat", "qgis_process.bat"):
                candidates.append(bin_dir / name)
    return candidates


def find_qgis_process(config: Optional[QgisProcessConfig] = None) -> Path:
    config = config or QgisProcessConfig()
    checked: List[Path] = []
    for path in ([config.bat_path] if config.bat_path else []) + config.extra_search + _candidate_bats():
        if path is None:
            continue
        checked.append(path)
        if path.is_file():
            return path.resolve()
    hint = "\n".join(f"  - {p}" for p in checked[:12])
    raise FileNotFoundError(
        "Could not find qgis_process bat. Install QGIS or set QGIS_PROCESS_BAT.\n"
        f"Checked:\n{hint}"
    )


def build_command(
    bat: Path,
    algorithm: str,
    params: Dict[str, object],
) -> List[str]:
    cmd: List[str] = [str(bat), "run", algorithm, "--"]
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        # Keep KEY=value as one argv token; list2cmdline quotes spaces for cmd.exe.
        cmd.append(f"{key}={value}")
    return cmd


class _StreamProgress:
    """Parse qgis_process stdout (including progress ticks without newlines)."""

    def __init__(
        self,
        *,
        job_index: int,
        job_count: int,
        job_label: str,
        log: Optional[LogFn] = None,
        progress: Optional[ProgressFn] = None,
    ) -> None:
        self.job_index = job_index
        self.job_count = max(job_count, 1)
        self.job_label = job_label
        self.log = log
        self.progress = progress
        self.line_buf = ""
        self.job_pct = 0.0
        self.stage_base = "Starting…"
        self.sub_cur: Optional[int] = None
        self.sub_total: Optional[int] = None
        self.sub_unit: str = ""
        self.sub_detail: str = ""
        self.saw_output = False
        self.t0 = time.monotonic()
        self._last_log_at = 0.0
        self._last_logged_stage = ""
        self._chunks: List[str] = []
        self._lock = threading.Lock()

    @property
    def elapsed_s(self) -> int:
        return max(0, int(time.monotonic() - self.t0))

    def _overall(self) -> float:
        base = (self.job_index - 1) / self.job_count
        return min(100.0, max(0.0, (base + self.job_pct / 100.0 / self.job_count) * 100.0))

    def _display_stage(self) -> str:
        base = self.stage_base or "Working…"
        # Always show a live clock so long quiet stretches never look frozen.
        return f"{base} · {self.elapsed_s}s"

    def _emit_progress(self) -> None:
        if not self.progress:
            return
        payload: Dict[str, object] = {
            "job_index": self.job_index,
            "job_count": self.job_count,
            "job_label": self.job_label,
            "job_pct": self.job_pct,
            "overall_pct": self._overall(),
            "stage": self._display_stage(),
            "stage_base": self.stage_base,
            "elapsed_s": self.elapsed_s,
            "sub_cur": int(self.sub_cur or 0) if self.sub_total else None,
            "sub_total": int(self.sub_total) if self.sub_total else None,
            "sub_unit": self.sub_unit if self.sub_total else "",
            "sub_detail": self.sub_detail if self.sub_total else "",
        }
        self.progress(payload)

    def set_stage(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self.stage_base = text
            self._emit_progress()

    def heartbeat(self) -> None:
        with self._lock:
            if not self.saw_output and self.job_pct <= 0:
                # Rotate hints while QGIS is silent on startup.
                elapsed = self.elapsed_s
                if elapsed < 5:
                    self.stage_base = "Starting QGIS process…"
                elif elapsed < 15:
                    self.stage_base = "Loading QGIS libraries / providers…"
                elif elapsed < 30:
                    self.stage_base = "Still starting QGIS (normal on first run)…"
                else:
                    self.stage_base = "Waiting for QGIS (still loading)…"
            self._emit_progress()

    def _should_log_line(self, line: str, *, force: bool = False) -> bool:
        if force or _IMPORTANT_LOG.search(line):
            return True
        now = time.monotonic()
        if now - self._last_log_at < _LOG_MIN_INTERVAL_S:
            return False
        return True

    def _log_line(self, line: str, *, force: bool = False) -> None:
        if not self.log or not line.strip():
            return
        stripped = line.strip()
        if _PROGRESS_DONE.search(stripped) or (
            _PROGRESS_MILESTONE.search(stripped)
            and all(c.isdigit() or c in ". -" for c in stripped)
        ):
            return
        if not self._should_log_line(line, force=force):
            return
        self._last_log_at = time.monotonic()
        self.log(line)

    def _update_pct_from_partial(self, text: str) -> None:
        if _PROGRESS_DONE.search(text):
            if self.job_pct < 100:
                self.job_pct = 100.0
                self.stage_base = "Finishing…"
                self._emit_progress()
            return
        found = [int(m.group(1)) for m in _PROGRESS_MILESTONE.finditer(text)]
        if not found:
            return
        pct = float(max(found))
        if pct > self.job_pct:
            self.job_pct = pct
            self.stage_base = f"Running… {int(pct)}%"
            self._emit_progress()

    def _clean_stage_line(self, text: str) -> str:
        """Remove glued ConsoleFeedback percent ticks like '0Placing…'."""
        text = text.strip()
        text = _GLUED_PROGRESS_PREFIX.sub("", text)
        return text.strip()

    def _apply_fraction_progress(self, stripped: str) -> bool:
        """Drive job % + sub-bar from '123/456 polygons' lines (not the log)."""
        m = _FRACTION_PROGRESS.search(stripped)
        if not m:
            return False
        total = int(m.group("total"))
        if total <= 0:
            return False
        cur = min(int(m.group("cur")), total)
        unit = (m.group("unit") or "").lower()
        if unit.startswith("polygon"):
            pct = 5.0 + 85.0 * (cur / total)
            nice_unit = "polygons"
            stage = "Placing points in masks"
        elif unit.startswith("tile"):
            pct = 10.0 + 80.0 * (cur / total)
            nice_unit = "tiles"
            stage = "Processing tiles"
        elif unit.startswith("point"):
            pct = 90.0 + 9.0 * (cur / total)
            nice_unit = "points"
            stage = "Sampling / writing points"
        else:
            pct = 100.0 * (cur / total)
            nice_unit = unit or "items"
            stage = self.stage_base or "Working…"
        pct = min(99.0, max(0.0, pct))
        self.sub_cur = cur
        self.sub_total = total
        self.sub_unit = nice_unit
        detail_m = _PROBE_DETAIL.search(stripped)
        if detail_m:
            self.sub_detail = (
                f"{int(detail_m.group('probes')):,} probes · "
                f"{int(detail_m.group('hits'))} hits · "
                f"{detail_m.group('secs')}s on current"
            )
        else:
            self.sub_detail = ""
        self.stage_base = stage
        if nice_unit == "polygons" or pct > self.job_pct:
            self.job_pct = pct
        self._emit_progress()
        return True

    def _log_stage(self, stripped: str, *, is_fraction: bool = False) -> None:
        # Fraction / heartbeat lines feed the sub-bar only — keep the log quiet.
        if is_fraction or not self.log or not stripped:
            return
        if stripped == self._last_logged_stage:
            return
        now = time.monotonic()
        if (now - self._last_log_at) < _LOG_MIN_INTERVAL_S and not _IMPORTANT_LOG.search(
            stripped
        ):
            return
        self._last_log_at = now
        self._last_logged_stage = stripped
        self.log(stripped)

    def _handle_complete_line(self, line: str) -> None:
        self._chunks.append(line + "\n")
        self.saw_output = True
        self._update_pct_from_partial(line)
        stripped = self._clean_stage_line(line)
        if not stripped:
            return
        if _PROGRESS_DONE.search(stripped):
            self.job_pct = 100.0
            self.stage_base = "Done"
            if self.sub_total:
                self.sub_cur = self.sub_total
            self._emit_progress()
            return
        if (
            _PROGRESS_MILESTONE.search(stripped)
            and all(c.isdigit() or c in ". -" for c in stripped)
            and "done" not in stripped.lower()
        ):
            return
        frac = _FRACTION_PROGRESS.search(stripped)
        if (
            len(stripped) < 320
            and not stripped.startswith("C:\\")
            and "=" not in stripped[:24]
            and (
                _STAGE_HINT.search(stripped)
                or stripped.endswith("…")
                or stripped.endswith("...")
                or frac
            )
        ):
            had_frac = self._apply_fraction_progress(stripped)
            if not had_frac:
                self.stage_base = stripped
                self._emit_progress()
            self._log_stage(stripped, is_fraction=bool(had_frac or frac))
            return
        if len(stripped) < 240 and not stripped.startswith("C:\\") and "=" not in stripped[:24]:
            had_frac = self._apply_fraction_progress(stripped)
            if not had_frac:
                self.stage_base = stripped
                self._emit_progress()
            self._log_stage(stripped, is_fraction=bool(had_frac or frac))
            return
        self._log_line(line, force=False)

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self._chunks.append(chunk)
        self.saw_output = True
        for ch in chunk:
            if ch == "\r":
                continue
            if ch == "\n":
                self._handle_complete_line(self.line_buf)
                self.line_buf = ""
            else:
                self.line_buf += ch
                if ch.isdigit() or ch in ".- ":
                    self._update_pct_from_partial(self.line_buf)

    def flush(self) -> None:
        if self.line_buf:
            self._handle_complete_line(self.line_buf)
            self.line_buf = ""

    def text(self) -> str:
        return "".join(self._chunks)


def run_algorithm(
    algorithm: str,
    params: Dict[str, object],
    *,
    bat: Optional[Path] = None,
    cwd: Optional[Path] = None,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    job_index: int = 1,
    job_count: int = 1,
    job_label: str = "",
    timeout: Optional[float] = None,
) -> RunResult:
    bat_path = bat or find_qgis_process()
    cmd = build_command(bat_path, algorithm, params)
    label = job_label or algorithm
    if log:
        log(" ".join(cmd))
    # .bat under "Program Files" must go through shell=True so cmd.exe gets the
    # Windows-correct /c ""path\with spaces\file.bat" args..." form. Passing
    # ["cmd","/c", list2cmdline(...)] double-escapes quotes and breaks.
    if bat_path.suffix.lower() == ".bat":
        popen_args: Any = subprocess.list2cmdline(list(cmd))
        use_shell = True
    else:
        popen_args = list(cmd)
        use_shell = False

    pump = _StreamProgress(
        job_index=job_index,
        job_count=job_count,
        job_label=label,
        log=log,
        progress=progress,
    )
    pump.set_stage("Starting QGIS process…")

    stop_beat = threading.Event()

    def _beat() -> None:
        while not stop_beat.wait(1.0):
            pump.heartbeat()

    beat_thread = threading.Thread(target=_beat, daemon=True)
    beat_thread.start()

    proc = subprocess.Popen(
        popen_args,
        shell=use_shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        cwd=str(cwd) if cwd else None,
    )
    assert proc.stdout is not None
    try:
        while True:
            block = proc.stdout.read(256)
            if not block:
                break
            pump.feed(block.decode("utf-8", errors="replace"))
        pump.flush()
        proc.wait(timeout=timeout)
    finally:
        stop_beat.set()
        beat_thread.join(timeout=2.0)

    ok = proc.returncode == 0
    if ok:
        pump.job_pct = 100.0
        pump.set_stage("Done")
    return RunResult(
        ok=ok,
        returncode=proc.returncode or 0,
        command=list(cmd),
        stdout=pump.text(),
        stderr="",
    )


def run_queue(
    jobs: Iterable[Dict[str, object]],
    *,
    bat: Optional[Path] = None,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> List[RunResult]:
    """
    jobs: iterable of {algorithm, params, label?}
    """
    job_list = list(jobs)
    results: List[RunResult] = []
    bat_path = bat or find_qgis_process()
    n = len(job_list)
    for i, job in enumerate(job_list, start=1):
        label = str(job.get("label") or job.get("algorithm") or f"job {i}")
        if log:
            log(f"\n=== [{i}/{n}] {label} ===")
        if progress:
            progress(
                {
                    "job_index": i,
                    "job_count": n,
                    "job_label": label,
                    "job_pct": 0.0,
                    "overall_pct": ((i - 1) / max(n, 1)) * 100.0,
                    "stage": "Starting…",
                }
            )
        result = run_algorithm(
            str(job["algorithm"]),
            dict(job.get("params") or {}),
            bat=bat_path,
            log=log,
            progress=progress,
            job_index=i,
            job_count=n,
            job_label=label,
        )
        results.append(result)
        if log:
            if result.ok:
                log(f"OK (exit {result.returncode})")
            else:
                log(f"FAILED (exit {result.returncode})")
                break
        if progress and result.ok:
            progress(
                {
                    "job_index": i,
                    "job_count": n,
                    "job_label": label,
                    "job_pct": 100.0,
                    "overall_pct": (i / max(n, 1)) * 100.0,
                    "stage": "Done",
                }
            )
    return results


if __name__ == "__main__":
    path = find_qgis_process()
    print(path)
    print("Python:", sys.executable)

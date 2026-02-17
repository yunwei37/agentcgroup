"""
MemcgController — Abstract interface for memory cgroup control.

Two backends:
  - BpfMemcgController:    wraps the memcg_priority eBPF binary (requires custom kernel)
  - CgroupMemcgController: uses standard cgroup v2 memory.low / memory.high controls

Use create_memcg_controller() to auto-detect the best available backend.
"""

import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("agentcgroupd")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class MemcgConfig:
    high_cgroup: str                    # path to HIGH priority cgroup
    low_cgroups: list[str] = field(default_factory=list)  # paths to LOW cgroups
    delay_ms: int = 50                  # BPF: over-high delay for LOW cgroups
    threshold: int = 1                  # page-fault threshold to trigger protection
    use_below_low: bool = True          # BPF: use below_low callback
    protection_window_s: float = 1.0    # how long protection stays active


# ---------------------------------------------------------------------------
# Cgroup file helpers
# ---------------------------------------------------------------------------

def _cgroup_read(path: str, filename: str) -> Optional[str]:
    """Read a cgroup control file, return contents or None on error."""
    filepath = os.path.join(path, filename)
    try:
        with open(filepath) as f:
            return f.read().strip()
    except OSError:
        return None


def _cgroup_write(path: str, filename: str, value: str) -> bool:
    """Write a value to a cgroup control file."""
    filepath = os.path.join(path, filename)
    try:
        with open(filepath, "w") as f:
            f.write(value)
        return True
    except OSError as e:
        log.warning("Failed to write '%s' to %s: %s", value, filepath, e)
        return False


def _read_memory_events(cgroup_path: str) -> dict[str, int]:
    """Parse memory.events into {name: count} dict."""
    raw = _cgroup_read(cgroup_path, "memory.events")
    if raw is None:
        return {}
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return result


def _read_psi_total(cgroup_path: str) -> int:
    """Read total stall time (microseconds) from memory.pressure PSI."""
    raw = _cgroup_read(cgroup_path, "memory.pressure")
    if raw is None:
        return 0
    # Format: "some avg10=X avg60=X avg300=X total=N\nfull ..."
    for line in raw.splitlines():
        if line.startswith("some "):
            for part in line.split():
                if part.startswith("total="):
                    try:
                        return int(part[6:])
                    except ValueError:
                        pass
    return 0


def _read_memory_current(cgroup_path: str) -> Optional[int]:
    """Read memory.current (bytes) from a cgroup."""
    raw = _cgroup_read(cgroup_path, "memory.current")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class MemcgController(ABC):
    """Unified interface for memory cgroup priority control."""

    @abstractmethod
    def attach(self, config: MemcgConfig) -> bool:
        """Start protecting HIGH and throttling LOW. Returns success."""

    @abstractmethod
    def detach(self) -> None:
        """Stop all protection, restore defaults."""

    @abstractmethod
    def poll(self) -> None:
        """Called periodically from the event loop. Handles monitoring logic."""

    @abstractmethod
    def get_stats(self) -> dict:
        """Return backend-specific statistics."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend name."""


# ---------------------------------------------------------------------------
# BPF backend — wraps memcg_priority binary
# ---------------------------------------------------------------------------

class BpfMemcgController(MemcgController):
    """Delegates to the memcg_priority eBPF program."""

    def __init__(self, binary_path: str):
        self._binary = binary_path
        self._proc: Optional[subprocess.Popen] = None
        self._config: Optional[MemcgConfig] = None

    @property
    def backend_name(self) -> str:
        return "bpf"

    def attach(self, config: MemcgConfig) -> bool:
        self._config = config
        cmd = [
            self._binary,
            "--high", config.high_cgroup,
        ]
        for low in config.low_cgroups:
            cmd += ["--low", low]
        cmd += ["--delay-ms", str(config.delay_ms)]
        cmd += ["--threshold", str(config.threshold)]
        if config.use_below_low:
            cmd.append("--below-low")

        try:
            self._proc = subprocess.Popen(cmd)
            log.info("BPF memcg started (PID %d): %s", self._proc.pid,
                     " ".join(cmd))
            return True
        except FileNotFoundError:
            log.error("BPF memcg binary not found: %s", self._binary)
            return False
        except OSError as e:
            log.error("Failed to start BPF memcg: %s", e)
            return False

    def detach(self) -> None:
        if self._proc and self._proc.poll() is None:
            log.info("Stopping BPF memcg (PID %d)", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def poll(self) -> None:
        if self._proc and self._proc.poll() is not None:
            log.warning("BPF memcg process exited unexpectedly (rc=%d)",
                        self._proc.returncode)

    def get_stats(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        return {"backend": "bpf", "running": running}


# ---------------------------------------------------------------------------
# Cgroup v2 backend — userspace fallback
# ---------------------------------------------------------------------------

class CgroupMemcgController(MemcgController):
    """
    Uses standard cgroup v2 controls to approximate BPF behavior.

    Normal state:
        HIGH  memory.low = 0          (no special protection)
        LOW   memory.high = max       (no throttling)

    Protection active (pressure detected):
        HIGH  memory.low = <large>    (kernel protects from reclaim)
        LOW   memory.high = <reduced> (kernel throttles allocations)

    Pressure detection (three signals, any one triggers protection):
      1. memory.events: 'high' counter increases (requires memory.high set)
      2. memory.pressure: PSI total stall time increases (standard kernel)
      3. memory.current: parent usage exceeds pressure_ratio of memory.max
    """

    PRESSURE_RATIO = 0.85  # activate when parent usage > 85% of limit

    def __init__(self):
        self._config: Optional[MemcgConfig] = None
        self._protection_active = False
        self._protection_start: float = 0.0
        self._last_high_events: int = 0
        self._last_psi_total: int = 0
        self._activation_count: int = 0
        self._last_trigger: str = ""
        self._known_tool_cgroups: set = set()

    @property
    def backend_name(self) -> str:
        return "cgroup"

    def attach(self, config: MemcgConfig) -> bool:
        self._config = config
        # Read baselines
        events = _read_memory_events(config.high_cgroup)
        self._last_high_events = events.get("high", 0)
        parent = os.path.dirname(config.high_cgroup)
        self._last_psi_total = _read_psi_total(parent)
        # Start in normal state
        self._set_normal()
        log.info("Cgroup memcg controller attached (fallback mode)")
        return True

    def detach(self) -> None:
        if self._config:
            self._set_normal()
            log.info("Cgroup memcg controller detached")
        self._config = None

    def poll(self) -> None:
        if not self._config:
            return

        now = time.monotonic()

        # If protection is active, check if window expired
        if self._protection_active:
            elapsed = now - self._protection_start
            if elapsed >= self._config.protection_window_s:
                self._set_normal()
                self._protection_active = False
                log.debug("Protection window expired, restored normal state")
            return  # don't re-check pressure while already protected

        pressure_detected = False
        trigger = ""

        # Signal 1: memory.events high counter (works when memory.high is set)
        events = _read_memory_events(self._config.high_cgroup)
        current_high = events.get("high", 0)
        delta = current_high - self._last_high_events
        self._last_high_events = current_high
        if delta >= self._config.threshold:
            pressure_detected = True
            trigger = f"memory.events(delta={delta})"

        # Signal 2: PSI total stall time on parent cgroup
        if not pressure_detected:
            parent = os.path.dirname(self._config.high_cgroup)
            psi_total = _read_psi_total(parent)
            psi_delta = psi_total - self._last_psi_total
            self._last_psi_total = psi_total
            if psi_delta > 0:
                pressure_detected = True
                trigger = f"psi(delta={psi_delta}us)"

        # Signal 3: parent memory.current approaching memory.max
        if not pressure_detected:
            parent = os.path.dirname(self._config.high_cgroup)
            current = _read_memory_current(parent)
            limit = self._read_memory_limit(parent)
            if current is not None and limit is not None and limit > 0:
                ratio = current / limit
                if ratio >= self.PRESSURE_RATIO:
                    pressure_detected = True
                    trigger = f"usage({ratio:.0%})"

        if pressure_detected:
            self._activate_protection()
            self._protection_active = True
            self._protection_start = now
            self._activation_count += 1
            self._last_trigger = trigger
            log.info("Memory pressure detected [%s], activating protection", trigger)

        # Scan for per-tool-call child cgroups created by bash wrapper
        self._manage_tool_cgroups()

    def get_stats(self) -> dict:
        return {
            "backend": "cgroup",
            "protection_active": self._protection_active,
            "activations": self._activation_count,
            "last_trigger": self._last_trigger,
            "known_tool_cgroups": len(self._known_tool_cgroups),
        }

    # -- internal --

    def _set_normal(self) -> None:
        """Restore normal state: no special protection, no throttling."""
        if not self._config:
            return
        _cgroup_write(self._config.high_cgroup, "memory.low", "0")
        for low in self._config.low_cgroups:
            _cgroup_write(low, "memory.high", "max")

    def _activate_protection(self) -> None:
        """Enter protection: shield HIGH from reclaim, throttle LOW."""
        if not self._config:
            return

        # Determine memory limits from parent cgroup
        parent = os.path.dirname(self._config.high_cgroup)
        total = self._read_memory_limit(parent)

        if total and total > 0:
            # Protect 80% for HIGH
            high_low = int(total * 0.8)
            # Restrict LOW to 50%
            low_high = int(total * 0.5)
        else:
            # Fallback: use a large value for protection, small for throttle
            high_low = 1 << 30  # 1 GiB
            low_high = 512 << 20  # 512 MiB

        _cgroup_write(self._config.high_cgroup, "memory.low", str(high_low))
        for low in self._config.low_cgroups:
            _cgroup_write(low, "memory.high", str(low_high))

    def _manage_tool_cgroups(self) -> None:
        """Scan for per-tool-call child cgroups and track them.

        The bash wrapper creates child cgroups under session_high for each
        tool call. This method discovers new ones for monitoring/stats.
        Stale entries (already removed by wrapper) are pruned.
        """
        if not self._config:
            return
        high_path = self._config.high_cgroup
        try:
            current = set()
            for entry in os.scandir(high_path):
                if entry.is_dir() and entry.name.startswith("tool_"):
                    current.add(entry.path)
                    if entry.path not in self._known_tool_cgroups:
                        self._known_tool_cgroups.add(entry.path)
                        log.debug("New tool cgroup: %s", entry.path)
            # Prune stale entries
            self._known_tool_cgroups &= current
        except OSError:
            pass

    @staticmethod
    def _read_memory_limit(cgroup_path: str) -> Optional[int]:
        """Read memory.max from a cgroup. Returns bytes or None."""
        raw = _cgroup_read(cgroup_path, "memory.max")
        if raw is None or raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_memcg_controller(script_dir: str) -> MemcgController:
    """Auto-detect: use BPF if binary exists, otherwise cgroup v2 fallback."""
    bpf_bin = os.path.join(script_dir, "memcg", "memcg_priority")
    if os.path.isfile(bpf_bin) and os.access(bpf_bin, os.X_OK):
        log.info("Using BPF memcg controller (%s)", bpf_bin)
        return BpfMemcgController(bpf_bin)
    log.info("BPF memcg binary not found, using cgroup v2 fallback")
    return CgroupMemcgController()

#!/usr/bin/env python3
"""
agentcgroupd - Coordinate three eBPF tools for agent cgroup management.

This daemon:
  1. Creates a cgroup hierarchy for agent sessions
  2. Starts the scx_flatcg CPU scheduler (sched_ext)
  3. Starts memcg_priority memory isolation (memcg_bpf_ops)
  4. Starts the process monitor and reacts to EXEC/EXIT events

Usage: sudo python3 agentcgroupd.py [--cgroup-root PATH] [--no-scheduler] [--no-memcg]
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from memcg_controller import MemcgConfig, create_memcg_controller

LOG_FORMAT = "[agentcgroupd] %(message)s"
log = logging.getLogger("agentcgroupd")


# ---------------------------------------------------------------------------
# Cgroup helpers
# ---------------------------------------------------------------------------

def cgroup_create(path: str) -> bool:
    """Create a cgroup directory if it doesn't exist."""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError as e:
        log.error("Failed to create cgroup %s: %s", path, e)
        return False


def cgroup_write(path: str, filename: str, value: str) -> bool:
    """Write a value to a cgroup control file."""
    filepath = os.path.join(path, filename)
    try:
        with open(filepath, "w") as f:
            f.write(value)
        return True
    except OSError as e:
        log.warning("Failed to write '%s' to %s: %s", value, filepath, e)
        return False


def cgroup_assign_pid(cgroup_path: str, pid: int) -> bool:
    """Move a process into a cgroup."""
    return cgroup_write(cgroup_path, "cgroup.procs", str(pid))


def setup_cgroup_hierarchy(root: str) -> bool:
    """Create the cgroup hierarchy with session_high and session_low.

    Enables subtree_control on session_high to allow per-tool-call
    child cgroups created by the bash wrapper.
    """
    high = os.path.join(root, "session_high")
    low = os.path.join(root, "session_low")

    if not cgroup_create(high) or not cgroup_create(low):
        return False

    # Enable controllers on root
    cgroup_write(root, "cgroup.subtree_control", "+memory +cpu")

    # Enable subtree_control on session_high for per-tool-call child cgroups
    cgroup_write(high, "cgroup.subtree_control", "+memory +cpu")

    # Set CPU weights (higher = more CPU time)
    cgroup_write(high, "cpu.weight", "150")
    cgroup_write(low, "cpu.weight", "50")

    log.info("Cgroup hierarchy ready at %s (per-tool-call subtree enabled)", root)
    return True


# ---------------------------------------------------------------------------
# Process event handling
# ---------------------------------------------------------------------------

def parse_process_event(line: str) -> Optional[dict]:
    """Parse a JSON event line from the process monitor."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        log.debug("Failed to parse JSON: %s (line: %s)", e, line[:100])
        return None


def handle_event(event: dict, cgroup_root: str) -> None:
    """React to a process monitor event.

    Note: With the bash wrapper active, per-tool-call cgroup assignment is
    handled by the wrapper itself. The daemon logs events for observability
    and can optionally adjust child cgroup limits.
    """
    event_type = event.get("event")
    pid = event.get("pid")
    comm = event.get("comm", "?")

    if event_type == "EXEC":
        # With bash wrapper, tool-call processes self-assign to child cgroups.
        # Daemon only logs the event for observability.
        log.info("EXEC: %s (%d) - tool call detected", comm, pid)

    elif event_type == "EXIT":
        duration = event.get("duration_ms")
        extra = f" (duration={duration}ms)" if duration else ""
        log.info("EXIT: %s (%s)%s", comm, pid, extra)

    elif event_type == "FILE_OPEN":
        log.debug("FILE_OPEN: %s (%s) %s", comm, pid,
                   event.get("filepath", ""))

    elif event_type == "BASH_READLINE":
        log.debug("BASH: %s (%s) %s", comm, pid,
                   event.get("command", ""))


# ---------------------------------------------------------------------------
# Subprocess management
# ---------------------------------------------------------------------------

class SubprocessManager:
    """Manage child processes with graceful shutdown."""

    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}

    def start(self, name: str, cmd: list[str], **kwargs) -> Optional[subprocess.Popen]:
        """Start a named subprocess."""
        try:
            proc = subprocess.Popen(cmd, **kwargs)
            self._procs[name] = proc
            log.info("Started %s (PID %d): %s", name, proc.pid,
                     " ".join(cmd))
            return proc
        except FileNotFoundError:
            log.error("%s binary not found: %s", name, cmd[0])
            return None
        except OSError as e:
            log.error("Failed to start %s: %s", name, e)
            return None

    def check_health(self) -> list[str]:
        """Return names of processes that have died unexpectedly."""
        dead = []
        for name, proc in self._procs.items():
            if proc.poll() is not None:
                dead.append(name)
        return dead

    def stop_all(self):
        """Gracefully stop all child processes."""
        for name, proc in self._procs.items():
            if proc.poll() is None:
                log.info("Stopping %s (PID %d)", name, proc.pid)
                proc.terminate()

        # Wait for graceful shutdown
        for name, proc in self._procs.items():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("Force killing %s (PID %d)", name, proc.pid)
                proc.kill()

        self._procs.clear()


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

class AgentCGroupDaemon:
    """Main daemon coordinating three eBPF tools."""

    def __init__(self, cgroup_root: str, script_dir: str,
                 enable_scheduler: bool = True,
                 enable_memcg: bool = True,
                 process_commands: str = "python,bash,pytest,node,npm"):
        self.cgroup_root = cgroup_root
        self.script_dir = script_dir
        self.enable_scheduler = enable_scheduler
        self.enable_memcg = enable_memcg
        self.process_commands = process_commands
        self.manager = SubprocessManager()
        self.memcg = None
        self._running = True

    def _signal_handler(self, signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        self._running = False

    def scan_tool_cgroups(self) -> list:
        """Scan session_high for per-tool-call child cgroups created by bash wrapper."""
        high_path = os.path.join(self.cgroup_root, "session_high")
        tool_cgroups = []
        try:
            for entry in os.scandir(high_path):
                if entry.is_dir() and entry.name.startswith("tool_"):
                    tool_cgroups.append(entry.path)
        except OSError:
            pass
        return tool_cgroups

    def _bin_path(self, *parts: str) -> str:
        return os.path.join(self.script_dir, *parts)

    def start(self) -> int:
        """Start the daemon. Returns exit code."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Step 1: Create cgroup hierarchy
        if not setup_cgroup_hierarchy(self.cgroup_root):
            return 1

        # Step 2: Start CPU scheduler
        if self.enable_scheduler:
            sched_bin = self._bin_path("scheduler", "scx_flatcg")
            if not self.manager.start("scheduler",
                                       [sched_bin, "-i", "5"]):
                log.warning("Scheduler failed to start, continuing without it")

        # Step 3: Start memory isolation (auto-detects BPF vs cgroup fallback)
        if self.enable_memcg:
            self.memcg = create_memcg_controller(self.script_dir)
            config = MemcgConfig(
                high_cgroup=os.path.join(self.cgroup_root, "session_high"),
                low_cgroups=[os.path.join(self.cgroup_root, "session_low")],
            )
            if not self.memcg.attach(config):
                log.warning("Memcg (%s) failed to attach, continuing without it",
                            self.memcg.backend_name)
                self.memcg = None
            else:
                log.info("Memcg active: backend=%s", self.memcg.backend_name)

        # Brief delay for BPF programs to attach
        time.sleep(1)

        # Step 4: Start process monitor
        proc_bin = self._bin_path("process", "process")
        proc = self.manager.start("process", [
            proc_bin, "-m", "2", "-c", self.process_commands,
        ], stdout=subprocess.PIPE, text=True)

        if not proc:
            log.error("Process monitor failed to start")
            self.manager.stop_all()
            return 1

        log.info("All components started. Press Ctrl+C to stop.")

        # Step 5: Event loop
        return self._event_loop(proc)

    def _event_loop(self, proc: subprocess.Popen) -> int:
        """Read process monitor output and react to events."""
        health_check_interval = 5.0
        last_health_check = time.monotonic()

        while self._running:
            # Read one line from process monitor (non-blocking via timeout)
            try:
                line = proc.stdout.readline()
            except (IOError, ValueError):
                break

            if line:
                event = parse_process_event(line)
                if event:
                    handle_event(event, self.cgroup_root)

            # Poll memcg controller (pressure detection for cgroup backend)
            if self.memcg:
                self.memcg.poll()

            # Periodic health check
            now = time.monotonic()
            if now - last_health_check >= health_check_interval:
                last_health_check = now
                dead = self.manager.check_health()
                for name in dead:
                    log.warning("%s has exited unexpectedly", name)

            # If process monitor died, exit
            if proc.poll() is not None and not line:
                if self._running:
                    log.error("Process monitor exited unexpectedly")
                break

        if self.memcg:
            self.memcg.detach()
        self.manager.stop_all()
        log.info("Shutdown complete")
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Coordinate eBPF tools for agent cgroup management")
    parser.add_argument("--cgroup-root", default="/sys/fs/cgroup/agentcg",
                        help="Root cgroup path (default: /sys/fs/cgroup/agentcg)")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="Don't start the CPU scheduler")
    parser.add_argument("--no-memcg", action="store_true",
                        help="Don't start memory isolation")
    parser.add_argument("--commands", default="python,bash,pytest,node,npm",
                        help="Comma-separated commands to monitor")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(format=LOG_FORMAT,
                        level=logging.DEBUG if args.verbose else logging.INFO)

    if os.geteuid() != 0:
        log.error("Must run as root")
        sys.exit(1)

    script_dir = str(Path(__file__).resolve().parent)

    daemon = AgentCGroupDaemon(
        cgroup_root=args.cgroup_root,
        script_dir=script_dir,
        enable_scheduler=not args.no_scheduler,
        enable_memcg=not args.no_memcg,
        process_commands=args.commands,
    )

    sys.exit(daemon.start())


if __name__ == "__main__":
    main()

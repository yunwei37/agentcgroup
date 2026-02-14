#!/usr/bin/env python3
"""
Integration test: Feed real agent traces through CgroupMemcgController.

Uses tmpdir to simulate cgroup filesystem — no root, no BPF required.
Validates that the state machine correctly detects pressure and activates
protection when HIGH and LOW sessions compete for memory.

Usage: python3 test_integration.py [-v]
"""

import json
import os
import sys
import tempfile
import time
import unittest

from memcg_controller import MemcgConfig, CgroupMemcgController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_trace(trace_path: str) -> list[dict]:
    """Load resources.json trace file, return [{epoch, mem_bytes, cpu_percent}]."""
    with open(trace_path) as f:
        data = json.load(f)

    samples = []
    for s in data.get("samples", []):
        mem_str = s.get("mem_usage", "0MB").split("/")[0].strip()
        if "GB" in mem_str:
            mem_bytes = int(float(mem_str.replace("GB", "")) * 1024**3)
        elif "MB" in mem_str:
            mem_bytes = int(float(mem_str.replace("MB", "")) * 1024**2)
        elif "kB" in mem_str:
            mem_bytes = int(float(mem_str.replace("kB", "")) * 1024)
        else:
            mem_bytes = 0

        samples.append({
            "epoch": s.get("epoch", 0),
            "mem_bytes": mem_bytes,
        })
    return samples


def write_memory_events(path: str, high: int = 0, low: int = 0,
                        max_: int = 0, oom: int = 0):
    """Write a simulated memory.events file."""
    with open(os.path.join(path, "memory.events"), "w") as f:
        f.write(f"low {low}\nhigh {high}\nmax {max_}\noom {oom}\noom_kill 0\n")


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Test: Pressure-driven protection with real traces
# ---------------------------------------------------------------------------

class TestTraceReplayIntegration(unittest.TestCase):
    """
    Simulate two competing agent sessions using real trace data.

    Scenario:
      - HIGH session: dask__dask-11628 (peak 321MB, 95 samples over 98s)
      - LOW session: sigmavirus24__github3.py-673 (or same trace)
      - Total memory limit: 512MB (forces contention)

    We simulate the memory.events counters as if the kernel were reporting
    pressure events when combined memory exceeds the limit.
    """

    TRACES_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "experiments", "all_images_haiku"
    )

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_integ_")
        self.root = os.path.join(self.tmpdir, "agentcg")
        self.high = os.path.join(self.root, "session_high")
        self.low = os.path.join(self.root, "session_low")
        os.makedirs(self.high)
        os.makedirs(self.low)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _find_trace(self, name: str) -> str:
        """Find a trace by experiment name."""
        path = os.path.join(self.TRACES_DIR, name, "attempt_1", "resources.json")
        if os.path.exists(path):
            return path
        return ""

    def test_pressure_activates_protection(self):
        """Simulate memory pressure scenario and verify controller response."""
        # Setup: total memory 512MB
        total_mem = 512 * 1024 * 1024  # 512MB
        with open(os.path.join(self.root, "memory.max"), "w") as f:
            f.write(str(total_mem))

        # Initialize controller with short protection window for test speed
        config = MemcgConfig(
            high_cgroup=self.high,
            low_cgroups=[self.low],
            threshold=1,
            protection_window_s=0.5,
        )
        write_memory_events(self.high, high=0)

        ctrl = CgroupMemcgController()
        ctrl.attach(config)

        # Verify initial state: normal
        self.assertEqual(read_file(os.path.join(self.high, "memory.low")), "0")
        self.assertEqual(read_file(os.path.join(self.low, "memory.high")), "max")

        # --- Simulate contention ---
        # Time step 1: Both sessions using moderate memory, no pressure
        write_memory_events(self.high, high=0)
        ctrl.poll()
        self.assertFalse(ctrl._protection_active)

        # Time step 2: HIGH experiences memory pressure (high events increase)
        # This simulates: combined mem > limit, kernel starts throttling HIGH
        write_memory_events(self.high, high=5)
        ctrl.poll()

        # Protection should now be active
        self.assertTrue(ctrl._protection_active)
        stats = ctrl.get_stats()
        self.assertEqual(stats["activations"], 1)

        # Verify protection values:
        # HIGH memory.low = 80% of 512MB = ~409MB
        high_low = int(read_file(os.path.join(self.high, "memory.low")))
        self.assertGreater(high_low, 400 * 1024 * 1024)
        self.assertLess(high_low, 420 * 1024 * 1024)

        # LOW memory.high = 50% of 512MB = 256MB
        low_high = int(read_file(os.path.join(self.low, "memory.high")))
        self.assertGreater(low_high, 250 * 1024 * 1024)
        self.assertLess(low_high, 260 * 1024 * 1024)

        print(f"\n  Protection activated:")
        print(f"    HIGH memory.low  = {high_low / (1024*1024):.0f} MB (protects from reclaim)")
        print(f"    LOW  memory.high = {low_high / (1024*1024):.0f} MB (throttles allocations)")

        # Time step 3: Wait for protection to expire
        time.sleep(0.6)
        ctrl.poll()
        self.assertFalse(ctrl._protection_active)
        self.assertEqual(read_file(os.path.join(self.high, "memory.low")), "0")
        self.assertEqual(read_file(os.path.join(self.low, "memory.high")), "max")
        print(f"    Protection expired → restored normal state")

        ctrl.detach()

    def test_trace_driven_pressure_timeline(self):
        """
        Drive the controller with a timeline of simulated pressure events
        derived from real trace memory usage patterns.
        """
        trace_path = self._find_trace("dask__dask-11628")
        if not trace_path:
            self.skipTest("Trace dask__dask-11628 not found")

        samples = load_trace(trace_path)
        self.assertGreater(len(samples), 10, "Need at least 10 samples")

        # Setup
        total_mem = 512 * 1024 * 1024
        with open(os.path.join(self.root, "memory.max"), "w") as f:
            f.write(str(total_mem))

        config = MemcgConfig(
            high_cgroup=self.high,
            low_cgroups=[self.low],
            threshold=1,
            protection_window_s=0.05,  # 50ms for fast test
        )
        write_memory_events(self.high, high=0)
        ctrl = CgroupMemcgController()
        ctrl.attach(config)

        # Simulate: when HIGH mem + LOW mem (assume 200MB constant) > total_mem,
        # the kernel would increment memory.events high counter
        low_mem_constant = 200 * 1024 * 1024  # LOW session uses 200MB
        high_events_counter = 0
        protection_activations = []
        timeline = []

        for i, sample in enumerate(samples):
            high_mem = sample["mem_bytes"]
            combined = high_mem + low_mem_constant

            # Simulate: if combined > total_mem, kernel increments 'high' events
            if combined > total_mem:
                high_events_counter += 1
                write_memory_events(self.high, high=high_events_counter)

            was_active = ctrl._protection_active
            ctrl.poll()
            is_active = ctrl._protection_active

            if is_active and not was_active:
                protection_activations.append({
                    "sample": i,
                    "high_mem_mb": high_mem / (1024 * 1024),
                    "combined_mb": combined / (1024 * 1024),
                })

            timeline.append({
                "sample": i,
                "high_mem_mb": high_mem / (1024 * 1024),
                "combined_mb": combined / (1024 * 1024),
                "protection": is_active,
            })

            # Small delay to let protection windows expire between polls
            time.sleep(0.01)

        ctrl.detach()

        stats = ctrl.get_stats()

        # Print summary
        mem_values = [s["mem_bytes"] for s in samples]
        print(f"\n  Trace: dask__dask-11628")
        print(f"    Samples: {len(samples)}")
        print(f"    HIGH mem: min={min(mem_values)/(1024**2):.0f}MB "
              f"avg={sum(mem_values)/len(mem_values)/(1024**2):.0f}MB "
              f"max={max(mem_values)/(1024**2):.0f}MB")
        print(f"    LOW mem: constant {low_mem_constant/(1024**2):.0f}MB")
        print(f"    Total limit: {total_mem/(1024**2):.0f}MB")
        print(f"    Pressure events: {high_events_counter}")
        print(f"    Protection activations: {stats['activations']}")

        if protection_activations:
            print(f"    First activation at sample {protection_activations[0]['sample']}: "
                  f"HIGH={protection_activations[0]['high_mem_mb']:.0f}MB, "
                  f"combined={protection_activations[0]['combined_mb']:.0f}MB")

        # Verify: protection should have activated at least once
        # (dask trace peaks at 321MB + 200MB LOW = 521MB > 512MB)
        self.assertGreater(stats["activations"], 0,
                           "Protection should activate when combined memory exceeds limit")
        # Verify: not ALL samples trigger protection (there are low-memory periods too)
        protected_count = sum(1 for t in timeline if t["protection"])
        self.assertLess(protected_count, len(timeline),
                        "Protection should not be active for all samples")

    def test_no_pressure_no_activation(self):
        """When traces don't cause contention, no protection should activate."""
        total_mem = 2 * 1024 * 1024 * 1024  # 2GB — plenty of room
        with open(os.path.join(self.root, "memory.max"), "w") as f:
            f.write(str(total_mem))

        config = MemcgConfig(
            high_cgroup=self.high,
            low_cgroups=[self.low],
            threshold=1,
            protection_window_s=0.05,
        )
        write_memory_events(self.high, high=0)
        ctrl = CgroupMemcgController()
        ctrl.attach(config)

        # Simulate 20 polls with no pressure (counter stays 0)
        for _ in range(20):
            ctrl.poll()
            time.sleep(0.01)

        stats = ctrl.get_stats()
        self.assertEqual(stats["activations"], 0)
        self.assertFalse(ctrl._protection_active)
        ctrl.detach()


class TestMultiLowCgroup(unittest.TestCase):
    """Test protection with multiple LOW cgroups."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_multi_")
        self.root = os.path.join(self.tmpdir, "agentcg")
        self.high = os.path.join(self.root, "session_high")
        self.low1 = os.path.join(self.root, "session_low_1")
        self.low2 = os.path.join(self.root, "session_low_2")
        os.makedirs(self.high)
        os.makedirs(self.low1)
        os.makedirs(self.low2)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_low_cgroups_throttled(self):
        """When pressure is detected, ALL low cgroups should be throttled."""
        total_mem = 1024 * 1024 * 1024  # 1GB
        with open(os.path.join(self.root, "memory.max"), "w") as f:
            f.write(str(total_mem))

        config = MemcgConfig(
            high_cgroup=self.high,
            low_cgroups=[self.low1, self.low2],
            threshold=1,
            protection_window_s=0.5,
        )
        write_memory_events(self.high, high=0)

        ctrl = CgroupMemcgController()
        ctrl.attach(config)

        # Trigger pressure
        write_memory_events(self.high, high=3)
        ctrl.poll()

        self.assertTrue(ctrl._protection_active)

        # Both LOW cgroups should have memory.high set
        for low_path in [self.low1, self.low2]:
            low_high = int(read_file(os.path.join(low_path, "memory.high")))
            expected = int(total_mem * 0.5)
            self.assertEqual(low_high, expected,
                             f"LOW cgroup {low_path} should be throttled")

        print(f"\n  Multi-LOW test: both LOW cgroups throttled to "
              f"{int(total_mem*0.5)/(1024**2):.0f}MB")

        ctrl.detach()

        # After detach, all should be restored
        for low_path in [self.low1, self.low2]:
            self.assertEqual(read_file(os.path.join(low_path, "memory.high")), "max")


if __name__ == "__main__":
    unittest.main()

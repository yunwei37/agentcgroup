#!/usr/bin/env python3
"""Tests for agentcgroupd - no root required, no eBPF required."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from agentcgroupd import (
    parse_process_event,
    handle_event,
    cgroup_create,
    cgroup_write,
    cgroup_assign_pid,
    setup_cgroup_hierarchy,
    SubprocessManager,
    AgentCGroupDaemon,
)


class TestParseProcessEvent(unittest.TestCase):
    """Test JSON event parsing from process monitor output."""

    def test_valid_exec_event(self):
        line = '{"timestamp":123,"event":"EXEC","comm":"python","pid":1234,"ppid":1}'
        event = parse_process_event(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "EXEC")
        self.assertEqual(event["pid"], 1234)
        self.assertEqual(event["comm"], "python")

    def test_valid_exit_event(self):
        line = '{"timestamp":456,"event":"EXIT","comm":"bash","pid":5678,"ppid":1,"exit_code":0,"duration_ms":1500}'
        event = parse_process_event(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "EXIT")
        self.assertEqual(event["duration_ms"], 1500)

    def test_valid_file_open_event(self):
        line = '{"timestamp":789,"event":"FILE_OPEN","comm":"python","pid":1234,"filepath":"/tmp/test","flags":0,"count":1}'
        event = parse_process_event(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "FILE_OPEN")

    def test_empty_line(self):
        self.assertIsNone(parse_process_event(""))
        self.assertIsNone(parse_process_event("  \n"))

    def test_invalid_json(self):
        self.assertIsNone(parse_process_event("not json"))
        self.assertIsNone(parse_process_event("{broken"))

    def test_whitespace_stripping(self):
        line = '  {"event":"EXEC","pid":1}  \n'
        event = parse_process_event(line)
        self.assertIsNotNone(event)
        self.assertEqual(event["event"], "EXEC")


class TestCgroupHelpers(unittest.TestCase):
    """Test cgroup filesystem operations using a temp directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cgroup_create(self):
        path = os.path.join(self.tmpdir, "test_cg")
        self.assertTrue(cgroup_create(path))
        self.assertTrue(os.path.isdir(path))

    def test_cgroup_create_nested(self):
        path = os.path.join(self.tmpdir, "a", "b", "c")
        self.assertTrue(cgroup_create(path))
        self.assertTrue(os.path.isdir(path))

    def test_cgroup_create_idempotent(self):
        path = os.path.join(self.tmpdir, "test_cg")
        self.assertTrue(cgroup_create(path))
        self.assertTrue(cgroup_create(path))  # second call should not fail

    def test_cgroup_write(self):
        """Test writing to a regular file (simulates cgroup control file)."""
        self.assertTrue(cgroup_write(self.tmpdir, "test_file", "hello"))
        with open(os.path.join(self.tmpdir, "test_file")) as f:
            self.assertEqual(f.read(), "hello")

    def test_cgroup_write_nonexistent_dir(self):
        self.assertFalse(cgroup_write("/nonexistent/path", "file", "val"))

    def test_cgroup_assign_pid(self):
        """Test PID assignment writes to cgroup.procs."""
        self.assertTrue(cgroup_assign_pid(self.tmpdir, 12345))
        with open(os.path.join(self.tmpdir, "cgroup.procs")) as f:
            self.assertEqual(f.read(), "12345")

    def test_setup_cgroup_hierarchy(self):
        """Test full hierarchy creation."""
        root = os.path.join(self.tmpdir, "agentcg")
        self.assertTrue(setup_cgroup_hierarchy(root))
        self.assertTrue(os.path.isdir(os.path.join(root, "session_high")))
        self.assertTrue(os.path.isdir(os.path.join(root, "session_low")))

        # Check CPU weights were written
        with open(os.path.join(root, "session_high", "cpu.weight")) as f:
            self.assertEqual(f.read(), "150")
        with open(os.path.join(root, "session_low", "cpu.weight")) as f:
            self.assertEqual(f.read(), "50")


class TestHandleEvent(unittest.TestCase):
    """Test event handling logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_test_")
        self.cgroup_root = os.path.join(self.tmpdir, "agentcg")
        setup_cgroup_hierarchy(self.cgroup_root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_exec_event_assigns_cgroup(self):
        """EXEC event should write PID to session_high/cgroup.procs."""
        event = {"event": "EXEC", "pid": 9999, "comm": "python"}
        handle_event(event, self.cgroup_root)

        procs_file = os.path.join(self.cgroup_root, "session_high", "cgroup.procs")
        with open(procs_file) as f:
            self.assertEqual(f.read(), "9999")

    def test_exec_event_multiple_pids(self):
        """Multiple EXEC events should overwrite cgroup.procs (like real cgroup)."""
        handle_event({"event": "EXEC", "pid": 100, "comm": "a"}, self.cgroup_root)
        handle_event({"event": "EXEC", "pid": 200, "comm": "b"}, self.cgroup_root)

        # In a real cgroup, both PIDs would be present.
        # In our test with regular files, only the last write is visible.
        procs_file = os.path.join(self.cgroup_root, "session_high", "cgroup.procs")
        self.assertTrue(os.path.exists(procs_file))

    def test_exit_event_no_crash(self):
        """EXIT event should not crash."""
        event = {"event": "EXIT", "pid": 9999, "comm": "python",
                 "exit_code": 0, "duration_ms": 500}
        handle_event(event, self.cgroup_root)  # should not raise

    def test_unknown_event_no_crash(self):
        """Unknown event types should be handled gracefully."""
        event = {"event": "UNKNOWN_TYPE", "pid": 1}
        handle_event(event, self.cgroup_root)  # should not raise

    def test_missing_fields_no_crash(self):
        """Events with missing fields should not crash."""
        handle_event({"event": "EXEC"}, self.cgroup_root)
        handle_event({}, self.cgroup_root)


class TestSubprocessManager(unittest.TestCase):
    """Test subprocess lifecycle management."""

    def test_start_valid_command(self):
        mgr = SubprocessManager()
        proc = mgr.start("test", ["echo", "hello"])
        self.assertIsNotNone(proc)
        proc.wait()
        mgr.stop_all()

    def test_start_invalid_command(self):
        mgr = SubprocessManager()
        proc = mgr.start("test", ["/nonexistent/binary"])
        self.assertIsNone(proc)

    def test_check_health_running(self):
        mgr = SubprocessManager()
        mgr.start("sleeper", ["sleep", "10"])
        dead = mgr.check_health()
        self.assertEqual(dead, [])
        mgr.stop_all()

    def test_check_health_dead(self):
        mgr = SubprocessManager()
        proc = mgr.start("fast", ["true"])
        proc.wait()  # wait for it to finish
        dead = mgr.check_health()
        self.assertIn("fast", dead)

    def test_stop_all(self):
        mgr = SubprocessManager()
        mgr.start("s1", ["sleep", "60"])
        mgr.start("s2", ["sleep", "60"])
        mgr.stop_all()
        # After stop_all, all processes should be terminated
        self.assertEqual(len(mgr._procs), 0)


class TestEventLoopIntegration(unittest.TestCase):
    """Integration test: feed JSON lines through a pipe, verify cgroup assignment."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_test_")
        self.cgroup_root = os.path.join(self.tmpdir, "agentcg")
        setup_cgroup_hierarchy(self.cgroup_root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stream_of_events(self):
        """Simulate a stream of JSON events and verify correct handling."""
        events = [
            {"event": "EXEC", "pid": 1001, "comm": "python", "ppid": 1,
             "filename": "/usr/bin/python3"},
            {"event": "EXEC", "pid": 1002, "comm": "bash", "ppid": 1001,
             "filename": "/usr/bin/bash"},
            {"event": "FILE_OPEN", "pid": 1001, "comm": "python",
             "filepath": "/tmp/test.py", "flags": 0, "count": 1},
            {"event": "EXIT", "pid": 1002, "comm": "bash", "ppid": 1001,
             "exit_code": 0, "duration_ms": 100},
            {"event": "EXIT", "pid": 1001, "comm": "python", "ppid": 1,
             "exit_code": 0, "duration_ms": 5000},
        ]

        for evt in events:
            line = json.dumps(evt)
            parsed = parse_process_event(line)
            self.assertIsNotNone(parsed)
            handle_event(parsed, self.cgroup_root)

        # Verify cgroup.procs was written
        procs_file = os.path.join(self.cgroup_root, "session_high",
                                  "cgroup.procs")
        self.assertTrue(os.path.exists(procs_file))


# ---------------------------------------------------------------------------
# MemcgController tests
# ---------------------------------------------------------------------------

from memcg_controller import (
    MemcgConfig,
    CgroupMemcgController,
    BpfMemcgController,
    create_memcg_controller,
    _read_memory_events,
)


class TestCgroupMemcgController(unittest.TestCase):
    """Test cgroup v2 fallback memory controller using tmpdir."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agentcg_memcg_test_")
        self.high = os.path.join(self.tmpdir, "session_high")
        self.low = os.path.join(self.tmpdir, "session_low")
        os.makedirs(self.high)
        os.makedirs(self.low)
        self.config = MemcgConfig(
            high_cgroup=self.high,
            low_cgroups=[self.low],
            threshold=1,
            protection_window_s=0.1,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_memory_events(self, path, high_count):
        """Write a simulated memory.events file."""
        with open(os.path.join(path, "memory.events"), "w") as f:
            f.write(f"low 0\nhigh {high_count}\nmax 0\noom 0\noom_kill 0\n")

    def test_attach_writes_initial_values(self):
        """attach() should write memory.low=0 on HIGH and memory.high=max on LOW."""
        self._write_memory_events(self.high, 0)
        ctrl = CgroupMemcgController()
        self.assertTrue(ctrl.attach(self.config))

        with open(os.path.join(self.high, "memory.low")) as f:
            self.assertEqual(f.read(), "0")
        with open(os.path.join(self.low, "memory.high")) as f:
            self.assertEqual(f.read(), "max")

    def test_poll_no_pressure(self):
        """poll() with no pressure change should keep normal state."""
        self._write_memory_events(self.high, 5)
        ctrl = CgroupMemcgController()
        ctrl.attach(self.config)

        # Poll without changing memory.events - no pressure
        ctrl.poll()

        self.assertFalse(ctrl._protection_active)
        stats = ctrl.get_stats()
        self.assertEqual(stats["activations"], 0)

    def test_poll_detects_pressure(self):
        """poll() should detect pressure when memory.events high counter increases."""
        self._write_memory_events(self.high, 0)
        ctrl = CgroupMemcgController()
        ctrl.attach(self.config)

        # Simulate pressure: increase 'high' counter
        self._write_memory_events(self.high, 5)
        # Write memory.max on parent so _activate_protection can read it
        with open(os.path.join(self.tmpdir, "memory.max"), "w") as f:
            f.write("1073741824")  # 1 GiB

        ctrl.poll()

        self.assertTrue(ctrl._protection_active)
        stats = ctrl.get_stats()
        self.assertEqual(stats["activations"], 1)

        # Verify protection values were written
        with open(os.path.join(self.high, "memory.low")) as f:
            val = int(f.read())
            self.assertGreater(val, 0)  # should be 80% of 1 GiB
        with open(os.path.join(self.low, "memory.high")) as f:
            val = int(f.read())
            self.assertGreater(val, 0)  # should be 50% of 1 GiB
            self.assertLess(val, 1073741824)

    def test_protection_expires(self):
        """Protection window should expire and restore normal state."""
        self._write_memory_events(self.high, 0)
        ctrl = CgroupMemcgController()
        ctrl.attach(self.config)

        # Trigger protection
        self._write_memory_events(self.high, 5)
        with open(os.path.join(self.tmpdir, "memory.max"), "w") as f:
            f.write("1073741824")
        ctrl.poll()
        self.assertTrue(ctrl._protection_active)

        # Wait for protection window to expire (0.1s)
        import time
        time.sleep(0.15)

        ctrl.poll()
        self.assertFalse(ctrl._protection_active)

        # Verify normal state restored
        with open(os.path.join(self.high, "memory.low")) as f:
            self.assertEqual(f.read(), "0")
        with open(os.path.join(self.low, "memory.high")) as f:
            self.assertEqual(f.read(), "max")

    def test_detach_restores_defaults(self):
        """detach() should restore memory.low=0 and memory.high=max."""
        self._write_memory_events(self.high, 0)
        ctrl = CgroupMemcgController()
        ctrl.attach(self.config)

        # Trigger protection first
        self._write_memory_events(self.high, 5)
        with open(os.path.join(self.tmpdir, "memory.max"), "w") as f:
            f.write("1073741824")
        ctrl.poll()

        # Now detach
        ctrl.detach()

        with open(os.path.join(self.high, "memory.low")) as f:
            self.assertEqual(f.read(), "0")
        with open(os.path.join(self.low, "memory.high")) as f:
            self.assertEqual(f.read(), "max")

    def test_get_stats(self):
        """get_stats() should return correct backend and counters."""
        ctrl = CgroupMemcgController()
        stats = ctrl.get_stats()
        self.assertEqual(stats["backend"], "cgroup")
        self.assertFalse(stats["protection_active"])
        self.assertEqual(stats["activations"], 0)


class TestBpfMemcgController(unittest.TestCase):
    """Test BPF memory controller (mocked subprocess)."""

    def test_attach_starts_process(self):
        """attach() should start the BPF binary with correct arguments."""
        with patch("memcg_controller.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            ctrl = BpfMemcgController("/fake/memcg_priority")
            config = MemcgConfig(
                high_cgroup="/sys/fs/cgroup/high",
                low_cgroups=["/sys/fs/cgroup/low"],
                delay_ms=50,
                threshold=1,
                use_below_low=True,
            )
            self.assertTrue(ctrl.attach(config))

            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd[0], "/fake/memcg_priority")
            self.assertIn("--high", cmd)
            self.assertIn("--low", cmd)
            self.assertIn("--delay-ms", cmd)
            self.assertIn("--below-low", cmd)

    def test_attach_binary_missing(self):
        """attach() should return False if binary not found."""
        ctrl = BpfMemcgController("/nonexistent/memcg_priority")
        config = MemcgConfig(high_cgroup="/fake/high")
        self.assertFalse(ctrl.attach(config))

    def test_detach_stops_process(self):
        """detach() should terminate the subprocess."""
        with patch("memcg_controller.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.poll.return_value = None  # still running
            mock_popen.return_value = mock_proc

            ctrl = BpfMemcgController("/fake/memcg_priority")
            config = MemcgConfig(high_cgroup="/fake/high")
            ctrl.attach(config)

            ctrl.detach()
            mock_proc.terminate.assert_called_once()


class TestAutoDetection(unittest.TestCase):
    """Test create_memcg_controller() auto-detection logic."""

    def test_selects_bpf_when_available(self):
        """Should return BpfMemcgController when binary exists and is executable."""
        tmpdir = tempfile.mkdtemp(prefix="agentcg_detect_test_")
        try:
            memcg_dir = os.path.join(tmpdir, "memcg")
            os.makedirs(memcg_dir)
            binary = os.path.join(memcg_dir, "memcg_priority")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(binary, 0o755)

            ctrl = create_memcg_controller(tmpdir)
            self.assertIsInstance(ctrl, BpfMemcgController)
            self.assertEqual(ctrl.backend_name, "bpf")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_falls_back_to_cgroup(self):
        """Should return CgroupMemcgController when binary doesn't exist."""
        tmpdir = tempfile.mkdtemp(prefix="agentcg_detect_test_")
        try:
            ctrl = create_memcg_controller(tmpdir)
            self.assertIsInstance(ctrl, CgroupMemcgController)
            self.assertEqual(ctrl.backend_name, "cgroup")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

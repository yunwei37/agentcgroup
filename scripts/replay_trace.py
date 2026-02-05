#!/usr/bin/env python3
"""
Trace Replay Tool for SWE-bench Experiments

Replays ALL tool calls from a Claude Code trace file in a container,
strictly matching the original timing, and records CPU/memory usage.

Supported tools:
- Bash: execute commands
- Read: read files
- Edit: apply file edits
- Write: write files
- Glob: search for files
- Grep: search in files

Usage:
    python scripts/replay_trace.py experiments/batch_swebench_18tasks/Web_Network_Easy/attempt_1
"""

import argparse
import json
import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from run_swebench import ResourceMonitor
from plot_resources import plot_from_attempt_dir


class TraceParser:
    """Parse Claude Code trace files to extract ALL tool calls."""

    # Tools we can replay
    REPLAYABLE_TOOLS = {'Bash', 'Read', 'Edit', 'Write', 'Glob', 'Grep'}

    def __init__(self, trace_file: Path):
        self.trace_file = trace_file
        self.tool_calls: List[Dict] = []
        self.start_timestamp: Optional[str] = None

    def parse(self) -> List[Dict]:
        """Parse trace file and extract all replayable tool calls."""
        tool_uses = {}  # tool_use_id -> tool info

        with open(self.trace_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    self._process_entry(entry, tool_uses)
                except json.JSONDecodeError:
                    continue

        # Convert to list and sort by timestamp
        self.tool_calls = list(tool_uses.values())
        self.tool_calls.sort(key=lambda x: x.get('timestamp', ''))

        if self.tool_calls:
            self.start_timestamp = self.tool_calls[0].get('timestamp')

        return self.tool_calls

    def _process_entry(self, entry: dict, tool_uses: dict):
        """Process a single trace entry."""
        entry_type = entry.get('type')

        if entry_type == 'assistant' and 'message' in entry:
            msg = entry['message']
            if 'content' in msg:
                for block in msg['content']:
                    if block.get('type') == 'tool_use':
                        tool_name = block.get('name')
                        if tool_name in self.REPLAYABLE_TOOLS:
                            tool_id = block.get('id')
                            tool_uses[tool_id] = {
                                'tool_use_id': tool_id,
                                'tool': tool_name,
                                'timestamp': entry.get('timestamp'),
                                'input': block.get('input', {}),
                            }


class TraceReplayer:
    """Replay all tool calls in a container with timing."""

    def __init__(self, image_name: str, tool_calls: List[Dict],
                 output_dir: Path, speed: float = 1.0, no_delay: bool = False,
                 task_name: str = ""):
        self.image_name = image_name
        self.tool_calls = tool_calls
        self.output_dir = output_dir
        self.speed = speed
        self.no_delay = no_delay
        self.task_name = task_name
        self.home = Path.home()
        self.container_id: Optional[str] = None
        self.fixed_image_name: Optional[str] = None
        self.replay_tool_calls: List[Dict] = []

    def run(self) -> dict:
        """Run the replay."""
        start_time = time.time()
        results = {
            "image": self.image_name,
            "start_time": datetime.now().isoformat(),
            "tool_call_count": len(self.tool_calls),
            "speed": self.speed,
            "no_delay": self.no_delay,
            "task_name": self.task_name,
        }

        resource_data = None
        try:
            print(f"[1/6] Setting up container for image: {self.image_name}")
            self._setup_container()

            # Collect image size
            print(f"[2/6] Collecting image and disk info...")
            results["image_info"] = self._get_image_info()
            results["disk_usage_before"] = self._get_disk_usage()
            print(f"  Image size: {results['image_info'].get('size_mb', 'N/A')} MB")
            print(f"  Disk usage (/testbed): {results['disk_usage_before'].get('testbed_mb', 'N/A')} MB")

            print(f"[3/6] Starting resource monitoring...")
            monitor = ResourceMonitor(self.container_id, interval=1.0)
            monitor.start()

            print(f"[4/6] Replaying {len(self.tool_calls)} tool calls (speed: {self.speed}x)...")
            replay_start = time.time()
            replay_results = self._replay_all(replay_start)
            results["replay_results"] = replay_results

            print(f"[5/6] Collecting results...")
            monitor.stop()

            # Collect disk usage after replay
            results["disk_usage_after"] = self._get_disk_usage()
            print(f"  Disk usage after (/testbed): {results['disk_usage_after'].get('testbed_mb', 'N/A')} MB")

            resource_data = {
                "samples": monitor.samples,
                "summary": monitor.get_summary()
            }
            results["resource_samples"] = resource_data

            summary = resource_data["summary"]
            print(f"  Collected {len(monitor.samples)} resource samples")
            print(f"  Memory: avg={summary['memory_mb']['avg']:.1f}MB, max={summary['memory_mb']['max']:.1f}MB")
            print(f"  CPU: avg={summary['cpu_percent']['avg']:.1f}%, max={summary['cpu_percent']['max']:.1f}%")

            print(f"[6/6] Saving results and generating plot...")
            self._save_results(results, resource_data)
            self._generate_plot()

        except Exception as e:
            results["error"] = str(e)
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._cleanup()

        results["total_time"] = time.time() - start_time
        results["end_time"] = datetime.now().isoformat()

        return results

    def _get_image_info(self) -> dict:
        """Get Docker image size and info."""
        info = {}
        try:
            # Get image size using podman inspect
            result = subprocess.run(
                ["podman", "image", "inspect", self.fixed_image_name, "--format", "{{.Size}}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                size_bytes = int(result.stdout.strip())
                info["size_bytes"] = size_bytes
                info["size_mb"] = round(size_bytes / (1024 * 1024), 2)

            # Get image ID
            result = subprocess.run(
                ["podman", "image", "inspect", self.fixed_image_name, "--format", "{{.Id}}"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                info["image_id"] = result.stdout.strip()[:12]

        except Exception as e:
            info["error"] = str(e)
        return info

    def _get_disk_usage(self) -> dict:
        """Get disk usage in the container."""
        usage = {}
        try:
            # Get /testbed size
            result = subprocess.run(
                ["podman", "exec", self.container_id, "du", "-sm", "/testbed"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                size_mb = int(result.stdout.split()[0])
                usage["testbed_mb"] = size_mb

            # Get overall container disk usage
            result = subprocess.run(
                ["podman", "exec", self.container_id, "df", "-m", "/testbed"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        usage["filesystem_total_mb"] = int(parts[1])
                        usage["filesystem_used_mb"] = int(parts[2])
                        usage["filesystem_avail_mb"] = int(parts[3])

        except Exception as e:
            usage["error"] = str(e)
        return usage

    def _setup_container(self):
        """Setup the container with fixed permissions."""
        safe_name = self.image_name.replace("/", "_").replace(":", "_")
        self.fixed_image_name = f"swebench-fixed-{safe_name}"

        result = subprocess.run(
            ["podman", "image", "exists", self.fixed_image_name],
            capture_output=True
        )

        if result.returncode != 0:
            print(f"  Creating fixed image...")
            self._fix_permissions()
        else:
            print(f"  Using existing fixed image: {self.fixed_image_name}")

        container_cmd = [
            "podman", "run", "-d",
            "--userns=keep-id",
            "--network=host",
            "-v", "/usr:/usr:ro",
            "-v", "/lib:/lib:ro",
            "-v", "/lib64:/lib64:ro",
            "-v", "/etc:/etc:ro",
            "-v", "/bin:/bin:ro",
            "-v", "/sbin:/sbin:ro",
            "-v", "/home:/home",
            "-v", "/tmp:/tmp",
            "-v", "/var:/var",
            "-w", "/testbed",
            "-e", f"HOME={self.home}",
            "-e", "PATH=/usr/local/bin:/usr/bin:/bin",
            self.fixed_image_name,
            "sleep", "infinity"
        ]

        result = subprocess.run(container_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start container: {result.stderr}")

        self.container_id = result.stdout.strip()
        print(f"  Container started: {self.container_id[:12]}")

        subprocess.run(
            ["podman", "exec", self.container_id, "bash", "-c",
             "git config user.email 'test@test.com' && git config user.name 'Test' && git config --add safe.directory /testbed"],
            capture_output=True
        )

    def _fix_permissions(self):
        """Create a modified image with fixed /testbed permissions."""
        uid = os.getuid()
        gid = os.getgid()

        subprocess.run(
            ["podman", "pull", f"docker.io/{self.image_name}"],
            capture_output=True
        )

        result = subprocess.run(
            ["podman", "run", "-d", f"docker.io/{self.image_name}", "sleep", "120"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create temp container: {result.stderr}")

        temp_container = result.stdout.strip()

        try:
            subprocess.run(
                ["podman", "exec", temp_container, "chown", "-R", f"{uid}:{gid}", "/testbed"],
                check=True, capture_output=True
            )
            subprocess.run(
                ["podman", "commit", temp_container, self.fixed_image_name],
                check=True, capture_output=True
            )
            print(f"  Created fixed image: {self.fixed_image_name}")
        finally:
            subprocess.run(["podman", "stop", temp_container], capture_output=True)
            subprocess.run(["podman", "rm", temp_container], capture_output=True)

    def _replay_all(self, replay_start: float) -> List[Dict]:
        """Replay all tool calls with timing."""
        results = []

        if not self.tool_calls:
            return results

        first_ts = self.tool_calls[0].get('timestamp', '')
        try:
            original_start = datetime.fromisoformat(first_ts.replace('Z', '+00:00')).timestamp()
        except:
            original_start = 0

        for i, tool_call in enumerate(self.tool_calls):
            tool_ts = tool_call.get('timestamp', '')
            tool_name = tool_call.get('tool')
            tool_input = tool_call.get('input', {})

            # Calculate timing
            try:
                tool_original_time = datetime.fromisoformat(tool_ts.replace('Z', '+00:00')).timestamp()
                relative_time = tool_original_time - original_start
            except:
                relative_time = 0

            # Wait for correct timing
            if not self.no_delay:
                target_time = replay_start + (relative_time / self.speed)
                wait_time = target_time - time.time()
                if wait_time > 0:
                    if wait_time > 1:
                        print(f"  Waiting {wait_time:.1f}s (t={relative_time:.1f}s)...")
                    time.sleep(wait_time)

            # Record tool call
            self.replay_tool_calls.append({
                'timestamp': datetime.now().isoformat(),
                'tool': tool_name,
                'id': tool_call.get('tool_use_id', f'replay_{i}')
            })

            # Execute tool
            desc = self._get_tool_description(tool_name, tool_input)
            print(f"  [{i+1}/{len(self.tool_calls)}] {tool_name}: {desc[:50]}...")

            exec_start = time.time()
            exec_result = self._execute_tool(tool_name, tool_input)
            exec_result['index'] = i
            exec_result['tool'] = tool_name
            exec_result['original_timestamp'] = tool_ts
            exec_result['replay_timestamp'] = datetime.now().isoformat()
            exec_result['execution_time'] = time.time() - exec_start

            results.append(exec_result)

        return results

    def _get_tool_description(self, tool_name: str, tool_input: dict) -> str:
        """Get a short description of the tool call."""
        if tool_name == 'Bash':
            return tool_input.get('description', tool_input.get('command', '')[:50])
        elif tool_name == 'Read':
            return tool_input.get('file_path', '')
        elif tool_name == 'Edit':
            return f"edit {tool_input.get('file_path', '')}"
        elif tool_name == 'Write':
            return f"write {tool_input.get('file_path', '')}"
        elif tool_name == 'Glob':
            return f"glob {tool_input.get('pattern', '')}"
        elif tool_name == 'Grep':
            return f"grep {tool_input.get('pattern', '')}"
        return str(tool_input)[:50]

    def _execute_tool(self, tool_name: str, tool_input: dict) -> dict:
        """Execute a tool call in the container."""
        try:
            if tool_name == 'Bash':
                return self._exec_bash(tool_input)
            elif tool_name == 'Read':
                return self._exec_read(tool_input)
            elif tool_name == 'Edit':
                return self._exec_edit(tool_input)
            elif tool_name == 'Write':
                return self._exec_write(tool_input)
            elif tool_name == 'Glob':
                return self._exec_glob(tool_input)
            elif tool_name == 'Grep':
                return self._exec_grep(tool_input)
            else:
                return {'success': False, 'error': f'Unknown tool: {tool_name}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _exec_bash(self, tool_input: dict) -> dict:
        """Execute a Bash command."""
        command = tool_input.get('command', '')
        timeout = tool_input.get('timeout', 300000) / 1000  # ms to seconds

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", command],
                capture_output=True, text=True, timeout=min(timeout, 300)
            )
            return {
                'success': result.returncode == 0,
                'exit_code': result.returncode,
                'stdout_len': len(result.stdout),
                'stderr_len': len(result.stderr),
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _exec_read(self, tool_input: dict) -> dict:
        """Execute a Read (cat file)."""
        file_path = tool_input.get('file_path', '')
        offset = tool_input.get('offset', 0)
        limit = tool_input.get('limit', 2000)

        # Use head/tail to simulate offset/limit
        if offset > 0:
            cmd = f"tail -n +{offset} '{file_path}' | head -n {limit}"
        else:
            cmd = f"head -n {limit} '{file_path}'"

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=30
            )
            return {
                'success': result.returncode == 0,
                'exit_code': result.returncode,
                'stdout_len': len(result.stdout),
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _exec_edit(self, tool_input: dict) -> dict:
        """Execute an Edit (string replacement)."""
        import base64

        file_path = tool_input.get('file_path', '')
        old_string = tool_input.get('old_string', '')
        new_string = tool_input.get('new_string', '')
        replace_all = tool_input.get('replace_all', False)

        # Use base64 encoding to avoid escaping issues
        b64_old = base64.b64encode(old_string.encode()).decode()
        b64_new = base64.b64encode(new_string.encode()).decode()
        replace_count = "" if replace_all else ", 1"

        py_cmd = f"""python3 -c "
import base64
file_path = '{file_path}'
old = base64.b64decode('{b64_old}').decode()
new = base64.b64decode('{b64_new}').decode()
with open(file_path, 'r') as f:
    content = f.read()
if old not in content:
    print('ERROR: old_string not found')
    exit(1)
if content.count(old) > 1 and {repr(not replace_all)}:
    print('ERROR: old_string not unique')
    exit(2)
content = content.replace(old, new{replace_count})
with open(file_path, 'w') as f:
    f.write(content)
print('OK')
"
"""

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", py_cmd],
                capture_output=True, text=True, timeout=30
            )
            return {
                'success': result.returncode == 0 and 'OK' in result.stdout,
                'exit_code': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr,
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _exec_write(self, tool_input: dict) -> dict:
        """Execute a Write (write file)."""
        file_path = tool_input.get('file_path', '')
        content = tool_input.get('content', '')

        # Write content using heredoc
        # Escape content for shell
        import base64
        b64_content = base64.b64encode(content.encode()).decode()

        cmd = f"echo '{b64_content}' | base64 -d > '{file_path}'"

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=30
            )
            return {
                'success': result.returncode == 0,
                'exit_code': result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _exec_glob(self, tool_input: dict) -> dict:
        """Execute a Glob (find files)."""
        pattern = tool_input.get('pattern', '')
        path = tool_input.get('path', '/testbed')

        # Use find with -name pattern
        cmd = f"find '{path}' -name '{pattern}' 2>/dev/null | head -100"

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=30
            )
            return {
                'success': result.returncode == 0,
                'exit_code': result.returncode,
                'stdout_len': len(result.stdout),
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _exec_grep(self, tool_input: dict) -> dict:
        """Execute a Grep (search in files)."""
        pattern = tool_input.get('pattern', '')
        path = tool_input.get('path', '/testbed')
        glob_filter = tool_input.get('glob', '')

        if glob_filter:
            cmd = f"grep -r --include='{glob_filter}' '{pattern}' '{path}' 2>/dev/null | head -100"
        else:
            cmd = f"grep -r '{pattern}' '{path}' 2>/dev/null | head -100"

        try:
            result = subprocess.run(
                ["podman", "exec", self.container_id, "bash", "-c", cmd],
                capture_output=True, text=True, timeout=60
            )
            return {
                'success': True,  # grep returns 1 if no match, but that's not an error
                'exit_code': result.returncode,
                'stdout_len': len(result.stdout),
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': 'timeout'}

    def _cleanup(self):
        """Clean up container."""
        if self.container_id:
            subprocess.run(["podman", "stop", self.container_id], capture_output=True)
            subprocess.run(["podman", "rm", self.container_id], capture_output=True)
            print(f"  Removed container: {self.container_id[:12]}")

    def _save_results(self, results: dict, resource_data: Optional[dict]):
        """Save results to output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        with open(self.output_dir / "results.json", "w") as f:
            json.dump(results, f, indent=2)

        if resource_data:
            with open(self.output_dir / "resources.json", "w") as f:
                json.dump(resource_data, f, indent=2)

        with open(self.output_dir / "tool_calls.json", "w") as f:
            json.dump(self.replay_tool_calls, f, indent=2)

        print(f"  Results saved to: {self.output_dir}")

    def _generate_plot(self):
        """Generate resource usage plot."""
        try:
            title = f"Replay - {self.task_name}" if self.task_name else "Trace Replay"
            if self.speed != 1.0:
                title += f" ({self.speed}x speed)"
            plot_from_attempt_dir(self.output_dir, title=title)
            print(f"  Plot saved to: {self.output_dir / 'resource_plot.png'}")
        except Exception as e:
            print(f"  Warning: Failed to generate plot: {e}")


def get_image_from_attempt(attempt_dir: Path) -> Optional[str]:
    """Extract Docker image name from attempt results."""
    results_file = attempt_dir / "results.json"
    if results_file.exists():
        with open(results_file) as f:
            results = json.load(f)
            return results.get("image")
    return None


def get_task_name_from_path(attempt_dir: Path) -> str:
    """Extract task name from attempt directory path."""
    parts = attempt_dir.parts
    for i, part in enumerate(parts):
        if part.startswith("batch_swebench"):
            if i + 1 < len(parts):
                return parts[i + 1]
    return attempt_dir.parent.name


def main():
    parser = argparse.ArgumentParser(
        description="Replay ALL tool calls from a Claude Code trace",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Replay with original timing (default)
  python scripts/replay_trace.py experiments/batch_swebench_18tasks/Web_Network_Easy/attempt_1

  # Replay at 2x speed
  python scripts/replay_trace.py <attempt_dir> --speed 2.0

  # No delay (as fast as possible)
  python scripts/replay_trace.py <attempt_dir> --no-delay
"""
    )
    parser.add_argument("attempt_dir", help="Path to attempt directory containing trace.jsonl")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="Speed multiplier (default: 1.0)")
    parser.add_argument("--no-delay", action="store_true",
                        help="Run without delays")
    parser.add_argument("--image", help="Override Docker image name")

    args = parser.parse_args()

    attempt_dir = Path(args.attempt_dir)
    if not attempt_dir.exists():
        print(f"Error: Attempt directory not found: {attempt_dir}")
        return 1

    trace_file = attempt_dir / "trace.jsonl"
    if not trace_file.exists():
        print(f"Error: Trace file not found: {trace_file}")
        return 1

    image_name = args.image or get_image_from_attempt(attempt_dir)
    if not image_name:
        print("Error: Could not determine Docker image. Use --image to specify.")
        return 1

    task_name = get_task_name_from_path(attempt_dir)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        base_dir = Path.home() / "agentcgroup" / "experiments" / "replays"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = base_dir / f"{task_name}_{timestamp}"

    print("=" * 70)
    print("Trace Replay Tool (ALL operations)")
    print("=" * 70)
    print(f"Source: {attempt_dir}")
    print(f"Task: {task_name}")
    print(f"Image: {image_name}")
    print(f"Output: {output_dir}")
    print(f"Speed: {args.speed}x {'(no delay)' if args.no_delay else '(original timing)'}")
    print("=" * 70)

    print("\nParsing trace file...")
    trace_parser = TraceParser(trace_file)
    tool_calls = trace_parser.parse()

    # Count by tool type
    from collections import Counter
    tool_counts = Counter(tc['tool'] for tc in tool_calls)
    print(f"Found {len(tool_calls)} replayable tool calls:")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        print(f"  {tool}: {count}")

    if not tool_calls:
        print("No replayable tool calls found")
        return 1

    # Calculate duration
    if len(tool_calls) > 1:
        first_ts = tool_calls[0].get('timestamp', '')
        last_ts = tool_calls[-1].get('timestamp', '')
        try:
            first_dt = datetime.fromisoformat(first_ts.replace('Z', '+00:00'))
            last_dt = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
            original_duration = (last_dt - first_dt).total_seconds()
            expected_duration = original_duration / args.speed if not args.no_delay else 0
            print(f"Original duration: {original_duration:.1f}s")
            if not args.no_delay:
                print(f"Expected replay duration: {expected_duration:.1f}s")
        except:
            pass

    print("\nStarting replay...")
    replayer = TraceReplayer(
        image_name=image_name,
        tool_calls=tool_calls,
        output_dir=output_dir,
        speed=args.speed,
        no_delay=args.no_delay,
        task_name=task_name
    )

    results = replayer.run()

    print("\n" + "=" * 70)
    print("Replay Summary")
    print("=" * 70)
    print(f"Total time: {results.get('total_time', 0):.1f}s")
    print(f"Tool calls executed: {len(tool_calls)}")

    if "resource_samples" in results:
        summary = results["resource_samples"].get("summary", {})
        print(f"Resource samples: {summary.get('sample_count', 0)}")
        if "memory_mb" in summary:
            print(f"Memory (MB): min={summary['memory_mb']['min']:.1f}, "
                  f"max={summary['memory_mb']['max']:.1f}, avg={summary['memory_mb']['avg']:.1f}")
        if "cpu_percent" in summary:
            print(f"CPU (%): min={summary['cpu_percent']['min']:.1f}, "
                  f"max={summary['cpu_percent']['max']:.1f}, avg={summary['cpu_percent']['avg']:.1f}")

    print(f"\nOutput saved to: {output_dir}")

    if "error" in results:
        print(f"\nError: {results['error']}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

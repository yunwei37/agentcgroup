#!/usr/bin/env python3
"""
Batch SWE-bench Test Runner

Runs 18 sample tasks (6 categories x 3 difficulties) with:
- No resource limits
- Full fix + test cycle prompt
- Retry mechanism
- Complete data collection

Usage:
    python scripts/batch_test_swebench.py                    # Run all 18 tasks
    python scripts/batch_test_swebench.py --task "SQL/Data,Easy"  # Run single task
    python scripts/batch_test_swebench.py --category "SQL/Data"   # Run one category
    python scripts/batch_test_swebench.py --difficulty Easy       # Run one difficulty
    python scripts/batch_test_swebench.py --resume                # Resume from progress
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Import from run_swebench.py
from run_swebench import SWEBenchRunner, ResourceMonitor
from plot_resources import plot_from_attempt_dir

# Sample tasks: 6 categories x 3 difficulties = 18 tasks
SAMPLE_TASKS = {
    # SQL/Data
    ('SQL/Data', 'Easy'): {
        'instance_id': 'sqlfluff__sqlfluff-5362',
        'repo': 'sqlfluff/sqlfluff',
        'docker_image': 'swerebench/sweb.eval.x86_64.sqlfluff_1776_sqlfluff-5362',
    },
    ('SQL/Data', 'Medium'): {
        'instance_id': 'tobymao__sqlglot-1177',
        'repo': 'tobymao/sqlglot',
        'docker_image': 'swerebench/sweb.eval.x86_64.tobymao_1776_sqlglot-1177',
    },
    ('SQL/Data', 'Hard'): {
        'instance_id': 'reata__sqllineage-438',
        'repo': 'reata/sqllineage',
        'docker_image': 'swerebench/sweb.eval.x86_64.reata_1776_sqllineage-438',
    },

    # DevOps/Build
    ('DevOps/Build', 'Easy'): {
        'instance_id': 'pre-commit__pre-commit-2524',
        'repo': 'pre-commit/pre-commit',
        'docker_image': 'swerebench/sweb.eval.x86_64.pre-commit_1776_pre-commit-2524',
    },
    ('DevOps/Build', 'Medium'): {
        'instance_id': 'beeware__briefcase-1525',
        'repo': 'beeware/briefcase',
        'docker_image': 'swerebench/sweb.eval.x86_64.beeware_1776_briefcase-1525',
    },
    ('DevOps/Build', 'Hard'): {
        'instance_id': 'iterative__dvc-777',
        'repo': 'iterative/dvc',
        'docker_image': 'swerebench/sweb.eval.x86_64.iterative_1776_dvc-777',
    },

    # ML/Scientific
    ('ML/Scientific', 'Easy'): {
        'instance_id': 'dask__dask-5510',
        'repo': 'dask/dask',
        'docker_image': 'swerebench/sweb.eval.x86_64.dask_1776_dask-5510',
    },
    ('ML/Scientific', 'Medium'): {
        'instance_id': 'dask__dask-11628',
        'repo': 'dask/dask',
        'docker_image': 'swerebench/sweb.eval.x86_64.dask_1776_dask-11628',
    },
    ('ML/Scientific', 'Hard'): {
        'instance_id': 'numba__numba-5721',
        'repo': 'numba/numba',
        'docker_image': 'swerebench/sweb.eval.x86_64.numba_1776_numba-5721',
    },

    # Web/Network
    ('Web/Network', 'Easy'): {
        'instance_id': 'encode__httpx-2701',
        'repo': 'encode/httpx',
        'docker_image': 'swerebench/sweb.eval.x86_64.encode_1776_httpx-2701',
    },
    ('Web/Network', 'Medium'): {
        'instance_id': 'streamlink__streamlink-3485',
        'repo': 'streamlink/streamlink',
        'docker_image': 'swerebench/sweb.eval.x86_64.streamlink_1776_streamlink-3485',
    },
    ('Web/Network', 'Hard'): {
        'instance_id': 'streamlink__streamlink-2160',
        'repo': 'streamlink/streamlink',
        'docker_image': 'swerebench/sweb.eval.x86_64.streamlink_1776_streamlink-2160',
    },

    # CLI/Tools
    ('CLI/Tools', 'Easy'): {
        'instance_id': 'asottile__pyupgrade-939',
        'repo': 'asottile/pyupgrade',
        'docker_image': 'swerebench/sweb.eval.x86_64.asottile_1776_pyupgrade-939',
    },
    ('CLI/Tools', 'Medium'): {
        'instance_id': 'Textualize__textual-2987',
        'repo': 'Textualize/textual',
        'docker_image': 'swerebench/sweb.eval.x86_64.textualize_1776_textual-2987',
    },
    ('CLI/Tools', 'Hard'): {
        'instance_id': 'joke2k__faker-1520',
        'repo': 'joke2k/faker',
        'docker_image': 'swerebench/sweb.eval.x86_64.joke2k_1776_faker-1520',
    },

    # Medical/Bio
    ('Medical/Bio', 'Easy'): {
        'instance_id': 'pydicom__pydicom-1000',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-1000',
    },
    ('Medical/Bio', 'Medium'): {
        'instance_id': 'pydicom__pydicom-1090',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-1090',
    },
    ('Medical/Bio', 'Hard'): {
        'instance_id': 'pydicom__pydicom-2065',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-2065',
    },
}

# Complete workflow prompt
WORKFLOW_PROMPT = '''Fix this issue: $(cat /issue.md)

IMPORTANT: You must complete the FULL workflow:
1. Read and understand the issue thoroughly
2. Explore the codebase to find relevant files
3. Implement the fix
4. Run the test suite to verify your fix
5. If ANY test fails, analyze the error and fix it
6. Repeat steps 4-5 until ALL tests pass
7. Only stop when tests are passing

DO NOT stop until you have:
- Made code changes that fix the issue
- Run the tests and confirmed they pass
- Shown the final git diff

If you encounter test failures, debug and fix them. Keep trying until successful.'''


# Default output directory name (fixed, for auto-resume)
DEFAULT_OUTPUT_DIR = "batch_swebench_18tasks"


class BatchSWEBenchRunner:
    """Run batch SWE-bench tests with retry and progress tracking."""

    def __init__(self, max_retries: int = 3, output_base: Optional[Path] = None,
                 use_timestamp: bool = False):
        self.max_retries = max_retries
        self.home = Path.home()

        if output_base:
            self.output_dir = output_base
        elif use_timestamp:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = self.home / "agentcgroup" / "experiments" / f"batch_test_{timestamp}"
        else:
            # Use fixed name for auto-resume
            self.output_dir = self.home / "agentcgroup" / "experiments" / DEFAULT_OUTPUT_DIR

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.progress_file = self.output_dir / "progress.json"
        self.results: List[dict] = []

    def run_all(self, tasks: Optional[Dict] = None):
        """Run all specified tasks."""
        if tasks is None:
            tasks = SAMPLE_TASKS

        completed = self._load_progress()

        print(f"\n{'='*60}")
        print(f"Batch SWE-bench Test Runner")
        print(f"{'='*60}")
        print(f"Total tasks: {len(tasks)}")
        print(f"Already completed: {len(completed)}")
        print(f"Output directory: {self.output_dir}")
        print(f"Max retries: {self.max_retries}")
        print(f"Resource limits: NONE (unlimited)")
        print(f"{'='*60}\n")

        for i, ((category, difficulty), task) in enumerate(tasks.items(), 1):
            task_key = f"{category}_{difficulty}".replace("/", "_")

            if task_key in completed:
                print(f"[{i}/{len(tasks)}] Skipping {task_key} (already completed)")
                continue

            print(f"\n{'='*60}")
            print(f"[{i}/{len(tasks)}] Running: {category} - {difficulty}")
            print(f"Instance: {task['instance_id']}")
            print(f"Image: {task['docker_image']}")
            print(f"{'='*60}\n")

            result = self._run_with_retry(task, category, difficulty)
            self.results.append(result)
            self._save_progress(task_key, result)

            # Cleanup images after each task to save disk space
            self._cleanup_images(task['docker_image'])

            status = "SUCCESS" if result.get('success') else "FAILED"
            print(f"\n[{task_key}] {status} after {result.get('attempts', 0)} attempt(s)")
            if result.get('total_time'):
                print(f"Total time: {result['total_time']:.1f}s")

        self._generate_report()

    def _run_with_retry(self, task: dict, category: str, difficulty: str) -> dict:
        """Run a single task with retry logic."""
        task_dir_name = f"{category.replace('/', '_')}_{difficulty}"
        task_dir = self.output_dir / task_dir_name
        task_dir.mkdir(parents=True, exist_ok=True)

        result = {
            'category': category,
            'difficulty': difficulty,
            'instance_id': task['instance_id'],
            'repo': task['repo'],
            'docker_image': task['docker_image'],
            'start_time': datetime.now().isoformat(),
            'attempts': 0,
            'success': False,
        }

        for attempt in range(1, self.max_retries + 1):
            print(f"\n--- Attempt {attempt}/{self.max_retries} ---")
            result['attempts'] = attempt

            attempt_dir = task_dir / f"attempt_{attempt}"
            attempt_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Use SWEBenchRunner with NO resource limits
                runner = SWEBenchRunner(
                    image_name=task['docker_image'],
                    memory_limit=None,  # No limit
                    cpu_limit=None,     # No limit
                    output_dir=attempt_dir
                )

                attempt_result = runner.run(prompt=WORKFLOW_PROMPT, run_tests=True)

                # Generate resource plot
                try:
                    plot_title = f"Resource Usage - {task['instance_id']} (Attempt {attempt})"
                    plot_from_attempt_dir(attempt_dir, title=plot_title)
                except Exception as pe:
                    print(f"  Warning: Failed to generate plot: {pe}")

                if self._check_success(attempt_result):
                    result['success'] = True
                    result['successful_attempt'] = attempt
                    result['attempt_results'] = attempt_result
                    break
                else:
                    print(f"Attempt {attempt} did not succeed")

            except Exception as e:
                print(f"Attempt {attempt} failed with error: {e}")
                with open(attempt_dir / "error.txt", "w") as f:
                    f.write(str(e))

        result['end_time'] = datetime.now().isoformat()
        result['total_time'] = (
            datetime.fromisoformat(result['end_time']) -
            datetime.fromisoformat(result['start_time'])
        ).total_seconds()

        return result

    def _check_success(self, result: dict) -> bool:
        """Check if the attempt was successful."""
        output = result.get('claude_output', {}).get('stdout', '')
        stderr = result.get('claude_output', {}).get('stderr', '')

        # Check for Claude Code CLI crash
        crash_indicators = ['No messages returned', 'UnhandledPromiseRejection', 'SIGKILL', 'SIGTERM']
        is_crash = any(indicator in stderr or indicator in output for indicator in crash_indicators)
        if is_crash:
            print(f"  Detected Claude Code crash!")
            return False

        has_diff = 'diff --git' in output

        pass_indicators = ['passed', 'all tests', 'tests passed', 'tests pass', 'OK', '0 failed']
        has_pass_indicator = any(kw.lower() in output.lower() for kw in pass_indicators)

        # Check for real failures, but exclude xfailed (expected failures in pytest)
        output_end = output[-2000:] if len(output) > 2000 else output
        # Remove xfailed/xpassed before checking for failures
        output_cleaned = output_end.replace('xfailed', '').replace('xpassed', '')
        fail_indicators = ['FAILED', 'ERROR', 'failure', 'failed']
        has_fail_indicator = any(kw in output_cleaned for kw in fail_indicators)

        success = has_diff and has_pass_indicator and not has_fail_indicator
        print(f"  Success check: diff={has_diff}, pass={has_pass_indicator}, fail={has_fail_indicator}, crash={is_crash}")
        return success

    def _cleanup_images(self, image_name: str):
        """Remove Docker images after task to save disk space."""
        print(f"  Cleaning up images for {image_name}...")
        try:
            # Remove fixed image
            safe_name = image_name.replace("/", "_").replace(":", "_")
            fixed_image = f"swebench-fixed-{safe_name}"
            subprocess.run(["podman", "rmi", "-f", fixed_image],
                          capture_output=True, timeout=30)

            # Remove original image
            subprocess.run(["podman", "rmi", "-f", f"docker.io/{image_name}"],
                          capture_output=True, timeout=30)

            # Prune dangling images
            subprocess.run(["podman", "image", "prune", "-f"],
                          capture_output=True, timeout=30)
            print(f"  Images cleaned up")
        except Exception as e:
            print(f"  Warning: Failed to cleanup images: {e}")

    def _load_progress(self) -> set:
        """Load completed tasks from progress file."""
        if self.progress_file.exists():
            with open(self.progress_file, "r") as f:
                progress = json.load(f)
                return set(progress.get('completed', []))
        return set()

    def _save_progress(self, task_key: str, result: dict):
        """Save progress to file."""
        if self.progress_file.exists():
            with open(self.progress_file, "r") as f:
                progress = json.load(f)
        else:
            progress = {'completed': [], 'results': {}}

        progress['completed'].append(task_key)
        progress['results'][task_key] = {
            'success': result.get('success'),
            'attempts': result.get('attempts'),
            'total_time': result.get('total_time'),
        }

        with open(self.progress_file, "w") as f:
            json.dump(progress, f, indent=2)

    def _generate_report(self):
        """Generate final summary report."""
        summary = {
            'total_tasks': len(self.results),
            'successful': sum(1 for r in self.results if r.get('success')),
            'failed': sum(1 for r in self.results if not r.get('success')),
            'total_time': sum(r.get('total_time', 0) for r in self.results),
            'results': self.results,
        }

        with open(self.output_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        report = f"""# Batch SWE-bench Test Report

Generated: {datetime.now().isoformat()}

## Summary

- **Total Tasks**: {summary['total_tasks']}
- **Successful**: {summary['successful']}
- **Failed**: {summary['failed']}
- **Success Rate**: {summary['successful']/max(summary['total_tasks'], 1)*100:.1f}%
- **Total Time**: {summary['total_time']:.1f}s

## Results by Task

| Category | Difficulty | Instance ID | Success | Attempts | Time (s) |
|----------|------------|-------------|---------|----------|----------|
"""
        for r in self.results:
            status = "Yes" if r.get('success') else "No"
            report += f"| {r.get('category')} | {r.get('difficulty')} | {r.get('instance_id')} | {status} | {r.get('attempts')} | {r.get('total_time', 0):.1f} |\n"

        with open(self.output_dir / "report.md", "w") as f:
            f.write(report)

        print(f"\n{'='*60}")
        print("Final Report")
        print(f"{'='*60}")
        print(f"Total: {summary['total_tasks']}, Success: {summary['successful']}, Failed: {summary['failed']}")
        print(f"Success Rate: {summary['successful']/max(summary['total_tasks'], 1)*100:.1f}%")
        print(f"\nReport saved to: {self.output_dir / 'report.md'}")


def main():
    parser = argparse.ArgumentParser(description="Batch SWE-bench Test Runner")
    parser.add_argument("--task", help="Run single task, e.g., 'SQL/Data,Easy'")
    parser.add_argument("--category", help="Run all tasks in category")
    parser.add_argument("--difficulty", help="Run all tasks of difficulty (Easy/Medium/Hard)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per task")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--new-run", action="store_true",
                        help="Start fresh with timestamped directory (default: auto-resume)")

    args = parser.parse_args()

    tasks = SAMPLE_TASKS.copy()

    if args.task:
        parts = args.task.split(",")
        if len(parts) == 2:
            key = (parts[0].strip(), parts[1].strip())
            if key in SAMPLE_TASKS:
                tasks = {key: SAMPLE_TASKS[key]}
            else:
                print(f"Task not found: {args.task}")
                print(f"Available: {list(SAMPLE_TASKS.keys())}")
                return 1

    if args.category:
        tasks = {k: v for k, v in tasks.items() if k[0] == args.category}
        if not tasks:
            print(f"No tasks found for category: {args.category}")
            return 1

    if args.difficulty:
        tasks = {k: v for k, v in tasks.items() if k[1] == args.difficulty}
        if not tasks:
            print(f"No tasks found for difficulty: {args.difficulty}")
            return 1

    output_base = None
    if args.output_dir:
        output_base = Path(args.output_dir)

    runner = BatchSWEBenchRunner(
        max_retries=args.max_retries,
        output_base=output_base,
        use_timestamp=args.new_run,
    )
    runner.run_all(tasks)

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Batch SWE-bench Test Runner

Runs 36 sample tasks (6 categories x 3 difficulties x 2 tasks each) with:
- No resource limits
- Full fix + test cycle prompt
- Retry mechanism
- Complete data collection

Usage:
    python scripts/batch_test_swebench.py                    # Run all 36 tasks
    python scripts/batch_test_swebench.py --task "SQL/Data,Easy,1"  # Run single task
    python scripts/batch_test_swebench.py --category "SQL/Data"   # Run one category
    python scripts/batch_test_swebench.py --difficulty Easy       # Run one difficulty
    python scripts/batch_test_swebench.py --resume                # Resume from progress
    python scripts/batch_test_swebench.py --model sonnet          # Use specific model
    python scripts/batch_test_swebench.py --local-model qwen3     # Use local model
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

# Sample tasks: 6 categories x 3 difficulties x 2 tasks = 36 tasks
# Key format: (category, difficulty, task_num)
SAMPLE_TASKS = {
    # SQL/Data - Task 1
    ('SQL/Data', 'Easy', 1): {
        'instance_id': 'sqlfluff__sqlfluff-5362',
        'repo': 'sqlfluff/sqlfluff',
        'docker_image': 'swerebench/sweb.eval.x86_64.sqlfluff_1776_sqlfluff-5362',
    },
    ('SQL/Data', 'Medium', 1): {
        'instance_id': 'tobymao__sqlglot-1177',
        'repo': 'tobymao/sqlglot',
        'docker_image': 'swerebench/sweb.eval.x86_64.tobymao_1776_sqlglot-1177',
    },
    ('SQL/Data', 'Hard', 1): {
        'instance_id': 'reata__sqllineage-438',
        'repo': 'reata/sqllineage',
        'docker_image': 'swerebench/sweb.eval.x86_64.reata_1776_sqllineage-438',
    },
    # SQL/Data - Task 2 (from all_images_local)
    ('SQL/Data', 'Easy', 2): {
        'instance_id': 'tobymao__sqlglot-1642',
        'repo': 'tobymao/sqlglot',
        'docker_image': 'swerebench/sweb.eval.x86_64.tobymao_1776_sqlglot-1642',
    },
    ('SQL/Data', 'Medium', 2): {
        'instance_id': '0b01001001__spectree-64',
        'repo': '0b01001001/spectree',
        'docker_image': 'swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64',
    },
    ('SQL/Data', 'Hard', 2): {
        'instance_id': 'facelessuser__soupsieve-147',
        'repo': 'facelessuser/soupsieve',
        'docker_image': 'swerebench/sweb.eval.x86_64.facelessuser_1776_soupsieve-147',
    },

    # DevOps/Build - Task 1
    ('DevOps/Build', 'Easy', 1): {
        'instance_id': 'pre-commit__pre-commit-2524',
        'repo': 'pre-commit/pre-commit',
        'docker_image': 'swerebench/sweb.eval.x86_64.pre-commit_1776_pre-commit-2524',
    },
    ('DevOps/Build', 'Medium', 1): {
        'instance_id': 'beeware__briefcase-1525',
        'repo': 'beeware/briefcase',
        'docker_image': 'swerebench/sweb.eval.x86_64.beeware_1776_briefcase-1525',
    },
    ('DevOps/Build', 'Hard', 1): {
        'instance_id': 'iterative__dvc-777',
        'repo': 'iterative/dvc',
        'docker_image': 'swerebench/sweb.eval.x86_64.iterative_1776_dvc-777',
    },
    # DevOps/Build - Task 2 (from all_images_local)
    ('DevOps/Build', 'Easy', 2): {
        'instance_id': 'iterative__dvc-745',
        'repo': 'iterative/dvc',
        'docker_image': 'swerebench/sweb.eval.x86_64.iterative_1776_dvc-745',
    },
    ('DevOps/Build', 'Medium', 2): {
        'instance_id': 'ARMmbed__mbed-tools-138',
        'repo': 'ARMmbed/mbed-tools',
        'docker_image': 'swerebench/sweb.eval.x86_64.armmbed_1776_mbed-tools-138',
    },
    ('DevOps/Build', 'Hard', 2): {
        'instance_id': 'Azure__azure-cli-2214',
        'repo': 'Azure/azure-cli',
        'docker_image': 'swerebench/sweb.eval.x86_64.azure_1776_azure-cli-2214',
    },

    # ML/Scientific - Task 1
    ('ML/Scientific', 'Easy', 1): {
        'instance_id': 'dask__dask-5510',
        'repo': 'dask/dask',
        'docker_image': 'swerebench/sweb.eval.x86_64.dask_1776_dask-5510',
    },
    ('ML/Scientific', 'Medium', 1): {
        'instance_id': 'dask__dask-11628',
        'repo': 'dask/dask',
        'docker_image': 'swerebench/sweb.eval.x86_64.dask_1776_dask-11628',
    },
    ('ML/Scientific', 'Hard', 1): {
        'instance_id': 'numba__numba-5721',
        'repo': 'numba/numba',
        'docker_image': 'swerebench/sweb.eval.x86_64.numba_1776_numba-5721',
    },
    # ML/Scientific - Task 2 (from all_images_local)
    ('ML/Scientific', 'Easy', 2): {
        'instance_id': 'AI4S2S__lilio-49',
        'repo': 'AI4S2S/lilio',
        'docker_image': 'swerebench/sweb.eval.x86_64.ai4s2s_1776_lilio-49',
    },
    ('ML/Scientific', 'Medium', 2): {
        'instance_id': 'numba__numba-9636',
        'repo': 'numba/numba',
        'docker_image': 'swerebench/sweb.eval.x86_64.numba_1776_numba-9636',
    },
    ('ML/Scientific', 'Hard', 2): {
        'instance_id': 'spacetelescope__poppy-411',
        'repo': 'spacetelescope/poppy',
        'docker_image': 'swerebench/sweb.eval.x86_64.spacetelescope_1776_poppy-411',
    },

    # Web/Network - Task 1
    ('Web/Network', 'Easy', 1): {
        'instance_id': 'encode__httpx-2701',
        'repo': 'encode/httpx',
        'docker_image': 'swerebench/sweb.eval.x86_64.encode_1776_httpx-2701',
    },
    ('Web/Network', 'Medium', 1): {
        'instance_id': 'streamlink__streamlink-3485',
        'repo': 'streamlink/streamlink',
        'docker_image': 'swerebench/sweb.eval.x86_64.streamlink_1776_streamlink-3485',
    },
    ('Web/Network', 'Hard', 1): {
        'instance_id': 'streamlink__streamlink-2160',
        'repo': 'streamlink/streamlink',
        'docker_image': 'swerebench/sweb.eval.x86_64.streamlink_1776_streamlink-2160',
    },
    # Web/Network - Task 2 (from all_images_local)
    ('Web/Network', 'Easy', 2): {
        'instance_id': 'AspenWeb__pando.py-586',
        'repo': 'AspenWeb/pando.py',
        'docker_image': 'swerebench/sweb.eval.x86_64.aspenweb_1776_pando.py-586',
    },
    ('Web/Network', 'Medium', 2): {
        'instance_id': 'redis__redis-py-3264',
        'repo': 'redis/redis-py',
        'docker_image': 'swerebench/sweb.eval.x86_64.redis_1776_redis-py-3264',
    },
    ('Web/Network', 'Hard', 2): {
        'instance_id': 'libp2p__py-libp2p-533',
        'repo': 'libp2p/py-libp2p',
        'docker_image': 'swerebench/sweb.eval.x86_64.libp2p_1776_py-libp2p-533',
    },

    # CLI/Tools - Task 1
    ('CLI/Tools', 'Easy', 1): {
        'instance_id': 'asottile__pyupgrade-939',
        'repo': 'asottile/pyupgrade',
        'docker_image': 'swerebench/sweb.eval.x86_64.asottile_1776_pyupgrade-939',
    },
    ('CLI/Tools', 'Medium', 1): {
        'instance_id': 'Textualize__textual-2987',
        'repo': 'Textualize/textual',
        'docker_image': 'swerebench/sweb.eval.x86_64.textualize_1776_textual-2987',
    },
    ('CLI/Tools', 'Hard', 1): {
        'instance_id': 'joke2k__faker-1520',
        'repo': 'joke2k/faker',
        'docker_image': 'swerebench/sweb.eval.x86_64.joke2k_1776_faker-1520',
    },
    # CLI/Tools - Task 2 (from all_images_local)
    ('CLI/Tools', 'Easy', 2): {
        'instance_id': 'simonw__files-to-prompt-44',
        'repo': 'simonw/files-to-prompt',
        'docker_image': 'swerebench/sweb.eval.x86_64.simonw_1776_files-to-prompt-44',
    },
    ('CLI/Tools', 'Medium', 2): {
        'instance_id': 'wemake-services__wemake-python-styleguide-3117',
        'repo': 'wemake-services/wemake-python-styleguide',
        'docker_image': 'swerebench/sweb.eval.x86_64.wemake-services_1776_wemake-python-styleguide-3117',
    },
    ('CLI/Tools', 'Hard', 2): {
        'instance_id': 'lovasoa__marshmallow_dataclass-121',
        'repo': 'lovasoa/marshmallow_dataclass',
        'docker_image': 'swerebench/sweb.eval.x86_64.lovasoa_1776_marshmallow_dataclass-121',
    },

    # Medical/Bio - Task 1
    ('Medical/Bio', 'Easy', 1): {
        'instance_id': 'pydicom__pydicom-1000',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-1000',
    },
    ('Medical/Bio', 'Medium', 1): {
        'instance_id': 'pydicom__pydicom-1090',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-1090',
    },
    ('Medical/Bio', 'Hard', 1): {
        'instance_id': 'pydicom__pydicom-2065',
        'repo': 'pydicom/pydicom',
        'docker_image': 'swerebench/sweb.eval.x86_64.pydicom_1776_pydicom-2065',
    },
    # Medical/Bio - Task 2 (from all_images_local)
    ('Medical/Bio', 'Easy', 2): {
        'instance_id': '12rambau__sepal_ui-411',
        'repo': '12rambau/sepal_ui',
        'docker_image': 'swerebench/sweb.eval.x86_64.12rambau_1776_sepal_ui-411',
    },
    ('Medical/Bio', 'Medium', 2): {
        'instance_id': 'cneud__alto-tools-29',
        'repo': 'cneud/alto-tools',
        'docker_image': 'swerebench/sweb.eval.x86_64.cneud_1776_alto-tools-29',
    },
    ('Medical/Bio', 'Hard', 2): {
        'instance_id': 'Aarhus-Psychiatry-Research__timeseriesflattener-186',
        'repo': 'Aarhus-Psychiatry-Research/timeseriesflattener',
        'docker_image': 'swerebench/sweb.eval.x86_64.aarhus-psychiatry-research_1776_timeseriesflattener-186',
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

If you encounter test failures, debug and fix them. Keep trying until successful.

CRITICAL REQUIREMENTS FOR TESTING:
- You MUST run the project's ORIGINAL test suite (pytest, unittest, tox, etc.)
- Do NOT write custom test scripts or verification scripts to bypass tests
- Do NOT claim success based on your own "All checks passed" output
- The test output MUST show real pytest format: "X passed, Y failed in Z seconds"
- If tests fail with ImportError or collection errors, fix the environment/import issue first
- Success means the project's actual test suite passes, not custom verification

WHAT COUNTS AS SUCCESS:
- Real pytest/unittest output showing tests passed
- Example: "===== 150 passed, 0 failed in 10.5s ====="

WHAT DOES NOT COUNT:
- Your own verification scripts saying "All checks passed"
- Manual testing or print statements
- Skipping tests due to import errors'''


# Default output directory name (fixed, for auto-resume)
# Keep using 18tasks folder to allow resuming from existing progress
DEFAULT_OUTPUT_DIR = "batch_swebench_18tasks"


class BatchSWEBenchRunner:
    """Run batch SWE-bench tests with retry and progress tracking."""

    def __init__(self, max_retries: int = 3, output_base: Optional[Path] = None,
                 use_timestamp: bool = False, model: str = "haiku",
                 local_model: Optional[str] = None):
        self.max_retries = max_retries
        self.home = Path.home()
        self.model = model
        self.local_model = local_model

        # Prepare extra environment for local model
        self.extra_env = None
        if local_model:
            self.extra_env = {
                "ANTHROPIC_BASE_URL": "http://localhost:4000",
                "ANTHROPIC_MODEL": local_model,
            }

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

        model_info = self.local_model if self.local_model else self.model
        print(f"\n{'='*60}")
        print(f"Batch SWE-bench Test Runner")
        print(f"{'='*60}")
        print(f"Total tasks: {len(tasks)}")
        print(f"Already completed: {len(completed)}")
        print(f"Output directory: {self.output_dir}")
        print(f"Max retries: {self.max_retries}")
        print(f"Model: {model_info}")
        if self.local_model:
            print(f"Local model endpoint: {self.extra_env.get('ANTHROPIC_BASE_URL')}")
        print(f"Resource limits: NONE (unlimited)")
        print(f"{'='*60}\n")

        for i, (task_key_tuple, task) in enumerate(tasks.items(), 1):
            # Handle both old (category, difficulty) and new (category, difficulty, task_num) format
            if len(task_key_tuple) == 3:
                category, difficulty, task_num = task_key_tuple
                task_key = f"{category}_{difficulty}_{task_num}".replace("/", "_")
            else:
                category, difficulty = task_key_tuple
                task_num = 1
                task_key = f"{category}_{difficulty}".replace("/", "_")

            if task_key in completed:
                print(f"[{i}/{len(tasks)}] Skipping {task_key} (already completed)")
                continue

            print(f"\n{'='*60}")
            print(f"[{i}/{len(tasks)}] Running: {category} - {difficulty} - Task {task_num}")
            print(f"Instance: {task['instance_id']}")
            print(f"Image: {task['docker_image']}")
            print(f"Model: {model_info}")
            print(f"{'='*60}\n")

            result = self._run_with_retry(task, category, difficulty, task_num)
            self.results.append(result)
            self._save_progress(task_key, result)

            # Cleanup images after each task to save disk space
            self._cleanup_images(task['docker_image'])

            status = "SUCCESS" if result.get('success') else "FAILED"
            print(f"\n[{task_key}] {status} after {result.get('attempts', 0)} attempt(s)")
            if result.get('total_time'):
                print(f"Total time: {result['total_time']:.1f}s")

        self._generate_report()

    def _run_with_retry(self, task: dict, category: str, difficulty: str, task_num: int = 1) -> dict:
        """Run a single task with retry logic (only retry on Claude Code crash)."""
        task_dir_name = f"{category.replace('/', '_')}_{difficulty}_{task_num}"
        task_dir = self.output_dir / task_dir_name
        task_dir.mkdir(parents=True, exist_ok=True)

        model_info = self.local_model if self.local_model else self.model
        result = {
            'category': category,
            'difficulty': difficulty,
            'task_num': task_num,
            'instance_id': task['instance_id'],
            'repo': task['repo'],
            'docker_image': task['docker_image'],
            'model': model_info,
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

                attempt_result = runner.run(
                    prompt=WORKFLOW_PROMPT,
                    run_tests=True,
                    model=self.model,
                    extra_env=self.extra_env
                )

                # Generate resource plot
                try:
                    plot_title = f"Resource Usage - {task['instance_id']} (Attempt {attempt})"
                    plot_from_attempt_dir(attempt_dir, title=plot_title)
                except Exception as pe:
                    print(f"  Warning: Failed to generate plot: {pe}")

                # Check for crash first - only crashes trigger retry
                is_crash = self._check_crash(attempt_result)
                if is_crash:
                    print(f"  Claude Code crashed on attempt {attempt}")
                    if attempt < self.max_retries:
                        print(f"  Retrying...")
                        continue
                    else:
                        print(f"  Max retries reached")
                        result['crash'] = True
                        break

                # No crash - check success (but don't retry on failure)
                if self._check_success(attempt_result):
                    result['success'] = True
                    result['successful_attempt'] = attempt
                    result['attempt_results'] = attempt_result
                else:
                    print(f"  Task did not succeed (no retry for non-crash failures)")
                    result['attempt_results'] = attempt_result
                # Exit loop - no retry for non-crash cases
                break

            except Exception as e:
                print(f"Attempt {attempt} failed with error: {e}")
                with open(attempt_dir / "error.txt", "w") as f:
                    f.write(str(e))
                # Exception counts as crash - retry
                if attempt >= self.max_retries:
                    result['crash'] = True

        result['end_time'] = datetime.now().isoformat()
        result['total_time'] = (
            datetime.fromisoformat(result['end_time']) -
            datetime.fromisoformat(result['start_time'])
        ).total_seconds()

        return result

    def _check_crash(self, result: dict) -> bool:
        """Check if Claude Code crashed (triggers retry)."""
        output = result.get('claude_output', {}).get('stdout', '')
        stderr = result.get('claude_output', {}).get('stderr', '')

        crash_indicators = ['No messages returned', 'UnhandledPromiseRejection', 'SIGKILL', 'SIGTERM']
        is_crash = any(indicator in stderr or indicator in output for indicator in crash_indicators)
        return is_crash

    def _check_success(self, result: dict) -> bool:
        """Check if the attempt was successful (task completed and tests passed)."""
        output = result.get('claude_output', {}).get('stdout', '')

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
        print(f"  Success check: diff={has_diff}, pass={has_pass_indicator}, fail={has_fail_indicator}")
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

        model_info = self.local_model if self.local_model else self.model
        report = f"""# Batch SWE-bench Test Report

Generated: {datetime.now().isoformat()}

## Summary

- **Total Tasks**: {summary['total_tasks']}
- **Successful**: {summary['successful']}
- **Failed**: {summary['failed']}
- **Success Rate**: {summary['successful']/max(summary['total_tasks'], 1)*100:.1f}%
- **Total Time**: {summary['total_time']:.1f}s
- **Model**: {model_info}

## Results by Task

| Category | Difficulty | Task# | Instance ID | Success | Attempts | Time (s) |
|----------|------------|-------|-------------|---------|----------|----------|
"""
        for r in self.results:
            status = "Yes" if r.get('success') else "No"
            task_num = r.get('task_num', 1)
            report += f"| {r.get('category')} | {r.get('difficulty')} | {task_num} | {r.get('instance_id')} | {status} | {r.get('attempts')} | {r.get('total_time', 0):.1f} |\n"

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
    parser.add_argument("--task", help="Run single task, e.g., 'SQL/Data,Easy,1' or 'SQL/Data,Easy'")
    parser.add_argument("--category", help="Run all tasks in category")
    parser.add_argument("--difficulty", help="Run all tasks of difficulty (Easy/Medium/Hard)")
    parser.add_argument("--task-num", type=int, help="Run only task 1 or task 2")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per task")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--new-run", action="store_true",
                        help="Start fresh with timestamped directory (default: auto-resume)")
    parser.add_argument("--model", default="haiku", help="Model to use (default: haiku)")
    parser.add_argument("--local-model", help="Use local model via litellm proxy (e.g., qwen3)")

    args = parser.parse_args()

    tasks = SAMPLE_TASKS.copy()

    if args.task:
        parts = args.task.split(",")
        if len(parts) == 3:
            key = (parts[0].strip(), parts[1].strip(), int(parts[2].strip()))
            if key in SAMPLE_TASKS:
                tasks = {key: SAMPLE_TASKS[key]}
            else:
                print(f"Task not found: {args.task}")
                print(f"Available: {list(SAMPLE_TASKS.keys())}")
                return 1
        elif len(parts) == 2:
            # Match both task 1 and task 2 for this category/difficulty
            category, difficulty = parts[0].strip(), parts[1].strip()
            tasks = {k: v for k, v in SAMPLE_TASKS.items()
                    if k[0] == category and k[1] == difficulty}
            if not tasks:
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

    if args.task_num:
        tasks = {k: v for k, v in tasks.items() if k[2] == args.task_num}
        if not tasks:
            print(f"No tasks found for task_num: {args.task_num}")
            return 1

    output_base = None
    if args.output_dir:
        output_base = Path(args.output_dir)

    runner = BatchSWEBenchRunner(
        max_retries=args.max_retries,
        output_base=output_base,
        use_timestamp=args.new_run,
        model=args.model,
        local_model=args.local_model,
    )
    runner.run_all(tasks)

    return 0


if __name__ == "__main__":
    sys.exit(main())

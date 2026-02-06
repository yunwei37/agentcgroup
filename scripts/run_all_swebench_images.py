#!/usr/bin/env python3
"""
Run All SWE-bench Images Runner

Downloads and runs ALL SWE-bench Docker images from the SWE-rebench dataset.
This script:
- Fetches all tasks with Docker images from SWE-rebench dataset
- Pulls each Docker image, runs it, and cleans up before moving to the next
- Uses SWEBenchRunner and ResourceMonitor from run_swebench.py
- Generates resource plots using plot_from_attempt_dir from plot_resources.py

Usage:
    python scripts/run_all_swebench_images.py --generate-task-list          # Generate task_list.json
    python scripts/run_all_swebench_images.py --generate-task-list --prioritize-dir experiments/batch_swebench_18tasks
    python scripts/run_all_swebench_images.py --task-list task_list.json    # Run from saved list
    python scripts/run_all_swebench_images.py --task-list task_list.json --resume
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Import from run_swebench.py
from run_swebench import SWEBenchRunner
from plot_resources import plot_from_attempt_dir


def get_model_env_vars(model: str) -> Tuple[Dict[str, str], str]:
    """
    Get environment variables and actual model name for the given model option.

    Returns:
        Tuple of (env_vars, actual_model_name)
    """
    if model == "haiku":
        # Use Anthropic's haiku (default, no env vars needed)
        return {}, "haiku"
    elif model == "qwen3":
        # Use local llama-server
        env_vars = {
            "ANTHROPIC_BASE_URL": "http://localhost:8080",
            "ANTHROPIC_AUTH_TOKEN": "llama",
            "ANTHROPIC_API_KEY": "",
        }
        return env_vars, "qwen3"
    else:
        # Assume it's a custom model name for local server
        env_vars = {
            "ANTHROPIC_BASE_URL": "http://localhost:8080",
            "ANTHROPIC_AUTH_TOKEN": "llama",
            "ANTHROPIC_API_KEY": "",
        }
        return env_vars, model


def check_image_exists(image_name: str) -> bool:
    """Check if Docker image exists locally or can be pulled."""
    result = subprocess.run(
        ["podman", "image", "exists", f"docker.io/{image_name}"],
        capture_output=True
    )
    return result.returncode == 0


def pull_image(image_name: str) -> bool:
    """Pull a Docker image."""
    print(f"  Pulling image: {image_name}...")
    result = subprocess.run(
        ["podman", "pull", f"docker.io/{image_name}"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"    Failed to pull: {result.stderr}")
        return False
    print(f"    Image pulled successfully")
    return True


def cleanup_images(image_name: str):
    """Remove Docker images to save disk space."""
    safe_name = image_name.replace("/", "_").replace(":", "_")
    fixed_image = f"swebench-fixed-{safe_name}"

    # Remove fixed image
    subprocess.run(["podman", "rmi", "-f", fixed_image],
                   capture_output=True, timeout=60)

    # Remove original image
    subprocess.run(["podman", "rmi", "-f", f"docker.io/{image_name}"],
                   capture_output=True, timeout=60)

    # Prune dangling images
    subprocess.run(["podman", "image", "prune", "-f"],
                   capture_output=True, timeout=30)


def load_progress(progress_file: Path) -> set:
    """Load completed tasks from progress file."""
    if progress_file.exists():
        with open(progress_file, "r") as f:
            progress = json.load(f)
            return set(progress.get('completed', []))
    return set()


def save_progress(progress_file: Path, task_key: str, result: dict):
    """Save progress to file."""
    progress = {'completed': [], 'results': {}}
    if progress_file.exists():
        with open(progress_file, "r") as f:
            progress = json.load(f)
        progress['completed'].append(task_key)
    else:
        progress['completed'] = [task_key]

    progress['results'][task_key] = {
        'success': result.get('success'),
        'attempts': result.get('attempts', 1),
        'total_time': result.get('total_time', 0),
    }

    with open(progress_file, "w") as f:
        json.dump(progress, f, indent=2)


def check_success(result: dict) -> bool:
    """Check if the attempt was successful."""
    output = result.get('claude_output', {}).get('stdout', '')

    has_diff = 'diff --git' in output

    pass_indicators = ['passed', 'all tests', 'tests passed', 'tests pass', 'OK', '0 failed']
    has_pass_indicator = any(kw.lower() in output.lower() for kw in pass_indicators)

    output_end = output[-2000:] if len(output) > 2000 else output
    output_cleaned = output_end.replace('xfailed', '').replace('xpassed', '')
    fail_indicators = ['FAILED', 'ERROR', 'failure', 'failed']
    has_fail_indicator = any(kw in output_cleaned for kw in fail_indicators)

    success = has_diff and has_pass_indicator and not has_fail_indicator
    return success


def run_single_task(task: dict, task_index: int, output_dir: Path,
                   max_retries: int = 1, model: str = "haiku") -> dict:
    """Run a single SWE-bench task using SWEBenchRunner."""
    task_dir = output_dir / f"task_{task_index}_{task['instance_id'].replace('/', '_')}"
    task_dir.mkdir(parents=True, exist_ok=True)

    result = {
        'task_index': task_index,
        'instance_id': task['instance_id'],
        'repo': task['repo'],
        'docker_image': task['docker_image'],
        'model': model,
        'start_time': datetime.now().isoformat(),
        'attempts': 0,
        'success': False,
    }

    for attempt in range(1, max_retries + 1):
        print(f"\n--- Attempt {attempt}/{max_retries} ---")
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

            # Get prompt (use None to let SWEBenchRunner use its default)
            # The prompt will be: 'Fix this issue: $(cat /issue.md). After fixing, run the tests to verify.'
            prompt = None
            model_env_vars, actual_model_name = get_model_env_vars(model)

            # Run with the specified model
            attempt_result = runner.run(prompt=prompt, run_tests=True, model=actual_model_name, extra_env=model_env_vars)
            result['attempt_results'] = attempt_result

            # Generate resource plot
            try:
                plot_title = f"Resource Usage - {task['instance_id']} (Attempt {attempt})"
                plot_from_attempt_dir(attempt_dir, title=plot_title)
            except Exception as pe:
                print(f"  Warning: Failed to generate plot: {pe}")

            # Check success
            if check_success(attempt_result):
                result['success'] = True
                result['successful_attempt'] = attempt
                print(f"  Task succeeded!")
                break
            else:
                print(f"  Task did not succeed")
                if attempt < max_retries:
                    print(f"  Retrying...")
                    continue
                break

        except Exception as e:
            error_str = str(e)
            print(f"Attempt {attempt} failed with error: {e}")
            with open(attempt_dir / "error.txt", "w") as f:
                f.write(error_str)

            # Check for image compatibility issues (skip retries for these)
            if "creating an ID-mapped copy of layer" in error_str or "error during chown" in error_str:
                print(f"  Image has compatibility issues with --userns=keep-id, skipping...")
                result['error'] = error_str
                result['skipped'] = True
                break

            if attempt >= max_retries:
                result['error'] = error_str

    result['end_time'] = datetime.now().isoformat()
    result['total_time'] = (
        datetime.fromisoformat(result['end_time']) -
        datetime.fromisoformat(result['start_time'])
    ).total_seconds()

    return result


def fetch_all_images_from_dataset(limit: Optional[int] = None) -> List[dict]:
    """Fetch all tasks with Docker images from SWE-rebench dataset."""
    from datasets import load_dataset

    print("Loading SWE-rebench dataset...")
    dataset = load_dataset("nebius/SWE-rebench", split="filtered")
    print(f"Loaded {len(dataset)} total tasks")

    # Filter for tasks with docker_image
    tasks_with_images = []
    for row in dataset:
        if row.get('docker_image'):
            tasks_with_images.append({
                'instance_id': row['instance_id'],
                'repo': row['repo'],
                'docker_image': row['docker_image'],
            })

    print(f"Found {len(tasks_with_images)} tasks with Docker images")

    if limit:
        tasks_with_images = tasks_with_images[:limit]
        print(f"Limiting to first {limit} tasks")

    return tasks_with_images


def collect_priority_images(prioritize_dir: Path) -> Set[str]:
    """Scan an experiment directory to collect docker_image names from results.json files."""
    priority_images = set()
    if not prioritize_dir.is_dir():
        print(f"Warning: prioritize directory does not exist: {prioritize_dir}")
        return priority_images

    for results_file in prioritize_dir.rglob("results.json"):
        try:
            with open(results_file, "r") as f:
                data = json.load(f)
            image = data.get("image")
            if image:
                priority_images.add(image)
        except Exception:
            pass

    return priority_images


def generate_task_list(tasks: List[dict], output_file: Path,
                       priority_images: Optional[Set[str]] = None) -> List[dict]:
    """
    Shuffle tasks, optionally putting priority images first, and save to a JSON file.
    Returns the ordered task list.
    """
    if priority_images:
        priority_tasks = [t for t in tasks if t['docker_image'] in priority_images]
        other_tasks = [t for t in tasks if t['docker_image'] not in priority_images]
        random.shuffle(priority_tasks)
        random.shuffle(other_tasks)
        ordered = priority_tasks + other_tasks
        print(f"Priority tasks (from previous experiments): {len(priority_tasks)}")
        print(f"Other tasks: {len(other_tasks)}")
    else:
        ordered = list(tasks)
        random.shuffle(ordered)

    with open(output_file, "w") as f:
        json.dump(ordered, f, indent=2)

    print(f"Task list saved to: {output_file}")
    print(f"Total tasks: {len(ordered)}")
    return ordered


def load_task_list(task_list_file: Path) -> List[dict]:
    """Load task list from a JSON file."""
    with open(task_list_file, "r") as f:
        tasks = json.load(f)
    print(f"Loaded {len(tasks)} tasks from {task_list_file}")
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Run All SWE-bench Images Runner")
    parser.add_argument("--max-retries", type=int, default=1, help="Max retries per task")
    parser.add_argument("--output-dir", help="Custom output directory")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of tasks to run")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from progress (skip completed tasks)")
    parser.add_argument("--model", default="qwen3", choices=["haiku", "qwen3"],
                        help="Model to use: haiku (default) or qwen3 (local llama-server)")

    # Task list options
    parser.add_argument("--generate-task-list", metavar="FILE",
                        help="Generate a shuffled task list and save to FILE (e.g. task_list.json), then exit")
    parser.add_argument("--prioritize-dir", metavar="DIR",
                        help="When generating task list, prioritize docker images that appeared in this experiment directory")
    parser.add_argument("--task-list", metavar="FILE",
                        help="Run tasks from a previously saved task list file instead of fetching from dataset")

    args = parser.parse_args()

    # Setup output directory - use script's parent directory (agentcgroup)
    script_dir = Path(__file__).parent.parent
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        # Use "local" suffix for local models like qwen3
        model_suffix = "local" if args.model in ["qwen3"] else args.model
        output_dir = script_dir / "experiments" / f"all_images_{model_suffix}"

    output_dir.mkdir(parents=True, exist_ok=True)
    progress_file = output_dir / "progress.json"

    # --- Mode 1: Generate task list and exit ---
    if args.generate_task_list:
        tasks = fetch_all_images_from_dataset(limit=args.limit)

        priority_images = None
        if args.prioritize_dir:
            prioritize_path = Path(args.prioritize_dir)
            if not prioritize_path.is_absolute():
                prioritize_path = script_dir / prioritize_path
            priority_images = collect_priority_images(prioritize_path)
            print(f"Found {len(priority_images)} priority images from {prioritize_path}")

        task_list_path = Path(args.generate_task_list)
        if not task_list_path.is_absolute():
            task_list_path = script_dir / task_list_path
        generate_task_list(tasks, task_list_path, priority_images)
        return 0

    # --- Mode 2: Run tasks ---

    # Load completed tasks if resuming
    completed = set()
    if args.resume:
        completed = load_progress(progress_file)
        print(f"Resuming from progress: {len(completed)} tasks already completed")

    # Get tasks: from saved list or from dataset
    if args.task_list:
        task_list_path = Path(args.task_list)
        if not task_list_path.is_absolute():
            task_list_path = script_dir / task_list_path
        tasks = load_task_list(task_list_path)
    else:
        tasks = fetch_all_images_from_dataset(limit=args.limit)
        random.shuffle(tasks)

    print(f"\n{'='*60}")
    print(f"Batch All SWE-bench Images Runner")
    print(f"{'='*60}")
    print(f"Model: {args.model}")
    print(f"Total tasks: {len(tasks)}")
    print(f"Already completed: {len(completed)}")
    print(f"Remaining tasks: {len(tasks) - len(completed)}")
    print(f"Output directory: {output_dir}")
    print(f"Max retries: {args.max_retries}")
    print(f"{'='*60}\n")

    results = []
    for i, task in enumerate(tasks, 1):
        task_key = task['instance_id']

        if task_key in completed:
            print(f"[{i}/{len(tasks)}] Skipping {task_key} (already completed)")
            continue

        print(f"\n{'='*60}")
        print(f"[{i}/{len(tasks)}] Running: {task_key}")
        print(f"Repo: {task['repo']}")
        print(f"Image: {task['docker_image']}")
        print(f"Model: {args.model}")
        print(f"{'='*60}")

        # Step 1: Pull image
        if not pull_image(task['docker_image']):
            print(f"  Skipping due to pull failure")
            continue

        # Step 2: Run task
        result = run_single_task(task, i, output_dir, max_retries=args.max_retries, model=args.model)
        results.append(result)

        # Step 3: Save progress
        save_progress(progress_file, task_key, result)

        # Step 4: Cleanup images
        print(f"\nCleaning up images...")
        cleanup_images(task['docker_image'])

        status = "SUCCESS" if result.get('success') else "FAILED"
        print(f"\n[{task_key}] {status}")
        if result.get('total_time'):
            print(f"Total time: {result['total_time']:.1f}s")

    # Generate final report
    print(f"\n{'='*60}")
    print("Generating final report...")
    print(f"{'='*60}")

    summary = {
        'total_tasks': len(results),
        'successful': sum(1 for r in results if r.get('success')),
        'failed': sum(1 for r in results if not r.get('success')),
        'total_time': sum(r.get('total_time', 0) for r in results),
        'results': results,
    }

    with open(output_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    report = f"""# All SWE-bench Images Test Report

Generated: {datetime.now().isoformat()}
Model: {args.model}

## Summary

- **Total Tasks**: {summary['total_tasks']}
- **Successful**: {summary['successful']}
- **Failed**: {summary['failed']}
- **Success Rate**: {summary['successful']/max(summary['total_tasks'], 1)*100:.1f}%
- **Total Time**: {summary['total_time']:.1f}s

## Results by Task

| Index | Instance ID | Success | Attempts | Time (s) |
|-------|-------------|---------|----------|----------|
"""
    for r in results:
        status = "Yes" if r.get('success') else "No"
        report += f"| {r.get('task_index')} | {r.get('instance_id')} | {status} | {r.get('attempts')} | {r.get('total_time', 0):.1f} |\n"

    with open(output_dir / "report.md", "w") as f:
        f.write(report)

    print(f"\n{'='*60}")
    print("Final Summary")
    print(f"{'='*60}")
    print(f"Model: {args.model}")
    print(f"Total: {summary['total_tasks']}, Success: {summary['successful']}, Failed: {summary['failed']}")
    print(f"Success Rate: {summary['successful']/max(summary['total_tasks'], 1)*100:.1f}%")
    print(f"\nReport saved to:")
    print(f"  - {output_dir / 'summary.json'}")
    print(f"  - {output_dir / 'report.md'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

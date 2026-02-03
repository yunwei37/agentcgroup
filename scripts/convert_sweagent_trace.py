#!/usr/bin/env python3
"""Convert SWE-agent trajectories to unified Trace IR format."""

import json
import re
import sys
from pathlib import Path


def extract_commands(trajectory: list) -> list:
    """Extract commands from SWE-agent trajectory steps."""
    steps = []
    step_id = 0

    for msg in trajectory:
        if msg.get('role') != 'ai' or not msg.get('text'):
            continue

        text = msg['text']

        # SWE-agent commands are in code blocks or at end of message
        # Common patterns: open, edit, goto, scroll_down, scroll_up, submit
        # Also bash-like commands

        # Extract code blocks
        code_blocks = re.findall(r'```(?:\w*\n)?(.*?)```', text, re.DOTALL)

        for block in code_blocks:
            block = block.strip()
            if not block:
                continue

            # Determine tool type
            tool = 'bash'  # default

            # SWE-agent specific commands
            if block.startswith(('open ', 'goto ', 'scroll_', 'edit ', 'submit', 'create ', 'search_')):
                tool = 'swe_agent_editor'

            steps.append({
                'step_id': step_id,
                'tool': tool,
                'command': block,
                'timeout_ms': 60000
            })
            step_id += 1

        # Also check for inline commands (last line of message)
        lines = text.strip().split('\n')
        last_line = lines[-1].strip() if lines else ''

        # Skip if we already captured it or it's just prose
        if last_line and not last_line.startswith(('```', '#', 'The ', 'This ', 'We ', 'I ')):
            if last_line.startswith(('open ', 'goto ', 'scroll_', 'edit ', 'submit')):
                steps.append({
                    'step_id': step_id,
                    'tool': 'swe_agent_editor',
                    'command': last_line,
                    'timeout_ms': 60000
                })
                step_id += 1

    return steps


def convert_sweagent_trace(row: dict) -> dict:
    """Convert a single SWE-agent trajectory to Trace IR."""
    trajectory = row.get('trajectory', [])
    steps = extract_commands(trajectory)

    return {
        'trace_id': row.get('instance_id', 'unknown'),
        'source': 'sweagent',
        'model': row.get('model_name', 'unknown'),
        'exit_status': row.get('exit_status', 'unknown'),
        'steps': steps
    }


def main():
    import pandas as pd

    # Load sample parquet
    cache_path = Path.home() / '.cache/huggingface/hub/datasets--nebius--SWE-agent-trajectories/snapshots'
    parquet_files = list(cache_path.glob('*/data/train-00000-of-00012.parquet'))

    if not parquet_files:
        print("No parquet file found. Run download first.", file=sys.stderr)
        sys.exit(1)

    df = pd.read_parquet(parquet_files[0])

    # Convert first few traces
    output_dir = Path(__file__).parent.parent / 'data/sample_traces'
    output_dir.mkdir(parents=True, exist_ok=True)

    for i in range(min(10, len(df))):
        row = df.iloc[i].to_dict()
        trace_ir = convert_sweagent_trace(row)

        # Use index suffix for unique filenames
        trace_name = f"{trace_ir['trace_id']}_{i:04d}"
        output_file = output_dir / f"{trace_name}.json"
        with open(output_file, 'w') as f:
            json.dump(trace_ir, f, indent=2)

        print(f"Converted: {trace_name} ({len(trace_ir['steps'])} steps)")


if __name__ == '__main__':
    main()

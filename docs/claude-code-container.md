# Running Claude Code in Docker/Podman Containers

This document describes how to run Claude Code CLI inside containers with resource limits (memory, CPU) while sharing the host's filesystem and libraries.

## Motivation

For AgentCgroup experiments, we need to:
- Run Claude Code with controlled resource limits
- Collect trace logs for analysis
- Test on standardized environments (e.g., SWE-bench Docker images)

## Working Configuration

After extensive testing, the following configuration works:

```bash
podman run --rm \
    --userns=keep-id \
    --network=host \
    -v /usr:/usr:ro \
    -v /lib:/lib:ro \
    -v /lib64:/lib64:ro \
    -v /etc:/etc:ro \
    -v /bin:/bin:ro \
    -v /sbin:/sbin:ro \
    -v /home:/home \
    -v /tmp:/tmp \
    -v /var:/var \
    -w /tmp \
    -e HOME=/home/$(whoami) \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/library/debian:bookworm-slim \
    claude --model haiku --print --dangerously-skip-permissions "Your prompt here"
```

## Key Parameters Explained

| Parameter | Purpose |
|-----------|---------|
| `--userns=keep-id` | Preserves host user ID, required for writing to ~/.claude |
| `--network=host` | Uses host networking for API calls |
| `-v /usr:/usr:ro` | Shares Node.js, Claude CLI, and system binaries |
| `-v /lib:/lib:ro` | Shares system libraries (glibc, libnode, etc.) |
| `-v /lib64:/lib64:ro` | Shares 64-bit libraries |
| `-v /etc:/etc:ro` | Shares SSL certificates and system config |
| `-v /home:/home` | Shares home directory for ~/.claude config and traces |
| `-v /tmp:/tmp` | Shares temp directory |
| `--memory=4g` | Memory limit for resource control experiments |
| `--cpus=2` | CPU limit for resource control experiments |

## Trace Log Location

Claude Code writes trace logs to `~/.claude/projects/<workdir>/`.

For example, if working directory is `/tmp`, traces go to:
```
~/.claude/projects/-tmp/<session-id>.jsonl
```

## Tested Scenarios

### Basic Test (Working)
```bash
# Simple prompt
podman run --rm \
    --userns=keep-id \
    --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /tmp \
    -e HOME=/home/yunwei37 \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/library/debian:bookworm-slim \
    claude --model haiku --print --dangerously-skip-permissions "What is 2+2?"

# Output: 4
```

### Code Generation (Working)
```bash
# Generate code
podman run --rm \
    --userns=keep-id \
    --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /tmp \
    -e HOME=/home/yunwei37 \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/library/debian:bookworm-slim \
    claude --model haiku --print --dangerously-skip-permissions \
    "Write a Python function that checks if a number is prime."

# Claude creates /tmp/prime.py with the implementation
```

### SWE-bench Container (Working)

SWE-bench provides standardized Docker images for evaluating code repair agents. Each image contains a specific GitHub issue's codebase at the exact commit before the fix.

**Image naming format**: `swerebench/sweb.eval.x86_64.<repo>_<id>_<repo>-<issue>`

**Example**: Fixing starlette issue #1147 (session cookie path)

```bash
# Pull the SWE-bench image
podman pull docker.io/swerebench/sweb.eval.x86_64.encode_1776_starlette-1147

# Run Claude haiku to fix the issue
podman run --rm \
    --userns=keep-id \
    --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /testbed \
    -e HOME=/home/yunwei37 \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 \
    bash -c '
        git config --global user.email "test@test.com"
        git config --global user.name "Test"
        git config --global --add safe.directory /testbed

        claude --model haiku --print --dangerously-skip-permissions \
            "Fix this issue: $(cat /issue.md)"

        git diff
    '
```

**Note**: First run with `--userns=keep-id` may take longer as Podman creates ID-mapped layer copies. Subsequent runs are faster.

**Important**: The `/testbed` directory in SWE-bench images is owned by root. You need a two-step process:

**Step 1**: Create a modified image with fixed permissions
```bash
# Create temp container, fix permissions, commit as new image
TEMP=$(podman run -d docker.io/swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 sleep 60)
podman exec $TEMP chown -R 1000:1000 /testbed
podman commit $TEMP swebench-starlette-1147-fixed
podman stop $TEMP && podman rm $TEMP
```

**Step 2**: Run Claude with userns=keep-id
```bash
podman run --rm \
    --userns=keep-id \
    --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /testbed \
    -e HOME=/home/$(whoami) \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    swebench-starlette-1147-fixed \
    bash -c '
        git config user.email "test@test.com"
        git config user.name "Test"
        git config --add safe.directory /testbed

        claude --model haiku --print --dangerously-skip-permissions \
            "Fix this issue: $(cat /issue.md)"

        git diff
    '
```

**Example output** (starlette-1147 fix):
```diff
diff --git a/starlette/middleware/sessions.py b/starlette/middleware/sessions.py
-                    header_value = "%s=%s; path=/; Max-Age=%d; %s" % (
+                    path = scope.get("root_path", "") or "/"
+                    header_value = "%s=%s; path=%s; Max-Age=%d; %s" % (
                         self.session_cookie,
                         data.decode("utf-8"),
+                        path,
                         self.max_age,
```

The trace logs will be saved to `~/.claude/projects/-testbed/`.

**Available SWE-rebench images** (with Docker support):
- Data Processing: dask (72), geopandas (20)
- ML/Scientific: pennylane (76), haystack (22)
- DevOps/Build: conan (73), dvc (72), briefcase (40)
- Web/Network: starlette (27), httpx (25)

See: https://huggingface.co/datasets/nebius/SWE-rebench

## Troubleshooting

### Problem: Permission denied writing to ~/.claude
**Solution**: Use `--userns=keep-id` to preserve user ID mapping.

### Problem: SSL certificate errors / HTTPS failures
**Solution**: Mount `/etc:/etc:ro` to share SSL certificates.

### Problem: libnode.so.115 not found
**Solution**: Mount both `/lib:/lib:ro` and `/lib64:/lib64:ro`.

### Problem: Claude hangs with no output
**Possible causes**:
1. Missing SSL certificates (mount /etc)
2. User ID mismatch (use --userns=keep-id)
3. stdin issues (ensure not running interactively without -it)

### Problem: "Cannot run with root privileges"
**Solution**: Use `--userns=keep-id` instead of running as root or using `--user`.

## Failed Approaches

The following approaches did NOT work:

1. **Mounting only specific libraries**: Too many dependencies, hard to track all of them.

2. **Using `--user $(id -u):$(id -g)` without `--userns=keep-id`**: User ID mapping issues cause permission errors.

3. **Not mounting /etc**: SSL certificate verification fails, causing API calls to hang.

4. **Mounting ~/.claude to a different path**: Claude expects ~/.claude relative to $HOME.

## Resource Monitoring

To monitor resource usage during experiments:

```bash
# In another terminal, watch container stats
podman stats --no-stream <container-id>

# Or use cgroup metrics directly
cat /sys/fs/cgroup/user.slice/user-1000.slice/memory.current
```

## Next Steps

1. Test with more complex SWE-bench tasks
2. Integrate with trace collection scripts
3. Add cgroup monitoring hooks

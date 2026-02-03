# Running Host OpenCode in Resource-Limited Containers

This document describes how to run the host machine's OpenCode CLI inside Docker/Podman containers with memory and CPU limits, without needing to install OpenCode or configure API tokens inside the container.

## Motivation

When profiling AI coding agents, you may want:
- **Resource isolation**: Limit memory and CPU usage
- **No reinstallation**: Use existing OpenCode binary and authentication
- **Simplicity**: Avoid managing API keys inside containers

## Prerequisites

- OpenCode installed on host: `/usr/local/bin/opencode`
- OpenCode authentication configured: `~/.local/share/opencode/auth.json`
- Docker or Podman installed

## Quick Start

```bash
podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  -v /path/to/your/workspace:/workspace \
  -w /workspace \
  --memory=4g --cpus=2 \
  docker.io/library/debian:bookworm-slim \
  bash -c "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && git config --global --add safe.directory /workspace && /usr/local/bin/opencode run 'Your prompt here'"
```

## Detailed Explanation

### What Gets Mounted

| Mount | Source | Target | Purpose |
|-------|--------|--------|---------|
| OpenCode binary | `/usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode` | `/usr/local/bin/opencode` | The native binary (~145MB) |
| Auth file | `~/.local/share/opencode/auth.json` | `/root/.local/share/opencode/auth.json` | API credentials |
| Workspace | Your code directory | `/workspace` | Directory for OpenCode to operate on |

### Resource Limits

| Flag | Example | Description |
|------|---------|-------------|
| `--memory` | `4g`, `8g` | Maximum memory the container can use |
| `--cpus` | `2`, `4` | Number of CPU cores available |

### Important Notes

1. **Only mount `auth.json`**: Do NOT mount the entire `~/.local/share/opencode/` directory. It contains session data that can cause errors like `Error: repository name must have at least one component`.

2. **Git safe.directory**: The workspace needs to be marked as a safe directory for git operations inside the container.

3. **Git installation**: OpenCode requires git to be available in the container.

## Finding OpenCode Paths

### Locate OpenCode binary
```bash
# Find the symlink
which opencode
# Output: /usr/local/bin/opencode

# Find the actual binary
file $(which opencode)
# Output: symbolic link to ../lib/node_modules/opencode-ai/bin/opencode

# The native binary location
ls /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/
# Output: opencode (145MB binary)
```

### Locate auth file
```bash
ls ~/.local/share/opencode/
# Look for auth.json
```

## Troubleshooting

### Error: "repository name must have at least one component"

**Cause**: Mounting the entire `~/.local/share/opencode/` directory instead of just `auth.json`.

**Solution**: Only mount the auth file:
```bash
-v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro
```

### Error: "dubious ownership in repository"

**Cause**: Git security check for mounted directories.

**Solution**: Add safe.directory config:
```bash
git config --global --add safe.directory /workspace
```

### OpenCode not found / wrong platform

**Cause**: Mounting the wrong binary for your platform.

**Solution**: Check available binaries:
```bash
ls /usr/local/lib/node_modules/opencode-ai/node_modules/
# Options: opencode-linux-x64, opencode-linux-arm64, opencode-darwin-x64, etc.
```

## Example: Interactive Session

For an interactive session with resource limits:

```bash
podman run --rm -it \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  -v ~/my-project:/workspace \
  -w /workspace \
  --memory=8g --cpus=4 \
  docker.io/library/debian:bookworm-slim \
  bash -c "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && git config --global --add safe.directory /workspace && /usr/local/bin/opencode"
```

## Example: Non-Interactive with Prompt

For automated runs with a specific prompt:

```bash
podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  -v ~/my-project:/workspace \
  -w /workspace \
  --memory=4g --cpus=2 \
  docker.io/library/debian:bookworm-slim \
  bash -c "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && git config --global --add safe.directory /workspace && /usr/local/bin/opencode run 'Fix the bug in main.py'" 2>&1 | tee output.log
```

## Pre-built Container Image (Optional)

To avoid installing git on every run, create a simple Dockerfile:

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/opencode"]
```

Build and use:
```bash
podman build -t opencode-runner .

podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  -v ~/my-project:/workspace \
  -w /workspace \
  --memory=4g --cpus=2 \
  opencode-runner run 'Your prompt here'
```

## Comparison with Full Container Installation

| Aspect | Host Binary Mount | Full Installation |
|--------|-------------------|-------------------|
| Setup time | Instant | Requires npm install |
| API key management | Uses host auth | Needs ANTHROPIC_API_KEY |
| Image size | ~50MB (debian:slim) | ~500MB+ (with Node.js) |
| Updates | Automatic (host binary) | Manual rebuild |
| Offline capability | Depends on host | Self-contained |

## Verification

Test that everything works:

```bash
# Check version
podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  docker.io/library/debian:bookworm-slim \
  /usr/local/bin/opencode --version

# Check auth
podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  docker.io/library/debian:bookworm-slim \
  /usr/local/bin/opencode auth list

# Test run
podman run --rm \
  -v /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode:/usr/local/bin/opencode:ro \
  -v ~/.local/share/opencode/auth.json:/root/.local/share/opencode/auth.json:ro \
  -v ~/any-git-repo:/workspace \
  -w /workspace \
  --memory=4g --cpus=2 \
  docker.io/library/debian:bookworm-slim \
  bash -c "apt-get update -qq && apt-get install -y -qq git >/dev/null 2>&1 && git config --global --add safe.directory /workspace && /usr/local/bin/opencode run 'Say hello'"
```

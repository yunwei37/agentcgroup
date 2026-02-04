# 实验日志 1: 在容器中运行 Claude Code 解决 SWE-bench 问题

**日期**: 2026-02-04
**目标**: 验证在 Docker/Podman 容器中运行 Claude Code (haiku) 解决 SWE-bench 问题的可行性
**实验环境**: Ubuntu Linux, Podman, Claude Code 2.1.16

---

## 1. 实验背景

### 1.1 目标
为 AgentCgroup 项目验证：
1. 能否在容器中运行 Claude Code CLI
2. 能否对容器施加资源限制 (memory, CPU)
3. 能否收集 Claude Code 的 trace 日志
4. 能否用 Claude Code 解决真实的 SWE-bench 问题

### 1.2 SWE-bench 简介
SWE-bench 是一个评估 AI 代码修复能力的 benchmark，包含 2294 个真实的 GitHub issues。SWE-rebench 提供了 3410 个带 Docker 镜像的 tasks。

**本次测试使用的 issue**: `encode/starlette#1147`
- **问题**: Session cookie 使用硬编码的 `path=/`，应该使用 ASGI root path
- **Docker 镜像**: `swerebench/sweb.eval.x86_64.encode_1776_starlette-1147`

---

## 2. 实验方法

### 2.1 容器配置探索

#### 2.1.1 初始尝试：挂载单独的库
```bash
# 失败：缺少依赖库
podman run --rm \
    -v /usr/bin/node:/usr/bin/node:ro \
    -v /usr/lib/x86_64-linux-gnu/libnode.so.115:/usr/lib/x86_64-linux-gnu/libnode.so.115:ro \
    ...
```
**结果**: 失败，Node.js 依赖太多库（libnode, libuv, libssl, libcrypto, libicui18n 等）

#### 2.1.2 尝试挂载 /host-lib
```bash
# 失败：glibc 版本不兼容
-v /lib/x86_64-linux-gnu:/host-lib:ro
-e LD_LIBRARY_PATH=/host-lib
```
**结果**: 失败，`undefined symbol: __tunable_is_initialized, version GLIBC_PRIVATE`

#### 2.1.3 最终方案：挂载所有关键目录到相同路径
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
    ...
```
**结果**: 成功！

### 2.2 关键参数说明

| 参数 | 作用 | 为什么需要 |
|------|------|-----------|
| `--userns=keep-id` | 保持宿主机用户 ID | Claude 需要写入 ~/.claude |
| `--network=host` | 使用宿主机网络 | API 调用需要网络 |
| `-v /usr:/usr:ro` | 挂载 /usr | Node.js, Claude CLI 在此 |
| `-v /lib:/lib:ro` | 挂载 /lib | 系统库 (glibc, libnode 等) |
| `-v /etc:/etc:ro` | 挂载 /etc | SSL 证书（关键！） |
| `-v /home:/home` | 挂载 home | ~/.claude 配置和 trace |
| `--memory=4g` | 内存限制 | 资源控制实验 |
| `--cpus=2` | CPU 限制 | 资源控制实验 |

### 2.3 遇到的问题与解决方案

#### 问题 1: Claude 无输出，一直超时
**原因**: 缺少 SSL 证书，HTTPS 请求失败
**解决**: 挂载 `-v /etc:/etc:ro`

#### 问题 2: Permission denied writing to ~/.claude
**原因**: 用户 ID 映射问题
**解决**: 使用 `--userns=keep-id`

#### 问题 3: "Cannot run with root privileges"
**原因**: `--dangerously-skip-permissions` 不能在 root 下运行
**解决**: 使用 `--userns=keep-id`（容器内用户不是真正的 root）

#### 问题 4: SWE-bench 容器内 /testbed 是 root 拥有的
**原因**: 镜像内文件所有权是 root
**解决**: 两步法
```bash
# Step 1: 创建修改权限的镜像
TEMP=$(podman run -d <image> sleep 60)
podman exec $TEMP chown -R 1000:1000 /testbed
podman commit $TEMP <image>-fixed

# Step 2: 用 userns=keep-id 运行
podman run --userns=keep-id ... <image>-fixed
```

---

## 3. 实验结果

### 3.1 基础测试

**命令**:
```bash
podman run --rm \
    --userns=keep-id --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /tmp \
    -e HOME=/home/yunwei37 \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/library/debian:bookworm-slim \
    claude --model haiku --print --dangerously-skip-permissions "What is 2+2?"
```

**结果**: `4` ✅

### 3.2 SWE-bench 修复测试 (只修复，不测试)

**Issue**: starlette#1147 - Session cookie should use root path

**命令**:
```bash
podman run --rm \
    --userns=keep-id --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /testbed \
    -e HOME=/home/yunwei37 \
    -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    swebench-starlette-fixed \
    bash -c '
        git config user.email "test@test.com"
        git config user.name "Test"
        git config --add safe.directory /testbed

        claude --model haiku --print --dangerously-skip-permissions \
            "Fix starlette/middleware/sessions.py: change hardcoded path=\"/\" to use scope.get(\"root_path\", \"\") or \"/\"."

        git diff
    '
```

**结果统计**:
| 指标 | 值 |
|------|-----|
| 总耗时 | 11.9 秒 |
| 工具调用 | 4 次 |
| 事件数 | 15 |

**工具调用序列**:
1. `Read` - 读取 sessions.py
2. `Edit` - 修改第一处 path=/
3. `Edit` - 修改第二处 path=/
4. `Read` - 验证修改

**Git diff**:
```diff
diff --git a/starlette/middleware/sessions.py b/starlette/middleware/sessions.py
-                    header_value = "%s=%s; path=/; Max-Age=%d; %s" % (
+                    path = scope.get("root_path", "") or "/"
+                    header_value = "%s=%s; path=%s; Max-Age=%d; %s" % (
                         self.session_cookie,
                         data.decode("utf-8"),
+                        path,
                         self.max_age,
                         self.security_flags,
                     )
```

### 3.3 SWE-bench 完整测试 (修复 + 测试)

**命令**:
```bash
claude --model haiku --print --dangerously-skip-permissions \
    "Fix this issue in starlette/middleware/sessions.py: $(cat /issue.md)

    After fixing, run the relevant tests to verify your fix works."
```

**结果统计**:
| 指标 | 值 |
|------|-----|
| 总耗时 | 119.6 秒 (2 分钟) |
| 工具调用 | 28 次 |
| 事件数 | 80 |

**工具调用统计**:
| 工具 | 次数 | 用途 |
|------|------|------|
| Bash | 14 | 运行 pytest、安装依赖 |
| Read | 6 | 读取源码和测试文件 |
| TodoWrite | 4 | 跟踪任务进度 |
| Edit | 3 | 修改源码和测试 |
| Task | 1 | 子任务 |

**工具调用详细序列**:
```
1.  Read: /testbed/starlette/middleware/sessions.py
2.  Task: (探索代码库)
3.  Read: /testbed/tests/middleware/test_session.py
4.  TodoWrite: (记录任务)
5.  Edit: /testbed/starlette/middleware/sessions.py (修复)
6.  TodoWrite
7.  Edit: /testbed/tests/middleware/test_session.py (添加测试)
8.  TodoWrite
9.  Bash: python -m pytest tests/middleware/test_session.py
10. Bash: python -m pytest tests/middleware/test_session.py
11. Read: /testbed/setup.cfg
12. Bash: pip install pytest-cov -q
13. Bash: python -m pytest tests/middleware/test_session.py
14. Bash: python -m pip install -e . -q
15. Bash: python -m pytest tests/middleware/test_session.py
16. Bash: ls -la /testbed/ | grep -E 'requirements|setup'
17. Read: /testbed/requirements.txt
18. Bash: python -m pip install itsdangerous pytest-cov -q
19. Bash: python -m pytest tests/middleware/test_session.py
20. Bash: python -m pip install pytest-asyncio -q
21. Bash: python -m pytest tests/middleware/test_session.py
22. Bash: python -m pytest tests/middleware/test_session.py
23. Read: /testbed/starlette/testclient.py
24. Edit: /testbed/tests/middleware/test_session.py (修复测试)
25. Bash: python -m pytest tests/middleware/test_session.py
26. TodoWrite
27. Read: /testbed/starlette/middleware/sessions.py (验证)
28. Bash: python -m pytest tests/middleware/ -v (最终验证)
```

**最终结果**:
- ✅ 修复了 sessions.py (2 处修改)
- ✅ 添加了新测试 `test_session_with_root_path`
- ✅ 37 个 middleware 测试全部通过

**完整 Git diff**:
```diff
diff --git a/starlette/middleware/sessions.py b/starlette/middleware/sessions.py
index d1b1d5a..e49929d 100644
--- a/starlette/middleware/sessions.py
+++ b/starlette/middleware/sessions.py
@@ -54,9 +54,11 @@ class SessionMiddleware:
                     data = b64encode(json.dumps(scope["session"]).encode("utf-8"))
                     data = self.signer.sign(data)
                     headers = MutableHeaders(scope=message)
-                    header_value = "%s=%s; path=/; Max-Age=%d; %s" % (
+                    path = scope.get("root_path", "") or "/"
+                    header_value = "%s=%s; path=%s; Max-Age=%d; %s" % (
                         self.session_cookie,
                         data.decode("utf-8"),
+                        path,
                         self.max_age,
                         self.security_flags,
                     )
@@ -64,9 +66,11 @@ class SessionMiddleware:
                 elif not initial_session_was_empty:
                     # The session has been cleared.
                     headers = MutableHeaders(scope=message)
-                    header_value = "{}={}; {}".format(
+                    path = scope.get("root_path", "") or "/"
+                    header_value = "{}={}; path={}; expires=Thu, 01 Jan 1970 00:00:00 GMT; {}".format(
                         self.session_cookie,
-                        "null; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT;",
+                        "null",
+                        path,
                         self.security_flags,
                     )
                     headers.append("Set-Cookie", header_value)

diff --git a/tests/middleware/test_session.py b/tests/middleware/test_session.py
index 3f71232..f33676a 100644
--- a/tests/middleware/test_session.py
+++ b/tests/middleware/test_session.py
@@ -101,3 +101,29 @@ def test_secure_session():

     response = secure_client.get("/view_session")
     assert response.json() == {"session": {}}
+
+
+def test_session_with_root_path():
+    app = create_app()
+    app.add_middleware(SessionMiddleware, secret_key="example")
+    client = TestClient(app, root_path="/app")
+
+    response = client.post("/update_session", json={"some": "data"})
+    assert response.json() == {"session": {"some": "data"}}
+
+    # check that cookie path uses root_path
+    set_cookie = response.headers["set-cookie"]
+    assert "path=/app" in set_cookie
+    ...
```

---

## 4. Trace 日志分析

### 4.1 Trace 文件位置
Claude Code 的 trace 日志保存在 `~/.claude/projects/<workdir>/` 目录下。

本次实验的 trace 文件：
- `~/.claude/projects/-testbed/f4a2df06-f20f-4740-b8c7-d2526d1d42b7.jsonl` (只修复)
- `~/.claude/projects/-testbed/d69f7721-1d5b-490b-922c-74af07d51bec.jsonl` (修复+测试)

### 4.2 Trace 格式
每行是一个 JSON 对象，包含：
- `type`: 事件类型 (user, assistant, queue-operation)
- `timestamp`: ISO 8601 时间戳
- `message`: 消息内容
  - `role`: user/assistant
  - `content`: 内容数组，包含 text 和 tool_use
- `toolUseResult`: 工具调用结果

### 4.3 Trace 示例
```json
{
  "type": "assistant",
  "timestamp": "2026-02-04T02:05:33.123Z",
  "message": {
    "role": "assistant",
    "content": [
      {"type": "text", "text": "I'll fix the session cookie path issue."},
      {"type": "tool_use", "id": "toolu_xxx", "name": "Read", "input": {"file_path": "/testbed/starlette/middleware/sessions.py"}}
    ]
  }
}
```

---

## 5. 资源使用

### 5.1 容器资源限制
- Memory: 4GB (`--memory=4g`)
- CPU: 2 cores (`--cpus=2`)

### 5.2 实际资源使用
可通过以下方式监控：
```bash
# 实时监控
podman stats <container-id>

# cgroup 指标
cat /sys/fs/cgroup/user.slice/user-1000.slice/memory.current
```

---

## 6. 结论与观察

### 6.1 可行性验证
✅ **成功**: 可以在 Podman 容器中运行 Claude Code (haiku) 解决 SWE-bench 问题

### 6.2 关键发现

1. **挂载策略**: 必须挂载完整的系统目录 (/usr, /lib, /etc 等)，不能只挂载单个文件
2. **用户映射**: `--userns=keep-id` 是关键参数，解决权限问题
3. **SSL 证书**: 必须挂载 /etc，否则 API 调用会失败
4. **SWE-bench 权限**: 需要两步法修改 /testbed 权限

### 6.3 性能观察

| 任务类型 | 耗时 | 工具调用 | 特点 |
|----------|------|----------|------|
| 只修复 | 12s | 4 | 快速，适合简单修复 |
| 修复+测试 | 120s | 28 | 完整，包含环境配置 |

**10x 耗时差异主要来自**:
- 安装缺失的 Python 依赖 (pytest-cov, pytest-asyncio)
- 多次运行测试排查问题
- 修复测试代码

### 6.4 后续工作

1. **资源监控**: 集成 cgroup 监控，收集详细的资源使用数据
2. **更多任务**: 测试更多 SWE-bench tasks (dask, pennylane, conan 等)
3. **对比实验**: 比较不同资源限制下的性能
4. **trace 分析**: 开发自动化工具分析 trace 中的时间分布

---

## 7. 复现指南

### 7.1 环境准备
```bash
# 安装 Podman
sudo apt install podman

# 确认 Claude Code 已安装
claude --version
# 输出: 2.1.16 (Claude Code)
```

### 7.2 运行实验

#### 基础测试
```bash
podman run --rm \
    --userns=keep-id --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /tmp \
    -e HOME=$HOME -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    docker.io/library/debian:bookworm-slim \
    claude --model haiku --print --dangerously-skip-permissions "What is 2+2?"
```

#### SWE-bench 测试
```bash
# Step 1: 拉取镜像并修改权限
podman pull docker.io/swerebench/sweb.eval.x86_64.encode_1776_starlette-1147
TEMP=$(podman run -d docker.io/swerebench/sweb.eval.x86_64.encode_1776_starlette-1147 sleep 60)
podman exec $TEMP chown -R $(id -u):$(id -g) /testbed
podman commit $TEMP swebench-starlette-fixed
podman stop $TEMP && podman rm $TEMP

# Step 2: 运行 Claude
podman run --rm \
    --userns=keep-id --network=host \
    -v /usr:/usr:ro -v /lib:/lib:ro -v /lib64:/lib64:ro \
    -v /etc:/etc:ro -v /bin:/bin:ro -v /sbin:/sbin:ro \
    -v /home:/home -v /tmp:/tmp -v /var:/var \
    -w /testbed \
    -e HOME=$HOME -e PATH=/usr/local/bin:/usr/bin:/bin \
    --memory=4g --cpus=2 \
    swebench-starlette-fixed \
    bash -c '
        git config user.email "test@test.com"
        git config user.name "Test"
        git config --add safe.directory /testbed

        claude --model haiku --print --dangerously-skip-permissions \
            "Fix this issue: $(cat /issue.md). After fixing, run tests."

        git diff
    '

# 清理
podman rmi swebench-starlette-fixed
```

### 7.3 查看 Trace
```bash
# 找到最新的 trace 文件
ls -lt ~/.claude/projects/-testbed/*.jsonl | head -1

# 分析 trace
python3 << 'EOF'
import json
trace_file = "~/.claude/projects/-testbed/<session-id>.jsonl"
with open(trace_file) as f:
    for line in f:
        e = json.loads(line)
        print(e.get('type'), e.get('timestamp'))
EOF
```

---

## 附录

### A. 失败的方法记录

| 方法 | 失败原因 |
|------|----------|
| 只挂载 libnode.so | 缺少其他依赖 (libuv, libssl 等) |
| 挂载到 /host-lib + LD_LIBRARY_PATH | glibc 版本不兼容 |
| 使用 `--user $(id -u):$(id -g)` | 无法写入 ~/.claude |
| 不挂载 /etc | SSL 证书缺失，API 调用失败 |
| 在 SWE-bench 容器内用 podman exec --user | 文件权限问题 |

### B. 相关文件

- 配置文档: `/home/yunwei37/agentcgroup/docs/claude-code-container.md`
- Trace 目录: `~/.claude/projects/-testbed/`
- SWE-bench 数据: `https://huggingface.co/datasets/nebius/SWE-rebench`

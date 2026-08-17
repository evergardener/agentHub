# MCP 共享工具层（Phase 7）

设计依据：设计文档 §3.7 / §Phase 7。Agent 间协作走 A2A，Agent 调工具走 MCP。

## 提供的 Server

| Server | 模块 | 工具 | 权限边界 |
|---|---|---|---|
| agent-filesystem | `tools.filesystem_server` | read_file / write_file / list_dir / search | 仅 `$AGENT_WORKSPACE` 根内，逃逸即 `PathEscapeError` |
| agent-git | `tools.git_server` | status / diff / log / commit | 仓库必须在根内；commit 必须显式列路径，禁止 `-A` |
| agent-browser | `tools.browser_server` | text / links | 只读 GET，仅 http/https；交互式浏览后置 |

均为 stdio 传输，启动方式：`python -m tools.<name>_server`。

## 挂载到 Codex（`~/.codex/config.toml`）

```toml
[mcp_servers.agent-filesystem]
command = "/Users/evergarden/Data/current-documents/Projects/local-agent-system/.venv/bin/python"
args = ["-m", "tools.filesystem_server"]
cwd = "/Users/evergarden/Data/current-documents/Projects/local-agent-system"

[mcp_servers.agent-filesystem.env]
PYTHONPATH = "/Users/evergarden/Data/current-documents/Projects/local-agent-system/src"
AGENT_WORKSPACE = "/Users/evergarden/AgentWorkspace"
```

git / browser 同理，替换模块名即可。

## 注意

- 启动命令中的 python 路径**不要 resolve 符号链接**——venv 依赖 `pyvenv.cfg`
  发现机制，解析后的真实路径会丢失 venv site-packages。
- FastMCP 启动 banner 走 stderr，不污染 stdio 协议通道。
- browser server 的 HTML→文本/链接提取用 stdlib `html.parser`，无重依赖；
  需要真实浏览器自动化时再接 playwright（后置）。

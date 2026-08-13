# DeepSeek Vision MCP

让纯文本 LLM 也能"看图"：通过 MCP 桥接 DeepSeek 网页版视觉对话，为 Claude Code / OpenCode / Codex / DeepSeek Harness 等任意 MCP 客户端提供本地图片分析能力。

- 使用你已登录的 DeepSeek 网页会话（非 API key，不消耗 API 额度）
- 单图 / 多图 / 批量 glob 分析、同一会话追问、Windows 自主截图
- 并发安全：跨进程共享一个网页会话，多分支并行不冲突

> **免责声明**：本项目是非官方逆向项目，通过分析 DeepSeek 网页版未公开接口实现，仅供学习研究使用。使用本项目即表示你了解并同意：自行承担风险，遵守 DeepSeek 服务条款，不得用于商业用途或对 DeepSeek 服务造成负担。`sha3_wasm_bg.wasm` 为 DeepSeek 网站资源，版权归其所有者。

## 特性

| 工具 | 说明 |
|---|---|
| `analyze_image` | 单张图片分析：看图、OCR、截图诊断、图表/界面/代码截图理解 |
| `analyze_images` | 多张图片联合分析：前后对比、多页截图、跨图信息核对 |
| `analyze_image_set` | 按目录/glob 批量收集分析：GUI 验收、before/after、多视口审查 |
| `continue_analysis` | 在同一视觉会话中继续追问（复用 `session_id`，不重复上传） |
| `capture_screen` | Windows 自主截图（整屏/当前窗口）并立即分析 |
| `vision_status` | 检查本地令牌、WASM、依赖是否就绪 |

## 原理

```
你的 LLM ──(MCP)──> ds_vision_mcp.py ──(HTTP)──> chat.deepseek.com
                        │
                        ├─ 读取 ~/.ds_web_token.json（网页登录令牌）
                        ├─ WASM 求解 DeepSeekHashV1 PoW 挑战
                        ├─ 上传图片 → fork 为 vision 模型文件
                        └─ SSE 流式 completion（含思考过程）
```

所有视觉请求复用同一个 DeepSeek 网页对话；每次工具调用返回独立逻辑分支 `session_id`，分支之间可并发，追问必须使用对应分支的 `session_id`。

## 安装

要求：Python 3.10+，Windows / Linux / macOS（自主截图仅 Windows）。

```bash
git clone https://github.com/zunyiqingfeng-code/deepseek-vision-mcp.git
cd deepseek-vision-mcp
pip install -r requirements.txt
```

依赖：`httpx`、`wasmtime`、`mcp`、`Pillow`、`mss`（Windows 截图可选）。

### 获取网页登录令牌

1. 用浏览器登录 [chat.deepseek.com](https://chat.deepseek.com)
2. F12 打开开发者工具 → Application / 存储 → Cookies，找到 `userToken`（或 localStorage 中的 `userToken`）
3. 写入本地文件（不要提交到仓库）：

```bash
# ~/.ds_web_token.json
{"token": "你的 userToken 值", "ts": 0, "from": "manual"}
```

也可以从浏览器任意请求头中复制 `Authorization: Bearer <token>` 的值。

### 配置 MCP 客户端

**Claude Code**（`.mcp.json`）:

```json
{
  "mcpServers": {
    "deepseek-vision": {
      "command": "python",
      "args": ["/绝对路径/deepseek-vision-mcp/ds_vision_mcp.py"]
    }
  }
}
```

**OpenCode**（`opencode.json`）:

```json
{
  "mcp": {
    "deepseek-vision": {
      "type": "local",
      "command": ["python", "/绝对路径/deepseek-vision-mcp/ds_vision_mcp.py"],
      "timeout": 180000,
      "enabled": true
    }
  }
}
```

**DeepSeek Harness**（`~/.dsh/profiles/web/cordis.patch.yml`）:

```yaml
- insert:
    - id: mcp-deepseek-vision
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: deepseek-vision
        transport: stdio
        command: python
        args:
          - C:/Users/你的用户名/ds-vision-mcp/ds_vision_mcp.py
        toolCallTimeoutMs: 180000
        failOnStartupError: false
```

## 使用

MCP 客户端会自动发现工具。模型侧的使用规则（可写入客户端 instructions）：

- 用户要求看图/识图/OCR/分析截图/检查界面/比较图片，或消息包含本地图片路径时，优先调用 `analyze_image` / `analyze_images`
- 不要因主模型不支持图片就回答"无法查看"——视觉由本 MCP 提供
- 同一图片追问必须用 `continue_analysis` 并传入上轮返回的 `session_id`，不要重新上传
- 用户说"看看屏幕/检查当前窗口"时调用 `capture_screen`，无需用户先截图
- 多张截图 / before-after / 多视口产物用 `analyze_image_set` 批量复核

### 命令行直接使用

```bash
python ds_vision.py 图片.png "这张图里有什么？"        # 单图问答
python ds_vision.py 图片.png "解释这个图表" --no-search  # 关闭联网搜索
```

## 测试

```bash
python -m unittest test_ds_vision test_ds_vision_mcp
```

测试全部使用 mock，不访问网络、不需要令牌。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DS_VISION_SESSION_FILE` | `~/ds_vision/.shared_session.json` | 共享会话状态文件路径 |
| `DS_VISION_LOCK_TIMEOUT` | `180` | 跨进程锁等待秒数 |
| `DS_VISION_MAX_PARALLEL` | `2` | DeepSeek 网页并发上限 |

## 许可证

[MIT](LICENSE)。注意：`sha3_wasm_bg.wasm` 为 DeepSeek 网站资源，版权归其所有者，仅用于本地 PoW 求解，不随 MIT 条款授权。

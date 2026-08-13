# DeepSeek Vision MCP

[中文](README.md) | **English**

Give text-only LLMs the ability to "see": this MCP server bridges the DeepSeek web vision conversation, providing local image analysis for any MCP client — Claude Code, OpenCode, Codex, DeepSeek Harness, and more.

- Uses your logged-in DeepSeek web session (no API key, no API quota consumption)
- Single / multi-image / batch glob analysis, follow-up in the same session, Windows screen capture
- Concurrency-safe: one shared web session across processes, parallel branches without conflicts

> **Disclaimer**: This is an unofficial reverse-engineered project built by analyzing DeepSeek's non-public web interfaces. It is provided for learning and research purposes only. By using this project you acknowledge and agree: use at your own risk, comply with DeepSeek's Terms of Service, do not use it commercially or place undue load on DeepSeek's services. `sha3_wasm_bg.wasm` is a resource from the DeepSeek website and belongs to its owner.

## Features

| Tool | Description |
|---|---|
| `analyze_image` | Analyze a single image: Q&A, OCR, screenshot diagnosis, charts/UI/code screenshots |
| `analyze_images` | Joint analysis of multiple images: before/after, multi-page screenshots, cross-image verification |
| `analyze_image_set` | Batch collect and analyze by directory/glob: GUI acceptance, before/after, multi-viewport review |
| `continue_analysis` | Follow up in the same vision session (reuse `session_id`, no re-upload) |
| `capture_screen` | Windows screen capture (full screen / active window) with immediate analysis |
| `vision_status` | Check local token, WASM, and dependency readiness |

## How It Works

```
Your LLM ──(MCP)──> ds_vision_mcp.py ──(HTTP)──> chat.deepseek.com
                        │
                        ├─ reads ~/.ds_web_token.json (web login token)
                        ├─ solves DeepSeekHashV1 PoW challenge via WASM
                        ├─ uploads images → forks them as vision-model files
                        └─ SSE streaming completion (including thinking)
```

All vision requests reuse the same DeepSeek web conversation; each tool call returns an independent logical branch `session_id`. Branches can run concurrently, and follow-ups must use the corresponding branch's `session_id`.

## Installation

Requirements: Python 3.10+, Windows / Linux / macOS (screen capture is Windows-only).

```bash
git clone https://github.com/zunyiqingfeng-code/deepseek-vision-mcp.git
cd deepseek-vision-mcp
pip install -r requirements.txt
```

Dependencies: `httpx`, `wasmtime`, `mcp`, `Pillow`, `mss` (optional, for Windows capture).

### Getting the Web Login Token

1. Log in to [chat.deepseek.com](https://chat.deepseek.com) in your browser
2. Press F12 → Application / Storage → Cookies, find `userToken` (or the same key in localStorage)
3. Write it to a local file (never commit it to a repository):

```bash
# ~/.ds_web_token.json
{"token": "your userToken value", "ts": 0, "from": "manual"}
```

Alternatively, copy the value of the `Authorization: Bearer <token>` header from any browser request.

### Configuring MCP Clients

**Claude Code** (`.mcp.json`):

```json
{
  "mcpServers": {
    "deepseek-vision": {
      "command": "python",
      "args": ["/absolute/path/deepseek-vision-mcp/ds_vision_mcp.py"]
    }
  }
}
```

**OpenCode** (`opencode.json`):

```json
{
  "mcp": {
    "deepseek-vision": {
      "type": "local",
      "command": ["python", "/absolute/path/deepseek-vision-mcp/ds_vision_mcp.py"],
      "timeout": 180000,
      "enabled": true
    }
  }
}
```

**DeepSeek Harness** (`~/.dsh/profiles/web/cordis.patch.yml`):

```yaml
- insert:
    - id: mcp-deepseek-vision
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: deepseek-vision
        transport: stdio
        command: python
        args:
          - C:/Users/your-username/ds-vision-mcp/ds_vision_mcp.py
        toolCallTimeoutMs: 180000
        failOnStartupError: false
```

## Usage

MCP clients discover the tools automatically. Suggested rules for the model (can be added to client instructions):

- When the user asks to view/OCR/analyze a screenshot/explain a chart/compare images, or the message contains a local image path, prefer `analyze_image` / `analyze_images`
- Never answer "cannot view images" just because the main model lacks vision — vision is provided by this MCP
- Follow-ups on the same image must use `continue_analysis` with the `session_id` from the previous turn; do not re-upload
- When the user says "look at the screen / check the current window", call `capture_screen` — no manual screenshot needed
- For multiple screenshots / before-after / multi-viewport artifacts, use `analyze_image_set` for batch review

### CLI Usage

```bash
python ds_vision.py image.png "What's in this image?"    # single-image Q&A
python ds_vision.py image.png "Explain this chart" --no-search
```

## Tests

```bash
python -m unittest test_ds_vision test_ds_vision_mcp
```

All tests are mocked: no network access, no token required.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DS_VISION_SESSION_FILE` | `~/ds_vision/.shared_session.json` | Shared session state file path |
| `DS_VISION_LOCK_TIMEOUT` | `180` | Cross-process lock wait timeout in seconds |
| `DS_VISION_MAX_PARALLEL` | `2` | DeepSeek web concurrency limit |

## License

[MIT](LICENSE). Note: `sha3_wasm_bg.wasm` is a resource from the DeepSeek website and belongs to its owner; it is used locally for PoW solving and is not covered by the MIT license.

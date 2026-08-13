# DeepSeek Harness 集成

本目录包含将本 MCP 集成到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）的组件。

## 1. 视觉 MCP（host 层）

在 `~/.dsh/profiles/web/cordis.patch.yml` 中追加（DSH 自带 `@deepseek-ai/dsh-mcp-client`，可直接连接本 MCP）：

```yaml
- insert:
    - id: mcp-deepseek-vision
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: deepseek-vision
        transport: stdio
        command: python
        args:
          - C:/你的路径/deepseek-vision-mcp/ds_vision_mcp.py
        toolCallTimeoutMs: 180000
        failOnStartupError: false
```

> 注意：`cordis.patch.yml` 中**新行必须放在 `insert` 列表里**。顶层 `- id:` 条目是"覆盖已有行"语义，指向不存在的行会被静默跳过 —— 这是最常见的集成失败原因。

## 2. 输入框发图插件（vision-send-image）

DeepSeek-V4-Flash 等纯文本模型不支持图片输入，GUI 会阻止粘贴图片。`vision-send-image` 插件解决这个问题：

- 输入框左侧出现 **📷 按钮**（选图）
- **Ctrl+V 直接粘贴图片**（全局 paste 监听）
- 图片自动保存到 `~/.dsh/vision-uploads/`，并把 `图片：<路径>` 插入输入框
- 发送后模型用视觉 MCP 自动识图

### 安装

1. 将 `vision-send-image/` 目录复制为 `~/.dsh/profiles/web/node_modules/@dsh-local/vision-send-image/`
2. 在 `cordis.patch.yml` 追加：

```yaml
- insert:
    - id: vision-send-image
      name: '@dsh-local/vision-send-image'
```

3. 重启 dsh，刷新浏览器页面

### 结构

| 文件 | 作用 |
|---|---|
| `package.json` | `dsh.client` 声明：client-modules 扫描到它后把 client 半注入浏览器启动图 |
| `lib/index.js` | host 半：注册 `POST /vision-upload` 路由，接收 base64 图片并保存到本地 |
| `lib/client.js` | client 半：手写 `window.__ModuleLoader__.load` bundle，注册输入框槽位 + paste 监听，通过 `fetch('/vision-upload')` 上传 |

### 原理

```
浏览器 (client.js)                          Node (index.js)
─────────────────                          ───────────────
Ctrl+V / 📷 选图                            webServer.register('/vision-upload')
   │                                            │
   └── fetch POST /vision-upload ──base64──▶   ├─ 校验格式（PNG/JPEG/WebP/GIF）
                                                ├─ 保存 ~/.dsh/vision-uploads/xxx.png
                                                └─ 返回 { ok, path }
   │
   └── setDraft("图片：C:\\...\\xxx.png") ──▶ 发送后模型调视觉 MCP 识图
```

选择 HTTP 路由而非 Typert Remote 的原因：正式 client 插件调 host 的标准途径（`ctx.remote`）依赖 Typert 构建管线生成 invocation descriptor；而 `webServer.register` + 浏览器原生 `fetch` 无需构建，client 半可以直接手写 bundle，零依赖、零构建、跨版本稳定。

## 3. 网络注意事项

本 MCP 依赖 `chat.deepseek.com`。若使用 Clash 等代理且国外节点不稳定（常见于 Cloudflare Pages/VLESS 节点），DeepSeek 请求会 403/SSL 失败。建议给 Clash 添加直连规则：

```yaml
rules:
  - DOMAIN-SUFFIX,deepseek.com,🎯 全球直连
```

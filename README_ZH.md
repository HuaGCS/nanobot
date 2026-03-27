<div align="center">
  <img src="nanobot_logo.png" alt="nanobot" width="500">
  <h1>nanobot：超轻量级个人 AI 助手</h1>
  <p>
    <a href="https://pypi.org/project/nanobot-ai/"><img src="https://img.shields.io/pypi/v/nanobot-ai" alt="PyPI"></a>
    <a href="https://pepy.tech/project/nanobot-ai"><img src="https://static.pepy.tech/badge/nanobot-ai" alt="Downloads"></a>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

`nanobot` 是一个超轻量级的个人 AI 助手框架，整体设计受 [OpenClaw](https://github.com/openclaw/openclaw) 启发，但体量更小、启动更快，也更适合本地化定制。

当前工作仓库是 `HuaGCS/nanobot`，持续同步 `HKUDS/nanobot`，并在此基础上落地了更偏陪伴型与本地化的能力，例如：

- SillyTavern 角色资产导入到 persona 工作区
- persona 级参考图与角色一致性生图
- persona 级 `VOICE.json`
- `channels.voiceReply` 下的 `openai` / `edge` / `sovits`
- `living-together` / `emotional-companion` / `translate` 内置技能
- WhatsApp 本地 bridge 代理支持

完整英文文档与更详细的发布记录见 [README.md](./README.md)。

## 目录

- [项目亮点](#项目亮点)
- [安装](#安装)
- [快速开始](#快速开始)
- [可选能力](#可选能力)
- [聊天渠道](#聊天渠道)
- [Agent 社交网络](#agent-社交网络)
- [配置说明](#配置说明)
- [多实例](#多实例)
- [CLI 参考](#cli-参考)
- [周期任务](#周期任务)
- [Docker](#docker)
- [Linux 服务](#linux-服务)
- [项目结构](#项目结构)
- [贡献与路线图](#贡献与路线图)

## 项目亮点

- 超轻量：更少的代码和更低的运行复杂度
- 易扩展：provider、tool、channel、persona、skill 结构清晰
- 多渠道：Telegram、Discord、WhatsApp、QQ、Slack、Feishu、Matrix、Email、Weixin、Wecom、Mochat
- 本地优先：支持本地 workspace、私有部署、工作区技能和本地文件交付
- 当前仓库已增强：SillyTavern 资产导入、persona 参考图、生图、语音回复、自定义声线、陪伴技能

## 安装

### 从源码安装

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
pip install -e .
```

### 使用 uv 安装

```bash
uv tool install nanobot-ai
```

### 从 PyPI 安装

```bash
pip install nanobot-ai
```

### 更新

```bash
pip install -U nanobot-ai
nanobot --version
```

如果你在使用 WhatsApp，升级后建议重建本地 bridge：

```bash
rm -rf ~/.nanobot/bridge
nanobot channels login whatsapp
```

## 快速开始

### 1. 初始化

```bash
nanobot onboard
```

如果想使用交互式向导：

```bash
nanobot onboard --wizard
```

### 2. 配置模型

默认配置文件路径：

- `~/.nanobot/config.json`

一个最小配置示例：

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "openrouter/openai/gpt-4o-mini"
    }
  }
}
```

### 3. 对话

```bash
nanobot agent
```

网关模式：

```bash
nanobot gateway
```

## 可选能力

### Web 搜索

`web_search` 支持 Brave Search 和 SearXNG。

Brave Search：

```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "apiKey": "your-brave-api-key"
      }
    }
  }
}
```

SearXNG：

```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "searxng",
        "baseUrl": "http://localhost:8080"
      }
    }
  }
}
```

### 图像生成

启用内置 `image_gen` 工具：

```json
{
  "tools": {
    "imageGen": {
      "enabled": true,
      "apiKey": "your-image-api-key",
      "baseUrl": "https://api.openai.com/v1",
      "model": "gpt-image-1"
    }
  }
}
```

说明：

- 生成结果统一写入 `<workspace>/out/image_gen`
- 若要把图片发送给用户，模型还需要调用 `message` 工具并把图片路径放进 `media`
- 当当前 persona 的 `.nanobot/st_manifest.json` 里有 `reference_image` 或 `reference_images` 时，`image_gen` 支持：
  - `reference_image="__default__"`
  - `reference_image="__default__:scene"`

这使得角色一致性出图、场景换装、生活陪伴类配图都可以复用 persona 参考图。

### 语音回复

当前仓库没有单独维护一套平行 TTS 系统，而是统一复用：

- `channels.voiceReply`

支持的 provider：

- `openai`
- `edge`
- `sovits`

OpenAI 兼容 TTS 示例：

```json
{
  "channels": {
    "voiceReply": {
      "enabled": true,
      "channels": ["telegram"],
      "provider": "openai",
      "url": "https://api.openai.com/v1",
      "model": "gpt-4o-mini-tts",
      "voice": "alloy",
      "instructions": "keep the delivery calm and clear",
      "speed": 1.0,
      "responseFormat": "opus"
    }
  }
}
```

`provider=edge` 示例：

```json
{
  "channels": {
    "voiceReply": {
      "enabled": true,
      "channels": ["telegram"],
      "provider": "edge",
      "edgeVoice": "zh-CN-XiaoxiaoNeural",
      "edgeRate": "+8%",
      "edgeVolume": "+0%"
    }
  }
}
```

`provider=sovits` 示例：

```json
{
  "channels": {
    "voiceReply": {
      "enabled": true,
      "channels": ["telegram"],
      "provider": "sovits",
      "sovitsApiUrl": "http://127.0.0.1:9880",
      "sovitsReferWavPath": "/data/voices/aria.wav",
      "sovitsPromptText": "这是角色参考语音。",
      "sovitsPromptLanguage": "zh",
      "sovitsTextLanguage": "zh"
    }
  }
}
```

补充说明：

- QQ 语音上传要求 `.silk`，因此在 QQ 场景里要用 `responseFormat: "silk"`
- `provider=edge` 不依赖 OpenAI API Key，但运行时需要本机安装 `edge-tts`
- `provider=sovits` 适合自定义声线 / GPT-SoVITS 克隆
- 语音回复会自动跟随当前 persona 的文本风格

### Persona 级 VOICE.json

默认 persona：

- `<workspace>/VOICE.json`

自定义 persona：

- `<workspace>/personas/<name>/VOICE.json`

示例：

```json
{
  "provider": "sovits",
  "apiBase": "http://127.0.0.1:9880",
  "voice": "nova",
  "instructions": "sound crisp, confident, and slightly faster than normal",
  "speed": 1.15,
  "referWavPath": "assets/voice/aria.wav",
  "promptText": "这是角色参考语音。",
  "promptLanguage": "zh",
  "textLanguage": "zh"
}
```

`VOICE.json` 同时兼容：

- `snake_case`
- `camelCase`

因此 persona 可以独立覆盖：

- provider
- endpoint
- voice
- Edge 的 voice / rate / volume
- GPT-SoVITS 的参考音频和采样参数

### 内置技能

当前仓库除默认技能外，还补了三类与本地产品化能力强相关的技能：

- `translate`
  忠实全文翻译，不用摘要代替翻译
- `living-together`
  用 persona 参考图和 `image_gen` 把“你也在场”的生活陪伴场景做出来
- `emotional-companion`
  情绪感知、记忆跟进、heartbeat 主动关怀

这些技能复用了当前仓库已有的：

- persona
- memory
- image_gen
- heartbeat

## 聊天渠道

可接入渠道概览：

| 渠道 | 你需要准备什么 |
|------|----------------|
| Telegram | `@BotFather` 生成的 Bot Token |
| Discord | Bot Token + Message Content Intent |
| WhatsApp | 扫码登录 |
| WeChat / Weixin | 扫码登录 |
| Feishu | App ID + App Secret |
| DingTalk | App Key + App Secret |
| Slack | Bot Token + App-Level Token |
| Matrix | Homeserver + Access Token |
| Email | IMAP / SMTP 账号 |
| QQ | App ID + App Secret |
| Wecom | Bot ID + Secret |
| Mochat | Claw Token |

支持多实例的渠道包括：

- `whatsapp`
- `telegram`
- `discord`
- `feishu`
- `mochat`
- `dingtalk`
- `slack`
- `email`
- `qq`
- `matrix`
- `wecom`

多实例路由形式是 `channel/name`，例如 `telegram/main`。

### Telegram

最推荐的入门渠道。

配置示例：

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

运行：

```bash
nanobot gateway
```

### Mochat

默认走 Socket.IO WebSocket，也支持 HTTP polling 回退。

最简单方式是直接让 nanobot 自己帮你接入 Mochat，英文 README 中保留了自动注册提示词。也可以手动配置：

```json
{
  "channels": {
    "mochat": {
      "enabled": true,
      "base_url": "https://mochat.io",
      "socket_url": "https://mochat.io",
      "socket_path": "/socket.io",
      "claw_token": "claw_xxx",
      "agent_user_id": "6982abcdef",
      "sessions": ["*"],
      "panels": ["*"]
    }
  }
}
```

### Discord

配置示例：

```json
{
  "channels": {
    "discord": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"],
      "groupPolicy": "mention"
    }
  }
}
```

`groupPolicy`：

- `mention`
- `open`

### Matrix

安装依赖：

```bash
pip install nanobot-ai[matrix]
```

配置示例：

```json
{
  "channels": {
    "matrix": {
      "enabled": true,
      "homeserver": "https://matrix.org",
      "userId": "@nanobot:matrix.org",
      "accessToken": "syt_xxx",
      "deviceId": "NANOBOT01",
      "e2eeEnabled": true,
      "allowFrom": ["@your_user:matrix.org"]
    }
  }
}
```

注意：

- 请保持稳定的 `deviceId`
- 多实例模式下会自动隔离到各自的 `matrix-store/<instance>`

### WhatsApp

需要：

- Node.js >= 18

登录：

```bash
nanobot channels login whatsapp
```

最小配置：

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["+1234567890"]
    }
  }
}
```

运行时通常需要两个终端：

```bash
# Terminal 1
nanobot channels login whatsapp

# Terminal 2
nanobot gateway
```

当前本地 Node.js bridge 已支持以下标准代理环境变量：

- `https_proxy`
- `http_proxy`
- `all_proxy`

也支持 `SOCKS5` URL，例如：

```bash
export https_proxy=http://127.0.0.1:7890
nanobot channels login whatsapp
```

或：

```bash
export all_proxy=socks5://127.0.0.1:1080
nanobot channels login whatsapp
```

多实例时，每个实例应有自己的：

- `bridgeUrl`
- `AUTH_DIR`
- `BRIDGE_PORT`

### Feishu

配置示例：

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "allowFrom": ["ou_YOUR_OPEN_ID"],
      "groupPolicy": "mention"
    }
  }
}
```

### QQ

当前支持：

- 私聊
- 本地图片、`.mp4`、`.silk` 语音的文件上传

配置示例：

```json
{
  "channels": {
    "qq": {
      "enabled": true,
      "appId": "YOUR_APP_ID",
      "secret": "YOUR_APP_SECRET",
      "allowFrom": ["YOUR_OPENID"],
      "mediaBaseUrl": "https://files.example.com/out/"
    }
  }
}
```

补充说明：

- 对 `workspace/out` 下的本地富媒体，QQ 会优先走 `file_data`
- 本地文件不再回退到 URL 上传
- 支持的本地富媒体：图片、`.mp4`、`.silk`

### DingTalk

配置示例：

```json
{
  "channels": {
    "dingtalk": {
      "enabled": true,
      "clientId": "YOUR_APP_KEY",
      "clientSecret": "YOUR_APP_SECRET",
      "allowFrom": ["YOUR_STAFF_ID"]
    }
  }
}
```

### Slack

配置示例：

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "allowFrom": ["YOUR_SLACK_USER_ID"],
      "groupPolicy": "mention"
    }
  }
}
```

### Email

配置示例：

```json
{
  "channels": {
    "email": {
      "enabled": true,
      "consentGranted": true,
      "imapHost": "imap.gmail.com",
      "imapPort": 993,
      "imapUsername": "my-nanobot@gmail.com",
      "imapPassword": "your-app-password",
      "smtpHost": "smtp.gmail.com",
      "smtpPort": 587,
      "smtpUsername": "my-nanobot@gmail.com",
      "smtpPassword": "your-app-password",
      "fromAddress": "my-nanobot@gmail.com",
      "allowFrom": ["your-real-email@gmail.com"]
    }
  }
}
```

### Weixin

从源码安装 Weixin 依赖：

```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
pip install -e ".[weixin]"
```

配置示例：

```json
{
  "channels": {
    "weixin": {
      "enabled": true,
      "allowFrom": ["YOUR_WECHAT_USER_ID"]
    }
  }
}
```

登录：

```bash
nanobot channels login weixin
```

### Wecom

安装可选依赖：

```bash
pip install nanobot-ai[wecom]
```

配置示例：

```json
{
  "channels": {
    "wecom": {
      "enabled": true,
      "botId": "your_bot_id",
      "secret": "your_bot_secret",
      "allowFrom": ["your_id"]
    }
  }
}
```

## Agent 社交网络

nanobot 可以接入 agent 社交网络。目前 README.md 中保留了例如：

- Moltbook
- ClawdChat

使用方式通常是把平台提供的 skill 地址作为消息发给 nanobot，让它自己读取并完成接入。

## 配置说明

默认配置文件：

- `~/.nanobot/config.json`

### Provider

支持的 provider 包括但不限于：

| Provider | 用途 |
|----------|------|
| `custom` | 任意 OpenAI 兼容接口 |
| `openrouter` | 推荐的聚合网关 |
| `openai` | GPT 官方接口 |
| `anthropic` | Claude 官方接口 |
| `azure_openai` | Azure OpenAI |
| `deepseek` | DeepSeek |
| `groq` | LLM + Whisper 语音转写 |
| `gemini` | Gemini |
| `dashscope` | 通义千问 |
| `moonshot` | Moonshot / Kimi |
| `zhipu` | GLM |
| `minimax` | MiniMax |
| `ollama` | 本地 Ollama |
| `ovms` | OpenVINO Model Server |
| `vllm` | 本地 vLLM 或任意兼容 OpenAI 的本地服务 |
| `openai_codex` | OAuth 登录的 Codex |
| `github_copilot` | OAuth 登录的 GitHub Copilot |

### OpenAI Codex OAuth

```bash
nanobot provider login openai-codex
```

配置模型：

```json
{
  "agents": {
    "defaults": {
      "model": "openai-codex/gpt-5.1-codex"
    }
  }
}
```

### GitHub Copilot OAuth

```bash
nanobot provider login github-copilot
```

### Custom Provider

```json
{
  "providers": {
    "custom": {
      "apiKey": "your-api-key",
      "apiBase": "https://api.your-provider.com/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "your-model-name"
    }
  }
}
```

### Ollama

```bash
ollama run llama3.2
```

```json
{
  "providers": {
    "ollama": {
      "apiBase": "http://localhost:11434"
    }
  },
  "agents": {
    "defaults": {
      "provider": "ollama",
      "model": "llama3.2"
    }
  }
}
```

### OVMS

适用于 Intel GPU 的 OpenVINO Model Server，本质上走 OpenAI 兼容接口。

```json
{
  "providers": {
    "ovms": {
      "apiBase": "http://localhost:8000/v3"
    }
  },
  "agents": {
    "defaults": {
      "provider": "ovms",
      "model": "openai/gpt-oss-20b"
    }
  }
}
```

### vLLM

```json
{
  "providers": {
    "vllm": {
      "apiKey": "dummy",
      "apiBase": "http://localhost:8000/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

### Channel 通用设置

```json
{
  "channels": {
    "sendProgress": true,
    "sendToolHints": false,
    "sendMaxRetries": 3
  }
}
```

说明：

- `sendProgress`
  是否把 agent 的文字进度流式发到渠道
- `sendToolHints`
  是否把工具调用提示发到渠道
- `sendMaxRetries`
  出站消息失败时的最大重试次数

### MCP

nanobot 支持 [MCP](https://modelcontextprotocol.io/)。

配置示例：

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
      },
      "my-remote-mcp": {
        "url": "https://example.com/mcp/",
        "headers": {
          "Authorization": "Bearer xxxxx"
        }
      }
    }
  }
}
```

支持两类传输：

- `command` + `args`
  本地 stdio
- `url` + `headers`
  远程 HTTP

### 安全

生产环境建议：

- `tools.restrictToWorkspace: true`

关键项：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `tools.restrictToWorkspace` | `false` | 把 shell、读写文件、列目录等工具限制在 workspace 内 |
| `tools.exec.enable` | `true` | 关闭后不注册 shell 工具 |
| `tools.exec.pathAppend` | `""` | 给 shell 额外追加 PATH |
| `tools.imageGen.enabled` | `false` | 开启内置 `image_gen` |
| `channels.*.allowFrom` | `[]` | 白名单，空数组默认拒绝所有 |

### 时区

默认使用 `UTC`。如果希望模型按本地时间理解运行时上下文：

```json
{
  "agents": {
    "defaults": {
      "timezone": "Asia/Shanghai"
    }
  }
}
```

## 多实例

可通过不同的 `--config` 和 `--workspace` 同时运行多个 nanobot 实例。

初始化多个实例：

```bash
nanobot onboard --config ~/.nanobot-telegram/config.json --workspace ~/.nanobot-telegram/workspace
nanobot onboard --config ~/.nanobot-discord/config.json --workspace ~/.nanobot-discord/workspace
nanobot onboard --config ~/.nanobot-feishu/config.json --workspace ~/.nanobot-feishu/workspace
```

启动：

```bash
nanobot gateway --config ~/.nanobot-telegram/config.json
nanobot gateway --config ~/.nanobot-discord/config.json
nanobot gateway --config ~/.nanobot-feishu/config.json --port 18792
```

路径解析规则：

| 组件 | 来源 |
|------|------|
| Config | `--config` |
| Workspace | `--workspace` 或配置文件 |
| Cron Jobs | config 所在目录 |
| 媒体 / 运行时状态 | config 所在目录 |

适用场景：

- 不同渠道独立运行
- 测试 / 生产隔离
- 不同团队用不同模型或 provider
- 多租户隔离

## CLI 参考

| 命令 | 说明 |
|------|------|
| `nanobot onboard` | 初始化默认配置和工作区 |
| `nanobot onboard --wizard` | 使用交互式向导初始化 |
| `nanobot agent` | 交互式 CLI 对话 |
| `nanobot agent -m "..."` | 单轮消息 |
| `nanobot agent -w <workspace>` | 指定工作区启动 |
| `nanobot agent -w <workspace> -c <config>` | 指定工作区和配置启动 |
| `nanobot gateway` | 启动网关 |
| `nanobot status` | 查看状态 |
| `nanobot channels login <channel>` | 交互式登录某个渠道 |
| `nanobot channels status` | 查看渠道状态 |
| `nanobot persona import-st-card <file>` | 导入 SillyTavern 角色卡 |
| `nanobot persona import-st-preset <file> --persona <name>` | 导入 preset 到 persona |
| `nanobot persona import-st-worldinfo <file> --persona <name>` | 导入 world info 到 persona |
| `nanobot provider login openai-codex` | Codex OAuth 登录 |
| `nanobot provider login github-copilot` | GitHub Copilot OAuth 登录 |

### Persona 资产

当前仓库支持把 SillyTavern 资产导入到 `<workspace>/personas/<name>/`，而不是使用全局 `~/.nanobot/sillytavern`。

导入角色卡：

```bash
nanobot persona import-st-card /path/to/aria.json -w ~/.nanobot/workspace
```

导入 preset：

```bash
nanobot persona import-st-preset /path/to/preset.json --persona Aria -w ~/.nanobot/workspace
```

导入 world info：

```bash
nanobot persona import-st-worldinfo /path/to/worldinfo.json --persona Aria -w ~/.nanobot/workspace
```

生成的典型目录结构：

```text
personas/Aria/
  SOUL.md
  USER.md
  STYLE.md
  LORE.md
  memory/
  .nanobot/
```

manifest 中可声明：

- `response_filter_tags`
- `reference_image`
- `reference_images`

### 聊天内斜杠命令

| 命令 | 说明 |
|------|------|
| `/new` | 开新会话 |
| `/lang current` | 查看当前命令语言 |
| `/lang list` | 查看可用语言 |
| `/lang set <en\|zh>` | 切换命令语言 |
| `/persona current` | 查看当前 persona |
| `/persona list` | 列出 persona |
| `/persona set <name>` | 切换 persona |
| `/skill search <query>` | 搜索公共技能 |
| `/skill install <slug>` | 安装 workspace 技能 |
| `/skill uninstall <slug>` | 卸载 workspace 技能 |
| `/skill list` | 查看技能 |
| `/skill update` | 更新技能 |
| `/mcp [list]` | 查看 MCP 服务和工具 |
| `/stop` | 停止当前任务 |
| `/restart` | 重启进程 |
| `/status` | 查看运行状态 |
| `/help` | 查看帮助 |

## 周期任务

`HEARTBEAT.md` 用来描述周期性任务。agent 也可以自己维护它，例如让它“添加一个周期任务”，它会直接更新 `HEARTBEAT.md`。

前提：

- `nanobot gateway` 正在运行
- 你至少和 bot 对话过一次，系统知道要把结果发往哪个渠道

## Docker

仓库已经提供：

- `Dockerfile`
- `docker-compose.yml`

`docker-compose` 快速开始：

```bash
docker compose run --rm nanobot-cli onboard
vim ~/.nanobot/config.json
docker compose up -d nanobot-gateway
```

常用命令：

```bash
docker compose run --rm nanobot-cli agent -m "Hello!"
docker compose logs -f nanobot-gateway
docker compose down
```

直接使用 `docker`：

```bash
docker build -t nanobot .
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot onboard
docker run -v ~/.nanobot:/root/.nanobot -p 18790:18790 nanobot gateway
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot agent -m "Hello!"
```

补充说明：

- `-v ~/.nanobot:/root/.nanobot` 用于把宿主机配置和工作区挂进容器
- 如果要跑 WhatsApp，多实例通常还需要多个 bridge 进程
- 如果走代理，记得把代理环境变量传进容器或 bridge 进程

## Linux 服务

可以把网关作为 systemd 用户服务启动。

先找可执行文件：

```bash
which nanobot
```

创建 `~/.config/systemd/user/nanobot-gateway.service`：

```ini
[Unit]
Description=Nanobot Gateway
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/nanobot gateway
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
```

启用：

```bash
systemctl --user daemon-reload
systemctl --user enable --now nanobot-gateway
```

常用操作：

```bash
systemctl --user status nanobot-gateway
systemctl --user restart nanobot-gateway
journalctl --user -u nanobot-gateway -f
```

如果希望退出登录后服务仍然运行：

```bash
loginctl enable-linger $USER
```

## 项目结构

```text
nanobot/
├── agent/          核心 agent 逻辑
│   ├── loop.py
│   ├── context.py
│   ├── memory.py
│   ├── skills.py
│   ├── subagent.py
│   └── tools/
├── skills/         内置技能
├── channels/       聊天渠道适配
├── bus/            消息路由
├── cron/           定时任务
├── heartbeat/      主动唤醒
├── providers/      模型与语音 provider
├── session/        会话管理
├── config/         配置模型与解析
└── cli/            CLI 命令
bridge/             WhatsApp Node.js bridge
tests/              测试
```

## 贡献与路线图

欢迎提 PR。这个项目的一个重要特点就是代码量小、结构清晰、方便继续演进。

分支策略：

| 分支 | 用途 |
|------|------|
| `main` | 稳定版本，修复 bug 与小幅增强 |
| `nightly` | 实验性功能与潜在破坏性改动 |

路线图方向：

- 多模态能力继续增强
- 长期记忆持续优化
- 多步推理与反思能力
- 更多外部集成
- 自我改进与反馈闭环

## 中文文档说明

这份 `README_ZH.md` 是面向当前工作仓库的完整中文整理版，重点保证这些内容是准确同步的：

- persona / SillyTavern 资产导入
- persona 参考图与 `image_gen`
- `channels.voiceReply` 的 `openai` / `edge` / `sovits`
- `VOICE.json` 自定义声线
- 陪伴技能与翻译技能
- WhatsApp 本地 bridge 代理支持

如果你需要逐段对照的原始英文说明、完整细节或最新补充，请直接查看：

- [README.md](./README.md)

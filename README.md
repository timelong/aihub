# AI Hub · 本地多模型工作台

一个轻量级、可本地部署的大模型工作台。**统一配置任意厂商的模型**，在同一个界面里完成
**多轮问答（流式）**、**图片生成**、**视频生成**。提供 **Web 界面** 和 **终端 CLI** 两种用法。

```
┌──────────┐    ┌─────────────────┐    ┌──────────────────────────────┐
│  Web UI  │──▶ │  FastAPI 后端   │──▶ │ Provider 适配层              │
│  CLI     │    │  SSE / 任务轮询 │    │ openai / gemini / dashscope  │
└──────────┘    │  SQLite 持久化  │    │ ark(豆包) / zhipu(智谱)      │
                └─────────────────┘    └──────────────────────────────┘
```

---

## 一、快速开始

```bash
cd aihub
cp .env.example .env      # 填入你有的 API Key（不需要全填）
./run.sh                  # 自动建 venv、装依赖、启动
```

打开 <http://127.0.0.1:8000>

手动方式：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

终端版：

```bash
python cli.py                       # 交互式对话
python cli.py --list                # 列出全部可用模型
python cli.py -m deepseek/deepseek-chat -p "用一句话解释量子纠缠"
python cli.py --image "赛博朋克风格的猫，霓虹灯"
python cli.py --video "海浪拍打礁石，慢镜头"
```

---

## 二、支持的服务商

| 服务商 | kind | 对话 | 图片 | 视频 | 需要的环境变量 |
|---|---|---|---|---|---|
| OpenAI | `openai` | ✅ GPT-4o | ✅ GPT-Image / DALL·E 3 | — | `OPENAI_API_KEY` |
| DeepSeek | `openai` | ✅ V3 / R1（含思维链） | — | — | `DEEPSEEK_API_KEY` |
| 硅基流动 | `openai` | ✅ | ✅ Kolors | — | `SILICONFLOW_API_KEY` |
| Ollama / vLLM 本地 | `openai` | ✅ | — | — | 无需 Key |
| 魔搭社区 ModelScope | `modelscope` | ✅ Qwen3 / DeepSeek / GLM | ✅ Qwen-Image / FLUX | — | `MODELSCOPE_API_KEY` |
| Google Gemini | `gemini` | ✅ 2.5 Flash/Pro | ✅ Imagen 4 | ✅ Veo 3 | `GEMINI_API_KEY` |
| 阿里百炼（通义/万相） | `dashscope` | ✅ 千问 / 千问VL | ✅ 万相 2.2 | ✅ 万相文生/图生视频 | `DASHSCOPE_API_KEY` |
| 火山方舟（豆包/即梦） | `ark` | ✅ 豆包 Seed | ✅ Seedream 4.0 | ✅ Seedance | `ARK_API_KEY` |
| 智谱 GLM | `zhipu` | ✅ GLM-4.6 / 4V | ✅ CogView-4 | ✅ CogVideoX-3 | `ZHIPU_API_KEY` |

> `kind: openai` 是**通用适配器**——任何 OpenAI 兼容的接口（Kimi、MiniMax、OpenRouter、
> 自建 vLLM…）只要在 `config.yaml` 里加一段 base_url + api_key 就能接入，无需改代码。

---

## 三、配置模型

全部配置集中在 `config.yaml`，也可以在 Web 界面「⚙️ 模型配置」页直接编辑并热重载。

```yaml
providers:
  - id: mycompany            # 唯一标识
    name: 公司内部模型
    kind: openai             # openai | gemini | dashscope | ark | zhipu
    base_url: http://10.0.0.9:8000/v1
    api_key: ${MY_API_KEY}   # 支持 ${环境变量} 插值，从 .env 读取
    models:
      chat:
        - id: qwen3-32b
          name: Qwen3 32B
          vision: true       # 标记支持图片输入
      image: []
      video: []

defaults:                    # 各页面默认选中的模型
  chat: mycompany/qwen3-32b
```

模型在系统中的唯一标识是 `provider_id/model_id`，例如 `deepseek/deepseek-chat`。

---

## 四、功能说明

**💬 对话**
- SSE 流式输出，打字机效果，可中途停止
- 多轮上下文，会话自动持久化到 SQLite，左侧可切换/删除
- 多模态：点 🖼 上传图片提问（模型需标记 `vision: true`）
- 推理模型（DeepSeek R1、Gemini thinking）的思维链单独灰色展示
- 参数面板：System Prompt、Temperature、Top P、Max Tokens

**🎨 图片生成**
- 提示词 / 负向提示词 / 尺寸 / 数量 / 种子
- 结果自动下载到本地（第三方图床链接通常 24h 过期，本地留存不怕丢）
- **保存目录可配置**：见下方「保存目录」一节

**📁 保存目录（可配置）**
- 图片与视频分别配置保存目录，在「⚙️ 模型配置 → 📁 保存目录」页设置
- 点「浏览…」打开目录选择器：可逐级进入子目录、跳转到工程/主目录、直接新建文件夹，
  并实时显示该目录是否可写
- 支持绝对路径（`/Users/me/Pictures/AI`）、`~/` 开头、或相对工程根目录的相对路径
- 也可以直接写在 `config.yaml` 的 `storage` 段里

```yaml
storage:
  image_dir: ~/Pictures/AI      # 图片保存目录
  video_dir: /data/ai/videos    # 视频保存目录
```

**🎬 视频生成**
- 文生视频 + 图生视频（上传首帧）
- 时长 / 比例 / 分辨率
- 异步任务：提交后后台轮询，界面每 5 秒自动刷新状态

---

## 五、目录结构

```
aihub/
├── app/
│   ├── main.py              # FastAPI 路由：chat(SSE) / image / video / jobs / config
│   ├── config.py            # config.yaml + .env 加载与 ${VAR} 插值
│   ├── storage.py           # SQLite：会话、消息、生成任务
│   └── providers/
│       ├── base.py          # 抽象基类 chat_stream / generate_image / submit_video / poll_video
│       ├── openai_compat.py # OpenAI 兼容通用适配器
│       ├── gemini.py        # Gemini / Imagen / Veo
│       ├── dashscope.py     # 通义千问 / 万相
│       ├── ark.py           # 豆包 / 即梦
│       └── zhipu.py         # GLM / CogView / CogVideoX
├── web/index.html           # 单文件前端，零构建
├── cli.py                   # 终端客户端
├── config.yaml              # 模型配置
├── data/                    # SQLite + 生成的图片视频（自动创建）
└── run.sh
```

---

## 六、扩展一个新厂商

1. 若对方是 OpenAI 兼容接口 → 只改 `config.yaml`，加一段 provider 即可，**不用写代码**。
2. 若是私有协议 → 在 `app/providers/` 下新建文件，继承 `BaseProvider`，实现需要的方法：

```python
class MyProvider(BaseProvider):
    kind = "mine"
    async def chat_stream(self, model, messages, params):
        ...  # yield {"type":"delta","text":"..."}
    async def generate_image(self, model, prompt, params) -> list[str]: ...
    async def submit_video(self, model, prompt, params) -> str: ...
    async def poll_video(self, model, remote_id) -> dict: ...
```

3. 在 `app/providers/__init__.py` 的 `REGISTRY` 里注册 `"mine": MyProvider`。

---

## 七、API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/models` | 所有服务商 / 模型 / Key 配置状态 |
| GET·PUT | `/api/config` | 读取 / 保存 config.yaml（热重载） |
| GET·PUT | `/api/storage` | 读取 / 设置图片与视频保存目录 |
| GET | `/api/fs?path=` | 浏览服务器目录（目录选择器用） |
| POST | `/api/fs/mkdir` | 新建文件夹 |
| POST | `/api/chat` | 流式对话（SSE） |
| POST | `/api/image` | 同步出图 |
| POST | `/api/video` | 提交视频任务 |
| GET | `/api/jobs?kind=` | 任务列表 |
| GET | `/api/jobs/{id}` | 单个任务状态 |
| GET·POST | `/api/conversations` | 会话列表 / 新建 |
| GET·DELETE | `/api/conversations/{id}` | 会话详情 / 删除 |
| GET | `/media/{kind}/{name}` | 本地媒体文件（kind = image / video） |

---

## 八、注意事项

- 默认只监听 `127.0.0.1`。若要局域网访问，设 `HOST=0.0.0.0`——但此服务**没有鉴权**，
  请勿直接暴露到公网。
- `.env` 已在 `.gitignore` 中，切勿把 Key 提交到仓库。
- 视频生成计费较高，建议先用短时长/低分辨率测试。
- 保存目录若填了不可写的路径，会在保存时报错并回退到默认目录。
- 各厂商接口偶有调整，若某个模型报错，优先核对 `config.yaml` 里的 model id 与官方文档是否一致。

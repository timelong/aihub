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

**环境要求：Python 3.9+**（macOS 系统自带的 `python3` 就是 3.9.6，可直接用；
Linux/Windows 用 3.10+ 更省心）。低于 3.9 启动时会直接给出提示，不会报一堆看不懂的错。

> 代码里普通函数用了 `X | None` 这种 3.10 写法，但因为每个模块都有
> `from __future__ import annotations`，注解不会在运行时求值，所以 3.9 能跑。
> **例外**：pydantic 模型字段和 FastAPI 路由参数的注解**会**被运行时求值，
> 这两处必须写 `Optional[X]`——改这两处代码时注意别退回 `X | None`，
> 否则 3.9 会报 `TypeError: unsupported operand type(s) for |`。

```bash
cd aihub
cp .env.example .env      # 填入你有的 API Key（不需要全填）
./run.sh                  # 自动建 venv、装依赖、启动
```

> 附件解析用到 `pypdf` / `python-docx` / `python-pptx` / `openpyxl`，已经写进
> requirements.txt。**从旧版本升上来的记得重装依赖**（`rm .venv/.deps_ok && ./run.sh`，
> 或直接 `pip install -r requirements.txt`），否则传 PDF/PPT 时会提示缺依赖。

服务起来后会自动打开浏览器（不想自动打开就用 `NO_OPEN=1 ./run.sh`），
也可以手动访问 <http://127.0.0.1:8000>

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
- SSE 流式输出，打字机效果；**点「停止」会把已经生成的部分存下来**（标注「已手动停止」），
  不会白跑一轮 token
- 多轮上下文，会话自动持久化到 SQLite，左侧可切换/重命名（✎）/删除，并支持**按标题和正文搜索**
- 多模态：点 🖼 上传图片提问（模型需标记 `vision: true`）。
  **给纯文本模型发图会被拦住**：那类模型会静默丢掉图片、然后回你"我没看到图片"，
  所以选图时就会提醒，发送前会弹出可用的视觉模型让你换（同一家优先、本地服务垫底），
  换完自动重发；日志里也会留一条 WARNING
- 🖼 和 📎 的区别：图片是**模型自己看**（要视觉模型），文档是**服务端先抽成文字**再发（任何模型都行）。
  扫描件 PDF、纯图 PPT 属于前者，用 🖼 加视觉模型才有效
- 推理模型（DeepSeek R1、Gemini thinking）的思维链单独灰色展示
- 参数面板：提示词预设、System Prompt、Temperature、Top P、Max Tokens、注入当前时间
- **工具（联网搜索 / 网页抓取）**：见下方「工具」一节

**✏️ 消息操作**（鼠标移到消息上才出现，不占视线）

| 操作 | 用户消息 | 助手消息 | 说明 |
|---|---|---|---|
| 复制 | ✅ | ✅ | 复制的是原始 markdown，不是渲染后的文本 |
| 编辑重发 | ✅ | — | 改完内容重跑这一轮 |
| 重新生成 | — | ✅ | 找到对应的用户消息，按原内容重跑 |
| 删除 | ✅ | ✅ | 只删这一条 |

> 「编辑重发」和「重新生成」都会先把那条用户消息**及其之后的所有消息**删掉再重跑——
> 否则历史里留着上一轮的答案，模型会被自己的旧回答带偏。

**📝 Markdown 渲染**
- 标题、列表、表格、引用、分割线、链接、图片、粗斜体删除线、行内码
- 代码块带语言标签、**一键复制**和语法高亮（自带的极简高亮，不依赖外部资源）
- LaTeX（`$$…$$`）和 mermaid 代码块体量太大，不打包进来，**用到时才从 CDN 懒加载**；
  没网就原样显示文本并提示一次，不影响其它内容
- 渲染器是手写的（约 90 行），因为这是个无构建的单文件前端，
  marked + highlight.js + katex 全量 vendor 进来有好几 MB

**⭐ 提示词预设**
- 参数面板顶部：选预设 → 「应用」写入 System Prompt（连带存的 temperature 一起恢复）
- 「另存为…」把当前提示词存成预设，**同名直接覆盖**；「删除」移除预设
- 存在 SQLite 的 `presets` 表里，和会话一样跟着 `data/aihub.db` 走

**📎 附件（PDF / Word / PPT / Excel / 文本）**
- composer 上的 📎 选文件，服务端**本地解析成文本**随消息发给模型
  （各家 chat 接口只收文本和图片，没有"传文件"的通道，所以必须先在本地抽）
- 支持：PDF、Word(.docx)、PPT(.pptx)、Excel(.xlsx/.xlsm)，以及
  txt / md / csv / json / yaml / 各种代码文件。老格式 .doc/.ppt/.xls 请先另存为新格式
- 抽取质量：PPT 按页给出标题、正文、表格和**演讲备注**；Word 保留标题层级和表格；
  Excel 按工作表逐行；PDF 按页。中文文本自动试 utf-8 / gb18030 / big5 解码
- **抽不出来会明说**：扫描件 PDF、纯图 PPT 会直接告诉你「这是图片型文件，建议换看图模型或先 OCR」，
  而不是悄悄发一段空内容给模型
- 附件卡片显示文件名、字数和页数等信息；超长会截断并标 ⚠，上限可配：

```yaml
attachments:
  max_chars: 60000      # 单个附件最多取多少字
  total_chars: 150000   # 一次请求所有附件合计
  max_mb: 30            # 单文件大小上限
```

- 附件正文会随消息存库，所以**第二轮追问「刚才那份 PPT 里…」模型还看得到**；
  回传给前端的会话详情里剥掉正文，只留文件名和字数
- 单个文件解析失败不影响同批其它文件，错误逐个提示

**🔁 自动重试 / 换模型**
- 撞到限流（429）或网关抖动（5xx）时自动退避重试，等待时间翻倍递增；
  服务端给了 `Retry-After` 就按它说的等。对话、出图、视频提交、轮询全都覆盖
  （实现在 transport 层，见 [`app/providers/retry.py`](app/providers/retry.py)）
- **不会重复扣费**：429/5xx 说明这次请求肯定没被处理，重试是安全的；
  而「连不上 / 读超时」这种不知道对方收没收到的错误，只对 GET（轮询）重试，
  绝不重发出图/视频的提交请求
- 重试到底还是失败时，界面右下角会弹一个面板：**要不要换个模型重试**。
  备选列表是「同能力 + 别的服务商 + 已配 key」，按和当前模型名字的相似度排序，
  点一下就用新模型重跑（对话会自动重发那一轮，出图会重新提交）
- 日志里能看到每次重试：`[flaky] POST /v1/chat/completions 返回 429（限流），1.0s 后重试（第 2/3 次）`

```yaml
retry:
  attempts: 3                    # 总共尝试几次，1 = 关掉重试
  backoff: 2.0                   # 首次等待秒数，之后翻倍
  max_backoff: 30.0              # 单次等待上限
  statuses: [429, 500, 502, 503, 504]
```

> 某家服务商想单独设置，就在它自己的条目下写同名的 `retry` 段覆盖。

**🕒 当前时间注入**
- 模型只知道训练数据里的日期，问「今天几号」「最近」会答错，所以每次请求都会在
  system prompt 前自动加一段当前时间说明（日期、时刻、星期、时区），并要求它以此为基准
- Web 和 `cli.py` 都生效（注入点在 provider 层）；参数面板可临时关掉，也能看到会告诉模型的时间
- 时区取 `config.yaml` 的 `chat.timezone`，留空则用服务器系统时区：

```yaml
chat:
  inject_datetime: true       # 关掉写 false
  timezone: Asia/Shanghai     # 留空用系统时区
  extra: ""                   # 想给所有对话都加的固定说明
```

**🔧 工具**
- 顶栏「工具」处点亮即可启用，选择记在浏览器本地，下次进来还在
- `联网搜索`：模型自己决定要不要搜、搜什么词，服务端执行后把结果回灌给模型
- `网页抓取`：按 URL 打开网页取正文，一般配合搜索结果使用
- 走标准 OpenAI function calling，任何支持 tools 的模型都能用；最多连续 5 轮工具调用。
  Gemini 例外：直接启用它自带的 Google 搜索接地
- 搜索后端按 key 自动选择，都没配就兜底抓 Bing 网页（可能被限流）：

```dotenv
TAVILY_API_KEY=      # 首选，专为 LLM 设计
BOCHA_API_KEY=       # 博查，国内直连
```

- 界面上会实时显示「调用了什么工具、搜到几条」，这条轨迹也会随会话存库

**🎨 图片生成**
- **异步任务**：提交后立刻返回，生成在后台跑，前端每 2 秒轮询一次进度。
  关掉页面、切走、网络抖动都不会把任务弄丢，回来看列表就行
  （脚本里想要一次调用拿结果，加 `?wait=true` 走同步模式）
- 提示词 / 负向提示词 / 尺寸 / 数量 / 种子
- **尺寸按模型走**：各家推荐分辨率差别很大（Qwen-Image 是 1328 系列、即梦用 2K/4K、
  DALL·E 3 只认三种），所以在 `config.yaml` 给出图模型写 `sizes`，界面「尺寸」下拉只列
  这些并自动标出比例，第一个为默认值；没配 `sizes` 的模型用通用列表。
  请求的尺寸不在列表里时不拦截，但会在日志里留一条 WARNING。

```yaml
      image:
        - id: Qwen/Qwen-Image-2512
          name: Qwen-Image 文生图
          sizes: [1328x1328, 1664x928, 928x1664, 1472x1104, 1104x1472, 1584x1056, 1056x1584]
```

- **图生图 / 图片编辑**：上传参考图（可多张）即从文生图切到图生图。
  各家接口吃的格式不同，已分别适配：OpenAI 兼容接口走 `/images/edits` 上传文件、
  火山方舟即梦与 Gemini 直接吃 base64、魔搭只认公网 URL（自动走下方 COS 临时图床）。
  阿里百炼/智谱当前配置的是纯文生图模型，传参考图会给出明确提示。
  在 `config.yaml` 给模型加 `image_input: true` 后，界面会提示该模型支持图生图；
  个别兼容服务（如硅基流动）的图生图是在 `/images/generations` 传 `image` 字段，
  给该 provider 加 `image_edit_mode: json` 即可
- **多参考图**：能吃多张的服务商会把所有图都传上去（即梦组图、Gemini、OpenAI
  `image[]`、魔搭 `image_url` 数组）。张数上限写在模型的 `max_images`，
  界面会在上传时就拦住并提示，服务端再兜底裁剪一次——不会出现多选了却被
  静默丢弃的情况。魔搭实测上限 4 张（超了接口直接返回
  `image_url count N exceeds maximum limit of 4`）
- 结果自动下载到本地（第三方图床链接通常 24h 过期，本地留存不怕丢）
- **生成完有明确反馈**：绿色提示「✅ 生成成功：N 张，用时 Ns，已保存到 …」；
  结果卡片显示缩略图（点开原图）、状态与用时、模型与尺寸、生成时间、
  **本地绝对路径 + 一键复制**；失败则显示完整错误原文
- 生成按钮上有已等待秒数；服务被重启后遗留的「生成中」任务会在下次启动时标为失败，
  不会一直转圈
- **列表可清理**：卡片右上角 ✕ 移除单条，或点「清空列表」。规则是**磁盘文件一律不删**：
  - 有结果的（生成成功）→ 只从页面隐藏，记录和图片都保留，勾选「显示已隐藏」可找回并 ↩ 恢复
  - 没有结果的（失败、卡住）→ 记录真正删除
- **保存目录可配置**：见下方「保存目录」一节

**☁️ 腾讯云 COS（临时图床）**
- 参考图怎么送给服务商由 provider 的 `ref_mode` 决定：`base64` 直传、`url` 走 COS，
  不写则自动判断（吃 base64 的直传；只认 URL 的走 COS，而 COS 关掉时**魔搭会自动
  降级成 base64 直传**——实测魔搭 `image_url` 也收 data URL）。
  想彻底不用 COS：`cos: enabled: false`，或给 modelscope 段写 `ref_mode: base64`
- 阿里百炼图生视频、智谱 CogVideoX 这些接口**只认公网图片 URL**，不吃 base64。
  本地上传的参考图会：**上传对象 → 取预签名 URL 交给服务商 → 任务结束立即删除对象**
- 删除时机：出图是请求结束就删；视频是异步任务，等后台轮询出结果（成功/失败/超时）才删，
  否则上游还没来取图就没了
- 预签名有效期 `expire_seconds` 要覆盖生成耗时，默认 1800 秒（和视频轮询上限一致）
- 对象 key = **前缀 / 时间戳 / 随机文件名**，例如
  `aihub/tmp/20260813200539/0693553f….png`；`timestamp_format` 可改成
  `%Y/%m/%d/%H%M%S` 按日期分层，或写 `epoch` 用 Unix 秒
- 「⚙️ 模型配置 → ☁️ 腾讯云 COS」页显示配置状态，**「测试连接」**会真跑一遍
  上传 → 预签名 → 下载校验 → 删除，一次确认 4 个参数对不对
- 密钥放 `.env`，其余放 `config.yaml`：

```dotenv
COS_SECRET_ID=
COS_SECRET_KEY=
```

```yaml
cos:
  secret_id: ${COS_SECRET_ID}
  secret_key: ${COS_SECRET_KEY}
  region: ap-guangzhou          # 存储桶地域
  bucket: my-bucket-1250000000  # 必须带 APPID
  prefix: aihub/tmp/            # key 前缀；最终 key = 前缀/时间戳/文件名
  timestamp_format: "%Y%m%d%H%M%S"  # 时间戳目录格式；写 epoch 则用 Unix 秒
  expire_seconds: 1800          # 预签名有效期(秒)
  scheme: https
  # enabled: false              # 显式关闭；不写则「配全了就自动启用」
```

- 依赖 `cos-python-sdk-v5`（已在 requirements.txt）。没装或没配全时，
  只影响需要公网 URL 的那几个服务商，其它照常用

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
- 提交后前端会自动轮询，完成/失败都有提示（失败且是限流时同样可以换模型重试）
- 文生视频 + 图生视频（上传首帧）
- 时长 / 比例 / 分辨率
- 异步任务：提交后后台轮询，界面每 5 秒自动刷新状态

**📜 运行日志 / 🗂 日志管理**
- 控制台 + 文件双写，**按天滚动**：当天写 `data/logs/aihub.log`，
  跨天自动改名成 `aihub-2026-08-12.log`
- 记录内容：每个接口的方法/路径/状态码/耗时、上游 API 的请求与响应（含状态码和耗时）、
  对话的模型与工具调用轨迹、出图/视频任务的开始与结果、异常堆栈
- **每行自动带上下文** `{job=... model=...}` / `{conv=... model=...}`：
  provider 层那些 `GET /v1/tasks/xxx` 也能看出是哪个任务、哪个模型在跑
- **轮询汇总成一行**：`轮询 #12 状态=RUNNING 已等 36s task=xxx`，
  原始的 →/← 请求行降到 DEBUG，不再刷屏
- 出图/出视频成功时记录**绝对路径和文件大小**，可以直接去文件夹里找
- 密钥会被自动打码，base64 图片会被压成 `<data:image/png base64 12345B>`，不会把日志刷爆
- 左侧「🗂 日志管理」页可以：
  - 设置**历史日志保留天数**（写进 `config.yaml` 的 `logging.retention_days`，
    0 = 永久保留），服务启动时与改配置时都会清理超期文件，也可点「立即清理过期日志」
  - 按文件查看（当天 / 各历史日期），单独删除某个历史文件（当天的正在写，不允许删）
  - **搜索**：关键词（空格分隔=都要包含，不区分大小写，命中处高亮）+ 级别过滤
    （INFO/WARNING/ERROR 及以上）+ 显示行数，右上角可开自动刷新
- 环境变量：

```yaml
logging:
  retention_days: 7     # 历史日志保留天数，0 = 永久保留
```

```dotenv
AIHUB_LOG_DIR=          # 日志目录，默认 data/logs
AIHUB_LOG_LEVEL=INFO    # 设 DEBUG 会额外记录请求体/响应体预览
AIHUB_LOG_BODY=800      # 请求/响应预览截断长度
```

- 每行都带**时区偏移和进程号**：`2026-08-13 10:25:48+0800 INFO [aihub:57181] ...`。
  多个实例同时在跑会写进同一个文件，靠 pid 就能把两条时间线分开

**🛑 服务突然停止怎么判断**
- 日志出现 `服务开始关闭 PID=xxx` + `Application shutdown complete`，且没有 traceback
  → 是**收到外部信号**（Ctrl-C / `kill` / `pkill` / 关掉终端），不是程序崩溃
- run.sh 会在退出时区分并打印原因：`⚠ 服务被 SIGTERM 终止` / `▶ 服务已停止` /
  `⚠ 服务异常退出，退出码 N`
- 启动时会打印 `▶ 服务进程 PID: xxxxx`，**要停就按这个 pid 停**。
  别用 `pkill -f uvicorn` 这种模式匹配——机器上有多个实例时会把别人的一起杀掉
- 真崩溃的话日志里一定有 `Traceback` 或 `未处理异常`，按那个查

---

## 五、目录结构

```
aihub/
├── app/
│   ├── main.py              # FastAPI 路由：chat(SSE) / image / video / jobs / config
│   ├── config.py            # config.yaml + .env 加载与 ${VAR} 插值、默认模型、保存目录
│   ├── logging_setup.py     # 日志：控制台 + data/logs/aihub.log 滚动文件、密钥打码
│   ├── tools.py             # 对话工具：联网搜索（Tavily/博查/Bing）、网页抓取
│   ├── context.py           # 运行时上下文注入：当前时间/时区
│   ├── logctx.py            # 日志上下文：让底层日志也带 job/conv/model
│   ├── cos.py               # 腾讯云 COS 临时图床：上传/预签名/用完删除
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
| GET·PUT | `/api/defaults` | 读取 / 设置各能力的默认模型 |
| GET | `/api/tools` | 可用工具清单（含当前搜索后端） |
| GET | `/api/context` | 当前会注入给模型的时间上下文 |
| GET | `/api/cos` | COS 配置状态（不含密钥） |
| POST | `/api/cos/test` | COS 自检：上传→预签名→下载→删除 |
| GET | `/api/logs?file=&q=&level=&lines=` | 查看 / 搜索日志 |
| GET | `/api/logs/files` | 日志文件清单 + 当前保留天数 |
| PUT | `/api/logs/config` | 设置保留天数（并立即清理） |
| POST | `/api/logs/cleanup` | 立即清理过期日志 |
| DELETE | `/api/logs/{name}` | 删除某个历史日志文件 |
| GET | `/api/fs?path=` | 浏览服务器目录（目录选择器用） |
| POST | `/api/fs/mkdir` | 新建文件夹 |
| POST | `/api/chat` | 流式对话（SSE） |
| POST | `/api/image` | 提交出图任务（`?wait=true` 同步等结果） |
| POST | `/api/video` | 提交视频任务 |
| GET | `/api/jobs?kind=&include_hidden=` | 任务列表 |
| DELETE | `/api/jobs/{id}` | 移除记录（有结果的只隐藏，不删文件） |
| POST | `/api/jobs/{id}/restore` | 恢复被隐藏的记录 |
| POST | `/api/jobs/clear?kind=` | 清空列表（同上规则） |
| GET | `/api/jobs/{id}` | 单个任务状态 |
| GET·POST | `/api/conversations` | 会话列表 / 新建 |
| GET·PATCH·DELETE | `/api/conversations/{id}` | 会话详情 / 重命名 / 删除 |
| GET | `/api/conversations?q=` | 按标题+正文搜索会话 |
| DELETE | `/api/messages/{id}?following=` | 删消息（`following=true` 连带其后全部） |
| POST | `/api/attachments` | 上传文档并解析成文本（multipart） |
| GET | `/api/attachments/info` | 支持的格式与大小上限 |
| GET·POST | `/api/presets` | 提示词预设列表 / 保存（同名覆盖） |
| DELETE | `/api/presets/{id}` | 删除预设 |
| GET | `/media/{kind}/{name}` | 本地媒体文件（kind = image / video） |

---

## 八、注意事项

- 默认只监听 `127.0.0.1`。若要局域网访问，设 `HOST=0.0.0.0`——但此服务**没有鉴权**，
  请勿直接暴露到公网。
- `.env` 已在 `.gitignore` 中，切勿把 Key 提交到仓库。
- 视频生成计费较高，建议先用短时长/低分辨率测试。
- 保存目录若填了不可写的路径，会在保存时报错并回退到默认目录。
- 各厂商接口偶有调整，若某个模型报错，优先核对 `config.yaml` 里的 model id 与官方文档是否一致。

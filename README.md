# AI 门诊病历生成系统

这是“门诊通用病历 + 上传录音 + AI 草稿 + 医生确认 + 导出”的真实部署骨架。

当前版本包含：

- FastAPI 后端
- SQLite 本地数据库
- 音频文件上传与保存
- ASR Provider 接口
- 实时录音分段转写
- 病历生成 Provider 接口
- OpenAI 大模型结构化病历生成
- 医生确认归档
- TXT / JSON 导出
- 浏览器直接打印 / 另存 PDF
- Docker 部署配置

当前 `ASR_PROVIDER=whisper`，使用本地 `faster-whisper` 做真实音频转写；`LLM_PROVIDER=deepseek`，优先使用 DeepSeek OpenAI-compatible Chat Completions JSON 模式生成结构化病历。如果没有配置 `DEEPSEEK_API_KEY`，系统会自动回退到规则版，避免页面中断。

## 目录结构

```text
.
├── backend/
│   ├── __init__.py
│   └── main.py
├── index.html
├── app.js
├── styles.css
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

运行后会生成：

```text
data/
├── ai_emr.sqlite3
└── uploads/
```

## 本机运行

建议使用虚拟环境：

```powershell
cd C:\Users\Administrator\Documents\ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

访问：

```text
http://localhost:8000
```

健康检查：

```text
http://localhost:8000/api/health
```

## Docker 部署

```powershell
cd C:\Users\Administrator\Documents\ai
copy .env.example .env
docker compose up -d --build
```

查看日志：

```powershell
docker compose logs -f
```

停止：

```powershell
docker compose down
```

## 核心接口

```http
GET  /api/health
POST /api/audio
POST /api/audio/{audio_id}/transcribe
POST /api/audio/chunk/transcribe
POST /api/emr/generate
POST /api/emr/confirm
GET  /api/emr/{record_id}/export.txt
GET  /api/emr/{record_id}/export.json
```

## 打印病历

医生确认病历后，前端会启用“打印病历”按钮。点击后浏览器只打印正式病历内容，不打印工作台界面、按钮、转写框或质控侧栏。

打印窗口中可以选择：

- 实体打印机
- 另存为 PDF
- 医院内网虚拟打印机

## 实时录音转写

前端提供“开始实时录音”和“停止录音”：

```text
浏览器麦克风
  ↓ 每 7 秒生成一个音频片段
POST /api/audio/chunk/transcribe
  ↓ Whisper 转写
追加到转写文本框
  ↓
点击“生成草稿”
  ↓
大模型生成病历
```

说明：

- 浏览器需要允许麦克风权限。
- CPU 转写时可能会有延迟，长问诊建议 GPU 部署。
- 当前实时转写是分段转写，不是低延迟逐字流式 ASR。
- 当前还没有说话人分离，医生/患者角色需要后续接入 diarization 或双麦克风方案。

## 本地真实 ASR

当前已接入 `faster-whisper`：

```env
ASR_PROVIDER=whisper
WHISPER_MODEL_SIZE=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=
WHISPER_BEAM_SIZE=8
WHISPER_TEMPERATURE=0
WHISPER_NO_SPEECH_THRESHOLD=0.8
WHISPER_FALLBACK_MIN_CHARS=18
```

说明：

- 第一次转写会自动下载 Whisper 模型，需要能访问 Hugging Face 模型仓库。
- 当前机器已成功下载并加载 `small` 模型，后续转写会优先使用本地缓存。
- `small` 模型适合先跑通真实转写；机器性能较弱时可改成 `base` 或 `tiny`。
- CPU 转写会比音频时长更慢，GPU 服务器可改成 `WHISPER_DEVICE=cuda`。
- MP3、WAV、M4A 等格式由 `faster-whisper` 读取，通常不需要单独安装 ffmpeg。
- 如果下载限速，可以配置 `HF_TOKEN` 或提前把模型缓存到服务器。
- 当前已加入中文门诊医学热词提示和常见医学错词纠正。
- `WHISPER_LANGUAGE=` 表示自动识别语言，适合普通话、口音、粤语或中英混说场景；如果只做普通话，可改回 `zh`。
- DeepSeek 生成病历前会先把方言、口音、口语和错词整理成标准医学普通话，再生成结构化病历。
- 如果第一遍只识别出很短文本，后端会自动关闭 VAD 再重识别一次，减少低音量患者回答被过滤的问题。

识别不准确时优先调整：

```env
# 准确率更高但更慢
WHISPER_MODEL_SIZE=medium

# CPU 可先保持 small；GPU 服务器建议 cuda
WHISPER_DEVICE=cuda

# 值越大搜索越充分，越慢
WHISPER_BEAM_SIZE=8
```

录音建议：

- 医生和患者尽量靠近麦克风。
- 避免多人同时说话。
- 使用外接麦克风优于电脑内置麦克风。
- 实时录音当前每 12 秒分段上传，片段越长上下文越好，但延迟越高。
- 方言、噪声、药名和检查名仍可能识别错误，需要结合大模型纠错和医生审核。

非普通话场景建议：

- 优先使用更清晰的近讲麦克风。
- 方言较重时使用更大模型，例如 `WHISPER_MODEL_SIZE=medium`。
- 保持 `WHISPER_LANGUAGE=` 自动识别。
- 系统会尝试将“肚屙、心口痛、喉咙痛、喘不过气、冇、唔”等表达标准化为医学普通话。
- 如果 Whisper 输出大量重复符号或乱码，例如连续的 `］］］`、`���`，后端会自动清洗；这类问题通常来自静音、噪声、音频片段过短或分段边界。
- 如果 Whisper 把一句话反复续写，例如“是一阵一阵的”重复很多遍，系统会自动压缩重复短句；同时已关闭 `condition_on_previous_text` 来减少重复循环。

常见问题：

- 上传后转写很慢：CPU 正常现象，可换 `tiny/base` 或部署 GPU。
- 返回“未识别到有效语音”：音频可能太短、静音、音量过低或没有清晰人声。
- 不能区分医生/患者：当前 Whisper 只做语音转文本；说话人分离需要额外接入 diarization 模型或使用双麦克风。

## 替换为医院 ASR

修改 `backend/main.py`：

```python
def transcribe_with_provider(audio_path: Path, original_name: str) -> str:
    if ASR_PROVIDER == "mock":
        return build_mock_transcript(original_name)
    if ASR_PROVIDER in {"whisper", "faster-whisper", "faster_whisper"}:
        return transcribe_with_faster_whisper(audio_path)

    # 在这里接入医院 ASR、Whisper、本地模型或云 ASR。
```

正式环境建议返回带角色的文本：

```text
医生：您这次主要哪里不舒服？
患者：我咳嗽三天了，还有点发烧。
```

如果 ASR 服务能返回时间戳和说话人 ID，建议扩展数据库表保存完整 segments。

## DeepSeek 大模型病历生成

`.env` 配置：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=你的 DeepSeek API key
```

DeepSeek 当前使用 OpenAI-compatible Chat Completions 接口和 JSON 输出模式。官方文档说明 base URL 为 `https://api.deepseek.com`，JSON 输出使用 `response_format: {"type": "json_object"}`。

后端会要求模型输出固定 JSON 结构：

```json
{
  "emr": {
    "chief_complaint": "...",
    "history_of_present_illness": "...",
    "past_history": "...",
    "allergy_history": "...",
    "physical_exam": "...",
    "diagnosis": "...",
    "plan": "..."
  },
  "missing_items": [],
  "risk_alerts": [],
  "evidence": []
}
```

如果 DeepSeek 调用失败，系统会自动回退到规则版，并在风险提示里显示失败原因。

## OpenAI 大模型病历生成

`.env` 配置：

```env
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-4o-mini
OPENAI_API_KEY=你的 API key
```

后端会要求模型输出固定 JSON 结构：

```json
{
  "emr": {
    "chief_complaint": "...",
    "history_of_present_illness": "...",
    "past_history": "...",
    "allergy_history": "...",
    "physical_exam": "...",
    "diagnosis": "...",
    "plan": "..."
  },
  "missing_items": [],
  "risk_alerts": [],
  "evidence": []
}
```

当前规则：

1. 输入转写文本。
2. 优先调用 OpenAI 大模型。
3. 输出结构化 JSON。
4. 后端校验字段和证据。
5. 前端展示并由医生确认。

关键约束：

- 未提及写“未提及”
- 不把“未提及”改写为“无”
- 不生成未出现的体格检查或辅助检查
- 诊断必须写“待医生确认”
- 过敏史、否定症状、用药史必须保留证据

## 生产环境必须补齐

上线前至少需要补齐：

- 登录与权限，建议 RBAC
- HTTPS
- 数据库换成 PostgreSQL 或医院指定数据库
- 音频文件加密存储
- 患者姓名、手机号、证件号等敏感字段加密
- 操作审计日志展示与留存策略
- 备份与恢复
- HIS / EMR 对接
- ASR 与 LLM 的隐私合规评审
- 医生确认前禁止写入正式 EMR

## 推荐部署拓扑

```text
浏览器
  ↓ HTTPS
Nginx / 网关
  ↓
FastAPI 应用
  ├─ PostgreSQL
  ├─ 文件存储 / MinIO
  ├─ ASR 服务
  └─ LLM 服务
```

## 当前安全边界

这个版本可以用于内网演示、流程联调和小范围试运行准备，不建议直接接入真实患者数据。真实患者数据上线前必须完成医院安全评审、权限控制、加密、审计和模型合规评估。

# CLAUDE.md

Maya AI Assistant 0.3.0 —— Autodesk Maya 2026 插件。自然语言 → 受控 JSON 计划 → 白名单执行器。

## 红线（违反即事故）

- **绝不 `eval` / `exec` 模型返回的任何内容。** 模型只能返回 JSON 计划，经 `_validate_plan()` 校验后由 `ALLOWED_OPS`（41 个）里的执行器落地。MEL / Shell / PowerShell / 任意系统命令一律不执行。
- **`ALLOWED_OPS` 之外的 op 必须拒绝**，不要为了方便加"临时逃生口"。
- **API Key 不写入 QSettings，不写入仓库、文档、示例、截图。** 当前实现只存内存 + 读环境变量。
- **不要改动已跑通的 provider 请求路径。** 三条路径各自独立，改一条不能影响另两条（见下）。
- **绝不在 QThread worker 里调用 `cmds.*`。** Maya 只允许主线程调用命令；从工作线程调用会被拒绝，并抛出**与本意无关的误导性错误**（如「必须为标志 "selection" 传递一个布尔参数」）。`GenerateWorker.run()` 里只能做纯网络 / 文件工作，需要场景数据就由主线程采集好传进去。

## 结构与约束

- 运行时只有 **一个文件**：`plug-ins/maya_ai_assistant.py`（约 1880 行）。不要拆分模块。
- **零外部依赖**：只用 Python 标准库 + Maya 2026 自带的 `maya.cmds` / `maya.api.OpenMaya` / `maya.OpenMayaUI` / `PySide6` / `shiboken6`。不要引入 `requests` 等第三方包，不要加 `requirements.txt`。
- 网络用 `urllib.request`（`_post_raw`）。远程地址必须 HTTPS，仅放行 `localhost` / `127.0.0.1` 的 HTTP（`_ensure_https_or_local`）。
- 代码注释与文档用中文，与现有风格一致。

## 三条 API 路径（核心）

`api_type` 是唯一决定走哪条路径的开关，在 `generate_plan()` 里分发：

| `api_type` | 方法 | 请求体特征 |
|---|---|---|
| `OpenAI Responses` | `_generate_responses` | 原生 Responses 格式 |
| `RightCode Responses` | `_generate_rightcode_responses` | `input[]` 带 `"type":"message"`，且 `"stream": true`，返回 SSE |
| 其余（默认） | `_generate_chat_completions` | `messages[]`，system + user |

- 三者共用 `_post_raw(url, payload, accept=...)` → 返回**原始文本**；`_post_json` 是它的 JSON 薄封装。
- `_responses_text_from_raw(raw)` 双模解析：`raw` 以 `{` 开头按普通 JSON，否则按 SSE 逐行取 `output_text.delta`。**两种返回都必须能走通。**
- `_join_api_url(base_url, "responses" | "chat/completions")` 负责拼端点。
- 新增 provider 时：加一个 `api_type` 取值 → 加一个 `_generate_*` 方法 → 在 `generate_plan()` 加分发分支。**不要改现有三个分支。**

## 预设机制（三值语义，极易写错）

`API_PRESETS`（模块级）里每个键对应一个预设。取值语义：

| 写法 | 含义 |
|---|---|
| 键缺失 / `None` | **不改动**该字段 |
| `""` | **清空**该字段 |
| 非空字符串 | 填入该值 |

**读取必须用 `preset.get(key)`（默认 `None`）。写成 `preset.get(key, "")` 会把"不改动"误判成"清空"。**

`_apply_preset(name)`：
- 预设只写 `api_type` / `base_url` / `model` 三项，**永不触碰 API Key 与「显示 Key」勾选**。
- `api_type` 的写入用 `QtCore.QSignalBlocker` 包住，使其与写入顺序无关。该阻断目前是**防御性**的（`_api_type_changed` 只在切到 `OpenAI Responses` 且 base_url 为空时写值，写出的值与预设一致），去掉不会立刻出问题，但不要因此删掉。
- `_apply_preset` 写入用 `setText()`，不发 `textEdited`；后续若要加"手改字段 → 下拉框回落到自定义"的联动，必须连 `textEdited` 而非 `textChanged`。

## 验证（改完必须做）

```bash
# 语法检查，不需要 Maya
python -c "import ast,io; ast.parse(io.open(r'plug-ins\maya_ai_assistant.py', encoding='utf-8').read()); print('OK')"
```

- `_responses_text_from_raw` / `_apply_preset` / `API_PRESETS` 是**模块级纯逻辑**，可以脱离 Maya 用 ast 抽取后离线单测，优先这么做。
- 真机验证只能在 Maya 内做（需要 API Key）。改动 provider 相关代码后，**OpenAI 与 DeepSeek 两条老路径都要实际发一次请求**，不能只测新路径。

## 深入文档

| 主题 | 去哪看 |
|---|---|
| 安装、插件管理器加载、常见报错 | `INSTALL_CN.md` |
| 各 provider 的 Base URL / 模型 / 请求格式 | `PROVIDER_CONFIGS.md` |
| 每个文件放哪、哪些是运行时必需 | `PROJECT_FILES.md` |
| 能力清单与示例指令 | `README.md` / `EXAMPLE_PROMPTS.md` |

# Glint — 个人电脑常驻语义路由器（OS-level Micro-Router）

> **A quiet glint of clarity, anywhere on your Mac.**
>
> 平时完全隐身，只在你需要的那一秒，用常驻 2-3B 本地小模型充当人与电脑内容之间的“前置分诊台”。

## 1. 项目定位与产品哲学

### 1.1 长期愿景：人与电脑之间的常驻路由器

在 Apple Silicon 48GB 统一内存的 Mac 上，一个 4-bit 量化的 2-3B 模型仅占用不到 2GB 内存，完全可以全天候常驻后台、毫秒级响应。Glint 不让小模型去干“写长文、做深度推理”这类重活，而是定位为整个操作系统的**常驻语义路由器**，覆盖人与电脑交互的三个双向核心场景：

| 方向 | 微能力（Micro-Skill） | 核心价值 | 规划阶段 |
| :--- | :--- | :--- | :--- |
| **写出去** | **认知提词（Cognitive Cards）** | 在任意输入框敲半句碎片，瞬间弹出 3 张思维卡片（认知框架/输出约束），一键追加或包裹。 | **Phase 1（前期唯一核心）** |
| **读进来** | **信息降噪（Instant Summary）** | 划选长文章、长邮件或终端报错日志，半秒内浮窗吐出 3 条核心摘要或报错主因，先看骨架再看原文。 | **Phase 2+（后期扩展）** |
| **找出来** | **本地资产与安全路由（Local Lookup）** | 用模糊线索秒捞本地笔记、常用命令、代码片段；或将模糊账号描述安全路由至系统钥匙串 / 密码管理器。 | **Phase 2+（后期扩展）** |

### 1.2 前期聚焦原则（MVP Scope）

**前期只做「认知提词」一个能力，把端到端体验和手感打磨到极致。** 其他微能力仅在后端架构中预留插件扩展口（`skills/`），绝不在第一版分散精力。

核心链路（Phase 1）：

$$
\text{部分输入} + \text{当前 App 上下文} \xrightarrow{\text{2-3B 本地小模型 (<150ms)}} \text{意图路由} \xrightarrow{\text{Taxonomy 匹配}} \text{弹出 3 张认知卡片}
$$

* **极速与隐私**：100% 本地推理，零 API 成本，数据绝不出本机。
* **不打断心流**：采用“悬浮卡片选择”替代“行内幽灵字补全”，彻底避开与中文输入法候选框的按键和视觉冲突。
* **解耦设计**：小模型不负责编造长文本，只负责输出极短的结构化路由指令或卡片 ID；Prompt 正文保存在本地纯文本库中，零幻觉、随时热更新。

## 2. 核心交互与用户体验设计（Phase 1：认知提词）

### 2.1 触发机制（克制双轨制）

1. **主动呼出（M1 主力）**：在任意 App 输入框敲完半句大白话或选中一段文字，按下全局快捷键（默认 `Option + Space`），悬浮卡片即刻弹出。
2. **微光提示（M2 进阶，可选）**：仅在白名单 App（如浏览器 AI 页面、Cursor、Obsidian）中，当用户停顿超过 1.5 秒且字数大于 6 个字时，在输入框角落亮起极小的微光角标，按 `Tab` 才展开卡片，绝不强行弹窗打断输入。

### 2.2 安全文本读取策略（三级降级）

为避免在文档编辑器（Obsidian / Notion / Cursor）中误触全选覆盖全文，读取输入内容严格遵循三级降级：

1. **优先读取选中文本**：用户划选了哪一段，就只处理哪一段。
2. **编辑器类 App（无选中时）**：模拟 `Cmd + Shift + Up`，仅抓取光标所在的**当前段落**。
3. **纯聊天/独立输入框 App（无选中时）**：才允许抓取当前输入框全文（M1 用剪贴板兜底，M2 切换至 `AXUIElement` 无感读取）。

### 2.3 卡片浮窗与上屏方式

* **不抢焦点**：基于 macOS 原生 `NSPanel`（`.nonactivatingPanel`），弹出时不丢失原输入框光标。
* **极简 3 卡片**：每次严格展示 **3 张不同认知维度**的卡片（标题 + 一行指令预览），避免同质化疲劳。未来扩展“秒速摘要”或“本地检索”时，复用同一个浮窗容器，仅切换卡片渲染样式（只读展示卡片 / 可复制卡片）。
* **键盘秒选**：
  * 按 `1 / 2 / 3` 或 `↑ / ↓ + Enter`：**追加模式（Append，默认）**——保留你现场写的大白话，仅将选中卡片的思维指令接在后面。
  * 按 `Shift + Enter`：**包裹模式（Wrap）**——将你写的内容作为 `{{input}}` 变量，嵌进完整模板替换原文。
  * 按 `Esc`：立即关闭并记录“未采纳”负反馈。

## 3. 认知卡片库体系（Prompt Taxonomy）

放弃僵硬的行业树状目录，采用**三层正交积木**设计，让卡片成为随手可取的“思考武器”：

* **第一层 · 核心意图（Intent）**：控制动作大方向（8–10 个高频动作，如批判排雷、分析拆解、文字润色、代码排障）。
* **第二层 · 认知框架（Cognitive Lens）**：高价值思维模型（如事前验尸、第一性原理、红队拷问、MECE 拆解）。
* **第三层 · 输出约束（Spec）**：控制大模型交卷格式（如先反问 3 个问题、Markdown 对比表、去 AI 味）。

### 3.1 第一版种子卡片库（`cards.yaml` 示例）

前期卡片在 50 张以内时，直接使用纯文本 `YAML` 管理，无需引入向量数据库，修改即生效：

```yaml
cards:
  - id: "premortem"
    title: "事前验尸排雷"
    dimension: "critique" # 维度标签，用于强制 3 张卡正交多样性
    intent: "批判排雷 / 方案评估"
    mode: "append"
    prompt: "\n\n请用事前验尸（Pre-mortem）视角：假设这个方案半年后彻底失败了，反推最致命的 3 个漏洞，并给出最低成本的验证方法。"

  - id: "clarify_first"
    title: "先反问我 3 个问题"
    dimension: "explore"
    intent: "需求模糊 / 思路发散"
    mode: "append"
    prompt: "\n\n先不要急着给完整方案。请先找出我描述中缺失的关键信息，向我提出 3 个最核心的澄清问题。"

  - id: "de_ai_rewrite"
    title: "去 AI 味极简重写"
    dimension: "polish"
    intent: "文字润色 / 邮件消息"
    mode: "wrap"
    prompt: "请将以下内容重写得更自然、精炼、像真人在日常沟通，去掉所有套话和过度修饰，保留核心事实：\n\n{{input}}"

  - id: "mece_breakdown"
    title: "MECE 结构化拆解"
    dimension: "structure"
    intent: "分析拆解 / 制定计划"
    mode: "append"
    prompt: "\n\n请按 MECE（不重不漏）原则将这件事拆解为 3-5 个核心模块，并为每个模块给出第一步最小可执行动作。"

  - id: "red_team"
    title: "红队反方拷问"
    dimension: "critique"
    intent: "决策验证 / 寻找盲区"
    mode: "append"
    prompt: "\n\n请扮演最挑剔的反对者（红队），不要附和，直接逐条攻击我上述想法中最脆弱的 3 个核心假设。"

  - id: "decision_matrix"
    title: "多维对比决策表"
    dimension: "structure"
    intent: "技术选型 / 方案取舍"
    mode: "append"
    prompt: "\n\n请提取最关键的 4-5 个评估维度，输出一份 Markdown 对比表格，并在最后给出明确的推荐结论与取舍理由。"

  - id: "code_review"
    title: "架构师 Code Review"
    dimension: "engineering"
    intent: "代码审查 / 重构优化"
    mode: "wrap"
    prompt: "请作为资深架构师审查以下代码。忽略代码风格和命名吹毛求疵，只指出潜在 Bug、边界/并发风险以及可简化的复杂度：\n\n{{input}}"

  - id: "root_cause"
    title: "根因定位与排障"
    dimension: "engineering"
    intent: "报错调试 / 故障排查"
    mode: "append"
    prompt: "\n\n请按发生概率从高到低，先列出最可能的 3 个根因，并针对每个根因给出一条最小验证命令或排查步骤。"
```

## 4. 系统架构与技术选型

### 4.1 可扩展四层解耦架构

将**前端系统交互壳**与**后端本地路由大脑**通过本地 HTTP 接口彻底隔离，并在后端预留**微能力插件层（Skills）**。前期只启用 `cognitive_cards` 技能；未来扩展摘要或本地检索时，Swift 客户端几乎无需重构。

```text
┌──────────────────────────────────────────────────────────┐
│  1. macOS 原生交互壳 (Swift + SwiftUI, Menu Bar App)       │
│     • 全局快捷键监听 / 读取 App 与上下文 / 不抢焦点浮窗      │
└─────────────────────────┬────────────────────────────────┘
                          │ HTTP (localhost:8765)
                          ▼
┌──────────────────────────────────────────────────────────┐
│  2. 本地路由服务 (Python FastAPI, launchd 开机静默自启)     │
│     • POST /route    : 意图分发 -> 调度对应微能力 (Skills)  │
│     • POST /feedback : 异步记录用户选择或取消行为           │
├──────────────────────────────────────────────────────────┤
│  2.5 微能力插件层 (skills/)                                │
│     • [Phase 1] cognitive_cards.py (认知提词卡片 · 唯一启用) │
│     • [Phase 2] instant_summary.py (划词秒速摘要 · 预留)    │
│     • [Phase 2] local_lookup.py    (笔记/命令/钥匙串 · 预留)│
└────────────┬────────────────────────────┬────────────────┘
             │                            │
             ▼                            ▼
┌─────────────────────────┐  ┌─────────────────────────────┐
│  3. 本地推理 & 资产库     │  │  4. 数据飞轮 (SQLite)        │
│  • Ollama / MLX-LM      │  │  • usage.db                 │
│  • keep_alive: -1 常驻   │  │  • 沉淀正负反馈微调语料       │
│  • cards.yaml (热加载)   │  └─────────────────────────────┘
└─────────────────────────┘
```

### 4.2 开源参考策略

* **重点参考 Cotabby（Swift / macOS）**：借鉴其基于 `AXUIElement` 读取焦点输入框、获取光标位置，以及利用 `NSPanel` 实现不抢焦点悬浮窗的核心实现；剥离其行内幽灵字渲染逻辑，替换为 Glint 的通用卡片浮窗 UI。
* **辅助参考 GhostType / KeyType**：借鉴其全局快捷键注册、输入停顿防抖（Debounce）逻辑以及与本地推理后端的轻量通信方式。

### 4.3 推荐技术栈（macOS 48GB 专属）

| 模块 | 选型 | 说明 |
| :--- | :--- | :--- |
| **桌面交互壳** | Swift + SwiftUI | 菜单栏常驻（无 Dock 图标），低内存占用，原生窗口体验极佳 |
| **本地服务** | Python 3.11+ / FastAPI / Uvicorn | 轻量易调试，插件化扩展成本低，天然衔接后续 Python 训练生态 |
| **本地推理** | Ollama（M1）$\to$ MLX-LM（M3） | M1 拉取 `qwen2.5:3b` 或 `7b`，开启 JSON Structured Output；强制 `keep_alive: -1` 常驻内存（仅占 ~2GB），彻底消除冷启动卡顿 |
| **进程托管** | macOS `launchd` (`.plist`) | 开机静默拉起 FastAPI 服务，崩溃自动重启，实现零感运行 |
| **数据存储** | YAML + SQLite | `cards.yaml` 管卡片，`usage.db` 管行为日志 |

### 4.4 Monorepo 项目目录结构

```text
glint/
├── macos-app/                    # Swift + SwiftUI 原生菜单栏应用
│   ├── App/
│   │   ├── GlintApp.swift        # 菜单栏入口、辅助功能权限引导、全局快捷键
│   │   └── FloatingPanel.swift   # 自定义 NSPanel (.nonactivatingPanel)
│   ├── Services/
│   │   ├── ContextReader.swift   # 获取当前 App 名 + 三级降级安全文本读取
│   │   ├── TextInjector.swift    # Append / Wrap 文本写回与剪贴板恢复
│   │   └── BrainClient.swift     # 调用 localhost:8765/route & /feedback
│   └── Views/
│       └── CardPopupView.swift   # 极简卡片列表 + 键盘事件 (1/2/3/Enter/Esc)
├── brain-server/                 # Python 本地路由服务
│   ├── main.py                   # FastAPI 接口定义
│   ├── router.py                 # 意图分发与 Ollama/MLX 推理调用
│   ├── skills/                   # 插件化微能力目录
│   │   └── cognitive_cards.py    # Phase 1 核心：认知提词卡片匹配与多样性约束
│   ├── logger.py                 # SQLite 日志读写
│   ├── com.glint.brain.plist     # macOS launchd 开机自启配置
│   └── data/
│       ├── cards.yaml            # 个人认知卡片库
│       └── usage.db              # 自动生成的反馈日志库
└── training/                     # M3 阶段：微调专属 2-3B 路由模型
    ├── export_dataset.py         # 从 usage.db 导出正负样本 + 前缀截断增强
    └── train_mlx_lora.sh         # 48GB Mac 本地 MLX LoRA 微调与权重融合脚本
```

## 5. 数据飞轮、模型专精化与安全红线

无需单独开发额外的键盘记录软件，**Glint 本身就是最高纯度的数据采集器**。

### 5.1 隐形日志表设计（`usage.db`）

每次触发快捷键并做出选择（或按 `Esc` 取消）时，后台静默写入一条记录：

* `id`, `timestamp`
* `app_name`：当前激活的窗口应用名（如 `Cursor`、`Google Chrome`、`Obsidian`）
* `skill_type`：触发的微能力类型（Phase 1 固定为 `cognitive_cards`）
* `input_text`：用户输入或选中的原始片段
* `recommended_ids`：模型给出的 3 张候选卡片 ID（JSON 数组）
* `selected_id`：用户最终选择的卡片 ID（若按 `Esc` 取消则为 `NULL`，天然构成负样本）
* `inject_mode`：`append` 或 `wrap`
* `latency_ms`：推理耗时（用于持续监控手感）

### 5.2 隐私与安全红线（极重要）

1. **应用白名单**：开启被动监听或扩展采集时，仅对白名单内的工作/AI 应用生效。
2. **密码框系统级过滤**：在 `ContextReader` 中强制忽略 `AXSecureTextField` 等安全输入控件。
3. **凭证路由隔离原则（针对未来密码匹配场景）**：**严禁将任何明文密码、API Key 或敏感凭证喂给小模型、存入 `usage.db` 或用于模型微调**（既防隐私泄露，也防小模型字符幻觉）。未来若实现“模糊找密码”，严格遵循**“模型管模糊意图，密码库管精确明文”**——2B 模型只负责将模糊描述路由到目标条目名，再通过安全 CLI（macOS Keychain / 1Password CLI / Bitwarden CLI）完成凭证读取与填充，模型全程不接触明文。
4. **纯本地闭环**：SQLite 文件与模型权重仅存本机，绝不上传云端。

### 5.3 从零训练到 2-3B 专精路由模型

当 `usage.db` 攒满 500–1000 条真实个人选择日志后：

1. **前缀截断增强（Prefix Truncation）**：对高频正样本按 30%、60%、100% 长度切分，训练模型在用户只敲了半句话时就能精准预判意图。
2. **MLX LoRA 微调**：在 48GB 统一内存 Mac 上使用苹果官方 `mlx-lm`，对 2B–3B 基座模型（如 Qwen2.5-3B）进行 LoRA 微调（耗时约 30–60 分钟）。
3. **热替换**：将微调融合后的模型挂载到 `brain-server`，响应速度压进 **100ms 以内**，且高度贴合个人思考习惯。

## 6. 分阶段实施路线图（Roadmap）

### Milestone 1：跑通「认知提词」最小闭环（Day 1 – Day 5）

> **目标**：只做认知提词，先用起来，验证“快捷键呼出 -> 3 张卡片 -> 一键上屏”的产品手感。

* **Day 1**：本地安装 Ollama（拉取 `qwen2.5:3b`，配置 `keep_alive: -1`），写完 `brain-server`（`main.py` + `skills/cognitive_cards.py` + 8 张种子 `cards.yaml`），用 `curl` 验证 300ms 内稳定返回正交 3 卡片 JSON。
* **Day 2–3**：搭建 `macos-app` SwiftUI 菜单栏工程，调通全局快捷键与不抢焦点的 `NSPanel` 3 卡片弹窗及键盘 `1/2/3/Esc` 交互。
* **Day 4**：实现三级降级文本读取（M1 采用剪贴板备份/恢复兜底）与 `Append / Wrap` 文本写回。
* **Day 5**：配置 `launchd` 开机自启，接通 `/feedback` SQLite 埋点，正式投入日常使用。

### Milestone 2：体验打磨与 Taxonomy 迭代（Week 2 – Week 4）

> **目标**：消除交互毛刺，打磨出最称手的个人认知卡片库。

* 将剪贴板兜底逐步升级为 `AXUIElement` 原生无感读写（保留剪贴板作为 Electron 应用的降级方案）。
* 根据日常使用体感，持续增删、合并 `cards.yaml`，并在超过 100 张卡片时引入 SQLite FTS5 / BM25 粗筛层。
* 在后台静默沉淀 500–1000 条带正负反馈的真实个人日志。

### Milestone 3：模型专精化（Month 2）

> **目标**：拥有一个完全懂你的毫秒级私人 2-3B 路由大脑。

* 导出 `usage.db` 真实数据，结合前缀截断增强构造训练集。
* 在 48GB Mac 上用 `mlx-lm` 完成 LoRA 微调并热替换推理引擎。
* 视需求探索“白名单 App 停顿微光角标”等轻度无感交互。

### Milestone 4：扩展 OS 级微能力生态（长期演进）

> **目标**：在不改动核心交互的前提下，逐步解锁 Glint 作为“常驻语义路由器”的全部潜力。

* **接入 `instant_summary`**：识别到用户划选大段只读文本或终端报错时，自动路由至摘要卡片，半秒输出 3 条核心要点。
* **接入 `local_lookup`**：接入本地 Obsidian 笔记、常用 CLI 命令库与代码片段检索。
* **接入安全凭证路由**：通过模糊语义匹配调用 macOS Keychain / 1Password CLI，实现安全的零明文暴露账号查找。

## 7. 关键工程避坑清单（Checklist）

* [ ] **防范围蔓延**：Phase 1 严格克制，只实现「认知提词」，跑通日常使用后再解锁其他微能力。
* [ ] **防全选误伤**：严禁在文档/代码编辑器里无脑执行 `Cmd + A`，必须落实“已选文本 $\to$ 当前段落 $\to$ 聊天框全选”三级降级。
* [ ] **防模型冷启动**：Ollama/MLX 必须开启常驻内存（`keep_alive: -1`），避免 5 分钟后再次呼出出现 2–3 秒卡顿。
* [ ] **防卡片同质化**：路由 Prompt 中必须强制要求返回的 3 张卡片覆盖不同 `dimension`（如 1 张挑刺 + 1 张结构化 + 1 张润色/反问）。
* [ ] **防双进程开机阻力**：第一周内务必配好 `launchd` 守护脚本，避免每天手动启动 Python 后端导致工具吃灰。
* [ ] **守住密码安全红线**：强制过滤 `AXSecureTextField`，严禁任何明文密码进入小模型上下文、日志库或微调数据。

## 8. 后续待办备忘（Backlog）

* [ ] **脱离外接硬盘运行依赖**：当前 `Glint.app` 已安装在本机 `/Applications/Glint.app`，但 `brain-server` 运行目录与数据（`cards.yaml`、`usage.db`）以及菜单栏「编辑卡片库」仍指向外接硬盘 `/Volumes/个人资料/2026/开发项目/Glint/brain-server`。后续评估将运行副本与数据目录迁至本机 `~/.glint/`（或由 `brain-server` 动态暴露数据路径），确保拔掉外接硬盘带出门也能正常常驻使用。
* [ ] **内容与意图路由深化**：积累一周真实 `usage.db`（含第 4 选项 `custom_intent`）后，复盘迭代个人卡片库与模型意图识别准确度。

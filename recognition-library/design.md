# Glint（微光）项目顶层蓝图与架构总纲 (v2.5)

> **文档定位**：本文件为 Glint 项目的唯一真相源（SSOT），聚焦「定义清楚系统需要具备哪些能力（What）与核心边界」，已收口前期讨论中的所有概念冲突与架构盲区。

---

## 一、核心定位与人机协作范式

* **产品定位**：Glint（微光）是人与个人电脑之间的**常驻认知路由器（OS-level Cognitive Router / 外接大脑）**。它不替代强 LLM 干重活，也不做静态知识库，而是利用本地常驻 2B 小模型的毫秒级响应，在用户产生想法的那一秒，将最趁手的认知能力（Lens）推到光标旁。
* **阶段边界**：
  * **Phase 1（当前唯一聚焦）**：**认知提词（写出去）**——在任意 App 输入碎片想法，秒出 3 张认知透镜卡片。
  * **Phase 2+（架构预留）**：**信息降噪（读进来）**（划词秒速摘要/日志排障）与**本地资产路由（找出来）**（模糊意图唤醒笔记/命令/密码库）。
* **极简人机分工**：
  $$\text{人类（低负担三元信号）} \xrightarrow{\text{毫秒级路由}} \text{Glint（本地 2B 路由器 + 认知透镜）} \xrightarrow{\text{高杠杆执行}} \text{强 LLM（深度思考）}$$

### 统一双层输入模型（已解决输入断层）
将“人类认知减负”与“机器精准路由”解耦为上下两层映射：
1. **人类表达层（三元认知信号）**：用户无需记忆提示词，只需自然表达三类主观信号：
   * **表达偏好（Preference）**：风格、语气、深浅与输出要求（如“硬核实战、去 AI 味、只给表格”）。
   * **表达期望状态（Desired State）**：想达到的终点或当下的模糊痛点（如“想做新产品但没头绪”）。
   * **表达期望变化（Desired Transformation）**：想对眼前材料施加的动作（如“优化前端”、“排雷”、“压缩”）。
2. **机器路由层（开集双路检索 + 进程内精排，2B 模型作为降级兜底与 Phase 2 引擎）**：
   * **闭集（2B 降级兜底与 Phase 2 微技能标签体系，词表在 `ontology.yaml` 冻结）**：
     * 预测原则：当主路 Cross-Encoder 不可用时降级到 `qwen3.5:2b` 闭集预测主 `primitive` / `goal` / `clarity`，随后按严格正交补齐 3 张卡片。
     * **核心边界（要求 $\neq$ 意图）**：风格/格式类要求（如“去 AI 味、只给表格”）在检索前置预处理阶段剥离，**不干扰主意图判定**（如“去 AI 味、只给表格，帮我分析这个架构”的主意图仍为 `Deconstruct`）；仅当输入无其他主动作时，“润色/去 AI 味重写”本身才判定为 `Translate`。
     * `goal`（核心诉求，5 项）：`create`（创造） / `optimize`（优化） / `debug`（排障） / `evaluate`（评估） / `digest`（消化）。
     * `clarity`（认知清晰度，3 项）：`vague`（模糊态） / `concrete`（具体态） / `conflicted`（纠结态）。
     * `primitive`（认知元操作，7 项）：见第二节本体论（单选主元操作）。
     * `preference_tags`（偏好修饰）：从“表达偏好”中提取的轻量风格/格式约束，作为执行期可组合修饰器（Modifier）。
   * **开集（本地 Embedding + BM25 混合召回与进程内 Cross-Encoder 精排，新增领域零重训）**：
     * 本地 Ollama 常驻 `bge-m3:latest`（1024 维中英跨语种向量）配合内存预索引 BM25，在 Stage 1 通过 RRF 融合产出高多样候选池，再由进程内 Apple Silicon MPS `BAAI/bge-reranker-v2-m3` 完成毫秒级精排。

---

## 二、认知边界与核心本体论（Ontology SSOT）

### 1. 认知边界测试（已修正“去事实”过死问题）
每当考虑纳入新素材或新能力时，执行**「知识类型过滤测试」**：
* **拒绝（放行给搜索引擎 / 笔记 / RAG）**：**陈述性知识（Declarative Knowledge，“是什么”）**——静态事实、百科定义、纯新闻资讯。
* **放行（纳入 Glint 外接大脑）**：**程序性 & 启发式认知能力（Procedural & Heuristic Knowledge，“怎么查、怎么判、怎么拆、怎么避坑”）**——思维框架、诊断清单、反模式、决策启发式、以及高杠杆工具索引。

### 2. 7 大冻结认知元操作（Cognitive Primitives）
作为 2B 路由器的稳定地基与推荐多样性的锚点：
1. **降噪压缩（Compress）**：多 $\rightarrow$ 少（提取骨架、主因与关键事实）。
2. **解构建模（Deconstruct）**：乱 $\rightarrow$ 序（MECE 拆解、因果链、排查清单）。
3. **批判排雷（Critique）**：单视角 $\rightarrow$ 对立面（事前验尸、红队拷问、隐含假设审计）。
4. **发散联想（Diverge）**：1 $\rightarrow$ N（跨界类比、破盲母题、逆向思维）。
5. **收敛决策（Converge）**：N $\rightarrow$ 1（权重对比矩阵、二阶后果、最小验证）。
6. **转译重构（Translate）**：语境 A $\rightarrow$ 语境 B（口语转专业 Prompt、去 AI 味、技术转业务）。
7. **元认知追问（Metacognize）**：给答案 $\rightarrow$ 问前提（揪出缺失变量、重新定义问题）。

> **高频易混边界划界准则（2B 分类与标注 SSOT）**：
> * **`Compress` vs `Deconstruct`**：`Compress` 做**信息减法**（多 $\rightarrow$ 少：摘要、提炼唯一主因/TL;DR）；`Deconstruct` 做**建立结构**（乱 $\rightarrow$ 序：拆模块、拆步骤、列排查清单）。
> * **`Critique` vs `Converge`**：`Critique` 针对**单一对象找漏洞**（1 $\rightarrow$ 反例/盲区）；`Converge` 针对**多选项做取舍**（N $\rightarrow$ 1：权衡矩阵、最小验证拍板）。
> * **两层挂载结构**：第一层（7 大元操作）作为“核心指令集”冻结稳定；第二层（具象思维透镜 Concrete Lenses）以**单 `primitive` 强归属**方式挂载其下，开放、有机生长。

---

## 三、五层认知供应链（L0 → L1 → L2 → L3 库存 → L4 上架）与现有资产整合

### 1. 供应链分层与准入标准
| 层级 | 定义与形态 | 消费对象 | 核心关卡与转化动作 |
| :--- | :--- | :--- | :--- |
| **L0 原始材料** | 书籍、行业长文、架构规范、复盘、`ATLAS-METHODS` 原始技能 | 人类阅读 | **Gate 1（L0 信噪比准入）**：评估信息密度与时效性，拒收水文、纯陈述与私有脚本 |
| **L1 认知素材** | 人类可读的结构化方法论/模板库（保留异构形态，供人查阅） | 人类 & 通用 AI | **L0 $\rightarrow$ L1 精炼**：去陈述性事实，只保留诊断清单、反模式、启发式、追问与范式 |
| **L2 透镜草稿 (Draft)** | LLM 从 L1 初步语义合成的原始原子透镜 YAML（只读存底，允许微脏） | **L2 $\rightarrow$ L3 清洗引擎** | **L1 $\rightarrow$ L2 语义合成**：原子化切分 + 闭集意图坐标标注 + 合成痛点与触发口语 |
| **L3 生产透镜库存 (Inventory)** | 经确定性清洗、人称/占位符规范、跨源查重与校验后的**合格可用库存**（可无限扩充，不直接上架） | **货架调度器 & 管理台** | **Gate 2–3（L2 $\rightarrow$ L3 确定性清洗）**：枚举/词表归一化、人称语境清洗、占位符修复、可调阈值查重（零 LLM 成本，秒级全量重跑） |
| **L4 生产上架透镜 (On-Shelf / `dist/current`)** | 真正上架供 `brain-server` 实时路由消费的精选货架（含向量索引与原子回滚） | **Glint 实时路由器** | **Gate 4（上下架调度 + 原子发布）**：**常青通用货架（`universal` 永不下架）+ 动态垂直货架（`domain_specific` 按热度/场景动态上下架）**，持续扩充 L3 库存绝不污染线上体验 |

### 2. 现有 4 大 L1 资产的接入定位
Glint 不替代原有仓库的策展，只做下游编译器与实时路由器（每个异构仓库配极薄的标准化提取适配器，避免把 YAML 写成复杂 DSL）：
* **Prompt-taxonomy**（224 模板 / 16 任务 / 19 工作流）：主力执行与转译层，在 `concrete`（具体态）下优先路由。
* **DISCOVERY-TAXONOMY**（18 张破盲卡 / 认知透镜）：元认知与破局层，在 `vague`（模糊态）与探索位优先路由。
* **ATLAS-METHODS**（多批次标准化方法论/技能库）：方法论底座，同时复用其清洗标准化经验。
* **SUPER-INDIVIDUAL-TOOLBOX**（9 岗位 / 36 兵器）：角色与杠杆工具索引层，通过资源唤起/清单形态提供支持。

### 3. 新领域 10 分钟冷启动（Glint Forge）
解决个人跨界学习与对外产品化的“大而全”难题：
* **通道 A（自带 L0 原料投喂）**：丢入 3–5 篇高质量文档或笔记 $\rightarrow$ 强 LLM 按“去陈述、留程序/启发式”精炼 $\rightarrow$ 自动编译为 L2 领域包，依托开集检索即插即用（零重训）。
* **通道 B（零原料逆向合成）**：用户仅输入三元信号（领域身份 + 期望状态/高频痛点 + 风格偏好） $\rightarrow$ 强 LLM 按 7 大元操作矩阵定向合成 15–20 张 **Starter Domain Pack**，后续靠实战摩擦淘汰进化。

---

## 四、系统核心能力全景（五大能力域）

> **核心工程原则**：一切变化关进“配置与流水线”里，核心运行代码写稳后尽量不再动。

### 域 A：生产与转化能力（Ingestion & Forge）
1. **多源标准化接入**：支持本地目录、Git 仓库、JSON 源及薄提取插件，基于 Hash 增量同步。
2. **L0 准入筛查与精炼**：过滤低质原料，执行 L0 $\rightarrow$ L1 程序性算子提取。
3. **语义补全（Enrichment）**：读取唯一词表，自动为缺失意图标签的 L1 素材补齐闭集坐标与置信度。
4. **日常摩擦捕获（Friction-to-Asset）**：自动记录 `Esc` 取消的“未满足意图”；支持快捷键一键将高光现场 Prompt 抽象为 L2 草稿。

### 域 B：治理、清洗与发布能力（Governance & Build）
5. **唯一真相源治理（Ontology SSOT）**：全局唯一 `ontology.yaml` 管理枚举、同义词映射（Alias Map）与 Schema 版本号。
6. **确定性清洗与查重门禁**：自动归一化枚举、校验占位符语法；基于语义相似度拦截跨源重复/冲突卡片。
7. **隔离区机制（Quarantine）**：未知枚举、低置信度或重复冲突条目自动入隔离区附带原因，不污染线上库。
8. **不可变覆写层（Overlay）**：上游同步目录严格只读；本地调教（废弃标记、权重微调、标题修改）写入独立 `overrides.yaml`，编译时自动合并，彻底杜绝上游同步冲掉本地修改。
9. **原子编译与回滚**：将激活态 L2 透镜编译为只读运行时索引库，软链接原子切换，支持一键回滚。

### 域 C：实时路由与交互能力（Runtime & UX）
10. **上下文感知与防误伤读取**：读取当前 App 与输入内容；遵循“已选中文本 $\rightarrow$ 当前段落/当前行 $\rightarrow$ 全框（仅限聊天 App）”三级降级，绝不在编辑器中误全选整篇文档。
11. **外置策略路由与质量门控正交**：Stage 2A 精排上下文加权（`stage2_rerank`）、工作流续接门槛（`workflow`）与 L3 穿透参数（`l3_breakthrough`）外置至 `routing_policy.yaml` 实时热加载；Top-3 采用**质量门控正交**选卡（严格覆盖 3 个不同 `primitive`，当第 2/3 位垂直卡跌入低分悬崖时自动以通用常青卡兜底替换），并由「认知种子」独立承担被动破盲探索。
12. **超时自动降级与常驻保活**：本地 `bge-m3` 与 `qwen3.5:2b` 常驻 Ollama、`bge-reranker-v2-m3` 常驻 `brain-server` 进程；若 Cross-Encoder 不可用则降级为 `qwen3.5:2b`，再失败则无缝降级为 Hybrid RRF 规则兜底，保证交互零卡顿。
13. **多动作协议与路由后闭环（已补齐交互断点）**：
    * 支持 4 类 `action_type`：`inject_prompt`（追加/包裹写入）、`show_checklist`（浮窗阅读不覆盖输入）、`open_resource`（唤起工具/链接）、`workflow_entry`（多步工作流入口）。
    * **多步工作流语义衔接**：选中工作流第 1 步后按 App 记录轻量会话态；后续仅在明确续接语句或达到语义相关性门槛时才置顶下一步透镜，切换无关话题自动清除会话，避免跨任务误劫持。
    * **零摩擦多变量处理**：`{{input}}` 自动承接当前文本；其余变量优先由上下文推断，无法推断时自动转为指令要求强 LLM 先反问澄清，彻底消灭手动逐个填空。

### 域 D：评测与双环自进化能力（Eval & Flywheel）
14. **黄金集离线回归门禁（Golden Eval）**：维护核心测试集，任何策略调整、新源接入或模型更新必须通过意图 F1、Hit@3 与 P95 延迟门禁方可发布。
15. **内环——周级材料自进化**：自动区分**「高曝光零点击（真废卡 $\rightarrow$ 淘汰/重写）初期仅记录曝光、调用频率，不急于判定认知价值」**与**「零曝光（冷门好卡 $\rightarrow$ 进探索位）」**；对高频 `Esc` 取消日志聚类并提议生成新卡草稿。
16. **外环——月级 2B 模型专精化**：日志攒满阈值后，导出正负反馈与难负样本，在 48GB Mac 上本地 MLX LoRA 微调闭集意图路由器，经黄金集评测达标后原子切换。

### 域 E：隐私安全与对外生态边界（Safety & Ecosystem）
17. **隐私脱敏与密码红线**：采用 App 白名单机制，跳过密码框，日志落盘前自动正则擦除 Key/Token/手机号。**严禁明文密码进入模型或日志**（未来密码场景严格遵循“模型管模糊意图，系统钥匙串/密码库管精确明文”）。
18. **对外产品形态**：**Glint Core**（极简开源内核 + 15–20 张通用核心透镜） + **Glint Forge**（内置领域精炼引擎） + **Domain Packs**（去中心化 Git 领域包协议）。

---

## 五、防过度设计纪律：设计一步到位，实现按需开关

为避免“蓝图太完美导致验证周期过长”，落地严格分三阶段按需点亮能力：
* **Stage 1（最小闭环验证，优先跑通）**：仅点亮「`Prompt-taxonomy` + `DISCOVERY-TAXONOMY` 薄接入 $\rightarrow$ 基础清洗编译 $\rightarrow$ 现成 Ollama 2B/3B 闭集路由 $\rightarrow$ macOS 悬浮 3 卡交互」，验证核心假设（弹出 3 张认知卡是否真的显著降低思考负担）。
* **Stage 2（治理与内环进化）**：点亮 Overlay 覆写层、隔离区、语义查重、摩擦捕获与周级体检。
* **Stage 3（模型专精与对外精炼）**：点亮黄金集离线评测、本地 MLX 微调 2B 路由器与 `glint forge` 新领域冷启动引擎。

---

## 六、核心链路详细设计：输入 → 意图 → Glint 认知材料

本节定义「输入 $\rightarrow$ 意图 $\rightarrow$ Glint 认知材料」运行期与数据面的核心技术规范，作为 Stage 1 开发的直接依据。

### 1. 两阶段极速路由架构与模型选型（当前在线 `v2_hybrid_rerank`）

```text
用户输入 (input_text) + macOS 上下文 (app_name) + 可选工作流会话态
   │
   ▼ [预处理：风格修饰语剥离（「要求 ≠ 意图」），仅当剩余主诉求 ≥ 6 字时生效]
   │
   ▼ [Stage 1：Hybrid Recall 混合粗排（~25–45 ms）]
     · 内存预索引 BM25（中文字面二元组 + 英文词，过滤高频对话停用词）
     · Ollama bge-m3:latest (1024 维) 计算查询向量做全量余弦检索
     · RRF 融合排序 → 产出 18 张高相关且覆盖 7 大元操作的候选池（模糊提问注入核心澄清锚点）
   │
   ▼ [Stage 2A：Cross-Encoder 精排（MPS FP16，batch=20–24 实测 P50 ~580–680 ms）]
     · 进程内 Apple Silicon MPS 运行 FP16 BAAI/bge-reranker-v2-m3（单线程专用执行器 + inference_mode）
     · 精排文档画像保留全部用户触发口语（不截断），按与当前输入的词面重合度动态前置
     · 由 _compute_stage2_score 统一叠加：低置信 Stage-1 兜底 + evergreen (+0.45) + App 亲和 (+0.20 + 领域先验) + 模糊先验 + base_weight，且全部支持从 routing_policy.yaml 热读取
     · 质量门控正交（Quality-Gated Orthogonality）选出 3 张不同 primitive 的严格正交卡
     · 在原始 logit 标度对齐比较并合并 L3 穿透（最多 1 张）与工作流续接置顶
     · 若 Reranker 不可用，自动降级至 Stage 2B (qwen3.5:2b) 或 Hybrid RRF 兜底
```

* **本地模型分工与索引选型**：
  * **Phase 1 选卡主力**：`bge-m3:latest`（Ollama 粗排）+ `BAAI/bge-reranker-v2-m3`（`torch` MPS 进程内精排，绕开当前 Ollama 暂不支持 `/api/rerank` 的限制）。34 条 Golden Eval 达成 **`Hit@1 = 97.1%`、`Hit@3 = 100.0%`、`Forbidden = 0.0%`、`Orthogonality = 100.0%`**。
  * **`qwen3.5:2b` 定位**：Phase 1 作为 Stage 2B 降级兜底；Phase 2 承担需要文本生成的微技能（划词秒速摘要 `instant_summary`、超长日志压缩、动态改写）。
  * 零重型向量库：L4 上架透镜在百卡量级下向量总大小不足 2 MB，构建阶段预计算并打包为轻量静态索引，服务启动时全量常驻内存做矩阵点积（耗时 < 0.5 ms）。

### 2. L2 认知材料（Lens）统一 Schema 规范

严格分离**「给路由器吃的索引面」**与**「给强 LLM / 人类吃的执行面」**。**Embedding 仅编码路由画像（`title + pain_point + trigger_utterances + domains`），严禁直接编码 `execution.content` 正文**，避免 Prompt 模板套话污染语义检索空间。

```yaml
# 1. 身份与展示面（UI 渲染）
id: "arch_mece_deconstruct"         # 全局唯一不可变 ID（供日志与 Overlay 关联）
title: "架构 MECE 拆解"              # 短标题（<= 12 字）
tagline: "把一团麻的架构理出边界与模块"  # 悬浮窗单行预览

# 2. 路由坐标与语义画像面（2B + Embedding 路由器消费）
routing:
  primitive: "deconstruct"          # 单 primitive 强归属（7 选 1，决定所属元操作桶）
  goals: ["optimize", "create"]     # 适用诉求（1~2 项）
  clarity_fit: ["vague", "concrete"]# 适用清晰度
  domains: ["architecture", "backend", "system_design"]  # 开集领域标签
  app_affinity: ["Cursor", "VS Code", "Notion", "Obsidian"]

  # Embedding 编码核心语料（与用户真实碎片口语对齐）
  pain_point: "系统/方案思路混乱、耦合严重、不知道从哪一层开始下手拆解"
  trigger_utterances:               # 3~5 句典型用户口语触发例句
    - "帮我分析一下这个架构，感觉太乱了"
    - "这个需求好复杂，不知道怎么拆模块"
    - "理一下这个系统的核心边界和依赖"
  anti_triggers:                    # 可选：负向排除语境（防误召回）
    - "单纯修一个语法报错"

# 3. 执行协议面（交互层 & 强 LLM 消费）
execution:
  action_type: "inject_prompt"      # inject_prompt | show_checklist | open_resource | workflow_entry
  inject_mode: "append"             # append | wrap（仅 inject_prompt 生效）
  content: |
    请按 MECE（不重不漏）原则将上述架构/问题拆解为 3-5 个核心模块，明确各模块职责与边界，并指出最脆弱的耦合点。
  variables: []                     # 除 {{input}} 外的额外变量（非空时自动触发“先反问澄清”封装）
  workflow: null                    # 多步工作流元数据：{ workflow_id, step, next_lens_id }

# 4. 治理与溯源面（编译流水线 & 自进化消费）
meta:
  source: "Prompt-taxonomy"         # 来源仓库 / glint-core / manual-custom / user-friction
  source_ref: "architecture/mece.md"# 上游相对路径（Hash 增量同步定位）
  status: "active"                  # active | quarantine | deprecated
  base_weight: 1.0                  # 基础权重（可由 overrides.yaml 无痛覆写）
  version: 1
  created_at: "2026-11-28T12:00:00Z"# 可选：新建/导入时间戳（用于「最新添加」排序，缺省按 L2 mtime 兜底）
  supersedes: null                  # 可选：升级替换的旧卡 ID（双向血缘回溯）
  superseded_by: null               # 可选：被哪张新卡替代（旧卡通过 Overlay 归档为 deprecated）
  evolution_note: null              # 可选：本次演进改进说明
```

### 3. 运行时策略配置与多态节奏规范

运行时超参数外置至 [routing_policy.yaml](file:///Volumes/个人资料/2026/开发项目/Glint/recognition-library/config/routing_policy.yaml) 并由 `LensRouterSkill` 热加载：
* `stage2_rerank`：控制常青底座提权（`evergreen_boost: 0.45`）、App 亲和（`app_affinity_boost: 0.20`）、权重缩放（`base_weight_scale: 0.55`）及质量悬崖替换阈值（`cliff_score_floor: -4.2`、`cliff_margin_below_top1: 3.5`）。
* `workflow`：控制工作流会话 TTL（`600s`）与跨查询续接相关性底线（`min_rerank_logit: -1.2`、`min_cosine: 0.45`）。
* `l3_breakthrough`：控制 L3 影子库穿透开关、双锚点底线及领先幅度。

在认知交互节奏上，系统同时覆盖三类清晰度状态的期望结构：

| `clarity` 状态 | 槽位 1（主命中位） | 槽位 2（核心协同位） | 槽位 3（补充 / 兜底位） |
| :--- | :--- | :--- | :--- |
| **`vague`（模糊态）** | 模糊安全网锚点 / `metacognize` / 最贴近主 `primitive` | 正交发散或元认知追问 | 解构建模（收口起手式）或通用常青卡 |
| **`conflicted`（纠结态）** | 收敛决策 (`converge`) 或最相关主 `primitive` | `critique`（批判排雷 / 红队拷问） | `metacognize`（追问隐含前提）或通用常青卡 |
| **`concrete`（具体态）** | 高杠杆执行卡（如 `Prompt-taxonomy`） | 次优正交视角 | 第 3 个正交 `primitive`（若垂直卡落入低分悬崖，自动回退通用常青正交卡） |

* **偏好约束处理**：用户输入的格式/风格要求（`preference_tags`）不作为硬过滤条件；原文中已包含的要求在注入执行期自然保留或透传。

---

## 七、L3/L4 三维立体组织与统一管理台架构 (v2.5)

为了避免数百张认知透镜平铺失控，并兼顾「切入新领域批量上架」与「非关注领域只留最高质量精华」两类需求，L3 库存与 L4 货架采用 **正交三维组织 + 声明式上架规则**：

1. **质量层级（`meta.tier: S | A | B`）**：
   - **`Tier S`（常青底座，本机实装 89 项：含公开常青、个人自建常青与私有 `meta-vault` 6 项）**：跨领域最高频元认知与核心动作，**永不下架**，且受进化调度器硬豁免；
   - **`Tier A`（领域精华）**：高置信度核心专业方法论；
   - **`Tier B`（长尾储备）**：细分/实验性/待验证卡片。
2. **二级领域分类树（[taxonomy.yaml](config/taxonomy.yaml)）**：
   - 5 大核心一级领域（通用思维 / 软件工程 / 产品增长与商业 / 写作与沟通 / 个人系统与职业）+ `custom` 自定义领域，下辖 19 个二级子分类；`L2 → L3` 按 `domains` 优先、文本次之的确定性规则自动归类（零 LLM 成本），可被 `overrides.yaml` 随时覆写。
3. **自定义专题包（`Collections`）与声明式 L4 货架（[shelf_policy.yaml](config/shelf_policy.yaml)）**：
   - 每个一级领域独立配置上架模式：**`off`（关闭） / `tier_a`（仅 S+A 精华） / `all`（全部）**；
   - 任意卡片可归入多个自定义专题包，管理台支持**单卡级专题包多选归属**（`PUT /api/lenses/{lens_id}/collections`）、**专题包新建与重命名**（`PUT /api/shelf/collections/{cid}`），且私有专题包读写与 `meta-vault/shelf_policy.yaml` 物理隔离；**最终 L4 上架集合**由公式确定性算出：
     $$\text{L4 On-Shelf} = \text{Tier S (Evergreen)} \;\cup\; \text{Active Collections} \;\cup\; \bigcup_{c} \text{Mode}(c) \;\cup\; \text{Manual Overrides}$$
4. **统一后端管理台（`http://127.0.0.1:18765/admin/`）**：
   - 直接挂载于 `brain-server`（零额外常驻进程），由 macOS 菜单栏 **`Glint 管理…`** 与 **`⚡ 从剪贴板导入认知卡…`** 一键打开。默认呈现 5 大日常视图（**首页与实时试用 / 我的卡片库 / 桌面陪伴节奏 / 制作新卡片 / 使用回顾**），支持 `🕒 最新添加` 排序（切入「✨ 我制作的」自动倒序）、闪电粘贴 YAML 导入、双向血缘归档与单卡专题包管理；开启左下角 **开发者模式** 可展开 **召回排序诊断 / 黄金评测与素材库 / 底层 YAML 与脱敏日志**。
5. **私有二阶资产隔离规范（`meta-vault/`）**：
   - 二阶元认知算子（`operators/`）与私有二阶 L2/L3 透镜存放于 `.gitignore` 忽略的独立私有仓 `meta-vault/`，仅在本地编译时原子合并入 `dist/current/`；
   - 与公开库出现同 ID 冲突或跨仓修改时显式报错阻断；私有卡片的隔离输出、常青与私有专题包配置（`meta-vault/shelf_policy.yaml`）及后台人工调校覆写（`meta-vault/overrides.yaml`）均物理隔离在 `meta-vault/` 内，不回流污染公开 `data/`。
6. **L3 断层领先穿透召回（L3 Breakthrough Gate）**：
   - 发布 `dist/current/` 时同步产出活跃 `off_shelf` 的 `l3_shadow_lenses.json + l3_shadow_embeddings.npz`；
   - 路由阶段复用 Stage 1 查询向量零额外调用捞取 Top-2 策略性待命 L3 候选（默认排除人工显式下架卡，防诈尸），与 L4 候选同批进入 MPS `bge-reranker-v2-m3` 打分；
   - 分差不大一律选 L4；仅当 L3 同时满足双锚点底线（`cos >= 0.45` 且 `logit >= -1.0`）与断层领先幅度（`ΔTop1 >= +1.5` 或 `ΔTop3 >= +2.0`）时，允许最多 1 张带 **`✦ L3 发现`** 角标穿透展示，被用户实际选用后自动在 M3 进化飞轮中最高优推荐晋升 L4。

---

## 八、双飞轮完整形态：被动「认知种子」与 CLE v0.7 底座 (v2.5)

主动语言召回飞轮解决的是**「已知需求 / 表偏好·表期望·表变化」**，但无法回答**「未知之未知（Unknown-Unknowns）」**——用户甚至不知道某个认知视角存在，自然无法主动检索。第二飞轮 **认知种子（Glint Seed · Ambient HUD）** 与主动路由共同构成「主动语言召回 + 被动桌面破盲」双飞轮闭环。

1. **Ambient HUD 桌面陪伴形态（`macos-app`: `SeedPanel` + `SeedController` + `SeedHUDView`）**：
   - **不抢焦、可拖拽、余光可读**：基于 `.nonactivatingPanel` + `canBecomeKey = false` + `isMovableByWindowBackground = true`；背景玻璃通透度与文字对比度解耦（标题与简介在静默态同样一眼清晰，悬停仅浮出微操作条与零延迟内联中文提示）。
   - **每 5 分钟自动慢切 + 零恐惧反馈**：鼠标悬停或展开阅读时自动暂停倒计时；点击「不再轮播 / 7 天不看」后立即切换并弹出 4.5 秒「撤销」条，且**屏蔽仅作用于被动轮播，完全不影响快捷键主动召回**（解除屏蔽后原钉选状态自动保留）。
   - **增强阅读卡（`SeedReaderPanel`）**：点击卡片正文或展开按钮，锚定弹出完整本体阅读卡，展示「适用场景 / 盲区信号」「认知指令本体」「快捷键呼出示例」及当前动态概率与入选理由；自动识别指令中的 `[...]` / `【...】` / `{{...}}` 占位变量与 `{{input}}` 槽位支持**就地填空**，支持一键复制、一键直达 **ChatGPT / Claude / Gemini** 及 `Esc` 快速收起。
2. **概率调度引擎（`brain-server`: `seed_store.py` + `/seed/*`）**：
   - 每张候选卡实时计算动态抽样权重：
     $$W(i) = W_{\text{tier}} \times M_{\text{recall}} \times M_{\text{behavior}} \times M_{\text{affinity}} \times M_{\text{freshness}}$$
   - **层级、梯队与重点聚焦轮播（Pinned Focus）**：L4 内按 `Tier S (4.0) > Tier A (2.0) > Tier B (1.0)` 加权；在默认 **`smart_mix`（全域智能混播）** 模式下，L3 休眠库自动归一化保持 **~15% 探索总概率**；支持通过 `PUT /seed/pinned` 将单卡或专题包钉入「重点聚焦」队列（`smart_mix` 下默认分配 **`30%`** 聚焦配额并启用自适应冷却，设为 `0%` 时退回自然权重）；菜单栏支持 **5 种模式**随时切换：`全域智能混播 / 仅看重点聚焦 / 仅 L4 在架 / 仅 L3 休眠探索 / 仅 S 级核心`。
   - **反回音室与行为反馈**：读取 `usage.db` 近 30 天主动召回次数，越常主动召回的卡被动曝光概率越低（把桌面注意力留给盲区）；手动快跳（45 秒内点下一张）轻度降权，展开/复制清除降权并温和提升同分类亲和度；最近 25 张进入防重复冷却。
   - **管理台「桌面陪伴节奏」控制面**：实时调整 S/A/B 权重、聚焦占比、L3 探索占比、召回衰减与快跳倍率，预览 Top 30 概率分布，管理重点聚焦队列及逐条恢复/重置已屏蔽或暂缓种子（状态独立存储于 `brain-server/data/seed_state.json`，不入库 git）。
3. **CLE v0.7 全量 82 项入库与常青通用底座**：
   - 完成认知语言工程（CLE v0.7）M1（9）+ M2（35）+ M3（9）+ M4（29）共 **82 张**接口的手工精炼与去术语化入库（`execution.content` 不含任何内部术语，外部 LLM 零门槛可执行）。
   - 将 CLE `结构核心` **27 项**、[glint-core](../brain-server/data/cards.yaml) 通用思考底座及跨领域高频卡纳入 [shelf_policy.yaml](config/shelf_policy.yaml) 的 `evergreen_ids`（叠加个人自建常青与本机 `meta-vault/shelf_policy.yaml` 6 项二阶元卡片，当前实装 **89 项 Tier S 常青**），其余 55 项 CLE 与 16 项 `atlas-methods` 共 71 张留在 L3 休眠库，通过 L3 破冰与认知种子持续触达。

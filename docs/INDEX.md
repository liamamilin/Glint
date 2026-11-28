# Glint（微光）文档与唯一权威来源（SSOT）索引

> 本文件定义 Glint 仓库各类信息的**唯一权威来源（Single Source of Truth）**与归档映射，避免跨文档重复与数值冲突。

## 1. 权威来源矩阵（SSOT Map）

| 信息域 | 唯一权威来源（SSOT） | 说明与边界 |
| :--- | :--- | :--- |
| **项目入口与快速启动** | [README.md](../README.md) | 架构概览、环境依赖、`18765` 服务与 macOS 客户端启动命令 |
| **系统架构与协议规范** | [recognition-library/design.md](../recognition-library/design.md) | 定位边界、7 大元操作本体论、L0–L4 供应链、Stage 1/2A/2B 路由架构、L3 穿透与双飞轮设计 |
| **里程碑与活跃待办** | [recognition-library/roadmap.md](../recognition-library/roadmap.md) | 当前运行基线快照、M1–M5 完成状态及剩余 TODO |
| **本体论与策略配置** | [recognition-library/config/](../recognition-library/config) | `ontology.yaml`（词表 SSOT）、`cleaning_rules.yaml`、`shelf_policy.yaml`、`routing_policy.yaml`、`taxonomy.yaml`、`sources.yaml` |
| **本地运行与部署配置** | [brain-server/config.py](../brain-server/config.py)<br>[brain-server/com.glint.brain.plist](../brain-server/com.glint.brain.plist)<br>[macos-app/scripts/build_and_run.sh](../macos-app/scripts/build_and_run.sh) | 端口（`18765`）、模型名称、launchd 常驻配置、Swift 编译签名与部署脚本 |
| **评测基准与套件** | [recognition-library/eval/golden_cases.yaml](../recognition-library/eval/golden_cases.yaml)<br>[recognition-library/eval/run_suite.py](../recognition-library/eval/run_suite.py)<br>[brain-server/scripts/benchmark_rerank_sweep.py](../brain-server/scripts/benchmark_rerank_sweep.py) | Golden Eval（34 题）、扩展压测集（`everyday_simulated` 72、`vague_ambiguous_stress` 36）及离线精排延迟/准确率权衡脚本 |
| **私有二阶元认知资产** | `meta-vault/README.md`（独立私有仓，受 `.gitignore` 忽略） | 二阶算子源码、私有 L2/L3 透镜（6 张）、`shelf_policy.yaml`（含私有专题包）、`overrides.yaml`、`quarantine/`、`eval/meta_cards.yaml`（40 题） |

## 2. 已归档文档（只读历史快照）

| 归档路径 | 原路径 | 归档日期 | 失效原因 | 替代权威文档 |
| :--- | :--- | :--- | :--- | :--- |
| [archive/glint_v0.1_notes.md](archive/glint_v0.1_notes.md) | `glint.md` | 2026-11-27 | v0.1 初版构想：端口 `8765`→`18765`、默认热键 `Option+Space`→`双击右 Option`、单阶段 `qwen2.5:3b`→Hybrid+MPS `bge-reranker-v2-m3`、缺失 `recognition-library/` 与五层供应链，且引用已废弃 `training/*.py/*.sh` 路径 | [design.md](../recognition-library/design.md)<br>[roadmap.md](../recognition-library/roadmap.md)<br>[README.md](../README.md) |

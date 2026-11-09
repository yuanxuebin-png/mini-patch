# MiniPatch —— 代码修改智能体

> 「Mini」指体量：三千行以内跑通完整闭环，每一层都能读完；
> 「Patch」指定位：**只做外科式的最小改动**，术前有快照、按风险分级授权、失手能回滚。
> 这和「整段重写」的代码生成工具是两种不同的思路。

一个能「读代码 → 定位问题 → 修改文件 → 跑测试 → 根据报错继续修」的代码修改 Agent。
核心关注点不是「能不能改」，而是**改错了怎么办、什么不能改、怎么证明改对了**。

LangChain 1.x（`create_agent` + 中间件）+ LangGraph 1.x（`StateGraph` 条件边），
模型 deepseek-chat，Python 3.13。

## 实测结果(全部真跑, 数字可复现)

| 指标 | 数字 | 出处 |
|---|---|---|
| 正样本修复率 | **14/14**(测试被动过 0 次) | `docs/eval_result.md` |
| 负样本守住率 | **3/6**(误改 3 次, 根因已定位) | `docs/eval_analysis.md` |
| 正样本平均输入 token | ≈9777 / 任务 | token 账本实测 |
| 隔离式 vs 单 Agent | 总输入多 28%, 但父上下文省 19% | `docs/ab_result.md` |

负样本 3 次误改指向同一根因: Agent 将「环境缺依赖」误判为代码问题并直接修改。
分析见 `docs/eval_analysis.md`, 对应的 v2 改进方向(报错分类前置)已列入 Roadmap。
负样本数据如实保留, 不做美化处理。

## 设计原则

1. **权限分级**: 只读工具免审批; 写文件必须先产出 diff 并落回滚快照;
   命令执行走白名单(仅 python/pytest/git 只读子命令, 禁 shell 拼接, 超时强杀)。
2. **上下文隔离**: 代码侦察由只读子 Agent 完成, 主 Agent 只接收
   `ScoutReport(目标文件, 行号区间, 理由)` 结构化结论, 不接收原始代码全文。
3. **可验证**: 20 条评测任务(14 正 6 负), 每条一个独立临时目录零污染;
   正样本判定 = 测试过 **且** 测试文件字节级未动(防"改测试凑通过")。

## 执行管线

```
scout(只读侦察, 结构化交卷)
        │  ScoutReport
        ▼
  fix(最小修改, 写前快照) ──失败──▶ 回灌报错重试
        │
        ▼
  verify(跑测试) ──过──▶ status=fixed
        │
        └─连续两次相同输出──▶ 判原地打转: 回滚快照, status=gave_up
```

token 记账零侵入: 直接从 `AIMessage.usage_metadata` 按阶段累加, 不改执行路径。

## 目录结构

```
mini-patch/
├── patch_agent/
│   ├── sandbox.py       # 工作区约束 + 路径越界防护
│   ├── tools_read.py    # 只读工具: list_dir / grep / read_snippet
│   ├── snapshot.py      # 文件快照与回滚(新建文件撤销即删除)
│   ├── tools_write.py   # 写入工具(apply_edit 唯一命中才改)
│   ├── commands.py      # 命令白名单执行器(无 shell, 超时强杀)
│   ├── scout.py         # 侦察子 Agent(隔离式)
│   ├── graph.py         # 执行循环 + 演示仓库
│   ├── pipeline.py      # StateGraph 编排(scout→fix→verify 条件边)
│   ├── chat.py          # 交互式对话窗口(动手/建议双模式, rich 彩色输出)
│   ├── knowledge.py     # 知识库联动工具(HTTP 调 pocket-rag, 失败明确降级)
│   ├── ledger.py        # token 账本(按阶段记账)
│   ├── ab_compare.py    # A/B 隔离收益对照实验
│   └── evals/           # 20 条评测集 + 自动判定执行器
├── docs/
│   ├── isolation.md     # 隔离式 Agent 设计知识
│   ├── ab_result.md     # A/B 实测报告
│   ├── eval_result.md   # 全量评测留档(自动生成)
│   └── eval_analysis.md # 负样本误改根因分析
└── sandbox_repo/        # 被修改的目标示例仓库(6 模块)
```

## 快速开始

```bash
# 依赖: python 3.13+, langchain 1.x, langgraph 1.x
cp .env.example .env        # 填入 OPENAI_API_KEY / OPENAI_BASE_URL

# 交互式对话窗口(动手/建议双模式): 以自然语言下达指令, 由 Agent 修改文件或给出建议
python -m patch_agent.chat [工作区目录]

# (可选)启动 pocket-rag 知识库后, 对话窗口获得私有规范检索能力:
# Agent 在涉及项目规范/技术约定的问题时, 通过 query_knowledge 工具检索知识库
cd ../pocket-rag && python -m pocket_rag.api   # 默认 127.0.0.1:8765

# 端到端演示: 注入 bug -> 侦察 -> 修复 -> 测试通过(无人值守)
python -m patch_agent.graph

# 全量评测(20 条, 约 3 分钟)
python -m patch_agent.evals.runner

# A/B 隔离收益对照
python -m patch_agent.ab_compare
```

## Roadmap

- [x] 项目骨架与隔离式设计文档
- [x] 工作区沙箱与路径防护
- [x] 只读工具层(输出上限 + 错误转模型消息)
- [x] 快照 / diff / 回滚
- [x] 写入工具与命令白名单
- [x] 侦察子 Agent 与两阶段编排
- [x] StateGraph 管线(verify 条件边 + 失败回灌 + 无进展回滚)
- [x] token 账本
- [x] A/B 隔离收益对照实验
- [x] 20 条评测集与自动判定(正 14/14, 负 3/6 + 根因分析)
- [ ] v2: 报错分类前置(缺依赖/环境问题直接上报不进修复循环)
- [ ] v2: checkpointer 断点续跑

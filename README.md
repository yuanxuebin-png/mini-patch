# MiniPatch

在真实的小仓库里自己跑完一件事的编码 Agent：读代码 → 找问题 → 改既有代码或从零写新文件 → 跑测试 → 看报错接着改。

名字是两个意思拼的：Mini 指体量，两千多行 Python，每一层都能读完；Patch 指它的工作方式，改既有代码优先动最小的那处，写新文件才整份落盘。两条路都有快照兜底，失手能回滚。

代码本身不难。花时间的是另一半问题：改错了怎么办、哪些东西不许碰、怎么判断它是真修好了还是把测试改通了。下面记的都是这部分。

LangChain 1.x（`create_agent` + 中间件）、LangGraph 1.x（`StateGraph` 条件边），模型 deepseek-chat，Python 3.13。

## 跑出来的数据

| 指标 | 结果 | 明细 |
|---|---|---|
| 正样本通过率 | 14/14，测试文件被动过 0 次 | `docs/eval_result.md` |
| 负样本守住率 | 3/6，误改 3 次 | `docs/eval_analysis.md` |
| 正样本平均输入 token | 约 9777 / 任务 | 账本实测 |
| 隔离式 vs 单 Agent | 总输入多 28%，父上下文省 19% | `docs/ab_result.md` |

负样本那 3 次误改是同一个原因：Agent 把「环境缺依赖」当成代码 bug 直接动手改了。根因写在 `docs/eval_analysis.md` 里，改法是让它在修复前先分类报错（v2 待办）。

这几条我没有挑好的报。隔离式那个 A/B 我本来是奔着省 token 去做的，结果总输入反而多 28%，虽然父上下文降了 19%。这组数据就留在仓库里，没删。

## 几个设计上的取舍

**权限不写在提示词里，写在工具清单里。** 建议模式在构造时就不注入写工具，模型手上根本没有这个能力，不需要靠「请不要修改文件」这种话去约束。只读工具免审批；要写文件必须先出 diff、先落快照；命令走白名单，只放 python / pytest / git 的只读子命令，不许 shell 拼接，超时强杀。

**侦察交给子 Agent，主线只看结论。** 找代码这一步让只读子 Agent 去做，它交回来的是 `ScoutReport(目标文件, 行号区间, 理由)`，不是原始代码全文。这样主 Agent 的上下文长度跟仓库大小脱钩，不会被一堆无关代码泡糊。报告里保留行号，需要看细节时还能按行号精确回读。

**「修好了」得有判据。** 20 条评测任务，14 正 6 负，每条跑在自己的临时目录里，互不污染。正样本通过的条件是测试过了，并且测试文件字节级没变过，专门防「把测试改通就算修好」这条路。负样本要求全程零写入。

## 执行流程

```
scout（只读侦察，交结构化报告）
        │  ScoutReport
        ▼
  fix（最小改动，写前快照） ──失败──▶ 回灌报错重试
        │
        ▼
  verify（跑测试） ──过──▶ status=fixed
        │
        └─连续两次相同输出──▶ 判原地打转：回滚快照，status=gave_up
```

verify 这条条件边是 `pipeline.py` 里最要紧的地方。除了按退出码分 pass / retry，还加了一个无进展判断：连着两次测试输出一模一样，就不再重试，回滚给个 `gave_up`。真跑的时候见过它卡在同一行反复改的样子，光靠重试次数拦不住。

token 记账是从 `AIMessage.usage_metadata` 按阶段累加的，没在业务代码里插桩。

## 目录

```
mini-patch/
├── patch_agent/
│   ├── sandbox.py       # 工作区约束 + 路径越界防护
│   ├── tools_read.py    # 只读工具：list_dir / grep / read_snippet
│   ├── snapshot.py      # 快照与回滚（新建文件撤销即删除）
│   ├── tools_write.py   # 写工具：apply_edit 唯一命中才改；write_file 新建整份文件
│   ├── commands.py      # 命令白名单执行器（无 shell，超时强杀）
│   ├── scout.py         # 侦察子 Agent
│   ├── graph.py         # 执行循环 + 演示仓库
│   ├── pipeline.py      # StateGraph 编排（scout→fix→verify 条件边）
│   ├── chat.py          # 交互式对话窗口（动手/建议双模式，rich 输出）
│   ├── knowledge.py     # 知识库工具（HTTP 调 pocket-rag，连不上会明说）
│   ├── ledger.py        # token 账本
│   ├── ab_compare.py    # 隔离收益对照实验
│   └── evals/           # 20 条评测集 + 自动判定
├── docs/
│   ├── isolation.md     # 隔离式 Agent 的笔记
│   ├── ab_result.md     # A/B 实测
│   ├── eval_result.md   # 全量评测留档（自动生成）
│   └── eval_analysis.md # 负样本误改根因
└── sandbox_repo/        # 被改的目标示例仓库（6 模块）
```

## 怎么跑

```bash
# 依赖 python 3.13+、langchain 1.x、langgraph 1.x
cp .env.example .env        # 填 OPENAI_API_KEY / OPENAI_BASE_URL

# 对话窗口，用自然语言下指令，它自己改文件或给建议
python -m patch_agent.chat [工作区目录]

# 端到端演示：注 bug → 侦察 → 修复 → 测试通过
python -m patch_agent.graph

# 全量评测，20 条大约 3 分钟
python -m patch_agent.evals.runner

# A/B 对照
python -m patch_agent.ab_compare
```

对话窗口也可以接 pocket-rag 的知识库：先把 pocket-rag 起起来（`cd ../pocket-rag && python -m pocket_rag.api`，默认 127.0.0.1:8765），之后问项目规范类的问题，它会去查而不是瞎编。连不上会直接报错并告诉你该怎么起，不会假装查到了。

## 之后想加的

项目到这里算完整跑通了一个阶段：从侦察到修复到验证的闭环、安全机制、评测都齐了。下面这些是往下走的方向，暂时没排期：

- 报错分类前置：缺依赖、环境问题这类不该进修复循环的，先识别出来直接上报
- checkpointer 断点续跑

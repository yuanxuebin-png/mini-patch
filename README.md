# patch-agent —— 代码修改智能体

一个能「读代码 → 定位问题 → 修改文件 → 跑测试 → 根据报错继续修」的代码修改 Agent。
核心关注点不是「能不能改」，而是**改错了怎么办、什么不能改、怎么证明改对了**。

## 设计原则

1. **权限分级**：只读工具免审批；写文件必须先产出 diff 并回滚快照；命令执行走白名单。
2. **上下文隔离**：代码侦察由只读的子 Agent 完成，主 Agent 上下文只接收结构化结论。
3. **可验证**：每条能力配评测任务，负样本（不该改的、改不动的）误改率必须为 0。

## 目录结构

```
patch-agent/
├── patch_agent/        # 包源码
│   ├── sandbox.py      # 工作区约束 + 路径越界防护
│   ├── tools_read.py   # 只读工具: list_dir / grep / read_snippet
│   ├── snapshot.py     # 文件快照与回滚
│   ├── tools_write.py  # 写入工具(带 diff 预览)
│   ├── commands.py     # 命令白名单执行器
│   ├── graph.py        # LangGraph 执行循环
│   └── scout.py        # 侦察子 Agent(隔离式)
├── docs/
│   └── isolation.md    # 隔离式 Agent 设计知识
├── tests/              # 评测任务集(含负样本)
└── sandbox_repo/       # 被修改的目标示例仓库
```

## 快速开始

```bash
# 依赖: python 3.13+, langchain 1.x, langgraph 1.x
cp .env.example .env        # 填入 OPENAI_API_KEY / OPENAI_BASE_URL
python -m patch_agent.demo  # 演示: 定位并修复一个注入的 bug
```

## Roadmap

- [x] 项目骨架
- [x] 工作区沙箱与路径防护
- [x] 只读工具层
- [x] 快照 / diff / 回滚
- [x] 写入工具与命令白名单
- [x] LangGraph 执行循环(v1 两阶段编排)
- [x] 侦察子 Agent(隔离式)
- [ ] 评测任务集与 token 账本
- [ ] verify 节点与条件边(测试不过自动回修, 超限回滚)
- [ ] 断点续跑(checkpointer)

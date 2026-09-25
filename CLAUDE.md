# 协作约定

给参与本项目开发的 AI 助手看的规则。开始任何改动前先读 `docs/DESIGN.md` 和 `docs/ROADMAP.md`。

## 文档

| 文件 | 内容 |
|---|---|
| `docs/DESIGN.md` | 架构、模块边界、核心接口——唯一事实来源 |
| `docs/EVAL.md` | 评测协议、比对口径、实验登记表 |
| `docs/ROADMAP.md` | 阶段计划、完成标准、待决事项 |
| `docs/DECISIONS.md` | 关键决策的背景和代价 |

改动涉及架构或口径时，**先改文档再改代码**。

## 必须先问用户的事

- 任何会产生 API 花费的操作：跑评测（250 题约 15 credit）、批量调用、探路测试
- 换模型，或给某个角色配置 `cn:deepseek-v4.1-flash` 以外的模型
- 修改 `eval/metrics.py` 的比对口径
- 修改 `agent/baseline.py`
- 引入新的第三方依赖
- 删除 `eval/results/` 下的任何文件
- git 提交、推送

## 不允许

- `llm/` 以外的代码 import `openai` 或 `anthropic`
- 在代码里写死模型名，模型只通过 `Router.for_role()` 获取
- 引入 LangChain、LlamaIndex 等 agent 框架
- 删掉 `sandbox/` 的任何一道防线
- 引用 30 题探路跑或 mini 数据集的准确率

## 代码风格

- 注释、docstring、给用户看的报错文案用中文；标识符用英文
- 注释写"为什么"，不写"做了什么"
- 失败返回结果对象，不抛异常到评测主循环
- "错了会静默产出假数字"的逻辑（比对、截断、计价），先写测试

## 常用命令

```bash
uv sync --group dev
uv run --group dev pytest tests/ -q        # 127 个测试；PG 测试库没起时跳过其中 19 个
docker compose -f tests/pg/docker-compose.yml up -d --wait   # PG 集成测试库（tmpfs，不落盘）
docker compose -f tests/pg/docker-compose.yml down
uv run python smoke.py                     # 端到端验证网关（极少量花费）

# 评测（会花钱，先问）
uv run python -m eval.runner --dataset eval/datasets/bird/dev_20240627 \
    --limit 250 --seed 0 --workers 2 --label <实验名>

# 管线自检（6 题，几乎不花钱）
uv run python -m eval.runner --dataset eval/datasets/mini --limit 6 --label mini-check
```

## 环境注意

- Windows 控制台中文乱码：命令前加 `PYTHONIOENCODING=utf-8`
- uv 缓存在 `D:\xs\uv-cache`（`UV_CACHE_DIR`），C 盘空间紧张，不要往 C 盘写大文件
- 网关并发 4 会返回 503，评测默认并发 2
- `.env` 含 key，不进 git

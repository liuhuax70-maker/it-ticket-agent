# 贡献指南

感谢参与！本文说明如何报告问题、提交代码和参与讨论。

## 行为准则

讨论中对人友善，尊重不同意见。提技术意见时对事不对人。

> 本仓库尚未提供 `CODE_OF_CONDUCT.md`；如需正式的贡献者行为准则，请先补充后再把上文链接化。

## 贡献方式

- 报告 bug
- 提功能建议
- 改进文档
- 提交代码
- 帮忙 review

## 报告 bug

提交 issue 时请包含：

- 版本 / commit
- 操作系统和运行环境（Python 版本、是否 Docker、本地还是容器）
- 复现步骤
- 期望结果和实际结果
- 日志或截图

> 本仓库尚未配置 issue 模板；如需结构化收集上述信息，可在 `.github/ISSUE_TEMPLATE/` 下补充。

**安全漏洞不要开公开 issue**，请私下联系维护者（见「沟通」一节）。

## 功能建议

先开 issue 讨论场景和动机，不要直接写大 PR。已有实现方向时，说明它与现有设计的取舍关系——本项目多数关键取舍有 `docs/adr/` 记录，新增前先看看是否已有相关决策。

## 开发环境

```bash
git clone https://github.com/liuhuax70-maker/permission-aware-rag.git
cd permission-aware-rag
make install          # 等价 pip install -e ".[dev]"
make infra-up         # 启动核心依赖（Postgres/Redis/OpenSearch/Milvus）
make test             # 全量测试
```

要求 Python 3.11+ 与 Docker。完整的环境变量见 `.env.example`，复制为 `.env` 后按需修改。

## 分支与提交

- 从 `main` 切分支：`feat/xxx`、`fix/xxx`、`docs/xxx`
- 提交信息用 Conventional Commits：`feat(scope): ...`、`fix(scope): ...`、`docs(scope): ...`
- 一次提交只做一件事

## 代码风格

提交前跑：

```bash
make fmt              # 统一格式
make lint             # 静态检查
make type-check       # 类型检查
```

- 不要混入无关格式化（`make fmt-check` 是 CI 门禁，无关 diff 会让 review 变难）
- 行宽 100，`ruff` 规则集 `E/F/I/UP/B`（见 `pyproject.toml`）

### 注释与 docstring

- 中文三引号 `"""`，写在模块 / 类 / 公共函数首行
- 写「为什么」与「契约 / 语义」，不翻译代码；行内注释只解释意图与坑
- 写注释前先想：更清晰的命名或结构能否替代它
- 涉及鉴权 / 越权 / 限流 / 审计的代码，注释里写明失败走向：
  - **fail-closed（失败即拒绝）**：缺身份头返回 403、OPA 默认拒绝、越权零容忍
  - **fail-open（失败即放行）**：Redis 不可用时限流放行、审计落库失败丢弃但 stdout 仍在。放行类降级要说明为何放行比拒绝更安全

## 测试

- 新功能加测试
- 修 bug 加回归测试
- 提交前跑：

```bash
make test             # 全量（按服务分进程）
make test-packages    # 只跑共享库
```

测试**必须按服务分进程**：每个服务都有顶层 `app` 包，同一个 pytest 会话里 `app` 只会绑定到最先导入的那个服务，所以不要试图在同一进程里跑多个服务的测试。CI 门禁为 `lint`、`type-check`、`fmt-check`、`corpus-check`。

## PR 流程

1. 切分支
2. 修改代码并补测试
3. 跑 `make fmt-check lint type-check test`
4. 提 PR，描述里覆盖下方清单
5. 等 review，按反馈修改

PR 检查清单：

- [ ] 测试通过
- [ ] 加了测试
- [ ] 更新了文档
- [ ] 没有无关改动
- [ ] 涉及鉴权 / 权限 / 限流 / 审计的改动，注释写明了 fail-closed / fail-open 取舍
- [ ] 行为变更同步更新了 `configs/` 下的配置版本或 `docs/` 文档

> 本仓库尚未配置 PR 模板；如需自动化，可在 `.github/PULL_REQUEST_TEMPLATE.md` 下补充。

## Review 与合并

维护者会尽量在数日内回复。小 PR 更容易合并。合并权在维护者。

改动涉及权限语义（ACL 过滤下推、缓存键身份维度、拒答行为、生命周期过滤）时，请说明你验证过隔离矩阵：`python scripts/verify_permissions.py`。这类的回归往往在功能测试里看不出来。

## 沟通

- Issue：https://github.com/liuhuax70-maker/permission-aware-rag/issues
- 邮件 / 内部沟通渠道：待补充

## 许可证

贡献代码默认按 [MIT](LICENSE) 许可证授权。
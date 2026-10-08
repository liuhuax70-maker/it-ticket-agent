# 贡献指南

感谢参与！本文说明如何报告问题、提交代码和参与讨论。

## 行为准则

参与即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 贡献方式

- 报告 bug
- 提功能建议
- 改进文档
- 提交代码
- 帮忙 review

## 报告 bug

请使用 [bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.yml)，其中会引导你填完版本 / commit、运行环境、复现步骤、期望与实际结果、日志。

**安全漏洞不要开公开 issue**，请通过 [GitHub Security Advisory](https://github.com/liuhuax70-maker/permission-aware-rag/security/advisories/new) 私下报告。

## 功能建议

先开 [功能建议 issue](.github/ISSUE_TEMPLATE/feature_request.yml) 讨论场景与动机，不要直接写大 PR。已有实现方向时，说明它与现有设计的取舍关系——本项目多数关键取舍记录在 `docs/adr/`，新增前先看看是否已有相关决策。

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

一句话原则：**注释补充代码表达不了的信息，解释「为什么」，不复述「做什么」**。代码优先自解释——好命名、清晰结构、小函数比注释更重要，注释是补充而不是补丁。

- 只写非显而易见的信息：业务规则、边界条件、历史坑、性能权衡、外部系统怪癖、临时方案
- 中文三引号 `"""`，写在模块 / 类 / 公共函数首行；行内注释写在语句后面，别隔太远
- 改代码必须改注释——**过时注释比没注释更危险**
- 不写：复述函数名与代码、每步日志式注释、变更日志、作者与日期、注释掉的代码、情绪化吐槽
- 面向未来维护者：假设他懂语言，但不懂这段业务与历史

公共 API（模块、类、对外函数）按 PEP 257 写文档注释，说清：做什么、参数含义、返回值、可能抛出的异常、副作用。内部私有函数不强求。

必须写注释的地方：复杂算法与非直观逻辑、业务 / 合规 / 计费规则、边界与特殊 case、外部系统兼容与协议怪癖、性能优化的原因、临时方案与 workaround、安全相关代码。

`TODO` / `FIXME` / `HACK` 带上下文，便于检索：

```python
# TODO(#123): 等 v2 API 上线后删除兼容逻辑
# FIXME: 并发下可能重复扣款，需要加锁
# HACK: 第三方库返回格式不一致，这里手动修正
```

不写无内容的 TODO（`# TODO: 优化`）与凑数的空话 docstring。

#### 安全不变量

涉及鉴权 / 越权 / 限流 / 审计的代码，注释里写明失败走向：

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
4. 提 PR（自动套用 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md)，含检查清单）
5. 等 review，按反馈修改

改动涉及权限语义（ACL 过滤下推、缓存键身份维度、拒答行为、生命周期过滤）时，PR 模板要求你说明验证过隔离矩阵：`python scripts/verify_permissions.py`。这类回归往往在功能测试里看不出来。

## Review 与合并

维护者会尽量在数日内回复。小 PR 更容易合并。合并权在维护者。

## 沟通

- Issue 与 PR：https://github.com/liuhuax70-maker/permission-aware-rag/issues
- 安全漏洞：走 [Security Advisory](https://github.com/liuhuax70-maker/permission-aware-rag/security/advisories/new)，不要公开提交

## 许可证

贡献代码默认按 [MIT](LICENSE) 许可证授权。
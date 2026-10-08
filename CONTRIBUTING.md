# 贡献指南

欢迎提交 Issue 与 Pull Request。本文件只规定**代码风格与注释约定**——
运行环境、构建、测试命令以各服务 `README` 与根目录 `Makefile` 为准。

本项目的注释规范参考成熟开源实践制定：

- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)（PEP 257 体系：docstring 描述契约而非实现）
- [scikit-learn](https://scikit-learn.org/dev/developers/contributing.html) 采用的 [numpydoc](https://numpydoc.readthedocs.io/) 章节结构
- FastAPI / requests 等高分仓库的共识：**README 只讲「是什么 / 怎么用」，规范细节不放 README**

---

## 1. 何时必须写 docstring

- 所有**类**都必须有 docstring；
- **公共 API**、含**非显然逻辑**、或有**一定规模**的函数 / 方法必须有；
- 纯数据类、自解释的 getter 不强制；
- 重写父类方法且未细化契约时可省略，否则应有。

## 2. 形式

- 一律**中文**、三双引号 `"""`，作为模块 / 类 / 函数 / 方法的**第一条语句**；
- 首行是一句话摘要（≤80 字符，以句号结尾）；多行主体与首行引号**同缩进**，摘要后**空一行**；
- 行内注释用 `#`，只解释**意图与坑**，绝不复述代码在做什么（"Assume the reader knows Python better than you."）。

## 3. 写什么：为什么，而非是什么

docstring 应让使用者**不读源码就能正确调用**——描述调用契约与语义（入参、返回、副作用、异常），而非实现细节。与调用无关的实现细节写成代码旁注释。

本项目特有要求：**涉及鉴权 / 授权 / 越权 / 限流 / 审计的代码，必须显式标注失败走向**：

- **fail-closed（失败即拒绝）**：缺身份头即 403、OPA 默认拒绝、越权泄露零容忍；
- **fail-open（失败即放行）**：Redis 不可用时限流放行、审计落库失败丢弃但 stdout 仍在、Keycloak 不可用时部分旁路。放行类降级**必须写明「为什么放行更安全」**，避免把 fail-open 当成偷懒。

任何「为什么不走更直白的写法」的非显然决策都要留证据（如 Milvus 加字段须重建集合、多个同名 `app` 包导致测试分进程跑）。

## 4. 结构（参考 numpydoc）

函数 / 类 docstring 按需使用以下章节，参数与返回标注类型：

```
摘要
（空行）
扩展说明（可选）
参数 (Parameters)：名称、类型、含义
返回 (Returns) / 产出 (Yields)
异常 (Raises)：仅列非显然或高概率的
注意 (Notes)：设计决策、安全不变量、坑位
示例 (Examples)：可选，鼓励 doctest 风格
```

## 5. 反例

- ❌ `x = x + 1  # 加一`、`"""处理请求"""` —— 零信息，不如不写；
- ✅ `"""缺时间 / 无法解析一律视为不过期（宁可留、不要误删）"""` —— 把 fail-safe 取向写进契约；
- ✅ `"""两种 MRR 不是同一量，标签必须写清，否则会被当成重复指标"""` —— 点出会误导人的同名量。

本仓库的验证脚本（`scripts/verify_*.py`、`scripts/check_*.py`）是上述风格的范本，新增代码请对齐。

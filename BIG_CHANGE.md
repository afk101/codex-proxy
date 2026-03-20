# 重大变更记录：支持 Codex freeform 工具类型（apply_patch）

## 变更日期

2026-03-20

## 问题背景

在使用 Codex CLI（v0.116.0）通过 codex-proxy 连接上游兼容模型时，频繁遇到运行时警告：

> "apply_patch was requested via exec_command. Use the apply_patch tool instead of exec_command."

该警告在每次文件编辑操作时都会稳定复现，严重影响使用体验。

## 根因分析

### Codex 中 apply_patch 的两条工作路径

Codex 的 `apply_patch` 文件编辑能力有两种调用方式：

| 路径 | 触发条件 | 工作方式 | 是否警告 |
|------|----------|----------|:--------:|
| 默认拦截模式 | `apply_patch` 未注册为独立工具 | 模型通过 `exec_command` 的 `cmd` 参数发送 `apply_patch <<'EOF'...` heredoc，Codex runtime 在 shell 执行前拦截处理 | ✅ 每次都报 |
| 独立工具模式 | `apply_patch` 注册为独立 function tool | 模型直接调用 `apply_patch` 工具，Codex runtime 通过 `ApplyPatchHandler` 处理 | ❌ 不报 |

### 两层卡点叠加导致问题

问题由 **Codex 配置** 和 **codex-proxy 转换逻辑** 两层卡点共同导致：

#### 卡点 1：Codex 侧 — apply_patch 默认不注册为独立工具

使用 custom provider 时，Codex 内建的模型信息（`model_info`）没有为自定义模型预设 `apply_patch_tool_type`。必须手动启用 feature flag 才能让 `apply_patch` 作为独立工具注册到 Responses API 请求的 tools 数组中。

正确配置方式（在 `~/.codex/config.toml` 中）：

```toml
[features]
apply_patch_freeform = true
```

> 注意：`include_apply_patch_tool = true`（顶层）和 `experimental_use_freeform_apply_patch = true`（顶层）均无效或已废弃。`include_apply_patch_tool` 是 `profiles.<name>.features` 下的子键，放在顶层会被忽略；`experimental_use_freeform_apply_patch` 已被标记为 deprecated，Codex 会提示改用 `[features].apply_patch_freeform`。

#### 卡点 2：codex-proxy 侧 — freeform 工具被无条件丢弃

即使 Codex 配置正确，将 `apply_patch` 注册为了独立的 freeform 工具并发送到 Responses API 请求中，codex-proxy 原来的 `_convert_tools()` 函数会**无条件跳过所有非 `function` 类型的工具**：

```python
# 旧代码（src/conversion/request_converter.py）
if tool_type != Constants.TOOL_FUNCTION:
    logger.warning(f"跳过非 function 类型的工具: type={tool_type}, name={tool_name}")
    continue  # apply_patch (type=freeform) 在这里被直接丢弃
```

Codex 的 `apply_patch` 工具类型是 `freeform`（不是标准的 `function`），所以**被代理层直接丢掉了**。上游模型根本看不到这个工具，只能退回到 `exec_command` 间接调用 `apply_patch`，每次都触发 Codex runtime 的警告。

### 因果链总结

```
Codex config 未启用 apply_patch_freeform
  → apply_patch 未注册为独立工具
    → 模型只能通过 exec_command 间接调用
      → Codex runtime 拦截并警告

Codex config 启用了，但 codex-proxy 丢弃了 freeform 工具
  → 上游模型看不到 apply_patch 工具
    → 模型仍然只能通过 exec_command 间接调用
      → Codex runtime 拦截并警告
```

## 变更内容

### 1. Codex 配置变更（`~/.codex/config.toml`）

```toml
# 启用独立 apply_patch 工具
[features]
apply_patch_freeform = true

# 可选：抑制开发中功能的警告提示
suppress_unstable_features_warning = true
```

### 2. codex-proxy 代码变更

#### 文件：`src/conversion/request_converter.py`

**改动 1：`_convert_tools()` 函数重写**

- **旧逻辑**：无条件跳过所有 `type != "function"` 的工具
- **新逻辑**：
  - 只跳过明确不兼容的工具类型（`computer_use_preview`、`code_interpreter`、`file_search`、`web_search_preview`）
  - 标准 `function` 类型工具：按原有逻辑正常转换
  - 非标准工具类型（`freeform`、`local_shell` 等）：**降级为 function 格式**传给上游
    - 尝试从 `input_schema` / `schema` / `parameters` 提取已有 schema
    - 如果没有 schema，兜底构造一个 `input` 字符串参数
    - 在工具描述中追加原始类型信息，帮助上游模型理解

**改动 2：新增 `_extract_freeform_tool_parameters()` 函数**

从非标准工具中按优先级提取或构造 parameters schema：
1. `input_schema` 字段（Codex freeform 工具使用）
2. `schema` 字段
3. `parameters` 字段
4. 兜底：构造 `{"type": "object", "properties": {"input": {"type": "string"}}}`

#### 文件：`src/api/endpoints.py`

新增两条 DEBUG 级别的请求摘要日志，用于调试请求转换过程：
- 原始 Responses 请求摘要
- 转换后 Chat Completions 请求摘要

#### 文件：`src/conversion/request_converter.py`（摘要函数）

新增三个调试摘要函数，提取关键信息而不暴露完整 prompt 内容：
- `summarize_responses_request()` — 原始请求摘要
- `summarize_converted_request()` — 转换后请求摘要
- `_summarize_message_content()` — 消息内容结构摘要

### 3. 转换效果对比

| | 旧行为 | 新行为 |
|---|---|---|
| `apply_patch` (freeform) | ❌ 被跳过，上游看不到 | ✅ 降级为 function 格式传给上游 |
| `exec_command` (function) | ✅ 正常转换 | ✅ 正常转换（不变） |
| `computer_use_preview` | ❌ 跳过 | ❌ 跳过（不变） |
| 工具总数（典型） | 10 个 | 11 个（多了 apply_patch） |

## 验证结果

修复前（3 次复现测试）：
- 每次通过 `exec_command` 包 `apply_patch` heredoc 编辑文件，Codex runtime 都会立即警告
- 工具列表中只有 10 个标准 function 工具，无 `apply_patch`
- 47 次历史工具调用全部是 `exec_command`

修复后：
- 模型直接调用独立的 `apply_patch` 工具，不再通过 `exec_command` 间接包装
- 工具列表中出现了 `apply_patch`（从 10 个增加到 11 个）
- "apply_patch was requested via exec_command" 警告消失

## 附加发现

### Codex apply_patch feature flag 配置踩坑记录

| 尝试 | 配置方式 | 结果 | 原因 |
|------|----------|------|------|
| ① | 顶层 `include_apply_patch_tool = true` | ❌ 无效 | 这是 `profiles.<name>.features` 下的子键，放顶层被 Codex 忽略 |
| ② | 顶层 `experimental_use_freeform_apply_patch = true` | ⚠️ 有 deprecated 警告 | 旧版兼容入口，已废弃 |
| ③ | `[features]` 节下 `apply_patch_freeform = true` | ✅ 正确生效 | Codex v0.116.0 的标准配置方式 |

### developer 消息角色映射

codex-proxy 将 Responses API 的 `developer` 角色消息映射为 Chat Completions 的 `system` 角色。这对于依赖 developer 消息语义优先级的 agent 工作流可能造成行为偏差，但本次未做修改，留待后续评估。

### reasoning items 静默丢弃

codex-proxy 在转换多轮对话时，会静默跳过 `type="reasoning"` 的 input items（Chat Completions API 不需要回传 reasoning）。这可能导致模型丢失之前的推理链上下文，但本次未做修改。

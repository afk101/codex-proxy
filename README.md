# codex-proxy

一个把 OpenAI `Responses API` 请求转换成 `Chat Completions API` 请求的代理服务，适合让依赖 Responses API 的客户端继续接入仅提供 OpenAI 兼容 Chat Completions 接口的上游。

当前项目提供两层能力：

- `FastAPI` 代理服务，暴露 `POST /v1/responses`
- 一个本地启动脚本 `cxc`，便于在 macOS 上长期运行

## 适用场景

当你的客户端默认发送 OpenAI Responses API 请求，但上游平台只兼容 Chat Completions API 时，可以在中间加这一层代理。

例如：

- Codex CLI 发 `Responses API`
- 本项目接收并转换请求
- 再转发到上游 OpenAI 兼容服务

## 功能特性

- 支持 `POST /v1/responses` 请求代理
- 支持普通响应和流式响应（SSE）
- 支持工具调用转换
- 支持 `reasoning.effort -> reasoning_effort` 映射
- 支持 `function_call_output` 转换
- 支持开发者消息映射为 system 消息
- 支持通过 `PROXY_API_KEY` 对接入方做二次鉴权
- 支持通过 `DEFAULT_MODEL` 统一替换请求中的 GPT 模型名
- 支持通过 `CUSTOM_HEADER_*` 注入上游请求头
- 支持客户端断开后取消上游请求
- 提供 `/health` 健康检查


## 环境要求

- Python `3.9+`
- 推荐使用 `uv`
- macOS 下如使用 `cxc`，默认依赖系统自带的 `caffeinate`

## 安装

### 1. 创建虚拟环境

```bash
uv venv
source .venv/bin/activate
```

### 2. 安装依赖

```bash
uv sync
```

如果你不使用 `uv`，也可以自行按 [pyproject.toml](/Users/qihoo/Documents/A_Own/codex-proxy/pyproject.toml) 中的依赖安装。

## 配置

服务启动时会直接读取环境变量。


## 启动方式

### 方式一：直接运行 Python 入口

```bash
source .venv/bin/activate
uv run start_proxy.py
```

### 方式二：运行 FastAPI 入口模块

```bash
source .venv/bin/activate
python src/main.py
```

### 方式三：使用 `cxc` 启动脚本

项目内已经提供 [start_codex_proxy.sh](/Users/qihoo/Documents/A_Own/codex-proxy/start_codex_proxy.sh)，并在 [package.json](/Users/qihoo/Documents/A_Own/codex-proxy/package.json) 中将其映射为 `cxc`。

常用示例：

```bash
./start_codex_proxy.sh
./start_codex_proxy.sh -d
./start_codex_proxy.sh PORT=9003 LOG_LEVEL=DEBUG
```

参数说明：

- `-h`, `--help`: 显示帮助
- `-d`, `--daemon`: 守护模式，服务退出后自动重启
- `VAR=VALUE`: 临时覆盖环境变量，仅对当前进程生效

这个脚本会：

- 自动切换到项目根目录
- 检查 `.venv` 是否存在
- 激活虚拟环境
- 通过 `caffeinate -is` 启动，减少 macOS 休眠导致的中断


启动后访问：

- `http://127.0.0.1:9002/health`
- `http://127.0.0.1:9002/v1/responses`

## 与客户端对接

客户端应把基地址指向本代理，而不是直接指向上游。

例如代理地址为：

```text
http://127.0.0.1:9002
```

则客户端请求目标应为：

```text
http://127.0.0.1:9002/v1/responses
```

如果启用了 `PROXY_API_KEY`，客户端还需要附带代理侧密钥。


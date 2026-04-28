改~/.codex/config.toml  

# 指定使用自定义 provider
model_provider = "custom"
model_reasoning_effort = "xhigh"
disable_response_storage = true
personality = "pragmatic"
model = "gpt-5.4"
# 抑制开发中功能的警告提示
suppress_unstable_features_warning = true
approvals_reviewer = "user"
# 启用独立 apply_patch 工具，避免模型通过 exec_command 间接调用 apply_patch
# 触发 "apply_patch was requested via exec_command" 警告

[features]
apply_patch_freeform = true

# 自定义 provider 配置
codex_hooks = true

[model_providers.custom]
name = "Zyuncs Proxy via Codex Proxy"
wire_api = "responses"
# base_url
base_url = "http://localhost:9002/v1"
# 读取环境变量，这里随便写一个在环境变量中已经存在的，实际上不用
env_key = "ZYUNCS_API_KEY"

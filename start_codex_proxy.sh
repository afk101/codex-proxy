#!/bin/bash

# 显示帮助信息
show_help() {
    echo "用法: cxc [选项] [VAR=VALUE ...]"
    echo ""
    echo "选项:"
    echo "  -h, --help     显示帮助信息"
    echo "  -d, --daemon   守护模式运行，服务退出后自动重启"
    echo ""
    echo "环境变量覆盖:"
    echo "  可以通过 VAR=VALUE 格式覆盖 .env 中的变量"
    echo "  覆盖仅对当前服务有效，不会修改 .env 文件"
    echo ""
    echo "示例:"
    echo "  cxc                              # 使用 .env 默认配置启动"
    echo "  cxc -d                           # 守护模式，服务退出后自动重启"
    echo "  cxc PORT=9003                    # 覆盖端口号"
    echo "  cxc OPENAI_BASE_URL=https://api.openai.com/v1 LOG_LEVEL=DEBUG   # 覆盖多个变量"
    echo ""
    echo "可覆盖的变量 (参考 .env 文件):"
    echo "  OPENAI_API_KEY, OPENAI_BASE_URL"
    echo "  PROXY_API_KEY"
    echo "  HOST, PORT, LOG_LEVEL"
    echo "  REQUEST_TIMEOUT, READ_TIMEOUT, MAX_RETRIES"
}

# 模式标志
DAEMON_MODE=false

# 解析命令行参数
parse_args() {
    for arg in "$@"; do
        case "$arg" in
            -h|--help)
                show_help
                exit 0
                ;;
            -d|--daemon)
                DAEMON_MODE=true
                echo "启用守护模式: 服务退出后将自动重启"
                ;;
            *=*)
                # 格式: VAR=VALUE，导出为环境变量
                export "$arg"
                echo "覆盖环境变量: $arg"
                ;;
            *)
                echo "警告: 忽略未知参数: $arg"
                ;;
        esac
    done
}

# 获取脚本的真实路径(解析符号链接)
# 在 macOS 和 Linux 上都能工作
get_real_script_path() {
    local source="${BASH_SOURCE[0]}"

    # 解析符号链接直到找到真实文件
    while [ -L "$source" ]; do
        local dir="$(cd -P "$(dirname "$source")" && pwd)"
        source="$(readlink "$source")"
        # 如果 readlink 返回相对路径,需要转换为绝对路径
        [[ $source != /* ]] && source="$dir/$source"
    done

    # 返回脚本所在的真实目录
    cd -P "$(dirname "$source")" && pwd
}

# 解析命令行参数（在获取项目路径之前，以便 -h 可以立即生效）
parse_args "$@"

# 获取项目根目录(脚本真实所在的目录)
PROJECT_ROOT_DIR="$(get_real_script_path)"
echo "项目根目录: $PROJECT_ROOT_DIR"
# 检查目录是否存在
if [ ! -d "$PROJECT_ROOT_DIR" ]; then
    echo "错误: 项目目录不存在: $PROJECT_ROOT_DIR"
    exit 1
fi

# 切换到项目根目录
cd "$PROJECT_ROOT_DIR" || {
    echo "错误: 无法切换到目录: $PROJECT_ROOT_DIR"
    exit 1
}

# 检查虚拟环境是否存在
if [ ! -d ".venv" ]; then
    echo "错误: 虚拟环境 .venv 不存在"
    echo "请在项目目录($PROJECT_ROOT_DIR)中运行 'uv venv' 创建虚拟环境"
    exit 1
fi

# 激活虚拟环境并运行脚本
source .venv/bin/activate

# 解析退出码，输出退出原因
parse_exit_code() {
    local exit_code=$1
    if [ $exit_code -eq 0 ]; then
        echo ""
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 服务正常退出"
    elif [ $exit_code -gt 128 ]; then
        # 退出码大于128表示被信号终止，信号值 = 退出码 - 128
        local signal_num=$((exit_code - 128))
        local signal_name
        case $signal_num in
            1)  signal_name="SIGHUP (终端挂断)" ;;
            2)  signal_name="SIGINT (Ctrl+C 中断)" ;;
            9)  signal_name="SIGKILL (强制终止)" ;;
            15) signal_name="SIGTERM (请求终止，可能是系统或其他进程发送)" ;;
            *)  signal_name="SIGNAL_$signal_num" ;;
        esac
        echo ""
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 服务被信号终止: $signal_name (退出码: $exit_code)"
    else
        echo ""
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 服务异常退出，退出码: $exit_code"
    fi
}

# 根据模式运行
if [ "$DAEMON_MODE" = true ]; then
    # 守护模式：服务退出后自动重启，日志实时输出
    RESTART_DELAY=1
    MAX_RAPID_RESTARTS=5
    RAPID_RESTART_WINDOW=60
    restart_times=()

    echo "守护模式已启用: 服务退出后将自动重启 (Ctrl+C 退出)"
    echo ""

    while true; do
        current_time=$(date +%s)

        # 清理超过时间窗口的重启记录
        new_times=()
        for t in "${restart_times[@]}"; do
            if [ $((current_time - t)) -lt $RAPID_RESTART_WINDOW ]; then
                new_times+=("$t")
            fi
        done
        restart_times=("${new_times[@]}")

        # 检查是否频繁重启
        if [ ${#restart_times[@]} -ge $MAX_RAPID_RESTARTS ]; then
            echo ""
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到频繁重启 (${RAPID_RESTART_WINDOW}秒内重启${MAX_RAPID_RESTARTS}次)，停止服务"
            exit 1
        fi

        # 使用 caffeinate 运行服务，防止 macOS App Nap 和系统休眠导致进程被终止
        # -i: 防止系统空闲休眠  -s: 防止系统休眠（仅AC电源时有效）
        caffeinate -is uv run start_proxy.py
        exit_code=$?

        parse_exit_code $exit_code

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${RESTART_DELAY}秒后自动重启... (Ctrl+C 取消)"

        # 记录本次重启时间
        restart_times+=("$(date +%s)")

        sleep $RESTART_DELAY
    done
else
    # 普通模式：直接运行，退出即停止
    # 使用 caffeinate 运行服务，防止 macOS App Nap 和系统休眠导致进程被终止
    # -i: 防止系统空闲休眠  -s: 防止系统休眠（仅AC电源时有效）
    caffeinate -is uv run start_proxy.py
    exit_code=$?

    parse_exit_code $exit_code

    exit $exit_code
fi

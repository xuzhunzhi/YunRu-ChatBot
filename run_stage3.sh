#!/usr/bin/env bash
# Linux 启动入口。与 run_stage3.bat 等价，但用 LF 换行与 POSIX shell。
#
# 用法：
#   ./run_stage3.sh              # 前台运行
#   ./run_stage3.sh &            # 后台运行
#   nohup ./run_stage3.sh > /dev/null 2>&1 &   # 断开终端后继续
#
# 配置文件：默认与开发环境共用 .env。若存在 .env.bot，则 Bot 用它——
# 这样 Bot 和你的日常对话可以用不同的 API Key，账单天然分开。
# 想显式指定别的文件：QQBOT_ENV_FILE=path ./run_stage3.sh
#
# 需要先准备：见 docs/DEPLOY_LINUX.md
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x ".venv/bin/python" ]; then
        PYTHON_BIN=".venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi

# 用 UTF-8 输出，避免中文日志在部分终端下变成乱码
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

# 有独立配置就用独立的；已由外部显式指定时优先尊重外部值。
if [ -f ".env.bot" ]; then
    export QQBOT_ENV_FILE="${QQBOT_ENV_FILE:-.env.bot}"
fi

exec "$PYTHON_BIN" -m qq_roleplay_bot.stage3_main

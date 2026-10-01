@echo off
REM Stage 3 启动入口。
REM
REM 配置文件：默认与开发环境共用 .env。若存在 .env.bot，则 Bot 用它——
REM 这样 Bot 和你的日常对话可以用不同的 API Key，账单天然分开。
REM 想显式指定别的文件，启动前 set QQBOT_ENV_FILE=路径 即可。
cd /d "%~dp0"
if exist "%~dp0.env.bot" set "QQBOT_ENV_FILE=.env.bot"
"%~dp0.venv\Scripts\python.exe" -m qq_roleplay_bot.stage3_main

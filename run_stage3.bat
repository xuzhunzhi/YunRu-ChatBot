@echo off
REM Stage 3 启动入口。
REM
REM 配置文件：默认与开发环境共用 .env。若存在 .env.bot，则 Bot 用它——
REM 这样 Bot 和你的日常对话可以用不同的 API Key，账单天然分开。
REM 想显式指定别的文件，启动前 set QQBOT_ENV_FILE=路径 即可。
cd /d "%~dp0"
REM 跑这一侧的 src（部署到 run/ 之后这条注释同样成立）
REM 显式指明包在哪：共用 venv 的 `.pth` 指向部署树（run/src），
REM 所以开发树必须自己说清"用我这份 src"，否则会静默 import 到另一边。
set "PYTHONPATH=%~dp0src"
REM 日志编码写死成 UTF-8：否则 data\bot.err.log 里的行编码跟着启动者的环境走，
REM 同一个文件半截 GBK 半截 UTF-8，多看几行就是乱码（2026-10-12 实测）。
set "PYTHONIOENCODING=utf-8"
if exist "%~dp0.env.bot" set "QQBOT_ENV_FILE=.env.bot"
"%~dp0.venv\Scripts\python.exe" -m qq_roleplay_bot.stage3_main

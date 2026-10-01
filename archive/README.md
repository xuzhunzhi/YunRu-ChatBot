# 归档目录

这里保存的是**不再参与默认运行路径**的历史实现和回退版本。它们被保留用于：

- 追溯早期设计决策（低频触发的 V1、引入短期对话状态的 Stage 2）；
- 需要时对照旧行为；
- 保留对应的回归测试。

## 内容

| 路径 | 原位置 | 说明 |
| --- | --- | --- |
| `qq_roleplay_bot_legacy/` | `src/qq_roleplay_bot/` | V1、Stage 2 与 `dev_*` 实验入口 |
| `qq_roleplay_bot_legacy/stage2_main.py` | 同名 | Stage 2 回退运行入口 |
| `qq_roleplay_bot_legacy/stage2_runtime.py` | 同名 | Stage 2 状态与旧输出解析 |
| `qq_roleplay_bot_legacy/v1_main.py` | 同名 | 最初的低频触发实现 |
| `qq_roleplay_bot_legacy/batch_main.py` 等 | 同名 | 批量与模型调试实验入口 |
| `test_legacy_stage2.py` | `tests/test_stage2.py` | Stage 2 与触发器的回归测试 |
| `test_legacy_v1.py` | `tests/test_v1.py` | V1 去重、批量与冷却测试 |
| `run_stage2.bat` | 项目根目录 | 旧的 Stage 2 启动脚本 |

## 与主代码的关系

- 这些模块从 `archive/` 导入主包的核心组件（`qq_roleplay_bot.transport`、`onebot_ws`、`llm_client`、`dev_config`），**主代码不再反向依赖 `archive/`**。
- 归档测试由 `tests/run_offline.py` 一并执行，因此它们仍然受回归保护，避免归档代码无声腐坏。
- 已删除的纯调试脚本（`private_debug.py`、`run_private_debug.bat`、`PRIVATE_DEBUG.md`）不在此保留：它们只是本地手工调试入口，`STAGE3_DESIGN.md` 和 `REAL_WORLD_TEST_CASES.md` 已覆盖同类验证。

## 使用注意

不要同时启动归档入口和 Stage 3：两者都会监听 8080，并可能对同一条消息重复处理。归档入口仅作为历史参考，**不再接受功能更新**；如果要恢复某个能力，应当把它重新实现在 Stage 3 主路径上，而不是修改归档副本。

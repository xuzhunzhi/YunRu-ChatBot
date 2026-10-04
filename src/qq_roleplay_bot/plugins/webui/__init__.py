"""控制面板插件（Stage 4）。

HTTP 服务跑在 bot 进程的一个线程里，由后台节拍看护与收尾。它**拿不到 transport、
拿不到 engine**，只能用装配点注入的闭包（`execute_action` / `apply_overrides` /
`memory_ops` / `prompt_library` / `knowledge` / `control_audit` / `self_id`）。
只写 `data/`，不改 `src/`、`.env`、`docs/yunru-source/`。
"""

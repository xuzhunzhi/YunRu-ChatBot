"""邮件通道插件：读信回信 + 每日汇报。

两条**后台插件**，开关相互独立（`QQBOT_MAIL_REPLY` / `QQBOT_MAIL_REPORT`）。
邮件通过私聊那条路进引擎，作为不可信 DATA；写信 agent 用独立的 client 与 `user_id`。
"""

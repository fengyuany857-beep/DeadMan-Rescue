# DeadMan Rescue

黑鲨 5 安卓「死人开关」自救模块：滚动续期 RTC 唤醒 + 编号登记制，让异常关机/落入充电层的手机能自救开机，正常关机则永不打扰。

- 规格：`docs/DESIGN.md`（经独立审计修订；审计报告 `docs/CODEX-AUDIT.md`、修订说明 `docs/REVISION-NOTES.md`）
- 当前阶段：P1a 主机模拟——在电脑上实现状态机与账本逻辑，用故障注入验证正确性；不涉及真机。任务书见 `TASK.md`。

# P1a 主机模拟测试报告

## 1. 范围、运行方法与实际结果

规格权威为 `docs/DESIGN.md`。本报告不修改、不替代设计中的真机验证状态。本次只实现 Python 3 标准库主机模型；未连接设备，未运行探针、真实关机、重启或 RTC 写入。测试逻辑不依赖真实时间或 sleep，时间均由 `SimClock` 推进。

在仓库根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

本次实际完整运行结果（不是预测计数）：

```text
test_module_storage_faults_at_each_persist_phase (test_sweeps.SweepTests) ... ok
test_old_instance_intent_is_quarantined_without_consumption (test_sweeps.SweepTests) ... ok
test_storage_fault_with_unreadable_slot_reports_unknown (test_sweeps.SweepTests) ... ok

----------------------------------------------------------------------
Ran 106 tests in 50.352s

OK
```

组合分类表在一个测试方法内枚举 2,592 种输入，不额外计作 2,592 个 unittest。24h 测试推进模拟时间 24h+60s，执行 1,441 次续期；检查发生压缩、重建一致、未完成事务及预算保留、快照/尾日志大小及包含代次切换临时空间的峰值上限。`peak_usage` 是空间预检查的保守上界，含预留空间，不是内核块分配的实测值。没有做真实 24h 运行。

状态口径：

- **host-sim PASS**：本行列出的主机断言全部通过；不是穷举证明，更不是设备认证。
- **partial**：有通过的主机子项，但本行还有明确未覆盖项或真机部分。
- **not covered**：没有能验证本行核心判据的测试；相关模型现象不替代该验证。

### 状态计数摘要

每组独立计数；不把下面不同映射表合并成覆盖率。

| 映射范围 | host-sim PASS | partial | not covered | 总行数 |
| --- | ---: | ---: | ---: | ---: |
| DESIGN 12.1 验证矩阵 | 2 | 13 | 4 | 19 |
| TASK.md「至少覆盖」 | 9 | 0 | 0 | 9 |
| DESIGN 5.3 崩溃恢复表 | 11 | 0 | 0 | 11 |
| DESIGN 7.2 分类表 | 9 | 0 | 0 | 9 |

## 2. 测试符号索引

下表使用真实类名、方法名，可通过 `rg -n 'class |def test_' tests` 核对。下文的 `Class.test_method` 均对应此处文件，不表示另有真机测试。

| 类 | 文件 |
| --- | --- |
| RTCTests | `tests/test_rtc.py` |
| StorageTests | `tests/test_storage.py` |
| LockTests | `tests/test_lock.py` |
| LedgerTests | `tests/test_ledger.py` |
| EngineTests | `tests/test_engine.py` |
| DaemonTests | `tests/test_daemon.py` |
| ShutdownTests | `tests/test_shutdown.py` |
| DisarmTests | `tests/test_disarm.py` |
| ReconcileTests | `tests/test_reconcile.py` |
| ClassifyTests | `tests/test_classify.py` |
| BudgetTests | `tests/test_budget.py` |
| ReoffTests | `tests/test_reoff.py` |
| StorageBoundsTests | `tests/test_storage_bounds.py` |
| ScenarioTests | `tests/test_scenarios.py` |
| SweepTests | `tests/test_sweeps.py` |

### 系统扫描的可复现口径

`SweepTests.test_every_defined_crash_point_per_reachable_entry` 通过 AST 遍历包内所有 `.hit(...)` 调用收集 **46 个命名断点模板**；动态阶段从实际 `.persist("PHASE")` 调用提取，动态文件名归一为 `{name}:suffix`。新表达式无法展开或有模板从未被扫描命中都会让测试失败，而不是静默跳过。动态文件名表示一类断点，不声称穷举所有文件名、同一模板的每一次重复调用或所有指令间隙。

扫描先记录各入口的可达断点，再对每个入口×可达模板分别用 subTest 注入崩溃；覆盖 renew、off、reboot、maintenance、disarm、boot、REOFF（含 reoff_after_action）、这些硬件失败路径，以及压缩和坏尾修复。每例独立拷贝已落盘的初始夹具，保留模拟时钟及进程许可，实际故障步骤仍经过真实 Storage 方法，不用 mock 跳过 fsync。分别重建同 boot/下一 boot，重复恢复，检查允许状态集、动作按原 tx 去重、消费键不改写、已提交对账不二次扣额；再注入外部槽并重建，确认恢复不清外部值。未提交的入口通过抛出异常而不是返回成功结束。尚未成功对账的隔离分支随后首次结算不被误算为“重复扣额”。

`SweepTests.test_module_storage_faults_at_each_persist_phase` 对 renew/off/reboot/disarm/reconcile 的 PREPARED、CLEARED_VERIFIED、COMMITTED，以及写槽入口的 READBACK_VERIFIED，逐项注入 ENOSPC/EIO/fsync/rename，共 **68 个模块级持久化故障子例**。断言失败、runtime FAULT、真实槽读回与报告一致、失败后 tick 不再改变槽或追加武装、没有关机动作。尾日志 append 本身不 rename，因此 rename 子例在指定阶段先触发实际快照/HEAD 压缩的 rename 错误，不能伪称 append 需要 rename。fsync 抛错前字节可能已可见，commit 出错前硬件也可能已写入：测试不谎称无残留；保证本进程不报告成功、不继续武装。`test_storage_fault_with_unreadable_slot_reports_unknown` 另测槽不可读时返回未知而非“已空”。

`SweepTests.test_old_instance_intent_is_quarantined_without_consumption` 独立构造新快照代次中保留的旧代次 INTENT，断言隔离、不消费、不复用；`test_maintenance_after_prepared_recovers_without_action` 单列维护清槽恢复。`test_module_integrity_failures_preserve_foreign_slot` 把坏尾、中段损坏、seq 回退、快照损坏、目录缺失、缓存超前、未知版本逐项送入 Module.boot/tick，断言 UNKNOWN、不清外部槽、不发动作、不盲建目录。

扫描发现并修复了两个恢复边界：未提交 reboot 已清空槽时不能像正常续期一样进入自动重试，现保持持久 FAULT/待处理意图；重试待定但无 pending 事务时出现外部非空槽，重建后即 QUARANTINE。已锁存 FAULT 的普通恢复只重建缓存，不重新解释/触碰未完成硬件事务，仍需可信修复。持久化失败返回也新增槽值/是否为空/是否未知的如实观察字段。

## 3. DESIGN 12.1 验证矩阵逐行对应

| # / 原试验 | 对应主机测试类/方法 | 状态及未证明部分 |
| --- | --- | --- |
| 1 主机状态机穷举与故障注入 | `SweepTests.test_every_defined_crash_point_per_reachable_entry`、`test_module_storage_faults_at_each_persist_phase`、`test_module_integrity_failures_preserve_foreign_slot`；`ReconcileTests.test_abnormal_boot_idempotent_and_no_second_charge`；`DaemonTests.test_lock_loser_and_unpermitted_instance` | host-sim PASS：定义的断点模板按可达入口系统扫描、持久化错误矩阵和完整性/外部槽负例通过；PASS 限于上述有限模型扫描，不是任意状态空间/指令时序的形式证明。 |
| 2 主机恶意输入与生命周期 | `StorageTests.test_symlinks_and_traversal`、`test_symlinked_state_directory_rejected`、`test_atomic_append_permissions`；`LedgerTests.test_enospc_and_record_limit`、`test_invalid_fields_not_written`、`test_seq_rollback_and_unknown_version`；`LockTests.test_identity_replacement_stops_old_object`；`DaemonTests.test_stop_generation_upgrade_blocks_old_instance` | partial：链接状态目录、超长记录、非法类型/版本、停用代次均有断言；异常所有者、TOCTOU、全部入口参数模糊测试未穷举，详见第 7 节。 |
| 3 T2a 直写 +300 正常关机 | 无设备测试；`RTCTests.test_faults_and_relative_only` 只证明模型相对写入契约 | not covered：插电/非插电、唤醒时刻、健康启动与使能状态均为「主机模拟不可证，待 T2/T3 裁决」。 |
| 4 T2b 插电重启落入充电层的受控故障复现 | `ScenarioTests.test_B_reboot_falls_into_charging_layer` 仅模拟计数到期后进入新 boot | not covered：没有真实充电层、通道保持或故障复现；「主机模拟不可证，待 T2/T3 裁决」。 |
| 5 T3 注销 | `ShutdownTests.test_off_commits_before_action`；`ScenarioTests.test_C_intentional_off_then_manual_boot`；`ReconcileTests.test_current_consumed_and_cancelled_intents_not_reused`、`test_off_without_prior_arm_manual_boot` | partial：模型先清再提交、人工启动分类/核销；真实越过截止点再观察 10 分钟、固件注销效果为「主机模拟不可证，待 T2/T3 裁决」。 |
| 6 T4 无登记强制断电 | `ScenarioTests.test_D_unregistered_disappearance`；`ClassifyTests.test_all_nine_mutually_exclusive_outcomes` | partial：模型到期/清残/保守预算与互斥分类；没有断电或 bootloop，剩余保护量及实际上电为「主机模拟不可证，待 T2/T3 裁决」。 |
| 7 T5 自有入口普通重启 | `ShutdownTests.test_reboot_resigns_and_trap_merges`；`ScenarioTests.test_A_normal_reboot`；`ReconcileTests.test_abnormal_boot_idempotent_and_no_second_charge`；`ClassifyTests.test_all_nine_mutually_exclusive_outcomes` | partial：改签、核销、同 boot 幂等和分类为模型断言；真实重启路径、映射、启动时长为「主机模拟不可证，待 T2/T3 裁决」。 |
| 8 T6 off 登记但受控保留唤醒 | `ReoffTests.test_disabled_default_and_enabled_once`、`test_enabled_attempt_bound_to_consuming_boot_and_not_repeated`、`test_crash_after_attempt_never_reissues`、`test_unknown_command_result_quarantined`、`test_clear_failure_prevents_attempt_and_action`、`test_manual_intervention_after_attempt_cancels_action`、`test_trusted_cancellation_immediately_before_action` | partial：仅门控/次数/取消协议；「主机模拟不可证，待 T6 裁决」。真实 bootreason 映射亦为「主机模拟不可证，待 T2/T3 裁决」。默认关闭，不能用本报告开启实机 REOFF。 |
| 9 SIGKILL/续期各窗口中断 | `EngineTests.test_crash_boundaries_recover_without_blind_actions`、`test_off_crash_after_stop_never_executes`、`test_off_clear_and_commit_crashes_do_not_replay_actions`；`DaemonTests.test_lock_loser_and_unpermitted_instance` | partial：异常抛出+对象重建，不是真 SIGKILL/掉电；设备监督和保护余量为「主机模拟不可证，待 T2/T3 裁决」。 |
| 10 系统菜单/厂商入口与 trap 采样 | `ShutdownTests.test_parse_powerctl`、`test_lost_and_delivered_trap`、`test_empty_trap_is_stop_not_intent`、`test_unknown_and_malformed_trap_are_safe_maintenance`、`test_maintenance_never_resigns` | partial：仅字符串解析和信号到达/丢失模型；厂商路径、原始值、延迟分位/超时、实机维护为「主机模拟不可证，待 T2/T3 裁决」。 |
| 11 调系统时间与 RTC 跳变 | `DaemonTests.test_rtc_jumps_quarantine`；`EngineTests.test_recovery_untrusted_written_value_cleans_only_own`；`RTCTests.test_clock_rejects_backwards` | partial：模型 RTC 正/负跳变检测和停用；系统校时、NTP 与真实 RTC 是否联动为「主机模拟不可证，待 T2/T3 裁决」。 |
| 12 响后残留与旧值回拨再入未来 | 无直接旧值回拨复响测试；`ScenarioTests.test_D_unregistered_disappearance` 只包含模型到期后清残 | not covered：可见 enabled、固件响后残留和再次触发均为「主机模拟不可证，待 T2/T3 裁决」；模型也未直接测试旧目标在回拨后重入未来的组合。 |
| 13 外部槽覆盖/清除竞争 | `DaemonTests.test_foreign_slot_untouched`；`EngineTests.test_conflict_between_clear_and_write_is_not_overwritten`；`ReconcileTests.test_foreign_boot_slot_untouched`、`test_conflict_immediately_before_clear_not_overwritten` | partial：覆盖竞争和已知边界通过，未穷举外部清槽时序；厂商并发写槽和关机竞争为「主机模拟不可证，待 T2/T3 裁决」。 |
| 14 清槽/fsync/关机命令失败 | `SweepTests.test_module_storage_faults_at_each_persist_phase`、`test_storage_fault_with_unreadable_slot_reports_unknown`；`StorageTests.test_faults_never_success`；`ShutdownTests.test_clear_failure_does_not_execute`；`ReoffTests.test_unknown_command_result_quarantined` | partial：主机部分 host-sim PASS，含 68 个阶段故障子例与槽未知报告；真实落盘、固件清槽、执行命令为「主机模拟不可证，待 T2/T3 裁决」，复关机结果另为「主机模拟不可证，待 T6 裁决」。 |
| 15 救援预算/冷却/热与低电门槛 | `BudgetTests.test_prepared_crash_reservation_survives_unknown_and_exhausts`、`test_verified_normal_reboot_releases_reservation`、`test_stable_reset_requires_all_health_conditions`、`test_health_failure_and_process_rebuild_restart_timers`、`test_boot_change_gets_no_cooldown_credit`；`DaemonTests.test_sensors_stop_and_clear` | host-sim PASS：预扣、未知不退、稳定/冷却、计时重启、传感器缺失/阈值模型通过。实机传感器路径/单位及阈值安全性不在此 PASS 内，属「主机模拟不可证，待 T2/T3 裁决」。 |
| 16 升级/禁用/卸载残留 | `DaemonTests.test_stop_generation_upgrade_blocks_old_instance`；`DisarmTests.test_disarm_complete_closing_records`、`test_uninstall_clears_before_deleting`、`test_forced_directory_delete_leaves_unowned_residue`、`test_disarm_does_not_bypass_fault_repair` | partial：模型收尾/停监督/强删残留通过，不是真实升级安装器或设备卸载；固件残留与人工收尾为「主机模拟不可证，待 T2/T3 裁决」。 |
| 17 有界存储与 24h 续期 | `StorageBoundsTests.test_24h_renewals_compact_with_bounded_peak_and_rebuild`、`test_uncompressible_storage_stands_down_without_drop`、`test_renewals_do_not_reparse_unchanged_verified_tail`；`LedgerTests.test_head_switch_crashes_recover_only_selected_generation`、`test_quota_compaction_does_not_drop_evidence` | partial：24h 模拟续期与限额通过；真实 24h 耗时/写失败统计、闪存寿命/耗电/槽独占为「主机模拟不可证，待 T2/T3 裁决」。 |
| 18 RescueX/bootmod 共存 | `BudgetTests.test_stable_reset_requires_all_health_conditions`；`DaemonTests.test_write_fail_max_and_supervisor`；`ShutdownTests.test_maintenance_never_resigns` | partial：只有注入健康状态与监督退避，不含真实 RescueX/bootmod 动作或写集离线审阅；共存、健康接口和停用边界为「主机模拟不可证，待 T2/T3 裁决」。 |
| 19 备选 PowerOffAlarm 契约 | 无；本实现只有模拟单槽 RTC 通道 | not covered：没有实现或验证备选设置/取消/持久状态；「主机模拟不可证，待 T2/T3 裁决」，不能视为第二通道兜底。 |

## 4. TASK.md「至少覆盖」逐项对应

| # / 要求 | 测试 | 状态 |
| --- | --- | --- |
| 1 每个事务提交点崩溃并按表恢复 | `SweepTests.test_every_defined_crash_point_per_reachable_entry`、`test_module_storage_faults_at_each_persist_phase`；`EngineTests.test_crash_boundaries_recover_without_blind_actions` | host-sim PASS：46 个定义模板全部命中，各可达入口同/跨 boot 恢复并查重；68 个持久化故障子例。 |
| 2 坏尾/seq/快照/缺目录后 UNKNOWN/QUARANTINE，不清他人槽 | `SweepTests.test_module_integrity_failures_preserve_foreign_slot`；`LedgerTests.test_bad_tail_preserved_before_truncate`；`ReconcileTests.test_cache_rollback_requires_new_generation_and_explicit_enable` | host-sim PASS：七类完整性失败均经 Module.boot/tick+外部槽验证，仍不外推所有 API 排列。 |
| 3 重复 boot、旧/消费/取消 INTENT | `ReconcileTests.test_abnormal_boot_idempotent_and_no_second_charge`、`test_current_consumed_and_cancelled_intents_not_reused`、`test_cancelled_intent_not_classified_as_off`、`test_mismatched_intent_quarantined`；`SweepTests.test_old_instance_intent_is_quarantined_without_consumption` | host-sim PASS：包括独立旧实例代次负例，不复用、不消费。 |
| 4 RTC 前跳/回拨停用、外部占用冲突 | `DaemonTests.test_rtc_jumps_quarantine`、`test_foreign_slot_untouched` | host-sim PASS。 |
| 5 预算预扣/耗尽/稳定冷却/未知不退 | `BudgetTests.test_prepared_crash_reservation_survives_unknown_and_exhausts`、`test_stable_reset_requires_all_health_conditions`、`test_boot_change_gets_no_cooldown_credit` | host-sim PASS。 |
| 6 ENOSPC 不假成功、不新增武装 | `EngineTests.test_prepare_and_commit_storage_failure`、`test_commit_failure_reports_uncertain_hardware`；`ReconcileTests.test_enospc_observation_never_clears_or_succeeds`、`test_enospc_consumption_preserves_pending_and_reservation` | host-sim PASS：提交失败可能留下已经写入、未提交的槽；报告不成功并保留待办，不声称绝无残留。 |
| 7 双实例锁互斥、后到不续期 | `LockTests.test_same_process_exclusion`；`DaemonTests.test_lock_loser_and_unpermitted_instance` | host-sim PASS：同宿主的两个对象；不是跨设备或真实多进程压力证明。 |
| 8 disarm 完整收尾、强删残留 | `DisarmTests.test_disarm_complete_closing_records`、`test_forced_directory_delete_leaves_unowned_residue` | host-sim PASS。 |
| 9 A–E 端到端 | `ScenarioTests.test_A_normal_reboot`、`test_B_reboot_falls_into_charging_layer`、`test_C_intentional_off_then_manual_boot`、`test_D_unregistered_disappearance`、`test_E_failed_off_unknown_keeps_system_on`；另有 `test_F_cancel_requires_explicit_resume` | host-sim PASS：只指模型流程；所有设备因果推断仍须 T2/T3。 |

## 5. DESIGN 5.3 崩溃恢复表逐行对应

| 中断点 | 测试 | 状态及边界 |
| --- | --- | --- |
| PREPARED 前或 fsync 未成功 | `SweepTests.test_every_defined_crash_point_per_reachable_entry`、`test_module_storage_faults_at_each_persist_phase`；`EngineTests.test_crash_boundaries_recover_without_blind_actions` | host-sim PASS：各目标入口 prepare-fsync/ENOSPC/EIO/rename 与命名崩溃边界通过；可见字节不冒称 fsync 成功。 |
| PREPARED 后、清槽前 | `SweepTests.test_every_defined_crash_point_per_reachable_entry`、`test_maintenance_after_prepared_recovers_without_action`；`EngineTests.test_off_pending_recovery_clears_without_action_replay` | host-sim PASS：含维护的独立 after_prepared 崩溃，续期保守停用、off/维护幂等清槽，不重发命令。 |
| 清成功、写前或清槽证据未落盘 | `EngineTests.test_crash_boundaries_recover_without_blind_actions`（after_clear / after_clear_evidence）；`test_cross_boot_pending_is_reconciling`；`ReconcileTests.test_crash_each_boot_boundary` | host-sim PASS：空窗、同 boot 新事务重试及跨 boot 清残。 |
| 写成功、验证或 COMMITTED 前 | `EngineTests.test_crash_boundaries_recover_without_blind_actions`（after_write / after_armed_verified）；`test_recovery_untrusted_written_value_cleans_only_own`、`test_recovery_refuses_unexplained_foreign_value`；`ReconcileTests.test_cross_boot_uncommitted_reboot_is_unknown_and_cleared` | host-sim PASS：可信匹配补提交、异常停用、跨 boot 不当成功心跳。 |
| 布防 COMMITTED、缓存更新前 | `EngineTests.test_crash_boundaries_recover_without_blind_actions`（after_commit）；`ReconcileTests.test_abnormal_boot_idempotent_and_no_second_charge` | host-sim PASS：权威链恢复，不重放动作。 |
| off 禁止续期后、清槽/读回失败 | `EngineTests.test_off_crash_after_stop_never_executes`；`ShutdownTests.test_clear_failure_does_not_execute`；`ScenarioTests.test_E_failed_off_unknown_keeps_system_on` | host-sim PASS：失败不关机、不声称清除，故障/残留可见。 |
| 清槽验证后、消费提交前 | `ReconcileTests.test_crash_each_boot_boundary`（boot_after_clear_evidence / boot_before_consumption，含 recover 入口）；`test_enospc_consumption_preserves_pending_and_reservation` | host-sim PASS：保留原 arm_id，重读/清槽，一次消费。 |
| 消费 COMMITTED、BOOT 缓存前 | `ReconcileTests.test_crash_each_boot_boundary`（boot_after_consumption / boot_after_cache）；`test_abnormal_boot_idempotent_and_no_second_charge` | host-sim PASS：仅恢复结论/缓存，不重复扣额或自动发动作。 |
| REOFF 请求持久化后、命令前/后失联 | `SweepTests.test_every_defined_crash_point_per_reachable_entry`（全部 reoff_*，含 reoff_after_action）；`ReoffTests.test_crash_after_attempt_never_reissues`、`test_unknown_command_result_quarantined` | host-sim PASS：请求后、动作前/后均崩溃重建，同/跨 boot 不二次发原动作。真实失联/复关机为「主机模拟不可证，待 T6 裁决」。 |
| 快照或 HEAD 切换中 | `LedgerTests.test_head_switch_crashes_recover_only_selected_generation`、`test_head_rename_crashes`；`ReconcileTests.test_integrity_recovery_crash_does_not_remove_rollback_gate` | host-sim PASS：仅选有效 HEAD，新旧代次/回滚门控保留；不代表物理掉电持久性。 |
| ENOSPC/EIO、目录丢失、链损坏 | `SweepTests.test_module_storage_faults_at_each_persist_phase`、`test_storage_fault_with_unreadable_slot_reports_unknown`、`test_module_integrity_failures_preserve_foreign_slot`；`StorageBoundsTests.test_uncompressible_storage_stands_down_without_drop` | host-sim PASS：五类入口的持久阶段错误矩阵、七类损坏+外部槽、槽不可读报告均覆盖。真实文件系统/闪存失败语义为「主机模拟不可证，待 T2/T3 裁决」。 |

上述阶段异常模拟不等价于真实掉电，所有硬件步骤的真实可达性、断电留槽和读回意义均为「主机模拟不可证，待 T2/T3 裁决」。

## 6. DESIGN 7.2 分类表逐行对应

以下九行的 PASS 只证明给定 Evidence 下的软件分类。原因映射、RTC 因果、可信时基和人工启动辨识均通过注入假定；其真实性全部是「主机模拟不可证，待 T2/T3 裁决」。涉及 REOFF 的许可还须 T6，不由分类 PASS 授权。

| 有效旧周期 / 充分证据 | 唯一结论 | 测试 | 状态 |
| --- | --- | --- | --- |
| reboot / 已验证普通重启、可信排除已响 | REBOOT_OK | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`；`ScenarioTests.test_A_normal_reboot`；`BudgetTests.test_verified_normal_reboot_releases_reservation` | host-sim PASS |
| reboot / 已验证且关联 arm 的 RTC 唤醒 | REBOOT_RESCUED | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`；`ScenarioTests.test_B_reboot_falls_into_charging_layer` | host-sim PASS |
| reboot / 缺失或矛盾证据 | REBOOT_OUTCOME_UNKNOWN | `ClassifyTests.test_defaults_conflicts_and_missing_association_stay_unknown`、`test_mapping_and_named_mismatches_are_unknown_in_every_family`；`ReconcileTests.test_cross_boot_uncommitted_reboot_is_unknown_and_cleared` | host-sim PASS |
| 无 INTENT、有已提交武装 / 已验证非 RTC 自恢复且早于触发 | SELF_RECOVERED | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`、`test_manual_and_self_recovery_evidence` | host-sim PASS |
| 无 INTENT、有已提交武装 / 关联 arm 的 RTC 唤醒 | RESCUED_ABNORMAL | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`；`ScenarioTests.test_D_unregistered_disappearance` | host-sim PASS |
| 无 INTENT / 其余，包括人工启动 | ABNORMAL_OUTCOME_UNKNOWN | `ClassifyTests.test_manual_and_self_recovery_evidence`、`test_mapping_and_named_mismatches_are_unknown_in_every_family` | host-sim PASS |
| off / 可信人工启动，无 RTC 冲突 | OFF_MANUAL | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`、`test_defaults_conflicts_and_missing_association_stay_unknown`；`ReconcileTests.test_off_without_prior_arm_manual_boot`；`ScenarioTests.test_C_intentional_off_then_manual_boot` | host-sim PASS |
| off / 可信关联 RTC 证据 | OFF_RTC_WAKE | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`；`ReoffTests.test_enabled_attempt_bound_to_consuming_boot_and_not_repeated` | host-sim PASS |
| off / 原因未知或冲突 | OFF_OUTCOME_UNKNOWN | `ClassifyTests.test_all_nine_mutually_exclusive_outcomes`、`test_mapping_and_named_mismatches_are_unknown_in_every_family`；`ScenarioTests.test_E_failed_off_unknown_keeps_system_on` | host-sim PASS |

`ClassifyTests.test_combination_table_has_exactly_one_outcome_in_correct_family` 统一枚举原因、意图种类、空/非空 arm、证据关联、映射验证、时基、冲突及是否已响，断言只落在相应三分支家族之一。未验证映射对三个家族都只给 UNKNOWN；显式不同 arm 为冲突。只有 RTC 关联结果必须提供相同的非空 arm。OFF_MANUAL 允许 off 时槽已空、arm_id 为空；无 INTENT 的 manual_power_key 不得被称为 SELF_RECOVERED。SELF_RECOVERED 只接受白名单 `non_rtc_recovery` 或 `normal_reboot`，并要求可信时基及明确未响；人工原因的原始 Evidence 保存在 BOOT 观察中。

## 7. 保守策略、未覆盖边界与非保证

### T2/T3 待裁决的策略

- `Module(config={"rtc_tolerance": ...})` 可配置采样容差，默认 `RTC_TOLERANCE=0`；不是本机测得的误差界。
- `Evidence.mapping_verified=False` 是默认；不从字符串或墙钟推定可信映射。`trusted_timebase` 默认 False、`rtc_fired_before` 默认 None，证据不足走 UNKNOWN。普通非 RTC 结果不强制空值必须命名 arm；显式命名错误仍是冲突。
- `reoff_enabled` 默认 False；显式开启只用于模型协议测试，不表示通过 T6。命令结果由 `reoff_command_result` 注入。
- `rescuex_healthy` 默认 None，无法核实就不复位预算；可注入 bool 或 callable。必须同时有本 boot 对账提交、boot_completed、守护健康、安全传感器、无失败/冲突和本 boot 连续稳定/冷却观察。
- RTC、传感器、bootreason、RescueX 离线契约和固件解除效果尚未定版；主机不得替实机背书。复位或可信修复后仍需显式 enable/resume，不自动重建保护。

### 明确的覆盖缺口

1. 已扫描所有定义的断点模板在列出入口的可达路径，含维护 PREPARED、REOFF 动作后崩溃和七类完整性错误+外部槽组合。仍不是所有机器指令间隙、同一模板每次重复调用、所有文件名、任意深度连续崩溃、全部状态/传感器/人工介入/并发时序的全排列证明。新增命名断点必须通过 AST 收集和命中检查，不能靠手工遗漏保持绿色。
2. `flock` 在本 Linux 宿主使用真实文件锁，但测试主要是同进程不同文件描述符/Module 对象，另有弱引用所有者登记；没有跨宿主锁、分布式文件系统或真实多进程争用压力测试。不能推出安卓监督器必然互斥。
3. 实现调用主机 fsync/rename，错误通过注入模拟；崩溃是异常后重建对象，不清除内核页缓存，不等于 SIGKILL、突然断电、闪存控制器落盘或 Android 文件系统耐久性证明。
4. RTC 是文件背书计数器，独立于 Unix 纪元，但没有真实 enabled 状态、PMIC、充电层、固件重写或唤醒硬件。到期属性不代表真实成功开机；旧值回拨再响和响后残留未做设备验证。
5. bootreason 和可信时间/关联关系由 Evidence 注入，不是采集或标定结果。尤其 REOFF 测试可直接注入 off 后 RTC 原因，不证明真实 off 清槽后仍能产生该证据链。没有验证厂商菜单、trap 可写窗口/延迟、系统时间与 RTC 的联动。
6. 权限检查实测以非 root 用户运行（本次 uid=1000），验证私有文件/目录和链接拒绝；未验证 Android root/SELinux、不同真实 uid 所有者攻击、权限降级、挂载替换、TOCTOU 竞态。没有恶意输入完整 fuzz、嵌套 JSON 资源耗尽、全部深层字段类型/所有入口绕锁的穷举证明。现有超长、非法字段类型、未知版本、符号链接、停用代次测试不能外推成全面安全审计。
7. 健康是离散观察和注入模型，不是连续进程监控；未验证 RescueX/bootmod 真正共存、接口真实性、待处理救援或传感器单位。原设计要求的真机热/低电风险不应通过危险实测补覆盖。
8. 配额按模型目录字节数及临时文件计；不等于文件系统实际分配块、磁盘剩余空间、闪存写放大。证据过大时允许 FAULT，不能为保持可用而丢未完成证据；没有长年历史增长/诊断档案/真实 I/O 时延基准。24h 仅模拟逻辑时间。
9. 未实现 PowerOffAlarm；没有双通道兜底。没有任何 G3、固件留槽不复响、必救、绝不误救或绝不漏救的保证。强删状态目录可留下未知归属硬件槽，程序不会盲清，也不会自动重建账本。

## 8. 交付检查

`.gitignore` 排除 `__pycache__/` 和 Python 字节码。交付只包含 `deadman_sim/`、`tests/`、本报告和 `.gitignore`；`docs/` 不改。提交前核对 `git diff --stat main -- docs` 输出为空。无第三方依赖，无 Android/真机实现，本报告不改变 DESIGN 原有真机待验证结论。

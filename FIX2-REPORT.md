# FIX2 报告：trusted_recover 保留退役 boot 墓碑

## 改动文件与要点

- `deadman_sim/system.py:562–570`：重建 control 时仍强制 `enabled=False`、`supervisor_permit=False`；仅当 loaded state 的原 control 存在 `retired_boots` 时，将其值原样带过。不重算墓碑，不新增缺失字段，不保留整个旧 control。
- `tests/test_reconcile.py:215`：新增压缩、完整性恢复与重放组合回归测试，覆盖退役身份、最近窗口身份和新身份。
- `FIX2-REPORT.md`：本报告。

仅使用标准库，未修改 `docs/`，未执行 git commit。原有未跟踪文件 `FIX2-BRIEF.md` 未修改。命令均在本 worktree 根目录运行，未创建大体积临时副本；测试临时目录沿用现有 ModuleCase 的标准库 TemporaryDirectory 行为。

## control 键审计表

检查了全仓库 Python 代码中 `control` 的直接索引、`.get()`、别名读取及更新位置。下表列出全部 11 个已知键；其中 `completed_through_seq` 仅由测试读取。行号为本次最终代码的行号。

| 键 | 所有运行时代码读取位置 | trusted_recover 是否必须保留原值及理由 |
| --- | --- | --- |
| `retired_boots` | `ledger.py:191`（retired_boot）、`:214`（compress_completed）；新增 `system.py:564–565`（存在性检查及复制） | **必须**。最近 64 项外的 boot 身份只靠此墓碑拒绝重放；恢复实例代次不能使旧身份重新获得消费资格。本次唯一生产行为修复。 |
| `enabled` | `system.py:287,366,368,432` | **不保留**，必须重置为 False；可信恢复清残后仍需显式 enable，不能复活旧续期授权。 |
| `supervisor_permit` | `system.py:433,610` | **不保留**，必须重置为 False；旧监督进程不得自动重新接管恢复后的实例。 |
| `owner` | `system.py:432` | **不保留**。旧进程 token 不是新实例的授权；显式 enable 或合法 claim 重新写入 owner。 |
| `boot_id` | `system.py:287,435` | **不保留**。这是旧续期会话的 boot 绑定，不能作为恢复后的会话许可；新 enable 写入当前 boot_id。与顶层历史 boot 身份不同。 |
| `retry` | `system.py:452` | **不保留**。旧 pending_txs 已隔离，不能继续旧 arm 重试；后续 `_stop()` 明确写 False。 |
| `cleanup_pending` | `system.py:290` | **不必保留旧值**。恢复先检查槽归属、隔离旧事务，再由 `_stop()` 建立当前清理待办，`_clear_owned()` 验证清槽后清除；失败时不返回恢复成功。 |
| `persistence_gate_state` | `system.py:157–158` | **不必保留**。只用于 IO-UNCERTAIN 路径选择 FAULT/STAND_DOWN；完整恢复重建代次、停止、清槽验证，成功后显式清除不确定性标记。失败时恢复流程自身保持停用。 |
| `fault_latched` | `system.py:299,372,391,482,517`；`reconcile.py:108` | **需区分可信完整性恢复是否也被定义为显式故障修复**。若不是，应保留到 repair_fault；若是，则可作为可信修复清除。当前实现丢弃此键，确实撤销上述门槛；下面单列实测行为与语义待裁决点。本次不修改。 |
| `sensor_stand_down` | `system.py:400,403,433,502,632` | **应保留到稳定健康观察通过**。实时传感器恢复合格不能替代规定的稳定观察；丢失会绕过 enable/resume、tick 和 reboot 的持久停用门槛。下面单列同类缺陷；按简报不修。 |
| `completed_through_seq` | 无生产代码读取；测试读取见下文 | **不必保留旧值**。这是完成历史压缩的诊断水位，不是运行时安全判据；完整性恢复重新从 seq=0 建链，旧链水位也不能直接当作新链水位。 |

补充完整读取范围：

- `system.py:431` 将 `loaded.state["control"]` 绑定到 `control`，随后读取的键均已列入表中；`:482` 使用 `state.get("control", {}).get(...)`，同样已覆盖。
- `ledger.py:116,154` 对 control 做合并更新，`system.py:_record()` 及 `engine.py:102` 组装 control 变更；这些没有额外读取未列出的业务键。快照/缓存整体序列化不另增判据。
- 既有测试读取：`test_daemon.py:18,25` 的 `owner`；`test_review_regressions.py:168,181,185` 的 `cleanup_pending`、`:169` 的 `enabled`、`:283,317` 的 `completed_through_seq`。
- 新回归测试读取：`test_reconcile.py:230,255` 的 `retired_boots`、`:239` 的 `enabled`、`:240` 的 `supervisor_permit`。其余新增断言对账本整体进行比较。

## 只审不修的额外发现

### sensor_stand_down：同类安全门槛丢失，已复现

标准库小型模拟探针执行 install → enable → arm → 电量降至 19 → tick 停用清槽 → 电量恢复至 80。此时 enable 返回 `stable observation required`。随后制造 CACHE_AHEAD 并调用 trusted_recover，`sensor_stand_down` 变为缺失，enable 立即成功；模拟单调时间仍为 0，未经过 1800 秒稳定观察，boot_completed/RescueX 健康条件也未设置。

这与 `docs/DESIGN.md` 第 7.3 节及 `BudgetTests.test_sensor_stand_down_needs_verified_stable_observation_before_resume` 的稳定观察要求冲突。根因同为替换 control 后持久拒绝标记消失。此发现只记录，未改代码、未新增其测试。

### fault_latched：丢失及重新 enable 已复现，修复语义待裁决

另一探针执行 install → enable → arm → 注入 3 次 RTC 写失败 → 3 次 tick，确认 `fault_latched=True`，恢复前 enable 被拒绝。制造 CACHE_AHEAD 后 trusted_recover 成功，`fault_latched` 缺失，enable 在时间 0 成功。

丢失会撤销故障锁存，行为已确认；但 trusted_recover 本身是显式可信操作，会清自有槽并解除 runtime_fault，是否同时授权替代 repair_fault 需要明确。因此将其单列为同类潜在缺陷，不把语义未裁决项宣称为确定违规，也不扩大本次修复范围。

探针最初尝试将小型模拟状态放在 worktree 的临时子目录，因祖先目录权限为 2770，被 Storage 的 `writable state ancestor` 检查拒绝；改用与现有测试相同的默认 TemporaryDirectory 后，上述两个探针均实际完成。没有修改目录权限或绕过存储校验，临时目录均已自动清理。

## 新测试及断言清单

测试：`test_reconcile.ReconcileTests.test_trusted_recovery_preserves_compressed_retired_boot_replay_guard`。

1. 先真实布防，再通过模块 boot 入口完成 70 个不同 boot 的对账；每次均成功。
2. 调用 `ledger.compact()`，经 `_switch()` 执行 `compress_completed()` 并持久化结果；确认无未完成事务、最近窗口恰为最后 64 个身份、最早 boot 已不在 boots 中且被 retired_boot 判为退役。
3. 写入领先于账本的缓存 seq，确认 LoadStatus.CACHE_AHEAD 且普通 boot 被拒绝；随后 trusted_recover 成功、实例代次改变、enabled/supervisor_permit 均为 False。
4. 重建模块并重放最早退役 boot：返回失败及 `retired/ambiguous boot identity`；seq、records、完整 state、budget、consumed_arms 全部不变，没有新增 BOOT 事务或 arm/救援预算消费，也没有执行关机动作。
5. `retired_boots` 与恢复前原值完全相同，恢复后 retired_boot 仍返回 True。
6. 对最近窗口内全部 64 个 boot 逐个重建模块并重放：均成功返回 repeated、原结论保持不变，seq 和完整 state 不变。
7. 创建一个从未出现的新 boot：正常成功且不是 repeated，seq 推进，身份进入 reconciled_boot_ids，并有对应 COMMITTED BOOT 记录。

## 实际验证结果

- 修复生产代码前，仅新增回归测试后运行：
  `python3 -m unittest discover -s tests -p test_reconcile.py -k trusted_recovery_preserves_compressed_retired_boot_replay_guard`
  实测 1 项，失败 1 项，耗时 9.028 秒；失败点是退役 boot 重放返回 success=True，确认测试能捕获原缺陷。
- 修复后同一命令：实测 1 项，通过 1 项，耗时 12.842 秒，输出 OK。
- 全套命令（在本 worktree 根目录）：`python3 -m unittest discover -s tests`

```text
............................................................................................................................
----------------------------------------------------------------------
Ran 124 tests in 67.605s

OK
```

全套总数 124，通过 124，失败 0，错误 0，跳过 0。原有 123 项加新增 1 项；64 个近期 boot 的 subTest 不另计为测试方法。`git diff --check` 通过。

上述 PASS 指本简报限定的 retired_boots 修复与回归验收；额外审计发现保留在本报告中，不表示这些范围外问题已修复。

验收状态：PASS

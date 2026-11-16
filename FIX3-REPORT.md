# FIX3 报告：trusted_recover 保留传感器停用与故障锁存

## 改动文件与要点

- `deadman_sim/system.py`：`trusted_recover()` 重建 control 时，对存在的 `retired_boots`、`sensor_stand_down`、`fault_latched` 原样复制原值，不补入原本缺失的键。`enabled=False`、`supervisor_permit=False` 以及其余键的既有重置语义保持不变。
- `tests/test_reconcile.py`：只新增下列两条 unittest 回归测试，未修改既有测试或断言。
- `FIX3-REPORT.md`：记录字段判据、断言清单及先失败后修复的实测结果。
- `LIVE_STATUS.md`：按工作区 AGENTS.md 的进度要求记录阶段与证据。

仅使用标准库；未修改 `docs/`；未执行 git commit。任务开始时已有的未跟踪文件 `FIX3-BRIEF.md` 未修改。所有测试命令都在本 worktree 根目录执行，未创建大体积临时副本；测试沿用 ModuleCase 的标准库 TemporaryDirectory。

## 附属字段与恢复语义

`sensor_stand_down` 当前是布尔值，没有内嵌观察结构。实际连续观察起点在当前 Module 对象的 `_healthy_since`，账本中的 `budget.stable_boot_id`、`budget.stable_since`、`budget.cooldown_since` 记录观察身份和时刻。既有 `trusted_recover()` 通过 `dict(loaded.state, ...)` 保留整个 budget，成功路径也不重置 `_healthy_since`；本次保持这些行为，不新增或重置计时字段。回归测试在恢复前实际开始观察，先经过 600 秒再恢复，恢复后检查完整 budget 不变，并在原观察起点后 1799 秒拒绝、1800 秒放行，证明没有丢失门槛或重启窗口。对象重建、boot 变化和健康失败时重新计时的既有规则不变。

`fault_latched` 当前同样是布尔值，enable 的锁存判定直接读取此键，没有必须额外复制的 control 原因或计数字段。顶层 `write_fail_count` 用于失败阈值和健康判定，`fault_flags` 记录故障历史；它们原本就在 `dict(loaded.state, ...)` 中保留，未改变其处理。新测试确认三次失败的计数跨恢复仍保持为 3。`persistence_gate_state` 不属于锁存判定所需的附属字段，未扩大保留范围。

遵循简报裁决：可信完整性恢复继续隔离事务、清自有槽、清 runtime_fault，并继续落到既有 DISARMED 状态；保留下来的 `fault_latched` 独立阻止 enable，直到既有 `repair_fault()` 成功清除锁存。未发现需要修改既有断言的冲突。

## 两条新测试及断言清单

### test_reconcile.ReconcileTests.test_sensor_stand_down_survives_trusted_recovery_without_restarting_observation

1. ModuleCase 执行 install、enable，测试实际 arm；电量降至 19 后 tick 失败、RTC 槽清空，`sensor_stand_down=True`。
2. 电量恢复至 80，enable 精确返回 `stable observation required`。
3. 完成本 boot 对账，设置 boot_completed 和 RescueX 健康条件；单调时间 100 秒时调用 enable 开始稳定观察，仍返回同一拒绝，budget 的 stable_since 与 cooldown_since 均等于当前单调时刻。
4. 观察 600 秒后写入领先账本的缓存 seq，确认 `LoadStatus.CACHE_AHEAD`，随后 trusted_recover 成功。
5. 恢复后 enable 仍精确返回 `stable observation required`；传感器标志原值、完整 budget 不变，enabled/supervisor_permit 均为 False，RTC 槽仍为空。
6. 再推进 1199 秒（距原观察起点 1799 秒），enable 仍拒绝；再推进 1 秒（满 1800 秒），enable 成功且 sensor_stand_down 清除。

### test_reconcile.ReconcileTests.test_fault_latch_survives_trusted_recovery_until_explicit_repair

1. ModuleCase 执行 install、enable，测试实际 arm；注入三次 RTC 写失败，逐次 tick 失败且 write_fail_count 依次为 1、2、3。
2. 确认状态为 FAULT、fault_latched=True、enable 被拒、RTC 槽为空。
3. 制造并确认 CACHE_AHEAD，trusted_recover 成功。
4. 恢复后 enable 精确返回 `trusted fault repair required`；锁存原值仍在，write_fail_count 仍为 3，enabled/supervisor_permit 均为 False，runtime_fault 为 False，RTC 槽为空。
5. 通过既有 repair_fault 路径成功修复，确认 fault_latched 清除，随后 enable 成功。

## 测试顺序与实测证据

基线 HEAD 为 `e375a21`。先只添加两个测试；首次运行后、修改生产代码前，`git diff --stat` 仅列 `tests/test_reconcile.py`（新增 63 行），`git diff -- deadman_sim/system.py` 为空。

### 1. 修复前：新增测试实际失败

```text
python3 -m unittest discover -s tests -p test_reconcile.py -k survives_trusted_recovery

FF
FAIL: test_fault_latch_survives_trusted_recovery_until_explicit_repair
  tests/test_reconcile.py:266
  实际：{'success': True}
  预期：{'success': False, 'error': 'trusted fault repair required'}

FAIL: test_sensor_stand_down_survives_trusted_recovery_without_restarting_observation
  tests/test_reconcile.py:237
  实际：{'success': False, 'error': 'budget exhausted'}
  预期：{'success': False, 'error': 'stable observation required'}

Ran 2 tests in 0.157s
FAILED (failures=2)
```

两项均先通过恢复前的拒绝检查和 trusted_recover 成功检查，再在恢复后的 enable 断言失败。传感器测试为验证观察连续性先进行了 boot 对账，预算已消费；原实现丢失传感器标志后跳过稳定观察检查，错误进入预算判断并返回 budget exhausted。这也是门槛丢失的实测，不将其误写成该测试中 enable 成功。

### 2. 随后修复生产代码：同一测试命令通过

仅修改 trusted_recover 的 control 保留键列表后，未调整两个新测试，运行同一命令：

```text
python3 -m unittest discover -s tests -p test_reconcile.py -k survives_trusted_recovery

..
----------------------------------------------------------------------
Ran 2 tests in 0.238s

OK
```

### 3. 全套实际结果

在本 worktree 根目录执行简报指定命令：

```text
python3 -m unittest discover -s tests

----------------------------------------------------------------------
Ran 126 tests in 71.504s

OK
```

全套 126 项，通过 126，失败 0，错误 0，跳过 0；即基线 124 项加本次新增 2 项。既有 retired_boots 恢复回归、传感器观察计时重启规则、RTC 故障修复等测试全部通过。`git diff --check` 通过。最终改动保留在 worktree，未提交。

验收状态：PASS

"""Independent regressions for the six P1a review findings."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deadman_sim.clock import SimClock
from deadman_sim.crash import CrashInjector, SimulatedCrash
from deadman_sim.ledger import HISTORY_LIMIT, State, canonical, retired_boot
from deadman_sim.system import Module
from support import ModuleCase


class ReviewRegressions(ModuleCase):
    def isolated(self, root):
        clock = SimClock()
        module = Module(root, Path(root) / 'hardware', clock)
        self.addCleanup(module.close)
        self.assertTrue(module.install()['success'])
        self.assertTrue(module.enable()['success'])
        self.assertTrue(module.tick()['success'])
        clock.advance(60)
        return module, clock

    def assert_persistence_gate(self, module, clock):
        root = module.root
        module.close()
        for next_boot in (False, True):
            if next_boot:
                clock.new_boot()
            rebuilt = Module(root, root / 'hardware', clock)
            self.addCleanup(rebuilt.close)
            self.assertFalse(rebuilt.recover()['success'])
            self.assertFalse(rebuilt.tick()['success'])
            self.assertFalse(rebuilt.resume()['success'])
            self.assertFalse(rebuilt.supervisor_restart()['success'])
            self.assertFalse(rebuilt.boot()['success'])
            self.assertTrue(rebuilt.storage.path('IO-UNCERTAIN').exists())
            self.assertEqual(rebuilt.executed_actions, [])
            rebuilt.close()
        repaired = Module(root, root / 'hardware', clock)
        self.addCleanup(repaired.close)
        self.assertTrue(repaired.repair_fault()['success'])
        self.assertIsNone(repaired.rtc.read_alarm())
        self.assertFalse(repaired.storage.path('IO-UNCERTAIN').exists())
        self.assertFalse(repaired.tick()['success'])

    def test_b1_each_append_phase_fsync_failure_survives_rebuild(self):
        for phase in ('PREPARED', 'CLEARED_VERIFIED', 'READBACK_VERIFIED', 'COMMITTED'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                module, clock = self.isolated(root)
                append = module.ledger.append
                fired = []
                def fail(**values):
                    if values.get('phase') == phase and not fired:
                        fired.append(phase)
                        module.storage.fail_next('fsync')
                    return append(**values)
                module.ledger.append = fail
                self.assertFalse(module.tick()['success'])
                self.assertEqual(fired, [phase])
                self.assert_persistence_gate(module, clock)

    def test_b1_each_atomic_file_and_error_survives_rebuild(self):
        for target in ('snapshot-', 'tail-', 'HEAD', 'state'):
            for fault in ('fsync', 'rename', 'dir_fsync'):
                with self.subTest(target=target, fault=fault), tempfile.TemporaryDirectory() as root:
                    module, clock = self.isolated(root)
                    write = module.storage.atomic_write
                    fired = []
                    def fail(name, data):
                        if name.startswith(target) and not fired:
                            fired.append(name)
                            module.storage.fail_next(fault)
                        return write(name, data)
                    module.storage.atomic_write = fail
                    def operation():
                        if target == 'state':
                            module.ledger.write_cache()
                        else:
                            module.ledger.compact()
                        return dict(success=True)
                    self.assertFalse(module._entry(operation)['success'])
                    self.assertEqual(len(fired), 1)
                    self.assert_persistence_gate(module, clock)

    def test_b1_failed_fault_record_cannot_erase_write_ahead_fence(self):
        self.arm()
        self.clock.advance(60)
        self.module.storage.fail_next('fsync', count=100)
        self.assertFalse(self.module.tick()['success'])
        self.module.close()
        self.module = self.make_module()
        self.module.storage.fail_next('fsync', count=100)
        self.assertFalse(self.module.recover()['success'])
        self.module.close()
        self.module = self.make_module()
        self.assertFalse(self.module.tick()['success'])
        self.assertIsNone(self.module.rtc.read_alarm())
        self.assertTrue(self.module.repair_fault()['success'])

    def test_b1_fence_removal_sync_failure_survives_rebuild(self):
        self.arm()
        self.clock.advance(60)
        sync = self.module.storage._sync_guard_directory
        fired = []

        def fail_removal():
            if not self.module.storage.path('IO-UNCERTAIN').exists() and not fired:
                fired.append(True)
                raise OSError(5, 'fence removal directory sync')
            sync()

        with patch.object(self.module.storage, '_sync_guard_directory', fail_removal):
            self.assertFalse(self.module.tick()['success'])
        self.assertEqual(fired, [True])
        self.assert_persistence_gate(self.module, self.clock)

    def test_m1_real_process_death_releases_instance_ownership(self):
        self.arm()
        self.module.close()
        script = '''
import json, sys
from pathlib import Path
from deadman_sim.clock import SimClock
from deadman_sim.system import Module
clock = SimClock(); clock.boot_id = sys.argv[2]
m = Module(sys.argv[1], Path(sys.argv[1]) / 'hardware', clock)
print(json.dumps(dict(recover=m.recover(), tick=m.tick())), flush=True)
sys.stdin.readline()
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root), self.clock.boot_id],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            ready = json.loads(child.stdout.readline())
            self.assertTrue(ready['recover']['success'])
            self.assertTrue(ready['tick']['success'])
            contender = self.make_module()
            seq = contender.ledger.load().seq
            self.assertFalse(contender.recover()['success'])
            self.assertFalse(contender.tick()['success'])
            self.assertEqual(contender.ledger.load().seq, seq)
            child.kill()
            child.wait(timeout=10)
            self.assertTrue(contender.recover()['success'])
            self.assertTrue(contender.tick()['success'])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_m2_stop_before_prepared_recovers_cleanup_without_commands(self):
        for entry in ('request_off', 'request_reboot', 'disarm', 'uninstall', 'request_maintenance'):
            for failure in (False, True):
                with self.subTest(entry=entry, clear_failure=failure), tempfile.TemporaryDirectory() as root:
                    module, clock = self.isolated(root)
                    module.crash = CrashInjector('before_prepared')
                    with self.assertRaises(SimulatedCrash):
                        getattr(module, entry)(*(['recovery'] if entry == 'request_maintenance' else []))
                    state = module.ledger.load().state
                    self.assertFalse(state['pending_txs'])
                    self.assertTrue(state['control']['cleanup_pending'])
                    self.assertFalse(state['control']['enabled'])
                    module.close()
                    rebuilt = Module(root, Path(root) / 'hardware', clock)
                    self.addCleanup(rebuilt.close)
                    if failure:
                        rebuilt.rtc.fail_clears = 1
                    result = rebuilt.recover()
                    self.assertEqual(rebuilt.executed_actions, [])
                    self.assertFalse(rebuilt.tick()['success'])
                    if failure:
                        self.assertFalse(result['success'])
                        self.assertEqual(rebuilt.state, State.FAULT)
                        self.assertTrue(rebuilt.ledger.load().state['control']['cleanup_pending'])
                    else:
                        self.assertTrue(result['success'])
                        self.assertIsNone(rebuilt.rtc.read_alarm())
                        self.assertFalse(rebuilt.ledger.load().state['control']['cleanup_pending'])

    def test_m3_each_sensor_recovery_rejection(self):
        for boundary in ('short', 'unhealthy', 'boot', 'rebuild'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                module, clock = self.isolated(root)
                module.sensors.temp = 45
                self.assertFalse(module.tick()['success'])
                module.sensors.temp = 25
                self.assertTrue(module.boot()['success'])
                module.boot_completed = module.rescuex_healthy = True
                self.assertTrue(module.observe_health()['success'])
                clock.advance(1799)
                if boundary == 'unhealthy':
                    module.daemon_healthy = False
                    self.assertFalse(module.observe_health()['success'])
                    module.daemon_healthy = True
                elif boundary in ('boot', 'rebuild'):
                    module.close()
                    if boundary == 'boot':
                        clock.new_boot()
                    module = Module(root, Path(root) / 'hardware', clock)
                    self.addCleanup(module.close)
                    module.boot()
                    module.boot_completed = module.rescuex_healthy = True
                self.assertFalse(module.resume()['success'])
                self.assertFalse(module.tick()['success'])
                self.assertIsNone(module.rtc.read_alarm())
                clock.advance(1 if boundary == 'short' else 1800)
                self.assertTrue(module.resume()['success'])

    def test_n1_persisted_foreign_boot_reoff_candidate_rejected_at_execution(self):
        from deadman_sim.classify import Evidence
        self.arm()
        off = self.module.request_off()
        self.new_boot(Evidence('rtc_wake', True, off['arm_id'], True, False, True),
                      dict(reoff_enabled=False))
        with self.module.lock:
            loaded = self.module.ledger.load()
            state = copy.deepcopy(loaded.state)
            state['reoff_candidates'][self.clock.boot_id]['boot_id'] = 'foreign-boot'
            self.module.ledger._switch(state, loaded.seq, loaded.checksum, loaded.generation + 1)
        self.module.close()
        self.module = self.make_module(dict(reoff_enabled=True))
        candidate = self.module.ledger.load().state['reoff_candidates'][self.clock.boot_id]
        seq = self.module.ledger.load().seq
        actions = self.module.executed_actions
        result = self.module._entry(lambda: self.module._attempt_reoff(candidate), recover=False)
        self.assertFalse(result['success'])
        self.assertEqual(self.module.ledger.load().seq, seq)
        self.assertEqual(self.module.ledger.load().state['reoff_attempts'], {})
        self.assertEqual(self.module.executed_actions, actions)

    def test_m4_completed_history_pressure_is_bounded_and_retired_boot_cannot_replay(self):
        self.arm()
        ledger = self.module.ledger
        budget = copy.deepcopy(ledger.load().state['budget'])
        unresolved = dict(tx_id='unfinished', phase='PREPARED', operation='arm',
                          arm_id='unfinished-arm', source_boot_id=self.clock.boot_id)
        with self.module.lock:
            ledger.append(**unresolved)
            self.module._record(operation='observe', state=State.ARMED)
        pending = copy.deepcopy(ledger.load().state['pending_txs'])
        sizes = []
        retired = []
        # Equivalent completed evidence from 39000 renewals and 900 boots;
        # each round independently exceeds the original failing probe size.
        for round_no in range(3):
            with self.module.lock:
                loaded = ledger.load()
                state = copy.deepcopy(loaded.state)
                state['superseded_arms'] += ['completed-arm-' + str(round_no) + '-' + str(i) + 'x' * 70
                                              for i in range(13000)]
                for i in range(300):
                    key = f'done-{round_no}-{i}'
                    boot = f'boot-{round_no}-{i}'
                    retired.append(boot)
                    record = dict(tx_id=key, arm_id=key, source_boot_id=boot)
                    state['intents'][key] = dict(record=record, consumed=True, cancelled=False)
                    state['consumed_arms'][key] = boot
                    state['reconciled_boot_ids'].append(boot)
                    state['boots'][boot] = dict(conclusion='OFF_RTC_WAKE', state='DISARMED')
                    state['reoff_candidates'][boot] = dict(intent_id=key, boot_id=boot, clear_verified=True)
                    state['reoff_attempts'][key] = 1
                    state['fault_flags'].append('old-error')
                    state['intent_wait'].append(key)
                # Include still-unresolved intent, quarantine and REOFF evidence.
                state['intents']['live'] = dict(record=dict(arm_id='live-arm'), consumed=False, cancelled=False)
                state['quarantined_intents']['live'] = 'unresolved identity'
                state['quarantined_transactions']['unresolved'] = dict(evidence='keep')
                ledger._switch(state, loaded.seq, loaded.checksum, loaded.generation + 1)
                for name in (loaded.head['snapshot'], loaded.head['tail']):
                    self.module.storage.delete(name)
                self.module.storage.delete('state') if self.module.storage.path('state').exists() else None
            loaded = ledger.load()
            state = loaded.state
            sizes.append(len(self.module.storage.read(loaded.head['snapshot'])))
            self.assertLess(sizes[-1], 100000)
            self.assertEqual(state['control']['completed_through_seq'], loaded.seq)
            self.assertEqual(state['pending_txs'], pending)
            self.assertEqual(state['budget'], budget)
            self.assertIn('live', state['intents'])
            self.assertEqual(state['quarantined_intents']['live'], 'unresolved identity')
            self.assertEqual(state['quarantined_transactions']['unresolved'], dict(evidence='keep'))
            for key in ('superseded_arms', 'consumed_arms', 'reconciled_boot_ids', 'boots',
                        'reoff_candidates', 'reoff_attempts', 'fault_flags'):
                self.assertLessEqual(len(state[key]), HISTORY_LIMIT)
            self.assertLessEqual(len(state['intents']), HISTORY_LIMIT + 1)
            self.assertLessEqual(len(state['intent_wait']), HISTORY_LIMIT)
            self.assertTrue(retired_boot(state, retired[0]))
        self.assertLess(max(sizes) - min(sizes), 2000)
        self.module.close()
        # A retired boot UUID is a conservative rejection, never a fresh boot.
        self.clock.boot_id = retired[0]
        rebuilt = self.make_module(dict(reoff_enabled=True))
        before = copy.deepcopy(rebuilt.ledger.load().state)
        seq = rebuilt.ledger.load().seq
        self.assertFalse(rebuilt.boot()['success'])
        self.assertEqual(rebuilt.ledger.load().seq, seq)
        self.assertEqual(rebuilt.ledger.load().state, before)
        self.assertEqual(rebuilt.executed_actions, [])

    def test_m4_tail_append_after_compaction_does_not_restore_pruned_history(self):
        self.arm()
        ledger = self.module.ledger
        with self.module.lock:
            for i in range(130):
                ledger.append(operation='renew', arm_id=f'a-{i}', previous_arm_id=f'a-{i-1}')
            with patch('deadman_sim.ledger.TAIL_LIMIT', 1):
                ledger.append(operation='observe')
        state = ledger.load().state
        self.assertEqual(len(state['superseded_arms']), HISTORY_LIMIT)
        self.assertEqual(state['control']['completed_through_seq'], ledger.load().seq - 1)
        self.module.close()
        rebuilt = self.make_module()
        self.assertEqual(rebuilt.ledger.load().state, state)
        self.assertTrue(rebuilt.recover()['success'])
        self.assertTrue(rebuilt.tick()['success'])

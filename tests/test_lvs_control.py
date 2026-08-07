import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))
import lvs_control as lvs  # noqa: E402


EXISTING = """global_defs {
    router_id LVS_41
}

include nginx-manager.d/*.conf

vrrp_instance VI_1 {
    state MASTER
    interface ens192
    authentication {
        auth_type PASS
        auth_pass rotate-this-secret
    }
    virtual_ipaddress {
        10.165.0.40/22
    }
}

virtual_server 10.165.0.40 443 {
    delay_loop 6
    lb_algo mh
    lb_kind DR
    protocol TCP

    real_server 10.165.0.43 443 {
        weight 1
        TCP_CHECK {
            connect_timeout 3
            nb_get_retry 3
            delay_before_retry 3
            connect_port 443
        }
    }
    real_server 10.165.0.44 443 {
        weight 1
        TCP_CHECK {
            connect_timeout 3
            nb_get_retry 3
            delay_before_retry 3
            connect_port 443
        }
    }
}
"""


class LvsControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "keepalived"
        self.root.mkdir()
        self.main = self.root / "keepalived.conf"
        self.main.write_text(EXISTING, encoding="utf-8")
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir()
        self.settings = SimpleNamespace(
            keepalived_config=str(self.main),
            keepalived_service="keepalived.service",
            keepalived_vip="10.165.0.40",
            keepalived_binary=sys.executable,
            helper_state_dir=str(self.state),
            max_file_bytes=4 * 1024 * 1024,
            command_timeout=3,
            lvs_management_enabled=True,
            lvs_managed_file=str(self.root / "nginx-manager.d" / "50-lvs-managed.conf"),
        )
        self.module = lvs.LvsControlModule(self.settings)

    @staticmethod
    def listener():
        return {"address": "10.165.0.40", "port": 443, "protocol": "TCP"}

    @classmethod
    def service(cls, weight=2):
        return {
            "name": "HTTPS VIP",
            "listener": cls.listener(),
            "scheduler": "mh",
            "forwarding": "DR",
            "delay_loop": 6,
            "persistence_seconds": None,
            "members": [
                {
                    "address": "10.165.0.43",
                    "port": 443,
                    "weight": weight,
                    "enabled": True,
                    "monitor": {
                        "kind": "tcp",
                        "connect_timeout": 3,
                        "retries": 3,
                        "delay_before_retry": 3,
                        "connect_port": 443,
                    },
                },
                {
                    "address": "10.165.0.44",
                    "port": 443,
                    "weight": 1,
                    "enabled": True,
                    "monitor": None,
                },
            ],
        }

    @classmethod
    def runtime(cls, weight=1):
        return [{
            "listener": cls.listener(),
            "scheduler": "mh",
            "members": [
                {"address": "10.165.0.43", "port": 443, "weight": weight, "forwarding": "Route"},
                {"address": "10.165.0.44", "port": 443, "weight": 1, "forwarding": "Route"},
            ],
        }]

    def payload(self, intent, adopt_existing=True):
        current = self.module.observe()
        plan_digest = hashlib.sha256(
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        payload = {
            "intent": intent,
            "expected_config_hash": current["config_hash"],
            "plan_digest": plan_digest,
        }
        if adopt_existing:
            payload["adopt_existing"] = True
        return payload

    def prepared_transaction(self, intent, job_id):
        normalized = lvs.normalize_intent(intent)
        graph = self.module._graph()
        candidates = self.module._candidate_files(graph, normalized, adopt_existing=True)
        return self.module._prepare_transaction(
            job_id,
            candidates,
            self.module._snapshot_expectations(graph, candidates),
            normalized["target"],
            lvs._target_runtime_snapshot(normalized["target"], self.runtime()),
        )

    def test_inventory_is_structured_and_does_not_expose_authentication(self):
        observation = self.module.observe()
        self.assertEqual("vrrp", observation["mode"])
        self.assertTrue(observation["management_enabled"])
        self.assertRegex(observation["config_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(1, observation["service_count"])
        service = observation["services"][0]
        self.assertEqual("mh", service["scheduler"])
        self.assertEqual("DR", service["forwarding"])
        self.assertEqual(2, len(service["members"]))
        self.assertTrue(service["editable"])
        self.assertNotIn("rotate-this-secret", json.dumps(observation))

    def test_standalone_inventory_and_apply_require_explicit_topology(self):
        self.main.write_text(EXISTING.split("vrrp_instance", 1)[0] + EXISTING.split("virtual_server", 1)[1].join(("virtual_server", "")), encoding="utf-8")
        self.settings.lvs_topology = "standalone"
        self.settings.keepalived_vip = None
        module = lvs.LvsControlModule(self.settings)
        observation = module.observe()
        self.assertEqual("standalone", observation["mode"])

        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=3),
            "change_note": "standalone update",
        }
        payload = {
            "intent": intent,
            "expected_config_hash": observation["config_hash"],
            "plan_digest": hashlib.sha256(
                json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "expected_mode": "standalone",
            "expected_topology": {"mode": "standalone"},
            "expected_role": "STANDALONE",
            "expected_vip": None,
            "adopt_existing": True,
        }
        runtime = [{
            "listener": self.listener(),
            "scheduler": "mh",
            "members": [
                {"address": "10.165.0.43", "port": 443, "weight": 3, "forwarding": "Route"},
                {"address": "10.165.0.44", "port": 443, "weight": 1, "forwarding": "Route"},
            ],
        }]
        with mock.patch.object(module, "_service_active", return_value=True), mock.patch.object(
            module, "_local_ip_addresses", return_value={"10.165.0.40"}
        ) as local_addresses, mock.patch.object(module, "_validate"), mock.patch.object(
            module, "_reload"
        ), mock.patch("lvs_control._read_ipvs_services", return_value=runtime):
            result = module.apply(payload, "standalone-job")
        self.assertTrue(result["applied"])
        self.assertGreaterEqual(local_addresses.call_count, 2)

    def test_standalone_rejects_malformed_or_inactive_topology_before_writing(self):
        self.main.write_text("virtual_server 10.165.0.40 443 { protocol TCP }\n", encoding="utf-8")
        self.settings.lvs_topology = "standalone"
        self.settings.keepalived_vip = None
        module = lvs.LvsControlModule(self.settings)
        original = self.main.read_bytes()
        intent = {
            "kind": "delete_service",
            "target": self.listener(),
            "change_note": "standalone delete",
        }
        base = {
            "intent": intent,
            "expected_config_hash": module.observe()["config_hash"],
            "plan_digest": "a" * 64,
            "expected_mode": "standalone",
            "expected_topology": {"mode": "standalone"},
            "expected_role": "STANDALONE",
            "expected_vip": None,
            "adopt_existing": True,
        }
        for override in (
            {"expected_mode": "vrrp"},
            {"expected_topology": {"mode": "standalone", "vip": None}},
            {"expected_role": "BACKUP"},
            {"expected_vip": "10.165.0.40"},
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                module.apply(dict(base, **override), "bad-standalone")
            self.assertEqual("invalid_lvs_intent", raised.exception.failure_code)
        with mock.patch.object(module, "_service_active", return_value=False):
            with self.assertRaises(lvs.LvsControlError) as raised:
                module.apply(base, "inactive-standalone")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        with mock.patch.object(module, "_service_active", return_value=True), mock.patch.object(
            module, "_local_ip_addresses", return_value=set()
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                module.apply(base, "nonlocal-standalone")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertIn("no longer a local address", str(raised.exception))
        self.assertEqual(original, self.main.read_bytes())

    def test_standalone_address_drift_before_reload_restores_original(self):
        self.main.write_text(
            EXISTING.split("vrrp_instance", 1)[0]
            + EXISTING.split("virtual_server", 1)[1].join(("virtual_server", "")),
            encoding="utf-8",
        )
        self.settings.lvs_topology = "standalone"
        self.settings.keepalived_vip = None
        module = lvs.LvsControlModule(self.settings)
        original = self.main.read_bytes()
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=3),
            "change_note": "address drift",
        }
        payload = {
            "intent": intent,
            "expected_config_hash": module.observe()["config_hash"],
            "plan_digest": "c" * 64,
            "expected_mode": "standalone",
            "expected_topology": {"mode": "standalone"},
            "expected_role": "STANDALONE",
            "expected_vip": None,
            "adopt_existing": True,
        }
        with mock.patch.object(module, "_service_active", return_value=True), mock.patch.object(
            module, "_local_ip_addresses", side_effect=[{"10.165.0.40"}, set()]
        ), mock.patch.object(module, "_validate"), mock.patch.object(
            module, "_reload"
        ) as reload_service, mock.patch("lvs_control._read_ipvs_services", return_value=[]):
            with self.assertRaises(lvs.LvsControlError) as raised:
                module.apply(payload, "address-drift-standalone")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertEqual("verify", raised.exception.failure_stage)
        # Candidate reload is skipped; the single call restores the previous
        # runtime after the candidate file was rolled back.
        reload_service.assert_called_once_with()
        self.assertEqual(original, self.main.read_bytes())

    def test_standalone_rejects_vrrp_hidden_in_include_graph(self):
        include_dir = self.root / "conf.d"
        include_dir.mkdir()
        self.main.write_text("include conf.d/*.conf\n", encoding="utf-8")
        (include_dir / "hidden.conf").write_text(
            "vrrp_instance VI_HIDDEN { virtual_ipaddress { 10.165.0.40 } }\n",
            encoding="utf-8",
        )
        self.settings.lvs_topology = "standalone"
        self.settings.keepalived_vip = None
        module = lvs.LvsControlModule(self.settings)
        intent = {
            "kind": "delete_service",
            "target": self.listener(),
            "change_note": "must remain standalone",
        }
        payload = {
            "intent": intent,
            "expected_config_hash": module.observe()["config_hash"],
            "plan_digest": "b" * 64,
            "expected_mode": "standalone",
            "expected_topology": {"mode": "standalone"},
            "expected_role": "STANDALONE",
            "expected_vip": None,
        }
        with mock.patch.object(module, "_service_active", return_value=True):
            with self.assertRaises(lvs.LvsControlError) as raised:
                module.apply(payload, "hidden-vrrp")
        self.assertEqual("concurrent_change", raised.exception.failure_code)

    def test_include_cycle_is_rejected_instead_of_treated_as_complete(self):
        include_dir = self.root / "conf.d"
        include_dir.mkdir()
        self.main.write_text("include conf.d/one.conf\n", encoding="utf-8")
        (include_dir / "one.conf").write_text("include ../keepalived.conf\n", encoding="utf-8")
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.observe()
        self.assertEqual("lvs_config_unsupported", raised.exception.failure_code)

    def test_unknown_virtual_service_directive_makes_only_that_service_read_only(self):
        self.main.write_text(EXISTING.replace("    protocol TCP\n", "    protocol TCP\n    quorum 2\n"), encoding="utf-8")
        service = self.module.observe()["services"][0]
        self.assertFalse(service["editable"])
        self.assertIn("quorum", service["unsupported_directives"])

    def test_unmodeled_member_directive_is_read_only_instead_of_being_dropped(self):
        self.main.write_text(
            EXISTING.replace("        weight 1\n", "        weight 1\n        inhibit_on_failure\n", 1),
            encoding="utf-8",
        )
        service = self.module.observe()["services"][0]
        self.assertFalse(service["editable"])
        self.assertIn("real_server.inhibit_on_failure", service["unsupported_directives"])

    def test_intent_rejects_raw_configuration_and_command_fields(self):
        with self.assertRaises(lvs.LvsControlError) as raised:
            lvs.normalize_intent({
                "kind": "upsert_service",
                "target": self.listener(),
                "service": self.service(),
                "command": "rm -rf /",
            })
        self.assertEqual("invalid_lvs_intent", raised.exception.failure_code)
        with self.assertRaises(lvs.LvsControlError):
            lvs.normalize_service({**self.service(), "raw_config": "auth_pass secret"})

    def test_intent_rejects_listener_rename_and_requires_create_delete(self):
        service = self.service()
        service["listener"] = {"address": "10.165.0.40", "port": 8443, "protocol": "TCP"}
        with self.assertRaises(lvs.LvsControlError) as raised:
            lvs.normalize_intent({
                "kind": "upsert_service",
                "target": self.listener(),
                "service": service,
                "change_note": "do not rename a listener in place",
            })
        self.assertEqual("invalid_lvs_intent", raised.exception.failure_code)

    def test_apply_replaces_only_target_block_and_preserves_vrrp_and_secret(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=3),
            "change_note": "increase server weight",
        }
        payload = {**self.payload(intent), "adopt_existing": True}
        runtime = [{
            "listener": self.listener(),
            "scheduler": "mh",
            "members": [
                {"address": "10.165.0.43", "port": 443, "weight": 3, "forwarding": "Route"},
                {"address": "10.165.0.44", "port": 443, "weight": 1, "forwarding": "Route"},
            ],
        }]
        with mock.patch.object(self.module, "_validate"), mock.patch.object(self.module, "_reload"), mock.patch(
            "lvs_control._read_ipvs_services", return_value=runtime
        ):
            result = self.module.apply(payload, "job-1")
        self.assertTrue(result["applied"])
        updated = self.main.read_text(encoding="utf-8")
        managed = Path(self.settings.lvs_managed_file).read_text(encoding="utf-8")
        self.assertIn("auth_pass rotate-this-secret", updated)
        self.assertNotIn("virtual_server 10.165.0.40 443", updated)
        self.assertIn("weight 3", managed)
        self.assertIn("# nginx-manager: managed", managed)
        observed = self.module.observe()["services"][0]
        self.assertEqual("HTTPS VIP", observed["name"])
        self.assertEqual("managed", observed["origin"])
        self.assertEqual(result, self.module.committed_result(payload, "job-1"))

    def test_existing_service_requires_explicit_takeover(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=3),
            "change_note": "not acknowledged",
        }
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module._candidate_files(self.module._graph(), lvs.normalize_intent(intent))
        self.assertEqual("lvs_takeover_required", raised.exception.failure_code)

        delete_intent = lvs.normalize_intent({
            "kind": "delete_service",
            "target": self.listener(),
            "change_note": "must adopt first",
        })
        with self.assertRaises(lvs.LvsControlError) as delete_raised:
            self.module._candidate_files(
                self.module._graph(), delete_intent, adopt_existing=True
            )
        self.assertEqual("lvs_takeover_required", delete_raised.exception.failure_code)

    def test_managed_service_rewrite_does_not_duplicate_control_headers(self):
        intent = lvs.normalize_intent({
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=3),
            "change_note": "take over",
        })
        adopted = self.module._candidate_files(
            self.module._graph(), intent, adopt_existing=True
        )
        for path, data in adopted.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        updated_intent = lvs.normalize_intent({
            **intent,
            "service": self.service(weight=4),
            "change_note": "edit managed service",
        })
        rewritten = self.module._candidate_files(self.module._graph(), updated_intent)
        managed = rewritten[Path(self.settings.lvs_managed_file).resolve()].decode("utf-8")
        self.assertEqual(1, managed.count("# nginx-manager: managed"))
        self.assertEqual(1, managed.count("# name: HTTPS VIP"))

    def test_committed_result_is_bound_to_job_payload_and_current_graph(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=4),
            "change_note": "durable result",
        }
        payload = self.payload(intent)
        runtime = [{
            "listener": self.listener(),
            "scheduler": "mh",
            "members": [
                {"address": "10.165.0.43", "port": 443, "weight": 4, "forwarding": "Route"},
                {"address": "10.165.0.44", "port": 443, "weight": 1, "forwarding": "Route"},
            ],
        }]
        with mock.patch.object(self.module, "_validate"), mock.patch.object(self.module, "_reload"), mock.patch(
            "lvs_control._read_ipvs_services", return_value=runtime
        ):
            expected = self.module.apply(payload, "durable-job")
        self.assertEqual(expected, self.module.committed_result(payload, "durable-job"))
        self.assertIsNone(self.module.committed_result({**payload, "plan_digest": "0" * 64}, "durable-job"))
        self.assertIsNone(self.module.committed_result(payload, "another-job"))
        self.main.write_text(self.main.read_text(encoding="utf-8") + "\n# external change\n", encoding="utf-8")
        self.assertIsNone(self.module.committed_result(payload, "durable-job"))

    def test_validation_failure_restores_original_file(self):
        original = self.main.read_bytes()
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=7),
            "change_note": "bad candidate",
        }
        payload = self.payload(intent)
        with mock.patch.object(
            self.module,
            "_validate",
            side_effect=[
                lvs.LvsControlError(
                    "candidate invalid", "keepalived_config_test_failed", "validate"
                ),
                None,
            ],
        ), mock.patch.object(self.module, "_reload") as reload_mock, mock.patch(
            "lvs_control._read_ipvs_services", return_value=self.runtime()
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module.apply(payload, "job-2")
        self.assertEqual("keepalived_config_test_failed", raised.exception.failure_code)
        self.assertTrue(raised.exception.rolled_back)
        reload_mock.assert_called_once_with()
        self.assertEqual(original, self.main.read_bytes())

    def test_expected_backup_role_is_rechecked_before_reload(self):
        original = self.main.read_bytes()
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=5),
            "change_note": "backup first",
        }
        payload = {
            **self.payload(intent),
            "expected_role": "BACKUP",
            "expected_vip": "10.165.0.40",
        }
        with mock.patch.object(self.module, "_service_active", return_value=True), mock.patch.object(
            self.module,
            "_local_ip_addresses",
            side_effect=[set(), {"10.165.0.40"}],
        ), mock.patch.object(self.module, "_validate") as validate_mock, mock.patch.object(
            self.module, "_reload"
        ) as reload_mock, mock.patch(
            "lvs_control._read_ipvs_services", return_value=self.runtime()
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module.apply(payload, "role-change")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertTrue(raised.exception.rolled_back)
        self.assertEqual(2, validate_mock.call_count)
        reload_mock.assert_called_once_with()
        self.assertEqual(original, self.main.read_bytes())

    def test_expected_role_mismatch_is_rejected_before_writing(self):
        original = self.main.read_bytes()
        intent = {
            "kind": "delete_service",
            "target": self.listener(),
            "change_note": "backup only",
        }
        payload = {
            **self.payload(intent),
            "expected_role": "BACKUP",
            "expected_vip": "10.165.0.40",
        }
        with mock.patch.object(self.module, "_service_active", return_value=True), mock.patch.object(
            self.module, "_local_ip_addresses", return_value={"10.165.0.40"}
        ), mock.patch.object(self.module, "_validate") as validate_mock:
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module.apply(payload, "wrong-role")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertFalse(raised.exception.rolled_back)
        validate_mock.assert_not_called()
        self.assertEqual(original, self.main.read_bytes())

    def test_recovery_defers_reload_when_keepalived_is_inactive(self):
        intent = lvs.normalize_intent({
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=8),
            "change_note": "interrupted reload",
        })
        candidates = self.module._candidate_files(
            self.module._graph(), intent, adopt_existing=True
        )
        graph = self.module._graph()
        transaction_dir, manifest = self.module._prepare_transaction(
            "cold-recovery",
            candidates,
            self.module._snapshot_expectations(graph, candidates),
            intent["target"],
            lvs._target_runtime_snapshot(intent["target"], self.runtime()),
        )
        self.module._replace_from_transaction(transaction_dir, manifest, candidate=True)
        manifest["phase"] = "reloading"
        manifest["reload_attempted"] = True
        self.module._write_manifest(transaction_dir, manifest)
        with mock.patch.object(self.module, "_validate") as validate_mock, mock.patch.object(
            self.module, "_service_active", return_value=False
        ), mock.patch.object(self.module, "_reload") as reload_mock:
            self.assertEqual(1, self.module.recover())
        validate_mock.assert_called_once_with()
        reload_mock.assert_not_called()
        recovered = json.loads((transaction_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("recovered", recovered["phase"])
        self.assertEqual("deferred_service_inactive", recovered["runtime_verification"])
        self.assertEqual(EXISTING, self.main.read_text(encoding="utf-8"))

    def test_runtime_restore_verification_fails_closed(self):
        manifest = {
            "runtime_target": self.listener(),
            "pre_runtime_services": lvs._target_runtime_snapshot(self.listener(), self.runtime()),
        }
        changed = self.runtime(weight=9)
        with mock.patch("lvs_control._read_ipvs_services", return_value=changed), mock.patch(
            "lvs_control.time.monotonic", side_effect=[0.0, 4.0]
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module._verify_restored_runtime(manifest, "rollback")
        self.assertEqual("rollback_failed", raised.exception.failure_code)
        self.assertEqual("rollback", raised.exception.failure_stage)

    def test_apply_never_reports_restored_when_runtime_rollback_is_unverified(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=7),
            "change_note": "candidate fails",
        }
        payload = self.payload(intent)
        with mock.patch.object(
            self.module,
            "_validate",
            side_effect=[
                lvs.LvsControlError(
                    "candidate invalid", "keepalived_config_test_failed", "validate"
                ),
                None,
            ],
        ), mock.patch.object(self.module, "_reload"), mock.patch(
            "lvs_control._read_ipvs_services", return_value=self.runtime()
        ), mock.patch(
            "lvs_control.time.monotonic", side_effect=[0.0, 4.0]
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module.apply(payload, "unverified-rollback")
        self.assertEqual("rollback_failed", raised.exception.failure_code)
        self.assertFalse(raised.exception.rolled_back)
        self.assertEqual("rollback", raised.exception.failure_stage)

    def test_new_service_is_written_only_to_managed_file(self):
        new_listener = {"address": "10.165.0.40", "port": 8443, "protocol": "TCP"}
        new_service = self.service()
        new_service["listener"] = new_listener
        new_service["name"] = "Admin HTTPS"
        intent = {
            "kind": "upsert_service",
            "target": new_listener,
            "service": new_service,
            "change_note": "new virtual service",
        }
        graph = self.module._graph()
        candidates = self.module._candidate_files(graph, lvs.normalize_intent(intent))
        managed_path = Path(self.settings.lvs_managed_file).resolve()
        self.assertEqual([managed_path], list(candidates))
        self.assertIn(b"virtual_server 10.165.0.40 8443", candidates[managed_path])
        self.assertEqual(EXISTING, self.main.read_text(encoding="utf-8"))

    def test_concurrent_change_is_rejected_before_writing(self):
        intent = {
            "kind": "delete_service",
            "target": self.listener(),
            "change_note": "delete",
        }
        payload = self.payload(intent)
        payload["expected_config_hash"] = "0" * 64
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.apply(payload, "job-3")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertIn("virtual_server 10.165.0.40 443", self.main.read_text(encoding="utf-8"))

    def test_prepare_rejects_manual_edit_after_candidate_snapshot_without_overwriting_it(self):
        intent = lvs.normalize_intent({
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "manual race",
        })
        graph = self.module._graph()
        candidates = self.module._candidate_files(graph, intent, adopt_existing=True)
        expectations = self.module._snapshot_expectations(graph, candidates)
        manually_edited = self.main.read_bytes() + b"\n# manual edit wins\n"
        self.main.write_bytes(manually_edited)
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module._prepare_transaction(
                "manual-race",
                candidates,
                expectations,
                intent["target"],
                lvs._target_runtime_snapshot(intent["target"], self.runtime()),
            )
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertEqual("prepare", raised.exception.failure_stage)
        self.assertEqual(manually_edited, self.main.read_bytes())
        self.assertFalse(self.module.state_dir.exists())

    def test_apply_aborts_without_rollback_when_graph_changes_after_backup(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "late manual race",
        }
        payload = self.payload(intent)
        original_prepare = self.module._prepare_transaction
        manually_edited = self.main.read_bytes() + b"\n# late manual edit wins\n"

        def prepare_then_edit(*args, **kwargs):
            prepared = original_prepare(*args, **kwargs)
            self.main.write_bytes(manually_edited)
            return prepared

        with mock.patch.object(self.module, "_prepare_transaction", side_effect=prepare_then_edit), mock.patch(
            "lvs_control._read_ipvs_services", return_value=self.runtime()
        ):
            with self.assertRaises(lvs.LvsControlError) as raised:
                self.module.apply(payload, "late-manual-race")
        self.assertEqual("concurrent_change", raised.exception.failure_code)
        self.assertFalse(raised.exception.rolled_back)
        self.assertEqual(manually_edited, self.main.read_bytes())
        transaction_dir = self.module.state_dir / self.module._transaction_id("late-manual-race")
        manifest = json.loads((transaction_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual("aborted", manifest["phase"])

    def test_recovery_blocks_corrupt_nonterminal_manifest(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "corrupt manifest",
        }
        transaction_dir, _manifest = self.prepared_transaction(intent, "corrupt-manifest")
        (transaction_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.recover()
        self.assertEqual("rollback_failed", raised.exception.failure_code)
        self.assertEqual("recovery", raised.exception.failure_stage)

    def test_recovery_rejects_tampered_backup_hash_and_metadata(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "tampered recovery",
        }
        transaction_dir, manifest = self.prepared_transaction(intent, "tampered-backup")
        (transaction_dir / manifest["files"][0]["backup"]).write_bytes(b"tampered")
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.recover()
        self.assertEqual("rollback_failed", raised.exception.failure_code)

        # A fresh transaction proves numeric ownership metadata is also bounded.
        shutil.rmtree(str(transaction_dir))
        transaction_dir, manifest = self.prepared_transaction(intent, "tampered-metadata")
        manifest["files"][0]["uid"] = -1
        (transaction_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.recover()
        self.assertEqual("rollback_failed", raised.exception.failure_code)

    def test_recovery_rejects_hardlinked_backup(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "hardlink recovery",
        }
        transaction_dir, manifest = self.prepared_transaction(intent, "hardlinked-backup")
        backup = transaction_dir / manifest["files"][0]["backup"]
        linked = self.root / "backup-hardlink"
        try:
            linked.hardlink_to(backup)
        except (OSError, NotImplementedError):
            self.skipTest("hard links are unavailable on this filesystem")
        self.addCleanup(lambda: linked.exists() and linked.unlink())
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.recover()
        self.assertEqual("rollback_failed", raised.exception.failure_code)

    def test_recovery_never_overwrites_an_unrecorded_manual_edit(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "manual edit after crash",
        }
        _transaction_dir, _manifest = self.prepared_transaction(intent, "manual-after-crash")
        manually_edited = self.main.read_bytes() + b"\n# operator repair\n"
        self.main.write_bytes(manually_edited)
        with self.assertRaises(lvs.LvsControlError) as raised:
            self.module.recover()
        self.assertEqual("rollback_failed", raised.exception.failure_code)
        self.assertEqual(manually_edited, self.main.read_bytes())

    def test_recovery_rejects_target_and_backup_path_escape(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "path escape",
        }
        transaction_dir, manifest = self.prepared_transaction(intent, "target-escape")
        outside = Path(self.temporary.name) / "outside.conf"
        outside.write_text("do not touch\n", encoding="utf-8")
        manifest["files"][0]["path"] = str(outside.resolve())
        (transaction_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        with self.assertRaises(lvs.LvsControlError):
            self.module.recover()
        self.assertEqual("do not touch\n", outside.read_text(encoding="utf-8"))

        shutil.rmtree(str(transaction_dir))
        transaction_dir, manifest = self.prepared_transaction(intent, "backup-escape")
        manifest["files"][0]["backup"] = "../outside.conf"
        (transaction_dir / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        with self.assertRaises(lvs.LvsControlError):
            self.module.recover()
        self.assertEqual("do not touch\n", outside.read_text(encoding="utf-8"))

    def test_terminal_transaction_retention_is_bounded(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(weight=6),
            "change_note": "retention",
        }
        for index in range(3):
            transaction_dir, manifest = self.prepared_transaction(intent, "terminal-{}".format(index))
            manifest["phase"] = "aborted"
            self.module._write_manifest(transaction_dir, manifest)
        with mock.patch.object(lvs, "MAX_RETAINED_TERMINAL_TRANSACTIONS", 2):
            self.assertEqual(0, self.module.recover())
        self.assertEqual(2, len(list(self.module.state_dir.iterdir())))

    def test_runtime_verification_rejects_extra_member_and_wrong_forwarding(self):
        intent = {
            "kind": "upsert_service",
            "target": self.listener(),
            "service": self.service(),
            "change_note": "verify exact runtime",
        }
        runtime = [{
            "listener": self.listener(),
            "scheduler": "mh",
            "members": [
                {"address": "10.165.0.43", "port": 443, "weight": 2, "forwarding": "Route"},
                {"address": "10.165.0.44", "port": 443, "weight": 1, "forwarding": "Route"},
                {"address": "10.165.0.99", "port": 443, "weight": 1, "forwarding": "Route"},
            ],
        }]
        self.assertFalse(lvs._runtime_matches(lvs.normalize_intent(intent), runtime))
        runtime[0]["members"].pop()
        runtime[0]["members"][0]["forwarding"] = "Masq"
        self.assertFalse(lvs._runtime_matches(lvs.normalize_intent(intent), runtime))
        runtime[0]["members"][0]["forwarding"] = "Route"
        self.assertTrue(lvs._runtime_matches(lvs.normalize_intent(intent), runtime))

        # A TCP_CHECK member may be absent while unhealthy; that is pool health,
        # not evidence that Keepalived rejected the published service.
        runtime[0]["members"].pop(0)
        self.assertTrue(lvs._runtime_matches(lvs.normalize_intent(intent), runtime))

    def test_strict_ipvs_observation_never_treats_unavailable_as_empty(self):
        missing = self.root / "missing-ip-vs"
        self.assertEqual([], lvs._read_ipvs_services(str(missing)))
        with self.assertRaises(lvs.LvsControlError) as raised:
            lvs._read_ipvs_services(str(missing), strict=True)
        self.assertEqual("ipvs_observation_unavailable", raised.exception.failure_code)

    def test_procfs_reader_decodes_network_order_ipv4_and_bracketed_ipv6(self):
        table = self.root / "ip_vs"
        table.write_text(
            "IP Virtual Server version 1.2.1 (size=4096)\n"
            "Prot LocalAddress:Port Scheduler Flags\n"
            "  -> RemoteAddress:Port Forward Weight ActiveConn InActConn\n"
            "TCP  0AA50028:01BB mh\n"
            "  -> 0AA5002B:01BB Route 1 0 0\n"
            "TCP  [2001:db8::40]:01BB rr\n"
            "  -> [2001:db8::43]:01BB Route 2 0 0\n",
            encoding="utf-8",
        )
        services = lvs._read_ipvs_services(str(table))
        self.assertEqual("10.165.0.40", services[0]["listener"]["address"])
        self.assertEqual("10.165.0.43", services[0]["members"][0]["address"])
        self.assertEqual("2001:db8::40", services[1]["listener"]["address"])
        self.assertEqual("2001:db8::43", services[1]["members"][0]["address"])


if __name__ == "__main__":
    unittest.main()

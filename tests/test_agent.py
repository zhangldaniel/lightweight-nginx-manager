import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))
import nginx_agent as agent  # noqa: E402


class SimulatedPowerLoss(BaseException):
    """Bypasses normal Exception rollback like SIGKILL or sudden power loss."""


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "nginx"
        self.root.mkdir()
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir()
        self.helper_state = Path(self.temporary.name) / "helper-state"
        self.helper_state.mkdir()
        self.main_config = self.root / "nginx.conf"
        self.main_config.write_text("events {}\nhttp {}\n", encoding="utf-8")
        self.config_root = self.root / "nginx-manager.d"
        self.config_root.mkdir()
        self.certificate_root = self.root / "ssl" / "nginx-manager"
        self.certificate_root.mkdir(parents=True)
        self.settings = agent.Settings(
            server_url="https://manager.example.test",
            node_name="test-node",
            nginx_binary=str(Path(sys.executable).resolve()),
            nginx_config=str(self.main_config),
            nginx_root=str(self.root),
            allowed_config_roots=[str(self.config_root)],
            allowed_certificate_roots=[str(self.certificate_root)],
            state_dir=str(self.state),
            helper_state_dir=str(self.helper_state),
            helper_socket=str(Path(self.temporary.name) / "helper.sock"),
            health_check=None,
        )
        self.store = agent.JobStore(self.state / "jobs.json")
        self.executor = agent.JobExecutor(self.settings, self.store)
        self.executor._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        self.executor._reload_only = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "reloaded"})

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()

    def job(self, job_id, action, payload):
        return {"id": job_id, "action": action, "payload": payload, "expires_at": "2099-01-01T00:00:00Z"}

    def test_config_apply_is_atomic_and_idempotent(self):
        target = self.config_root / "site.conf"
        old = b"server { listen 80; }\n"
        new = b"server { listen 8080; }\n"
        target.write_bytes(old)
        job = self.job(
            "job-apply-1",
            "config_apply",
            {
                "path": str(target),
                "content": new.decode(),
                "expected_sha256": self.sha(old),
                "new_sha256": self.sha(new),
                "reload": True,
            },
        )
        first = self.executor.execute(job)
        self.assertEqual("succeeded", first["status"])
        self.assertEqual(new, target.read_bytes())
        self.assertTrue(first["result"]["applied"])
        self.assertTrue(first["result"]["reloaded"])

        # Re-delivery must return the cached result and must not touch a later edit.
        later = b"server { listen 9090; }\n"
        target.write_bytes(later)
        second = self.executor.execute(job)
        self.assertEqual(first, second)
        self.assertEqual(later, target.read_bytes())
        self.assertEqual(1, self.executor._nginx_test.call_count)

    def test_config_inventory_lists_only_conf_files_without_mutation(self):
        first = self.config_root / "api.example.conf"
        first_content = b"server { listen 80; server_name api.example.com; }\n"
        first.write_bytes(first_content)
        (self.config_root / "api.example.conf.bak").write_text("ignored", encoding="utf-8")
        nested = self.config_root / "nested"
        nested.mkdir()
        second = nested / "static.example.conf"
        second_content = b"server { listen 80; server_name static.example.com; }\n"
        second.write_bytes(second_content)
        private = self.config_root / "unsafe.conf"
        private.write_text("-----BEGIN PRIVATE KEY-----\nrefused\n", encoding="utf-8")

        response = self.executor.execute(self.job("job-inventory", "config_inventory", {}))

        self.assertEqual("succeeded", response["status"])
        inventory = response["result"]
        self.assertEqual(2, inventory["file_count"])
        self.assertEqual(1, inventory["skipped_count"])
        self.assertFalse(inventory["truncated"])
        by_name = {Path(item["path"]).name: item for item in inventory["files"]}
        self.assertEqual(first_content.decode(), by_name[first.name]["content"])
        self.assertEqual(self.sha(first_content), by_name[first.name]["sha256"])
        self.assertEqual(second_content.decode(), by_name[second.name]["content"])
        self.assertNotIn("api.example.conf.bak", by_name)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        mapped = agent._to_server_result(response)
        self.assertEqual("config_inventory", mapped["action"])
        self.assertEqual(2, mapped["details"]["file_count"])
        self.assertEqual(2, len(mapped["details"]["files"]))

    def test_certificate_inventory_pairs_referenced_files_without_returning_private_key(self):
        certificate_path = self.certificate_root / "int.example.pem"
        private_key_path = self.certificate_root / "different-name.key"
        certificate_data = b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"
        private_key_data = b"-----BEGIN PRIVATE KEY-----\nsuper-secret-key-material\n-----END PRIVATE KEY-----\n"
        certificate_path.write_bytes(certificate_data)
        private_key_path.write_bytes(private_key_data)
        (self.certificate_root / "orphan.crt").write_bytes(certificate_data)
        (self.config_root / "tls.conf").write_text(
            "server { listen 443 ssl; ssl_certificate %s; ssl_certificate_key %s; }\n" % (
                certificate_path,
                private_key_path,
            ),
            encoding="utf-8",
        )
        self.executor._openssl = mock.Mock(return_value=b"matching-public-key")
        self.executor._certificate_metadata = mock.Mock(return_value={
            "domains": ["int.example.test"],
            "subject": "int.example.test",
            "issuer": "Test CA",
            "not_after": "2027-01-01T00:00:00Z",
            "days_remaining": 170,
            "fingerprint": ":".join(["AA"] * 32),
        })

        response = self.executor.execute(self.job("job-certificate-inventory", "certificate_inventory", {}))

        self.assertEqual("succeeded", response["status"])
        inventory = response["result"]
        self.assertEqual(1, inventory["certificate_count"])
        self.assertEqual(1, inventory["skipped_count"])
        item = inventory["certificates"][0]
        self.assertTrue(Path(item["certificate_path"]).samefile(certificate_path))
        self.assertTrue(Path(item["private_key_path"]).samefile(private_key_path))
        self.assertEqual(self.sha(certificate_data), item["certificate_sha256"])
        self.assertEqual(self.sha(private_key_data), item["key_material_sha256"])
        mapped = agent._to_server_result(response)
        self.assertEqual("certificate_inventory", mapped["action"])
        self.assertNotIn("super-secret-key-material", json.dumps(mapped))

    def test_config_apply_present_replaces_existing_but_never_creates(self):
        target = self.config_root / "migrated-site.conf"
        old = b"server { listen 80; }\n"
        new = b"server { listen 8080; }\n"
        target.write_bytes(old)
        replaced = self.executor.execute(
            self.job(
                "job-replace-present",
                "config_apply",
                {
                    "path": str(target),
                    "content": new.decode(),
                    "expected_sha256": "present",
                    "reload": True,
                },
            )
        )
        self.assertEqual("succeeded", replaced["status"])
        self.assertEqual(self.sha(old), replaced["result"]["previous_sha256"])
        self.assertEqual(new, target.read_bytes())

        missing = self.config_root / "missing-site.conf"
        refused = self.executor.execute(
            self.job(
                "job-replace-missing",
                "config_apply",
                {
                    "path": str(missing),
                    "content": new.decode(),
                    "expected_sha256": "present",
                    "reload": True,
                },
            )
        )
        self.assertEqual("failed", refused["status"])
        self.assertIn("expected an existing file", refused["error"])
        self.assertFalse(missing.exists())

    def test_config_delete_requires_exact_hash_then_tests_and_reloads(self):
        target = self.config_root / "delete-me.conf"
        original = b"server { listen 8080; }\n"
        target.write_bytes(original)
        response = self.executor.execute(
            self.job(
                "job-delete-1",
                "config_delete",
                {"path": str(target), "expected_sha256": self.sha(original), "reload": True},
            )
        )
        self.assertEqual("succeeded", response["status"])
        self.assertFalse(target.exists())
        self.assertTrue(response["result"]["deleted"])
        self.assertEqual(self.sha(original), response["result"]["previous_sha256"])
        self.executor._nginx_test.assert_called_once()
        self.executor._reload_only.assert_called_once()

        mapped = agent._to_server_result(response)
        self.assertTrue(mapped["details"]["deleted"])
        self.assertEqual(self.sha(original), mapped["details"]["previous_config_hash"])

    def test_config_delete_refuses_non_exact_or_stale_hash(self):
        target = self.config_root / "protected.conf"
        original = b"server { listen 8080; }\n"
        target.write_bytes(original)
        for job_id, expected in (("job-delete-present", "present"), ("job-delete-stale", "0" * 64)):
            response = self.executor.execute(
                self.job(job_id, "config_delete", {"path": str(target), "expected_sha256": expected})
            )
            self.assertEqual("failed", response["status"])
            self.assertEqual(original, target.read_bytes())
        self.assertIn("exact current SHA-256", self.store.get("job-delete-present")["response"]["error"])
        self.assertIn("does not match", self.store.get("job-delete-stale")["response"]["error"])
        self.executor._nginx_test.assert_not_called()
        self.executor._reload_only.assert_not_called()

    def test_config_delete_failed_nginx_test_restores_original(self):
        target = self.config_root / "restore-delete.conf"
        original = b"server { listen 8080; }\n"
        target.write_bytes(original)
        self.executor._nginx_test.side_effect = [
            agent.CommandError("configuration requires the deleted file"),
            {"exit_code": 0, "stdout": "", "stderr": "restored syntax is ok"},
        ]
        response = self.executor.execute(
            self.job(
                "job-delete-invalid",
                "config_delete",
                {"path": str(target), "expected_sha256": self.sha(original), "reload": True},
            )
        )
        self.assertEqual("failed", response["status"])
        self.assertIn("restored", response["error"])
        self.assertEqual(original, target.read_bytes())
        self.executor._reload_only.assert_not_called()

    def test_config_delete_power_loss_recovers_original(self):
        target = self.config_root / "power-delete.conf"
        original = b"server { listen 8080; }\n"
        target.write_bytes(original)
        original_persist = self.executor._persist_manifest

        def lose_power_after_delete(path, manifest):
            if manifest["phase"] == "replacing" and manifest["files"][0]["replaced"]:
                raise SimulatedPowerLoss()
            return original_persist(path, manifest)

        with mock.patch.object(self.executor, "_persist_manifest", side_effect=lose_power_after_delete):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-delete-power",
                        "config_delete",
                        {"path": str(target), "expected_sha256": self.sha(original), "reload": True},
                    )
                )
        self.assertFalse(target.exists())

        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-delete-jobs.json"))
        recovery._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        recovery._reload_only = mock.Mock()
        self.assertEqual(1, recovery.recover_incomplete_transactions())
        self.assertEqual(original, target.read_bytes())
        recovery._nginx_test.assert_called_once()
        recovery._reload_only.assert_not_called()

    def test_validate_only_tests_candidate_then_restores_original(self):
        target = self.config_root / "site.conf"
        old = b"old-config\n"
        candidate = b"server { listen 8080; }\n"
        target.write_bytes(old)

        def assert_candidate_visible():
            self.assertEqual(candidate, target.read_bytes())
            return {"exit_code": 0, "stdout": "", "stderr": "syntax is ok"}

        self.executor._nginx_test.side_effect = assert_candidate_visible
        result = self.executor.execute(
            self.job(
                "job-validate-1",
                "config_apply",
                {
                    "path": str(target),
                    "content": candidate.decode(),
                    "expected_sha256": self.sha(old),
                    "validate_only": True,
                    "reload": True,
                },
            )
        )
        self.assertEqual("succeeded", result["status"])
        self.assertFalse(result["result"]["applied"])
        self.assertFalse(result["result"]["reloaded"])
        self.assertEqual(old, target.read_bytes())
        self.executor._reload_only.assert_not_called()

    def test_failed_nginx_test_restores_original(self):
        target = self.config_root / "site.conf"
        old = b"working\n"
        target.write_bytes(old)
        self.executor._nginx_test.side_effect = [
            agent.CommandError("invalid nginx syntax"),
            {"exit_code": 0, "stdout": "", "stderr": "restored syntax is ok"},
        ]
        response = self.executor.execute(
            self.job(
                "job-invalid-1",
                "config_apply",
                {
                    "path": str(target),
                    "content": "server { listen 8081; }\n",
                    "expected_sha256": self.sha(old),
                    "reload": False,
                },
            )
        )
        self.assertEqual("failed", response["status"])
        self.assertIn("restored", response["error"])
        self.assertIn("phase testing", response["error"])
        self.assertEqual("nginx_config_test_failed", response["failure_code"])
        self.assertEqual("nginx_test", response["failure_stage"])
        self.assertEqual("restored", response["rollback_status"])
        self.assertEqual(old, target.read_bytes())

        mapped = agent._to_server_result(response)
        self.assertEqual("failed", mapped["status"])
        self.assertEqual("nginx_config_test_failed", mapped["details"]["failure_code"])
        self.assertEqual("nginx_test", mapped["details"]["failure_stage"])
        self.assertEqual("restored", mapped["details"]["rollback_status"])

    def test_failed_reload_is_distinct_from_nginx_test_failure(self):
        target = self.config_root / "reload-failure.conf"
        old = b"server { listen 8080; }\n"
        target.write_bytes(old)
        self.executor._reload_only.side_effect = agent.CommandError("nginx reload command failed")
        self.executor._nginx_is_running = mock.Mock(return_value=False)

        response = self.executor.execute(
            self.job(
                "job-reload-failure",
                "config_apply",
                {
                    "path": str(target),
                    "content": "server { listen 8081; }\n",
                    "expected_sha256": self.sha(old),
                    "reload": True,
                },
            )
        )

        self.assertEqual("failed", response["status"])
        self.assertEqual("nginx_reload_failed", response["failure_code"])
        self.assertEqual("reload", response["failure_stage"])
        self.assertEqual("restored", response["rollback_status"])
        self.assertEqual(old, target.read_bytes())

        mapped = agent._to_server_result(response)
        self.assertEqual("nginx_reload_failed", mapped["details"]["failure_code"])
        self.assertEqual("reload", mapped["details"]["failure_stage"])
        self.assertEqual("restored", mapped["details"]["rollback_status"])

    def test_power_loss_between_certificate_replaces_recovers_both_files(self):
        self.executor._verify_certificate_pair = mock.Mock(return_value="AA:BB:CC")
        cert_path = self.certificate_root / "fullchain.pem"
        key_path = self.certificate_root / "privkey.pem"
        old_cert = b"-----BEGIN CERTIFICATE-----\nOLD-CERT\n-----END CERTIFICATE-----\n"
        old_key = b"-----BEGIN PRIVATE KEY-----\nOLD-PRIVATE\n-----END PRIVATE KEY-----\n"
        new_cert = "-----BEGIN CERTIFICATE-----\nNEW-CERT\n-----END CERTIFICATE-----\n"
        new_key = "-----BEGIN PRIVATE KEY-----\nNEW-PRIVATE\n-----END PRIVATE KEY-----\n"
        cert_path.write_bytes(old_cert)
        key_path.write_bytes(old_key)

        original_persist = self.executor._persist_manifest

        def lose_power_after_first_replace(path, manifest):
            if manifest["phase"] == "replacing" and manifest["files"][0]["replaced"]:
                raise SimulatedPowerLoss()
            return original_persist(path, manifest)

        with mock.patch.object(self.executor, "_persist_manifest", side_effect=lose_power_after_first_replace):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-cert",
                        "certificate_apply",
                        {
                            "certificate": {
                                "path": str(cert_path),
                                "pem": new_cert,
                                "expected_sha256": self.sha(old_cert),
                            },
                            "private_key": {
                                "path": str(key_path),
                                "pem": new_key,
                                "expected_sha256": self.sha(old_key),
                            },
                            "reload": True,
                        },
                    )
                )

        self.assertEqual(new_cert.encode(), cert_path.read_bytes())
        self.assertEqual(old_key, key_path.read_bytes())
        self.assertEqual(1, len(list((self.helper_state / "transactions").glob("tx-*.json"))))

        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-jobs.json"))
        recovery._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        recovery._reload_only = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "reloaded"})
        self.assertEqual(1, recovery.recover_incomplete_transactions())
        self.assertEqual(old_cert, cert_path.read_bytes())
        self.assertEqual(old_key, key_path.read_bytes())
        recovery._nginx_test.assert_called_once()
        recovery._reload_only.assert_not_called()  # Crash happened before reload began.
        self.assertEqual([], list((self.helper_state / "transactions").glob("tx-*.json")))
        for path in Path(self.temporary.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(b"NEW-PRIVATE", path.read_bytes(), str(path))

    def test_power_loss_after_new_file_replace_removes_originally_missing_target(self):
        target = self.config_root / "new-site.conf"
        original_persist = self.executor._persist_manifest

        def lose_power_after_replace(path, manifest):
            if manifest["phase"] == "replacing" and manifest["files"][0]["replaced"]:
                raise SimulatedPowerLoss()
            return original_persist(path, manifest)

        with mock.patch.object(self.executor, "_persist_manifest", side_effect=lose_power_after_replace):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-new",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": "server { listen 8088; }\n",
                            "expected_sha256": "missing",
                        },
                    )
                )
        self.assertTrue(target.exists())
        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-new-jobs.json"))
        recovery._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        recovery._reload_only = mock.Mock()
        self.assertEqual(1, recovery.recover_incomplete_transactions())
        self.assertFalse(target.exists())
        recovery._nginx_test.assert_called_once()

    def test_power_loss_after_reload_restores_and_reloads_old_config(self):
        target = self.config_root / "reload.conf"
        old = b"server { listen 8000; }\n"
        target.write_bytes(old)
        original_phase = self.executor._set_manifest_phase

        def lose_power_after_reload(path, manifest, phase):
            if phase == "reloaded":
                raise SimulatedPowerLoss()
            return original_phase(path, manifest, phase)

        with mock.patch.object(self.executor, "_set_manifest_phase", side_effect=lose_power_after_reload):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-reload",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": "server { listen 8001; }\n",
                            "expected_sha256": self.sha(old),
                            "reload": True,
                        },
                    )
                )
        self.executor._reload_only.assert_called_once()
        self.assertNotEqual(old, target.read_bytes())

        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-reload-jobs.json"))
        recovery._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        recovery._reload_only = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "reloaded"})
        recovery._nginx_is_running = mock.Mock(return_value=True)
        self.assertEqual(1, recovery.recover_incomplete_transactions())
        self.assertEqual(old, target.read_bytes())
        recovery._nginx_test.assert_called_once()
        recovery._reload_only.assert_called_once()

    def test_boot_recovery_does_not_reload_when_nginx_is_stopped(self):
        target = self.config_root / "boot.conf"
        old = b"server { listen 8040; }\n"
        target.write_bytes(old)
        original_phase = self.executor._set_manifest_phase

        def lose_power_after_reload(path, manifest, phase):
            if phase == "reloaded":
                raise SimulatedPowerLoss()
            return original_phase(path, manifest, phase)

        with mock.patch.object(self.executor, "_set_manifest_phase", side_effect=lose_power_after_reload):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-boot",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": "server { listen 8041; }\n",
                            "expected_sha256": self.sha(old),
                            "reload": True,
                        },
                    )
                )
        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-boot-jobs.json"))
        recovery._nginx_test = mock.Mock(return_value={"exit_code": 0, "stdout": "", "stderr": "syntax is ok"})
        recovery._reload_only = mock.Mock()
        recovery._nginx_is_running = mock.Mock(return_value=False)
        self.assertEqual(1, recovery.recover_incomplete_transactions())
        self.assertEqual(old, target.read_bytes())
        recovery._nginx_test.assert_called_once()
        recovery._reload_only.assert_not_called()

    def test_power_loss_after_durable_commit_keeps_new_config(self):
        target = self.config_root / "committed.conf"
        old = b"server { listen 8030; }\n"
        new = b"server { listen 8031; }\n"
        target.write_bytes(old)
        original_remove = self.executor._remove_manifest

        def lose_power_before_manifest_unlink(path):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest["phase"] == "committed":
                raise SimulatedPowerLoss()
            return original_remove(path)

        with mock.patch.object(self.executor, "_remove_manifest", side_effect=lose_power_before_manifest_unlink):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-commit",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": new.decode(),
                            "expected_sha256": self.sha(old),
                        },
                    )
                )
        self.assertEqual(new, target.read_bytes())
        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-commit-jobs.json"))
        recovery._nginx_test = mock.Mock()
        recovery._reload_only = mock.Mock()
        self.assertEqual(0, recovery.recover_incomplete_transactions())
        self.assertEqual(new, target.read_bytes())
        recovery._nginx_test.assert_not_called()
        self.assertEqual([], list((self.helper_state / "transactions").glob("tx-*.json")))

    def test_recovery_refuses_tampered_manifest_path(self):
        target = self.config_root / "tamper.conf"
        old = b"server { listen 8010; }\n"
        target.write_bytes(old)
        original_persist = self.executor._persist_manifest

        def lose_power_after_replace(path, manifest):
            if manifest["phase"] == "replacing" and manifest["files"][0]["replaced"]:
                raise SimulatedPowerLoss()
            return original_persist(path, manifest)

        with mock.patch.object(self.executor, "_persist_manifest", side_effect=lose_power_after_replace):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-tamper",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": "server { listen 8011; }\n",
                            "expected_sha256": self.sha(old),
                        },
                    )
                )
        outside = Path(self.temporary.name) / "outside.conf"
        outside.write_bytes(b"DO-NOT-TOUCH")
        manifest_path = next((self.helper_state / "transactions").glob("tx-*.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["target"] = str(outside)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-tamper-jobs.json"))
        recovery._nginx_test = mock.Mock()
        with self.assertRaises(agent.ActionError):
            recovery.recover_incomplete_transactions()
        self.assertEqual(b"DO-NOT-TOUCH", outside.read_bytes())
        recovery._nginx_test.assert_not_called()

    def test_recovery_verification_failure_keeps_manifest_and_refuses_progress(self):
        target = self.config_root / "verify.conf"
        old = b"server { listen 8020; }\n"
        target.write_bytes(old)
        original_persist = self.executor._persist_manifest

        def lose_power_after_replace(path, manifest):
            if manifest["phase"] == "replacing" and manifest["files"][0]["replaced"]:
                raise SimulatedPowerLoss()
            return original_persist(path, manifest)

        with mock.patch.object(self.executor, "_persist_manifest", side_effect=lose_power_after_replace):
            with self.assertRaises(SimulatedPowerLoss):
                self.executor.execute(
                    self.job(
                        "job-power-verify",
                        "config_apply",
                        {
                            "path": str(target),
                            "content": "server { listen 8021; }\n",
                            "expected_sha256": self.sha(old),
                        },
                    )
                )
        recovery = agent.JobExecutor(self.settings, agent.JobStore(self.state / "recovery-verify-jobs.json"))
        recovery._nginx_test = mock.Mock(side_effect=agent.CommandError("restored nginx is invalid"))
        with self.assertRaises(agent.ActionError):
            recovery.recover_incomplete_transactions()
        self.assertEqual(old, target.read_bytes())
        manifests = list((self.helper_state / "transactions").glob("tx-*.json"))
        self.assertEqual(1, len(manifests))
        persisted = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual("recovery_failed", persisted["phase"])

    def test_path_escape_and_symlink_are_rejected(self):
        outside = Path(self.temporary.name) / "outside.conf"
        outside.write_text("outside", encoding="utf-8")
        escaped = self.executor.execute(self.job("job-outside", "config_hash", {"path": str(outside)}))
        self.assertEqual("failed", escaped["status"])
        self.assertIn("outside allowed_configuration_roots", escaped["error"])

        if hasattr(os, "symlink"):
            link = self.config_root / "link.conf"
            try:
                link.symlink_to(outside)
            except OSError:
                return  # Windows CI may not grant symlink privilege.
            linked = self.executor.execute(self.job("job-symlink", "config_hash", {"path": str(link)}))
            self.assertEqual("failed", linked["status"])
            self.assertIn("symbolic-link", linked["error"])

    def test_expected_sha_prevents_lost_update(self):
        target = self.config_root / "site.conf"
        current = b"current"
        target.write_bytes(current)
        response = self.executor.execute(
            self.job(
                "job-stale",
                "config_apply",
                {"path": str(target), "content": "server { listen 8082; }", "expected_sha256": "0" * 64},
            )
        )
        self.assertEqual("failed", response["status"])
        self.assertIn("concurrent change", response["error"])
        self.assertEqual(current, target.read_bytes())
        self.executor._nginx_test.assert_not_called()

    def test_certificate_private_key_is_not_in_server_result(self):
        self.executor._verify_certificate_pair = mock.Mock(return_value="AA:BB:CC")
        cert_dir = self.certificate_root
        cert_path = cert_dir / "fullchain.pem"
        key_path = cert_dir / "privkey.pem"
        private_key = "-----BEGIN PRIVATE KEY-----\nVERY-SECRET-MATERIAL\n-----END PRIVATE KEY-----\n"
        response = self.executor.execute(
            self.job(
                "job-cert",
                "certificate_apply",
                {
                    "certificate": {
                        "path": str(cert_path),
                        "pem": "-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----\n",
                        "expected_sha256": "missing",
                    },
                    "private_key": {"path": str(key_path), "pem": private_key, "expected_sha256": "missing"},
                    "reload": False,
                },
            )
        )
        self.assertEqual("succeeded", response["status"])
        server_result = agent._to_server_result(response)
        serialized = json.dumps(server_result)
        self.assertNotIn("VERY-SECRET-MATERIAL", serialized)
        self.assertNotIn("private_key_sha256", serialized)
        self.assertEqual(hashlib.sha256(private_key.encode()).hexdigest(), server_result["details"]["key_material_sha256"])
        if os.name == "posix":
            self.assertEqual(0o600, key_path.stat().st_mode & 0o777)

        read_key = self.executor.execute(self.job("job-read-key", "config_read", {"path": str(key_path)}))
        self.assertEqual("failed", read_key["status"])
        self.assertIn("managed configuration paths", read_key["error"])

    def test_result_mapping_matches_control_plane_schema(self):
        local = {
            "job_id": "x",
            "action": "config_apply",
            "status": "succeeded",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "result": {
                "sha256": "a" * 64,
                "previous_sha256": "b" * 64,
                "validated": True,
                "applied": True,
                "reloaded": True,
            },
        }
        mapped = agent._to_server_result(local)
        self.assertEqual({"status", "job_id", "action", "details", "duration_ms"}, set(mapped))
        self.assertEqual("x", mapped["job_id"])
        self.assertEqual("config_apply", mapped["action"])
        self.assertEqual("succeeded", mapped["status"])
        self.assertEqual("a" * 64, mapped["details"]["config_hash"])
        self.assertEqual("b" * 64, mapped["details"]["previous_config_hash"])
        self.assertTrue(mapped["details"]["syntax_ok"])
        self.assertEqual(1000, mapped["duration_ms"])

        expired = dict(local, status="expired", error="job expired")
        mapped_expired = agent._to_server_result(expired)
        self.assertEqual("expired", mapped_expired["status"])
        self.assertEqual("job_expired", mapped_expired["details"]["failure_code"])
        self.assertEqual("queue", mapped_expired["details"]["failure_stage"])

    def test_nginx_failure_mapping_returns_only_fixed_diagnostic_and_line(self):
        sensitive_path = "/apps/nginx/cert/customer-secret-name.pem"
        local = {
            "job_id": "failed-nginx-test",
            "action": "config_apply",
            "status": "failed",
            "error": (
                "publish failed at phase testing and previous files were restored: "
                "nginx: [emerg] cannot load certificate \"{}\": BIO_new_file() failed "
                "in /apps/nginx/conf/conf.d/private-site.conf:17"
            ).format(sensitive_path),
            "failure_code": "nginx_config_test_failed",
            "failure_stage": "nginx_test",
            "rollback_status": "restored",
        }

        mapped = agent._to_server_result(local)
        details = mapped["details"]
        self.assertEqual("certificate_file_missing", details["nginx_error_code"])
        self.assertEqual(17, details["nginx_error_line"])
        self.assertNotIn(sensitive_path, json.dumps(details))
        self.assertNotIn("private-site", json.dumps(details))
        self.assertEqual({}, agent._nginx_error_metadata("unclassified secret output"))

        invalid_url = agent._nginx_error_metadata(
            "nginx: [emerg] invalid URL prefix in /apps/nginx/conf/conf.d/private-site.conf:23"
        )
        self.assertEqual("invalid_url_prefix", invalid_url["nginx_error_code"])
        self.assertEqual(23, invalid_url["nginx_error_line"])
        self.assertNotIn("private-site", json.dumps(invalid_url))

    def test_interrupted_job_is_not_replayed(self):
        self.store.begin("job-crashed", "nginx_reload")
        response = self.executor.execute(self.job("job-crashed", "nginx_reload", {}))
        self.assertEqual("failed", response["status"])
        self.assertIn("not replayed", response["error"])
        self.executor._nginx_test.assert_not_called()

    def test_http_requires_explicit_opt_in(self):
        settings = agent.Settings(
            server_url="http://127.0.0.1:8080",
            node_name="node",
            nginx_binary=str(Path(sys.executable).resolve()),
            nginx_config=str(self.main_config),
            nginx_root=str(self.root),
            allowed_config_roots=[str(self.config_root)],
            allowed_certificate_roots=[str(self.certificate_root)],
            state_dir=str(self.state),
            helper_state_dir=str(self.helper_state),
            helper_socket=str(Path(self.temporary.name) / "helper.sock"),
        )
        with self.assertRaises(agent.AgentError):
            settings.validate()
        settings.allow_insecure_http = True
        settings.validate()

    def test_health_check_rejects_unapproved_host(self):
        with self.assertRaises(agent.ActionError):
            self.executor._health_check({"url": "http://169.254.169.254/latest/meta-data", "attempts": 1})

    def test_enrollment_secret_is_persisted_before_first_request_and_retried(self):
        service = agent.AgentService(self.settings, agent.threading.Event())
        calls = []

        def first_attempt(path, payload, token=None):
            calls.append((path, payload, token))
            pending = json.loads(service.identity_path.read_text(encoding="utf-8"))
            self.assertTrue(pending["enrollment_pending"])
            self.assertEqual(pending["enrollment_id"], payload["enrollment_id"])
            self.assertEqual(pending["enrollment_secret"], payload["enrollment_secret"])
            raise agent.ApiError("response lost")

        service.api.post = first_attempt
        with self.assertRaises(agent.ApiError):
            service.enroll()
        pending = json.loads(service.identity_path.read_text(encoding="utf-8"))
        self.assertNotIn("agent_id", pending)
        self.assertNotIn("machine_credential", pending)

        def retry(path, payload, token=None):
            calls.append((path, payload, token))
            self.assertEqual(pending["enrollment_id"], payload["enrollment_id"])
            return {"status": "pending", "enrollment_id": payload["enrollment_id"]}

        service.api.post = retry
        result = service.enroll()
        self.assertEqual("pending", result["status"])
        self.assertEqual(2, len(calls))

    def test_approval_derives_and_persists_machine_credential(self):
        service = agent.AgentService(self.settings, agent.threading.Event())

        def approve(path, payload, token=None):
            return {
                "status": "approved",
                "enrollment_id": payload["enrollment_id"],
                "agent_id": "agent-1",
            }

        service.api.post = approve
        identity = service.enroll()
        saved = json.loads(service.identity_path.read_text(encoding="utf-8"))
        self.assertEqual("agent-1", identity["agent_id"])
        self.assertEqual(identity, saved)
        self.assertEqual(64, len(identity["machine_credential"]))
        self.assertNotIn("enrollment_secret", saved)

    def test_reenrollment_rejection_restores_previous_machine_identity(self):
        previous = {"agent_id": "agent-old", "machine_credential": "old-secret"}
        service = agent.AgentService(self.settings, agent.threading.Event())
        agent._atomic_json(service.identity_path, previous)

        def reject(path, payload, token=None):
            return {"status": "rejected", "enrollment_id": payload["enrollment_id"]}

        service.api.post = reject
        with self.assertRaises(agent.AgentError):
            service.enroll(force=True)
        self.assertEqual(previous, json.loads(service.identity_path.read_text(encoding="utf-8")))

    def test_legacy_agent_identity_is_migrated_in_memory(self):
        service = agent.AgentService(self.settings, agent.threading.Event())
        agent._atomic_json(service.identity_path, {"agent_id": "legacy", "agent_token": "legacy-secret"})
        identity = service.identity()
        self.assertEqual(
            {"agent_id": "legacy", "machine_credential": "legacy-secret"},
            identity,
        )

    def test_managed_config_policy_blocks_privilege_escape_directives(self):
        blocked = {
            "include": "server { include /etc/nginx/other.conf; }",
            "lua": "server { content_by_lua_block { return 1; } }",
            "log": "server { access_log /etc/cron.d/nginx-manager; }",
            "main": "http { server { listen 80; } }",
            "certificate": "server { ssl_certificate /etc/passwd; }",
            "third-party": "server { passenger_enabled on; passenger_user root; }",
        }
        for index, (name, content) in enumerate(blocked.items()):
            target = self.config_root / (name + ".conf")
            response = self.executor.execute(
                self.job(
                    "job-policy-{}".format(index),
                    "config_apply",
                    {"path": str(target), "content": content, "expected_sha256": "missing"},
                )
            )
            self.assertEqual("failed", response["status"], name)
            self.assertFalse(target.exists(), name)

    def test_managed_config_policy_allows_certificate_paths_in_managed_root(self):
        target = self.config_root / "tls.conf"
        certificate = self.certificate_root / "example.crt"
        private_key = self.certificate_root / "example.key"
        content = "server { listen 443 ssl; ssl_certificate %s; ssl_certificate_key %s; }" % (
            certificate,
            private_key,
        )
        response = self.executor.execute(
            self.job(
                "job-policy-valid",
                "config_apply",
                {"path": str(target), "content": content, "expected_sha256": "missing"},
            )
        )
        self.assertEqual("succeeded", response["status"])

    def test_python36_compatibility_contract(self):
        source = (AGENT_DIR / "nginx_agent.py").read_text(encoding="utf-8")
        installer = (AGENT_DIR.parent / "deploy" / "install-agent.sh").read_text(encoding="utf-8")
        self.assertNotIn("from dataclasses import", source)
        self.assertNotIn(".fromisoformat(", source)
        self.assertNotIn("text=True", source)
        self.assertNotIn('add_subparsers(dest="command", required=True)', source)
        self.assertIn("sys.version_info >= (3, 6)", installer)
        self.assertNotIn("sys.version_info >= (3, 8)", installer)
        self.assertNotIn("mode & 0o077", source)
        self.assertIn("mode & 0o022", source)
        self.assertIn("控制端暂不可达或申请待审批", installer)

    def test_settings_mutable_defaults_are_isolated(self):
        first = agent.Settings("https://manager.example.test", "first")
        second = agent.Settings("https://manager.example.test", "second")
        first.labels["region"] = "one"
        first.allowed_config_roots.append("/tmp/extra")
        self.assertEqual({}, second.labels)
        self.assertNotIn("/tmp/extra", second.allowed_config_roots)

    def test_python36_iso8601_parser_handles_protocol_timestamps(self):
        parsed = agent._parse_iso8601("2026-07-14T12:34:56.123456789+08:00")
        self.assertEqual(123456, parsed.microsecond)
        self.assertEqual(agent.dt.timedelta(hours=8), parsed.utcoffset())
        self.assertEqual(
            1500,
            agent._duration_ms("2026-07-14T00:00:00Z", "2026-07-14T00:00:01.500Z"),
        )

    def test_installer_generates_a_systemd_219_compatible_sandbox(self):
        installer = (AGENT_DIR.parent / "deploy" / "install-agent.sh").read_text(encoding="utf-8")
        self.assertIn('if (( systemd_version >= 232 )); then', installer)
        self.assertIn('protect_system="full"', installer)
        self.assertIn('write_access_key="ReadWriteDirectories"', installer)
        self.assertIn('ProtectSystem=${protect_system}', installer)
        self.assertIn('${write_access_key}=${STATE_DIR}', installer)

    def test_tls_verification_can_only_be_skipped_explicitly_for_https(self):
        settings = agent.Settings(
            "https://manager.example.test",
            "skip-ca",
            tls_skip_verify=True,
        )
        settings.validate()
        with mock.patch.object(agent.ssl, "_create_unverified_context", wraps=agent.ssl._create_unverified_context) as create:
            agent.ApiClient(settings)
        create.assert_called_once_with()

        with self.assertRaises(agent.AgentError):
            agent.Settings(
                "https://manager.example.test",
                "conflict",
                ca_file="/tmp/ca.crt",
                tls_skip_verify=True,
            ).validate()
        with self.assertRaises(agent.AgentError):
            agent.Settings(
                "http://manager.example.test",
                "plain-http",
                tls_skip_verify=True,
                allow_insecure_http=True,
            ).validate()

    def test_api_client_uses_python36_compatible_http_client_transport(self):
        settings = agent.Settings(
            "http://127.0.0.1:8443",
            "transport-test",
            allow_insecure_http=True,
        )
        response = mock.Mock()
        response.status = 200
        response.read.return_value = b'{"status":"pending"}'
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(agent.http.client, "HTTPConnection", return_value=connection) as constructor:
            result = agent.ApiClient(settings).post(
                "/api/v1/agent/enroll",
                {"node_name": "transport-test"},
            )

        self.assertEqual({"status": "pending"}, result)
        constructor.assert_called_once_with("127.0.0.1", 8443, timeout=30.0)
        request_args = connection.request.call_args
        self.assertEqual("POST", request_args[0][0])
        self.assertEqual("/api/v1/agent/enroll", request_args[0][1])
        self.assertEqual("close", request_args[1]["headers"]["Connection"])
        connection.close.assert_called_once_with()

    def test_api_client_completes_real_http_post(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received["path"] = self.path
                received["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
                body = b'{"status":"pending"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format_string, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            settings = agent.Settings(
                "http://127.0.0.1:{}".format(server.server_port),
                "transport-test",
                allow_insecure_http=True,
                api_timeout=3,
            )
            result = agent.ApiClient(settings).post(
                "/api/v1/agent/enroll",
                {"node_name": "transport-test"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

        self.assertEqual({"status": "pending"}, result)
        self.assertEqual("/api/v1/agent/enroll", received["path"])
        self.assertEqual({"node_name": "transport-test"}, received["payload"])

    def test_api_client_rejects_http_error_without_reading_response_body(self):
        settings = agent.Settings(
            "http://127.0.0.1:8443",
            "transport-test",
            allow_insecure_http=True,
        )
        response = mock.Mock()
        response.status = 503
        connection = mock.Mock()
        connection.getresponse.return_value = response

        with mock.patch.object(agent.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(agent.ApiError) as raised:
                agent.ApiClient(settings).post("/api/v1/agent/enroll", {})

        self.assertEqual(503, raised.exception.status_code)
        response.read.assert_not_called()
        connection.close.assert_called_once_with()

    def test_installer_accepts_custom_apps_nginx_layout(self):
        installer = (AGENT_DIR.parent / "deploy" / "install-agent.sh").read_text(encoding="utf-8")
        for option in (
            "--insecure-skip-tls-verify",
            "--nginx-binary",
            "--managed-config-dir",
            "--managed-cert-dir",
            "--managed-include-file",
        ):
            self.assertIn(option, installer)
        self.assertIn('"tls_skip_verify": tls_skip_verify == "1"', installer)
        self.assertIn('"allow_insecure_http": allow_insecure_http == "1"', installer)
        self.assertIn('value.scheme not in {"http", "https"}', installer)
        self.assertIn('"${NGINX_BINARY}" -t -c "${NGINX_CONFIG}"', installer)
        self.assertIn("--managed-config-already-included", installer)
        self.assertIn("zz-nginx-manager-probe.", installer)
        self.assertIn("is not loaded by nginx", installer)
        self.assertIn('if [[ -e "${MANAGED_CONFIG_DIR}" ]]', installer)
        self.assertIn('if [[ -e "${MANAGED_CERT_DIR}" ]]', installer)

    def test_privileged_units_allow_custom_nginx_test_to_bind_low_ports(self):
        root = AGENT_DIR.parent
        installer = (root / "deploy" / "install-agent.sh").read_text(encoding="utf-8")
        helper = (AGENT_DIR / "nginx-manager-agent-helper.service").read_text(encoding="utf-8")
        recovery = (AGENT_DIR / "nginx-manager-agent-recover.service").read_text(encoding="utf-8")
        self.assertGreaterEqual(installer.count("CAP_NET_BIND_SERVICE"), 2)
        self.assertIn("CAP_NET_BIND_SERVICE", helper)
        self.assertIn("CAP_NET_BIND_SERVICE", recovery)
        self.assertIn("recover_existing_transactions\nbegin_install_transaction", installer)
        self.assertIn('journalctl -u "${APP_NAME}-recover.service"', installer)

    def test_agent_uninstaller_preserves_managed_nginx_files(self):
        root = AGENT_DIR.parent
        bootstrap = (root / "uninstall-agent.sh").read_text(encoding="utf-8")
        uninstaller = (root / "deploy" / "uninstall-agent.sh").read_text(encoding="utf-8")
        self.assertIn("deploy/uninstall-agent.sh", bootstrap)
        self.assertIn('--purge) PURGE="1"', uninstaller)
        self.assertIn('rm -rf -- "${APP_DIR}"', uninstaller)
        self.assertIn('rm -rf -- "${ETC_DIR}" "${STATE_DIR}" "${HELPER_STATE_DIR}"', uninstaller)
        self.assertNotIn("nginx-manager.d", uninstaller)
        self.assertNotIn("ssl/nginx-manager", uninstaller)

    def test_python36_can_construct_and_run_the_cli_parser(self):
        parsed = agent.build_parser().parse_args(["validate-config"])
        self.assertEqual("validate-config", parsed.command)
        completed = agent.subprocess.run(
            [sys.executable, str(AGENT_DIR / "nginx_agent.py"), "--help"],
            stdin=agent.subprocess.DEVNULL,
            stdout=agent.subprocess.PIPE,
            stderr=agent.subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()

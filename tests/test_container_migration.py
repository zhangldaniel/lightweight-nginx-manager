import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContainerMigrationContractTests(unittest.TestCase):
    @staticmethod
    def _shell_function(script, name):
        start = script.index("{}() {{".format(name))
        end = script.index("\n}\n", start) + 3
        return script[start:end]

    def test_container_keeps_every_runtime_state_path(self):
        compose = (ROOT / "deploy" / "container" / "compose.yaml").read_text(encoding="utf-8")
        dockerfile = (ROOT / "deploy" / "container" / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        backup = (ROOT / "deploy" / "backup-server.sh").read_text(encoding="utf-8")
        restore = (ROOT / "deploy" / "restore-server-backup.sh").read_text(encoding="utf-8")
        migration = (ROOT / "deploy" / "migrate-server-to-container.sh").read_text(encoding="utf-8")

        self.assertIn("/var/lib/nginx-manager:/var/lib/nginx-manager", compose)
        self.assertIn("/etc/nginx-manager:/etc/nginx-manager:ro", compose)
        self.assertNotIn("NGINX_MANAGER_DB_PATH:", compose)
        self.assertNotIn("NGINX_MANAGER_ATTACHMENTS_DIR:", compose)
        self.assertIn("COPY frontend/public/ui-assets /app/ui/ui-assets", dockerfile)
        self.assertTrue((ROOT / "frontend" / "public" / "ui-assets" / "IBMPlexSansSC-Regular.woff").is_file())
        self.assertNotIn("frontend/public/ui-assets", dockerignore)
        self.assertIn("site_attachments", backup)
        self.assertNotIn('source "${ENV_FILE}"', backup)
        self.assertIn('read_env_value "${ENV_FILE}" NGINX_MANAGER_DB_PATH', backup)
        self.assertIn('tar -C "${WORK_DIR}" -czf "${ARCHIVE_TMP}" data etc', backup)
        self.assertIn('mv -- "${ARCHIVE_TMP}" "${ARCHIVE}"', backup)
        self.assertIn('[[ -z "${ARCHIVE_TMP}" || ! -e "${ARCHIVE_TMP}" ]] || rm -f', backup)
        self.assertIn('printf \'%s\\n\' "${DB_NAME}" >"${WORK_DIR}/database-name.txt"', backup)
        self.assertIn('"$(dirname -- "${resolved_db}")" == "${resolved_data}"', backup)
        self.assertIn('mapfile -t archived_db_names <"${WORK_DIR}/database-name.txt"', restore)
        self.assertIn('"${DATABASE_NAME}" == "${ARCHIVED_DB_NAME}"', restore)
        self.assertIn('mv -- "${STAGED_DATA}/manager.db" "${DATABASE_TARGET}"', restore)
        self.assertIn('"$(dirname -- "${resolved_archived_db}")" == "${resolved_data}"', restore)
        self.assertIn("trap finish_exit EXIT", restore)
        self.assertIn("trap 'exit $?' ERR", restore)
        self.assertIn("trap 'exit 130' INT", restore)
        self.assertIn("trap 'exit 143' TERM", restore)
        self.assertIn("trap 'exit 129' HUP", restore)
        self.assertIn("SWAP_STARTED=1", restore)
        self.assertIn("RESTORE_COMMITTED=1", restore)
        self.assertIn(
            'DATA_INSTALLED=1\nif ! mv -- "${STAGED_DATA}" "${DATA_DIR}"',
            restore,
        )
        self.assertIn(
            'ETC_INSTALLED=1\nif ! mv -- "${STAGED_ETC}" "${ETC_DIR}"',
            restore,
        )
        self.assertIn('rollback_path "${ETC_DIR}"', restore)
        self.assertIn('rollback_path "${DATA_DIR}"', restore)
        self.assertIn('python3 - "${db_path}"', migration)
        self.assertNotIn('source "${ETC_DIR}/server.env"', migration)
        self.assertIn('read_env_value "${ETC_DIR}/server.env" NGINX_MANAGER_DB_PATH', migration)
        self.assertIn('"$(dirname -- "${resolved_db}")" == "${resolved_data}"', migration)
        self.assertIn('systemctl start "${SERVICE}"', migration)
        self.assertIn("trap 'rollback_and_exit 130' INT", migration)
        self.assertIn("trap 'rollback_and_exit 143' TERM", migration)
        self.assertIn("trap 'rollback_and_exit 129' HUP", migration)
        self.assertIn("trap - ERR INT TERM HUP", migration)
        self.assertIn('if systemctl cat "${SERVICE}"', migration)
        self.assertIn('systemctl disable "${SERVICE}"', migration)
        self.assertNotIn('systemctl disable "${SERVICE}" >/dev/null 2>&1 || true', migration)

    def test_migration_requires_a_backup_before_container_start(self):
        migration = (ROOT / "deploy" / "migrate-server-to-container.sh").read_text(encoding="utf-8")
        backup_call = 'bash "${SCRIPT_DIR}/backup-server.sh" >/dev/null'

        self.assertEqual(1, migration.count(backup_call))
        self.assertNotIn('if [[ -x "/opt/nginx-manager/current/venv/bin/python" ]]; then', migration)
        self.assertIn("set -Eeuo pipefail", migration)
        self.assertLess(migration.index(backup_call), migration.index('python3 - "${db_path}"'))

    @unittest.skipUnless(shutil.which("bash"), "bash is needed to exercise safe env parsing")
    def test_server_env_values_are_read_without_shell_execution(self):
        backup = (ROOT / "deploy" / "backup-server.sh").read_text(encoding="utf-8")
        reader = self._shell_function(backup, "read_env_value")
        with tempfile.TemporaryDirectory() as directory:
            environment_file = Path(directory) / "server.env"
            marker = Path(directory) / "must-not-exist"
            environment_file.write_text(
                'NGINX_MANAGER_DB_PATH="$(touch {})"\n'.format(marker.as_posix()),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -Eeuo pipefail\n{}\nread_env_value \"$1\" NGINX_MANAGER_DB_PATH".format(reader),
                    "bash",
                    environment_file.as_posix(),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual("$(touch {})".format(marker.as_posix()), result.stdout)
            self.assertFalse(marker.exists())

            environment_file.write_text(
                "NGINX_MANAGER_DB_PATH=/var/lib/nginx-manager/one.db\n"
                "NGINX_MANAGER_DB_PATH=/var/lib/nginx-manager/two.db\n",
                encoding="utf-8",
            )
            duplicate = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -Eeuo pipefail\n{}\nread_env_value \"$1\" NGINX_MANAGER_DB_PATH".format(reader),
                    "bash",
                    environment_file.as_posix(),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(0, duplicate.returncode)
            self.assertIn("重复定义", duplicate.stderr)

    @unittest.skipUnless(shutil.which("bash"), "bash is needed to exercise restore rollback")
    def test_restore_rollback_reinstates_both_original_directories(self):
        restore = (ROOT / "deploy" / "restore-server-backup.sh").read_text(encoding="utf-8")
        functions = "\n".join([
            self._shell_function(restore, "rollback_path"),
            self._shell_function(restore, "rollback_restore"),
        ])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            etc = root / "etc"
            previous_data = root / "previous-data"
            previous_etc = root / "previous-etc"
            data.mkdir()
            previous_data.mkdir()
            previous_etc.mkdir()
            (data / "state").write_text("new", encoding="utf-8")
            (previous_data / "state").write_text("old-data", encoding="utf-8")
            (previous_etc / "state").write_text("old-etc", encoding="utf-8")
            subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -Eeuo pipefail\n{}\n"
                    "DATA_DIR=$1 ETC_DIR=$2 PREVIOUS_DATA=$3 PREVIOUS_ETC=$4 "
                    "DATA_INSTALLED=1 ETC_INSTALLED=0\nrollback_restore".format(functions),
                    "bash",
                    data.as_posix(),
                    etc.as_posix(),
                    previous_data.as_posix(),
                    previous_etc.as_posix(),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual("old-data", (data / "state").read_text(encoding="utf-8"))
            self.assertEqual("old-etc", (etc / "state").read_text(encoding="utf-8"))
            self.assertFalse(previous_data.exists())
            self.assertFalse(previous_etc.exists())

    @unittest.skipUnless(shutil.which("bash"), "bash is needed to exercise the container entrypoint")
    def test_healthcheck_uses_an_address_reachable_from_each_bind(self):
        entrypoint = ROOT / "deploy" / "container" / "entrypoint.sh"
        cases = [
            ("0.0.0.0", "0", "http://127.0.0.1:8443/healthz"),
            ("192.0.2.45", "1", "https://192.0.2.45:8443/healthz"),
            ("::", "0", "http://[::1]:8443/healthz"),
            ("2001:db8::45", "1", "https://[2001:db8::45]:8443/healthz"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            fake_python = Path(directory) / "python"
            fake_python.write_text('#!/bin/sh\nprintf %s "$NGINX_MANAGER_HEALTH_URL"\n', encoding="utf-8")
            fake_python.chmod(0o755)
            for bind, tls, expected in cases:
                with self.subTest(bind=bind, tls=tls):
                    environment = dict(os.environ)
                    environment.update({
                        "PATH": directory + os.pathsep + environment.get("PATH", ""),
                        "NGINX_MANAGER_CONTAINER_BIND": bind,
                        "NGINX_MANAGER_CONTAINER_TLS": tls,
                    })
                    result = subprocess.run(
                        ["bash", str(entrypoint), "healthcheck"],
                        check=True,
                        capture_output=True,
                        text=True,
                        env=environment,
                    )
                    self.assertEqual(expected, result.stdout)

    @unittest.skipUnless(shutil.which("bash"), "bash is needed for shell syntax checks")
    def test_container_shell_scripts_parse(self):
        scripts = [
            ROOT / "deploy" / "backup-server.sh",
            ROOT / "deploy" / "restore-server-backup.sh",
            ROOT / "deploy" / "migrate-server-to-container.sh",
            ROOT / "deploy" / "container" / "entrypoint.sh",
            ROOT / "migrate-server-to-container.sh",
        ]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)


if __name__ == "__main__":
    unittest.main()

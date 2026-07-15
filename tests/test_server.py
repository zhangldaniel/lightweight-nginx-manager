import secrets
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

from app import (  # noqa: E402
    LDAPUnavailableError,
    LoginRequest,
    Settings,
    _derive_agent_credential,
    _ldap_role_for_groups,
    create_app,
)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "test.db")
        self.settings = Settings(
            db_path=self.db_path,
            online_after_seconds=90,
            default_job_ttl_seconds=300,
            max_job_ttl_seconds=86400,
            sensitive_job_ttl_seconds=900,
            max_payload_bytes=2 * 1024 * 1024,
            max_ui_state_bytes=2 * 1024 * 1024,
            session_ttl_seconds=28800,
            enrollment_pending_ttl_seconds=86400,
            password_iterations=100000,
            login_window_seconds=300,
            login_max_attempts=8,
        )
        self.client_context = TestClient(
            create_app(self.settings),
            base_url="https://testserver",
        )
        self.client = self.client_context.__enter__()
        created = self.client.app.state.database.bootstrap_admin(
            "admin",
            "correct-horse-battery-staple",
            self.settings.password_iterations,
        )
        self.assertTrue(created["created"])
        self.csrf = self.login().json()["csrf_token"]

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def login(self, username="admin", password="correct-horse-battery-staple"):
        return self.client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )

    @property
    def admin_headers(self):
        return {"X-CSRF-Token": self.csrf}

    def start_enrollment(self, node_name="edge-01", enrollment_id=None, secret=None):
        request_id = enrollment_id or str(uuid.uuid4())
        enrollment_secret = secret or secrets.token_urlsafe(32)
        payload = {
            "enrollment_id": request_id,
            "enrollment_secret": enrollment_secret,
            "node_name": node_name,
            "hostname": node_name + ".example.test",
            "labels": {"region": "test"},
        }
        response = self.client.post("/api/v1/agent/enroll", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return payload, response.json()

    def approve(self, enrollment_id):
        response = self.client.post(
            "/api/v1/admin/enrollments/{}/approve".format(enrollment_id),
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def enroll(self, node_name="edge-01"):
        request, pending = self.start_enrollment(node_name)
        self.assertEqual("pending", pending["status"])
        approved = self.approve(request["enrollment_id"])
        completed = self.client.post("/api/v1/agent/enroll", json=request)
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual("approved", completed.json()["status"])
        agent_id = approved["agent_id"]
        credential = _derive_agent_credential(
            request["enrollment_secret"], request["enrollment_id"], agent_id
        )
        return {"agent_id": agent_id, "machine_credential": credential}

    def test_web_login_session_cookie_and_csrf(self):
        anonymous = TestClient(self.client.app, base_url="https://testserver")
        self.assertEqual(anonymous.get("/api/v1/admin/nodes").status_code, 401)
        self.assertEqual(self.login(password="wrong-password").status_code, 401)

        session = self.client.get("/api/v1/auth/session")
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json()["username"], "admin")
        self.assertEqual(session.json()["role"], "admin")
        self.assertEqual(session.json()["auth_source"], "local")
        self.assertTrue(session.json()["csrf_token"])
        cookie = self.login().headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

        rejected = self.client.put(
            "/api/v1/admin/ui-state",
            json={"revision": 0, "state": {}},
        )
        self.assertEqual(rejected.status_code, 403)

    def test_ldap_roles_are_enforced_server_side_and_local_admin_has_priority(self):
        ldap_settings = replace(
            self.settings,
            ldap_enabled=True,
            ldap_url="ldap://directory.example.test:389",
            ldap_base_dn="dc=example,dc=test",
            ldap_bind_dn="cn=nginx-manager,ou=service,dc=example,dc=test",
            ldap_bind_password_file=str(Path(self.tempdir.name).resolve() / "ldap-password"),
        )
        calls = []

        def authenticate(_settings, username, _password):
            calls.append(username)
            role = {"readonly": "auditor", "ops": "operator", "ldapadmin": "admin"}[username]
            return {
                "principal_id": "ldap:" + username,
                "username": username,
                "role": role,
                "auth_source": "ldap",
            }

        with TestClient(
            create_app(ldap_settings, ldap_authenticator=authenticate),
            base_url="https://testserver",
        ) as ldap_client:
            local_rejected = ldap_client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
            )
            self.assertEqual(local_rejected.status_code, 401)
            self.assertNotIn("admin", calls)

            readonly = ldap_client.post(
                "/api/v1/auth/login", json={"username": "readonly", "password": "directory-password"}
            )
            self.assertEqual(readonly.status_code, 200, readonly.text)
            self.assertEqual(readonly.json()["role"], "auditor")
            self.assertEqual(readonly.json()["auth_source"], "ldap")
            readonly_csrf = {"X-CSRF-Token": readonly.json()["csrf_token"]}
            self.assertEqual(ldap_client.get("/api/v1/admin/nodes").status_code, 200)
            self.assertEqual(
                ldap_client.put(
                    "/api/v1/admin/ui-state",
                    headers=readonly_csrf,
                    json={"revision": 0, "state": {}},
                ).status_code,
                403,
            )

            operator = ldap_client.post(
                "/api/v1/auth/login", json={"username": "ops", "password": "directory-password"}
            )
            operator_csrf = {"X-CSRF-Token": operator.json()["csrf_token"]}
            saved = ldap_client.put(
                "/api/v1/admin/ui-state",
                headers=operator_csrf,
                json={"revision": 0, "state": {"sites": []}},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            request, _pending = self.start_enrollment("operator-cannot-approve")
            self.assertEqual(
                ldap_client.post(
                    "/api/v1/admin/enrollments/{}/approve".format(request["enrollment_id"]),
                    headers=operator_csrf,
                ).status_code,
                403,
            )

            ldap_admin = ldap_client.post(
                "/api/v1/auth/login", json={"username": "ldapadmin", "password": "directory-password"}
            )
            self.assertEqual(ldap_admin.status_code, 200, ldap_admin.text)
            self.assertEqual(ldap_admin.json()["role"], "admin")
            self.assertEqual(
                ldap_client.post(
                    "/api/v1/admin/enrollments/{}/approve".format(request["enrollment_id"]),
                    headers={"X-CSRF-Token": ldap_admin.json()["csrf_token"]},
                ).status_code,
                200,
            )

    def test_ldap_outage_does_not_disable_local_emergency_admin(self):
        ldap_settings = replace(
            self.settings,
            ldap_enabled=True,
            ldap_url="ldap://directory.example.test:389",
            ldap_base_dn="dc=example,dc=test",
            ldap_bind_dn="cn=nginx-manager,ou=service,dc=example,dc=test",
            ldap_bind_password_file=str(Path(self.tempdir.name).resolve() / "ldap-password"),
        )

        def unavailable(_settings, _username, _password):
            raise LDAPUnavailableError("offline")

        with TestClient(
            create_app(ldap_settings, ldap_authenticator=unavailable),
            base_url="https://testserver",
        ) as ldap_client:
            directory_login = ldap_client.post(
                "/api/v1/auth/login", json={"username": "directory-user", "password": "password"}
            )
            self.assertEqual(directory_login.status_code, 503)
            local_login = ldap_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "correct-horse-battery-staple"},
            )
            self.assertEqual(local_login.status_code, 200, local_login.text)
            self.assertEqual(local_login.json()["auth_source"], "local")

    def test_ldap_accepts_upn_and_matches_group_dn_case_insensitively(self):
        login = LoginRequest(username="alice@example.test", password="directory-password")
        self.assertEqual(login.username, "alice@example.test")
        group_settings = replace(
            self.settings,
            ldap_admin_group="CN=Nginx-Admins,OU=Groups,DC=example,DC=test",
        )
        role = _ldap_role_for_groups(
            group_settings,
            ["cn=nginx-admins,ou=groups,dc=EXAMPLE,dc=TEST"],
        )
        self.assertEqual(role, "admin")

    def test_bootstrap_admin_is_idempotent(self):
        outcome = self.client.app.state.database.bootstrap_admin(
            "different", "another-very-long-password", self.settings.password_iterations
        )
        self.assertFalse(outcome["created"])
        self.assertEqual(outcome["username"], "admin")

    def test_health_pending_approval_and_heartbeat(self):
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["configured"])

        request, pending = self.start_enrollment()
        self.assertEqual(pending["status"], "pending")
        listing = self.client.get("/api/v1/admin/enrollments", headers=self.admin_headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["node_name"], "edge-01")
        self.assertNotIn("secret", listing.text.lower())
        self.assertNotIn("credential", listing.text.lower())

        self.approve(request["enrollment_id"])
        completed = self.client.post("/api/v1/agent/enroll", json=request).json()
        credential = _derive_agent_credential(
            request["enrollment_secret"], request["enrollment_id"], completed["agent_id"]
        )
        response = self.client.post(
            "/api/v1/agent/heartbeat",
            headers={"Authorization": "Bearer " + credential},
            json={
                "status": "online",
                "agent_version": "0.2.0",
                "nginx_version": "nginx/1.26.0",
                "config_hash": "abc123",
                "capabilities": ["nginx_test", "nginx_reload"],
                "facts": {"os": "linux", "arch": "x86_64", "ignored": "value"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        nodes = self.client.get("/api/v1/admin/nodes", headers=self.admin_headers).json()["items"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["status"], "online")
        self.assertNotIn("token_hash", nodes[0])

    def test_reject_enrollment_and_remove_manual_token_endpoint(self):
        request, _pending = self.start_enrollment("rejected-node")
        rejected = self.client.post(
            "/api/v1/admin/enrollments/{}/reject".format(request["enrollment_id"]),
            headers=self.admin_headers,
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        status_response = self.client.post("/api/v1/agent/enroll", json=request)
        self.assertEqual(status_response.json()["status"], "rejected")
        self.assertEqual(
            self.client.post(
                "/api/v1/admin/enrollment-tokens",
                headers=self.admin_headers,
                json={"node_name": "unused"},
            ).status_code,
            404,
        )

    def test_reenrollment_rotates_machine_credential_only_after_approval(self):
        first = self.enroll("same-node")
        request, pending = self.start_enrollment("same-node")
        self.assertEqual(pending["status"], "pending")
        old_headers = {"Authorization": "Bearer " + first["machine_credential"]}
        self.assertEqual(self.client.post("/api/v1/agent/poll", headers=old_headers, json={}).status_code, 200)

        approved = self.approve(request["enrollment_id"])
        second_credential = _derive_agent_credential(
            request["enrollment_secret"], request["enrollment_id"], approved["agent_id"]
        )
        new_headers = {"Authorization": "Bearer " + second_credential}
        self.assertEqual(self.client.post("/api/v1/agent/poll", headers=old_headers, json={}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/agent/poll", headers=new_headers, json={}).status_code, 200)

    def test_transactional_claim_and_idempotent_result(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "nginx_test",
                "payload": {"config_path": "/etc/nginx/nginx.conf"},
                "ttl_seconds": 60,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        job_id = created.json()["jobs"][0]["id"]
        first_poll = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={"limit": 1})
        second_poll = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={"limit": 1})
        self.assertEqual([job_id], [job["id"] for job in first_poll.json()["jobs"]])
        self.assertEqual([], second_poll.json()["jobs"])

        result_body = {
            "status": "succeeded",
            "exit_code": 0,
            "duration_ms": 12,
            "output": "nginx: configuration file syntax is ok",
            "details": {"syntax_ok": True, "previous_config_hash": "b" * 64},
        }
        result = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id), headers=agent_headers, json=result_body
        )
        repeated = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id), headers=agent_headers, json=result_body
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["idempotent"])
        self.assertTrue(repeated.json()["idempotent"])
        jobs = self.client.get("/api/v1/admin/jobs", headers=self.admin_headers).json()["items"]
        self.assertEqual(jobs[0]["status"], "succeeded")
        self.assertEqual(jobs[0]["result"]["previous_config_hash"], "b" * 64)
        self.assertNotIn("output", jobs[0]["result"])
        self.assertIn("output_sha256", jobs[0]["result"])

    def test_certificate_payload_is_redacted_after_claim(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        private_key = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "certificate_apply",
                "payload": {"certificate_pem": "cert", "private_key_pem": private_key},
                "ttl_seconds": 3600,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        job_id = created.json()["jobs"][0]["id"]
        polled = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={}).json()["jobs"]
        self.assertEqual(polled[0]["payload"]["private_key_pem"], private_key)
        with self.client.app.state.database.connection() as connection:
            row = connection.execute(
                "SELECT payload_json, expires_at, created_at FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        self.assertNotIn("PRIVATE KEY", row["payload_json"])
        self.assertLessEqual(row["expires_at"] - row["created_at"], 900)

    def test_config_delete_is_a_fixed_action_with_safe_result_metadata(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "config_delete",
                "payload": {
                    "path": "/etc/nginx/nginx-manager.d/example.conf",
                    "expected_sha256": "a" * 64,
                    "reload": True,
                },
                "ttl_seconds": 300,
            },
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["jobs"][0]["id"]
        polled = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={}).json()["jobs"]
        self.assertEqual("config_delete", polled[0]["action"])

        completed = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={
                "status": "succeeded",
                "details": {
                    "path": "/etc/nginx/nginx-manager.d/example.conf",
                    "previous_config_hash": "a" * 64,
                    "deleted": True,
                    "syntax_ok": True,
                    "reloaded": True,
                    "ignored": "not persisted",
                },
            },
        )
        self.assertEqual(200, completed.status_code, completed.text)
        jobs = self.client.get("/api/v1/admin/jobs", headers=self.admin_headers).json()["items"]
        result = jobs[0]["result"]
        self.assertTrue(result["deleted"])
        self.assertTrue(result["reloaded"])
        self.assertEqual("a" * 64, result["previous_config_hash"])
        self.assertNotIn("ignored", result)

    def test_ui_state_optimistic_lock_and_secret_rejection(self):
        initial = self.client.get("/api/v1/admin/ui-state", headers=self.admin_headers)
        self.assertEqual(initial.json(), {"revision": 0, "state": {}})
        saved = self.client.put(
            "/api/v1/admin/ui-state",
            headers=self.admin_headers,
            json={"revision": 0, "state": {"sites": [{"domain": "example.test", "note": "demo"}]}},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        conflict = self.client.put(
            "/api/v1/admin/ui-state",
            headers=self.admin_headers,
            json={"revision": 0, "state": {"sites": []}},
        )
        self.assertEqual(conflict.status_code, 409)
        rejected = self.client.put(
            "/api/v1/admin/ui-state",
            headers=self.admin_headers,
            json={"revision": 1, "state": {"private_key": "do-not-store"}},
        )
        self.assertEqual(rejected.status_code, 400)

    def test_installer_uses_password_bootstrap_and_relocatable_uvicorn(self):
        installer = (Path(__file__).resolve().parents[1] / "deploy" / "install-server.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("bootstrap-admin", installer)
        self.assertIn("管理员账号=${ADMIN_USERNAME}", installer)
        self.assertIn("管理员密码=${ADMIN_PASSWORD}", installer)
        self.assertNotIn("NGINX_MANAGER_ADMIN_TOKEN=${", installer)
        self.assertNotIn("seed-enrollment-token", installer)
        self.assertIn('exec_start="${CURRENT_LINK}/venv/bin/python -m uvicorn app:app', installer)
        self.assertIn("ExecStart=${exec_start}", installer)
        self.assertNotIn("ExecStart=${CURRENT_LINK}/venv/bin/uvicorn app:app", installer)
        self.assertIn("--behind-nginx", installer)
        self.assertIn("--host 127.0.0.1", installer)
        self.assertIn("NGINX_MANAGER_LDAP_ENABLED", installer)
        self.assertIn("NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE", installer)
        self.assertIn('sed "s|${CURRENT_LINK}|${NEW_RELEASE}|g"', installer)
        self.assertIn('systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null', installer)
        self.assertIn('chown -R root:"${APP_GROUP}" "${STAGING_DIR}"', installer)
        self.assertIn('runuser -u "${APP_USER}" -- "${NEW_RELEASE}/venv/bin/python"', installer)
        requirements = (Path(__file__).resolve().parents[1] / "server" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("ldap3==2.9.1", requirements)


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
import secrets
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

from app import (  # noqa: E402
    LDAPAuthenticationError,
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
        self.assertIn("__Host-nginx_manager_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

        rejected = self.client.put(
            "/api/v1/admin/ui-state",
            json={"revision": 0, "state": {}},
        )
        self.assertEqual(rejected.status_code, 403)

    def test_http_login_uses_separate_non_secure_cookie(self):
        with TestClient(self.client.app, base_url="http://testserver") as http_client:
            login = http_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "correct-horse-battery-staple"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            cookie = login.headers["set-cookie"]
            self.assertIn("nginx_manager_session=", cookie)
            self.assertNotIn("__Host-nginx_manager_session=", cookie)
            self.assertNotIn("Secure", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            self.assertNotIn("Strict-Transport-Security", login.headers)
            session = http_client.get("/api/v1/auth/session")
            self.assertEqual(session.status_code, 200, session.text)
            self.assertEqual(session.json()["username"], "admin")

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

    def test_ldap_session_role_is_rechecked_and_revoked(self):
        ldap_settings = replace(
            self.settings,
            ldap_enabled=True,
            ldap_url="ldap://directory.example.test:389",
            ldap_base_dn="dc=example,dc=test",
            ldap_bind_dn="cn=nginx-manager,ou=service,dc=example,dc=test",
            ldap_bind_password_file=str(Path(self.tempdir.name).resolve() / "ldap-password"),
            ldap_session_recheck_seconds=60,
        )
        current = {"role": "operator", "revoked": False}

        def authenticate(_settings, username, _password):
            return {
                "principal_id": "ldap:" + username,
                "username": username,
                "role": "operator",
                "auth_source": "ldap",
            }

        def check_role(_settings, username):
            if current["revoked"]:
                raise LDAPAuthenticationError("group membership removed")
            return {
                "principal_id": "ldap:" + username,
                "username": username,
                "role": current["role"],
                "auth_source": "ldap",
            }

        app = create_app(
            ldap_settings,
            ldap_authenticator=authenticate,
            ldap_role_checker=check_role,
        )
        with TestClient(app, base_url="https://testserver") as ldap_client:
            login = ldap_client.post(
                "/api/v1/auth/login", json={"username": "ops", "password": "directory-password"}
            )
            self.assertEqual(200, login.status_code, login.text)
            current["role"] = "auditor"
            with app.state.database.transaction() as connection:
                connection.execute("UPDATE web_sessions SET role_checked_at = 0")
            self.assertEqual(200, ldap_client.get("/api/v1/admin/nodes").status_code)
            denied = ldap_client.put(
                "/api/v1/admin/ui-state",
                headers={"X-CSRF-Token": login.json()["csrf_token"]},
                json={"revision": 0, "state": {}},
            )
            self.assertEqual(403, denied.status_code)
            current["revoked"] = True
            with app.state.database.transaction() as connection:
                connection.execute("UPDATE web_sessions SET role_checked_at = 0")
            self.assertEqual(401, ldap_client.get("/api/v1/admin/nodes").status_code)
            self.assertEqual(401, ldap_client.get("/api/v1/admin/nodes").status_code)

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

    def test_expired_lease_redelivers_same_job_and_accepts_late_result(self):
        enrolled = self.enroll("lease-node")
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "nginx_test",
                "payload": {"probe": "lease"},
                "ttl_seconds": 60,
            },
        ).json()["jobs"][0]
        first = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={}).json()["jobs"][0]
        self.assertEqual(created["id"], first["id"])
        with self.client.app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET lease_expires_at = 0 WHERE id = ?", (created["id"],)
            )
        second = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={}).json()["jobs"][0]
        self.assertEqual(created["id"], second["id"])
        with self.client.app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET expires_at = ? WHERE id = ?", (int(time.time()) - 1, created["id"])
            )
        late = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(created["id"]),
            headers=agent_headers,
            json={"status": "succeeded", "job_id": created["id"], "action": "nginx_test"},
        )
        self.assertEqual(200, late.status_code, late.text)
        self.assertEqual("succeeded", late.json()["status"])
        jobs = self.client.get(
            "/api/v1/admin/jobs?ids=" + created["id"], headers=self.admin_headers
        ).json()["items"]
        self.assertEqual(2, jobs[0]["attempt_count"])
        self.assertEqual("succeeded", jobs[0]["status"])

    def test_active_job_heartbeat_renews_only_its_lease(self):
        enrolled = self.enroll("lease-renew-node")
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={"node_ids": [enrolled["agent_id"]], "action": "nginx_test", "payload": {}},
        ).json()["jobs"][0]
        self.client.post("/api/v1/agent/poll", headers=agent_headers, json={})
        with self.client.app.state.database.transaction() as connection:
            connection.execute("UPDATE jobs SET lease_expires_at = 1 WHERE id = ?", (created["id"],))
        renewed = self.client.post(
            "/api/v1/agent/heartbeat",
            headers=agent_headers,
            json={"status": "online", "active_job_id": created["id"]},
        )
        self.assertEqual(200, renewed.status_code, renewed.text)
        self.assertTrue(renewed.json()["job_lease_renewed"])
        with self.client.app.state.database.connection() as connection:
            lease = connection.execute(
                "SELECT lease_expires_at FROM jobs WHERE id = ?", (created["id"],)
            ).fetchone()[0]
        self.assertGreater(lease, int(time.time()))

    def test_atomic_operation_publishes_immutable_revision(self):
        enrolled = self.enroll("operation-node")
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        request_id = "operation-test-0001"
        body = {
            "request_id": request_id,
            "site_id": "site-test-1",
            "kind": "publish",
            "base_version": 0,
            "candidate": {
                "id": "site-test-1",
                "domain": "example.test",
                "config": "server { listen 8080; }\n",
                "changeNote": "initial publish",
            },
            "jobs": [
                {
                    "node_id": enrolled["agent_id"],
                    "action": "config_apply",
                    "payload": {
                        "path": "/etc/nginx/nginx-manager.d/example.test.conf",
                        "content": "server { listen 8080; }\n",
                        "expected_sha256": "missing",
                    },
                }
            ],
            "ttl_seconds": 60,
        }
        created = self.client.post(
            "/api/v1/admin/operations", headers=self.admin_headers, json=body
        )
        self.assertEqual(201, created.status_code, created.text)
        repeated = self.client.post(
            "/api/v1/admin/operations", headers=self.admin_headers, json=body
        )
        self.assertTrue(repeated.json()["idempotent"])
        job_id = created.json()["jobs"][0]["id"]
        self.client.post("/api/v1/agent/poll", headers=agent_headers, json={})
        result = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={
                "status": "succeeded",
                "job_id": job_id,
                "action": "config_apply",
                "details": {"config_hash": "a" * 64, "path": "/etc/nginx/nginx-manager.d/example.test.conf"},
            },
        )
        self.assertEqual(200, result.status_code, result.text)
        operation = self.client.get(
            "/api/v1/admin/operations/" + request_id, headers=self.admin_headers
        ).json()
        self.assertEqual("succeeded", operation["operation"]["status"])
        self.assertEqual("admin", operation["jobs"][0]["created_by"])
        revision = self.client.get(
            "/api/v1/admin/sites/site-test-1/revisions/1", headers=self.admin_headers
        )
        self.assertEqual(200, revision.status_code, revision.text)
        self.assertEqual("server { listen 8080; }\n", revision.json()["snapshot"]["config"])

    def test_failed_publish_does_not_consume_the_candidate_version(self):
        enrolled = self.enroll("failed-publish-node")
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        body = {
            "request_id": "operation-failure-0001",
            "site_id": "site-failed-publish",
            "kind": "publish",
            "base_version": 0,
            "candidate": {"id": "site-failed-publish", "config": "server { listen 80; }"},
            "jobs": [{
                "node_id": enrolled["agent_id"],
                "action": "config_apply",
                "payload": {"path": "/etc/nginx/site.conf", "content": "server { listen 80; }"},
            }],
        }
        created = self.client.post(
            "/api/v1/admin/operations", headers=self.admin_headers, json=body
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["jobs"][0]["id"]
        self.client.post("/api/v1/agent/poll", headers=agent_headers, json={})
        failed = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={"status": "failed", "job_id": job_id, "action": "config_apply"},
        )
        self.assertEqual(200, failed.status_code, failed.text)
        revisions = self.client.get(
            "/api/v1/admin/sites/site-failed-publish/revisions", headers=self.admin_headers
        ).json()["items"]
        self.assertEqual([], revisions)
        body["request_id"] = "operation-failure-0002"
        retry = self.client.post(
            "/api/v1/admin/operations", headers=self.admin_headers, json=body
        )
        self.assertEqual(201, retry.status_code, retry.text)
        self.assertEqual(1, retry.json()["operation"]["base_version"] + 1)

    def test_admin_can_revoke_agent_credential(self):
        enrolled = self.enroll("revoked-node")
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        revoked = self.client.post(
            "/api/v1/admin/nodes/{}/revoke".format(enrolled["agent_id"]),
            headers=self.admin_headers,
        )
        self.assertEqual(200, revoked.status_code, revoked.text)
        self.assertEqual(
            401,
            self.client.post("/api/v1/agent/poll", headers=agent_headers, json={}).status_code,
        )

    def test_failed_jobs_expose_only_safe_failure_metadata(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}

        def create_claim_and_fail(details, error, output):
            created = self.client.post(
                "/api/v1/admin/jobs",
                headers=self.admin_headers,
                json={
                    "node_ids": [enrolled["agent_id"]],
                    "action": "config_apply",
                    "payload": {
                        "path": "/etc/nginx/nginx-manager.d/failure-test.conf",
                        "content": "server { listen 8080; }\n",
                        "expected_sha256": "missing",
                    },
                    "ttl_seconds": 60,
                },
            )
            self.assertEqual(201, created.status_code, created.text)
            job_id = created.json()["jobs"][0]["id"]
            polled = self.client.post(
                "/api/v1/agent/poll", headers=agent_headers, json={"limit": 1}
            )
            self.assertEqual(job_id, polled.json()["jobs"][0]["id"])
            failed = self.client.post(
                "/api/v1/agent/jobs/{}/result".format(job_id),
                headers=agent_headers,
                json={
                    "status": "failed",
                    "job_id": job_id,
                    "action": "config_apply",
                    "exit_code": 1,
                    "duration_ms": 37,
                    "error": error,
                    "output": output,
                    "details": details,
                },
            )
            self.assertEqual(200, failed.status_code, failed.text)
            return job_id

        raw_error = "nginx: [emerg] unknown directive; private-token=must-not-be-returned"
        raw_output = "raw nginx stderr and configuration content must not be returned"
        known_id = create_claim_and_fail(
            {
                "failure_code": "nginx_config_test_failed",
                "failure_stage": "nginx_test",
                "rollback_status": "restored",
                "nginx_error_code": "certificate_file_missing",
                "nginx_error_line": 17,
                "syntax_ok": False,
                "failure_summary": "arbitrary agent supplied text must be discarded",
            },
            raw_error,
            raw_output,
        )
        unknown_id = create_claim_and_fail(
            {
                "failure_code": "run_arbitrary_command",
                "failure_stage": "shell",
                "rollback_status": "maybe",
                "syntax_ok": False,
            },
            "unknown enum failure text",
            "unknown enum output",
        )
        invalid_diagnostic_id = create_claim_and_fail(
            {
                "failure_code": "nginx_config_test_failed",
                "failure_stage": "nginx_test",
                "nginx_error_code": "return_raw_stderr",
                "nginx_error_line": "17",
                "syntax_ok": False,
            },
            "diagnostic enum must remain bounded",
            "diagnostic output must remain digest-only",
        )

        jobs = self.client.get("/api/v1/admin/jobs", headers=self.admin_headers).json()["items"]
        by_id = {job["id"]: job for job in jobs}
        known = by_id[known_id]
        self.assertEqual("failed", known["status"])
        self.assertEqual("nginx_config_test_failed", known["result"]["failure_code"])
        self.assertEqual("nginx_test", known["result"]["failure_stage"])
        self.assertEqual("restored", known["result"]["rollback_status"])
        self.assertEqual("certificate_file_missing", known["result"]["nginx_error_code"])
        self.assertEqual(17, known["result"]["nginx_error_line"])
        self.assertFalse(known["result"]["syntax_ok"])
        self.assertEqual(1, known["result"]["exit_code"])
        self.assertEqual(len(raw_error.encode("utf-8")), known["result"]["error_bytes"])
        self.assertEqual(hashlib.sha256(raw_error.encode("utf-8")).hexdigest(), known["result"]["error_sha256"])
        self.assertNotIn("error", known["result"])
        self.assertNotIn("output", known["result"])
        self.assertNotIn("failure_summary", known["result"])
        serialized = json.dumps(known, ensure_ascii=False)
        self.assertNotIn(raw_error, serialized)
        self.assertNotIn(raw_output, serialized)
        self.assertNotIn("arbitrary agent supplied text", serialized)

        unknown = by_id[unknown_id]
        self.assertEqual("failed", unknown["status"])
        self.assertFalse(unknown["result"]["syntax_ok"])
        self.assertNotIn("failure_code", unknown["result"])
        self.assertNotIn("failure_stage", unknown["result"])
        self.assertNotIn("rollback_status", unknown["result"])

        invalid_diagnostic = by_id[invalid_diagnostic_id]
        self.assertEqual("nginx_config_test_failed", invalid_diagnostic["result"]["failure_code"])
        self.assertEqual("nginx_test", invalid_diagnostic["result"]["failure_stage"])
        self.assertNotIn("nginx_error_code", invalid_diagnostic["result"])
        self.assertNotIn("nginx_error_line", invalid_diagnostic["result"])

    def test_server_expired_job_exposes_safe_queue_failure_metadata(self):
        enrolled = self.enroll()
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "nginx_test",
                "payload": {},
                "ttl_seconds": 60,
            },
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["jobs"][0]["id"]

        # Move the deadline into the past without sleeping; listing jobs performs
        # the same expiry sweep used by polling and snapshot endpoints.
        with self.client.app.state.database.transaction() as connection:
            connection.execute("UPDATE jobs SET expires_at = 0 WHERE id = ?", (job_id,))

        response = self.client.get("/api/v1/admin/jobs", headers=self.admin_headers)
        self.assertEqual(200, response.status_code, response.text)
        job = next(item for item in response.json()["items"] if item["id"] == job_id)
        self.assertEqual("expired", job["status"])
        self.assertEqual(
            {"failure_code": "job_expired", "failure_stage": "queue"},
            job["result"],
        )

    def test_config_inventory_result_is_bounded_validated_and_visible_to_web(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "config_inventory",
                "payload": {},
                "ttl_seconds": 60,
            },
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["jobs"][0]["id"]
        polled = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={"limit": 1})
        self.assertEqual("config_inventory", polled.json()["jobs"][0]["action"])

        content = "server { listen 80; server_name imported.example.com; }\n"
        digest = hashlib.sha256(content.encode()).hexdigest()
        result = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={
                "status": "succeeded",
                "job_id": job_id,
                "action": "config_inventory",
                "details": {
                    "files": [
                        {
                            "path": "/apps/nginx/conf/conf.d/imported.conf",
                            "content": content,
                            "sha256": digest,
                            "size": len(content.encode()),
                        },
                        {
                            "path": "/apps/nginx/conf/conf.d/tampered.conf",
                            "content": content,
                            "sha256": "0" * 64,
                            "size": len(content.encode()),
                        },
                    ],
                    "skipped_count": 1,
                    "truncated": False,
                },
            },
        )
        self.assertEqual(200, result.status_code, result.text)
        jobs = self.client.get(
            "/api/v1/admin/jobs?action=config_inventory",
            headers=self.admin_headers,
        ).json()["items"]
        inventory = jobs[0]["result"]["config_inventory"]
        self.assertEqual(1, inventory["file_count"])
        self.assertEqual(2, inventory["skipped_count"])
        self.assertEqual(content, inventory["files"][0]["content"])
        self.assertEqual(digest, inventory["files"][0]["sha256"])
        self.assertNotIn("tampered.conf", json.dumps(inventory))

    def test_certificate_inventory_exposes_paths_and_hashes_but_no_private_key_content(self):
        enrolled = self.enroll()
        agent_headers = {"Authorization": "Bearer " + enrolled["machine_credential"]}
        created = self.client.post(
            "/api/v1/admin/jobs",
            headers=self.admin_headers,
            json={
                "node_ids": [enrolled["agent_id"]],
                "action": "certificate_inventory",
                "payload": {},
                "ttl_seconds": 60,
            },
        )
        self.assertEqual(201, created.status_code, created.text)
        job_id = created.json()["jobs"][0]["id"]
        polled = self.client.post("/api/v1/agent/poll", headers=agent_headers, json={"limit": 1})
        self.assertEqual("certificate_inventory", polled.json()["jobs"][0]["action"])
        valid = {
            "certificate_path": "/apps/nginx/cert/int.example.pem",
            "private_key_path": "/apps/nginx/cert/int.example.key",
            "certificate_sha256": "a" * 64,
            "key_material_sha256": "b" * 64,
            "fingerprint": ":".join(["AB"] * 32),
            "not_after": "2027-01-01T00:00:00Z",
            "days_remaining": 170,
            "issuer": "Test CA",
            "subject": "int.example.test",
            "domains": ["int.example.test", "*.int.example.test"],
        }
        invalid = dict(valid)
        invalid["certificate_path"] = "/apps/nginx/cert/rejected.pem"
        invalid["key_material_sha256"] = "not-a-hash"
        result = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={
                "status": "succeeded",
                "job_id": job_id,
                "action": "certificate_inventory",
                "details": {
                    "certificates": [valid, invalid],
                    "skipped_count": 1,
                    "truncated": False,
                },
            },
        )
        self.assertEqual(200, result.status_code, result.text)
        jobs = self.client.get(
            "/api/v1/admin/jobs?action=certificate_inventory",
            headers=self.admin_headers,
        ).json()["items"]
        inventory = jobs[0]["result"]["certificate_inventory"]
        self.assertEqual(1, inventory["certificate_count"])
        self.assertEqual(2, inventory["skipped_count"])
        self.assertEqual(valid["private_key_path"], inventory["certificates"][0]["private_key_path"])
        self.assertEqual(valid["key_material_sha256"], inventory["certificates"][0]["key_material_sha256"])
        self.assertNotIn("PRIVATE KEY", json.dumps(inventory))
        self.assertNotIn("rejected.pem", json.dumps(inventory))

    def test_certificate_payload_is_retained_for_lease_retry_then_redacted(self):
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
        self.assertIn("PRIVATE KEY", row["payload_json"])
        self.assertLessEqual(row["expires_at"] - row["created_at"], 900)
        completed = self.client.post(
            "/api/v1/agent/jobs/{}/result".format(job_id),
            headers=agent_headers,
            json={"status": "failed", "job_id": job_id, "action": "certificate_apply"},
        )
        self.assertEqual(200, completed.status_code, completed.text)
        with self.client.app.state.database.connection() as connection:
            redacted = connection.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["payload_json"]
        self.assertNotIn("PRIVATE KEY", redacted)

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

    def test_ui_resources_are_split_from_the_compact_state_document(self):
        document = {
            "sites": [{"id": "site-a", "domain": "a.example.test", "config": "server {}"}],
            "certificates": [{"id": "cert-a", "domain": "*.example.test", "issuer": "Test CA"}],
            "runs": [{"id": "run-a"}],
        }
        saved = self.client.put(
            "/api/v1/admin/ui-state",
            headers=self.admin_headers,
            json={"revision": 0, "state": document},
        )
        self.assertEqual(200, saved.status_code, saved.text)
        loaded = self.client.get("/api/v1/admin/ui-state", headers=self.admin_headers).json()
        self.assertEqual("a.example.test", loaded["state"]["sites"][0]["domain"])
        self.assertEqual("*.example.test", loaded["state"]["certificates"][0]["domain"])
        with self.client.app.state.database.connection() as connection:
            compact = connection.execute(
                "SELECT state_json FROM ui_state WHERE singleton_id = 1"
            ).fetchone()[0]
            resources = connection.execute(
                "SELECT kind, id FROM resources ORDER BY kind, id"
            ).fetchall()
        self.assertNotIn("a.example.test", compact)
        self.assertNotIn("*.example.test", compact)
        self.assertEqual([("certificate", "cert-a"), ("site", "site-a")], [tuple(row) for row in resources])

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
        self.assertIn("--allow-direct-http", installer)
        self.assertIn('DIRECT_HTTP="0"', installer)
        self.assertIn('DIRECT_HTTP="1"', installer)
        self.assertIn('PUBLIC_URL="http://${MANAGER_HOST}:${LISTEN_PORT}"', installer)
        self.assertIn('local bind_host="127.0.0.1"', installer)
        self.assertIn('bind_host="0.0.0.0"', installer)
        self.assertIn("--host ${bind_host}", installer)
        self.assertIn("NGINX_MANAGER_LDAP_ENABLED", installer)
        self.assertIn("NGINX_MANAGER_LDAP_BIND_PASSWORD_FILE", installer)
        self.assertIn('sed "s|${CURRENT_LINK}|${NEW_RELEASE}|g"', installer)
        self.assertIn('systemctl is-enabled --quiet "${APP_NAME}.service" 2>/dev/null', installer)
        self.assertIn('chown -R root:"${APP_GROUP}" "${STAGING_DIR}"', installer)
        self.assertIn('runuser -u "${APP_USER}" -- "${NEW_RELEASE}/venv/bin/python"', installer)
        self.assertIn("prune_old_releases", installer)
        self.assertIn("NGINX_MANAGER_RELEASE_RETENTION", installer)
        bootstrap = (Path(__file__).resolve().parents[1] / "install-server.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("NGINX_MANAGER_REQUIRE_PINNED_REF", bootstrap)
        self.assertIn("NGINX_MANAGER_ARCHIVE_SHA256", bootstrap)
        requirements = (Path(__file__).resolve().parents[1] / "server" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("ldap3==2.9.1", requirements)

    def test_server_uninstaller_preserves_data_unless_purge_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        bootstrap = (root / "uninstall-server.sh").read_text(encoding="utf-8")
        uninstaller = (root / "deploy" / "uninstall-server.sh").read_text(encoding="utf-8")
        self.assertIn("deploy/uninstall-server.sh", bootstrap)
        self.assertIn('--purge) PURGE="1"', uninstaller)
        self.assertIn("nginx-manager-uninstall-", uninstaller)
        self.assertIn('rm -rf -- "${APP_ROOT}"', uninstaller)
        self.assertIn('if [[ "${PURGE}" == "1" ]]', uninstaller)
        self.assertIn('rm -rf -- "${ETC_DIR}" "${DATA_DIR}"', uninstaller)


if __name__ == "__main__":
    unittest.main()

import shutil
import subprocess
import unittest
from pathlib import Path


class ConsoleTestCase(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the inline console regression test")
    def test_certificate_paths_and_wildcard_scope_follow_node_inventory(self):
        html_path = Path(__file__).resolve().parents[1] / "nginx-cluster-console.html"
        script = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const inlineStart = html.lastIndexOf("<script>");
const inlineEnd = html.lastIndexOf("</script>");
if (inlineStart < 0 || inlineEnd <= inlineStart) throw new Error("inline script not found");
new Function(html.slice(inlineStart + "<script>".length, inlineEnd));
if (!html.includes('id="toast" role="status" aria-live="polite" popover="manual"')
    || !html.includes("toast.showPopover()")) {
  throw new Error("toast is not promoted into the browser top layer");
}
if (!html.includes('class="certificate-grid"')
    || !html.includes('certificate-card--danger')
    || !html.includes('node-card--online')
    || !html.includes('summary-card--certificates')) {
  throw new Error("node and certificate risk cards are missing from the operations dashboard theme");
}
if (!html.includes('data-nav="logs"')
    || !html.includes('data-nav="monitoring"')
    || !html.includes('id="live-log-output"')
    || !html.includes('class="monitor-charts"')
    || !html.includes("logLines.length > 5000")) {
  throw new Error("the runtime log and monitoring workspaces are incomplete");
}
if (html.includes('data-action="download-live-log"')) {
  throw new Error("the live log workspace must not expose a download action");
}
if (!html.includes('data-copy-label="证书路径"')
    || !html.includes("runWithBusyButton(actionTarget")) {
  throw new Error("readable path-copy and button-busy interactions are missing");
}
if (!html.includes('class="cert-node-header"')
    || !html.includes('cert-location--offline')
    || !html.includes("node.online ? '在线节点' : '节点离线'")) {
  throw new Error("certificate paths are not visibly grouped by node and online state");
}
if (!html.includes('class="smart-select-menu"')
    || !html.includes('role="listbox"')
    || !html.includes("['ArrowDown', 'ArrowUp', 'Enter', ' ']")
    || !html.includes("['site-node-filter', 'site-status-filter', 'config-certificate']")) {
  throw new Error("the high-frequency filters are missing the accessible smart-select interaction");
}
if (!html.includes('data-site-create-mode="generic"')
    || !html.includes('value="generic-stub-status"')
    || !html.includes('value="upstream-https"')
    || !html.includes('value="websocket-long"')
    || !html.includes('data-action="convert-generic"')) {
  throw new Error("the unified site/generic Conf workflow or sanitized templates are missing");
}
if (!html.includes("applySiteConfTemplate(true, event.target.value, false)")
    || !html.includes("event.target.value = previousTemplate")) {
  throw new Error("selecting a Conf template must apply it immediately and restore the previous choice when cancelled");
}
if ((html.match(/'delete-config', 'delete-site-record'/g) || []).length < 2) {
  throw new Error("platform record deletion is not protected by both action and visibility permissions");
}
if ((html.match(/applyRemoteUiStateDocument\(error\.body\)/g) || []).length < 2) {
  throw new Error("revision conflicts do not consistently reload the authoritative remote UI state");
}
function take(startName, endName) {
  const start = html.indexOf("      " + startName);
  const end = html.indexOf("\n      " + endName, start);
  if (start < 0 || end < 0) throw new Error("cannot extract " + startName);
  return html.slice(start, end);
}
eval(take("function sha256Hex", "function roleLabel"));
eval(take("function safeResourceName", "function managedConfigFilename"));
eval(take("function managedConfigFilename", "function migrationConfigPath"));
eval(take("function managedCertificateRoot", "function certificateTargetPaths"));
eval(take("function certificateTargetPaths", "function renderCertificateNodeChoices"));
eval(take("function certificateCoversDomain", "function getSite"));
eval(take("function normalizeSiteDeploymentStates", "function loadState"));
eval(take("function capturePendingRemoteState", "function relativeTime"));
eval(take("function siteHasUnpublishedChanges", "function runStatus"));
eval(take("function normalizeProxyTarget", "function defaultConfig"));
eval(take("function defaultConfig", "function configCertificateState"));
eval(take("function rewriteConfigCertificatePaths", "function openConfigEditor"));
eval(take("function canDeleteSiteRecord", "async function deletePlatformSiteRecord"));
eval(take("function rememberProcessedOperation", "function stripNginxComments"));
eval(take("function stripNginxComments", "function nginxStatements"));
eval(take("function nginxStatements", "function importInventoryFile"));
eval(take("function managedConfigContent", "async function startRemoteConfigRun"));

const node = { id: "node-1", managedCertificateRoot: "/apps/nginx/cert" };
const wrongWildcard = {
  domain: "*.itbkcmdb.int.hypergryph.com",
  nodePaths: {
    "node-1": {
      certificatePath: "/apps/nginx/cert/itbkcmdb.crt",
      keyPath: "/apps/nginx/cert/itbkcmdb.key"
    }
  }
};
const correctWildcard = {
  domain: "*.int.hypergryph.com",
  nodePaths: {
    "node-1": {
      certificatePath: "/apps/nginx/cert/int.hypergryph.com.pem",
      keyPath: "/apps/nginx/cert/int.hypergryph.com.key"
    }
  }
};
const state = { nodes: [node], sites: [], certificates: [] };
let activeCertificate = wrongWildcard;
function getCert() { return activeCertificate; }
function getNode(nodeId) { return state.nodes.find((item) => item.id === nodeId); }
function getSite(siteId) { return state.sites.find((item) => item.id === siteId); }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function buildSiteFailure(jobs, pending) {
  return { summary: "validation failed", items: [], operation: pending.operation || "" };
}
function saveState() {}
function notify() {}

const statusMetadata = inventorySiteMetadata({
  path: "/apps/nginx/conf/conf.d/nginx-status.conf",
  content: "server { listen 127.0.0.1:18080; server_name localhost; location = /nginx_status { stub_status; } }"
});
if (statusMetadata.resourceType !== "generic" || statusMetadata.name !== "Nginx Status") {
  throw new Error("a loopback stub_status fragment was imported as a fake domain site");
}
const upstreamMetadata = inventorySiteMetadata({
  path: "/apps/nginx/conf/conf.d/backend-pool.conf",
  content: "upstream backend_pool { server 192.0.2.21:8080; }"
});
if (upstreamMetadata.resourceType !== "generic") {
  throw new Error("an upstream-only fragment was not classified as generic Conf");
}
const businessMetadata = inventorySiteMetadata({
  path: "/apps/nginx/conf/conf.d/api.conf",
  content: "upstream backend { server 192.0.2.21:8080; } server { listen 443 ssl; server_name api.example.com; }"
});
if (businessMetadata.resourceType !== "site" || businessMetadata.domain !== "api.example.com") {
  throw new Error("a business server_name configuration was not classified as a site Conf");
}
const genericRecord = { resourceType: "generic", name: "Nginx Status", filename: "nginx-status.conf" };
if (resourceTitle(genericRecord) !== "Nginx Status" || managedConfigFilename(genericRecord) !== "nginx-status.conf") {
  throw new Error("generic Conf identity or safe filename was lost");
}

const staleValidationDraft = {
  id: "site-stale-validation-draft",
  version: 1,
  status: "draft",
  nodeIds: ["node-1"],
  nodeHashes: {},
  configMode: "conf",
  config: "server { listen 80; }",
  certificateId: ""
};
activeCertificate = null;
staleValidationDraft.nodeHashes["node-1"] = sha256Hex(staleValidationDraft.config);
if (!normalizeNoopDraftStates([staleValidationDraft]) || staleValidationDraft.status !== "published") {
  throw new Error("a legacy no-op validation draft was not restored to published");
}
const realChangedDraft = Object.assign({}, staleValidationDraft, {
  id: "site-real-draft",
  status: "draft",
  config: "server { listen 8080; }"
});
if (normalizeNoopDraftStates([realChangedDraft]) || realChangedDraft.status !== "draft") {
  throw new Error("a real config draft was incorrectly normalized to published");
}
activeCertificate = wrongWildcard;

const remoteConflictSite = {
  id: "site-conflict",
  version: 1,
  status: "published",
  nodeIds: ["node-1"],
  nodeHashes: { "node-1": "old-hash" },
  configMode: "conf",
  config: "server { listen 80; }",
  certificateId: ""
};
const conflictingPending = {
  operation: "validate",
  publish: false,
  baseStatus: "published",
  jobs: [{ id: "conflict-validation", nodeId: "node-1", candidateHash: sha256Hex("server { listen 8080; }") }]
};
state.sites = [remoteConflictSite];
state.certificates = [];
const restoredConflict = restorePendingRemoteState({
  sites: [Object.assign({}, remoteConflictSite, { pendingRemote: conflictingPending })],
  certificates: []
});
if (!restoredConflict.changed || !restoredConflict.conflicts || !remoteConflictSite.pendingRemote) {
  throw new Error("a UI-state conflict discarded an already submitted validation job");
}
if (remoteConflictSite.status !== "published" || !remoteConflictSite.pendingRemote.submissionFailure) {
  throw new Error("a conflicting validation job changed lifecycle state or lost its conflict marker");
}

const legacyRemovedSite = {
  version: 1,
  status: "draft",
  nodeIds: [],
  changeNote: "从节点移除配置：edge-a-01"
};
if (!normalizeSiteDeploymentStates([legacyRemovedSite])
    || legacyRemovedSite.status !== "unassigned"
    || legacyRemovedSite.version !== 1) {
  throw new Error("a retained site from the old delete flow was not migrated without a version bump");
}

const unassignedSite = {
  id: "site-unassigned",
  domain: "old.example.com",
  version: 1,
  status: "unassigned",
  nodeIds: [],
  nodeHashes: {},
  nodeConfigPaths: {},
  nodeConfigs: {}
};
const deployedSite = {
  id: "site-deployed",
  domain: "live.example.com",
  version: 1,
  status: "published",
  nodeIds: ["node-1"]
};
if (siteStatus(unassignedSite).label !== "未部署" || siteVersionLabel(unassignedSite) !== "v1") {
  throw new Error("an unassigned site still looks like a v2 draft");
}
if (siteVersionLabel({ version: 1, status: "draft" }) !== "v1 → v2") {
  throw new Error("a real draft lost its candidate-version label");
}
state.sites = [unassignedSite, deployedSite];
state.certificates = [{ id: "cert-1", siteIds: [unassignedSite.id, deployedSite.id], nodeIds: ["node-1"] }];
if (!removeSiteRecordFromState(unassignedSite)) {
  throw new Error("an unassigned platform record could not be deleted");
}
if (state.sites.some((site) => site.id === unassignedSite.id)
    || state.certificates[0].siteIds.includes(unassignedSite.id)
    || state.certificates[0].nodeIds.length !== 1) {
  throw new Error("platform record deletion did not clean only the certificate reverse reference");
}
if (removeSiteRecordFromState(deployedSite)) {
  throw new Error("a deployed site record was deleted before its node config");
}

const publishedValidation = {
  id: "site-validation-published",
  version: 1,
  status: "published",
  nodeIds: ["node-1"],
  pendingRemote: {
    jobs: [{ id: "validate-success", nodeId: "node-1" }],
    publish: false,
    operation: "validate",
    baseStatus: "published"
  }
};
state.sites = [publishedValidation];
state.certificates = [];
if (siteStatus(publishedValidation).label !== "校验中" || siteVersionLabel(publishedValidation) !== "v1") {
  throw new Error("a read-only validation looks like a v2 draft while it is running");
}
applyRemoteJobOutcomes([{ id: "validate-success", status: "succeeded" }]);
if (publishedValidation.status !== "published" || publishedValidation.version !== 1 || publishedValidation.pendingRemote) {
  throw new Error("a successful read-only validation changed the published lifecycle or version");
}

const failedValidation = {
  id: "site-validation-failed",
  version: 1,
  status: "published",
  nodeIds: ["node-1"],
  pendingRemote: {
    jobs: [{ id: "validate-failed", nodeId: "node-1" }],
    publish: false,
    operation: "validate",
    baseStatus: "published"
  }
};
state.sites = [failedValidation];
applyRemoteJobOutcomes([{ id: "validate-failed", status: "failed" }]);
if (failedValidation.status !== "published"
    || failedValidation.version !== 1
    || siteStatus(failedValidation).label !== "校验失败") {
  throw new Error("a failed read-only validation replaced the published lifecycle state");
}

const draftValidation = {
  id: "site-validation-draft",
  version: 1,
  status: "draft",
  nodeIds: ["node-1"],
  pendingRemote: {
    jobs: [{ id: "validate-draft", nodeId: "node-1" }],
    publish: false,
    operation: "validate",
    baseStatus: "draft"
  }
};
state.sites = [draftValidation];
applyRemoteJobOutcomes([{ id: "validate-draft", status: "succeeded" }]);
if (draftValidation.status !== "draft" || draftValidation.version !== 1) {
  throw new Error("validating a real draft discarded or published the draft");
}

const successfulPublish = {
  id: "site-publish",
  version: 1,
  status: "publishing",
  nodeIds: ["node-1"],
  pendingRemote: {
    jobs: [{ id: "publish-success", nodeId: "node-1", path: "/etc/nginx/site.conf" }],
    publish: true,
    operation: "publish",
    baseStatus: "draft"
  }
};
state.sites = [successfulPublish];
applyRemoteJobOutcomes([{ id: "publish-success", status: "succeeded", result: { config_hash: "abc" } }]);
if (successfulPublish.status !== "published" || successfulPublish.version !== 2) {
  throw new Error("a successful real publish no longer increments the version exactly once");
}

const partialPublish = {
  id: "site-partial-publish",
  version: 3,
  status: "publishing",
  nodeIds: ["node-1", "node-2"],
  nodeHashes: { "node-1": "old-1", "node-2": "old-2" },
  pendingRemote: {
    jobs: [
      { id: "publish-partial-success", nodeId: "node-1", path: "/etc/nginx/site.conf" },
      { id: "publish-partial-failed", nodeId: "node-2", path: "/etc/nginx/site.conf" }
    ],
    publish: true,
    operation: "publish",
    baseStatus: "draft"
  }
};
state.sites = [partialPublish];
applyRemoteJobOutcomes([
  { id: "publish-partial-success", status: "succeeded", result: { config_hash: "new-1" } },
  { id: "publish-partial-failed", status: "failed", result: {} }
]);
if (partialPublish.version !== 3 || partialPublish.status !== "failed") {
  throw new Error("a partial publish failure incorrectly incremented the version");
}
if (partialPublish.nodeHashes["node-1"] !== "new-1" || partialPublish.nodeHashes["node-2"] !== "old-2") {
  throw new Error("a partial publish failure lost the successful node hash needed for retry");
}

if (normalizeProxyTarget("10.165.0.29:8080") !== "http://10.165.0.29:8080") {
  throw new Error("a bare guided proxy target did not receive the HTTP scheme");
}
if (normalizeProxyTarget("https://backend.example:8443/api/") !== "https://backend.example:8443/api/") {
  throw new Error("an explicit HTTPS target was changed");
}
if (normalizeProxyTarget("$backend") !== "$backend" || normalizeProxyTarget("unix:/run/nginx.sock") !== "unix:/run/nginx.sock") {
  throw new Error("an advanced proxy target was silently rewritten");
}
const guidedSite = {
  configMode: "guided",
  type: "proxy",
  target: "10.165.0.29:8080",
  config: "server {\n  location / {\n    proxy_pass 10.165.0.29:8080;\n  }\n}"
};
if (!normalizeGuidedSiteConfig(guidedSite)
    || guidedSite.target !== "http://10.165.0.29:8080"
    || !guidedSite.config.includes("proxy_pass http://10.165.0.29:8080;")) {
  throw new Error("an existing guided draft was not repaired");
}
const customSite = {
  configMode: "conf",
  type: "proxy",
  target: "10.165.0.29:8080",
  config: "proxy_pass 10.165.0.29:8080;"
};
if (normalizeGuidedSiteConfig(customSite) || customSite.config.includes("http://")) {
  throw new Error("custom Conf content was silently rewritten");
}

if (certificateCoversDomain(wrongWildcard, "test.int.hypergryph.com")) {
  throw new Error("a wildcard matched across more than one DNS label");
}
if (!certificateCoversDomain(correctWildcard, "test.int.hypergryph.com")) {
  throw new Error("the correct one-label wildcard did not match");
}

const fresh = defaultConfig(
  "test.int.hypergryph.com", "proxy", "127.0.0.1:8080", wrongWildcard, node
);
if (!fresh.includes("/apps/nginx/cert/itbkcmdb.crt")) {
  throw new Error("a new guided config ignored the scanned node certificate path");
}
if (!fresh.includes("proxy_pass http://127.0.0.1:8080;")) {
  throw new Error("a new guided config retained an invalid bare proxy target");
}

const oldSite = {
  domain: "test.int.hypergryph.com",
  certificateId: "cert-1",
  config: "server {\n"
    + " ssl_certificate /etc/nginx/ssl/nginx-manager/_.itbkcmdb.int.hypergryph.com.crt;\n"
    + " ssl_certificate_key /etc/nginx/ssl/nginx-manager/_.itbkcmdb.int.hypergryph.com.key;\n}"
};
const migrated = managedConfigContent(oldSite, node);
if (!migrated.includes("/apps/nginx/cert/itbkcmdb.crt") || migrated.includes("_.itbkcmdb")) {
  throw new Error("an existing guided config did not migrate to the scanned path");
}
const rebound = rewriteConfigCertificatePaths(migrated, correctWildcard, node);
if (!rebound.includes("/apps/nginx/cert/int.hypergryph.com.pem")
    || !rebound.includes("/apps/nginx/cert/int.hypergryph.com.key")) {
  throw new Error("rebinding did not update both certificate directives");
}
"""
        completed = subprocess.run(
            [shutil.which("node"), "-e", script, str(html_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()

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
if (!html.includes('id="toast" role="status" aria-live="polite" popover="manual"')
    || !html.includes("toast.showPopover()")) {
  throw new Error("toast is not promoted into the browser top layer");
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
eval(take("function safeResourceName", "function managedConfigFilename"));
eval(take("function managedCertificateRoot", "function certificateTargetPaths"));
eval(take("function certificateTargetPaths", "function renderCertificateNodeChoices"));
eval(take("function certificateCoversDomain", "function getSite"));
eval(take("function normalizeSiteDeploymentStates", "function loadState"));
eval(take("function siteStatus", "function runStatus"));
eval(take("function normalizeProxyTarget", "function defaultConfig"));
eval(take("function defaultConfig", "function configCertificateState"));
eval(take("function rewriteConfigCertificatePaths", "function openConfigEditor"));
eval(take("function canDeleteSiteRecord", "async function deletePlatformSiteRecord"));
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

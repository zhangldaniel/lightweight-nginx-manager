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
eval(take("function normalizeProxyTarget", "function defaultConfig"));
eval(take("function defaultConfig", "function configCertificateState"));
eval(take("function rewriteConfigCertificatePaths", "function openConfigEditor"));
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
const state = { nodes: [node] };
let activeCertificate = wrongWildcard;
function getCert() { return activeCertificate; }

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

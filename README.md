# Lightweight Nginx Manager

轻量、自托管的多节点 Nginx 管理台：FastAPI + SQLite + 单 HTML 页面，节点通过 Linux Agent 主动连接控制端。

主要能力：节点审批接入、配置发布/复制/删除、`nginx -t`、reload、证书替换、LDAP/AD、RBAC、审计记录和失败回滚。不提供任意 Shell。

## 一键安装

### 1. 安装 Server（默认 HTTP）

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-server.sh | \
sudo bash -s -- \
  --host 192.0.2.20 \
  --port 8443 \
  --open-firewall
```

访问：`http://192.0.2.20:8443`

默认登录信息：

- 账号：`admin`
- 密码：没有固定值，首次安装自动生成 48 位随机密码
- 查看密码：`sudo cat /root/nginx-manager-credentials.txt`

升级不会重置已有账号和密码。

### 2. 安装 Agent（系统 Nginx）

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01
```

### 3. 安装 Agent（Nginx 位于 `/apps/nginx`）

适用于二进制在 `/apps/nginx/sbin/nginx`、主配置在 `/apps/nginx/conf/nginx.conf` 的环境：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01 \
  --nginx-binary /apps/nginx/sbin/nginx \
  --nginx-root /apps/nginx \
  --nginx-config /apps/nginx/conf/nginx.conf \
  --managed-config-dir /apps/nginx/conf/nginx-manager.d \
  --managed-cert-dir /apps/nginx/certs/nginx-manager \
  --managed-include-file /apps/nginx/conf/conf.d/00-nginx-manager.conf \
  --nginx-service nginx.service
```

安装器会执行 `nginx -t`，并确认主配置已经加载 `conf.d/*.conf`。如果实际 systemd 单元不是 `nginx.service`，请修改 `--nginx-service`。

安装完成后登录 Web，在“节点 Agent”中批准待审批节点。Agent 不监听端口，也不需要注册令牌。

## HTTPS 与 LDAP

默认 HTTP 仅建议用于隔离且可信的管理网。生产环境推荐复用本机 Nginx HTTPS：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --behind-nginx \
  --port 8443
```

代理示例：`deploy/nginx-manager-proxy.conf.example`。如果 HTTPS 反代同时还要保留 HTTP 直连，增加 `--allow-direct-http`。

LDAP/AD 示例：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --behind-nginx \
  --ldap-url ldaps://ad.example.com:636 \
  --ldap-base-dn 'DC=example,DC=com' \
  --ldap-bind-dn 'CN=nginx-manager,OU=Service Accounts,DC=example,DC=com' \
  --ldap-bind-password-file /root/nginx-manager-ldap-password
```

默认 LDAP 组：`nginx-admin`、`nginx-operator`、`nginx-auditor`。本地 `admin` 始终作为应急账号保留。

## 自定义 Nginx 目录说明

Agent 只管理专用目录，不会任意修改其他 Nginx 文件：

```text
/apps/nginx/conf/nginx-manager.d/                 托管配置
/apps/nginx/certs/nginx-manager/                  托管证书
/apps/nginx/conf/conf.d/00-nginx-manager.conf     托管目录 include
```

配置发布采用原子替换、Hash 并发检查、`nginx -t`、reload 和失败恢复。证书任务只允许写入配置好的托管证书目录。

## 备份与升级

```bash
sudo ./deploy/backup-server.sh
```

备份默认保存在 `/var/backups/nginx-manager/`。升级时重新执行相同安装命令即可；Server 保留数据库和账号，Agent 保留机器身份。

## 一键卸载

卸载 Server，保留数据和凭据：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash
```

彻底卸载 Server（删除前自动备份）：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash -s -- --purge
```

卸载 Agent，保留身份和连接配置：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash
```

彻底删除 Agent 身份和配置：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash -s -- --purge
```

Agent 卸载器不会自动删除已发布的 Nginx 配置和证书，避免造成站点中断。

## 常用检查

```bash
# Server
systemctl status nginx-manager
journalctl -u nginx-manager -f
curl -fsS http://127.0.0.1:8443/healthz

# Agent
systemctl status nginx-manager-agent nginx-manager-agent-helper
journalctl -u nginx-manager-agent -f
curl --connect-timeout 5 http://192.0.2.20:8443/healthz

# 自定义 Nginx
/apps/nginx/sbin/nginx -t -c /apps/nginx/conf/nginx.conf
```

HTTP 会明文传输账号、会话、Agent 身份和任务内容；跨不可信网络请使用 HTTPS。

Agent 接入出现 `timed out` 时，先在 Agent 节点执行上面的 `curl`。如果超时，请在 Server
检查 `ss -lntp | grep 8443`；默认 HTTP 应显示监听 `0.0.0.0:8443`，同时确认主机防火墙和网络 ACL 已放行。

# Lightweight Nginx Manager（轻量级 Nginx 管理平台）

这是一个轻量、可以自己部署的多节点 Nginx 管理平台。装好以后，你可以直接在 Web 页面里管理站点、HTTP/Stream 配置和 TLS 证书，也可以导入服务器上已经存在的配置。

每台 Nginx 服务器只需要安装一个 Agent。Agent 会主动连接管理端，不用额外开放管理端口，也不会给平台一个可以随便执行 Shell 的入口。先管几台机器没有问题，后面再慢慢增加节点也可以。

## 界面预览

![站点与配置界面预览](docs/images/console-overview.png)

| 运行监控 | 实时日志 |
| --- | --- |
| ![运行监控界面](docs/images/runtime-monitoring.png) | ![实时日志界面](docs/images/runtime-logs.png) |

## 先把它装起来

### 第一步：安装 Server

下面是最简单的 HTTP 安装方式，适合隔离、可信的内网：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-server.sh | \
sudo bash -s -- \
  --host 192.0.2.20 \
  --port 8443 \
  --open-firewall
```

把示例 IP 换成管理端服务器的真实 IP，然后打开 `http://192.0.2.20:8443`。没有特殊需求时，不用填写 `--public-url`。

登录账号是 `admin`。首次安装会随机生成密码，不使用固定默认密码，可以这样查看：

```bash
sudo cat /root/nginx-manager-credentials.txt
```

### 第二步：安装 Agent

如果 Nginx 是通过系统软件源安装的，通常只需要：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01
```

### 自定义 Nginx 目录

如果 Nginx 装在 `/apps/nginx`，把二进制、主配置、配置目录、证书目录和日志目录都明确写出来。下面这条命令可以直接作为参考：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01 \
  --nginx-binary /apps/nginx/sbin/nginx \
  --nginx-root /apps/nginx \
  --nginx-config /apps/nginx/conf/nginx.conf \
  --managed-config-dir /apps/nginx/conf/conf.d \
  --managed-config-already-included \
  --managed-cert-dir /apps/nginx/cert \
  --nginx-log-dir /apps/nginx/logs \
  --stub-status-url http://127.0.0.1:18080/nginx_status \
  --allow-plaintext-log-stream \
  --nginx-service nginx.service
```

一台 Nginx 可以有多个 HTTP 配置目录。多写几次 `--managed-config-dir` 就可以，第一个目录会作为默认写入位置：

```bash
  --managed-config-dir /apps/nginx/conf/conf.d \
  --managed-config-dir /apps/nginx/conf/sites.d \
  --managed-config-already-included
```

如果你还使用了 Stream 配置，例如主配置里有：

```nginx
http {
    include /apps/nginx/conf/conf.d/*.conf;
}

stream {
    include /apps/nginx/conf/conf.d/*.stream;
}
```

安装 Agent 时再加上对应的 Stream 目录：

```bash
  --managed-config-dir /apps/nginx/conf/conf.d \
  --managed-stream-dir /apps/nginx/conf/conf.d \
  --managed-config-already-included
```

`--managed-config-dir` 和 `--managed-stream-dir` 都可以重复。创建、复制或迁移配置时，也可以单独选择每个节点要写入哪个目录。

平台只管理这些目录最外层的文件，不会递归扫描子目录。HTTP 配置使用 `*.conf`，Stream 配置使用 `*.stream`。Agent 还会通过真实的 `nginx -T` 确认目录确实已经被 Nginx 加载，避免把文件写到一个根本不会生效的位置。

为了避免从 Web 页面临时扩大写入范围，配置入口只能由 root 重新执行安装命令来调整。符号链接目录不会被接管；符号链接文件可以看到，但不能修改。

Agent 安装完成后，登录 Web 页面，在“节点 Agent”里批准接入申请。这里不需要注册令牌。接着：

1. 在“站点与配置”中点击“导入节点现有配置”。
2. 在“证书”页面点击“扫描节点证书”。

扫描过程是只读的，私钥内容不会离开节点。站点列表还可以按 Agent 筛选，右侧会显示真实文件路径、Hash 和配置预览。

`--nginx-log-dir` 也可以重复指定。平台只会按需实时读取这些目录里的普通 `*.log` 文件，不会把日志正文长期保存在管理端。

如果 Agent 通过 HTTP 连接管理端，需要显式添加 `--allow-plaintext-log-stream` 才能查看实时日志。以后改成 HTTPS，这个参数留着也不会关闭 TLS。

`--stub-status-url` 只接受本机回环地址。安装时暂时访问不到不会失败，Agent 会在后台继续重试。可以在 Nginx 中加入：

```nginx
server {
    listen 127.0.0.1:18080;
    server_name localhost;
    access_log off;
    location = /nginx_status {
        stub_status;
        allow 127.0.0.1;
        deny all;
    }
}
```

“运行监控”每 15 秒采集一次宿主机、Nginx 进程和 Stub Status。原始数据保留 2 小时，分钟级历史保留 24 小时。“实时日志”一次查看一个节点上的一个文件，浏览器最多显示 5,000 行。

## 怎么管理配置

点击“新增配置”后，可以选择四种方式：

- **向导站点**：填写域名和上游，由平台生成基础配置。
- **站点 Conf**：直接编写包含业务 `server_name` 的站点配置，可绑定证书。
- **通用 Conf**：托管 `upstream`、`map`、`geo`、限流区和本机 Stub Status 等 HTTP 片段，不要求域名或证书。
- **Stream Conf**：托管 TCP/UDP `server`、`upstream` 等 Stream 片段，使用 `.stream` 文件；证书路径直接写在正文中。

如果你已经有一份完整配置，直接使用 Conf 模式会更省事。平台不会用自己的规则限制 Nginx 指令，而是把配置交给目标节点的真实 `nginx -t` 检查。因此 `auth_basic`、第三方模块和其他合法指令都可以使用。

Agent 只会写入安装时登记过的目录。写入时会检查 SHA-256，使用原子替换；如果校验或 reload 失败，会恢复原文件。

通用 Conf 只需要填写文件名，最终目录由每个节点选择的配置入口决定。目标节点上如果已经有同名文件，请先导入现有配置，平台不会直接覆盖一个自己不了解的文件。

HTTP 配置只能复制或迁移到 HTTP 入口，Stream 配置也只能进入 Stream 入口。同一个节点切换目录时，平台会把“写入新位置、删除旧文件、校验和 reload”放在同一次操作里；中间任何一步失败，两个位置都会恢复。

`--nginx-config` 指定的主配置会单独显示。默认只能查看，不能删除、移动、重命名或复制。如果确实要在平台里编辑主配置，重新执行 Agent 安装命令并加上：

```bash
  --allow-main-config-edit
```

即使打开了编辑权限，主配置仍然不能删除。每次发布都会先执行真实的 `nginx -t`；如果失败，Agent 会恢复原文件。

## HTTPS 和 LDAP

如果管理网是隔离且可信的，直接使用 HTTP 最省事。只要会经过不可信网络，就应该在 Server 前面放一个本机 Nginx，由它处理 HTTPS：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --behind-nginx \
  --port 8443
```

可以直接参考 `deploy/nginx-manager-proxy.conf.example`。如果还想保留 `http://服务器IP:8443` 这个入口，再加上 `--allow-direct-http`。

需要接 LDAP 或 AD 时，先准备下面四项：

```text
--ldap-url
--ldap-base-dn
--ldap-bind-dn
--ldap-bind-password-file
```

完整参数可以运行 `sudo ./deploy/install-server.sh --help` 查看。默认会识别 `nginx-admin`、`nginx-operator` 和 `nginx-auditor` 三个角色组。本地 `admin` 不会被关闭，可以在 LDAP 出现问题时应急登录。

## 升级和日常检查

升级很简单：重新执行原来的安装命令即可。Server 会保留数据库和账号，Agent 也会保留已经批准的机器身份。

建议按这个顺序升级：

1. 先升级 Server。
2. 再升级所有 Agent。
3. 最后在浏览器按 `Ctrl+F5` 刷新页面。

Agent 遇到网络中断时，不会直接丢掉执行结果，而是先保存在 `/var/lib/nginx-manager-agent/result-outbox.json`，等 Server 确认收到后再删除。多节点发布如果只有一部分成功，页面会把每个节点的真实结果分别显示出来。

只有已经成功发布并生成快照的版本才能自动回滚。很早以前只留下页面历史、没有配置快照的记录，不能直接恢复。

升级前建议先备份 Server：

```bash
sudo ./deploy/backup-server.sh
```

平时检查服务可以使用：

```bash
# Server
systemctl status nginx-manager
journalctl -u nginx-manager -f
curl -fsS http://127.0.0.1:8443/healthz

# Agent
systemctl status nginx-manager-agent nginx-manager-agent-helper
journalctl -u nginx-manager-agent -f

# 自定义 Nginx
/apps/nginx/sbin/nginx -t -c /apps/nginx/conf/nginx.conf
```

正式环境不要一直从会变化的 `main` 直接安装。确认一个测试过的提交后，把 40 位 commit 固定下来：

```bash
export NGINX_MANAGER_REF='<40位Git提交>'
export NGINX_MANAGER_REQUIRE_PINNED_REF=1
curl -fsSL "https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/${NGINX_MANAGER_REF}/install-server.sh" | \
sudo -E bash -s -- --host 192.0.2.20 --port 8443
```

安装 Agent 时也可以使用这两个环境变量。如果还想校验 GitHub 源码归档，可以再设置 `NGINX_MANAGER_ARCHIVE_SHA256`。

Server 默认保留最近 3 个程序版本，数据库和业务数据不在这些 release 目录里。需要调整数量时，可以设置 `NGINX_MANAGER_RELEASE_RETENTION`。

任务和操作记录默认保留 30 天，审计记录保留 180 天。LDAP 登录会每 5 分钟重新确认一次角色。相关环境变量都在 `server/env.example`。

替换证书时，私钥会在任务排队和重试期间临时保存在权限为 `0600` 的 Server SQLite 中，任务结束后立即删除。因此 Server 磁盘、备份和管理网络仍然需要认真保护。

## 不用了怎么卸载

普通卸载会保留 Server 数据或 Agent 身份，方便以后重新安装。Agent 卸载器也不会删除已经发布到 Nginx 的配置和证书：

```bash
# Server
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash

# Agent
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash
```

如果确认所有保留数据都不再需要，再使用 `--purge`：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash -s -- --purge
```

> HTTP 会明文传输登录会话、Agent 身份和任务内容。只要流量会经过不可信网络，就必须使用 HTTPS。

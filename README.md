# 轻量 Nginx 托管台

这是一个面向少量到中等规模 Linux Nginx 节点的轻量自托管版本。控制端使用 FastAPI、SQLite 和单 HTML 页面；每个节点运行一个只主动出站的 Agent。

首版闭环：

- Agent 接入、心跳、版本和配置 Hash 上报
- `nginx -t`、reload、配置读取/Hash
- 配置备注、变更说明与控制端持久化
- 多节点配置校验和发布
- 已发布配置复制到其他节点，可选择安全新建或原位替换同名配置
- 按节点安全移除托管配置：精确 Hash 防误删、`nginx -t`、reload 和失败/掉电恢复
- 手动 PEM 证书与私钥多节点部署
- 原子替换、并发 Hash 检查、备份和失败自动回滚
- LDAP / Active Directory 登录、本地应急管理员与三档角色权限
- 控制端默认提供内网 HTTP，也可切换为本机 Nginx HTTPS 反代或直连 TLS
- 执行记录及敏感信息脱敏

不提供任意 Shell。前端只能下发八个固定动作：`inspect`、`nginx_test`、`nginx_reload`、`config_read`、`config_hash`、`config_apply`、`config_delete`、`certificate_apply`。

直接双击 `nginx-cluster-console.html` 时是本地演示模式；由 Linux 控制端通过 HTTP(S) 打开时会自动进入真实 API 模式。

## 架构

```text
浏览器 / Agent ──HTTP──> 控制端 0.0.0.0:8443 ──> SQLite
       或
浏览器 / Agent ──HTTPS──> 本机 Nginx :443 ──HTTP──> 控制端 127.0.0.1:8443

LDAP / AD <──LDAPS 或 StartTLS── 控制端
各节点普通用户 Agent ──HTTP(S) 主动轮询──┘
        │
        └─Unix Socket─> root helper ─> Nginx 配置 / nginx -t / reload
```

- 节点不监听端口，也不需要修改节点防火墙。
- 网络 Agent 使用普通系统用户运行。
- root helper 不联网接收控制请求，只接受本机 Unix Socket 固定动作，并校验调用者 UID。
- Web 支持 LDAP / AD 登录，并保留一个本地 `admin` 应急账号；HTTPS 使用 Secure、HttpOnly、SameSite Cookie，HTTP 使用独立的 HttpOnly、SameSite Cookie。
- `admin`、`operator`、`auditor` 三档权限由服务端强制校验，前端隐藏按钮只是交互提示。
- Agent 安装不需要人工令牌；首次连接进入待审批列表，批准后自动建立每机独立机器身份，控制端只保存不可逆摘要。

## 支持环境

- Ubuntu 22.04/24.04、Debian 11/12
- Rocky Linux / AlmaLinux 8/9
- systemd
- 控制端 Python 3.9+
- Agent Python 3.6+（兼容 CentOS 7 自带 Python；仍建议在受支持的系统上使用 3.8+）
- 已安装 Nginx；也可以显式使用 `--install-nginx`

建议先在测试节点验证，不要让控制端管理承载控制台自身的 Nginx。

## 从 GitHub 一键安装

默认 HTTP 安装 Server：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-server.sh | \
  sudo bash -s -- \
    --host 192.0.2.20 \
    --port 8443 \
    --open-firewall
```

在每台 Nginx 节点安装 Agent：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
  sudo bash -s -- \
    --server http://192.0.2.20:8443 \
    --node-name edge-a-01
```

两个入口脚本只负责通过 HTTPS 下载本仓库快照，然后调用 `deploy/` 下的正式安装器。可通过环境变量 `NGINX_MANAGER_REF` 固定到分支、标签或提交；生产环境建议固定版本并在执行前审核脚本。

### 默认登录账号和密码

- 默认账号：`admin`
- 默认密码：**没有固定值**。首次安装会生成 48 位十六进制随机密码，避免公开仓库里的通用弱密码被直接利用。
- 凭据文件：`/root/nginx-manager-credentials.txt`，权限为 `0600`。

查看首次登录密码：

```bash
sudo cat /root/nginx-manager-credentials.txt
```

升级不会重置已经存在的管理员账号和密码。如果凭据文件遗失，应使用既有密码或通过受控的数据库恢复流程处理，不要反复安装期待密码重置。

## 仓库结构

```text
├── nginx-cluster-console.html
├── install-server.sh
├── install-agent.sh
├── uninstall-server.sh
├── uninstall-agent.sh
├── agent/
├── server/
├── tests/
└── deploy/
    ├── install-server.sh
    ├── install-agent.sh
    ├── uninstall-server.sh
    ├── uninstall-agent.sh
    ├── backup-server.sh
    └── nginx-manager-proxy.conf.example
```

`deploy/` 下的正式安装脚本依赖这个相对目录结构；直接使用仓库根目录的一键入口即可自动下载完整快照。

## 一、部署 Linux 控制端

如需先审核再安装，可克隆仓库并进入项目目录：

```bash
git clone https://github.com/zhangldaniel/lightweight-nginx-manager.git
cd lightweight-nginx-manager
```

### 默认：内网 HTTP 直连

不指定 `--behind-nginx`、证书或 `--self-signed` 时，控制端默认监听
`0.0.0.0:8443`，可直接打开 `http://服务器IP:8443`：

```bash
sudo ./deploy/install-server.sh \
  --host 192.0.2.20 \
  --port 8443 \
  --open-firewall
```

HTTP 会明文传输登录密码、会话、配置和证书任务，只建议在隔离且可信的管理网使用。
跨网段时建议增加 `--allow-cidr` 限制防火墙来源。

### 推荐：复用本机 Nginx，不让控制端管理 CA

控制端只监听 `127.0.0.1:8443`，不生成、不保存控制端 CA 或服务端证书。外部浏览器和 Agent 统一访问现有 Nginx 的 HTTPS 地址：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --behind-nginx \
  --port 8443
```

复制并修改发布包中的代理示例：

```bash
sudo cp ./deploy/nginx-manager-proxy.conf.example /etc/nginx/conf.d/nginx-manager.conf
sudo nginx -t
sudo nginx -s reload
```

如果本机 Nginx 安装在 `/opt/custom-nginx`，把配置放进主配置已经加载的目录，并按实际路径校验：

```bash
sudo /opt/custom-nginx/sbin/nginx -t -c /opt/custom-nginx/conf/nginx.conf
sudo /opt/custom-nginx/sbin/nginx -s reload
```

代理配置必须保留 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto https`。推荐的外部入口使用 HTTPS，登录 Cookie 使用 `Secure` 与 `__Host-` 约束。默认情况下 `8443` 不需要放行防火墙，服务也不会监听所有网卡。

如需在本机 Nginx HTTPS 反代之外，同时保留 `http://服务器IP:8443`，显式增加
`--allow-direct-http`。它会让反代后端同时监听所有网卡：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-server.sh | \
  sudo bash -s -- \
    --host 192.0.2.20 \
    --behind-nginx \
    --allow-direct-http \
    --port 8443 \
    --open-firewall
```

只需要 HTTP 时不要传 `--behind-nginx` 和 `--allow-direct-http`，直接使用上面的默认安装命令。

部署完成后检查：

```bash
sudo systemctl status nginx-manager
curl -fsS http://127.0.0.1:8443/healthz
curl -fsS https://nginx-manager.example.com/healthz
sudo cat /root/nginx-manager-credentials.txt
```

### 接入 LDAP / Active Directory

先创建只包含一行查询账号密码的 root-only 文件，不要把密码直接写进安装命令或 shell 历史：

```bash
sudo install -m 0600 /dev/null /root/nginx-manager-ldap-password
sudoedit /root/nginx-manager-ldap-password
```

Active Directory 示例：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --behind-nginx \
  --ldap-url ldaps://ad.example.com:636 \
  --ldap-base-dn 'DC=example,DC=com' \
  --ldap-bind-dn 'CN=nginx-manager,OU=Service Accounts,DC=example,DC=com' \
  --ldap-bind-password-file /root/nginx-manager-ldap-password \
  --ldap-admin-group 'nginx-admin' \
  --ldap-operator-group 'nginx-operator' \
  --ldap-auditor-group 'nginx-auditor'
```

目录证书由系统信任时不需要 `--ldap-ca-file`；私有目录 CA 可追加 `--ldap-ca-file /root/ldap-ca.crt`。使用 `ldap://` 配合 StartTLS 时追加 `--ldap-start-tls`。完全不使用 LDAP TLS 虽然不需要 CA，但用户密码会经过未加密的 LDAP 连接，不建议这样配置。

默认用户过滤器同时接受 AD 短用户名、UPN（`user@example.com`）和 OpenLDAP `uid`。角色按直属组名或完整组 DN 匹配，不展开嵌套组：

| LDAP 组 | 平台角色 | 权限 |
|---|---|---|
| `nginx-admin` | 管理员 | 全部操作，包括批准或拒绝 Agent 接入 |
| `nginx-operator` | 运维操作员 | 查看并执行配置、证书、校验、reload 等操作；不能审批 Agent |
| `nginx-auditor` | 只读审计员 | 查看节点、配置状态和执行记录 |

OpenLDAP 没有 `memberOf` 时，可增加：

```bash
--ldap-group-search-base 'OU=Groups,DC=example,DC=com' \
--ldap-group-filter '(member={user_dn})'
```

安装器始终保留本地 `admin` 账号作为 LDAP 故障时的应急入口，凭据在 `/root/nginx-manager-credentials.txt`。本地账号优先于同名 LDAP 用户，LDAP 密码不会写入 SQLite。升级时不重复传 LDAP 参数会保留现有 LDAP 配置；需要关闭时显式使用 `--disable-ldap`。

### 内网 IP / 本地 CA

```bash
sudo ./deploy/install-server.sh \
  --host 192.0.2.20 \
  --self-signed
```

上面的 `--self-signed` 会启用直连 TLS；不传证书或 `--self-signed` 时才是默认 HTTP。
脚本只有收到 `--open-firewall` 才会修改已启用的 ufw/firewalld。如需为默认 HTTP
只放行指定网段：

```bash
sudo ./deploy/install-server.sh \
  --host 192.0.2.20 \
  --open-firewall \
  --allow-cidr 192.0.2.0/24
```

### 使用已有域名证书

证书文件应包含完整链：

```bash
sudo ./deploy/install-server.sh \
  --host nginx-manager.example.com \
  --cert /root/fullchain.pem \
  --key /root/privkey.pem
```

部署完成后：

```bash
sudo systemctl status nginx-manager
sudo journalctl -u nginx-manager -f
sudo cat /root/nginx-manager-credentials.txt
```

控制端程序按版本安装在 `/opt/nginx-manager/releases/<版本>/`，服务始终通过
`/opt/nginx-manager/current` 指向当前版本。SQLite、TLS 和服务端环境文件不放在
release 目录内，分别保存在 `/var/lib/nginx-manager/` 与 `/etc/nginx-manager/`。

默认模式打开 `http://控制端地址:8443`；反代模式默认打开 `https://--host`，地址不同时才需要指定 `--public-url`；直连 TLS 模式打开 `https://控制端地址:8443`。使用 LDAP / AD 或凭据文件中的本地管理员账号登录。浏览器脚本不能读取会话 Cookie，退出登录或会话到期后需要重新登录。

自签模式需要通过可信的运维通道复制 CA，并核对安装脚本输出的 SHA-256 指纹：

```bash
scp root@192.0.2.20:/etc/nginx-manager/tls/ca.crt ./ca.crt
openssl x509 -in ./ca.crt -noout -fingerprint -sha256
```

不要从未经验证的 HTTP 地址下载并自动信任 CA。

## 二、准备节点接入

在页面进入“节点 Agent”，点击“接入 Agent 节点”，填写节点名后复制安装命令。安装完成后，该节点会出现在“待审批接入”区域；核对节点名和主机名后点击“批准接入”。批准前 Agent 没有领取或执行任务的权限。

## 三、在 Nginx 节点安装 Agent

也可以在节点克隆同一仓库，并与自签 CA（如有）一起准备后运行：

```bash
git clone https://github.com/zhangldaniel/lightweight-nginx-manager.git
cd lightweight-nginx-manager

sudo ./deploy/install-agent.sh \
  --server https://nginx-manager.example.com \
  --node-name edge-a-01 \
  --labels env=prod,region=region-a \
  --health-url http://127.0.0.1/healthz
```

脚本不会询问或接收注册令牌。Agent 会自行生成 root-only 的接入秘密并持续等待 Web 审批。

外层 Nginx 使用系统可信证书时省略 `--ca-file`；使用私有 CA 时把外层 Nginx 的 CA 传给 Agent。这里不再涉及控制端自建 CA。如果业务没有可靠的健康检查地址，也可先省略 `--health-url`；此时仍会执行 `nginx -t`、原子切换和 reload，但不会做 HTTP 健康检查。

可信内网中若不便分发自签 CA，可显式使用 `--insecure-skip-tls-verify`。连接仍是 HTTPS，但 Agent 不再验证控制端身份；该参数不能与 `--ca-file` 同时使用。

自定义 `/opt/custom-nginx` 布局示例：

```bash
sudo ./deploy/install-agent.sh \
  --server https://nginx-manager.example.com \
  --node-name edge-custom-01 \
  --nginx-binary /opt/custom-nginx/sbin/nginx \
  --nginx-root /opt/custom-nginx \
  --nginx-config /opt/custom-nginx/conf/nginx.conf \
  --managed-config-dir /opt/custom-nginx/conf/nginx-manager.d \
  --managed-cert-dir /opt/custom-nginx/certs/nginx-manager \
  --managed-include-file /opt/custom-nginx/conf/conf.d/00-nginx-manager.conf
```

托管配置目录和托管证书目录必须是 `--nginx-root` 的严格子目录，二者不能重叠；`--managed-include-file` 的父目录必须已经被主配置加载。

安装后检查：

```bash
sudo systemctl status nginx-manager-agent nginx-manager-agent-helper
sudo journalctl -u nginx-manager-agent -f
sudo /usr/sbin/nginx -t
```

大约数秒后，节点申请会出现在页面中；批准后节点自动上线，无需回到终端继续输入。

## 四、使用托管台

### 新增和发布站点

1. 新增站点并选择真实在线节点。
2. 如需 HTTPS，只能绑定已经成功部署在全部所选节点上的证书。证书下拉框按所选节点实时取交集；缺少证书或证书仍在部署中的选项不可选择。
3. 编辑受控 Nginx 配置，填写长期“配置备注”和必填“本次变更说明”。首版允许常用官方 HTTP 指令；未知第三方指令、`include` 和动态脚本类指令会被 Agent 拒绝。
4. 先点“校验”。控制端会把候选内容送到每个节点，Agent 临时替换后执行节点本机 `nginx -t`，随后必定恢复原文件。
5. 发布时每个节点独立执行：

   ```text
   expected SHA-256 检查
   → 同目录临时文件 + fsync
   → 原子替换
   → nginx -t
   → reload
   → 可选 health_url
   → 失败自动回滚并再次 reload
   ```

首版会同时向所选节点下发任务；节点之间没有“全局事务”。一个节点失败不会伪装成全局成功，执行记录会分别显示成功、失败或过期。

### 复制配置到其他节点

在站点右侧详情点击“复制到其他节点”，平台会根据每台目标 Agent 上报的 `managed_config_root`，把配置写入该节点自己的托管配置目录。例如同一站点可以分别落到：

```text
/etc/nginx/nginx-manager.d/api.example.com.conf
/opt/custom-nginx/conf/nginx-manager.d/api.example.com.conf
/usr/local/nginx/conf/nginx-manager.d/api.example.com.conf
```

可选择两种互斥策略：

- “仅新建”使用 `expected_sha256=missing`，目标存在同名文件时拒绝覆盖。
- “仅替换”使用 `expected_sha256=present`，目标文件不存在时拒绝创建；Agent 会记录替换前 Hash。该模式要求目标 Agent 0.3.0+。

两种策略都会先暂存候选内容、执行目标节点本机 `nginx -t`，通过后才原子写入并 reload；失败自动恢复。成功节点会加入站点托管范围，原节点配置不会自动删除。配置中的平台证书目录会按目标节点自动改写，但证书和私钥不会随配置复制；若站点已经绑定证书，缺少该证书的目标节点会直接变为不可选，必须先到“证书”页面完成部署。

节点内发布有持久化事务 manifest：候选文件、原文件备份和阶段均先 fsync，再替换。即使在证书与私钥两次替换之间断电，helper 下次启动也会在接收任务前恢复整组旧文件，重新执行 `nginx -t`，必要时 reload；恢复校验失败则拒绝启动并保留现场，不会继续发布。

平台首次创建的站点默认写入：

```text
/etc/nginx/nginx-manager.d/<域名>.conf
```

如果目标文件已经存在，`expected_sha256=missing` 会阻止覆盖。现有配置应先读取 Hash/导入后再托管。

### 手动证书

在“证书”中选择“手动上传 PEM”，上传 fullchain 与私钥并选择节点。Agent 会：

- 使用 OpenSSL 验证证书有效期以及证书/私钥是否匹配
- 以 `0644/0600` 写入 `/etc/nginx/ssl/nginx-manager/`
- 执行 `nginx -t`
- 可选 reload 和健康检查
- 任一步失败则恢复旧文件

私钥不会写入 UI 状态、操作日志或任务列表。敏感 payload 在 SQLite 中只保留到 Agent 领取任务为止，领取时立即脱敏覆盖。

更新已有手动证书时，点击“部署”并重新选择 PEM/私钥。页面会把上次成功结果中的证书 Hash 与私钥材料 Hash 作为并发前置条件；若节点文件被平台外修改，本次更新会停止而不是静默覆盖。这里只保存不可逆 Hash，不保存私钥内容。

ACME 自动申请和续期目前只保留了页面入口，尚未接入真实签发流程。

## 常用目录

### 控制端

```text
/opt/nginx-manager/releases/        各版本程序与独立 Python venv
/opt/nginx-manager/current          当前版本原子链接
/etc/nginx-manager/server.env       非敏感运行配置，0640
/etc/nginx-manager/ldap-bind-password  LDAP 查询密码，0640
/etc/nginx-manager/ldap-ca.crt       可选的 LDAP 私有 CA
/etc/nginx-manager/tls              仅直连模式使用；反代模式不会保留
/var/lib/nginx-manager/manager.db   SQLite，0600
```

### Agent 节点

```text
/opt/nginx-manager-agent/nginx_agent.py
/etc/nginx-manager-agent/config.json
/var/lib/nginx-manager-agent/identity.json
/var/lib/nginx-manager-agent-helper/    root helper 幂等与锁状态
/run/nginx-manager-agent/helper.sock
/etc/systemd/system/nginx-manager-agent-recover.service
/etc/systemd/system/nginx.service.d/nginx-manager-agent-recovery.conf
/etc/nginx/nginx-manager.d/              平台专用站点配置
/etc/nginx/ssl/nginx-manager/             平台专用证书与私钥
/etc/nginx/conf.d/00-nginx-manager.conf   只包含上述专用配置目录
```

## 备份

使用 SQLite Backup API 创建一致性备份：

```bash
sudo ./deploy/backup-server.sh
```

默认生成到 `/var/backups/nginx-manager/`。归档包含密码摘要、Agent 身份摘要、LDAP 查询密码及可能存在的 TLS 私钥，权限为 `0600`；复制到远端前还应再次加密。

## 一键卸载

卸载 Server 程序和服务，保留数据库、配置及管理员凭据：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash
```

彻底卸载 Server 时会先把数据备份到 `/var/backups/nginx-manager/`，再删除数据库、配置、凭据和系统账号：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash -s -- --purge
```

卸载 Agent，默认保留连接配置和机器身份，方便重新安装：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash
```

删除 Agent 配置及机器身份：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash -s -- --purge
```

Agent 卸载器不会自动删除已经发布的 Nginx 配置、证书或 include 文件，避免导致现有站点中断；确认不再使用后应结合实际 Nginx 路径人工清理并执行 `nginx -t`。

## 升级

先备份，然后用新发布包和首次安装相同的入口模式重新运行安装脚本。默认 HTTP 模式只需继续传 `--host` 和端口；反代模式继续传 `--behind-nginx`，外部地址与 `--host` 不同时再传 `--public-url`；需要同时保留直连 HTTP 时继续传 `--allow-direct-http`。直连 TLS 模式继续传证书参数。LDAP 已启用时可以不重复传 LDAP 参数，安装器会保留已有环境、查询密码和 LDAP CA；需要修改 LDAP 时传入完整的一组 LDAP 参数。控制端会先完成发布包预检，在独立 release 目录创建虚拟环境、安装依赖并执行 Python 导入检查；这些步骤不会改动正在运行的 `current`。

预装成功后，脚本会短暂停止旧服务并用 SQLite Backup API 创建一致性快照，再原子切换 `current`、显式 `systemctl restart nginx-manager`，并检查服务状态和 `/healthz`；直连模式还会校验 TLS 证书钉扎。任一步失败时，脚本会自动切回升级前的 release，校验恢复原服务文件、环境文件、TLS、LDAP 密钥材料与 SQLite，并重新启动旧服务。首次安装失败时同样会撤销 `current` 和 systemd 单元，不会留下一个伪成功服务。

从旧版升级时，脚本会在现有 SQLite 中创建首个 `admin` Web 账号，并把一次性初始密码写入 root-only 凭据文件；旧管理员令牌不会进入新服务环境。首次切换到角色会话模型后需要重新登录，后续升级会保留账号、密码摘要、新会话和节点身份，不会重置密码。Agent 安装会保留节点 identity，除非显式指定 `--force-enroll`。

Agent 安装器会先备份二进制、配置、CA、systemd 单元和 identity，再停止旧服务；新 helper/Agent 任一步启动失败都会恢复文件及原来的 enabled/active 状态。待审批申请在首次请求前已原子落盘，掉电或断网后会继续使用同一个申请。重新接入被拒绝时会恢复仍有效的旧身份。自动回滚本身校验失败时，`/var/tmp/nginx-manager-agent-install.*` 的 `0700` 恢复副本不会被删除。

使用“仅替换”复制配置前，应在目标节点用新版发布包重新运行 `install-agent.sh`，将 Agent 升级到 0.3.0 或更高版本；安装器会保留原机器身份，不需要重新审批。

如果系统文件损坏导致自动回滚本身无法完整完成，脚本不会删除唯一的安装前备份，而会打印一个权限为 `0700` 的 `/var/tmp/nginx-manager-install.*/rollback` 路径。该位置可跨正常重启保留；此时不要再次运行安装脚本，应先按错误输出人工恢复并检查旧服务。安装脚本检测到这类未确认事务时也会拒绝开始下一次升级，避免覆盖恢复现场。

查看当前版本与保留的 release：

```bash
readlink -f /opt/nginx-manager/current
sudo ls -1 /opt/nginx-manager/releases
```

入口模式、域名、端口、证书、labels、健康检查等参数在升级时建议保持与首次安装一致。

## 故障排查

### 控制端打不开

```bash
sudo systemctl status nginx-manager
sudo journalctl -u nginx-manager --since -1h
sudo ss -lntp | grep 8443
```

### Agent 不上线

```bash
sudo systemctl status nginx-manager-agent nginx-manager-agent-helper
sudo journalctl -u nginx-manager-agent --since -1h
sudo namei -l /etc/nginx-manager-agent/config.json
timedatectl status
```

常见原因：接入申请尚未在 Web 批准、申请被拒绝或过期、自签 CA 未传入、控制端防火墙未允许节点网段。可用 `--force-enroll` 重新发起申请。

### 配置发布失败

```bash
sudo /usr/sbin/nginx -t
sudo journalctl -u nginx-manager-agent-helper --since -1h
sudo ls -la /etc/nginx/nginx-manager.d /etc/nginx/ssl/nginx-manager
```

`current SHA-256 does not match expected_sha256` 表示节点配置已被平台外修改，应先确认漂移，不要直接覆盖。

## 上线边界

这个版本适合放在内网、VPN 或零信任访问层后面。已提供 LDAP / AD 与基础 RBAC；正式公网暴露前仍建议在外层入口增加访问控制、集中密钥管理和审计归档。

需要明确的边界：

- 配置编辑仅开放首版审核过的常用 Nginx 官方 HTTP 指令；不要为了兼容第三方模块而关闭 Agent 指令白名单。
- LDAP 角色只读取登录时的直属组成员关系；目录组变化在用户下次登录后生效，嵌套组不会递归展开。
- 本地 `admin` 是故障应急账号，应把 `/root/nginx-manager-credentials.txt` 纳入密码库并严格限制 root 权限。
- 证书敏感任务领取前会短暂存在于 SQLite；应启用磁盘加密并保护备份。
- root helper 的健康检查主机默认只允许 `127.0.0.1`、`::1` 和 `localhost`；安装时配置的健康主机会加入白名单。
- 不支持任意 Shell、SSH 跳板、Docker Socket 或“执行任意命令”。
- 不建议让该平台管理控制端自身入口，避免误配置造成自锁。

## 验证

项目包含控制端和 Agent 的 unittest。当前已覆盖：本地与 LDAP 会话、角色权限、LDAP 故障应急登录、CSRF、Agent 审批接入、旧身份回退、任务事务领取、结果幂等、UI revision 冲突、配置/证书原子更新、发布中断恢复、Hash 防覆盖、路径越界、私钥脱敏和健康检查主机限制。

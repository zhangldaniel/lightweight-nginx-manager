# Nginx Steward

一个自己部署的多节点 Nginx 管理台。你可以在 Web 页面管理 HTTP/Stream 配置、TLS 证书、实时日志和运行状态。

每台 Nginx 主机安装一个主动连接 Server 的 Agent。Agent 只执行内置运维动作，不提供任意 Shell 入口。

界面正文使用[更纱黑体 UI SC](frontend/public/ui-assets/SarasaGothic-LICENSE.txt)，标题和关键数字使用[得意黑](frontend/public/ui-assets/SmileySans-LICENSE.txt)。字体随 Server 同源提供，内网环境也能正常显示。

## 界面

![站点与配置](docs/images/console-overview.png?v=20260803-fonts)

| 运行监控 | 实时日志 |
| --- | --- |
| ![运行监控](docs/images/runtime-monitoring.png?v=20260803-fonts) | ![实时日志](docs/images/runtime-logs.png?v=20260803-fonts) |

## 功能

- 管理多节点 HTTP、Stream 配置和多个配置目录
- 导入现有配置，替换节点原路径中的证书
- 查看实时日志、宿主机状态和 Nginx Stub Status
- 查看 Keepalived 主备角色、VIP 归属和双机架构
- 只读查看 Linux LVS/IPVS 的虚拟服务、后端成员和连接统计
- 发布前执行真实 `nginx -t`，失败时恢复原文件
- 支持本地账号、LDAP/AD、操作记录和版本回滚

## 安装 Server

下面的 HTTP 方式只适合隔离的可信内网：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-server.sh | \
sudo bash -s -- \
  --host 192.0.2.20 \
  --port 8443 \
  --open-firewall
```

打开 `http://192.0.2.20:8443`，使用 `admin` 登录。安装脚本会随机生成密码：

```bash
sudo cat /root/nginx-manager-credentials.txt
```

公网或跨不可信网络使用时，请在 Server 前配置 HTTPS 反向代理。LDAP 参数和其他选项可运行：

```bash
sudo ./deploy/install-server.sh --help
```

## 安装 Agent

系统路径中的 Nginx：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01
```

安装后去 Web 的“节点 Agent”批准接入，再导入配置和扫描证书。

### `/apps/nginx`：常用短命令

`--nginx-prefix /apps/nginx` 会自动带上常见的二进制、主配置、证书、日志和 `conf.d` 路径。证书目录优先使用已有的 `cert`，其次是 `certs`，都没有时创建 `cert`。`--manage-stream` 表示同一个 `conf.d` 目录内的 `*.stream` 也由平台管理。

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01 \
  --nginx-prefix /apps/nginx \
  --manage-stream \
  --allow-plaintext-log-stream \
  --allow-main-config-edit
```

`--allow-main-config-edit` 允许平台编辑 `nginx.conf`；不需要时删掉即可。HTTP 控制端的实时日志需要显式保留 `--allow-plaintext-log-stream`，HTTPS 控制端不受它影响。

短命令适用于现有 `nginx.conf` 已加载 `conf/conf.d/*.conf` 的机器；安装器会用 `nginx -T` 实测，不满足时会停止且不接管目录。

### Stub Status（可选）

先在节点上确认 URL 返回 `Active connections`，再把同一个 URL 加到安装命令中。URL 必须和 Nginx 的真实 `location` 完全一致；例如 Nginx-UI 常见配置是 `51820 + /stub_status`：

```bash
curl -fsS http://localhost:51820/stub_status
# 输出中应包含：Active connections
```

```bash
--stub-status-url http://localhost:51820/stub_status
```

### 两台 Keepalived 高可用节点

只有高可用对中的两台机器才加这一段；普通独立节点不要加。下面用脱敏地址示例：`.108` 和 `.111` 是两台 Nginx，VIP 是 `.110`；`.198` 是独立节点，不带 Keepalived 参数。`--node-ip` 是 `--labels ha_ip=...` 的短写法。

```bash
  --node-ip 192.0.2.108 \
  --keepalived-vip 192.0.2.110
```

另一台只需换成本机 IP：

```bash
  --node-ip 192.0.2.111 \
  --keepalived-vip 192.0.2.110
```

只给 `--keepalived-vip` 时，安装器默认读取 `/etc/keepalived/keepalived.conf`，默认服务为 `keepalived.service`。如果 Keepalived 放在自定义位置，再追加：

```bash
--keepalived-binary /apps/keepalived/sbin/keepalived
```

高可用页面只读取真实 VIP 归属、Keepalived 状态和脱敏的 VRRP 摘要，不会修改配置、启停 Keepalived 或主动漂移 VIP。

配置里用了 `vrrp_script` 时，先确认检查脚本及其父目录不能被非 root 用户写入，再在 `global_defs` 中显式加入 `script_user root` 和 `enable_script_security`。否则 Keepalived 服务可能仍在运行，但“校验配置”会按安全问题报错；平台不会替你忽略或自动改写这项策略。

### LVS / IPVS 只读观测（可选）

只有运行 Linux IPVS 的调度节点才需要加：

```bash
--enable-lvs-observer
```

Agent 只读取 `/proc/net/ip_vs` 和 `/proc/net/ip_vs_stats`，用于展示 Virtual Service、后端成员、权重、转发方式和连接统计；不会执行 `ipvsadm`，也不会新增、删除或修改转发规则。

这个开关与 Keepalived 相互独立。仅用 Keepalived 给 Nginx 做 VIP 主备，不代表节点已经启用 LVS；如果 `/proc/net/ip_vs` 不存在，页面会显示“IPVS 未加载”，安装器不会替你加载内核模块。

### 已装 Agent：只升级程序

日常升级不必重打一长串参数，单独执行 `--upgrade` 会保留原来的配置、身份和 systemd 服务：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- --upgrade
```

它只更新程序文件。要改节点名、目录、日志、Stub Status、Keepalived、LVS 观测或 systemd 权限，仍需重新运行完整安装命令。

### 已有身份、改名与重复节点

已经装过 Agent 的机器会保留本机身份；单纯改 `--node-name` **不会**自动新增或改名。需要纠正重名、重新命名或重新接入时，在完整安装命令末尾加 `--force-enroll`，然后去 Web 批准新的接入申请。

`--node-name` 在同一个 Server 中必须唯一。不要让两台机器复用同名，否则后接入的机器可能替换原节点身份。已经错绑时，先让占错名字的机器用自己的正确名称 `--force-enroll` 并批准，再让原机器用原名称 `--force-enroll` 并批准；一次处理一台。

### 写多行命令时的一个坑

反斜杠 `\` 必须是每一行的最后一个字符，后面不能有空格、字母或其他参数；否则下一行不会被当成同一条命令。

### 高级：自定义目录

默认没有覆盖到你的目录时再用这些参数。目录参数可重复写多次：

```bash
--nginx-binary /绝对路径/nginx \
--nginx-root /绝对路径 \
--nginx-config /绝对路径/nginx.conf \
--managed-config-dir /绝对路径/conf.d \
--managed-stream-dir /绝对路径/stream.d \
--managed-cert-dir /绝对路径/cert \
--nginx-log-dir /绝对路径/logs
```

## 配置目录

`--managed-config-dir` 和 `--managed-stream-dir` 都能重复填写。HTTP 文件使用 `*.conf`，Stream 文件使用 `*.stream`。

Agent 会通过 `nginx -T` 确认 Nginx 已加载这些目录。平台只管理登记目录最外层的文件，也不会接管符号链接目录。

主配置 `nginx.conf` 默认只读。需要从平台编辑时，重新安装 Agent 并添加：

```bash
--allow-main-config-edit
```

主配置仍不能删除。所有发布都会执行 `nginx -t`，校验或 reload 失败时恢复原文件。

## 升级与检查

先升级 Server，再执行 Agent 的 `--upgrade`，最后在浏览器按 `Ctrl+F5`。

```bash
# Server
systemctl status nginx-manager
journalctl -u nginx-manager -f

# Agent
systemctl status nginx-manager-agent nginx-manager-agent-helper
journalctl -u nginx-manager-agent -f
```

生产环境建议固定测试过的 commit，并在升级前备份：

```bash
sudo ./deploy/backup-server.sh
```

## 卸载

```bash
# Server
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-server.sh | sudo bash

# Agent
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/uninstall-agent.sh | sudo bash
```

卸载默认保留数据和 Agent 身份。确认不再使用时，为卸载脚本添加 `--purge`。

> HTTP 会明文传输登录会话、Agent 身份和任务内容，只能用于隔离的可信网络。

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

安装在 `/apps/nginx` 的示例：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/install-agent.sh | \
sudo bash -s -- \
  --server http://192.0.2.20:8443 \
  --node-name edge-a-01 \
  --nginx-binary /apps/nginx/sbin/nginx \
  --nginx-root /apps/nginx \
  --nginx-config /apps/nginx/conf/nginx.conf \
  --managed-config-dir /apps/nginx/conf/conf.d \
  --managed-stream-dir /apps/nginx/conf/conf.d \
  --managed-config-already-included \
  --managed-cert-dir /apps/nginx/cert \
  --nginx-log-dir /apps/nginx/logs \
  --stub-status-url http://127.0.0.1:18080/nginx_status \
  --allow-plaintext-log-stream \
  --nginx-service nginx.service
```

两台 Nginx 使用 Keepalived 提供同一 VIP 时，两端安装命令都追加：

```bash
  --keepalived-config /etc/keepalived/keepalived.conf \
  --keepalived-service keepalived.service \
  --keepalived-vip 10.165.0.110
```

升级 Agent 时也要保留这三个参数。Web 的“高可用”页只查看真实 VIP 归属和校验现有配置，不会主动漂移 VIP 或修改 Keepalived。

安装后登录 Web 页面：

1. 在“节点 Agent”批准接入。
2. 在“站点与配置”导入节点现有配置。
3. 在“证书”扫描节点证书。

## 配置目录

`--managed-config-dir` 和 `--managed-stream-dir` 都能重复填写。HTTP 文件使用 `*.conf`，Stream 文件使用 `*.stream`。

Agent 会通过 `nginx -T` 确认 Nginx 已加载这些目录。平台只管理登记目录最外层的文件，也不会接管符号链接目录。

主配置 `nginx.conf` 默认只读。需要从平台编辑时，重新安装 Agent 并添加：

```bash
--allow-main-config-edit
```

主配置仍不能删除。所有发布都会执行 `nginx -t`，校验或 reload 失败时恢复原文件。

## 升级与检查

重新运行原安装命令即可升级。先升级 Server，再升级 Agent，最后在浏览器按 `Ctrl+F5`。

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

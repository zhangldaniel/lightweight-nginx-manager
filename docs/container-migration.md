# 控制端容器迁移

控制端只有两个持久化目录：

- `/var/lib/nginx-manager`：SQLite、备注截图等业务数据。
- `/etc/nginx-manager`：运行参数、LDAP 密码/CA，以及直连 TLS 模式下的证书和私钥。

Agent 身份、审批关系、账号、站点、证书索引和执行记录都在 SQLite 中。保留数据库并继续使用原地址后，Agent 不需要重新审批。

SQLite 文件名可自定义，但必须直接放在 `/var/lib/nginx-manager` 下。迁移和恢复会核对 `server.env` 中的路径与备份元数据；不一致时会在替换原数据前停止，不会静默创建空库。

## 同机从 systemd 切换到 Compose

先安装 Docker Engine 和 `docker compose` 插件，再运行：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/migrate-server-to-container.sh | sudo bash
```

脚本会先构建镜像和创建备份，然后短暂停止 systemd 服务，复用原来的两个数据目录启动容器。健康检查失败时会自动停容器并重新启动 systemd 服务。

原服务由本机 Nginx 反向代理时，也可显式指定：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/migrate-server-to-container.sh | \
sudo bash -s -- --behind-nginx --port 8443
```

回滚只需：

```bash
cd /opt/nginx-manager-container
sudo docker compose down
sudo systemctl enable --now nginx-manager
```

## 迁移到另一台宿主机

旧机导出：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/deploy/backup-server.sh | sudo bash
```

将 `/var/backups/nginx-manager/nginx-manager-*.tar.gz` 加密传到新机，然后执行：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangldaniel/lightweight-nginx-manager/main/migrate-server-to-container.sh | \
sudo bash -s -- \
  --backup /root/nginx-manager-20260807T120000Z.tar.gz \
  --behind-nginx --port 8443
```

新旧控制端不能同时运行。切换 DNS/IP 前先在新机确认 `/healthz`、管理员登录和 Agent 在线状态。

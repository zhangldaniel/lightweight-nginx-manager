# Nginx Manager 控制端

控制端使用 FastAPI + SQLite，提供单 HTML 管理页面、本地或 LDAP / AD 登录、Agent 审批接入和固定类型任务队列。

## 身份模型

- 本地应急管理员通过账号密码登录。密码使用 PBKDF2-SHA256、独立随机盐和可配置迭代次数保存。
- LDAP 用户先由查询账号唯一定位 DN，再使用用户自己的密码 bind；密码不会写入 SQLite。直属组映射为 `admin`、`operator`、`auditor`。
- 登录成功后签发 `Secure`、`HttpOnly`、`SameSite=Strict` Cookie；写请求还必须携带会话内的 CSRF 校验值。
- Agent 不需要人工注册令牌。首次连接提交待审批申请，管理员从 Web 批准后，Agent 和控制端从 Agent 自持秘密推导每机独立机器凭据。
- 控制端数据库只保存 Agent 接入秘密和机器凭据的 SHA-256 摘要，不保存可回显明文。
- 旧版已注册节点的机器身份继续有效；旧管理员令牌和一次性注册入口不再由新服务使用。

## 首个管理员

安装脚本会自动执行一次：

```bash
NGINX_MANAGER_DB_PATH=/var/lib/nginx-manager/manager.db \
NGINX_MANAGER_BOOTSTRAP_USERNAME=admin \
NGINX_MANAGER_BOOTSTRAP_PASSWORD='至少 12 位初始密码' \
python app.py bootstrap-admin
```

命令仅在数据库没有管理员时创建账号，重复运行不会重置现有密码。不要把 bootstrap 密码放入长期 systemd 环境文件。

## API

浏览器身份：

- `POST /api/v1/auth/login`
- `GET /api/v1/auth/session`
- `POST /api/v1/auth/logout`

Agent 接入：

- `POST /api/v1/agent/enroll`
- `POST /api/v1/agent/heartbeat`
- `POST /api/v1/agent/poll`
- `POST /api/v1/agent/jobs/{job_id}/result`

Web 管理 API：

- `GET /api/v1/admin/enrollments`
- `POST /api/v1/admin/enrollments/{id}/approve`
- `POST /api/v1/admin/enrollments/{id}/reject`
- `GET /api/v1/admin/nodes`
- `GET|POST /api/v1/admin/jobs`
- `GET /api/v1/admin/snapshot`
- `GET|PUT /api/v1/admin/ui-state`

管理 API 不返回 Agent 的机器凭据、任务敏感 payload 或原始输出。UI 状态接口拒绝私钥、密码和常见秘密字段。

`auditor` 可读；`operator` 还可保存 UI 状态和创建固定任务；只有 `admin` 能批准或拒绝 Agent 接入。权限由 API 服务端强制执行。

## 运行环境

参见 `env.example`。推荐由本机 Nginx 终止 HTTPS，Uvicorn 只监听 `127.0.0.1` HTTP；`deploy/install-server.sh --behind-nginx` 会自动生成这种 systemd 配置，控制端自身不生成 CA。外部入口必须是 HTTPS；Cookie 使用 `__Host-` 前缀，因此不会在明文 HTTP 上传输。

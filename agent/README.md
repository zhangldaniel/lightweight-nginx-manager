# Nginx Manager Linux Agent

Agent 仅依赖 Python 3.6+ 标准库，拆分为两个进程：

- `run`：普通系统用户运行，只负责 HTTP(S) 接入、心跳、拉取固定任务和回传结果。
- `helper`：root 运行，只监听本机 Unix Socket，仅接受固定动作，不提供任意 Shell。

## 无令牌接入

```bash
python3 nginx_agent.py --config config.json enroll
```

首次执行会在 `state_dir/identity.json` 中以 `0600` 原子保存 Agent 自己生成的接入秘密，然后向控制端提交申请。命令返回“pending”是正常状态；管理员需要在 Web 的“节点 Agent / 待审批接入”中批准。

批准后，Agent 自动推导并保存每机独立 `machine_credential`，清除接入秘密并开始心跳。断电、请求超时或响应丢失不会生成新的申请；Agent 会使用同一份本地申请继续轮询。

重新接入：

```bash
python3 nginx_agent.py --config config.json enroll --force
```

重新接入申请在批准前保留旧机器身份；被拒绝时恢复旧身份。旧版 `identity.json` 中的 `agent_token` 会被当作历史机器凭据继续使用，不需要重新接入。

## 固定动作

`inspect`、`nginx_test`、`nginx_reload`、`config_inventory`、`certificate_inventory`、`config_read`、`config_hash`、`config_apply`、`config_delete`、`certificate_apply`。

`config_inventory` 只读取允许目录内扩展名严格为 `.conf` 的普通文件，忽略 `.bak`、符号链接、私钥内容和超限文件；不会修改配置或 reload Nginx。

`certificate_inventory` 只扫描允许证书目录中的 `.pem` / `.crt`，在节点本地校验证书与私钥是否匹配，只回传域名、签发者、到期时间、原路径和 SHA-256；私钥内容永不离开节点。

配置和证书只能写入安装时指定的专用托管目录。每次发布使用期望 Hash、防符号链接/路径越界、原子替换、`nginx -t`、reload、可选健康检查和失败恢复。

`config_apply.expected_sha256` 支持真实 SHA-256、`missing` 和 `present`。`missing` 只允许新建，文件已存在即拒绝；`present` 只允许替换，文件不存在即拒绝，并在结果中返回替换前 Hash。这样控制端可以把同一份配置安全复制到不同节点各自的托管配置目录。

`config_delete` 只接受配置文件当前的精确 SHA-256，不接受 `present` 或盲删。它只可删除托管配置目录内的 `.conf`，删除后固定执行 `nginx -t` 和 reload；失败或掉电时使用持久化事务恢复原文件。此动作需要 Agent 0.4.0 或更高版本。

推荐通过根目录的 `deploy/install-agent.sh` 安装 systemd 服务，不要手工以 root 运行网络 Agent。
安装器接受 HTTP 或 HTTPS 控制端；HTTP 会自动写入 `allow_insecure_http=true`，仅应在隔离且可信的管理网使用。

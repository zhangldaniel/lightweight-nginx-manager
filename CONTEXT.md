# Traffic management domain

本上下文描述 Linux IPVS 四层流量入口、后端成员和调度节点之间的关系。

## Language

**Director Group**：共同承载一组 Virtual Service，并预期保持运行规则一致的调度节点集合。

_Avoid_：Device Group、LVS 集群

**Director Node**：运行 Linux IPVS，并为 Director Group 接收前端流量的调度主机。

_Avoid_：Real Server、后端节点

**Virtual Service**：由地址、端口和协议组合，或由 fwmark 标识的四层流量入口。

_Avoid_：Site、Virtual Server

**Backend Pool**：属于一个 Virtual Service 的后端成员集合；它不是 IPVS 内核中的独立对象。

_Avoid_：Service Group

**Real Server**：最终接收转发流量的后端地址身份。

_Avoid_：Agent Node、Director

**Pool Member**：Real Server 在一个 Backend Pool 中的可调度实例。

_Avoid_：Node

**Scheduler**：在可用 Pool Member 之间分配新流量的调度策略。

_Avoid_：Load Balancing Method

**Persistence Profile**：在限定时间和范围内，让相关流量继续命中同一 Pool Member 的规则。

_Avoid_：Session Stickiness

**Health Monitor**：判断 Pool Member 是否可服务的检查定义或证据来源。仅凭运行表中存在该成员，不能宣称其健康。

_Avoid_：IPVS Health

**Configuration Owner**：对 Virtual Service 期望配置拥有最终修改权的系统。

_Avoid_：Source

**Observed State**：从当前 Linux 运行环境读取的事实，不代表期望配置。

_Avoid_：Configuration

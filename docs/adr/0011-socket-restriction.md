# ADR-0011：隔离中的命令不得创建套接字通道

- 状态：已接受
- 日期：2026-09-29
- 决策者：chenmingboi（仓库作者；2026-09-28 指示「一直往下尽可能的做」，本 ADR 的决策委托本次提案的推荐方案，可随时以新 ADR 推翻）
- 相关：开发规范 §10.7.1、§10.7.3、§10.7.5、§13.8；ROADMAP 小项 L.5、L.6；ADR-0010；THREAT_MODEL TM-37

## 背景

ADR-0010 已实现并合并：Linux 上 `run_command` 启动的命令默认在 Landlock 隔离中运行。L.4 重新评估威胁模型时，在 Linux 6.18、Landlock ABI 7 上实测了 ADR-0010 列为「未覆盖」的通道：

- 隔离中的命令可以按路径连接工作区之外的 Unix 套接字。Landlock 管辖文件的打开、创建与删除，不管辖对一个已存在套接字文件的 `connect`。
- 隔离中的命令可以发送 UDP 数据报。Landlock 的网络规则只覆盖 TCP 的 `bind` 与 `connect`。
- `/dev/shm` 被拒绝；`socketpair` 不受影响。

第一条的后果比「剩余风险」更重。Docker 守护进程的套接字、用户的 D-Bus 会话总线（经由它可以让 `systemd --user` 启动任意进程）、SSH agent，都是「让一个不在隔离中的进程替你做事」的入口。在能访问这些服务的主机上，隔离对一个知道路径的脚本等于不存在，阶段 L 的退出条件「被允许的工作区脚本不能读写工作区以外的宿主文件」在这些主机上不成立。

在同一个容器里用 ctypes 装载 seccomp-bpf 过滤器做了原型：拒绝 `socket(AF_UNIX, …)` 之后，`socketpair`、asyncio 的事件循环、`multiprocessing.Pipe` 都照常工作，子进程继承过滤器；在拒绝网络时再拒绝其余地址族的 `socket()`，UDP 也被拒绝。

## 决策

### D1 用 seccomp-bpf 限制套接字的创建

我们将在执行包装进程里、`execve` 之前，于 Landlock 之外再装载一个 seccomp-bpf 过滤器，同样经 `ctypes` 调用 `prctl`，不引入依赖。过滤器对被拒绝的调用返回 `EACCES`，命令看到的是普通的权限错误，而不是被杀死。

规则按系统调用号与第一个参数（地址族）判断，这是 seccomp 能可靠检查的全部内容——它无法读取 `connect` 的目标路径，所以限制放在创建套接字这一步：

- 总是拒绝 `socket(AF_UNIX, …)`：没有 Unix 套接字，就无法按路径连接任何本地服务，也无法连接抽象命名空间的套接字。`socketpair` 不受影响，它创建的是一对彼此相连、没有名字的套接字，asyncio 与 multiprocessing 依赖它。
- 拒绝网络时（默认），拒绝任何地址族的 `socket()`：这同时关闭了 UDP，以及 VSOCK、netlink 等其他通道，并使 TCP 拒绝不再依赖 Landlock ABI ≥ 4。
- 总是拒绝 `io_uring_setup`：io_uring 可以不经 `socket()` 系统调用创建套接字，会绕过上面的规则。
- 系统调用的架构与过滤器不符时终止进程：否则一个 64 位进程可以经 32 位的系统调用入口绕过按调用号的判断。x86_64 上的 x32 调用号一律拒绝。

### D2 支持的架构

过滤器支持 x86_64 与 aarch64。其他架构上套接字限制不可用，状态为 `partial`，原因写明；`required` 因此拒绝启动。

### D3 状态与记录

`enforced` 从此意味着文件系统、所要求的网络限制与套接字限制都生效。`partial` 表示文件系统隔离生效，但网络或套接字限制之一未生效。隔离事件增加 `sockets_restricted` 字段。网络限制在 seccomp 可用时不再依赖 Landlock ABI：两层都可用时两层都装载。

### D4 失败即关闭

过滤器装载失败与 Landlock 建立失败同样处理：原命令不运行，结果为 `SANDBOX_SETUP_FAILED`。

## 备选方案

**维持 ADR-0010 的现状，只在文档里写明。** 优点是没有新代码。没有选是因为「能访问 Docker 或会话总线的主机上隔离无效」不是一条边缘风险，而是隔离的主要用途在这些主机上失效。

**等待 Landlock 管辖 Unix 套接字的新 ABI。** 没有选是因为当前内核上不可用，而受影响的主机今天就存在。新 ABI 可用后可以叠加，不冲突。

**用 seccomp 拒绝 `connect`。** 没有选是因为 seccomp 看不到 `connect` 的目标地址，只能全部拒绝或全部放行；拒绝全部 `connect` 与拒绝 `socket()` 效果相同，却更难解释。

**挂载命名空间隐藏套接字文件。** 没有选，理由与 ADR-0010 放弃 `unshare` 相同：非特权用户命名空间在主流发行版上越来越多地被默认限制。

**放开网络时也拒绝所有 `socket()`。** 没有选是因为用户放开网络就是要让命令建立 TCP 连接。放开网络时仍然拒绝 Unix 套接字。

## 后果

- **正面：**
  - 隔离中的脚本不能再经 Docker、会话总线或 SSH agent 让隔离之外的进程替它做事。
  - 拒绝网络时 UDP 与其余地址族一并关闭，且不再依赖 Landlock ABI ≥ 4。
- **负面与新增风险：**
  - 使用 Unix 套接字的程序在隔离中失败，例如启动 Unix 套接字服务的测试、写 `/dev/log` 的 syslog 处理器、图形界面测试、Docker 客户端。缓解是 `--sandbox off`，并在 README 说明；放开网络不会放开 Unix 套接字。
  - 拒绝网络时，连创建一个仅用于查询空闲端口的套接字也会失败。这与 ADR-0010 已经拒绝 TCP `bind` 的效果一致。
  - 过滤器依赖系统调用号与审计架构常量，只覆盖两种架构；其他架构的状态为 `partial`。
  - 内核的 seccomp 实现成为新的信任基础。
- **需要同步修改：** 开发规范 §10.7.1、§10.7.3、§10.7.5、§13.8、§15.1；`docs/ROADMAP.md`；`CHANGELOG.md`。实现落地时还需要修改 §5.1 目录树、`README.md`、`docs/DESIGN.md`、`docs/THREAT_MODEL.md`（TM-37 的状态与剩余风险）和 `docs/COMPLIANCE.md`。

## 验证

- 在 seccomp 与 Landlock 都可用的 Linux 上：隔离中的脚本创建 Unix 套接字失败；按路径连接工作区之外一个正在监听的 Unix 套接字失败；拒绝网络时发送 UDP 失败；`socketpair`、asyncio 事件循环与 `multiprocessing.Pipe` 仍然可用；放开网络时 TCP 连接成功而 Unix 套接字仍被拒绝；子进程继承这些限制。
- 过滤器装载失败时原命令不运行，结果为 `SANDBOX_SETUP_FAILED`。
- 不支持的架构上状态为 `partial`，`required` 拒绝启动。
- 隔离事件记录 `sockets_restricted`。

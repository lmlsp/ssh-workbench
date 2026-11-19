# SSH Workbench · 运维工作台

一个面向个人的本机 SSH 工作台。首页可以添加多个独立的 SSH 窗口，平铺查看或切回单窗口。把常用命令存成快捷指令，或将几条指令排成自动化流程，执行时直接看终端输出。

SSH Workbench is a local, single-user tool for tabbed and split SSH terminals, reusable commands, and ordered workflows. It runs without a separate workflow engine or database service.

## 本机启动

需要 Linux、Python 3.10+、Node.js/npm 和 OpenSSH 客户端。目标主机需要 Linux Bash 和常见的 coreutils。切换用户使用 util-linux `su`，跟踪日志需要目标主机上的 `/usr/bin/python3`。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --prefix ui
python workbench.py set-password
python workbench.py
```

打开 [http://127.0.0.1:14441/](http://127.0.0.1:14441/)，用 `admin` 和刚刚设置的密码登录。所有配置初始为空。可用 `WORKBENCH_PORT` 改端口，`WORKBENCH_DATA_DIR` 指定独立的数据目录。默认监听 IPv4 回环地址。

内网主机可用 `WORKBENCH_BIND_ADDRESS` 选择一个具体的 IPv4 地址，`WORKBENCH_ALLOWED_NETWORKS` 填允许访问的 IPv4 CIDR（多项用逗号分隔），并通过 `WORKBENCH_TLS_CERT` 和 `WORKBENCH_TLS_KEY` 指定 HTTPS 证书和私钥。证书应覆盖访问 IP。内网模式仍使用单个管理员账户。

选择明文 HTTP 时，保留具体绑定 IP 和允许访问的网段，设置 `WORKBENCH_ALLOW_HTTP=1`，并留空 TLS 证书和私钥配置。这时浏览器到工作台的连接为 HTTP。

需要管理工作台所在主机时，可在非 root 的部署中设置 `WORKBENCH_ENABLE_LOCAL_TERMINAL=1`。首页会显示「本机终端」，直接以工作台进程的用户在其家目录打开 Bash。该按钮让网页登录的管理员拥有该部署用户可执行的本机操作权限，默认关闭。

## 使用方式

1. 在「服务器」保存 SSH 主机、端口和登录用户。同一主机与 SSH 端口下的不同账号归在同一组。登录密钥和 SSH 配置沿用当前系统用户的 OpenSSH 配置，也可在连接编辑页保存加密的 SSH 登录密码。首次连接新主机时仍需在终端核对指纹。SSH 登录密码和快捷指令的 `su` 密码分开设置。
2. 在「终端」选择连接，直接输入命令。Bash 返回提示符后，可以在右侧把执行过的命令保存为快捷指令。也可以在「快捷指令与流程」直接录入命令。`su - 用户` 切换成功时立即显示在执行记录里，保存后可单独执行，在选中终端切换到目标用户。类型和切换用户密码都是选填。
3. 将同一连接、同一执行用户的指令按顺序组合。拖动步骤左侧的把手可以排序，聚焦把手后也可以按上下方向键。在列表点「复制」可直接编辑一份副本，副本沿用原有快捷指令。每条步骤可以有自己的工作目录。流程开始时读取指令配置，之后的指令编辑会影响下一次执行。
4. 在命令里选中一个完整的值，再点「把选中的内容设为参数」。例如 `printf '%s\n' {{buildUrl}}` 会在运行时提示输入值。参数会按一个 shell 参数传入。一个流程里的同名参数只填写一次。
5. 终端右侧显示选中服务器的快捷指令和流程，执行时复用当前选中的终端。终端需要使用该操作的 SSH 连接，并已回到命令提示符。用「添加窗口」建立更多独立终端，平铺时默认每行最多两个窗口。拖动窗口右下角可以单独调整宽高，也可以用方向键调整，双击或按 Home 恢复默认。点对应窗口或标签的关闭按钮即可关闭该连接。
6. 快捷指令在选中的 Bash 里执行，`cd` 等命令会改变该终端的状态。流程成功提示在 5 秒后收起。某步失败时会停住，也可以接管正在执行的终端，回答程序的提示，或用 `Ctrl+C` 中断后手工排查。处理完选择重试、手工完成后继续，或结束。浏览器刷新后可以接回原来的终端。
7. 删除流程会保留它引用的快捷指令。删除被引用的快捷指令前，工作台会列出使用它的流程。删除服务器或单条 SSH 连接时可以先查看关联配置，再一起清理。有终端任务运行或等待输入时，先处理完任务。
8. 在终端里可以用 `ssh 用户@域名`、`ssh 用户@IP` 或 `ssh -p 端口 用户@主机` 登录下一台服务器，按提示核对主机指纹和输入密码。登录成功后，右侧会显示当前服务器的执行记录。第一次保存快捷指令时会一并保存目标连接。指令按登录路径关联，再次沿同一路径登录就能复用。输入 `exit` 会回到上一层。带远程命令或其他选项的 SSH 调用沿用 OpenSSH 的行为。

## 适用范围

这是单用户本机应用，已保存的命令以远程账号本身拥有的权限执行。录入的切换密码加密存于本机数据目录，管理员可以在编辑页查看或更换。终端的密码提示输入不会保存为命令。命令自身含有的文本会保留，请用切换密码栏或你已有的凭据机制传递秘密。

首次连接新主机时，在终端核对并接受 SSH 主机指纹。终端保存在本机服务进程内，服务退出后需要重新连接。已有版本保存的连接和指令会按主机及 SSH 端口归类，已有日志配置可以在删除服务器时一并清理。

## 开发检查

```sh
python -m unittest discover -s tests -v
node --check ui/ssh.js
node --check ui/terminal-ui.js
bash -n shell-integration.sh
```

本项目采用 [MIT 许可证](LICENSE)。浏览器终端使用 [xterm.js](https://github.com/xtermjs/xterm.js)，其许可证随 npm 依赖安装。

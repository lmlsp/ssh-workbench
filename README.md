# SSH Workbench · 运维工作台

一个面向个人的本机 SSH 工作台。用浏览器连服务器、执行命令、保存快捷指令，或把几条指令排成流程。从应用日志窗口发起任务时，日志和执行终端可以放在同一个页面。

SSH Workbench is a local, single-user tool for interactive SSH, reusable commands, ordered workflows, and side-by-side application logs. It runs without a separate workflow engine or database service.

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

打开 [http://127.0.0.1:14441/](http://127.0.0.1:14441/)，用 `admin` 和刚刚设置的密码登录。所有配置初始为空。可用 `WORKBENCH_PORT` 改本机端口，`WORKBENCH_DATA_DIR` 指定独立的数据目录。服务仅监听 IPv4 回环地址。

## 使用方式

1. 在「服务器」保存 SSH 主机、端口和登录用户。可为同一台机器保存多个账号，也可以关联应用及其日志文件。登录密钥和 SSH 配置沿用当前系统用户的 OpenSSH 配置。
2. 在「终端」选择连接，直接输入命令。Bash 返回提示符后，可以在右侧把执行过的命令保存为快捷指令。也可以在「快捷指令与流程」直接录入命令。类型、关联应用及切换用户密码都是选填。
3. 将同一连接、同一执行用户的指令按顺序组合。每条步骤可以有自己的工作目录。流程开始时读取指令配置，之后的编辑会影响下一次执行。
4. 在命令里选中一个完整的值，再点「把选中的内容设为参数」。例如 `printf '%s\n' {{buildUrl}}` 会在运行时提示输入值。参数会按一个 shell 参数传入。一个流程里的同名参数只填写一次。
5. 在「看日志」打开最多四个独立窗口。每个窗口可选应用和日志来源，保留文件原有的换行和缩进，脱敏常见凭据。各窗口的「快捷执行」按连接、应用和类型筛选，执行终端和日志可以同时显示。
6. 流程的一步失败时会停住。也可以主动接管正在执行的终端，回答程序的提示，或用 `Ctrl+C` 中断后手工排查。处理完选择重试、手工完成后继续，或结束。后台终端可以在浏览器刷新后重新打开。

## 适用范围

这是单用户本机应用，已保存的命令以远程账号本身拥有的权限执行。录入的切换密码加密存于本机数据目录，管理员可以在编辑页查看或更换。终端的密码提示输入不会保存为命令。命令自身含有的文本会保留，请用切换密码栏或你已有的凭据机制传递秘密。

应用日志使用独立的 SSH 连接读取，所选登录用户需要具备文件读取权限和无人值守的 SSH 密钥认证。首次连接新主机时，在终端核对并接受 SSH 主机指纹。终端保存在本机服务进程内，服务退出后需要重新连接。

## 开发检查

```sh
python -m unittest discover -s tests -v
node --check ui/ssh.js
bash -n shell-integration.sh
```

本项目采用 [MIT 许可证](LICENSE)。浏览器终端使用 [xterm.js](https://github.com/xtermjs/xterm.js)，其许可证随 npm 依赖安装。

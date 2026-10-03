<#
.SYNOPSIS
    启动守望服务（隐藏窗口，供登录自启使用）。

.DESCRIPTION
    这一层存在的唯一理由是：**让守望服务完全脱离调用者的控制台**。

    ## 为什么不能是 `.cmd`

    这个启动路径试过三种写法，前两种都**看起来**能用、实际不行：

    | 写法 | 结果 |
    | --- | --- |
    | `start "NovaLoc Watch" /min launcher.cmd` | 前台**不返回**；守望随调用者一起死 |
    | `start "" /b cmd.exe /c start ... /min launcher.cmd` | 前台**立刻返回**、父进程已退出，**但守望仍然随调用者一起死** |
    | 本脚本（`Start-Process`） | 不继承调用者句柄，立即返回 |

    失败原因是**控制台**：`.cmd` 起的子进程会继承调用者的控制台与控制台组，
    还握着它 stdout/stderr 的句柄。调用者的控制台一关，
    整组收到 `CTRL_CLOSE_EVENT`，常驻的孙子进程跟着一起走。

    ⚠️ 这类问题**很难在自动化里证伪**：本仓库的工具链在任务结束时
    会杀掉整棵进程树，所以"从后台任务里测出守望存活"根本不成立。
    第二版写法就是这样被误判成"可用的"——它打印出"已脱离"，
    但父进程链断了 ≠ 进程组断了。**要真验证只能靠一次真实登录。**

    ## 为什么不是 `.vbs`

    `.vbs` + `WScript.Shell.Run(cmd, 0, False)` 是隐藏窗口的经典写法，
    也试过并**弃用**：这台机器上 VBScript 行为异常 ——
    `cscript` 打印了 `PROBE_DONE`，但它本该写出的文件根本不存在；
    还对**已存在**的目录报"路径未找到"。排查它的成本远超收益。

    ## 为什么必须 `-EncodedCommand`（在安装脚本里）

    快捷方式的 `Arguments` 是一个**字符串**，而仓库路径含中文。
    手工拼引号必然在这个路径上翻车，所以安装脚本用
    `-EncodedCommand`（base64 UTF-16LE）：一个参数、零引号、零代码页问题。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'watch-new-games.cmd'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "缺少守望启动器：$launcher"
}

# -WindowStyle Hidden：不弹窗；不 -Wait ⇒ 立即返回，服务常驻。
# Start-Process 起的新进程**不继承**本进程的 stdout/stderr 句柄。
Start-Process -FilePath $launcher -WindowStyle Hidden

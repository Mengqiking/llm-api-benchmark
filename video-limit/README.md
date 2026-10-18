# K3 视频输入上限探针

这个目录用于重复测试兼容 OpenAI Chat Completions 的 K3 视频 URL 输入能力。

## 安全默认值

脚本默认只执行公开 URL 的 `HEAD` 预检，不调用模型、不产生模型用量。只有显式添加 `-Execute` 才会发起真实模型请求。

API Key 不写入脚本、清单或结果文件。运行前临时设置环境变量：

```powershell
$env:ONELINK_API_KEY = '<your-key>'
```

测试结束后清除：

```powershell
Remove-Item Env:ONELINK_API_KEY -ErrorAction SilentlyContinue
```

## 使用方法

只验证 URL、MIME 和文件字节数，不调用模型：

```powershell
cd D:\Dev\test\k3\benchmark-kit\video-limit
.\video-size-probe.ps1 -Set controlled
```

对同一 SampleFile 系列从大到小测试；遇到第一个成功点停止：

```powershell
.\video-size-probe.ps1 -Set controlled -Order descending -Execute
```

复现 PSU 长视频实验组：

```powershell
.\video-size-probe.ps1 -Set psu -Order descending -Execute
```

即使成功也继续跑完全部条目：

```powershell
.\video-size-probe.ps1 -Set controlled -Order descending -Execute -ContinueAfterSuccess
```

每次运行的 JSON 和 CSV 会写入 `runs/<timestamp>/`。

## 真实编码的 100 到 50 MiB 边界测试

`Run-ControlledDescendingProbe.ps1`生成或复用六个真实媒体负载视频：60 秒、H.264/AAC、640×480、25fps，并通过调整视频码率得到约 100、90、80、70、60、50 MiB 的档位。它们不是通过追加 `free` box 制作的空填充文件。

脚本会临时启动本机只读 HTTP 服务和 Cloudflare Quick Tunnel，按 100 到 50 MiB 递减并跑完所有档位。正式运行还会用 `reasoning_effort=none` 与 `low` 各做一次小请求，判断接口是否接受独立思考关闭参数。无论成功或失败，退出时都会关闭临时服务和隧道。

只验证生成、隧道和六个公网URL，不调用模型：

```powershell
cd D:\Dev\test\k3\benchmark-kit\video-limit
.\Run-ControlledDescendingProbe.ps1 -PreflightOnly
```

```powershell
$env:ONELINK_API_KEY = '<your-key>'
cd D:\Dev\test\k3\benchmark-kit\video-limit
.\Run-ControlledDescendingProbe.ps1
Remove-Item Env:ONELINK_API_KEY -ErrorAction SilentlyContinue
```

推荐通过 toolkit 的 provider 通用入口调用：

```powershell
$env:SHUYANAI_API_KEY = '<your-key>'
& D:\Dev\test\k3\benchmark-kit\scripts\Run-VideoLimit.ps1 -Provider shuyanai -Model kimi-k3
Remove-Item Env:SHUYANAI_API_KEY -ErrorAction SilentlyContinue
```

每轮输出 `benchmark_report.md`、`summary.json`、`results.json` 和 `results.csv`。报告首页直接显示本轮最大成功视频档位、相邻失败档位和独立思考开关结论。

## 测试方法论

1. 先做无模型调用的预检：HTTP 200、`video/mp4`、`Content-Length` 与清单一致。
2. 同一轮边界判断只比较同一个 `set`。不同来源、编码、分辨率和时长的数据不得直接合并成文件大小阈值。
3. 已有成功下界和失败上界后，选择两者中点制作下一档。例如同系列 50 MiB 成功、100 MiB 失败，下一档约为 75 MiB；如果已有可信的 72.9 MiB 失败，则下一档约为 61.5 MiB。
4. 从大到小跑完全部档位，避免单次成功或失败造成误判；需要严谨结论时，对边界相邻两档各复测一次。
5. 每个候选视频应记录精确字节数、容器、视频/音频编码、分辨率、帧率、时长、公开 URL 和校验值。
6. 推荐固定 H.264 + AAC、MP4、相同分辨率/帧率/时长，只调整目标码率来生成不同大小。生成后放到支持匿名 HTTPS、HEAD 和 Range 请求的稳定对象存储中，再写入 `video-cases.json`。
7. `LOCAL_OR_SOURCE_ERROR` 和 `PREFLIGHT_ONLY` 没有模型调用；带远端 `request_id` 的失败表示网关已经接收并处理请求，不能假定免费。

## 计费核对

OneLinkAI 的公开文档说明按实际使用量计费并提供消耗统计，但没有承诺所有失败请求免费。因此首次测试新的错误类型时：

1. 在控制台记录调用前余额或用量明细。
2. 只发起一次请求，并保存 `request_id`。
3. 等待账单入账后记录调用后余额和明细。
4. 只有账单证据显示该错误类型连续不计费，才考虑扩大由大到小的失败探测；仍应设置最大请求数。

## 结果解释

- `Media exceeds download limit` 表示命中远程文件下载字节限制。
- `Media could not be decoded within supported limits` 表示命中解码/预处理限制，不能单独用来推断文件大小上限。
- 最大成功档位只是测试集合中的成功下界；如果最大档位也成功，应继续增加档位后再宣称平台绝对上限。
- `reasoning_effort=none` 被接受但输出侧没有可观察差异时，报告只写“参数被接受”，不会直接宣称已关闭思考。


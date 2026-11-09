# SSE Proxy Studio

独立的 SSE 录制 / 本地回放代理。采用 mitmproxy 作为 HTTP(S) 代理，使用本地浏览器控制台管理记录，无需修改 iOS、Android 或 Web 工程。

当前版本是本地代理＋Web 控制台，不是原生 macOS `.app`，也不是 Charles/Proxyman 的完整替代品。

## 启动

需要 macOS、Python 3.10+ 和 `mitmdump`。本次验证环境：Python 3.14、mitmproxy 12.2.3。

```sh
brew install mitmproxy
python3 run.py
```

- 控制台：`http://127.0.0.1:9090`
- HTTP(S) 代理：`127.0.0.1:8088`
- 数据：`~/Library/Application Support/SSEProxyStudio/`
- `Ctrl+C` 关闭；再次启动保留历史录制，但模式恢复为透传。

控制台和 SQLite 使用系统 Python，mitmproxy 插件只使用它自带的 Python 环境，兼容 Homebrew 的独立二进制发行版。无需 pip/npm 安装前端或数据库依赖。

自定义启动参数：

```sh
python3 run.py --proxy-port 8088 --ui-port 9090 \
  --data-dir "$HOME/Library/Application Support/SSEProxyStudio"
```

## 接入客户端

### Mac / iOS Simulator

让客户端使用 HTTP 代理 `127.0.0.1:8088`。命令行可显式传递代理；Simulator 的 App 通常需要配置 Mac 网络代理，然后重新发起连接。

HTTPS 需要在目标系统安装并信任此工具的 CA：

1. 客户端经代理访问 `http://mitm.it`，或从控制台「连接说明」下载 CA。
2. Mac 通过钥匙串安装并信任证书。
3. iOS 安装描述文件后，在「设置 → 通用 → 关于本机 → 证书信任设置」启用信任。
4. Simulator 也可用下面的命令添加到指定测试模拟器：

```sh
xcrun simctl keychain booted add-root-cert \
  "$HOME/Library/Application Support/SSEProxyStudio/certificates/mitmproxy-ca-cert.pem"
```

工具不会自动修改系统代理、安装证书或改变系统信任。使用完毕后手动恢复代理配置。

### iPhone 真机

```sh
python3 run.py --listen-host 0.0.0.0
```

手机和 Mac 连接同一可信局域网。在 iOS Wi-Fi 的 HTTP 代理中填写 **Mac 的局域网 IP** 和 `8088`，再安装并信任 CA。控制台始终仅监听本机 `127.0.0.1`。

开启 `0.0.0.0` 后，该代理对可连接到此 Mac 的网络设备开放，不应暴露到公共网络。

## 录制

1. 在控制台选择「录制」。
2. 可用 Host / Path 通配符缩小范围，例如 `*.example.com`、`*/chat/stream*`。
3. 正常操作客户端。仅响应 `Content-Type: text/event-stream` 的流会被保存。
4. 查看历史记录的事件时间线、请求 / 响应和原始 JSON。

真实响应在转发的同时以原始字节块写入 SQLite WAL，保留事件、注释心跳、UTF-8 分块以及不完整帧。请求结束后标记 completed；断线标记 error；代理退出前未结束的记录在下次启动标记 interrupted。

切换到透传只影响后续请求；已经开始录制的流继续记录到结束。需要停止当前流时，在客户端停止请求。

每块记录相对请求转发前的时间；界面事件时间为完整 SSE 帧到达时刻。磁盘或控制台错误会写入终端日志，已经开始的真实 SSE 仍继续转发；若路由控制台不可用，新请求返回明确的 502，避免意外穿透回放规则。

## 回放历史

1. 选择一条已结束的历史记录，点击「选为回放源」。
2. 切换为「回放」，选择 0.5× / 1× / 2× / 4× 或即时模式。
3. 客户端再次请求相同接口。

匹配 **HTTP 方法＋协议＋域名＋有效端口＋路径**，可选择是否同时匹配 query。请求正文不参与匹配。命中后转到本地定时流式服务，不连接真实上游；其他请求照常转发。

历史回放保留原始响应字节、首响应等待、各块间隔和结束等待。倍速按时间轴缩放；每个回放使用独立连接，可并发。客户端断开会终止本地回放连接。error/interrupted 记录在末尾断开，不伪造正常完成。

只启用一条回放规则。选用另一条录制后，后续请求使用新的规则，已开始回放的请求不受影响。

## 回放本地 JSON

控制台支持「新建 JSON」和「导入 JSON」，也可直接编辑历史记录，再「保存副本并选用」。选用不会自动开启回放模式。

使用 `examples/local-response.json` 作为模板：

```json
{
  "format": "sse-proxy/v1",
  "name": "自定义回复",
  "request": {
    "method": "POST",
    "url": "https://api.example.com/chat/stream"
  },
  "response": {
    "status": 200,
    "headers": { "content-type": "text/event-stream; charset=utf-8" }
  },
  "response_offset": 0,
  "duration": 1,
  "outcome": "completed",
  "frames": [
    { "offset": 0.2, "event": "message", "data": { "text": "你好" } },
    { "offset": 1, "event": "done", "data": "[DONE]" }
  ]
}
```

- `frames[].data` 支持字符串或 JSON 对象。对象序列化为 JSON；多行字符串正确转换为多个 `data:` 行。
- 支持 `event`、`id`、`retry`、`comments`；`comments` 为字符串数组。
- `offset` 是从请求开始算起的秒数，必须非负、不递减，不能早于 `response_offset`。
- `duration` 不能小于最后一个 offset，最大 24 小时。
- `outcome` 支持 `completed`、`error`、`interrupted`。
- 原始导出使用 `chunks: [{"offset": 0.2, "base64": "..."}]`，精确保留字节分块。`frames` 和 `chunks` 二选一。
- 事件 JSON 编辑副本可能规范化换行、忽略未知 SSE 字段；原始 JSON 不做这种转换。压缩响应和末尾不完整帧只提供原始字节编辑。
- 导入上限 32 MB，保存时产生新的录制 ID，原记录保持不变。

工具不会自动改写业务 `conversation_id`、`request_id` 等字段。需要让录制内容对应当前会话时，在事件 JSON 中修改相应数据。

## 本地演示

另开终端启动示例服务：

```sh
python3 demo.py
```

在控制台开启录制后：

```sh
curl --noproxy '' -N -x http://127.0.0.1:8088 \
  -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:9101/stream
```

将历史记录选为回放源并开启回放，再执行相同 curl。查询示例服务的上游请求计数：

```sh
curl --noproxy '*' http://127.0.0.1:9101/count
```

回放命中时，上游计数不增加。也可以停止示例服务，继续验证本地回放。

## 范围和限制

- 不修改应用代码；只有实际经过代理的 HTTP(S) 请求可被捕获。
- 尚未针对真实 AgnesCN App 做完整联调。普通 HTTP 链路仍访问真实服务，不是全离线 App。
- 证书锁定、不遵循系统代理的自定义连接、QUIC/HTTP3 等需要客户端额外处理；本工具不会绕过这些设置。
- 失败回放模拟的是已记录响应后的中断，不模拟 TLS 握手错误、TCP 包、操作系统网络错误码或 HTTP trailers。
- 默认过滤 Authorization、Cookie、Set-Cookie 等常见敏感请求头。URL 和 SSE 原始字节仍会保存；请求正文仅展示前 64 KiB 的 UTF-8 预览，并用 `body_truncated` 标记截断。导出前可编辑检查。
- 列表展示最近 500 条；磁盘记录不会自动清除，可在界面删除。
- 控制台仅限本机、验证 Host/Origin、写入 API 需要每次启动生成的会话密钥；不依赖外部 CDN。
- SQLite 和证书位于独立数据目录；运行目录不包含用户流量和证书私钥。

## 结构

| 文件 | 职责 |
| --- | --- |
| `run.py` | 启动 / 停止代理和控制台，检查端口、独占数据目录 |
| `addon.py` | mitmproxy SSE 识别、原始流录制、本地回放路由 |
| `server.py` | 本地控制 API、界面静态文件、独立定时回放服务 |
| `storage.py` | SQLite、SSE 事件展示、JSON 导入导出 |
| `static/` | 无 npm 构建依赖的管理界面 |
| `demo.py` | 可独立运行的 SSE 示例上游 |

MITM 代理参考：

- https://docs.mitmproxy.org/stable/overview/getting-started/
- https://docs.mitmproxy.org/stable/concepts/certificates/
- https://github.com/mitmproxy/mitmproxy/blob/main/examples/addons/http-stream-modify.py

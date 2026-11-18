# Google API 出口代理（127.0.0.1:18890）

生产服务器访问 Google（GA4、Search Console、OAuth 令牌）走专用代理
`website-analytics-google-proxy.service`，监听 `127.0.0.1:18890`。
2026-11-18 起启用，取代原来的 `cyber.service`（127.0.0.1:8890）。

## 文件

| 仓库路径 | 服务器路径 |
|---|---|
| `website-analytics-google-proxy.service` | `/etc/systemd/system/website-analytics-google-proxy.service` |
| `website-analytics-sync@.service.d/proxy.conf` | `/etc/systemd/system/website-analytics-sync@.service.d/proxy.conf` |
| `website-analytics-sync@.service.d/zz-google-proxy.conf` | `/etc/systemd/system/website-analytics-sync@.service.d/zz-google-proxy.conf` |

`proxy.conf`（2026-08）设置 8890 代理和 `NO_PROXY`；`zz-google-proxy.conf`
按文件名排序后加载，把代理覆盖为 18890，并依赖代理服务启动。

## 不在仓库里的内容

- `/etc/website-analytics-google-proxy/node.json`：代理节点配置，含节点地址和凭据，
  只保存在服务器上，通过 `LoadCredential` 加载。**不要提交到仓库**（仓库是公开的）。

## 注意

- `zz-google-proxy.conf` 用 `ExecStartPre=` 和 `ExecStopPost=` 清空了主服务文件里的
  启动前/停止后步骤，因此不再运行 `scripts/configure_cyber_proxy.py`（只用于 8890），
  也不再添加访问询盘数据库 `65.49.212.57` 的 `ip rule`。启用后询盘来源一直正常，
  如果以后询盘连接出问题，先检查这一点。
- 授权辅助脚本 `oauth_helper.py` 的代理地址是写死的；续期时要用指向 18890 的副本。
- 回退到 8890：删除 `zz-google-proxy.conf`，然后 `systemctl daemon-reload`。

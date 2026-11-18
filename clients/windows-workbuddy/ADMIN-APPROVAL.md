# 管理员单独批准清单（当前未执行）

本客户端包不是授权。先确认老板本人和Windows设备、公钥指纹、数据只读范围，再批准下列具体动作。

- 新建独立`analyticsquery-tunnel`身份，和运行身份analyticsquery分离；无sudo、无额外组、不能读源凭据/私有数据。
- root管理`/etc/ssh/authorized_keys/analyticsquery-tunnel`，目录0755、文件0600；仅登记本人核对过的公钥，保留逐人指纹记录以便撤销。私钥不离开设备。
- 在`sshd_config`末尾追加仅匹配该身份的Match块，不能改变原root/管理账号设置。
- 允许publickey和local TCP forwarding，只到`127.0.0.1:8765`。`MaxSessions 0`、无TTY、agent/X11/streamlocal/remote forwarding均关闭，`PermitListen none`、`GatewayPorts no`、`PermitUserRC no`。
- 保留管理员现有SSH会话，先`sshd -t`及该身份/原管理员的有效配置对比。仅在批准后reload ssh；配置或账号状态不兼容时停止，不放开shell/密码认证绕过。
- 不开放8765公网、不改防火墙/Nginx/DNS，不分发root私钥、不增加Google/数据库权限。

可参考仓库已审阅的`deploy/query-service/sshd-match-at-eof.conf.example`。本包不包含自动安装、sudo规则或远程命令脚本。

试点前后验收：Windows OpenSSH与组织脚本策略、服务器指纹、正向六项查询、错误请求拒绝、只能转发8765且不能shell/SFTP/其他端口、query无源凭据、关闭和重连、按公钥撤销。
是否账号nologin/锁定状态适配当前PAM须实际验证，不要通过扩大权限“修好”。

所有试点用户将可读服务中已批准的聚合案例与版本；本阶段不做按案例RBAC。若需数据可见范围分隔，必须先确认，不能靠共享通道名假装隔离。

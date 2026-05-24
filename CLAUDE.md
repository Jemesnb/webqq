# CLAUDE.md

QQ Web — Flask + SQLite 网页版 QQ 管理工具，通过 NapCat API 收发消息。

## 项目结构

- **`server.py`** — Flask 后端（单文件），运行在云 VPS 上，与 NapCat 同机
  - 代理 NapCat OneBot 11 接口：消息收发、通讯录、群管理、好友请求
  - SQLite（`qq_relay.db`）存储收到的消息和好友申请
  - HTTP Basic Auth，`/webhook` 和 `/api/proxy` 免认证
  - `napcat_api(path, method, payload)` — 通用 NapCat 代理函数
  - `init_db()` — 启动时自动建表/升级表结构
- **`templates/index.html`** — 单页 Web UI，侧边栏导航，所有 JS/CSS 内联
  - 页面：消息（收发）、通讯录（好友/群）、好友管理（申请处理）、群管理（成员/禁言）
  - 3 秒轮询新消息
  - CQ 码图片解析，通过 `/api/proxy` 代理显示
  - 支持 Ctrl+V 粘贴图片发送
- **`upload.py`** — 部署脚本，用 paramiko SFTP 上传 `server.py` + `index.html` 到远程 VPS

## 部署

```bash
python upload.py
```
然后 SSH 登录 VPS 重启服务：
```bash
pkill -f main.py && cd /root/esp32s3 && nohup python3 main.py > server.log 2>&1 &
tail -f server.log
```
注意：本地 `server.py` 上传到远端自动改名为 `main.py`。

## 关键配置（server.py 第 18-22 行）

```python
NAPCAT_API = "http://127.0.0.1:5700"
NAPCAT_TOKEN = "11"
HTTP_USER = "dengjiewei"
HTTP_PASS = "dhd781102"
```

## 数据库表

- `received_messages`: id, msg_type, sender_id, sender_name, group_id, group_name, message, raw_json, created_at
- `friend_requests`: id, user_id, nickname, comment, flag, status, created_at
- 启用 WAL 模式

## 常用 NapCat 接口

| 接口 | 用途 |
|---|---|
| `send_private_msg` / `send_group_msg` | 发消息 |
| `get_login_info` | 获取当前 QQ 身份 |
| `get_friend_list` / `get_group_list` | 通讯录 |
| `get_group_member_list` | 群成员 + 角色检测 |
| `set_group_ban` / `set_group_whole_ban` | 群禁言 |
| `set_friend_add_request` / `set_doubt_friends_add_request` | 处理好友申请 |
| `get_doubt_friends_add_request` | 获取可疑好友申请列表 |
| `delete_friend` | 删除好友 |
| `get_image` | 图片代理 |

## 注意事项

- 图片消息使用 CQ 码格式 `[CQ:image,file=xxx]`，通过 `/api/proxy?file=xxx` 代理显示
- 群成员接口返回 `my_role` 字段（owner/admin/member），前端根据角色控制权限
- 禁言时间单位是秒（NapCat 格式），前端输入分钟自动转秒

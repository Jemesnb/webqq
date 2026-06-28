# QQ Web

基于 NapCat API 的 QQ Web 管理面板，通过浏览器收发消息、管理好友和群组。

## 功能

- **消息** — 收发私聊/群聊消息，支持文字 + 图片（Ctrl+V 粘贴发送）
- **通讯录** — 查看好友列表和群列表
- **好友管理** — 处理好友申请（批准/拒绝），删除好友
- **群管理** — 查看群成员及角色，单人或全员禁言
- **图片代理** — CQ 码图片自动解析并代理显示

## 技术栈

- **后端** — Flask + SQLite，单文件 `server.py`
- **前端** — 单页 Web UI，所有 JS/CSS 内联在 `templates/index.html`
- **API** — 代理 NapCat OneBot 11 接口

## 快速开始

1. 安装依赖

```bash
pip install flask python-dotenv
```

2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 NapCat API 地址和 Token
```

3. 启动

```bash
python server.py
```

4. 浏览器打开 `http://你的IP:5000`，输入配置的用户名密码登录

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `NAPCAT_API` | NapCat HTTP 地址 | `（空）` |
| `NAPCAT_TOKEN` | NapCat API Token | `（空）` |
| `HTTP_USER` | Web 登录用户名 | `（空）` |
| `HTTP_PASS` | Web 登录密码 | `（空）` |
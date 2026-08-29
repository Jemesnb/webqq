"""
QQ Web - Flask + SQLite
Web UI send/receive QQ msgs via NapCat API (private + group)
"""
import json
import sqlite3
import os
import re
import base64
import shutil
import tempfile
from datetime import datetime
from urllib import request as urllib_request, error as urllib_error
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, Response

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 群文件上传上限
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "qq_relay.db"))

NAPCAT_API = os.environ.get("NAPCAT_API")
NAPCAT_TOKEN = os.environ.get("NAPCAT_TOKEN")

HTTP_USER = os.environ.get("HTTP_USER", "")
HTTP_PASS = os.environ.get("HTTP_PASS", "")

if not NAPCAT_API or not NAPCAT_TOKEN or not HTTP_USER or not HTTP_PASS:
    print("检查环境变量文件是否重命名为 .env，并复制到同目录下")
    print("或检查 NAPCAT_API, NAPCAT_TOKEN, HTTP_USER, HTTP_PASS 是否已经填写")


def napcat_api(path, method="GET", payload=None):
    """Proxy request to NapCat API"""
    url = NAPCAT_API.rstrip("/") + "/" + path.lstrip("/")
    headers = {}
    if NAPCAT_TOKEN:
        headers["Authorization"] = "Bearer " + NAPCAT_TOKEN

    try:
        if payload:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib_request.Request(url, data=data, headers=headers, method=method)
        else:
            req = urllib_request.Request(url, headers=headers, method=method)
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib_error.HTTPError as e:
        return None, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, str(e)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS received_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_type TEXT NOT NULL DEFAULT 'private',
            sender_id INTEGER,
            sender_name TEXT,
            group_id INTEGER,
            group_name TEXT,
            message TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS friend_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nickname TEXT,
            comment TEXT,
            flag TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)
    # Add columns if missing (upgrade old DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(received_messages)").fetchall()]
    if "sender_name" not in cols:
        conn.execute("ALTER TABLE received_messages ADD COLUMN sender_name TEXT")
    if "group_name" not in cols:
        conn.execute("ALTER TABLE received_messages ADD COLUMN group_name TEXT")
    if "self_sent" not in cols:
        conn.execute("ALTER TABLE received_messages ADD COLUMN self_sent INTEGER DEFAULT 0")
    if "target_id" not in cols:
        conn.execute("ALTER TABLE received_messages ADD COLUMN target_id INTEGER")
    if "msg_id" not in cols:
        conn.execute("ALTER TABLE received_messages ADD COLUMN msg_id INTEGER")
    # msg_id 唯一化：先清历史重复行（同 msg_id 保留最小 rowid），再建部分唯一索引
    # 根治 webhook 重推/双格式/自发消息双写导致的重复消息
    try:
        conn.execute(
            "DELETE FROM received_messages WHERE msg_id IS NOT NULL AND id NOT IN "
            "(SELECT MIN(id) FROM received_messages WHERE msg_id IS NOT NULL GROUP BY msg_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_msgid ON received_messages(msg_id) WHERE msg_id IS NOT NULL"
        )
    except Exception as db_err:
        print("WARN: msg_id unique index setup failed: {}".format(db_err))
    conn.commit()
    conn.close()


init_db()


def check_auth(user, password):
    return user == HTTP_USER and password == HTTP_PASS


def authenticate():
    return jsonify({"status": "error", "msg": "unauthorized"}), 401, {
        "WWW-Authenticate": 'Basic realm="QQ Web"'
    }


@app.before_request
def require_auth():
    if request.path in ("/webhook", "/api/proxy"):
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return authenticate()
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except Exception:
        return authenticate()
    if not check_auth(user, password):
        return authenticate()


@app.errorhandler(413)
def request_too_large(e):
    return jsonify({"status": "error", "msg": "文件超过上传大小限制(200MB)"}), 413


def _extract_reply(rid):
    """调 NapCat get_msg 获取被引用消息的文本和发送者，失败返回 ("", "")"""
    if not rid:
        return "", ""
    try:
        res, err = napcat_api("get_msg", method="POST", payload={"message_id": str(rid)})
        if err or not isinstance(res, dict):
            return "", ""
        d = res.get("data") or {}
        if not d:
            return "", ""
        # 从结构化 message 数组提取纯文本（处理 text/at/image）
        msg = d.get("message")
        text = ""
        if isinstance(msg, list):
            txts = []
            for s in msg:
                if not isinstance(s, dict):
                    continue
                if s.get("type") == "text":
                    txts.append(s.get("data", {}).get("text", ""))
                elif s.get("type") == "at":
                    qq = s.get("data", {}).get("qq", "")
                    txts.append("@" + ("所有人" if qq == "all" else qq))
                elif s.get("type") == "image":
                    txts.append("[图片]")
            text = "".join(txts).strip()
        if not text:
            text = str(d.get("raw_message") or "").strip()
        text = text[:120]
        sndr = d.get("sender") or {}
        sender = sndr.get("card") or sndr.get("nickname") or ""
        return text, sender
    except Exception as e:
        print("get_msg fail:", e)
        return "", ""


def parse_message_to_cq(data):
    """把 NapCat 结构化 message 数组转成增强 CQ 码字符串。

    - at: [CQ:at,qq=xxx]
    - reply: [CQ:reply,id=xxx,text=被回复内容,sender=发送者]
      优先从 raw.records 取，否则主动调 NapCat get_msg 补全
    - image/face: 原样 CQ 码
    """
    msg_array = data.get("message")
    if not isinstance(msg_array, list):
        return data.get("raw_message") or ""
    # 提取被回复消息的文本和发送者（NapCat 部分配置会带 raw.records）
    reply_text = ""
    reply_sender = ""
    raw = data.get("raw") or {}
    records = raw.get("records") or []
    if records:
        r0 = records[0] or {}
        for el in (r0.get("elements") or []):
            te = el.get("textElement") or {}
            if te.get("content"):
                reply_text = te["content"]
                break
        reply_sender = r0.get("sendNickName") or r0.get("sendMemberName") or ""
    out = []
    for seg in msg_array:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            out.append(d.get("text", ""))
        elif t == "at":
            qq = d.get("qq", "")
            out.append("[CQ:at,qq={}]".format(qq if qq else "all"))
        elif t == "reply":
            rid = d.get("id", "")
            text_extra = reply_text
            sender_extra = reply_sender
            # 如果 raw.records 里没有（如多数 webhook 配置），主动调 get_msg 补全
            if not text_extra and not sender_extra:
                text_extra, sender_extra = _extract_reply(rid)
            extra = ""
            if text_extra:
                extra += ",text=" + text_extra.replace(",", "，").replace("]", "】")
            if sender_extra:
                extra += ",sender=" + sender_extra.replace(",", "，").replace("]", "】")
            out.append("[CQ:reply,id={}{}]".format(rid, extra))
        elif t == "image":
            f = d.get("file", "")
            if f:
                out.append("[CQ:image,file={}]".format(f))
        elif t == "face":
            fid = d.get("id", "")
            if fid:
                out.append("[CQ:face,id={}]".format(fid))
        else:
            if d.get("text"):
                out.append(d["text"])
    return "".join(out)


def migrate_reply_details():
    """历史数据迁移：对没有 text/sender 的 reply 消息，调 NapCat get_msg 补全"""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, message FROM received_messages WHERE message LIKE '%[CQ:reply%' AND message NOT LIKE '%,text=%'"
        ).fetchall()
        conn.close()
        if not rows:
            print("MIGRATE: no legacy replies to enrich")
            return
        print("MIGRATE: enriching", len(rows), "legacy reply messages...")
        n = 0
        for rid, msg in rows:
            m = re.match(r'\[CQ:reply,id=([^,\]]+)', msg)
            if not m:
                continue
            target_msg_id = m.group(1)
            text, sender = _extract_reply(target_msg_id)
            if not text and not sender:
                continue
            extra = ""
            if text:
                extra += ",text=" + text.replace(",", "，").replace("]", "】")
            if sender:
                extra += ",sender=" + sender.replace(",", "，").replace("]", "】")
            new_msg = msg.replace(
                "[CQ:reply,id=" + target_msg_id + "]",
                "[CQ:reply,id=" + target_msg_id + extra + "]",
                1
            )
            if new_msg != msg:
                c2 = get_db()
                c2.execute("UPDATE received_messages SET message=? WHERE id=?", (new_msg, rid))
                c2.commit()
                c2.close()
                n += 1
        print("MIGRATE: enriched", n, "messages")
    except Exception as e:
        print("MIGRATE: failed", e)


migrate_reply_details()


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "msg": "no json"}), 400

    post_type = data.get("post_type")
    if post_type in ("message_event", "message"):
        msg_type = data.get("message_type")
        if msg_type in ("private", "group"):
            sender_id = data.get("sender", {}).get("user_id")
            sender_name = data.get("sender", {}).get("nickname", "")
            group_id = data.get("group_id") if msg_type == "group" else None
            group_name = data.get("group_name", "") if msg_type == "group" else None
            # 机器人自己发的消息事件（群聊会推送）也标记 self_sent，并归属到正确会话
            self_id = data.get("self_id")
            is_self = sender_id is not None and sender_id == self_id
            target_id = None
            if is_self and msg_type == "private":
                user_id = data.get("user_id")
                target_id = user_id if user_id != self_id else None
            message = parse_message_to_cq(data)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db()
            conn.execute(
                "INSERT OR IGNORE INTO received_messages (msg_type, sender_id, sender_name, group_id, group_name, message, raw_json, created_at, self_sent, target_id, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_type, sender_id, sender_name, group_id, group_name, str(message), json.dumps(data, ensure_ascii=False), now, 1 if is_self else 0, target_id, data.get("message_id")),
            )
            conn.commit()
            conn.close()
            return jsonify({"status": "ok"})

    if post_type == "request":
        req_type = data.get("request_type")
        if req_type == "friend":
            user_id = data.get("user_id")
            comment = data.get("comment", "")
            flag = data.get("flag", "")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db()
            existing = conn.execute("SELECT id FROM friend_requests WHERE flag=?", (flag,)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO friend_requests (user_id, nickname, comment, flag, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                    (user_id, str(user_id), comment, flag, now),
                )
                conn.commit()
            conn.close()
            return jsonify({"status": "ok"})

    return jsonify({"status": "ignored"})


@app.route("/api/send", methods=["POST"])
def send():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "msg": "no data"}), 400

    msg_type = data.get("msg_type", "private")
    message = data.get("message", "")
    image_b64 = data.get("image_base64", "")
    image_url = data.get("image_url", "")
    reply_to = data.get("reply_to")          # 被引用消息的 NapCat message_id
    reply_to_text = data.get("reply_to_text", "")
    reply_to_sender = data.get("reply_to_sender", "")

    # Build CQ code for image: base64 优先，否则使用 URL（收藏表情等）
    image_cq = ""
    if image_b64:
        image_cq = "[CQ:image,file=base64://{}]".format(image_b64)
    elif image_url:
        image_cq = "[CQ:image,file={}]".format(image_url)
    if message and image_cq:
        full_message = message + " " + image_cq
    elif image_cq:
        full_message = image_cq
    else:
        full_message = message

    if not full_message:
        return jsonify({"status": "error", "msg": "empty message"}), 400

    # 引用：发给 NapCat 用纯 [CQ:reply,id=xxx]；存库用增强格式（带 text/sender 供前端显示）
    napcat_message = full_message
    if reply_to:
        napcat_message = "[CQ:reply,id={}]".format(reply_to) + napcat_message
        extra = ""
        if reply_to_text:
            extra += ",text=" + reply_to_text.replace(",", "，").replace("]", "】")
        if reply_to_sender:
            extra += ",sender=" + reply_to_sender.replace(",", "，").replace("]", "】")
        full_message = "[CQ:reply,id={}{}]".format(reply_to, extra) + full_message

    if msg_type == "group":
        target_id = data.get("group_id")
        if not target_id:
            return jsonify({"status": "error", "msg": "missing group_id"}), 400
        url = NAPCAT_API.rstrip("/") + "/send_group_msg"
        payload = {"group_id": target_id, "message": napcat_message}
        print("SEND group {}: {} img={}".format(target_id, napcat_message[:60], bool(image_b64)))
    else:
        target_id = data.get("target_id")
        if not target_id:
            return jsonify({"status": "error", "msg": "missing target_id"}), 400
        url = NAPCAT_API.rstrip("/") + "/send_private_msg"
        payload = {"user_id": target_id, "message": napcat_message}
        print("SEND private {}: {} img={}".format(target_id, napcat_message[:60], bool(image_b64)))

    headers = {"Content-Type": "application/json"}
    if NAPCAT_TOKEN:
        headers["Authorization"] = "Bearer " + NAPCAT_TOKEN
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            # 发送成功后，也将消息写入 received_messages 表（这样轮询能拿到自己发的消息）
            nap_data = result.get("data") if isinstance(result, dict) else None
            saved_msg_id = nap_data.get("message_id") if isinstance(nap_data, dict) else None
            row_id, row_time = None, None
            try:
                my_info_res, _ = napcat_api("get_login_info", method="POST", payload={})
                my_data = (my_info_res.get("data") if isinstance(my_info_res, dict) else my_info_res) or {}
                my_uid = my_data.get("user_id")
                my_nick = my_data.get("nickname", "我")
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn = get_db()
                cur = conn.execute(
                    "INSERT OR IGNORE INTO received_messages (msg_type, sender_id, sender_name, group_id, group_name, message, raw_json, created_at, self_sent, target_id, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (msg_type, my_uid, my_nick,
                     target_id if msg_type == "group" else None,
                     None,  # 群名暂不填
                     full_message,
                     json.dumps({"self_sent": True, "message": full_message}, ensure_ascii=False),
                     now,
                     target_id if msg_type == "private" else None,
                     saved_msg_id),
                )
                conn.commit()
                if cur.rowcount == 0 and saved_msg_id:
                    # 同 msg_id 已由 webhook 落库：沿用已有行，保证前端乐观副本与落库行 id 一致
                    row = conn.execute(
                        "SELECT id, created_at FROM received_messages WHERE msg_id=?", (saved_msg_id,)
                    ).fetchone()
                    row_id, row_time = (row[0], row[1]) if row else (cur.lastrowid, now)
                else:
                    row_id, row_time = cur.lastrowid, now
                conn.close()
            except Exception as db_err:
                print("WARN: failed to save sent message to DB: {}".format(db_err))
            return jsonify({"status": "ok", "napcat_response": result, "id": row_id,
                            "time": row_time, "msg_id": saved_msg_id})
    except urllib_error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return jsonify({"status": "error", "msg": body}), 502
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 502


@app.route("/api/me")
def me():
    result, err = napcat_api("get_login_info", method="POST", payload={})
    if err:
        result, err = napcat_api("get_login_info")
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    return jsonify({"status": "ok", "data": data or {}})


@app.route("/api/friends")
def friends():
    result, err = napcat_api("get_friend_list", method="POST", payload={})
    if err:
        # fallback to GET
        result, err = napcat_api("get_friend_list")
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    return jsonify({"status": "ok", "data": data or []})


def get_login_user_id():
    """获取当前登录 QQ 的 user_id"""
    result, _ = napcat_api("get_login_info", method="POST", payload={})
    if not result:
        result, _ = napcat_api("get_login_info")
    data = (result or {}).get("data") if isinstance(result, dict) else result
    return data.get("user_id") if isinstance(data, dict) else None


def get_my_group_role(group_id):
    """获取机器人自己在群内的角色（owner/admin/member）"""
    my_id = get_login_user_id()
    if not my_id:
        return "member"
    result, err = napcat_api("get_group_member_list", method="POST", payload={"group_id": group_id})
    if err:
        return "member"
    data = result.get("data") if isinstance(result, dict) else result
    for m in data or []:
        if isinstance(m, dict) and m.get("user_id") == my_id:
            return m.get("role", "member")
    return "member"


@app.route("/api/groups")
def groups():
    result, err = napcat_api("get_group_list", method="POST", payload={})
    if err:
        result, err = napcat_api("get_group_list")
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    return jsonify({"status": "ok", "data": data or []})


@app.route("/api/group/members")
def group_members():
    group_id = request.args.get("group_id", type=int)
    if not group_id:
        return jsonify({"status": "error", "msg": "missing group_id"}), 400
    result, err = napcat_api("get_group_member_list", method="POST", payload={"group_id": group_id})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result

    # Get current user's role in this group
    my_role = "member"
    my_id = get_login_user_id()
    if my_id and isinstance(data, list):
        for m in data:
            if isinstance(m, dict) and m.get("user_id") == my_id:
                my_role = m.get("role", "member")
                break

    return jsonify({"status": "ok", "data": data or [], "my_role": my_role})


@app.route("/api/group/ban", methods=["POST"])
def group_ban():
    data = request.get_json(silent=True)
    if not data or "group_id" not in data or "user_id" not in data:
        return jsonify({"status": "error", "msg": "missing params"}), 400
    duration = data.get("duration", 60)
    result, err = napcat_api("set_group_ban", method="POST", payload={
        "group_id": data["group_id"], "user_id": data["user_id"], "duration": duration
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/whole_ban", methods=["POST"])
def group_whole_ban():
    data = request.get_json(silent=True)
    if not data or "group_id" not in data:
        return jsonify({"status": "error", "msg": "missing group_id"}), 400
    enable = data.get("enable", True)
    result, err = napcat_api("set_group_whole_ban", method="POST", payload={
        "group_id": data["group_id"], "enable": enable
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/notices")
def group_notices():
    """获取群公告列表（代理 NapCat _get_group_notice）"""
    group_id = request.args.get("group_id")
    if not group_id:
        return jsonify({"status": "error", "msg": "missing group_id"}), 400
    result, err = napcat_api("_get_group_notice", method="POST", payload={"group_id": str(group_id)})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    return jsonify({"status": "ok", "data": data or []})


@app.route("/api/group/files")
def group_files():
    """获取群文件列表（根目录走 get_group_root_files，子目录走 get_group_files_by_folder）

    实测这版 NapCat 的 get_group_files_by_folder 在根目录(/)不返回 folders，
    必须用 get_group_root_files；子目录 by_folder 正常。子目录 folder_id 自带 / 前缀。
    """
    group_id = request.args.get("group_id")
    folder_id = request.args.get("folder_id", "/")
    if not group_id:
        return jsonify({"status": "error", "msg": "missing group_id"}), 400
    if folder_id == "/":
        action = "get_group_root_files"
        payload = {"group_id": str(group_id), "file_count": 200}
    else:
        action = "get_group_files_by_folder"
        payload = {"group_id": str(group_id), "folder_id": folder_id, "file_count": 200}
    result, err = napcat_api(action, method="POST", payload=payload)
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    if not isinstance(data, dict):
        data = {"files": [], "folders": []}
    resp = {"status": "ok", "data": data}
    # with_role=1 时附带机器人角色，用于前端控制文件管理按钮权限（避免每次翻目录都查角色）
    if request.args.get("with_role"):
        resp["my_role"] = get_my_group_role(group_id)
    return jsonify(resp)


@app.route("/api/group/file_url")
def group_file_url():
    """获取群文件下载链接（代理 NapCat get_group_file_url）"""
    group_id = request.args.get("group_id")
    file_id = request.args.get("file_id")
    busid = request.args.get("busid", 102)
    if not group_id or not file_id:
        return jsonify({"status": "error", "msg": "missing params"}), 400
    result, err = napcat_api("get_group_file_url", method="POST", payload={
        "group_id": str(group_id), "file_id": file_id, "busid": int(busid)
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        return jsonify({"status": "error", "msg": "no url"}), 502
    return jsonify({"status": "ok", "url": url})


@app.route("/api/group/file_download")
def group_file_download():
    """代理下载群文件（流式转发 QQ CDN 内容）

    QQ CDN 不带 Content-Disposition，浏览器直接打开会存成无后缀的哈希名，
    所以由服务器转发并强制 attachment + 列表里的原始文件名
    """
    group_id = request.args.get("group_id")
    file_id = request.args.get("file_id")
    busid = request.args.get("busid", 102)
    name = request.args.get("name", "file")
    if not group_id or not file_id:
        return jsonify({"status": "error", "msg": "missing params"}), 400
    name = re.sub(r'[\r\n"\x00-\x1f]', "", name).strip() or "file"
    result, err = napcat_api("get_group_file_url", method="POST", payload={
        "group_id": str(group_id), "file_id": file_id, "busid": int(busid)
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        return jsonify({"status": "error", "msg": "no url"}), 502
    try:
        upstream = urllib_request.urlopen(urllib_request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://qun.qq.com/"}), timeout=30)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 502

    def stream():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
    }
    length = upstream.headers.get("Content-Length")
    if length:
        headers["Content-Length"] = length
    return Response(stream(), headers=headers)


@app.route("/api/group/file/upload", methods=["POST"])
def group_file_upload():
    """上传群文件（multipart：file + group_id + folder_id）

    Flask 与 NapCat 同机：先落盘临时文件，再把本地路径交给 NapCat 上传
    """
    group_id = request.form.get("group_id")
    folder_id = request.form.get("folder_id", "/")
    f = request.files.get("file")
    if not group_id or not f or not f.filename:
        return jsonify({"status": "error", "msg": "missing params"}), 400
    fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(f.filename)[1])
    try:
        with os.fdopen(fd, "wb") as tmp:
            shutil.copyfileobj(f.stream, tmp)
        payload = {"group_id": str(group_id), "file": "file://" + tmp_path, "name": f.filename}
        if folder_id and folder_id != "/":
            payload["folder"] = folder_id
        result, err = napcat_api("upload_group_file", method="POST", payload=payload)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/file/delete", methods=["POST"])
def group_file_delete():
    data = request.get_json(silent=True)
    if not data or "group_id" not in data or "file_id" not in data:
        return jsonify({"status": "error", "msg": "missing params"}), 400
    result, err = napcat_api("delete_group_file", method="POST", payload={
        "group_id": str(data["group_id"]), "file_id": data["file_id"]
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/file/rename", methods=["POST"])
def group_file_rename():
    data = request.get_json(silent=True)
    if not data or not all(k in data for k in ("group_id", "file_id", "folder_id", "new_name")):
        return jsonify({"status": "error", "msg": "missing params"}), 400
    result, err = napcat_api("rename_group_file", method="POST", payload={
        "group_id": str(data["group_id"]), "file_id": data["file_id"],
        "current_parent_directory": data["folder_id"], "new_name": data["new_name"]
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/file/move", methods=["POST"])
def group_file_move():
    data = request.get_json(silent=True)
    if not data or not all(k in data for k in ("group_id", "file_id", "current_folder_id", "target_folder_id")):
        return jsonify({"status": "error", "msg": "missing params"}), 400
    result, err = napcat_api("move_group_file", method="POST", payload={
        "group_id": str(data["group_id"]), "file_id": data["file_id"],
        "current_parent_directory": data["current_folder_id"],
        "target_parent_directory": data["target_folder_id"]
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/group/folder/create", methods=["POST"])
def group_folder_create():
    data = request.get_json(silent=True)
    if not data or "group_id" not in data or not data.get("name"):
        return jsonify({"status": "error", "msg": "missing params"}), 400
    result, err = napcat_api("create_group_file_folder", method="POST", payload={
        "group_id": str(data["group_id"]), "folder_name": data["name"]
    })
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/friend_requests/doubt")
def list_doubt_friend_requests():
    result, err = napcat_api("get_doubt_friends_add_request", method="POST", payload={})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    return jsonify({"status": "ok", "data": data or []})

@app.route("/api/friend_requests")
def list_friend_requests():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, user_id, nickname, comment, status, created_at FROM friend_requests WHERE status='pending' AND id IN (SELECT MIN(id) FROM friend_requests WHERE status='pending' GROUP BY flag) ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify({"status": "ok", "data": [
        {"id": r["id"], "user_id": r["user_id"], "nickname": r["nickname"],
         "comment": r["comment"], "status": r["status"], "time": r["created_at"]}
        for r in rows
    ]})


@app.route("/api/friend_request/handle", methods=["POST"])
def handle_friend_request():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "msg": "missing data"}), 400

    # Support both id and flag
    flag = data.get("flag", "")
    req_id = data.get("id")
    is_doubt = data.get("doubt", False)

    if not flag and not req_id:
        return jsonify({"status": "error", "msg": "missing flag or id"}), 400

    if not flag and req_id:
        conn = get_db()
        row = conn.execute("SELECT flag FROM friend_requests WHERE id=?", (req_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"status": "error", "msg": "request not found"}), 404
        flag = row["flag"]

    approve = data.get("approve", True)
    remark = data.get("remark", "")

    if is_doubt:
        result, err = napcat_api("set_doubt_friends_add_request", method="POST", payload={
            "flag": flag, "approve": approve
        })
    else:
        result, err = napcat_api("set_friend_add_request", method="POST", payload={
            "flag": flag, "approve": approve, "remark": remark
        })

    if err:
        return jsonify({"status": "error", "msg": err}), 502
    # Update local DB
    conn = get_db()
    conn.execute("UPDATE friend_requests SET status=? WHERE flag=?", ("approved" if approve else "rejected", flag))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/friend/delete", methods=["POST"])
def delete_friend():
    data = request.get_json(silent=True)
    if not data or "user_id" not in data:
        return jsonify({"status": "error", "msg": "missing user_id"}), 400
    user_id = data["user_id"]
    result, err = napcat_api("delete_friend", method="POST", payload={"user_id": user_id})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    return jsonify({"status": "ok"})


@app.route("/api/custom_faces")
def custom_faces():
    """获取用户收藏的自定义表情（代理 NapCat fetch_custom_face）"""
    count = request.args.get("count", 20, type=int)
    if count > 200:
        count = 200
    result, err = napcat_api("fetch_custom_face", method="POST", payload={"count": count})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    faces = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item:
                faces.append({"id": "", "url": item})
            elif isinstance(item, dict):
                url = item.get("url") or item.get("face_url") or item.get("qurl") or ""
                fid = item.get("id") or item.get("face_id") or ""
                if url:
                    faces.append({"id": fid, "url": url})
    return jsonify({"status": "ok", "data": faces})


@app.route("/api/proxy")
def proxy():
    file_id = request.args.get("file")
    if not file_id:
        return jsonify({"status": "error", "msg": "missing file"}), 400
    # Use NapCat get_image API to get the actual image URL
    result, err = napcat_api("get_image", method="POST", payload={"file": file_id})
    if err:
        return jsonify({"status": "error", "msg": err}), 502
    data = result.get("data") if isinstance(result, dict) else result
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        return jsonify({"status": "error", "msg": "no image url"}), 502
    # Proxy from NapCat's resolved URL
    try:
        req = urllib_request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        with urllib_request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        return Response(data, content_type=ctype)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 502


@app.route("/api/messages")
def get_messages():
    limit = request.args.get("limit", 50, type=int)
    msg_type = request.args.get("type")  # private / group，可选
    peer_id = request.args.get("peer_id", type=int)  # 对方 user_id 或 group_id，可选
    conn = get_db()
    if msg_type and peer_id:
        # 按会话过滤：私聊匹配 sender_id 或 target_id（对方发的/我发给对方的）
        if msg_type == "group":
            rows = conn.execute(
                "SELECT id, msg_type, sender_id, sender_name, group_id, group_name, message, created_at, self_sent, target_id, msg_id FROM received_messages WHERE msg_type='group' AND group_id=? ORDER BY id DESC LIMIT ?",
                (peer_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, msg_type, sender_id, sender_name, group_id, group_name, message, created_at, self_sent, target_id, msg_id FROM received_messages WHERE msg_type='private' AND (sender_id=? OR target_id=?) ORDER BY id DESC LIMIT ?",
                (peer_id, peer_id, limit),
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, msg_type, sender_id, sender_name, group_id, group_name, message, created_at, self_sent, target_id, msg_id FROM received_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return jsonify([
        {
            "id": r["id"],
            "msg_type": r["msg_type"],
            "sender_id": r["sender_id"],
            "sender_name": r["sender_name"],
            "group_id": r["group_id"],
            "group_name": r["group_name"],
            "message": r["message"],
            "time": r["created_at"],
            "self_sent": bool(r["self_sent"]),
            "target_id": r["target_id"],
            "msg_id": r["msg_id"],
        }
        for r in rows
    ])


@app.route("/api/conversations", methods=["POST"])
def conversations_agg():
    """会话聚合：每个会话的末条消息 + 未读数（供前端会话列表动态排序）

    入参 JSON: {"seen": {"g:群号": 已读最大rowid, "p:QQ号": ...}}
    未读 = self_sent=0 且 id > 该会话已读游标 的条数；扫描窗口限定最近 1000 行
    （游标老于窗口的极端积压会少算，前端显示封顶 99+，可接受）
    """
    body = request.get_json(silent=True) or {}
    seen = body.get("seen") or {}
    g_seen, p_seen = {}, {}
    for k, v in seen.items():
        try:
            cursor = int(v)
        except Exception:
            continue
        if str(k).startswith("g:"):
            g_seen[int(str(k)[2:])] = cursor
        elif str(k).startswith("p:"):
            p_seen[int(str(k)[2:])] = cursor

    conn = get_db()
    out = []
    # ---- 群会话末条 ----
    rows = conn.execute(
        "SELECT group_id, id, self_sent, message, created_at FROM received_messages "
        "WHERE msg_type='group' AND id IN "
        "(SELECT MAX(id) FROM received_messages WHERE msg_type='group' GROUP BY group_id)"
    ).fetchall()
    for r in rows:
        out.append({"key": "g:%s" % r["group_id"], "type": "g", "id": r["group_id"],
                    "max_id": r["id"], "last_time": r["created_at"],
                    "last_message": r["message"], "last_mine": bool(r["self_sent"]),
                    "unread": 0})
    # ---- 私聊会话末条（自己发的按 target_id 归属对方）----
    rows = conn.execute(
        "SELECT CASE WHEN self_sent=1 THEN target_id ELSE sender_id END AS peer, "
        "id, self_sent, message, created_at FROM received_messages "
        "WHERE msg_type='private' AND id IN "
        "(SELECT MAX(id) FROM received_messages WHERE msg_type='private' "
        "GROUP BY CASE WHEN self_sent=1 THEN target_id ELSE sender_id END)"
    ).fetchall()
    for r in rows:
        if r["peer"] is None:
            continue
        out.append({"key": "p:%s" % r["peer"], "type": "p", "id": r["peer"],
                    "max_id": r["id"], "last_time": r["created_at"],
                    "last_message": r["message"], "last_mine": bool(r["self_sent"]),
                    "unread": 0})

    # ---- 未读数：增量窗口扫描，按各会话已读游标精确计数 ----
    all_cursors = [c for c in list(g_seen.values()) + list(p_seen.values()) if c > 0]
    if all_cursors:
        global_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM received_messages").fetchone()[0]
        floor = max(min(all_cursors), global_max - 1000)
        rows = conn.execute(
            "SELECT id, msg_type, group_id, sender_id, target_id FROM received_messages "
            "WHERE self_sent=0 AND id > ? ORDER BY id", (floor,)
        ).fetchall()
        by_key = {}
        for r in rows:
            if r["msg_type"] == "group":
                key = "g:%s" % r["group_id"] if r["group_id"] else None
                cursor = g_seen.get(r["group_id"], 0)
            else:
                peer = r["target_id"] if r["target_id"] else r["sender_id"]
                key = "p:%s" % peer if peer else None
                cursor = p_seen.get(peer, 0)
            if key and r["id"] > cursor:
                by_key[key] = by_key.get(key, 0) + 1
        for item in out:
            item["unread"] = by_key.get(item["key"], 0)
    conn.close()
    return jsonify(out)


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    print("QQ Web Server starting...")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)

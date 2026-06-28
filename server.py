"""
QQ Web - Flask + SQLite
Web UI send/receive QQ msgs via NapCat API (private + group)
"""
import json
import sqlite3
import os
import base64
from datetime import datetime
from urllib import request as urllib_request, error as urllib_error

from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, Response

load_dotenv()
app = Flask(__name__)
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
            message = data.get("raw_message") or data.get("message")
            if isinstance(message, list):
                message = json.dumps(message, ensure_ascii=False)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_db()
            conn.execute(
                "INSERT INTO received_messages (msg_type, sender_id, sender_name, group_id, group_name, message, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_type, sender_id, sender_name, group_id, group_name, str(message), json.dumps(data, ensure_ascii=False), now),
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

    # Build CQ code for image if present
    image_cq = ""
    if image_b64:
        image_cq = "[CQ:image,file=base64://{}]".format(image_b64)
    if message and image_cq:
        full_message = message + image_cq
    elif image_cq:
        full_message = image_cq
    else:
        full_message = message

    if not full_message:
        return jsonify({"status": "error", "msg": "empty message"}), 400

    if msg_type == "group":
        target_id = data.get("group_id")
        if not target_id:
            return jsonify({"status": "error", "msg": "missing group_id"}), 400
        url = NAPCAT_API.rstrip("/") + "/send_group_msg"
        payload = {"group_id": target_id, "message": full_message}
        print("SEND group {}: {} img={}".format(target_id, full_message[:60], bool(image_b64)))
    else:
        target_id = data.get("target_id")
        if not target_id:
            return jsonify({"status": "error", "msg": "missing target_id"}), 400
        url = NAPCAT_API.rstrip("/") + "/send_private_msg"
        payload = {"user_id": target_id, "message": full_message}
        print("SEND private {}: {} img={}".format(target_id, full_message[:60], bool(image_b64)))

    headers = {"Content-Type": "application/json"}
    if NAPCAT_TOKEN:
        headers["Authorization"] = "Bearer " + NAPCAT_TOKEN
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        req = urllib_request.Request(url, data=body, headers=headers, method="POST")
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return jsonify({"status": "ok", "napcat_response": result})
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
    login_result, _ = napcat_api("get_login_info", method="POST", payload={})
    if not login_result:
        login_result, _ = napcat_api("get_login_info")
    ld = (login_result or {}).get("data") if isinstance(login_result, dict) else login_result
    my_id = ld.get("user_id") if isinstance(ld, dict) else None
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
    conn = get_db()
    rows = conn.execute(
        "SELECT id, msg_type, sender_id, sender_name, group_id, group_name, message, created_at FROM received_messages ORDER BY id DESC LIMIT ?",
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
        }
        for r in rows
    ])


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    print("QQ Web Server starting...")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "true").lower() == "true"
    app.run(host=host, port=port, debug=debug)

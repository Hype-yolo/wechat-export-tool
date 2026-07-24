#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信导出向导 —— 本地网页版的薄壳。

不碰引擎 wechat_export.py（已验证），只在它外面套一层：
把「解密 / 列联系人 / 导出」三步接成 HTTP 接口，前端 web/index.html 点按钮调用。

全程本地：服务只绑 127.0.0.1，和命令行版一样不联网、不外传。密码只用于本机解密。
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # 引擎 wechat_export.py 所在目录
sys.path.insert(0, str(ROOT))
import wechat_export as engine  # noqa: E402

PORT = 47653
# 中间产物（解出来的数据库）藏在用户目录下；成品导到桌面，用户一眼看到
WORK_DIR = Path.home() / ".wechat-export-gui"
DB_DIR = WORK_DIR / "wechat_db"
OUT_DIR = Path.home() / "Desktop" / "微信聊天记录导出"

_decrypt_lock = threading.Lock()


def _has_db():
    return DB_DIR.exists() and any(DB_DIR.rglob("WCDB_Contact.sqlite"))


def _list_contacts():
    """组装联系人 JSON，复用引擎底层函数，不走命令行打印。"""
    account = engine._pick_account(str(DB_DIR), "")
    names = engine._contact_names(account)
    counts = engine._count_chats(account)
    hash_to_wxid = {engine._table_for_wxid(w): w for w in names}
    rows = []
    for table, n in counts.items():
        wxid = hash_to_wxid.get(table)
        if not wxid:
            continue  # 反查不到 wxid 的（极少数），GUI 先不列
        name = names.get(wxid, wxid)
        rows.append({
            "wxid": wxid,
            "name": name,
            "count": n,
            "is_group": wxid.endswith("@chatroom"),
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def _do_decrypt(password, backup_dir):
    """调引擎 extract。引擎用 sys.exit 报错 → 这里 SystemExit 接住转成 JSON。"""
    args = SimpleNamespace(
        password=password,
        backup_dir=backup_dir or "",
        db_dir=str(DB_DIR),
    )
    try:
        engine.cmd_extract(args)
    except SystemExit as e:
        return {"ok": False, "error": str(e.code) if e.code else "解密失败"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def _do_export(wxids, fmt):
    account = engine._pick_account(str(DB_DIR), "")
    names = engine._contact_names(account)
    tables = engine._chat_tables(account)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(me_label="我")
    done, skipped = [], []
    for wxid in wxids:
        name = names.get(wxid, wxid)
        table = engine._table_for_wxid(wxid)
        db = tables.get(table)
        if not db:
            skipped.append(name)
            continue
        try:
            engine._export_one(db, table, name, wxid, OUT_DIR, args)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{name}（{e}）")
            continue
        safe = engine.re.sub(r'[\\/:*?"<>|]', "_", name) or wxid
        keep = f"{safe}.txt" if fmt == "txt" else f"{safe}.jsonl"
        # 两种格式引擎都写了；只保留用户选的那种，另一种删掉
        other = OUT_DIR / (f"{safe}.jsonl" if fmt == "txt" else f"{safe}.txt")
        if other.exists():
            other.unlink()
        done.append({"name": name, "file": keep})
    return {"ok": True, "out_dir": str(OUT_DIR), "done": done, "skipped": skipped}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音访问日志

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = (HERE / "web" / "index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path == "/api/ping":
            return self._send(200, {"ok": True})
        if self.path == "/api/state":
            return self._send(200, {"decrypted": _has_db()})
        if self.path == "/api/contacts":
            try:
                return self._send(200, {"ok": True, "contacts": _list_contacts()})
            except SystemExit as e:
                return self._send(200, {"ok": False, "error": str(e.code)})
            except Exception as e:  # noqa: BLE001
                return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/api/decrypt":
            if not _decrypt_lock.acquire(blocking=False):
                return self._send(200, {"ok": False, "error": "正在解密中，请稍候"})
            try:
                return self._send(200, _do_decrypt(body.get("password", ""), body.get("backup_dir", "")))
            finally:
                _decrypt_lock.release()
        if self.path == "/api/export":
            wxids = body.get("wxids", [])
            fmt = body.get("format", "txt")
            if not wxids:
                return self._send(200, {"ok": False, "error": "没选联系人"})
            try:
                return self._send(200, _do_export(wxids, fmt))
            except Exception as e:  # noqa: BLE001
                return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        if self.path == "/api/open-folder":
            if OUT_DIR.exists():
                subprocess.Popen(["open", str(OUT_DIR)])
                return self._send(200, {"ok": True})
            return self._send(200, {"ok": False, "error": "还没有导出结果"})
        if self.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (os._exit(0))).start()
            return
        return self._send(404, {"error": "not found"})


def main():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 已被占用，可能向导已在运行。")
        sys.exit(0)
    print(f"微信导出向导已启动：http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

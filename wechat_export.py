#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat-export-tool — 从 iPhone 加密备份里，把「你自己的」微信聊天记录导出成可读文本。

只处理你自己设备、你自己账号的数据。全程本地运行，备份密码只用于本机解密，绝不联网、绝不外传。

三步：
    1) extract  从加密备份解出微信数据库  →  ./wechat_db/
    2) list     列出所有联系人和消息条数（挑你要导的那个）
    3) export   把某个联系人（或全部）的聊天导成 .txt / .jsonl

用法：
    python wechat_export.py extract --password 你的备份加密密码
    python wechat_export.py list
    python wechat_export.py export --name 张三
    python wechat_export.py export --all

依赖：pip install -r requirements.txt   （核心是 iphone_backup_decrypt）
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 微信在 iPhone 备份里的域名
WECHAT_DOMAIN = "%tencent.xin%"
DEFAULT_DB_DIR = Path("./wechat_db")
DEFAULT_OUT_DIR = Path("./out")

# iPhone 默认备份位置（macOS）。Windows 见 README，用 --backup-dir 指定。
MAC_BACKUP_ROOT = Path.home() / "Library/Application Support/MobileSync/Backup"


# ----------------------------------------------------------------------------
# 第 1 步：从加密备份里把微信数据库解出来
# ----------------------------------------------------------------------------
def cmd_extract(args):
    try:
        from iphone_backup_decrypt import EncryptedBackup
    except ImportError:
        sys.exit("✗ 缺依赖：pip install -r requirements.txt")

    backup_dir = _pick_backup(args.backup_dir)
    print(f"● 备份目录：{backup_dir}")
    print("● 用密码解锁备份（密钥全程只在本机内存里）…")
    backup = EncryptedBackup(backup_directory=str(backup_dir), passphrase=args.password)
    try:
        backup.test_decryption()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"✗ 解密失败，多半是备份密码不对：{e}")
    print("✓ 密码正确，备份已解锁")

    cur = backup.manifest_db_cursor()
    cur.execute(
        "SELECT relativePath FROM Files WHERE domain LIKE ? AND relativePath != ''",
        (WECHAT_DOMAIN,),
    )
    paths = [r[0] for r in cur.fetchall()]
    if not paths:
        sys.exit("✗ 备份里没有微信数据。可能这台 iPhone 没登微信，或备份没把微信包含进去。")
    print(f"● 备份里有微信文件 {len(paths)} 个，正在导出数据库到 {DEFAULT_DB_DIR} …")

    out = Path(args.db_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 只导数据库文件就够解析了（图片/视频体积大，MVP 先不导；见 README 路线图）
    n = backup.extract_files(
        domain_like=WECHAT_DOMAIN,
        relative_paths_like="%.sqlite",
        output_folder=str(out),
        preserve_folders=True,
        domain_subfolders=True,
    )
    print(f"✓ 完成：{n} 个数据库已解密到 {out}")
    print("下一步：python wechat_export.py list")


def _pick_backup(explicit):
    if explicit:
        return Path(explicit)
    if not MAC_BACKUP_ROOT.exists():
        sys.exit(
            f"✗ 没找到默认备份目录 {MAC_BACKUP_ROOT}\n"
            "  先在「访达」给 iPhone 做一次「加密本地备份」，或用 --backup-dir 指定备份路径。"
        )
    cands = [p for p in MAC_BACKUP_ROOT.iterdir() if (p / "Manifest.plist").exists()]
    if not cands:
        sys.exit("✗ 备份目录是空的，还没做过备份。")
    newest = max(cands, key=lambda p: (p / "Manifest.plist").stat().st_mtime)
    if len(cands) > 1:
        print(f"⚠ 有 {len(cands)} 个备份，自动选最新：{newest.name}（要指定用 --backup-dir）")
    return newest


# ----------------------------------------------------------------------------
# 找到解出来的微信数据库目录（DB/ 下有 MM.sqlite、message_N.sqlite、WCDB_Contact.sqlite）
# ----------------------------------------------------------------------------
def _find_accounts(db_dir):
    db_dir = Path(db_dir)
    if not db_dir.exists():
        sys.exit(f"✗ 没找到 {db_dir}，先跑 extract。")
    # 一台手机可能登过多个微信号，每个号一个 Documents/<hash>/DB 目录
    accounts = []
    for contact_db in db_dir.rglob("WCDB_Contact.sqlite"):
        accounts.append(contact_db.parent)
    if not accounts:
        sys.exit(f"✗ {db_dir} 里没找到微信数据库（WCDB_Contact.sqlite）。extract 成功了吗？")
    return accounts


def _pick_account(db_dir, want):
    accounts = _find_accounts(db_dir)
    if want:
        for a in accounts:
            if want in str(a):
                return a
        sys.exit(f"✗ 没有匹配 --account {want} 的账号。")
    if len(accounts) == 1:
        return accounts[0]
    # 多个号：选消息最多的那个
    best, best_n = None, -1
    for a in accounts:
        n = sum(_count_chats(a).values())
        if n > best_n:
            best, best_n = a, n
    print(f"⚠ 检测到 {len(accounts)} 个微信号，自动选消息最多的（要指定用 --account）")
    return best


# ----------------------------------------------------------------------------
# 联系人名字：WCDB_Contact.Friend.dbContactRemark 是 protobuf，
# 里面 field1=昵称、field2=备注。写个最小 protobuf 读取器，不额外装库。
# ----------------------------------------------------------------------------
def _read_varint(b, i):
    shift = 0
    val = 0
    while i < len(b):
        byte = b[i]
        i += 1
        val |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return val, i
        shift += 7
    return None, i


def _pb_fields(blob):
    """粗解 protobuf：返回 {字段号: 第一个出现的 bytes 值}（只收长度定界字段）。"""
    out = {}
    i, n = 0, len(blob)
    while i < n:
        tag, i = _read_varint(blob, i)
        if tag is None:
            break
        field, wt = tag >> 3, tag & 7
        if wt == 0:  # varint
            _, i = _read_varint(blob, i)
        elif wt == 2:  # length-delimited（字符串/bytes）
            ln, i = _read_varint(blob, i)
            if ln is None:
                break
            out.setdefault(field, blob[i:i + ln])
            i += ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return out


def _contact_names(account_dir):
    """返回 {wxid: 显示名}，显示名 = 备注 > 昵称 > wxid。"""
    con = sqlite3.connect(account_dir / "WCDB_Contact.sqlite")
    names = {}
    try:
        rows = con.execute("SELECT userName, dbContactRemark FROM Friend").fetchall()
    except sqlite3.Error:
        rows = []
    con.close()
    for wxid, blob in rows:
        if not wxid:
            continue
        nick = remark = ""
        if blob:
            f = _pb_fields(blob)
            nick = _s(f.get(1, b""))
            remark = _s(f.get(2, b""))
        names[wxid] = remark or nick or wxid
    return names


def _s(b):
    try:
        return b.decode("utf-8", "ignore").strip()
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------------------
# 聊天表：Chat_<md5(wxid)>，分散在 MM.sqlite / message_N.sqlite 里
# ----------------------------------------------------------------------------
def _chat_tables(account_dir):
    """返回 {表名: 数据库文件路径}，只收 Chat_ 表（排除 ChatExt2_）。"""
    tables = {}
    for db in sorted(account_dir.glob("*.sqlite")):
        if db.name in ("WCDB_Contact.sqlite", "WCDB_OpLog.sqlite"):
            continue
        try:
            con = sqlite3.connect(db)
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Chat\\_%' ESCAPE '\\'"
            ).fetchall():
                tables.setdefault(name, db)
            con.close()
        except sqlite3.Error:
            continue
    return tables


def _count_chats(account_dir):
    """返回 {表名: 条数}。"""
    counts = {}
    for name, db in _chat_tables(account_dir).items():
        try:
            con = sqlite3.connect(db)
            counts[name] = con.execute(f"SELECT count(*) FROM '{name}'").fetchone()[0]
            con.close()
        except sqlite3.Error:
            counts[name] = 0
    return counts


def _table_for_wxid(wxid):
    return "Chat_" + hashlib.md5(wxid.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# 第 2 步：列联系人
# ----------------------------------------------------------------------------
def cmd_list(args):
    account = _pick_account(args.db_dir, args.account)
    names = _contact_names(account)
    counts = _count_chats(account)
    # 把 Chat 表反查回 wxid / 名字
    hash_to_wxid = {_table_for_wxid(w): w for w in names}
    rows = []
    for table, n in counts.items():
        wxid = hash_to_wxid.get(table)
        name = names.get(wxid, "（未知联系人）") if wxid else "（未知/群聊）"
        rows.append((n, name, wxid or table))
    rows.sort(reverse=True)
    print(f"账号：{account.parent.name}    联系人/会话共 {len(rows)} 个\n")
    print(f"{'消息数':>8}  {'名字':<24}  wxid")
    print("-" * 70)
    shown = rows if args.all_contacts else rows[: args.top]
    for n, name, wxid in shown:
        print(f"{n:>8}  {name:<24}  {wxid}")
    if not args.all_contacts and len(rows) > args.top:
        print(f"\n…只显示前 {args.top} 个。全部加 --all-contacts。")
    print("\n导出：python wechat_export.py export --name 名字   或   --all")


# ----------------------------------------------------------------------------
# 消息类型 → 可读文本
# ----------------------------------------------------------------------------
try:  # Python 3.14+ 标准库自带 zstd；更老的版本装 zstandard 包（见 requirements.txt）
    from compression.zstd import decompress as _zstd_decompress
except ImportError:
    try:
        import zstandard as _zstd

        def _zstd_decompress(b):
            return _zstd.ZstdDecompressor().decompressobj().decompress(b)
    except ImportError:
        _zstd_decompress = None


def _txt(v):
    """SQLite 有时把 Message 当 BLOB 返回 bytes，统一转成 str。"""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        if b[:4] == b"\x28\xb5\x2f\xfd":  # 新版微信把部分长消息 zstd 压缩后存库
            # 多数是 iOS 微信用私有字典压缩的（frame 里带 dictionary_id），
            # 字典没在备份里，标准 zstd 解不开——尽力解，解不开就给占位而不是吐乱码。
            if _zstd_decompress is not None:
                try:
                    return _zstd_decompress(b).decode("utf-8", "ignore")
                except Exception:
                    pass
            return "[未解码的长消息]"
        return b.decode("utf-8", "ignore")
    return v


def _decode(msg, mtype):
    msg = _txt(msg)
    if mtype == 1:
        return msg
    if mtype == 3:
        return "[图片]"
    if mtype == 34:
        return "[语音]"
    if mtype == 43:
        return "[视频]"
    if mtype == 47:
        return "[表情]"
    if mtype == 42:
        return "[名片]"
    if mtype == 48:
        return "[位置]"
    if mtype == 50:
        return "[音视频通话]"
    if mtype in (10000, 10002):
        return "[系统消息]"
    if mtype == 49:  # 链接 / 文件 / 小程序 / 引用 等，内容是 XML
        title = _xml(msg, "title")
        sub = _xml(msg, "type")
        kind = {"5": "链接", "6": "文件", "19": "合并转发", "33": "小程序",
                "36": "小程序", "57": "引用", "2000": "转账"}.get(sub, "应用消息")
        url = _xml(msg, "url")
        out = f"[{kind}] {title}".strip()
        if kind == "链接" and url:
            out += f" {url}"
        return out or "[应用消息]"
    return f"[类型{mtype}]"


def _xml(s, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", s, re.S)
    if not m:
        m = re.search(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", s, re.S)
    return (m.group(1).strip() if m else "")[:200]


def _clean_group_sender(msg):
    """群聊里收到的消息前面带 'wxid_xxx:\\n'，1v1 不带。去掉这个前缀。"""
    msg = _txt(msg)
    m = re.match(r"^([a-zA-Z0-9_\-@.]{4,}):\n", msg)
    if m:
        return msg[m.end():], m.group(1)
    return msg, None


# ----------------------------------------------------------------------------
# 第 3 步：导出
# ----------------------------------------------------------------------------
def cmd_export(args):
    account = _pick_account(args.db_dir, args.account)
    names = _contact_names(account)
    tables = _chat_tables(account)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 决定导谁
    targets = []  # (wxid, 名字)
    if args.all:
        counts = _count_chats(account)
        hash_to_wxid = {_table_for_wxid(w): w for w in names}
        for table, n in counts.items():
            if n < args.min_msgs:
                continue
            wxid = hash_to_wxid.get(table)
            if wxid:
                targets.append((wxid, names.get(wxid, wxid)))
    elif args.wxid:
        targets = [(args.wxid, names.get(args.wxid, args.wxid))]
    elif args.name:
        for wxid, name in names.items():
            if args.name in name:
                targets.append((wxid, name))
        if not targets:
            sys.exit(f"✗ 没有名字包含「{args.name}」的联系人。先跑 list 看看。")
    else:
        sys.exit("✗ 指定导谁：--name 名字 / --wxid xxx / --all")

    for wxid, name in targets:
        table = _table_for_wxid(wxid)
        db = tables.get(table)
        if not db:
            print(f"⚠ 跳过「{name}」：找不到聊天表（可能没有聊天记录）")
            continue
        _export_one(db, table, name, wxid, out_dir, args)


def _export_one(db, table, name, wxid, out_dir, args):
    con = sqlite3.connect(db)
    rows = con.execute(
        f"SELECT CreateTime, Des, Type, Message FROM '{table}' ORDER BY CreateTime"
    ).fetchall()
    con.close()

    safe = re.sub(r'[\\/:*?"<>|]', "_", name) or wxid
    txt_path = out_dir / f"{safe}.txt"
    jsonl_path = out_dir / f"{safe}.jsonl"

    n = 0
    with open(txt_path, "w", encoding="utf-8") as ftxt, \
         open(jsonl_path, "w", encoding="utf-8") as fj:
        ftxt.write(f"# 和 {name}（{wxid}）的聊天记录  共 {len(rows)} 条\n\n")
        for create_time, des, mtype, message in rows:
            body = message or ""
            if des == 1:  # 收到的
                body, _ = _clean_group_sender(body)
            text = _decode(body, mtype)
            sender = args.me_label if des == 0 else name
            ts = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M")
            ftxt.write(f"[{ts}] {sender}：{text}\n")
            fj.write(json.dumps({
                "time": create_time, "date": ts,
                "sender": "me" if des == 0 else "other",
                "name": sender, "type": mtype, "text": text,
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"✓ {name}：{n} 条  →  {txt_path.name} / {jsonl_path.name}")


# ----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="从 iPhone 加密备份导出你自己的微信聊天记录（本地运行，数据不出本机）。"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="从加密备份解出微信数据库")
    pe.add_argument("--password", required=True, help="iPhone 加密备份的密码")
    pe.add_argument("--backup-dir", default="", help="备份目录（不给则自动挑最新）")
    pe.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    pe.set_defaults(func=cmd_extract)

    pl = sub.add_parser("list", help="列出联系人和消息条数")
    pl.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    pl.add_argument("--account", default="", help="多个微信号时指定账号目录名的一部分")
    pl.add_argument("--top", type=int, default=40, help="显示前 N 个（默认 40）")
    pl.add_argument("--all-contacts", action="store_true", help="显示全部联系人")
    pl.set_defaults(func=cmd_list)

    px = sub.add_parser("export", help="导出某人（或全部）的聊天为 txt/jsonl")
    px.add_argument("--db-dir", default=str(DEFAULT_DB_DIR))
    px.add_argument("--account", default="")
    px.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    px.add_argument("--name", default="", help="按备注/昵称包含匹配")
    px.add_argument("--wxid", default="", help="按 wxid 精确匹配")
    px.add_argument("--all", action="store_true", help="导出所有联系人")
    px.add_argument("--min-msgs", type=int, default=20, help="--all 时跳过少于这么多条的会话")
    px.add_argument("--me-label", default="我", help="你自己发言的显示名（默认「我」）")
    px.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

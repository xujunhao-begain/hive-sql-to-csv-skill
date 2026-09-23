#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在 Hive (HiveServer2) 上执行一段 SQL，把结果流式写入 CSV。

设计要点：
- 用 PyHive 直连 HiveServer2（Thrift）。
- 结果用游标逐批 fetch，边取边写盘，因此对百万行以上的大结果也不会把内存撑爆。
- 连接信息默认从 config.yml 读取；命令行参数 / 环境变量可覆盖任意字段。
- CSV：带表头、纯 UTF-8、逗号分隔，NULL 写成空串。

用法示例：
    python3 run_hive_to_csv.py --sql-file query.sql
    python3 run_hive_to_csv.py --sql "select * from db.t limit 100" --out /tmp/r.csv
    python3 run_hive_to_csv.py --sql-file q.sql --config /path/to/config.yml --database dw
    python3 run_hive_to_csv.py --sql-file q.sql --name 退费明细    # 指定归档名
    python3 run_hive_to_csv.py --list-sql                          # 列出已归档的 .sql
    python3 run_hive_to_csv.py --save-only --sql "..." --name 退费明细  # 只存档不执行
"""

import argparse
import csv
import os
import re
import sys
import time


# ---------------------------------------------------------------------------
# 目录约定
# ---------------------------------------------------------------------------

def skill_dir():
    """本技能根目录（scripts/ 的上一级）。

    用 realpath 而非 abspath：本技能可能通过 ~/.claude/skills/ 下的 symlink 被调用，
    abspath 不解析软链，会让下面的 project_root() 上溯到错误的目录。
    """
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def project_root():
    """仓库根（技能目录本身；独立仓库后 scripts/ 的上一级即仓库根）。"""
    return skill_dir()


def sql_archive_dir():
    """用户贴来的 SQL 归档目录：<技能目录>/sql/。"""
    return os.path.join(skill_dir(), "sql")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def find_config_path(explicit):
    """按优先级找 config.yml：显式 --config > 环境变量 > 技能目录 > 项目根 > ~/.hive/。"""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("HIVE_CONFIG"):
        candidates.append(os.environ["HIVE_CONFIG"])
    sk = skill_dir()
    candidates.append(os.path.join(sk, "config.yml"))
    candidates.append(os.path.join(sk, "config.yaml"))
    root = project_root()
    candidates.append(os.path.join(root, "config.yml"))
    candidates.append(os.path.join(root, "config.yaml"))
    candidates.append(os.path.expanduser("~/.hive/config.yml"))
    candidates.append(os.path.expanduser("~/.hive/config.yaml"))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def load_config(explicit):
    """读取 config.yml 里的 hive 段，返回 dict（找不到文件时返回空 dict）。"""
    path = find_config_path(explicit)
    if not path:
        return {}, None
    try:
        import yaml
    except ImportError:
        sys.exit("需要 PyYAML 才能读取 config.yml：pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # 支持顶层直接是连接字段，或包在 hive: 下
    hive = data.get("hive", data) if isinstance(data, dict) else {}
    return hive, path


def resolve_conn(args):
    """合并 config.yml、环境变量、命令行参数，得到最终连接参数。优先级：CLI > env > config。"""
    cfg, cfg_path = load_config(args.config)

    def pick(cli_val, env_key, cfg_key, default=None):
        if cli_val is not None:
            return cli_val
        if env_key and os.environ.get(env_key) is not None:
            return os.environ[env_key]
        if cfg.get(cfg_key) is not None:
            return cfg[cfg_key]
        return default

    conn = {
        "host": pick(args.host, "HIVE_HOST", "host"),
        "port": int(pick(args.port, "HIVE_PORT", "port", 10000)),
        "username": pick(args.user, "HIVE_USER", "username"),
        # 密码只从 config.yml（或 --password）取，不读 HIVE_PASSWORD 环境变量
        "password": pick(args.password, None, "password"),
        "database": pick(args.database, "HIVE_DATABASE", "database", "default"),
        "auth": pick(args.auth, "HIVE_AUTH", "auth", "LDAP"),
    }
    conn["_config_path"] = cfg_path
    return conn


# ---------------------------------------------------------------------------
# SQL 读取与拆分
# ---------------------------------------------------------------------------

SAFE_NAME_RE = re.compile(r"[^\w一-鿿.-]+")
# 归档文件名尾部的 _YYYY-MM-DD_HHMMSS
TS_SUFFIX_RE = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{6}$")


def sanitize_name(name):
    """把用户给的名字清洗成安全的文件名主干（保留中文、字母数字、下划线、点、连字符）。"""
    base = SAFE_NAME_RE.sub("_", (name or "").strip()).strip("._-")
    return base or "query"


def resolve_sql_file(path):
    """把 --sql-file 解析成真实路径。

    直接命中就用；否则当作归档里的名字，在 <技能目录>/sql/ 下按 <名字>.sql、
    以及 <名字>_<时间戳>.sql 前缀匹配（取最新的一个）。
    """
    if os.path.isfile(path):
        return path

    archive = sql_archive_dir()
    stem = os.path.splitext(os.path.basename(path))[0]
    direct = os.path.join(archive, stem + ".sql")
    if os.path.isfile(direct):
        return direct

    if os.path.isdir(archive):
        matches = sorted(
            f for f in os.listdir(archive)
            if f.endswith(".sql") and f.startswith(stem)
        )
        if matches:
            return os.path.join(archive, matches[-1])  # 文件名带时间戳，最后一个即最新

    sys.exit(
        f"找不到 SQL 文件：{path}\n"
        f"（也在归档目录 {archive} 里按名字找过了；用 --list-sql 看已有的 .sql）"
    )


def save_sql_file(sql, name, timestamp):
    """把一段 SQL 存成 <技能目录>/sql/<名字>_<时间戳>.sql，返回路径。"""
    archive = sql_archive_dir()
    os.makedirs(archive, exist_ok=True)
    path = os.path.join(archive, f"{sanitize_name(name)}_{timestamp}.sql")
    text = sql if sql.endswith("\n") else sql + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def list_sql_files():
    """列出归档目录里的 .sql 文件，按修改时间倒序。"""
    archive = sql_archive_dir()
    if not os.path.isdir(archive):
        print(f"归档目录还不存在：{archive}（还没存过 SQL）")
        return
    files = [f for f in os.listdir(archive) if f.endswith(".sql")]
    if not files:
        print(f"归档目录是空的：{archive}")
        return
    entries = []
    for f in files:
        p = os.path.join(archive, f)
        st = os.stat(p)
        entries.append((st.st_mtime, f, st.st_size))
    entries.sort(reverse=True)
    print(f"归档目录 {archive}（{len(entries)} 个）：")
    for mtime, fname, size in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        print(f"  {when}  {size:>7d}B  {fname}")


def read_sql(args):
    """拿到要执行的 SQL 文本，返回 (sql, 来源文件路径或 None)。

    来源文件路径用于命名 CSV；直接传字符串 / 标准输入时为 None，
    调用方会把它归档成 .sql 后再回填。
    """
    if args.sql_file:
        path = resolve_sql_file(args.sql_file)
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), path
    if args.sql:
        return args.sql, None
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data, None
    sys.exit("没有拿到 SQL：请用 --sql-file、--sql 或标准输入传入。")


def split_statements(sql):
    """把脚本按分号拆成多条语句，跳过注释与字符串里的分号。

    Hive 脚本经常先 set 一堆参数、建临时表，最后再 select。我们把前面的都当作
    “要执行但不产出结果”的语句，最后一条产出结果的 select 写进 CSV。
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    in_squote = in_dquote = in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        # 普通状态
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def is_result_producing(stmt):
    """判断一条语句是否会返回结果集（select / with ... select / show / describe 等）。"""
    s = stmt.lstrip().lower()
    # 去掉前导注释行
    s = re.sub(r"^(--[^\n]*\n|/\*.*?\*/\s*)+", "", s, flags=re.DOTALL).lstrip()
    return s.startswith(("select", "with", "show", "describe", "desc ", "explain"))


# ---------------------------------------------------------------------------
# 执行 + 写 CSV
# ---------------------------------------------------------------------------

def clean_columns(description):
    """把游标 description 里的列名清洗成干净表头（去掉 `别名.` 前缀）。"""
    cols = []
    for d in description:
        name = d[0]
        if name and "." in name:
            name = name.split(".")[-1]  # t.col -> col
        cols.append(name)
    return cols


def run(conn, statements, out_path, batch_size, verbose):
    from pyhive import hive

    connect_kwargs = {
        "host": conn["host"],
        "port": conn["port"],
        "username": conn["username"],
        "database": conn["database"],
    }
    auth = (conn.get("auth") or "NONE").upper()
    if auth in ("LDAP", "CUSTOM"):
        connect_kwargs["auth"] = auth
        connect_kwargs["password"] = conn["password"]
    elif auth == "KERBEROS":
        connect_kwargs["auth"] = "KERBEROS"
        connect_kwargs["kerberos_service_name"] = conn.get("kerberos_service_name", "hive")
    elif auth == "NONE":
        connect_kwargs["auth"] = "NONE"
    else:
        connect_kwargs["auth"] = auth

    if verbose:
        redacted = {k: ("***" if k == "password" else v) for k, v in connect_kwargs.items()}
        print(f"[连接] {redacted}", file=sys.stderr)

    connection = hive.connect(**connect_kwargs)
    cursor = connection.cursor()

    # 找到最后一条产出结果的语句；它之前的语句先按顺序执行（set / create temp / use 等）。
    result_idx = None
    for idx, stmt in enumerate(statements):
        if is_result_producing(stmt):
            result_idx = idx  # 一直更新，最后一条产出结果的即为目标

    if result_idx is None:
        # 没有 select，只执行语句，不写 CSV
        for idx, stmt in enumerate(statements):
            if verbose:
                print(f"[执行 {idx + 1}/{len(statements)}] {stmt[:80]}...", file=sys.stderr)
            cursor.execute(stmt)
        connection.close()
        return {"rows": 0, "cols": [], "no_result": True}

    for idx, stmt in enumerate(statements):
        if idx < result_idx:
            if verbose:
                print(f"[前置 {idx + 1}] {stmt.splitlines()[0][:80]}", file=sys.stderr)
            cursor.execute(stmt)

    target = statements[result_idx]
    if verbose:
        print(f"[查询] 执行结果语句（{len(target)} 字符）...", file=sys.stderr)
    cursor.execute(target)

    columns = clean_columns(cursor.description or [])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    total = 0
    # newline="" 让 csv 模块自己管行结束符；utf-8 纯编码（无 BOM）。
    with open(out_path, "w", encoding="utf-8", newline="") as fout:
        writer = csv.writer(fout)
        writer.writerow(columns)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                # NULL -> 空串；其余转字符串交给 csv 模块处理引号转义
                writer.writerow(["" if v is None else v for v in row])
            total += len(rows)
            if verbose:
                print(f"  ...已写 {total} 行", file=sys.stderr)

    connection.close()
    return {"rows": total, "cols": columns, "no_result": False}


# ---------------------------------------------------------------------------
# 输出路径
# ---------------------------------------------------------------------------

def default_out_path(sql_file, timestamp):
    """默认输出到 docs/hive-sql-to-csv/ 下，按来源名 + 时间戳命名。

    来源名里已经带时间戳（归档文件都带）的话先剥掉，再贴上本次运行的时间戳，
    避免出现 name_旧时间戳_新时间戳.csv 这种叠加。
    """
    out_dir = os.path.join(project_root(), "docs", "hive-sql-to-csv")
    if sql_file:
        base = sanitize_name(os.path.splitext(os.path.basename(sql_file))[0])
        base = TS_SUFFIX_RE.sub("", base) or "hive_result"
        name = f"{base}_{timestamp}"
    else:
        name = f"hive_result_{timestamp}"
    return os.path.join(out_dir, name + ".csv")


def main():
    ap = argparse.ArgumentParser(description="在 Hive 上执行 SQL 并把结果写入 CSV")
    ap.add_argument("--sql-file", help="SQL 文件路径；也可只给归档里的名字，自动在 <技能目录>/sql/ 下找最新的")
    ap.add_argument("--sql", help="直接传入 SQL 字符串（会自动归档成 <技能目录>/sql/<名字>_<时间戳>.sql）")
    ap.add_argument("--name", help="归档 .sql 的名字（配合 --sql 使用，默认 query）")
    ap.add_argument("--no-save-sql", action="store_true",
                    help="传入 SQL 字符串时不落归档文件（默认会落）")
    ap.add_argument("--save-only", action="store_true",
                    help="只把 SQL 存成 .sql 文件，不连 Hive 执行")
    ap.add_argument("--list-sql", action="store_true", help="列出 <技能目录>/sql/ 里已归档的 .sql 并退出")
    ap.add_argument("--out", help="CSV 输出路径（默认写到 docs/hive-sql-to-csv/ 带时间戳）")
    ap.add_argument("--config", help="config.yml 路径（默认自动查找）")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--database")
    ap.add_argument("--auth", help="NONE / LDAP / KERBEROS，默认取 config 或 LDAP")
    ap.add_argument("--batch-size", type=int, default=10000, help="每批 fetch 行数（默认 10000）")
    ap.add_argument("-q", "--quiet", action="store_true", help="安静模式，少打进度")
    args = ap.parse_args()

    if args.list_sql:
        list_sql_files()
        return

    verbose = not args.quiet
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    sql, sql_file = read_sql(args)
    statements = split_statements(sql)
    if not statements:
        sys.exit("拆分后没有可执行语句。")

    # 用户直接贴的 SQL（没有源文件）默认归档成 .sql，让每次执行都有可复现的存档。
    if sql_file is None and not args.no_save_sql:
        sql_file = save_sql_file(sql, args.name, ts)
        print(f"💾 SQL 已存档: {sql_file}")

    if args.save_only:
        if sql_file is None:
            sys.exit("--save-only 需要落档，但同时给了 --no-save-sql。")
        print(f"✅ 只存档不执行（{len(statements)} 条语句）。")
        return

    conn = resolve_conn(args)
    missing = [k for k in ("host", "username") if not conn.get(k)]
    if missing:
        sys.exit(
            f"缺少连接参数 {missing}。请在 config.yml 里填 hive.host / hive.username，"
            f"或用 --host/--user 传入。（当前 config: {conn.get('_config_path')}）"
        )
    if (conn.get("auth") or "").upper() == "LDAP" and not conn.get("password"):
        sys.exit("auth=LDAP 但没有密码。请在 config.yml 填 hive.password 或用 --password 传入。")

    out_path = args.out or default_out_path(sql_file, ts)

    t0 = time.time()
    info = run(conn, statements, out_path, args.batch_size, verbose)
    dt = time.time() - t0

    if info["no_result"]:
        print(f"✅ 执行完成（{len(statements)} 条语句），没有 SELECT 产出，未写 CSV。耗时 {dt:.1f}s")
        return

    print(f"✅ 完成：{info['rows']} 行 × {len(info['cols'])} 列，耗时 {dt:.1f}s")
    print(f"📄 CSV: {out_path}")
    if sql_file:
        print(f"📝 SQL: {sql_file}")
    if info["cols"]:
        preview = ", ".join(info["cols"][:12])
        more = "" if len(info["cols"]) <= 12 else f" …(+{len(info['cols']) - 12})"
        print(f"🧾 列: {preview}{more}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive-sql-to-csv-skill 的配置检测与引导。

既作命令行工具（供 install.sh / agent 判断是否需要首次配置引导），也作可复用模块。
配置由 config.yml 的 hive: 段组成：host / port / username / password / database / auth。
真实 LDAP 凭据不入库，本地放在 config.yml（权限 600）。

退出码:
  0 = 就绪（config.yml 存在、host/username/password 都填了且不是占位值）
  2 = 未配置（缺 config.yml）
  3 = 配置不完整（host/username/password 缺失或仍是占位值，列出具体项）

命令行用法:
    python3 check_config.py [--skill-dir <技能根目录>]
"""
import argparse
import os
import sys

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# config.example.yml 里的占位值，用来识别“还没改”的配置
PLACEHOLDER_HOST = "your-hive-server.example.com"
PLACEHOLDER_PASSWORD = "YOUR_LDAP_PASSWORD"


def skill_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config(skill_dir):
    """返回 (cfg, err)。err 为 None 表示成功；否则为 (类型, 详情)。

    读取 config.yml，支持顶层直接是连接字段，或包在 hive: 下（与 run_hive_to_csv.py 一致）。
    """
    path = os.path.join(skill_dir, "config.yml")
    if not os.path.isfile(path):
        return None, ("NOT_CONFIGURED", path)
    if not HAS_YAML:
        return None, ("NO_PYYAML", "pip install pyyaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return None, ("INVALID", str(e))
    hive = data.get("hive", data) if isinstance(data, dict) else {}
    return hive, None


def check(skill_dir):
    """返回 (缺失项列表, 告警列表)。缺失项非空 -> 退出码 3。

    检查：
      - host 不为空、不是占位 your-hive-server.example.com
      - username 不为空
      - auth=LDAP 时 password 不为空、不是占位 YOUR_LDAP_PASSWORD
    """
    missing, warns = [], []
    cfg, err = load_config(skill_dir)
    if err:
        if err[0] == "NOT_CONFIGURED":
            return ["config.yml（复制 config.example.yml 后填 Hive 连接）"], warns
        if err[0] == "NO_PYYAML":
            return ["PyYAML（pip install pyyaml）"], warns
        return [f"config.yml 解析失败: {err[1]}"], warns

    host = cfg.get("host")
    username = cfg.get("username")
    password = cfg.get("password")
    auth = (cfg.get("auth") or "LDAP").upper()

    if not host:
        missing.append("config.yml 的 hive.host（空）")
    elif host == PLACEHOLDER_HOST:
        missing.append(f"hive.host 仍是占位 {PLACEHOLDER_HOST}（改成你的 HiveServer2 地址）")

    if not username:
        missing.append("config.yml 的 hive.username（空，填 LDAP 账号）")

    if auth == "LDAP":
        if not password:
            missing.append("config.yml 的 hive.password（空，LDAP 认证需要密码）")
        elif password == PLACEHOLDER_PASSWORD:
            missing.append(f"hive.password 仍是占位 {PLACEHOLDER_PASSWORD}（填当前有效的 LDAP 密码）")

    return missing, warns


def is_ready(skill_dir=None):
    """供模块调用：配置是否就绪。"""
    skill_dir = skill_dir or skill_root()
    missing, _ = check(skill_dir)
    return not missing


def main():
    ap = argparse.ArgumentParser(description="hive-sql-to-csv-skill 配置检测")
    ap.add_argument("--skill-dir", default=skill_root(),
                    help="技能根目录（默认: 脚本所在目录的上一级）")
    args = ap.parse_args()

    cfg, err = load_config(args.skill_dir)
    if err and err[0] == "NOT_CONFIGURED":
        print(f"[未配置] 找不到 {err[1]}")
        print("引导: cp config.example.yml config.yml  然后填入 Hive 连接信息")
        return 2
    if err and err[0] == "NO_PYYAML":
        print(f"[缺依赖] {err[1]}")
        return 3

    missing, warns = check(args.skill_dir)
    if missing:
        print("[配置不完整] 以下项缺失/无效：")
        for m in missing:
            print(f"  - {m}")
        print("\n引导:")
        print("  1. cp config.example.yml config.yml")
        print("  2. 在 config.yml 填入 Hive 连接信息（host/port/username/password/auth）")
        print("  3. 重跑: python3 scripts/check_config.py")
        return 3

    for w in warns:
        print(f"[告警] {w}")
    print("[就绪] config.yml 的 Hive 连接已填，可以跑 run_hive_to_csv.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

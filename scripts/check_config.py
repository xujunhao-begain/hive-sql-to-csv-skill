#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hive-sql-to-csv-skill 的配置检测与引导（Agent 友好）。

既作命令行工具（供 install.sh / Agent 判断是否需要首次配置引导），也作可复用模块。
配置由 config.yml 的 hive: 段组成：host / port / username / password / database / auth。
真实 LDAP 凭据不入库，本地放在 config.yml（权限 600）。

== 输出模式 ==
默认输出 JSON（机器可读，供 Agent 程序化消费）。
加 --human / --text 切回人本可读的纯文本。
--json 显式指定 JSON（与默认一致，便于脚本调用更明确）。

== 退出码（与输出模式无关）==
  0 = 就绪
  2 = 未配置（缺 config.yml）
  3 = 配置不完整（host/username/password 缺失或仍是占位值，或在缺依赖 / 解析失败）

== JSON 形态 ==
{
  "ready": <bool>,
  "skill_dir": "<abs path>",
  "config_path": "<abs path or null>",
  "config_exists": <bool>,
  "missing": [{"field": "host|username|password|config.yml|pyyaml", "reason": "..."}],
  "warnings": [...],
  "next_actions": [
    {"step": "copy_template", "cmd": "cp .../config.example.yml .../config.yml"},
    {"step": "ask_user", "fields": ["host","username","password"], "hint": "..."},
    {"step": "write_config", "path": ".../config.yml", "note": "..."},
    {"step": "chmod", "cmd": "chmod 600 .../config.yml", "mode": "600"},
    {"step": "verify", "cmd": "python3 .../scripts/check_config.py --skill-dir ..."}
  ]
}

命令行用法:
    python3 check_config.py [--skill-dir <技能根目录>] [--human|--json]
"""
import argparse
import json
import os
import sys

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# config.example.yml 里的占位值，用来识别"还没改"的配置
PLACEHOLDER_HOST = "your-hive-server.example.com"
PLACEHOLDER_PASSWORD = "YOUR_LDAP_PASSWORD"

# Agent 视角下需要"问用户"的连接字段
CONNECTION_FIELDS = ("host", "username", "password")


def skill_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path_for(skill_dir):
    return os.path.join(skill_dir, "config.yml")


def template_path_for(skill_dir):
    return os.path.join(skill_dir, "config.example.yml")


def load_config(skill_dir):
    """返回 (cfg, err)。err 为 None 表示成功；否则为 (类型, 详情)。

    读取 config.yml，支持顶层直接是连接字段，或包在 hive: 下（与 run_hive_to_csv.py 一致）。
    """
    path = config_path_for(skill_dir)
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


# ---------------------------------------------------------------------------
# 缺失项检测
# ---------------------------------------------------------------------------

def check(skill_dir):
    """返回 (missing_items, warnings)。

    missing_items: list of {"field": str, "reason": str}
      field 取值: "host" / "username" / "password" / "config.yml" / "pyyaml"
      （"config.yml" 表示文件本身解析失败；"pyyaml" 表示缺依赖；
       其它三项缺失时直接给出对应字段名）
    warnings: list of str
    """
    missing, warns = [], []
    cfg, err = load_config(skill_dir)
    if err:
        if err[0] == "NOT_CONFIGURED":
            # config.yml 不存在 → 所有连接字段都视作缺失，便于 Agent 一次性问齐
            for f in CONNECTION_FIELDS:
                missing.append({"field": f, "reason": "config.yml 不存在"})
        elif err[0] == "NO_PYYAML":
            missing.append({"field": "pyyaml", "reason": "缺 PyYAML（pip install pyyaml）"})
        else:  # INVALID
            missing.append({"field": "config.yml", "reason": f"解析失败: {err[1]}"})
        return missing, warns

    host = cfg.get("host")
    username = cfg.get("username")
    password = cfg.get("password")
    auth = (cfg.get("auth") or "LDAP").upper()

    if not host:
        missing.append({"field": "host", "reason": "hive.host 为空"})
    elif host == PLACEHOLDER_HOST:
        missing.append({"field": "host", "reason": f"hive.host 仍是占位 {PLACEHOLDER_HOST}"})

    if not username:
        missing.append({"field": "username", "reason": "hive.username 为空"})

    if auth == "LDAP":
        if not password:
            missing.append({"field": "password", "reason": "hive.password 为空（LDAP 认证需要密码）"})
        elif password == PLACEHOLDER_PASSWORD:
            missing.append({"field": "password", "reason": f"hive.password 仍是占位 {PLACEHOLDER_PASSWORD}"})

    return missing, warns


def is_ready(skill_dir=None):
    """供模块调用：配置是否就绪。"""
    skill_dir = skill_dir or skill_root()
    missing, _ = check(skill_dir)
    return not missing


# ---------------------------------------------------------------------------
# 构建 Agent 可执行的动作清单
# ---------------------------------------------------------------------------

def verify_cmd(skill_dir):
    return f"python3 {os.path.join(skill_dir, 'scripts', 'check_config.py')} --skill-dir {skill_dir}"


def next_actions(skill_dir, missing):
    """根据缺失项构建 Agent 可执行的动作清单（按推荐执行顺序）。"""
    if not missing:
        return []

    cfg_path = config_path_for(skill_dir)
    tpl_path = template_path_for(skill_dir)
    fields_missing = [m["field"] for m in missing]

    actions = []

    # 缺依赖 → 先装 PyYAML
    if "pyyaml" in fields_missing:
        actions.append({
            "step": "install_dep",
            "cmd": "pip install pyyaml",
            "reason": "缺 PyYAML 才能读取 config.yml",
        })

    # config.yml 不存在 / 解析失败 → 复制模板（或重写）
    needs_copy = any(
        m["field"] in CONNECTION_FIELDS and "config.yml 不存在" in m["reason"]
        for m in missing
    ) or "config.yml" in fields_missing
    if needs_copy:
        actions.append({
            "step": "copy_template",
            "cmd": f"cp {tpl_path} {cfg_path}",
            "from": tpl_path,
            "to": cfg_path,
        })

    # 问用户要连接字段
    ask_fields = sorted({f for f in fields_missing if f in CONNECTION_FIELDS})
    if ask_fields:
        actions.append({
            "step": "ask_user",
            "fields": ask_fields,
            "hint": "向用户询问 Hive 连接信息；不要猜，不要用默认值；密码只写本地 config.yml（权限 600，不入库）",
        })
        actions.append({
            "step": "write_config",
            "path": cfg_path,
            "note": "按 config.example.yml 的 hive: 段结构填入用户提供的值（host/port/username/password/database/auth）",
        })

    # 锁权限（仅当存在 / 新建了 config.yml 且涉及连接字段或解析失败）
    if any(f in (*CONNECTION_FIELDS, "config.yml") for f in fields_missing):
        actions.append({
            "step": "chmod",
            "cmd": f"chmod 600 {cfg_path}",
            "path": cfg_path,
            "mode": "600",
        })

    # 复跑验证
    actions.append({
        "step": "verify",
        "cmd": verify_cmd(skill_dir),
        "expect": "ready=true 即装好；仍 false 则按 missing 继续追问用户",
    })

    return actions


def status(skill_dir):
    """组装一个完整的状态 dict（供 JSON 输出）。"""
    missing, warns = check(skill_dir)
    cfg_path = config_path_for(skill_dir)
    cfg_exists = os.path.isfile(cfg_path)
    return {
        "ready": not missing,
        "skill_dir": os.path.abspath(skill_dir),
        "config_path": os.path.abspath(cfg_path) if cfg_exists else None,
        "config_exists": cfg_exists,
        "missing": missing,
        "warnings": warns,
        "next_actions": next_actions(skill_dir, missing),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def print_json(status_obj):
    print(json.dumps(status_obj, ensure_ascii=False, indent=2))


def print_human(status_obj):
    """人本可读的文本输出。"""
    skill_dir = status_obj["skill_dir"]
    cfg_path = status_obj["config_path"]
    missing = status_obj["missing"]
    warnings = status_obj["warnings"]

    if not missing:
        for w in warnings:
            print(f"[告警] {w}")
        print(f"[就绪] config.yml 的 Hive 连接已填（{cfg_path}），可以跑 run_hive_to_csv.py")
        return

    # 有缺失项
    if not status_obj["config_exists"]:
        print(f"[未配置] 找不到 {config_path_for(skill_dir)}")
        print("引导: cp config.example.yml config.yml  然后填入 Hive 连接信息")
        return

    print("[配置不完整] 以下项缺失/无效：")
    for m in missing:
        print(f"  - {m['field']}: {m['reason']}")
    print("\n引导:")
    print("  1. cp config.example.yml config.yml")
    print("  2. 在 config.yml 填入 Hive 连接信息（host/port/username/password/auth）")
    print("  3. 重跑: python3 scripts/check_config.py")


def main():
    ap = argparse.ArgumentParser(
        description="hive-sql-to-csv-skill 配置检测（默认 JSON，Agent 友好）"
    )
    ap.add_argument("--skill-dir", default=skill_root(),
                    help="技能根目录（默认: 脚本所在目录的上一级）")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--human", "--text", dest="human", action="store_true",
                     help="输出人本可读文本（默认是 JSON）")
    fmt.add_argument("--json", action="store_true",
                     help="显式指定 JSON 输出（与默认一致）")
    args = ap.parse_args()

    skill_dir = args.skill_dir or skill_root()
    status_obj = status(skill_dir)

    if args.human:
        print_human(status_obj)
    else:
        print_json(status_obj)

    # 退出码：ready → 0；缺 config.yml → 2；其它不完整 → 3
    if status_obj["ready"]:
        return 0
    if not status_obj["config_exists"]:
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())

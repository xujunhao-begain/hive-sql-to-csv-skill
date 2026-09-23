#!/bin/sh
# install.sh — 一键安装 hive-sql-to-csv-skill 到 Trae-CN 和/或 Claude Code
#
# 用法:
#   sh install.sh               # 自动检测环境，装到 global（检测到哪个装哪个，都检测到就都装）
#   sh install.sh --project     # 装到当前项目（<项目>/.trae/skills/ 与 <项目>/.claude/skills/）
#   sh install.sh --trae        # 只装 Trae-CN（global）
#   sh install.sh --claude      # 只装 Claude Code（global）
#   sh install.sh --non-interactive   # Agent 模式：装完跑 check_config.py --json，最后输出一段 JSON 给 Agent 消费
#   sh install.sh --yes        # 同 --non-interactive
#   sh install.sh --json       # 同 --non-interactive（显式指定 JSON 输出）
#
# 安装布局:
#   Trae-CN     → 整目录装入 ~/.trae-cn/skills/hive-sql-to-csv-skill/（项目级为 <项目>/.trae/skills/）
#   Claude Code → 整目录装入 ~/.claude/skills/hive-sql-to-csv-skill/（项目级为 <项目>/.claude/skills/）
#   入口均为 SKILL.md，按 description 自动路由触发。
#
# copy_tree 用 tar 排除 .git / __pycache__ / config.yml / sql（归档的本机 SQL，含真实业务 SQL）/
#   docs（产物 CSV）——这些是不入库的本机数据，安装时不带进 skill 目录，由 check_config.py 引导准备。
#
# 装完自动跑一次 check_config.py 检测配置状态（退出码 0=就绪 2=缺 config.yml 3=不完整）。
# 默认（交互模式）输出 Agent 也可照做的"动作清单"（具体命令）；
# --non-interactive / --yes / --json 则输出 JSON 给 Agent 程序化消费。

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NAME="hive-sql-to-csv-skill"
ONLY=""
MODE="global"
JSON_MODE=0

for arg in "$@"; do
  case "$arg" in
    --project) MODE="project" ;;
    --trae)    ONLY="trae" ;;
    --claude)  ONLY="claude" ;;
    --non-interactive|--yes|--json) JSON_MODE=1 ;;
    *) echo "未知参数: $arg（支持 --project / --trae / --claude / --non-interactive / --yes / --json）" >&2; exit 2 ;;
  esac
done

# 注意：Trae-CN 的 global 目录是 ~/.trae-cn，但项目级是 <项目>/.trae（不是 .trae-cn）
if [ "$MODE" = "project" ]; then
  TRAE_ROOT="$PWD/.trae"
  CLAUDE_ROOT="$PWD/.claude"
else
  TRAE_ROOT="$HOME/.trae-cn"
  CLAUDE_ROOT="$HOME/.claude"
fi
TRAE_DEST="$TRAE_ROOT/skills/$NAME"
CLAUDE_DEST="$CLAUDE_ROOT/skills/$NAME"

# ---------------------------------------------------------------- 工具函数
real() { CDPATH= cd -- "$1" 2>/dev/null && pwd; }
is_same() { [ "$(real "$1")" = "$(real "$2")" ]; }

copy_tree() {
  # copy_tree <src> <dest> — 复制并排除 git/缓存/本机数据
  mkdir -p "$2"
  (cd "$1" && tar \
       --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
       --exclude=.DS_Store \
       --exclude=config.yml \
       --exclude=sql --exclude=docs \
       -cf - .) | (cd "$2" && tar -xf -)
}

safe_rmtree() {
  # safe_rmtree <dir> — 只删非空名字的合法路径，防变量为空导致误删
  case "$1" in
    "" | "/" | "$HOME") echo "拒绝删除可疑路径: '$1'" >&2; exit 2 ;;
    *) rm -rf "$1" ;;
  esac
}

# ---------------------------------------------------------------- 安装动作
install_one() {  # install_one <dest>
  if is_same "$SRC_DIR" "$1"; then
    echo "[源即目标] 跳过复制: $1"
  else
    safe_rmtree "$1"
    copy_tree "$SRC_DIR" "$1"
    echo "[已安装] $1"
  fi
  chmod +x "$1/scripts/"*.py 2>/dev/null
  chmod +x "$1/install.sh" 2>/dev/null
}

# ---------------------------------------------------------------- 决定装哪
DO_TRAE=0
DO_CLAUDE=0
if [ "$ONLY" = "trae" ]; then
  DO_TRAE=1
elif [ "$ONLY" = "claude" ]; then
  DO_CLAUDE=1
elif [ "$MODE" = "project" ]; then
  DO_TRAE=1                                # 项目级 trae 约定固定为 .trae/skills/
  [ -d "$CLAUDE_ROOT" ] && DO_CLAUDE=1
else
  [ -d "$TRAE_ROOT" ] && DO_TRAE=1
  [ -d "$CLAUDE_ROOT" ] && DO_CLAUDE=1
fi

if [ "$DO_TRAE" -eq 0 ] && [ "$DO_CLAUDE" -eq 0 ]; then
  echo "未检测到 Trae-CN（$HOME/.trae-cn）或 Claude Code（$HOME/.claude）环境。" >&2
  echo "请显式指定: sh install.sh --trae 或 sh install.sh --claude" >&2
  exit 1
fi

DETECT_DIR=""
if [ "$DO_TRAE" -eq 1 ]; then
  install_one "$TRAE_DEST"
  DETECT_DIR="$TRAE_DEST"
fi
if [ "$DO_CLAUDE" -eq 1 ]; then
  install_one "$CLAUDE_DEST"
  [ -z "$DETECT_DIR" ] && DETECT_DIR="$CLAUDE_DEST"
fi

# ---------------------------------------------------------------- 配置检测
# 默认（交互模式）: 打印 Agent 也可直接照做的「动作清单」（具体命令）。
# --non-interactive / --yes / --json: 调 check_config.py（默认 JSON），
#   用 ---BEGIN CONFIG JSON--- / ---END CONFIG JSON--- 包起来，Agent 按这两个标记截取。
if [ "$JSON_MODE" -eq 1 ]; then
  echo "---BEGIN CONFIG JSON---"
  python3 "$DETECT_DIR/scripts/check_config.py" --skill-dir "$DETECT_DIR" --json
  echo "---END CONFIG JSON---"
else
  echo ""
  echo "=== 配置检测 ==="
  if [ -f "$DETECT_DIR/scripts/check_config.py" ]; then
    python3 "$DETECT_DIR/scripts/check_config.py" --skill-dir "$DETECT_DIR" --human
    RC=$?
    if [ "$RC" -ne 0 ]; then
      echo ""
      echo "=== 下一步动作（按顺序执行，Agent 可直接照做） ==="
      echo "  1. cp $DETECT_DIR/config.example.yml $DETECT_DIR/config.yml"
      echo "  2. 询问用户的 Hive 连接信息：host / port(默认 10000) / username / password / database(默认 default) / auth(默认 LDAP)"
      echo "     （不要猜，不要用默认值；密码只写本地 config.yml，权限 600，不入库）"
      echo "  3. 把上述值写入 $DETECT_DIR/config.yml 的 hive: 段"
      echo "  4. chmod 600 $DETECT_DIR/config.yml"
      echo "  5. python3 \"$DETECT_DIR/scripts/check_config.py\" --skill-dir \"$DETECT_DIR\" --human"
      echo ""
      echo "（Agent 模式可改用: sh install.sh --non-interactive  或  check_config.py --json 拿机器可读状态）"
    fi
  else
    echo "[警告] $DETECT_DIR/scripts/check_config.py 不存在，跳过配置检测" >&2
  fi
fi

exit 0

# hive-sql-to-csv-skill

在 Hive (HiveServer2) 上执行一段 SQL，把查询结果流式导出成 CSV 文件。支持直接传 SQL 字符串、传 `.sql` 文件路径、传已归档 SQL 的名字，或复杂脚本（先 `set` 参数 / 建临时表、最后一条 `select` 出结果）。用户贴来的 SQL 会自动归档成 `.sql` 文件便于复用和复现。结果用游标流式写盘，百万行以上也不撑内存；CSV 带表头、纯 UTF-8、逗号分隔。

完整行为细节见 [SKILL.md](SKILL.md)。

## 安装

### 一键安装（推荐）

```bash
git clone <repo-url> hive-sql-to-csv-skill
cd hive-sql-to-csv-skill
sh install.sh                 # 自动检测 trae-cn / claude，装到 global
# 或: sh install.sh --project # 装到当前项目（<项目>/.trae/skills/ 与 <项目>/.claude/skills/）
# 或: sh install.sh --trae / --claude   # 只装其中一个
```

`install.sh` 会把整目录复制到：

- Trae-CN：`~/.trae-cn/skills/hive-sql-to-csv-skill/`（项目级为 `<项目>/.trae/skills/`）
- Claude Code：`~/.claude/skills/hive-sql-to-csv-skill/`（项目级为 `<项目>/.claude/skills/`）

安装时用 tar 排除 `config.yml`、`sql/`（归档的本机 SQL，含真实业务 SQL）、`docs/`（产物 CSV）——这些是本机数据，不会带进 skill 目录。装完自动跑 `check_config.py` 检测配置状态。

### 依赖

```bash
pip install -r requirements.txt   # PyYAML + pyhive + thrift + sasl + thrift_sasl
```

## 配置（首次使用）

复制配置模板并填入自己的 Hive 连接信息：

```bash
cp config.example.yml config.yml
chmod 600 config.yml
```

`config.yml` 不入库（含真实 LDAP 凭据）。需要填的字段（`hive:` 段）：

- `host` / `port`：HiveServer2 地址和端口（默认 10000）
- `username` / `password`：LDAP 账号和密码
- `database`：默认库（默认 `default`）
- `auth`：认证方式，默认 `LDAP`

完成配置后用 `check_config.py` 验证：

```bash
python3 scripts/check_config.py
# 退出码 0 = 就绪，2 = 缺 config.yml，3 = 配置不完整（列出缺失项）
```

`install.sh` 装完会自动跑一次，按提示补齐即可。

## 用法

```bash
# 从 .sql 文件执行（最常见）
python3 scripts/run_hive_to_csv.py --sql-file /path/to/query.sql

# 用户贴了一段 SQL：自动归档成 .sql 再执行；--name 给它起个有意义的名字
python3 scripts/run_hive_to_csv.py --sql "select * from db.t limit 100" --name 退款明细

# 只把用户贴的 SQL 存成文件，先不执行
python3 scripts/run_hive_to_csv.py --save-only --sql "..." --name 退款明细

# 看看之前归档过哪些 SQL
python3 scripts/run_hive_to_csv.py --list-sql

# 重跑一个归档过的查询：只给名字即可，自动取同名里最新的那份
python3 scripts/run_hive_to_csv.py --sql-file 退款明细

# 指定输出路径 / 覆盖默认库
python3 scripts/run_hive_to_csv.py --sql-file q.sql --out /tmp/r.csv --database dw
```

完整参数见 `python3 scripts/run_hive_to_csv.py -h`。

产物默认落在 `docs/hive-sql-to-csv/<来源名>_<时间戳>.csv`（不传 `--out` 时）。来源名取自 `.sql` 文件名，CSV 和它对应的 SQL 存档同名可对齐，方便回溯哪份结果来自哪段 SQL。

## 说明与边界

- 只负责「执行 + 落 CSV」。不改写、不优化 SQL；不判断字段业务含义或来源表；不扫描文件里出现了哪些字段。
- 连接失败（网络不通 / 认证错 / 库表不存在）时，如实把 PyHive 的报错转达，不伪装成功。
- 大结果导出会持续占用 Hive 连接与本地磁盘，跑之前对明显没有 `limit` 的全表扫描可以提醒一句。

## License

MIT

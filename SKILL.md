---
name: hive-sql-to-csv-skill
description: 在 Hive (HiveServer2) 上执行一段 SQL，把查询结果导出成 CSV 文件。当用户说「在 hive 里跑这个 sql / 执行这段 hive sql / 跑一下这个查询」「把查询结果存成 csv / 导出成 csv / 拉成 csv」「跑一下这个 .sql 文件把结果导出」「连 hive 查一下这张表并保存结果」「把这段 sql 存成文件 / 存下来下次再跑」「我之前存的那个查询再跑一遍」时使用。支持直接传 SQL 字符串、传 .sql 文件路径、传已归档 SQL 的名字、或复杂脚本（先 set 参数 / 建临时表、最后一条 select 出结果）。用户贴来的 SQL 会自动归档成 .sql 文件便于复用和复现。结果用游标流式写盘，百万行以上也不撑内存；CSV 带表头、纯 UTF-8、逗号分隔。连接信息从 config.yml 读取（LDAP 认证）。不负责解析字段血缘（那是 sql-field-lineage），也不负责扫描文件里出现了哪些字段（那是 field-search）。
version: 1.1.0
---

# hive-sql-to-csv-skill

给定一段 Hive SQL（字符串、`.sql` 文件、或用户 IDE 里打开的 SQL），连上 HiveServer2 执行，
把最终的查询结果**流式**写入一个 CSV 文件，然后在对话里精简汇报行数、列、耗时和 CSV 路径。

典型场景：「在 hive 里跑这个 sql，把结果存成 csv」——用户手上有一段现成的 Hive 查询，想拿到落地的结果表。

SQL 有两种来源，都走同一个脚本：

- **从 `.sql` 文件执行**——用户给文件路径，或只给一个已归档 SQL 的名字。
- **用户直接贴 SQL**——脚本先把它归档成 `.sql` 文件（默认行为），再执行；
  这样每份 CSV 都有一份对应的、可复现可复用的 SQL 存档。

## 前置：连接配置

脚本用 PyHive 直连（LDAP 认证）。连接信息**全部**放在 `config.yml` 的 `hive:` 段：

- **密码**：`hive.password`，直接写在 `config.yml` 里（**不读** `HIVE_PASSWORD` 环境变量）。
  该文件含真实凭证，权限已设为 600，**不要提交到版本库**。
- **其余项**：`host / port / username / database / auth`。
  查找顺序：`--config` 指定的路径 → 环境变量 `HIVE_CONFIG` → **本技能目录**下的
  `config.yml` → 仓库根 `config.yml` → `~/.hive/config.yml`。

命令行参数（`--host/--user/--password/...`）可覆盖任意字段，优先级 **CLI > config.yml**。
除密码外的字段仍支持 `HIVE_HOST/HIVE_USER/...` 环境变量覆盖；**密码只认 config.yml 和 `--password`**。

如果脚本报「缺少连接参数」或「auth=LDAP 但没有密码」，**不要**去猜 host / 账号密码，
如实告诉用户，并按报错项分别提示：
缺密码或认证失败 → 检查本技能目录下 `config.yml` 的 `hive.password` 是否为当前有效的 LDAP 密码，
缺其它字段 → 同样检查该 `config.yml`。

## 怎么做

脚本承担全部机械工作（归档 SQL、连接、执行、流式写盘）。`$SK` 为本技能目录。

```bash
SK="<本技能目录>"

# ① 跑一个 .sql 文件（最常见）
python3 "$SK/scripts/run_hive_to_csv.py" --sql-file "/path/to/query.sql"

# ② 用户贴了一段 SQL：自动归档成 .sql 再执行；--name 给它起个有意义的名字
python3 "$SK/scripts/run_hive_to_csv.py" --sql "select * from db.t limit 100" --name 退款明细

# ③ 只把用户贴的 SQL 存成文件，先不执行
python3 "$SK/scripts/run_hive_to_csv.py" --save-only --sql "..." --name 退款明细

# ④ 看看之前归档过哪些 SQL
python3 "$SK/scripts/run_hive_to_csv.py" --list-sql

# ⑤ 重跑一个归档过的查询：只给名字即可，自动取同名里最新的那份
python3 "$SK/scripts/run_hive_to_csv.py" --sql-file 退款明细

# 指定输出路径 / 覆盖默认库
python3 "$SK/scripts/run_hive_to_csv.py" --sql-file q.sql --out /tmp/r.csv --database dw
```

### 从文件执行

`--sql-file` 接受两种值，脚本自动区分：

- **真实路径**（绝对或相对）——直接读。
- **归档名**（如 `退款明细`）——在 `<技能目录>/sql/` 下找 `<名字>.sql`，
  或按 `<名字>_<时间戳>.sql` 前缀匹配，取**最新**的一份。找不到会报错并提示用 `--list-sql`。

用户说「我之前存的那个查询再跑一遍」但没说清是哪个时，先 `--list-sql` 把归档列出来让他挑，别猜。

**用户在 IDE 里打开了 .sql 文件**（对话上下文里会有该文件路径）而没有另外贴 SQL 时，
默认就把那个文件当作要跑的 SQL，用 `--sql-file` 指过去；跑之前跟用户确认一句是不是跑这个文件。

### 用户贴 SQL → 生成 .sql 文件

用户在对话里贴 SQL（或让你写一段）时，**不要**把 SQL 内联塞进 `--sql` 就完事：
脚本默认会把它归档到 `<技能目录>/sql/<名字>_<时间戳>.sql`，让这次查询可复现、下次可复用。

- 用 `--name` 给一个**能看懂业务含义的中文或英文名**（`退款明细`、`order_funnel`），
  别用 `query1` 这种。不传 `--name` 时统一叫 `query`，可读性差。
- 名字里的空格和特殊字符会被清成下划线，中文保留。
- SQL 很长、或含引号/换行不方便走命令行时，**先用 Write 工具把它写成 `.sql` 文件再 `--sql-file` 跑**，
  比拼一条巨长的 `--sql` 命令行更可靠。这种情况下脚本不会重复归档。
- 只想存不想跑：`--save-only`。确实不想留档：`--no-save-sql`（少用）。

不传 `--out` 时，CSV 默认写到 `docs/hive-sql-to-csv/<来源名>_<时间戳>.csv`。
来源名取自 `.sql` 的文件名，所以 CSV 和它对应的 SQL 存档**同名可对齐**，方便回溯哪份结果来自哪段 SQL。

## 脚本行为要点

- **SQL 归档**：用户直接传字符串（`--sql` 或标准输入）时，先把原文写到 `<技能目录>/sql/`
  再执行；已经从 `.sql` 文件读的不重复归档。`--list-sql` 按修改时间倒序列出归档。
- **流式写盘**：用游标 `fetchmany`（默认每批 1 万行）边取边写，因此结果多大都不会把内存撑爆。
  可用 `--batch-size` 调批大小。
- **多语句脚本**：Hive 脚本常先 `set` 参数、`use` 库、建临时表，最后才 `select`。脚本按分号
  拆分（正确跳过注释和字符串里的分号），依次执行前置语句，把**最后一条产出结果的语句**
  （`select` / `with…select` / `show` / `describe` 等）的结果写进 CSV。若整段没有产出结果的语句，
  就只执行、不写 CSV，并如实说明。
- **CSV 格式**：带表头、纯 UTF-8（无 BOM）、逗号分隔；`NULL` 写成空串；含逗号/换行/引号的字段
  由 csv 模块自动加引号转义。表头列名会去掉 `别名.` 前缀（`t.col` → `col`）。
- **只跑一条结果语句**：如果脚本里有多条独立的 `select`，只有最后一条会落 CSV。用户要多个结果表时，
  分多次跑、或让用户把每条查询单独给你。

## 交付什么

跑完在对话里精简汇报：

- 结果**行数 × 列数**、**耗时**；
- **CSV 文件的绝对路径**；
- **归档 `.sql` 的路径**（这次是从用户贴的 SQL 生成的话），并说明下次可以用
  `--sql-file <名字>` 直接重跑；
- 结果的**列名**（多的话截断显示）。

结果很小（几行）时，可以顺手把 CSV 头几行贴进对话方便用户当场看；大结果只给路径和统计即可。

## 说明与边界

- 只负责「执行 + 落 CSV」。不改写、不优化 SQL；不判断字段业务含义或来源表（那是 `sql-field-lineage`）；
  不扫描文件里出现了哪些字段（那是 `field-search`）。
- 连接失败（网络不通 / 认证错 / 库表不存在）时，如实把 PyHive 的报错转达给用户，不要伪装成功。
- 大结果导出会持续占用 Hive 连接与本地磁盘，跑之前对明显没有 `limit` 的全表扫描可以提醒用户一句。

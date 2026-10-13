# Docket Agent

第一阶段：通过 CourtListener API 获取指定案件的条目，保存原始 JSON。
项目按 ingest / store / analyze / notify 分层；目前实现 ingest、SQLite 基线与变化检测、单次运行入口；分析和通知仍为占位文件。

## 运行

```bash
cd /Users/terrychen/Desktop/Lawfirm_AgentWorkflow
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Only create .env if it does not already exist; preserve private local settings.
if [ ! -e .env ]; then cp .env.example .env; fi
# 在本地编辑 .env，填入 COURTLISTENER_TOKEN；不要提交或分享 Token。
python -m src.main
```

默认读取 config.yaml 中的 docket 73132799：
https://www.courtlistener.com/docket/73132799/interactive-games-v-draftkings-inc/

结果保存为 data/73132799_entries.json。只有全部分页获取成功后才替换原快照；失败返回非零退出码，不把失败当作没有更新。首次运行未成功时不会生成快照。

.env 自动加载，已有环境变量优先。LLM_API_KEY 当前未使用。
401/403 需要检查 Token、接口权限或访问限制，不能假定有 Token 就一定能读取条目。

## 当前边界

- 只读取 CourtListener 已收录的 docket 条目，不获取 PDF 全文。
- 每次拉取完整快照，与 SQLite 中已保存记录比较；尚未实现 LLM、通知或后台定时运行。
- check_interval_minutes 只是预留配置，不会启动调度器。
- PacerMonitor 尚未接入。
- eval/labeled.jsonl 故意留空，待真实数据获取后手工标注。

官方接口文档：https://wiki.free.law/c/courtlistener/help/api/rest/v4/pacer-data

## 基线与更新检测

先把已有 JSON 导入为基线（不会调用 API，不会产生历史新增通知）：

```bash
python -m src.main --import-baseline
python -m src.main
```

没有基线时，第一次成功抓取自动建立基线。重复导入基线会跳过，不能覆盖已有状态。
SQLite 数据库位于 `data/docket.db`，它是监控状态的依据；JSON 是供查看的原始快照导出。

- `entries`：每个案件已见条目的最新原始数据和用于比较的关键内容。
- `changes`：新增或修改记录，含 before_json / after_json 和关联 run_id。
- `runs`：每次运行的来源、UTC 起止时间、状态、条目数、新增数、修改数、缺失数及错误。
- `dockets`：每个案件的基线建立时间。

修改比较包括条目描述、提交日期/时间、编号及关联文件描述、可用性、路径、正文和校验值。忽略索引更新时间、返回顺序等变化。缺失只计数并保留原记录，不视为删除。初次空快照拒绝建立基线。
整个响应获取及校验成功后，SQLite 事务一次提交记录、变化历史和成功状态。失败保留原基线。强制终止可能留下 running 状态，当前不包含调度、重叠任务管理或崩溃恢复。

测试不访问 API，使用独立内存数据库：

```bash
python -m unittest discover -s tests -v
```

## Email demo

Normal email screening now requires date_filed to equal today in America/Los_Angeles and a description beginning with a recognized court order/judgment or explicit court notice. Party motions and proposed orders are excluded. This is deliberately broad and can produce false positives; it is not LLM analysis or legal advice. Messages quote the source description and link to the entry API and docket. PDF text is not fetched.

First generate a historical demo preview (fresh fetch, up to five most recent matching records):

```bash
python -m src.main --email-demo
```

Preview files are saved under data/email_*.txt. Fill the SMTP and email variables listed in .env.example into your private .env. Never put real secrets in .env.example.

```bash
# Fetch, then send the historical demo to EMAIL_TO:
python -m src.main --email-demo --send-email
# Fetch, then send only important detected changes, including unsent older changes:
python -m src.main --send-email
```

The historical demo is marked as historical, not a new case development. It sends once per docket/sender/recipient. Normal mode sends one email per important change. First-ever baseline creation does not notify historical records. No 15-minute scheduler is installed.

SQLite email_outbox stores recipient, body, message ID, status and timestamps. Accepted means SMTP accepted the message, not confirmed inbox delivery. Existing outbox entries prevent duplicate attempts. An interrupted/failed SMTP attempt is left as sending/uncertain and must be reviewed manually before retry; this avoids duplicate mail when server acceptance is ambiguous. The demo does not provide automatic delivery retries or bounce handling.

### Today's court updates

Normal mode sends only detected new/modified records with today's date_filed and conservative court-origin wording. Source metadata does not prove authorship or issuance date; these are candidates for review. This can miss unusual court descriptions and late-arriving filings from previous dates. All entries are still stored and compared; only email selection is date-filtered. --email-demo remains an explicit historical preview option and is not part of the requested normal flow.

## PDF text extraction and OCR

Local extraction is available independently of the monitoring/email command:

```bash
# macOS
brew install poppler tesseract
# Ubuntu / GitHub Actions runner
sudo apt-get update && sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-eng

python -m src.ingest.pdf /path/to/filing.pdf --output-dir data/ocr
# Force OCR for mixed image/text pages or an unreliable existing text layer:
python -m src.ingest.pdf /path/to/filing.pdf --force-ocr --source-url https://example.org/filing.pdf
```

Requires Python 3.10+ and the external executables `pdfinfo`, `pdftotext`,
`pdftoppm` (Poppler), and `tesseract`. No additional Python packages are required.
Default language is English; `--language eng+chi_sim` requires the corresponding
Tesseract language data to be installed.

Each page first uses native text extraction. Pages with fewer than 40 alphanumeric
characters fall back to OCR. Rendering targets 300 DPI with the longest dimension
capped at 5000 pixels. Output is a new directory containing `document.json` and
`document.txt`; JSON includes the source file, optional reference URL, SHA-256,
page numbers, extraction method, text and warnings. The reference URL is metadata
only. Temporary page images are removed automatically. Existing outputs are retained.

Defaults: 50 MiB maximum input, 200 pages, and a 120-second timeout per external
command. The Python `extract_pdf()` API accepts custom limits; the CLI supports
`--max-pages`. A failed page aborts extraction with a nonzero exit rather than
publishing a partial document. Empty pages are flagged for manual review.

OCR text is not verified evidence: recognition can misread dates, names and numbers,
and native text can omit embedded images. Use `--force-ocr` where appropriate and
review the source PDF. Layout/table reconstruction and handwriting accuracy are
not guaranteed. This module does not download CourtListener attachments or modify
the current email flow; its page-numbered output is ready for a later analysis step.


## 云端配置准备（尚未部署）

本地继续使用已有 `.env`；不上传、不复制到仓库或 Actions artifacts。
`.env.example` 只提供配置变量名和非敏感默认值。现有程序通过环境变量
读取配置，`load_dotenv` 不覆盖已经设置的环境变量。

计划在 GitHub 私有仓库的 Settings → Secrets and variables → Actions
中，由项目所有者自行填写以下配置。不要将真实值写进 README、workflow、
终端命令或日志。

| 变量 | GitHub 配置位置 | 用途与当前状态 |
| --- | --- | --- |
| `COURTLISTENER_TOKEN` | Secrets | API 抓取需要，当前已使用 |
| `SMTP_USERNAME` | Secrets | SMTP 用户名，发送邮件时需要 |
| `SMTP_PASSWORD` | Secrets | SMTP 凭据，发送邮件时需要 |
| `EMAIL_FROM` | Secrets | 发件地址，作为私人信息保存 |
| `EMAIL_TO` | Secrets | 收件地址，作为私人信息保存 |
| `DATABASE_URL` | Secrets | 可选：PostgreSQL 连接 URL；留空使用本地 SQLite |
| `LLM_API_KEY` | Secrets | 预留：模型服务密钥，当前未使用 |
| `SMTP_HOST` | Variables | 非敏感 SMTP 服务器地址 |
| `SMTP_PORT` | Variables | SMTP 端口，465 或 587 |

后续 workflow 需要显式把 Secrets 和 Variables 映射到同名环境变量；
仅在 GitHub 中保存配置，并不会自动传入 Python 程序。
案件编号继续放在 `config.yaml`。其中 `check_interval_minutes` 当前仍是
预留项；未来定时频率由 workflow 的 schedule 配置控制。

未设置或留空 `DATABASE_URL` 时，存储仍是 `data/docket.db`（SQLite）。设置有效的
PostgreSQL URL 后，程序使用 PostgreSQL，并在首次连接时创建缺少的表。
已有 SQLite 数据不会自动迁移；迁移或明确建立新基线需要另行处理。
当前已提供仅运行测试的 Actions workflow，尚未启用定时监控或接入 LLM。

上传范围为源代码、测试、依赖清单、配置样例、文档和后续工作流。
本地 `.env`、`.venv`、数据库、原始快照、邮件预览和 OCR 输出不提交；
不要把整个工作目录打包上传为 artifact。需要保留的历史状态应单独迁移。


## SQLite / PostgreSQL 存储后端

安装更新后的 `requirements.txt`，其中包含 `psycopg[binary]`。
`DATABASE_URL` 接受 `postgresql://` 或 `postgres://` URL；真实连接字符串由
所有者私下配置，使用数据库提供方要求的 TLS 参数。未安装驱动、URL 无效或
连接失败都会报错退出，不会静默退回 SQLite。连接超时为 10 秒。

`Store(path)` 保留 SQLite 用法；`Store(database_url=...)` 使用 PostgreSQL。
两种后端共享基线、变化历史、运行状态和邮件 outbox 逻辑。
PostgreSQL 使用显式事务、自动生成的 run/change ID 和案件级事务锁；
SQLite 在事务开始时获取写锁。外部 API 和 SMTP 调用不放在数据库事务内。
配置连接后首次运行需要建表权限；当前只创建缺少的表，不修改既有表结构，
也不提供已有数据库的自动迁移或版本升级机制。

数据库错误对用户仅输出固定错误消息，不输出连接 URL 或驱动原始异常。
SMTP 的 sending/uncertain 状态仍需人工检查，不能保证严格的 exactly-once 投递。

离线测试不读取 `.env`，使用临时 SQLite 和模拟 PostgreSQL 接口；
模拟测试不能替代真实 PostgreSQL 的 SQL、事务和并发验证。


## GitHub Actions 测试工作流

`.github/workflows/tests.yml` 提供两个独立 job：

- 离线测试：Python 3.11，运行 `python -m unittest discover -s tests -v`。
- PostgreSQL 集成测试：启动临时 PostgreSQL 16 服务，运行
  `python tests/integration/postgres_checks.py`，验证基线、变化检测、回滚、
  重连后去重、并发基线建立及并发邮件领取。

上传到 GitHub 后，push、pull_request 或手动 workflow_dispatch 会触发测试。
该工作流没有 schedule，不调用抓取入口，不发送真实邮件，不需要 GitHub
Secrets，也不读取 `.env`。邮件使用模拟 transport，测试数据全部为虚构数据。
权限仅为 contents: read；没有数据或工作目录 artifact 上传步骤。
每个 job 超时为 10 分钟，服务容器由 GitHub 在 job 结束后清理。
OCR 单元测试模拟外部命令；此工作流尚不包含真实 OCR 引擎的集成验证。

集成脚本仅接受 `TEST_POSTGRES_PORT`，连接 localhost 上的专用 `lawfirm_test`
数据库，使用固定测试账号。它会在每项测试前清空该测试库的表，禁止用于
存放真实数据的数据库。脚本不会读取生产用 `DATABASE_URL`。本地复现时，
先启动隔离的 PostgreSQL 16 容器（数据库 lawfirm_test、用户 postgres、
密码 local-test-only），再将容器映射到本机的端口传给脚本：

```bash
TEST_POSTGRES_PORT=你的临时容器端口 python tests/integration/postgres_checks.py
```

未提供有效测试端口时脚本直接失败，避免集成测试被悄悄跳过。
普通 unittest discover 不执行该集成脚本。本地验证不等于 GitHub 托管 runner
已经运行成功；上传后仍需检查 Actions 页面上的两个 job。

参考：[GitHub PostgreSQL service containers](https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers)。


## 手动检查云数据库连接

`Check cloud database` 工作流仅支持手动触发。先由所有者在 GitHub repository
secret 中保存 `DATABASE_URL`，上传代码后在 Actions 中选择该工作流，点击
Run workflow。它不会因 push、PR 或定时事件自动运行。

该检查调用 `python -m src.store.check_connection`，不读取 `.env`，不实例化会
自动建表的 `Store`。它只执行只读事务设置、查询超时设置和 `SELECT 1`。
未指定 sslmode 时强制 require；保留 verify-ca / verify-full；拒绝允许明文的
模式，并检查客户端到所选数据库端点的 TLS 是否启用。该检查不证明 pooler
到数据库的链路、建表权限或业务功能；require 也不等于完整证书身份验证。

连接超时 10 秒，查询超时 10 秒，工作流超时 5 分钟。
日志仅输出固定成功/失败消息，不打印 URI、密码或驱动异常详情。
本地单元测试使用模拟连接，不会访问真实云数据库。原有 Tests 工作流继续
使用虚构数据和临时 PostgreSQL，不会取得这个 Secret。


## 手动初始化云数据库

上传后，在 Actions 中选择 `Initialize cloud database` → Run workflow，
使用已保存的 `DATABASE_URL` 创建缺少的五张表：`dockets`、`entries`、`runs`、
`changes`、`email_outbox`。该 workflow 仅支持手动触发，不读取 `.env`，不抓取
案件、不导入本地数据、不发邮件，也不建立案件基线。

入口为 `python -m src.store.init_database`。缺少连接配置直接失败，不会退回
SQLite。强制 TLS，保留更严格的证书验证；限定 public schema，并设置语句和锁
等待超时。账号需要建表权限。复用 Store 的事务建表逻辑，重复运行保留已有表
及记录；这不是 schema 升级工具，不会修复结构不兼容的已有表。

日志只显示固定成功或失败信息，不输出连接字符串。初始化成功后仍需另行批准
首次抓取/基线建立。Supabase Data API 应继续保持关闭；这一步不设置 API 权限。


## 手动验证云端存储读写

`Check cloud storage` 工作流只允许手动触发，使用 repository secret
`DATABASE_URL`。入口 `python -m src.store.check_storage` 不读取 `.env`。
它在 public schema 创建唯一命名的 `_storage_probe_<随机标识>` 普通测试表，
写入一条虚构记录并提交，关闭写入连接后使用新连接读取核对，最后删除该表，
并检查表已不存在。使用普通表而非 PostgreSQL TEMP 表，才能跨连接验证提交。

不访问或修改五张业务表，不抓取案件、不发送邮件、不建立基线。
要求 TLS 及 CREATE/DROP 测试表权限；保留更严格的证书验证设置。
每个连接超时 10 秒、查询超时 10 秒、锁等待 5 秒。成功必须同时完成读取验证
和清理确认。失败时只输出固定阶段说明，不输出凭据或原始驱动错误。

连接异常时会尽力清理；强制取消或 runner 被终止可能留下测试表。
清理失败日志会显示本次唯一测试表名称，供人工检查；没有自动清理其他表的
逻辑。该检查证明跨连接读写与提交成功，不验证备份、灾难恢复或长期可用性。
上传后由所有者在 Actions → Check cloud storage → Run workflow 手动运行。


## 手动抓取案件并保存云端基线

`Check dockets manually` 工作流仅支持手动运行，没有 schedule。
所有者需先配置两个 repository Secrets：`DATABASE_URL`（已验证的 Session
pooler URI）和 `COURTLISTENER_TOKEN`（现有 CourtListener API Token）。
不要把 Token 或连接字符串提交到仓库。无需 SMTP 或 LLM 密钥。

上传后，在 Actions → Check dockets manually → Run workflow 运行。
入口 `python -m src.main --cloud-check` 读取 `config.yaml` 中的案件编号，
当前为 73132799。它不读取 `.env`，必须连接 PostgreSQL，不会退回 SQLite；
缺少任一 Secret 时在抓取前失败。连接强制 TLS，保留严格证书验证选项。

首次成功的非空快照建立基线，不生成历史新增事件。后续手动运行比较变化，
将新增/修改及运行统计保存到数据库。失败保留原基线。它不会迁移本地 SQLite
历史，也不发送邮件、不生成邮件预览、不导出本地 JSON、不下载 PDF 或运行 OCR。
`--cloud-check` 不能与导入基线或任何邮件选项组合。

日志显示案件编号、run ID、抓取/新增/修改/缺失数量；云模式失败仅记录异常类型
或 HTTP 状态码，不输出原始异常或凭据。401/403 通常需要检查 Token 或接口权限。
GitHub job 限时 15 分钟，同一监控组串行运行，不取消正在进行的检查。
强制终止可能留下 running 记录；自动恢复队列仍未实现。

预期验证：第一次成功显示 baseline established；再次运行且源数据未变化时，
added 和 modified 均为 0。是否出现真实更新取决于 CourtListener 数据。
当前本地测试使用模拟 API 和内存 SQLite，不调用真实 CourtListener 或 Supabase。

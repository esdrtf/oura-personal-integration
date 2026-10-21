# Personal Oura → Google Drive export

个人 Oura v2 数据归档。GitHub Actions 每天执行，不依赖 Mac 开机。Python 3.11+ 标准库，无 pip 依赖；仅用 GitHub 托管 runner 自带的 `gh` 加密更新 OAuth secret。

**代码部署不等于数据同步完成。** 首次安装必须配置 Secrets、运行 backfill，并确认工作流成功及 Drive 的 manifest/10 月记录。仓库不包含健康数据或凭证。

## Drive 结构与读取约定

| 路径 | 用途 |
|---|---|
| `raw/<collection>/<sha256>.json` | 每次成功 Oura API HTTP 响应的完整原始字节；包括所有字段、数组、分页游标。不保存 OAuth token 响应 |
| `raw/runs/<sha256>.json` | 请求日期、分页、原始文件引用、导出时间 |
| `raw/manifests/<sha256>.json` | 旧 manifest 及每次新 manifest 的不可变快照 |
| `normalized/<collection>/<sha256>.jsonl` | 保留完整记录字段及原始 ID 的不可变 collection 快照，一行一个对象 |
| `normalized/<collection>.jsonl` | 方便人工读取的最新副本 |
| `derived/snapshots/<sha256>.csv` | 与 manifest 一致的日汇总快照 |
| `derived/daily_summary.csv` | 最新日汇总，一天一行 |
| `manifest.json` | 当前发布指针及每个 collection 的同步状态 |
| `history/`、`changes/`、`profile.json`、根目录旧导出 | 原样保留；不移动、不删除、不覆盖 |

Health Coach 应先读 `manifest.json`，再按 `derived.daily_summary.drive_id` 或 `collections.<name>.file.drive_id` 读取对应快照。固定文件名只是便捷副本；若上传中断，它可能落后于 manifest。每个引用包含 `path`、`drive_id`、`sha256`、`bytes`。

顶层字段：`generated_at`、`source`、`timezone`、`schema_version=2`、`status`、`coverage_notes`、`not_exported`。每个 collection 均有 `last_record_day`、`last_success_at`、`record_count`、`status`、`coverage_notes`、`not_exported`，另有查询窗口、成功游标和文件引用。

- `ok`：本次完整请求成功且返回记录；`empty`：完整请求成功但窗口为空；`error`：请求、解析或上传失败，保留旧成功数据和游标。顶层 `partial_failure` 会令 Actions 失败。
- `last_record_day` 是真实记录日，`generated_at` 是导出日；二者不能互换。无日期的 profile/configuration 为 `null`。
- 覆盖 19 个 collection：`daily_activity`、`daily_cardiovascular_age`、`daily_readiness`、`daily_resilience`、`daily_sleep`、`daily_spo2`、`daily_stress`、`enhanced_tag`、`heartrate`、`personal_info`、`rest_mode_period`、`ring_battery_level`、`ring_configuration`、`session`、`sleep`、`sleep_time`、`tag`、`vO2_max`、`workout`。
- 请求不传 `fields`，保留 Oura 返回的全部字段。App 中有而 API 没有的指标不虚构；403/404 不伪装为空。

## 去重、增量与恢复

1. 有 ID 的记录按 **collection + Oura ID** 去重；同 ID 新响应替换 normalized 中旧版本，原始旧响应继续保留。不同 ID 即使日期相同也保留。
2. Oura 心率与电量样本没有原生 ID。心率使用 UTC 时间戳 + source，电量使用 UTC 时间戳作为内部去重键；不向记录注入伪造 ID。其他无 ID 记录按完整内容哈希去重。
3. 正常增量从每个 collection 上次成功查询边界往前 14 天开始，补入延迟同步/修订；长期停机不会只取最近两周。默认首次起点为 `2026-10-01`。更早历史通过手动日期范围补数。
4. 每 7 天一个 API 窗口，逐页取到 `next_token` 为空。跨窗口边界的重复记录按 ID 合并；请求可能包含服务端边界日，原始和 normalized 不丢弃它。
5. 旧 schema v1 的 `history/YYYY-MM.json` 和 `profile.json` 会读取、去重并迁入 normalized。旧原始 HTTP 响应无法事后重建，因此不会把历史聚合文件冒充 raw 响应。
6. Drive 创建使用预生成 file ID，网络重试不会制造同名副本。已有同名冲突则停止，避免猜错文件。快照按内容寻址，重跑相同数据不新增快照。
7. 写完快照才更新 manifest，发布后回读校验 SHA-256、数量、ID 唯一性与 daily_summary。发现旧任务同时改 manifest 就终止；**迁移时必须先停用旧写入任务**。检查不是跨服务分布式锁，不能让两套导出同时写根目录。
8. 不根据 API 未返回某 ID 就删除历史；源端删除不会传播。需要物理删除隐私数据时应单独明确处理归档及版本。

## 日汇总口径

- 日期使用 Oura `day`；仅时间戳记录换算 `America/New_York`，正确处理夏令时。包括已存历史及请求区间中没有记录的日期；缺失指标留空，列出 `missing_collections`，不填 0 或前值。
- `is_complete_day` 只说明当地自然日是否结束，**不保证该日设备已同步完整**。
- 包含睡眠/准备度/活动分、步数、活动和总热量、HRV、睡眠心率、呼吸、温差、SpO2、睡眠阶段秒数、压力/恢复秒数、血管年龄、resilience、训练记录及时间。
- 主睡眠优先 `long_sleep`，再取最长时段；不把多个算法版本、午睡或不同睡眠 ID 相加。全部 ID 与 `quality_notes` 保留供追溯。睡眠心率是 API 原值，可能与 App 聚合显示略不同。
- `workout_count` 是去重后的 Oura 记录数；重叠训练时长取区间并集。没有训练记录时留空，不直接认定未运动。
- `failed_collections` 是本次导出失败集合，不表示每个历史日都缺失。

## 一次性配置

GitHub 仓库 Settings → Environments 创建 **`oura-export`**。将以下全部作为该环境的 Secrets 保存；**`OURA_TOKEN_JSON` 必须是环境 secret**。

| Secret | 内容 |
|---|---|
| `OURA_CLIENT_ID` | Oura OAuth 应用 client ID |
| `OURA_CLIENT_SECRET` | Oura OAuth 应用 client secret |
| `OURA_TOKEN_JSON` | 含 `refresh_token` 的 token JSON，可直接使用有效的 `~/.oura-mcp/tokens.json` |
| `GOOGLE_CLIENT_ID` | 拥有目标 Drive 访问权的 Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | 同一 Google OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | 该 Google 用户授予离线 Drive 读写访问的 refresh token |
| `GH_SECRETS_TOKEN` | fine-grained GitHub PAT，仅授权本仓库，repository **Environments: read/write**，用于保存轮换后的 Oura token |

环境 Variable：`DRIVE_FOLDER_ID` = 原有 Oura 目录 ID。目录 ID 不写入公共仓库。部署账户需要创建环境、管理 Secrets/Variables 的权限。`GH_SECRETS_TOKEN` 不需要代码写权限；其过期后要替换。

### Oura OAuth

Oura refresh token **仅能用一次**。运行前先用原值测试 secret 写权限，再刷新，并立即把新 access/refresh token JSON 存回同一个环境 secret。OAuth 刷新请求不自动重试；网络结果不确定时停止，可能需要重新授权。不存在跨 Oura 与 GitHub 的原子事务：若刷新成功后 GitHub 持续不可用，仍可能需要重新授权。

不要同时让旧 MCP 和 Actions 消费同一个 refresh token；先停旧消费者，或为云端导出单独完成一份授权。请求覆盖所需 scopes：`personal daily heartrate workout tag session spo2 ring_configuration stress heart_health`（以应用授权页实际提供为准）；email 仅在确需导出邮箱时授予。权限不足在 manifest 明确报错。

使用 job-level concurrency 且 `cancel-in-progress: false`，避免中途取消 token 轮换。环境 secret 在 **job 开始**时加载，避免排队工作流读取旧的 repository secret 快照。不要设置会阻止每日无人值守执行的环境人工审批规则；只允许可信 `main` 部署。不要给 fork/PR job 提供这些 secrets。

### Google Drive 凭证

ChatGPT 的 Google Drive 连接授权不能直接拿给 GitHub Actions。需要自己的 Google OAuth 客户端和 refresh token：

1. 在自己的 Google Cloud 项目启用 Drive API，配置 OAuth consent，创建 **Desktop app** 客户端并下载 JSON。
2. 通过官方 OAuth 客户端流程取得用户离线授权（`access_type=offline`，必要时 `prompt=consent`）。现有旧目录需要相应读写 scope；普通 `drive.file` 只允许该应用创建/明确授权的文件，可能无法访问旧历史。若使用完整 `https://www.googleapis.com/auth/drive`，授权范围是该账户 Drive；脚本本身只操作配置的根目录。
3. 外部 OAuth 应用若处于 **Testing**，非基础 profile scope 的 refresh token 通常 7 天过期；用于长期运行需完成适用的 production 配置/要求。`docs/` 提供公开应用说明、隐私政策及使用条款；可通过 GitHub Pages 的 `main /docs` 发布，并将实际网址填入 Branding。按 Google 要求验证域名，再切换发布状态；网站只包含文档。
4. 准备本地 `authorized_user` JSON，含 `client_id`、`client_secret`、`refresh_token`。不提交此文件。此实现使用用户 OAuth，不使用个人 My Drive 中没有存储配额的 service account。

如果已有两边凭证，可在保存旧 Oura 凭证的电脑上运行一次（之后电脑无需在线）：

```bash
gh auth login
git clone https://github.com/esdrtf/oura-personal-integration.git
cd oura-personal-integration
python3 configure_secrets.py --google-authorized-user /absolute/path/to/google-authorized-user.json
```

辅助脚本读取 `~/.oura-mcp/config.json`、`~/.oura-mcp/tokens.json`；缺 client 值时隐藏输入。支持 `--oura-config`、`--oura-tokens` 指定其他路径。所有敏感值经 stdin 交给 `gh secret set`，不进入命令参数或日志。脚本不提取浏览器 cookies，不读取 ChatGPT 连接凭证，不代替用户授权。

### 没有旧 refresh token 时：一次性云端授权兑换

`Initialize OAuth credentials` 工作流可在 GitHub runner 内完成授权码兑换，避免把客户端密钥下载到其他电脑。先配置对应 provider 的 client ID/secret 和 `GH_SECRETS_TOKEN`。

1. 由授权发起方生成独立的随机 `state` 和 PKCE verifier（43–128 字符），在官方授权 URL 中传入 `state`、`code_challenge=BASE64URL(SHA256(verifier))`、`code_challenge_method=S256`。Google 使用 Desktop app、loopback redirect、`access_type=offline` 和 `prompt=consent`；Oura 使用应用已经登记的 redirect。
2. 用户在官方授权页确认权限。保留完整的 loopback callback URL；它含一次性授权码，不要贴入聊天、issue、日志或 workflow input。授权发起方应尽快处理，授权码会过期。
3. 将 `provider`（`oura` 或 `google`）、`callback_url`、原始 `redirect_uri`、预期 `state`、`code_verifier` 组成 JSON，保存为 `oura-export` 环境 Secret **`OAUTH_BOOTSTRAP_JSON`**。这些字段必须来自同一次授权。
4. 手动运行 **Initialize OAuth credentials**，选择匹配的 provider。脚本先验证 callback/state，再确认 Secrets 写权限，授权码只兑换一次；新凭证直接保存为 `OURA_TOKEN_JSON` 或 `GOOGLE_REFRESH_TOKEN`，成功后删除临时 Secret。重复失败时重新发起授权，不盲目重复兑换旧码。
5. 两边初始化成功后，再运行正常 backfill。初始化工作流不读写健康归档；它与导出共用 concurrency，避免 token 竞争。云端保存有重试，仍无法把 OAuth 发行与 GitHub 写入变成跨服务原子事务。

需要重新授权时也可使用本流程。不要重置原应用 client secret 来代替找回凭证，除非已经确认其他消费者的影响。

## 首次补数与验收

Actions → **Oura to Google Drive** → Run workflow：

- `start_date`: `2026-10-01`
- `end_date`: 留空，自动截至纽约当地明天（包含今天）
- `verify_from`: `2026-10-01`

或：

```bash
gh workflow run oura-export.yml --repo esdrtf/oura-personal-integration \
  -f start_date=2026-10-01 -f verify_from=2026-10-01
gh run list --repo esdrtf/oura-personal-integration --workflow oura-export.yml --limit 5
gh run watch RUN_ID --repo esdrtf/oura-personal-integration --exit-status
```

成功必须同时满足：

1. Actions `Export and verify Drive readback` 通过；不能只看测试 job。
2. 原目录 `manifest.json` 的 `generated_at` 更新、`schema_version=2`、顶层 `status=ok`；19 个 collection 均有明确状态。
3. `daily_sleep`、`daily_readiness`、`daily_activity` 的 `last_record_day >= 2026-10-01`；`--verify-from` 自动执行该最低验收。它不保证每天都有测量，更不保证截至今天都有同步。
4. `derived/daily_summary.csv` 出现 2026-10 日期和真实指标，缺失日保留空值；旧 `history/`、`changes/`、`profile.json` 的内容不变。
5. 同范围重跑 normalized 数量、ID 唯一性保持稳定；新同步修订可能改变字段值但不会重复累计同 ID。

每日运行在 **12:17 UTC**（纽约夏令时 08:17、冬令时 07:17），早于 09:00 Health Coach。GitHub 定时可能排队延迟，且公共仓库长时间无活动可能禁用 schedule；在 GitHub 通知设置中订阅 Actions 失败通知。

## 失败定位

| 信号 | 处理 |
|---|---|
| `Missing secret/configuration` | 检查 `oura-export` 环境 Secrets 和 Variable；不要把密钥贴进日志 |
| `Cannot persist Oura tokens` | 检查 PAT 到期、仓库限制及 Environments 写权限；发生于刷新前则没有消耗 token |
| `Oura refresh failed` | 旧 token 失效、被其他程序消费，或刷新响应丢失；重新 OAuth 授权后替换环境 token JSON |
| OAuth 400/401 | 日志只显示白名单中的标准错误代码；`invalid_client` 检查 ID/secret 配对，`invalid_grant` 检查过期/已消费的授权码、redirect、PKCE、refresh token 或授权撤销 |
| collection HTTP 403/404 | 检查 Oura scope、会员/API 支持；旧数据保留，不标为成功 |
| 429/5xx/网络错误 | 数据请求最多 5 次指数退避，尊重 Retry-After（单次最多 60 秒）；耗尽后 job 失败 |
| `Manifest changed` | 停止其他写入者，再重跑相同范围 |
| 重复 Drive 文件名/校验不符 | 停止并核对冲突；不要删除旧历史来“修复” |
| manifest 成功但副本更新失败 | 已发布快照仍有效；使用 manifest 文件 ID，重跑恢复便捷副本 |

日志只输出 collection、状态和安全错误码，不输出 token、URL 查询、原始身体数据或响应正文。健康数据也不上传 GitHub Actions artifacts。请求可重试；不盲目自动重跑整个 OAuth 轮换。

本地测试：

```bash
python3 -m unittest discover -s tests -v
```

测试使用合成数据，覆盖历史迁移、幂等补数、完整分页、失败后游标/旧数据保留、时间边界、ID 去重、token 顺序、校验及并发写入检测。

## Privacy Policy / Terms

This is a personal integration, not a public service. It reads only the owner's authorized Oura data. Credentials are encrypted in GitHub Actions environment Secrets; GitHub-hosted runners temporarily process the data and upload it to the owner's configured Google Drive folder. No credentials or health exports belong in this public source repository or its Actions artifacts. Local MCP usage may separately process data through the owner's chosen agent services. Access can be revoked in Oura, Google, and GitHub. Historical Drive archives remain until the owner explicitly removes them.

Unofficial; not endorsed by Oura. Informational use only, with no diagnosis or treatment. API availability and the connected services' terms apply.

## Official references

- [Oura OAuth and single-use refresh tokens](https://cloud.ouraring.com/docs/authentication)
- [Oura v2 API](https://cloud.ouraring.com/v2/docs) — endpoint inventory checked against OpenAPI 1.42
- [GitHub secret loading timing](https://docs.github.com/en/actions/reference/security/secrets)
- [GitHub environment secrets API and permissions](https://docs.github.com/en/rest/actions/secrets)
- [GitHub CLI secret set](https://cli.github.com/manual/gh_secret_set)
- [Drive pre-generated IDs and safe retries](https://developers.google.com/workspace/drive/api/guides/create-file)
- [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)

# 仓储 AMR 竞品情报系统

面向「智能制造 / 智慧物流机器人」赛道，聚焦 **仓储 AMR** 方向的情报采集与监控骨架。

## 核心设计原则

**一条数据只有一个正规入口。** API 能拿的永远走 API，API 拿不到的才退到爬虫；
两条通路进来后归一到同一张事件表，用 `source_type` 区分。

```
【API 通路】                       【爬虫通路】
上市公告 (AkShare)                 招标/中标 (ccgp，限频)
                                 官网变更 (watch_targets)
        ↓                                ↓
        └──────── 统一事件表 events ──────┘
                        ↓
                相关度打分 → 日报 / 告警
```

## ⚠️ 实测结论（重要，别跳过）

本项目在 2026-10 对目标源做了真实验证，结论与常见教程的假设**不一致**，已在
`config/sources.yaml` 的 `status` 字段逐条记录：

| 源 | 实测结果 | 处理方式 |
|---|---|---|
| **上市公告 (AkShare/东财)** | ✅ **最稳**，结构化无风控，300532 单次返回 130 条 | **已接入，主力数据源** |
| **股本变动 / 融资信号 (AkShare)** | ✅ 交易所公开披露，免费结构化。含「增发新股」「股权激励」 | 已接入，仅覆盖上市公司 |
| **行业媒体（4 源）** | ✅ **一源覆盖三条线**：新品、融资、大单 | 已接入 |
| ├ 中国机器人网 / OFweek机器人 | 综合机器人媒体 | 191 条 |
| └ **AGV-AMR网 / 新战略机器人** | **移动机器人垂直媒体**（命中率显著更高） | 212 条 |
| **工信部政策文件** | ✅ 信噪比最高（制造业数字化转型/绿色工厂） | chromium 渲染 + 专用词表 |
| 中国政府采购网 (ccgp) | ✅ 可用，但 HTTP 被拦必须走 HTTPS；**有 IP 级频率限制** | HTTPS + 关键词间等待 60s + 分页 |
| ccgp 中标详情页 | ✅ **无有效 robots.txt**（返回 404 错误页，非真实规则） | 逐条解析，支持 3 种表格版式 |
| 竞品官网（海柔/快仓/极智数通/仙工） | ✅ 已实测有效，变更检测可触发 | 已接入 |
| 中国政府网-最新政策 | ⚠️ 可用但**信噪比低**（多为民生/政法类） | chromium 渲染，靠词表过滤 |
| 今天国际官网资讯 | ❌ Nuxt SPA，`/news`、`/xinwen`、`/about/news` 均 404 | 改由**上市公告**覆盖 |
| 国家发改委 政策 | ❌ **robots.txt 明确禁止抓取** | 合规跳过 |
| **大项目招投标平台** | ❌ **全国平台需登录 / JS 逆向**，详见下节 | **未接入** |
| 高工机器人 gg-robot.com | ❌ HTTP 503 | 排除 |
| 物流技术与应用 | ❌ 域名解析失败 / 404 | 排除 |
| 亿欧 iyiou.com | ❌ HTTP 202 反爬 | 排除 |
| 罗戈网 logclub.com | ❌ 跳转验证码页 | 排除 |

### 竞品识别的两个坑（已修）

**坑 1：短名误命中。** 竞品名单里有「快仓」「仙工」「国自」等简称，若不校验长度，
任意文本都可能误命中。现在规定：**2 字中文词只有在已知别名表中才参与匹配**。

**坑 2：自身主体被当成竞品。** 今天国际的 131 条上市公司公告，标题必然写
「今天国际:关于…」。早期版本把这些全部标成了「竞品命中」——**公司提及自己
不是竞品动态**。修复方式：对 `announcement`/`finance` 这类**自有主体频道**，
匹配时排除该记录自身主体；媒体频道则不排除（稿件里的竞品名是被提及方）。

修复前后：竞品命中 154 条（含 131 条误报）→ **20 条真实命中**，覆盖 9 家竞品。

### 大项目招投标：调研结论（为什么不接入）

这是「竞品拿大单了吗」的关键缺口，我实际调研了主要平台：

| 平台 | 实测结果 |
|---|---|
| 中国招标投标公共服务平台 | 公告在 `bulletin.cebpubservice.com`，**静态与渲染后均无链接**，内容靠 JS 动态请求，需逆向接口（高成本、易失效） |
| 全国公共资源交易平台 | 导航为 `javascript:;`，同样依赖 JS 主动请求 |
| 中国采购与招标网 | 首页是资讯聚合，详情页明确显示 **「请登录 免费注册」**，项目数据在账号墙后 |
| 中国国际招标网 / 招标网 | HTTP 521，直接拒绝 |

**结论**：可行的公开渠道要么需登录，要么需逆向 JS 接口。前者涉及绕过访问控制（不做），后者成本高且随时失效。
**替代方案**：行业媒体（已接入）会报道「XX 拿下亿元大单」，能覆盖部分大单信息；纯商业项目本就无公示义务。

**核心教训：政府/招标类站点，"能跑一次"不等于"能稳定跑"。** 别把教程里的
URL 当成事实——先验证，再写代码。本项目开发中猜的官网子路径（海柔 `/resources/news`、
今天国际 `/news`、极智数通 `/cn/news`、仙工 `/news`）**全部 404**，已逐个校正为：
海柔 `/news`、仙工 `seer-robotics.ai/media`、极智数通 `blog.geekplus.com`。

**政策源必须用独立词表。** 政策标题几乎不会出现"AMR/AGV/仓储机器人"这类招标
品类词；若复用招标词表，实测会导致**全部政策被过滤**（21 条全丢）。因此
`policy_keywords.yaml` 按政策语汇（制造业/物流/新质生产力）单独设计。

### 已知局限

- **ccgp 限频**：60 秒强制间隔意味着招标情报无法实时。要日更中标数据需评估
  第三方数据服务 API —— 这正是"API 优先"原则的体现。
- **政策时效性**：政策文件通常滞后市场半年以上，优先级应低于竞品动态。

## 目录结构

| 路径 | 作用 |
|---|---|
| `config/competitors.yaml` | 竞品清单（三梯队）+ 官网监控点 |
| `config/tender_keywords.yaml` | 招标检索关键词、场景词、噪音词、竞品名 |
| `config/policy_keywords.yaml` | **政策专用词表**（与招标词表分离，原因见下） |
| `config/sources.yaml` | 信息源 + **实测状态标记** |
| `config/alerts.yaml` | 告警触发条件与渠道（凭据走环境变量） |
| `db/schema.sql` | SQLite 表结构 |
| `db/schema_alert.sql` | 告警去重表 |
| `db/schema_ui.sql` | 看板增量列（note/starred/updated_at） |
| `src/common/` | 数据库、抓取器、打分、工具 |
| `src/adapters/` | 采集适配器（**10 个**：上市公告、股本变动、行业媒体、招标、中标详情、官网变更、政策、正文抓取、报告同步、上下文导出、来源回补） |
| `src/report/` | 日报生成、告警推送（可选 LLM 摘要） |
| `scripts/dashboard.py` | **零依赖看板**（标准库实现，见下） |
| `scripts/` | 执行入口 + 告警端到端测试 |
| `data/` | 数据库、原始快照、日报 |

## 快速开始

```powershell
python -m pip install -r requirements.txt

# 可选：打通 JS 渲染的政策源（体积较大，约 150MB）
python -m pip install playwright
python -m playwright install chromium

python scripts/init_db.py                  # 1. 初始化数据库
python scripts/run_announcement.py         # 2. 上市公告（API 通路，最稳）
python scripts/run_award.py                # 3. 中标详情页 → 中标供应商
python scripts/run_tender.py 30            # 4. 招标/中标（注意限频）
python scripts/run_site_change.py          # 5. 官网变更（首次只建基线）
python scripts/run_policy.py               # 6. 政策栏目（需 chromium）
python scripts/make_report.py 7            # 7. 生成日报
python scripts/run_articles.py 200 media   # 8. 抓正文（供提炼用）
python scripts/run_llm_digest.py 200 media # 9. LLM 提炼（需 API key）
python scripts/send_alerts.py --dry-run    # 10. 预览告警
python scripts/sync_reports.py             # 11. 同步深度分析报告（见「旁路系统」）
python scripts/make_report_brief.py        # 12. 报告要点提炼（幂等）
python scripts/dashboard.py                # 13. 打开看板 http://127.0.0.1:8000
```

**一键全量**：`python scripts/run_all.py`（9 个阶段，含限频等待约 15 分钟）

## 看板（`scripts/dashboard.py`）

**零依赖** —— 只用 Python 标准库（`http.server` + `sqlite3`），不引入 Flask、无前端构建链。

```powershell
python scripts/dashboard.py            # 默认 http://127.0.0.1:8000
```

功能：筛选（竞品/频道/状态/时间/相关度）· 搜索 · 排序 · 标记已读 · 归档 · 加星 ·
备注（自动保存）· CSV 导出 · 统计卡片 · 14 天趋势。
仅监听 `127.0.0.1`，不对外暴露。

### 摘要提炼（`digest.py` / `article_adapter.py` / `llm.py`）

列表里每条记录分三块呈现：

```
┌──────────────────────────────┬─────────────────────────┐
│ 一句话摘要（大字）            │  提炼                    │
│  ↳ 对我意味着什么（绿字）     │  把这条信息摊开讲的      │
│  原始标题（小灰字）           │  2~4 句话（默认展开）    │
│  元信息 · 标签 · 操作按钮     │                          │
└──────────────────────────────┴─────────────────────────┘
```

**三步流水线**：

| 步骤 | 模块 | 产出 | 状态 |
|---|---|---|---|
| ① 抓正文 | `adapters/article_adapter.py` | `events.body`（各站正文选择器已实测适配） | ✅ 已跑通 110 条 |
| ② LLM 提炼 | `report/llm.py` | `llm_summary`（几句话）+ `llm_digest` + `llm_impact` | ⏸ 待配 key |
| ③ 规则兜底 | `report/digest.py` | 无 LLM 时的一句话摘要 + 影响判断 | ✅ 已生效 |

**无 LLM key 时**，右侧「提炼」区显示**正文前 400 字摘录**，仍然可读。

### 配置 / 轮换 LLM 密钥

密钥存放在 **`config/llm.local.yaml`**（明文，但**看得见、改得动、删得掉**，
且已被 `.gitignore` 排除）。优先级：**环境变量 > 本文件**。

```powershell
python scripts/set_api_key.py                    # 交互式设置（输入不回显）
python scripts/set_api_key.py --show             # 查看当前配置（key 打码）
python scripts/set_api_key.py --test-only        # 只验证当前 key 是否可用
python scripts/set_api_key.py --provider openai  # 切换服务商
```

脚本会**自动备份原配置**，并在写入后**发起一次真实调用**验证 —— 避免改错
缩进导致配置静默失效（表现为 LLM 步骤被跳过却不报错）。

支持的服务商（`--provider`）：`deepseek` / `openai` / `moonshot` / `dashscope` /
`zhipu` / `custom`（自定义 base_url）。任何兼容 OpenAI 协议的服务都能用。

> **安全提醒**：密钥是明文存储。切勿提交到版本库或分享；怀疑泄露时立即
> 用上面的脚本轮换。

```powershell
python scripts/run_articles.py 200 media      # 抓正文
python scripts/run_llm_digest.py 200 media    # LLM 提炼
```

**正文抓取的站点适配**（实测确认的容器）：

| 站点 | 正文容器 | 实测 |
|---|---|---|
| 中国机器人网 | `div.content` | ✅ |
| OFweek机器人 | `div.artical-content` | ✅ |
| AGV-AMR网 | `div.content` | ✅ |
| 新战略机器人 | `div.articleWrapper` | ✅ |

均需剥掉头部工具栏噪音（`打印 字号: 小 中 大 分享 34K`）与导航菜单。

列表里看到的**第一行是提炼后的一句话**，而不是原始标题 —— 扫一眼就知道发生了什么，
愿意深看再点「打开原文」。每条还带一句「对我意味着什么」。

**两层实现**：

| 层 | 覆盖 | 成本 | 状态 |
|---|---|---|---|
| **规则层** | 招投标（谁·多少钱·中什么标）、上市公告、股本变动、媒体、政策 | 免费、即时 | ✅ 已启用 |
| **LLM 层** | 规则拼不出的（需读懂语义才能提炼的媒体稿） | 需 API key | ⏸ 待配置 |

规则层产出示例：
```
【重大】今天国际 控股股东签署股份转让协议构成管理层收购
  ↳ 对手股权/控制权生变，战略与渠道可能调整，优先关注

上海百蝶教育科技 中标（30.5 万元）：天津滨海职业学院…医药智能仓储平台
  ↳ 已有厂商拿下该单，此项目机会窗口关闭

【竞品·极智数通】超百台AMR-极智嘉助力全球鞋服巨头威富集团打造全场景自动化仓库
  ↳ 信息直接涉及竞品，注意其动作方向
```

**启用 LLM 层**（可选，质量更好）：
```powershell
$env:OPENAI_API_KEY  = "sk-..."
$env:OPENAI_BASE_URL = "https://api.deepseek.com/v1"   # 可选，兼容 OpenAI 协议即可
$env:OPENAI_MODEL    = "deepseek-chat"
python scripts/run_llm_digest.py 200
```
结果存入 `events.llm_digest` / `llm_impact`，**展示时优先于规则层**；
未配置 key 时整体静默降级，不影响任何主流程。

### 筛选区的职责划分

界面上有两组控件，**职责不重叠**：

| 控件 | 维度 | 行为 |
|---|---|---|
| **顶部统计卡**（全部/今日新增/未读/已加星） | **状态维度** | 点击**清空**其余条件，只保留该状态 |
| **标签栏**（重大变更/融资动态/订单信号/新品发布/竞品命中/官网变更/中标/招标） | **内容维度** | 点击**叠加**到现有条件之上 |

**标签多选是 OR（任一命中），不是 AND。** 界面上有文字提示。
例：同时选「重大变更 + 竞品命中」= 两者并集（10 + 20 = 30 条）。

**标签上的数字会随其他筛选条件实时变化**，且保证等于「点下去会看到的条数」。
这是刻意的设计——此前数字取全量、点击却叠加了「近 7 天」等条件，
出现过「显示 10 条、点进去 0 条」的不一致。

**「已解析」不出现在界面上**：那是采集流程的内部状态（中标详情页是否抓过），
对判读情报无价值。

**注意**：看板不是常驻服务，电脑重启后需重新启动。
可用 `D:\Harness-spaces\start_dashboard.ps1` 一键启动（已在运行则直接开浏览器）。

## 旁路系统：competitor-intel（深度战略分析）

除了本系统（每日情报流），还有一套**按需触发**的深度分析系统，
基于开源项目 `WwaitW/competitor-intel` 改造。

| | 自建情报系统（本仓库） | competitor-intel |
|---|---|---|
| 定位 | 持续监控，每日情报流 | 按需深度分析，出战略报告 |
| 触发 | 每天 09:00 自动 | 手动 / 批量命令 |
| 数据源 | 政采、公告、媒体、政策（直采） | Tavily 搜索（全网） |
| 产出 | 一句话摘要 + 影响判断 | 完整 SWOT + 威胁分析 + 行动建议 |
| 成本 | 每天约 ¥0.1 | 每次约 ¥0.03 |

### 用法

```powershell
# Web UI（4 个 Tab：单竞品/批量矩阵/想法解析/知识库）
cd D:\Harness-spaces\competitor-intel
python -m streamlit run app.py          # http://127.0.0.1:8501

# 批量分析（目标从本项目的 competitors.yaml 读取）
python run_batch_analysis.py             # 第一梯队
python run_batch_analysis.py --tier tier2
python run_batch_analysis.py 海柔创新 快仓智能
```

报告输出到 `D:\Harness-spaces\competitor-reports\*.md`，
**分析完成后自动同步到本系统**（`channel='analysis'`，看板新增「深度分析」频道，
点「查看完整报告」阅读全文）。

每日任务也会自动同步一次（`scripts/sync_reports.py`，幂等）。

### 双向数据打通（核心融合点）

两套系统**不合并代码**（依赖与生命周期差异太大），而是**在数据层双向互通**：

```
方向 1：自建事实 → 深度分析（提升分析质量）
    自建库近期动态 → context_exporter → 上游 prior_context 参数
    注入 researcher 的 prompt，让分析"带着我方已知事实"进行

方向 2：搜索来源 → 自建库（补覆盖缺口）
    competitor-intel 的搜索结果 → source_import_adapter
    → events 表 channel='analysis_src'
    解决自建固定源覆盖不到的问题
```

**为什么需要方向 2（实测驱动）**：
自建系统的固定源覆盖率很低 —— 45 个竞品里**只有 10 个有数据**，
第一梯队核心的「海柔创新」在库里是 **0 条**（未上市、不走政采、媒体提及少）。
实测跑一次 competitor-intel 后，海柔从 0 条变成 6 条，
包括「递表港交所」「进军欧洲」「HaiPick Climb 新品」等直采抓不到的情报。

**为什么需要方向 1**：
competitor-intel 靠 Tavily 搜索，不知道我们每天直采的一手数据（政采中标、
上市公司公告）。注入后，它的 SWOT 能基于我方已知的最新事实，而非仅搜索结果。

**两条方向都在 `run_batch_analysis.py` 里自动完成**，无需手工干预：

```powershell
python run_batch_analysis.py 海柔创新
# 输出：
#   已注入内部上下文: 1/1 个竞品
#   已回补搜索来源 4 条到自建库
#   已同步报告到自建看板: {'inserted': 1, ...}
```

**看板新增两个频道**：
- `深度分析` —— competitor-intel 的完整报告，点「查看完整报告」阅读
- `分析来源` —— 深度分析时检索到的链接（`analysis_src`），标注「未经人工核验」

### 看板右侧「提炼」栏（各频道统一）

列表每条右侧都有一栏，**内容按频道来源不同**：

| 频道 | 栏标题 | 内容来源 |
|---|---|---|
| `media` | 提炼 | LLM 摊开讲的 2~4 句（`llm_summary`） |
| `analysis` | 提炼 | **深度报告的 定位/威胁/行动 要点**（`reports.brief`） |
| `analysis_src` | 来源说明 | 该链接的用途与可信度提示 |
| `site_change` | 变更提示 | 变了什么、为何需人工核对 |

**深度报告的提炼**由 `scripts/make_report_brief.py` 生成：
把 4000~6000 字的报告压成约 250 字的**定位 / 威胁 / 行动**三段式，
保留关键数字（市占率、融资金额、客户数），并给出可执行动作。

退化链路：`brief`（LLM 提炼）→ `strategy`（原文节选）→ `summary`，
任一层缺失都不会导致该栏空白。

```powershell
python scripts/make_report_brief.py        # 处理待提炼的（幂等）
python scripts/make_report_brief.py --redo # 改提示词后强制重做
```

### 本地改造

对上游做了 DeepSeek 接入与两处 bug 修复，完整清单见
`D:\Harness-spaces\competitor-intel\本地改造说明.md`。要点：
- `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 可配置，缺省仍指回 OpenRouter
- 修复上游 Key 预检失效（用公开端点校验，伪造 key 也报"有效"）
- 修复静默失败崩溃（HTTP 200 + error body 导致 `choices=None` → TypeError）

### 竞品名单共用

两套系统读**同一份** `config/competitors.yaml`，只需维护一处。
`competitor-intel` 用其中的 `tiers.tier1` / `tier2` 等字段。

## 采集能力对照
| 想回答的问题 | 覆盖 |
|---|---|
| 竞品股权/战略变了吗 | ✅ 上市公告 |
| 竞品融资了吗 | ✅ 股本变动 API + 媒体（仅上市公司有公开披露） |
| 竞品出新品了吗 | ✅ 行业媒体 |
| 竞品拿到大单了吗 | ⚠️ 部分（媒体会报道大单；政采只覆盖小额） |
| 市场有需求吗 | ✅ 招标公告 |
| 谁中标了、多少钱 | ✅ 中标详情页解析 |
| 竞品官网有动作吗 | ✅ 变更监控 |
| 竞品专利动态 | ❌ 未接入（需付费 API，且公开滞后 18 个月，性价比低） |

## 告警（`config/alerts.yaml`）

**只推"新出现的"高价值信号**，同一事件不会反复打扰。

- **触发条件**：命中 `重大变更` / `订单信号` / `竞品命中` / `官网变更` 任一标签，
  且相关度 ≥ 0.7
- **三个渠道**：邮件（SMTP）、钉钉群机器人（支持加签）、通用 Webhook（企业微信/飞书/Slack）
- **凭据走环境变量**，配置文件里只写变量名，密钥不入库：

```powershell
$env:ALERT_DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=..."
$env:ALERT_DINGTALK_SECRET  = "SEC..."
python scripts/send_alerts.py
```

- **去重机制**：`alerts_sent` 表按 `(event_id, channel)` 记录。注意同一事项
  的整组事件会被一并标记，否则同组残余会在下一轮被当成新信号重复推送。
- **失败可重试**：推送失败的事件不会标记为已发，下一轮会自动重试。

### 端到端测试

`scripts/test_alert_e2e.py` 会起一个本地 mock webhook，验证「首次推送 + 二次去重」：

```powershell
python scripts/test_alert_e2e.py
```

## 日报的分层逻辑

长列表会让人漏掉关键信号，所以日报做两级处理：

1. **高价值信号置顶**：`🚨 重大变更`（控制权/收购/重组）→ `💰 订单信号`（中标/重大合同）→ `🎯 竞品直接命中`
2. **同事件折叠**：上市公司会对同一事项按月发进展公告，标题高度相似。
   若不折叠，10 条"控制权拟变更进展"会占满置顶区。`cluster_events()` 按
   标题指纹归并，显示为「10 条 / 2 个事件」并标注最近一条。

实测效果：今天国际的「管理层收购暨控制权拟变更」被正确识别并折叠。

## 相关度打分机制

`src/common/scoring.py` 负责把噪音挡在入库之前。**两套词表，因为两类内容语汇完全不同：**

| | 招标（`tender_keywords.yaml`） | 政策（`policy_keywords.yaml`） |
|---|---|---|
| 核心词示例 | 仓储机器人、AMR、AGV、无人叉车 | 制造业、智能制造、物流、新质生产力 |
| 阈值 | 0.5（宁缺毋滥） | 0.2（宽口径收录后人工筛） |

打分规则：
- **核心词**命中 → +0.6（标题命中再 +0.2）
- **弱匹配词 + 场景词**共现 → +0.3
- **竞品名**命中 → +0.2，标记 `竞品命中`（日报置顶）
- **噪音词**（扫地机器人、体育赛事等）→ **一票否决，直接 0 分**

调用方式：`score_relevance(title, content, kind="tender"|"policy")`

## 合规红线

- 只采集**公开**信息；`robots.txt` 禁止的一律跳过（发改委已因此排除）
- 请求强制随机延迟，政府站点用更保守的 3~6 秒
- ccgp 关键词间强制等待 60 秒，避免触发风控
- 只采**企业/公开事件**，不采个人信息
- 每条记录保留 `source_url` + `captured_at`，可溯源
- 商用前需过法务，尤其第三方数据平台 API 的二次分发授权

## 下一步扩展点

1. **上市公告**：接 AkShare（今天国际 300532），API 通路最稳的一环
2. **招投标扩面**：各省公共资源交易中心（建议先人工跑通一个省再写代码）
3. **融资/专利**：接天眼查/企查查/IT桔子开放 API（需申请 key）
4. **告警**：日报接邮件 / 钉钉 Webhook
5. **changedetection.io**：如需图形化界面管理官网监控，可与其并存

# 仓储 AMR 竞品情报系统

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

面向**仓储 AMR（料箱机器人 / 货到人 / 无人叉车 / 仓储集成）**赛道的竞品情报系统。

从公开渠道持续采集对手动态，用 LLM 提炼成可读情报，通过企业微信推送，
并在本地看板上浏览与检索。

> 本项目由一个自研采集系统 + 一个第三方深度分析项目（MIT）组成，
> 两者在**数据层双向打通**。详见 [架构](#架构)。

---

## 它能回答什么问题

| 问题 | 覆盖情况 |
|---|---|
| 竞品股权/战略变了吗 | ✅ 上市公司公告（AkShare） |
| 竞品融资了吗 | ✅ 交易所股本变动 + 行业媒体 |
| 竞品出新品了吗 | ✅ 行业媒体（4 个源） |
| 市场有需求吗 | ✅ 政府采购招标公告 |
| 谁中标了、多少钱 | ✅ 中标公告详情页解析 |
| 竞品官网有动作吗 | ✅ 页面变更监控 |
| 竞品拿到大单了吗 | ⚠️ 部分（媒体会报道；纯商业项目无公示义务） |
| 竞品专利动态 | ❌ 未接入（公开滞后约 18 个月，性价比低） |

---

## 架构

```
┌──────────────────────── intel-system（自研）────────────────────────┐
│                                                                     │
│  采集层        上市公告 · 股本变动 · 行业媒体 · 招标/中标            │
│                · 中标详情页 · 竞品官网变更 · 政策栏目                │
│      ↓                                                              │
│  提炼层        抓正文 → LLM 提炼（摘要 + 对我方的影响）              │
│      ↓                                                              │
│  输出层        本地看板（127.0.0.1:8000） · 企业微信推送             │
└─────────────────────────────────────────────────────────────────────┘
                    ↕ 数据层双向打通
┌──────────────── third_party/competitor-intel（MIT）─────────────────┐
│                                                                     │
│  方向 1  自建事实 → 注入分析上下文（prior_context）                  │
│  方向 2  搜索来源 → 回补自建库（填补覆盖缺口）                       │
│                                                                     │
│  产出    SWOT / 威胁分析 / 行动建议的完整战略报告                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 为什么这么设计

两套系统的依赖与生命周期差异很大（自研系统刻意零框架；分析系统需要
langgraph + streamlit），**合并代码会让两边都不稳**。因此只做数据层打通：

- **方向 1**：把自研系统采集到的一手事实注入分析提示词，让 SWOT 基于
  最新事实而非仅搜索结果
- **方向 2**：把分析时检索到的来源回补进自研库 —— 自研的固定源覆盖很窄
  （45 个竞品里只有 12 个有数据），搜索引擎能补上这个缺口

---

## 快速开始

### 环境要求

| 软件 | 版本 | 说明 |
|---|---|---|
| Python | 3.10+ | 必需 |
| Chromium | — | 可选，政策栏目需要 JS 渲染 |

> Windows 用户请确保 Python 在 PATH 中，或用 `PYTHON_EXE` 环境变量指定。

### 安装

```bash
git clone https://github.com/YZ-max0/qingbao.git
cd qingbao/intel-system

pip install -r requirements.txt

# 可选：打通需要 JS 渲染的政策源
pip install playwright
python -m playwright install chromium
```

### 配置

**1. LLM 密钥**（必需，否则提炼降级为规则摘要）

```bash
copy config\llm.local.example.yaml config\llm.local.yaml   # Windows
cp config/llm.local.example.yaml config/llm.local.yaml     # Linux/macOS
```

编辑 `config/llm.local.yaml` 填入 `api_key`。任何兼容 OpenAI 协议的服务都可
（DeepSeek / OpenAI / Moonshot / 通义千问 / 智谱 …）。

也可以交互式配置，它会自动验证密钥可用性：

```bash
python scripts/set_api_key.py
python scripts/set_api_key.py --show        # 查看配置（密钥打码）
python scripts/set_api_key.py --test-only   # 只验证
```

**2. 调整监控目标**（可选）

- `config/competitors.yaml` —— 竞品清单与官网监控点
- `config/tender_keywords.yaml` —— 招标检索关键词、场景词、噪音词
- `config/policy_keywords.yaml` —— 政策关键词

### 运行

```bash
python scripts/init_db.py        # 初始化数据库

# 一键跑完整链路（含限频等待，约 15 分钟）
python scripts/run_all.py

# 或分步执行
python scripts/run_announcement.py      # 上市公告（API，最稳）
python scripts/run_article.py           # 抓正文
python scripts/run_llm_digest.py 200    # LLM 提炼
python scripts/run_tender.py 30         # 招标/中标
python scripts/run_award.py             # 中标详情页 → 供应商
python scripts/run_site_change.py       # 官网变更
python scripts/run_policy.py            # 政策栏目
python scripts/make_report.py 7         # 生成日报
python scripts/send_alerts.py           # 推送告警
```

### 看板

```bash
python scripts/dashboard.py             # http://127.0.0.1:8000
```

零依赖（仅 Python 标准库），仅监听 `127.0.0.1`。

功能：筛选 · 搜索 · 排序 · 标记已读 · 归档 · 加星 · 备注 · CSV 导出 ·
统计卡片 · 趋势图 · 深度报告阅读。

**两个 Tab**：

| Tab | 内容 |
|---|---|
| 每日情报 | 媒体、招标、公告、政策、融资、官网变更 |
| 深度分析 | 战略报告 + 分析来源 |

---

## 定时任务（Windows）

`run_scheduled.ps1` 是**可移植**的启动器（自动定位自身目录，无硬编码路径）。

```powershell
$ps  = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$dir = "D:\path\to\intel-system"

$act = New-ScheduledTaskAction -Execute $ps `
       -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$dir\run_scheduled.ps1`""
$trg = New-ScheduledTaskTrigger -Daily -At 09:00

Register-ScheduledTask -TaskName 'IntelSystem_Daily' -Action $act -Trigger $trg
```

> **注意**：`.ps1` 必须保存为 **UTF-8 with BOM**，否则 Windows PowerShell
> 会按 ANSI 读取并弄乱非 ASCII 文本；`.bat` 必须用 **CRLF** 换行。
> 项目里的启动器已按此处理。

告警 Webhook 通过 `config/secrets.local.ps1` 注入（模板见
`config/secrets.local.example.ps1`），避免密钥出现在代码中。

---

## 核心概念

### 两种通路，同一张表

| 通路 | 适用 | 例子 |
|---|---|---|
| **API** | 有官方接口/开放数据 | 上市公告、股本变动 |
| **爬虫** | 只有网页 | 招标、官网变更、媒体 |

两者写入**同一张 events 表**，用 `source_type` 区分 —— 便于联合分析。

### 相关度打分

不同内容语汇完全不同，故用**两套词表**：

| | 招标 | 政策 |
|---|---|---|
| 词表示例 | 仓储机器人、AMR、AGV | 制造业、物流、新质生产力 |
| 阈值 | 0.5（宁缺毋滥） | 0.2（宽口径） |

打分规则：核心词命中 +0.6（标题命中再 +0.2），弱匹配 + 场景词共现 +0.3，
竞品名命中 +0.2，**噪音词一票否决**。

> 实测教训：政策源若复用招标词表，会导致**全部政策被过滤**（21 条全丢）。

### LLM 提炼

每条情报产出三样：

- **一句话摘要**（列表首行）
- **对我意味着什么**（影响判断）
- **摊开讲的几句话**（右侧「提炼」栏）

按频道走不同路径：媒体报道用 LLM 概括；招投标从结构化字段拼装；
深度报告压缩成「定位 / 威胁 / 行动」三段。

**静默失败防护**：部分服务在限流时返回 `HTTP 200 + {"error":...}`，
SDK 不抛异常但 `choices` 为空。代码会识别并按可重试错误处理，
否则会以 `TypeError` 崩溃。

---

## 目录结构

```
qingbao/
├── intel-system/                  自研采集与展示系统
│   ├── config/                    配置（含示例文件，真实密钥被 gitignore）
│   ├── db/                        SQLite schema
│   ├── scripts/                   执行入口 + 看板
│   ├── src/
│   │   ├── common/                抓取器、数据库、打分
│   │   ├── adapters/              11 个采集适配器
│   │   └── report/                提炼、日报、告警
│   ├── run_daily.bat              每日任务（手工/双击）
│   └── run_scheduled.ps1          每日任务（计划任务用，可移植）
│
├── third_party/
│   └── competitor-intel/          第三方深度分析（MIT，见其 LICENSE）
│       └── 本地改造说明.md          我们的适配改动
│
└── docs/
    └── 实测记录.md                 采集源可用性、已知限制
```

---

## 实测记录与已知限制

**这个项目最大的价值之一是"踩过的坑"。** 详见
[`docs/实测记录.md`](docs/实测记录.md)，包括：

- 政府站点**没有 RSS**（实测均无）
- 发改委 robots.txt **明确禁止**抓取（已合规排除）
- 政府采购网**有 IP 级频率限制**（已加 60 秒强制间隔）
- 部分站点是 **JS 动态渲染**（静态请求拿不到内容）
- 系统代理会导致**部分国内站点随机失败**（代码已改为直连）

---

## 合规声明

本项目**只采集公开信息**，并遵守以下原则：

- 遵守 `robots.txt`（禁止抓取的一律跳过）
- 请求间强制随机延迟，政府站点更保守
- 只采集**企业/公开事件**信息，不采集个人信息
- 每条记录保留来源 URL 与抓取时间，可溯源

**使用者需自行确保**：目标站点的服务条款允许、频率不超过其承受能力、
以及商用场景下的数据授权（尤其第三方数据平台 API）。

---

## 许可

本项目采用 **MIT License**，详见 [LICENSE](LICENSE)。

`third_party/competitor-intel/` 是独立作品，来自
[WwaitW/competitor-intel](https://github.com/WwaitW/competitor-intel)，
采用其自身的 MIT 许可证，版权归原作者所有 —— 其原始许可证保留在该目录的
`LICENSE` 文件中，我们对它的改动记录在同目录的 `本地改造说明.md`。

---

## 第三方依赖

| 依赖 | 用途 | 许可 |
|---|---|---|
| [AkShare](https://github.com/akfamily/akshare) | 上市公告、股本变动 | MIT |
| [competitor-intel](https://github.com/WwaitW/competitor-intel) | 深度战略分析 | MIT |
| [Tavily](https://tavily.com) | 分析系统的搜索后端 | 商业服务 |

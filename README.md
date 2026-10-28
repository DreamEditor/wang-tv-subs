# wang-tv-subs

电视订阅的**永久固定地址**（GitHub Pages，public 仓库，免费额度），内容由 GitHub Actions
每 6 小时检测 / 修复 / 重新发布一次。仓库里没有任何 token、密码或密钥，workflow 只用默认
`GITHUB_TOKEN`。

## 1. 订阅地址（长期不变）

| 用途 | 固定地址 |
|---|---|
| 直播（m3u，FongMi / 电视家 / 各类播放器） | `https://dreameditor.github.io/wang-tv-subs/live.m3u` |
| 点播（TVBox / FongMiTV JSON） | `https://dreameditor.github.io/wang-tv-subs/tvbox.json` |

备用镜像（内容与上面完全相同，同一份仓库文件）：

| 镜像 | 地址 | 本机直连实测 |
|---|---|---|
| jsDelivr (fastly) | `https://fastly.jsdelivr.net/gh/DreamEditor/wang-tv-subs@main/live.m3u` | 200 / 6582 B ✅ |
| jsDelivr (gcore) | `https://gcore.jsdelivr.net/gh/DreamEditor/wang-tv-subs@main/live.m3u` | 200 / 6582 B ✅ |
| jsDelivr (默认 cdn) | `https://cdn.jsdelivr.net/gh/DreamEditor/wang-tv-subs@main/live.m3u` | 连接被 reset（TLS 中断）❌ |

> 结论：**`cdn.jsdelivr.net` 在本机网络直连不通**（需要代理），真正可用的 jsDelivr 节点是
> `fastly.jsdelivr.net` / `gcore.jsdelivr.net`；`raw.githubusercontent.com`、`ghproxy.net`、
> `cdn.statically.io` 在本机直连同样不可用。所以首选地址始终用 `dreameditor.github.io`，
> 镜像只作备用。

## 2. 仓库结构

```
live.m3u                        # 发布物：直播列表（固定文件名）
tvbox.json                      # 发布物：点播配置（固定文件名）
sources/live-sources.json       # 真相源：每频道 [{name, group, candidates:[{url, prio, fails, last_ok}]}]
sources/vod-sites.json          # 真相源：站点 [{key, name, api, type, prio, fails, last_ok}]
sources/tvbox-base.json         # tvbox.json 的站点以外的公共字段（spider/parses/lives/flags…）
state/health.json               # 最近一次检测结果（每频道/站点状态、错误分类、Pages 校验）
state/last-good/                # 上一版可用产物，格式自检失败时回滚用
state/history/YYYYMMDD-HHMM.json# 历史快照（保留最近 30 份）
state/keepalive.txt             # 月度保活时间戳
tools/health.py                 # 检测 + 修复 + 生成（纯标准库，无第三方依赖）
tools/bootstrap_sources.py      # 一次性：把本地素材转成真相源
.github/workflows/health.yml    # 每 6 小时 + 手动触发
.github/workflows/keepalive.yml # 每月一次保活提交
```

## 3. health.py 的判定逻辑（一句话版）

**直播线路要“分片级可用”才算活着：m3u8 必须 200 且含 `#EXTM3U`，然后真的下载最后 2 个 ts
分片，至少 1 个 200 且 >20KB；失败只累计 `fails`，连续 2 次检测失败才切候选（候选按 prio、
历史成功者优先，实测通过才启用），无可用候选就保留原线路并记录故障；生成后做格式自检
（≥30 频道、JSON 可解析且 sites 非空），不通过就回滚到 `state/last-good/`。**

细节：

- **直播**：`GET m3u8`（跟随 302，UA `Mozilla/5.0 (Linux; Android 11; TV)`）→ `#EXTM3U` →
  master playlist 自动下钻一层选最高码率 → 取最后 2 个分片各自 `GET`（只读前 64KB）→
  至少 1 片 `200` 且 `>20480` 字节才算可用。
- **错误分类分开记录**：`http-403 / http-404 / http-429 / http-5xx / timeout / dns /
  conn-refused / conn-reset / tls / no-extm3u / no-segments / segment:too-small`。
- **点播**：`?ac=videolist&wd=庆余年` → 必须返回 JSON 且 `list` 非空 → 取首条
  `vod_play_url` 的第一个 URL → 是 m3u8 就要求 200 且含 `#EXTM3U`，否则要求 200 且 >1KB；
  `type:3`（spider.js 站点）改为校验脚本本身 200 且 >1KB。
- **重试**：任何单次失败先原地重试 1 次，仍失败才计入 `fails`。
- **站点降级**：连续 2 次失败才从 `tvbox.json` 的 `sites` 移除；但发布结果**始终 ≥5 个站点**，
  健康站点不足 5 个时退回上一版发布列表（再不行退回全量原状）并在 `state/health.json` 告警。
- **永不清空**：频道只切换线路、不删除；48 个频道在没有任何可用源时仍以原线路保留。
- **格式自检 + 回滚**：`live.m3u` 频道数 ≥30、每条 `#EXTINF` 至少一个 http 地址、
  `tvbox.json` 可被 json 解析且 `sites` 非空，任一不满足 → 用 `state/last-good/` 的上一版覆盖，
  绝不提交坏文件。

## 4. vantage（观测点）保护 —— 为什么 40 个频道显示 unverified

`cg01.hrtn.net` 这类运营商网关的 DNS 记录本身指向 **私有地址**（实测 `172.19.29.159`，
8.8.8.8 与本地 DNS 都是），只会 302 进运营商内网。GitHub runner 在境外，既解析不到也连不上，
这**不代表源失效**。因此 health.py：

- 播单一跳转进私有地址（`vantage:private-redirect`）或 runner 直接 `DNS` 解析失败
  （`vantage:dns-unresolvable`）→ 记为 `unverified`，**不计入 fails、不触发切换**；
- 只有能真正观测到的失败（403/404/429/5xx/分片不达标/超时于公网可达主机）才累计 `fails`。

所以 `state/health.json` 里 40/48 频道长期是 `unverified` 属于**预期行为**，它们的可用性要
在本地网络判断（见下一节）。

## 5. 本地校正（推荐，真正的“中国网络”视角）

```powershell
cd C:\Users\zheng\Documents\ChatGPT\其他\WANG-TV\subs-repo
python tools/health.py --timeout 15 --workers 12   # 全量检测 + 修复 + 重新生成
git add -A; git commit -m "subs: local check"; git push
```

本机实测：**48/48 频道通过、9/10 站点通过、耗时约 44 秒**（`--generate-only` 可只生成不探测）。

## 6. 工作流

`health.yml`：`cron: '0 */6 * * *'` + `workflow_dispatch`（可选 `no_probe`），
`permissions: contents: write`，`concurrency` 防并发。步骤：checkout → 跑 health.py →
有变更则 `git commit -m "subs: vYYYYMMDD-HHMM (48ch/62line, 9 sites)"` 并 push →
sleep 后 `curl` 公开页面并比对 **sha256**（最多 15 次 ×20s）→ 结果写进
`state/health.json` 的 `pages` 字段 + workflow summary，再提交一次 state。

`keepalive.yml`：每月 1 号 `17 3 * * *` 写一个 `state/keepalive.txt` 时间戳并提交，
防止 GitHub 因 60 天无活动停用定时任务（提交本身就是活动）。

## 7. 已完成的验证记录

| 项目 | 结果 |
|---|---|
| 仓库 | https://github.com/DreamEditor/wang-tv-subs （public，main） |
| Pages | `build_type: legacy`，`source: main /`，`https_enforced: true`，status `built` |
| `live.m3u` | HTTP 200 / 6582 B / `#EXTM3U` / **48 频道 62 线路** |
| `tvbox.json` | HTTP 200 / 2288 B / JSON 可解析 / **9 个 site** |
| Pages 内容一致性 | 远端与本地 sha256 完全相同（`5a24642c…` / `c94c44f5…`），第 1 次轮询即匹配 |
| workflow | `40729698342` success（2m47s）、`40730286570` success（47s）、keep-alive `40730536923` success |
| 本机直连 github.io | 200（live.m3u `audio/x-mpegurl`，tvbox.json `application/json`） |

## 8. 已知限制

1. **Actions 可能延迟**：`*/6` 是“尽力而为”，高峰排队常见，实际间隔可能 6~8 小时。
2. **Actions 可能被停用**：仓库连续 60 天无活动时 GitHub 会停用 schedule；本项目用
   `keepalive.yml` 每月空提交规避，但仍需偶尔看一眼 Actions 页。
3. **runner 视角 ≠ 你家网络**：境外 runner 只能判定约 8 个公网源；40 个运营商内网源会一直
   显示 `unverified`，需要本地跑一次才是真实结论。
4. **第三方源会失效**：免费 IPTV / 采集站随时关停或改协议，脚本只能降级或保留故障记录，
   不能凭空造出可用源；候选池来自 `sources/…json`，可以手工加候选。
5. **Pages 构建有延迟**：push 到页面生效通常 20~90 秒，极端时数分钟；workflow 最多等 5 分钟。
6. **播放器差异**：同一频道最多给 2 条线路（主 + 备用），部分播放器只用第一条。
7. **镜像**：`cdn.jsdelivr.net` 在本机网络被 reset，备用请用 `fastly.` / `gcore.` 前缀。

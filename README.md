# Helldivers 2 更新推送

[![Tests](https://github.com/TouristH/HelldiversPatchFeed/actions/workflows/tests.yml/badge.svg)](https://github.com/TouristH/HelldiversPatchFeed/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个面向 MaiBot Plugin SDK 2.x 的《HELLDIVERS 2》社区插件。它会自治检查官方 Steam 公告，筛选版本更新并推送到已订阅的 QQ 群；也提供 Wiki 查询、随机配装、单格重抽、权限控制和渲染自检。

本项目是非官方粉丝作品，与 Arrowhead Game Studios 或 Sony Interactive Entertainment 无关联。随机配装的数据与图像资源包含第三方来源，完整署名和许可边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 主要功能

- 每 12 小时检查一次 Steam 官方新闻，可在 WebUI 中调整间隔。
- 自动推送只面向明确的版本更新，避免前瞻、媒体转载和普通活动公告刷屏。
- 优先采用官方中文 RSS；没有中文版本时自动回退英文原文。
- 按群维护订阅与投递记录，失败可重试，已经送达的公告不会重复推送。
- 支持手动查询补丁、热修复、战争债券、Wiki 最近更改和数值调整。
- 生成八格随机配装卡，并在限定时间内按编号单独重抽任意一格。
- 提供管理员白名单、用户黑名单、订阅门禁、请求冷却和出图诊断。
- 所有运行数据保存在插件专属数据目录，不需要外部数据库。

## 兼容性

| 项目 | 要求 |
|---|---|
| MaiBot Host | `>= 1.0.0` |
| MaiBot Plugin SDK | `>= 2.0.0, < 3.0.0` |
| Python | 以当前 MaiBot/SDK 支持版本为准 |
| 可选出图环境 | Playwright + Chromium |

插件清单位于 `_manifest.json`，运行时只请求实际使用的 `chat.open_session`、`send.text`、`send.image` 和 `render.html2png` 能力。

稳定插件 ID 为 `github.touristh.helldivers2-patch-feed`。正式发布后不会更改该 ID，以免运行数据与安装记录失联。

## 安装

### 从 MaiBot 插件市场安装

插件通过官方审核后，可在 MaiBot WebUI 的插件市场中搜索“Helldivers 2 更新推送”并安装。市场安装会根据 `_manifest.json` 检查 Host、SDK、依赖和能力兼容性。

### 手动安装

在 MaiBot 的 `plugins` 目录中执行：

```bash
git clone https://github.com/TouristH/HelldiversPatchFeed.git
```

重启 MaiBot，Runner 会发现仓库根目录中的 `_manifest.json` 和 `plugin.py`。插件首次加载时会生成 `config.toml`，建议直接在 MaiBot WebUI 中修改配置。

如果宿主没有成功自动安装渲染依赖，可在运行 MaiBot 的同一个 Python 环境中执行：

```bash
python -m pip install -r plugins/HelldiversPatchFeed/requirements.txt
python -m playwright install chromium
```

只需要文字配装时可以不安装浏览器；图片渲染失败会自动回退为文字，不影响更新推送。

## 快速配置

最少只需决定接收自动推送的群：

1. 让群内用户发送 `/helldivers subscribe`；或
2. 在 WebUI 中把群号写入 `default_groups`。

`default_groups` 与群内订阅会合并去重。首次成功采集只建立历史基线，不会把已有旧公告一次性灌入群；之后出现的新版本才会自动推送。

完整示例见 [config.example.toml](config.example.toml)。主要配置如下：

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `enabled` | `true` | 是否启动自动轮询；关闭后群命令仍可使用 |
| `poll_interval_seconds` | `43200` | 自动检查间隔，单位秒 |
| `default_groups` | `[]` | 固定接收自动推送的 QQ 群号 |
| `require_subscription` | `true` | 未订阅群是否只能执行订阅命令 |
| `admin_users` | `[]` | `list`、`diag` 管理指令白名单 |
| `command_blacklist` | `{}` | 按指令限制功能性命令的用户 |
| `steam_enabled` | `true` | 是否采集 Steam 官方新闻 |
| `steam_app_id` | `553850` | HELLDIVERS 2 的 Steam App ID |
| `steam_endpoint` | 官方 API | Steam 新闻接口或自建镜像 |
| `steam_locale` | `schinese` | 官方 RSS 本地化语言；留空可关闭中文增强 |
| `wiki_enabled` | `false` | 是否把 Wiki 编辑动态加入自动采集 |
| `wiki_endpoint` | 官方 Wiki API | Wiki API 或自建镜像 |
| `request_timeout_seconds` | `20.0` | 单次上游请求超时 |
| `force_push_cooldown_seconds` | `60` | 两次手动刷新上游的最小间隔 |
| `wiki_detail_max_age_days` | `30` | 为近期版本反查 Wiki 详情页的天数窗口 |
| `wiki_recent_limit` | `10` | Wiki 最近更改条数 |
| `wiki_digest_max_chars` | `1200` | Wiki 数值摘要最大字符数 |
| `loadout_reroll_seconds` | `120` | 单格重抽会话的有效时间 |

只接受 `http://` 或 `https://` 数据源地址；`file:`、本机路径等不会被当作远程接口读取。

## 群命令

`/helldivers`、`/hd`、`/潜兵`、`/绝地潜兵` 四个前缀等价。

| 命令 | 说明 | 默认权限 |
|---|---|---|
| `/helldivers subscribe` | 订阅当前群 | 功能性 |
| `/helldivers unsubscribe` | 取消订阅当前群 | 功能性 |
| `/helldivers push [类型]` | 立即检查并返回最近一期指定内容 | 功能性 |
| `/helldivers loadout [1-8]` | 生成配装，或在有效期内重抽一格 | 功能性 |
| `/helldivers status` | 查看本群订阅、公告与投递状态 | 功能性 |
| `/helldivers help [主题]` | 查看总览或某条命令的说明 | 功能性 |
| `/helldivers list` | 列出全部订阅群 | 管理性 |
| `/helldivers diag` | 执行真实图片渲染与发送自检 | 管理性 |

`push` 类型包括：

| 类型 | 内容 |
|---|---|
| `version` | 最新版本更新，裸 `push` 的默认类型 |
| `patch` | 最新补丁公告 |
| `hotfix` | 最新热修复 |
| `warbond` | 最新战争债券公告 |
| `all` | 最新一条官方公告，不限类型 |
| `wiki` | Wiki 最近更改 |
| `balance` | 最近补丁页的数值调整摘要 |

命令支持中文动作、中文类型和字母简写。例如：

```text
/hd sub
/潜兵 推送 热修复
/绝地潜兵 p wb
/hd loadout
/hd lo 3
/helldivers help 简写
```

## 自动推送规则

插件会丢弃非 `steam_community_announcements` 的第三方媒体稿。自动推送只发送满足以下任一条件的官方公告：

- 标题包含三段版本号，例如 `7.1.0`；
- Steam 将公告标记为 `patchnotes`。

其它官方公告仍会入库，可通过 `push patch`、`push warbond` 或 `push all` 按需查询。两次手动刷新处于冷却期时，普通分类会直接返回本地已存的最新内容；必须实时访问 Wiki 的命令会提示稍后再试。宿主操作员不受手动刷新冷却限制。

中文 Wiki 优先于英文 Wiki；中文页尚未更新时会回退英文页。包含 Wiki 内容的消息会附原页面链接及与实际来源站点匹配的许可署名：中文站为 CC BY-SA 4.0，英文站为 CC BY-NC-SA 4.0。

## 权限模型

权限优先级为：宿主操作员 → `admin_users` → `command_blacklist` → 默认策略。

- `subscribe`、`unsubscribe`、`push`、`loadout`、`status`、`help` 是功能性命令，默认开放，可用黑名单限制。
- `list`、`diag` 是管理性命令，仅宿主操作员和 `admin_users` 可以使用。
- 管理员不会被功能性命令黑名单拦截。
- `require_subscription = true` 时，未订阅群只能执行 `subscribe`；私聊和宿主操作员不受该门禁影响。

黑名单示例：

```toml
[plugin]
admin_users = ["123456789"]

[plugin.command_blacklist]
"*" = ["10001"]
push = ["10002"]
```

## 随机配装

`/helldivers loadout` 会抽取一件主武器、一件副武器、一件投掷物、一项强化资源和四项不重复战略配备。卡片左上角的编号对应：

```text
1 主武器 / 2 副武器 / 3 投掷物 / 4 强化资源 / 5-8 战略配备
```

在 `loadout_reroll_seconds` 时间内发送 `/helldivers loadout 1-8` 可只重抽对应格；每次成功重抽都会刷新有效期。会话按“聊天流 + 用户”隔离，不会改到同群其他人的配装。

配装卡使用 `loadout_data.json` 与 `loadout_icons.json`，渲染 HTML 完全自包含，不在出图时访问外网。某件装备没有图标时，对应格仍会正常出图并只显示名称，不会插入空白或破损图片；只有整张卡渲染失败或图片发送失败时，才会把整套配装回退为纯文本。

当前装备目录依据 Helldivers Wiki 中文《[武器](https://helldivers.wiki.gg/zh/wiki/武器)》与《[战略配备](https://helldivers.wiki.gg/zh/wiki/战略配备)》页面校准；中文目录尚未收录的新品参考同站英文 [Weapons](https://helldivers.wiki.gg/wiki/Weapons)、[Boosters](https://helldivers.wiki.gg/wiki/Boosters) 与 [Stratagems](https://helldivers.wiki.gg/wiki/Stratagems)，并保留 Wiki 英文规范名。中文 Wiki 衍生内容按 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans) 再分发，英文 Wiki 衍生内容按 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) 再分发。

随机配装的初始结构与现有内嵌图标参考了 [Xenfo-LC/Helldivers-2-Random-Loadout-Generator-CN](https://github.com/Xenfo-LC/Helldivers-2-Random-Loadout-Generator-CN)。本次从 Wiki 补入、但没有可靠可复用图标的装备仅显示名称。页面修订号、上游作者与完整许可边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 数据、网络与隐私

- SQLite 数据库保存在 MaiBot 注入的 `ctx.paths.data_dir` 中。
- 数据库包含公告、订阅群号和按群投递状态；仓库不会上传这些运行数据。
- 插件仅主动访问配置中的 Steam 新闻端点、Steam RSS 和 Helldivers Wiki 端点。
- `config.toml`、数据库、缓存、虚拟环境和本地开发浏览器资料均已从 Git 提交中排除。
- 插件不收集分析数据，不向本项目作者回传群号、用户号或消息内容。

## 常见问题

### 配装只返回文字

先由管理员执行：

```text
/helldivers diag
```

常见处理方式：

| 诊断结果 | 处理 |
|---|---|
| 渲染能力被禁用 | 在 MaiBot 配置中启用 `plugin_runtime.render.enabled` |
| 未安装 Playwright | 在 MaiBot 的 Python 环境安装 `playwright` |
| 找不到浏览器 | 执行 `python -m playwright install chromium` |
| 首次渲染超时 | 浏览器首次启动或下载较慢，完成后再次执行 `diag` |

### 没有自动推送历史公告

这是预期行为。首次运行建立基线，防止安装后把旧公告集中发到群里。可用 `/helldivers push all` 查询当前库存中的最新官方公告。

### Wiki 返回限流或找不到页面

中文站和英文站位于同一服务，连续请求可能同时受到限流。插件会降级为不附 Wiki 详情，不影响 Steam 公告推送；请等待冷却后重试。

## 开发与验证

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖公告解析、官方来源过滤、中文增强、Wiki 降级、数据库迁移、幂等投递、权限矩阵、配置模型、命令别名、随机配装和 manifest 约束。合并前仍建议在真实 MaiBot 中至少验证插件加载、订阅、`push`、`diag` 与热更新配置。

发布版本时，`_manifest.json` 的 `version`、Git Tag 和 GitHub Release 版本必须一致；已经发布并被插件中心收录的版本不得移动 Tag。

## 贡献者与 AI 辅助

- [TouristH](https://github.com/TouristH)：项目作者与维护者。
- OpenAI Codex：AI 辅助贡献者，参与装备数据核对、代码与测试审查及文档整理；所有改动均由项目维护者审阅并发布。

完整说明见 [CONTRIBUTORS.md](CONTRIBUTORS.md)。Codex 不是本仓库所有者、维护者或版权持有人。

## 许可与署名

- 本项目原创部分：Copyright © 2026 TouristH，使用 [MIT License](LICENSE)。
- 中文与英文 Wiki 衍生的装备目录分别使用 CC BY-SA 4.0 与 CC BY-NC-SA 4.0；随机配装参考项目与既有图像另有权利边界，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。这些内容不应被误认为自动纳入本项目 MIT 许可。
- HELLDIVERS 2 的名称、图像及相关媒体归其各自权利人所有。

问题与建议请提交到 [GitHub Issues](https://github.com/TouristH/HelldiversPatchFeed/issues)。

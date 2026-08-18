# AstrBot LogVault

LogVault 为 AstrBot 增加可靠的日志文件留存、分类、轮换、压缩、清理、搜索和聊天内发送能力。它适合两类场景：日常保留最近日志，以及向插件作者提供某个问题发生时的准确日志。

## 这个版本解决了什么

这个版本基于上游 `astrbot_plugin_logplus`，但使用了新的插件 ID `astrbot_plugin_logvault`，不会与上游的插件数据目录混用。

- 修复 Linux 上“日志目录里只有旧 `.gz`、最近日志没有导出”的问题。旧版清理任务可能压缩或删除仍被 `FileHandler` 打开的当前 `plugin.log`；Linux 允许删除打开的文件，后续内容会写入目录中看不见的 inode。LogVault 只压缩已经轮换、已关闭的日志文件，也不会删除活动日志。
- 修复 Windows、Linux 和混合路径分隔符下的插件识别，插件日志不会因为路径格式被错误归入 Core。
- 发送命令会从 AstrBot 插件注册表和 `data/plugins` 识别真实插件 ID，不再把“还没有产生专属日志”误报为“插件不存在”。
- 兼容 AstrBot 4.27+ 的 `astrbot.plugin.<插件名>` 专属 logger，不依赖旧版本的全局 logger 传播行为。
- 在插件加载钩子和后台扫描中同时发现新建的专属 logger，避免插件启动顺序导致日志漏记。
- 导出和搜索会在后台线程执行，减少大日志文件阻塞消息处理的概率；压缩日志保留原始修改时间，按天筛选不会把旧内容误当成新日志。
- 增加按插件和天数发送：`/logplus send plugin <插件名> <天数>`。
- 发送/导出默认会读取旧版 `astrbot_plugin_logplus` 数据目录，但只读，不会修改或清理旧目录。

## 安装

1. 将 `astrbot_plugin_logvault` 文件夹放入 AstrBot 的 `data/plugins/` 目录。
2. 重启 AstrBot，或在 WebUI 中重新加载插件。
3. 在插件配置中检查日志级别、保留天数和隐私脱敏设置。

建议先停止并卸载旧的 `astrbot_plugin_logplus`，再启用 LogVault。两个插件可以使用不同的数据目录，但它们都可能注册 `/logplus`，同时启用时命令归属取决于 AstrBot 的注册顺序。LogVault 的无冲突主命令是 `/logvault`。

## 命令

`/logvault` 是主命令组；`/logplus` 是兼容别名。下面两种前缀都可以使用。

```text
/logvault status
/logvault search <关键词>
/logvault export [天数]
/logvault clean
/logvault help

/logvault send all [天数]
/logvault send errors [天数]
/logvault send plugin <插件名> [天数]
```

例如，发送 `astrbot_plugin_dynamic_card_plus` 最近 3 天的日志：

```text
/logplus send plugin dynamic_card_plus 3
```

插件名支持不区分大小写的模糊匹配。如果匹配到多个插件，命令会列出候选项并要求输入更具体的名称。天数必须是 `1` 到 `3650` 的整数；省略时默认最近 7 天。

可用插件名会从 AstrBot 当前的插件注册表读取，不要求该插件已经产生专属日志目录。如果插件已识别但最近 N 天没有日志，命令会明确说明“尚未捕获到日志”；LogVault 不会伪造或回溯安装之前的记录。

如果日志已经被 AstrBot 写入默认的 `data/logs/astrbot.log`，但还没有被分流到专属目录，`send plugin` 会按插件 ID 从该后台日志中筛选相关记录，不会打包整份 Core 日志。

### 为什么旧导出里只有 LogVault 自己的日志？

AstrBot 4.27 将插件日志改为独立的 `astrbot.plugin.<插件名>` logger，并设置 `propagate=False`。上游 LogPlus 调用 `logger.addHandler()` 时，`astrbot.api.logger` 会解析到 LogPlus 自己的专属 logger，因此它在新版 AstrBot 中看不到其他插件的记录；这不是 Dynamic Card Plus 没有产生日志。LogVault 2.0.3 会同时连接全局 logger、已有的专属 logger，并在后续插件加载时继续连接新 logger。

你提供的旧备份还存在另一种历史结构：

```text
plugins/<插件>/plugin.log/plugin.log
```

这通常是旧版本轮换或解压过程中留下的目录形态。LogVault 会把它作为只读历史文件读取；如果文件本身已经早于查询天数，按天发送仍会正确排除它，不会把旧记录冒充成最新日志。

## 日志位置

LogVault 的新数据目录由 AstrBot 的 `StarTools.get_data_dir()` 管理，通常是：

```text
data/plugin_data/astrbot_plugin_logvault/
├── all/all.log                 # 全部记录
├── core/core.log               # Core 记录
├── errors/error.log            # ERROR 及以上
├── plugins/<插件名>/plugin.log # 插件记录
└── exports/                    # 命令生成的 zip 包
```

旧版目录 `data/plugin_data/astrbot_plugin_logplus/` 如果存在，会自动作为只读历史来源参与 `status` 以外的搜索、导出和发送。也可以在配置中的 `legacy_data_dirs` 每行填入其他历史目录。

## 配置

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `log_level` | `DEBUG` | 写入的最低级别 |
| `max_file_size_mb` | `10` | 单个活动日志达到此大小后轮换 |
| `backup_count` | `5` | 每类日志保留的轮换槽位数 |
| `rotation_strategy` | `size` | `size` 按大小轮换，`time` 按小时/天轮换 |
| `rotation_interval` | `daily` | 时间轮换的间隔：`hourly` 或 `daily` |
| `enable_compression` | `true` | 压缩已关闭的轮换日志 |
| `compression_after_days` | `1` | 轮换日志超过多少天后压缩 |
| `auto_clean_enabled` | `true` | 启用后台清理 |
| `max_total_size_mb` | `500` | 日志目录总大小上限；活动日志不会被删除 |
| `max_age_days` | `30` | 轮换日志最长保留天数 |
| `enable_sensitive_filter` | `true` | 脱敏 token、password、secret 等键值 |
| `sensitive_keywords` | 见配置页 | 逗号分隔的自定义敏感关键词 |
| `include_legacy_data` | `true` | 是否读取旧版目录 |
| `legacy_data_dirs` | `[]` | 额外历史目录，每行一个路径 |
| `host_log_dirs` | `[]` | AstrBot 共享日志的额外目录；默认自动读取 `data/logs` 和核心配置中的日志路径 |

脱敏只影响 LogVault 写入的副本和共享日志回退筛选生成的副本，不会修改 AstrBot 的控制台日志或其他 Handler。发送日志前仍应确认内容不包含不应公开的数据。

## 从旧版升级

1. 停止旧版插件或停止 AstrBot。
2. 安装 LogVault 并启动一次，让它创建新的数据目录。
3. 如果旧日志仍位于默认的 `data/plugin_data/astrbot_plugin_logplus/`，无需复制；LogVault 会自动读取它们。
4. 如果旧日志在备份目录，填入 `legacy_data_dirs` 后再使用 `send` 或 `export`。

升级后请重启 AstrBot，或在 WebUI 中完整卸载并重新加载 LogVault。已经运行的旧进程不会自动读取 GitHub 上的新代码。重启后先让目标插件产生一条新日志，再执行：

```text
/logplus send plugin astrbot_plugin_dynamic_card_plus 1
```

如果使用了自定义 AstrBot 日志路径，LogVault 会从核心配置自动发现；仍未发现时，把该日志所在目录逐行填入 `host_log_dirs`。

旧版曾经因为活动文件被清理而把新日志写到不可见 inode；这些已经丢失的内容无法从文件系统恢复。升级后新产生的日志会写入新的活动文件，并且不会再被后台清理任务删除。

## 许可与致谢

本项目遵循 GNU Affero General Public License v3.0（AGPL-3.0）。

项目仓库：[Whereis-Alice/astrbot_plugin_logvault](https://github.com/Whereis-Alice/astrbot_plugin_logvault)

感谢 [lxfight/astrbot_plugin_logplus](https://github.com/lxfight/astrbot_plugin_logplus) 提供原始实现和功能设计。本项目是面向实际部署的兼容性与可靠性改进版，保留上游许可和致谢；问题修复与新增功能不代表上游项目的官方发布。

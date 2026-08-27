# AstrBot LogVault

LogVault 为 AstrBot 提供完整的日志留存能力：捕获、分类、轮换、压缩、清理、搜索、按天导出，并在 Dashboard 中提供一个可视化的日志中心页面。

它面向两类场景：日常保留最近日志以便回溯，以及在问题发生时向插件作者提交一份准确、范围可控的日志包。

当前版本：**2.1.0**

## 2.1.0 的三个核心改动

### 1. 日志不再缺失（捕获层重写）

旧版本只通过 `logger.addHandler()` 挂载 `logging` 处理器，这在 AstrBot 4.27+ 上会漏掉大量记录，原因有两层：

- **级别在源头就被丢掉了。** AstrBot 把控制台日志级别同步到 `astrbot` logger 以及每个 `astrbot.plugin.<插件名>` logger。如果控制台配置为 INFO，插件里的 `logger.debug()` 在到达任何 Handler 之前就被拒绝，LogVault 自己写多低的级别都没用。
- **挂载点覆盖不到全部来源。** AstrBot 的日志实际由 loguru 承载，`logging` 侧只是通过 intercept handler 转发；直接使用 loguru 的核心代码、以及各种第三方库自己的 logger，都可能不在被挂载的 logger 树里。

LogVault 2.1.0 的做法：

- 新增 `capture_mode`。默认 `auto` 会直接向 loguru 注册一个 sink，把 loguru 记录转换成 `logging.LogRecord` 再交给 LogVault 的分流器。这样核心、插件、第三方库的日志走同一条通路，一次捕获全部覆盖。注册失败（例如 loguru 不可用）会自动回退到旧的 `logging` 模式，两种模式互斥，不会重复写入。
- 新增 `force_source_debug`。开启后 LogVault 会把 `astrbot` 及所有 `astrbot.plugin.*` logger 的级别降到 `log_level`，让本来不会产生的低级别记录真正产生出来。**这是「日志不全」最常见的原因**，代价是 AstrBot 控制台也会变啰嗦。
- 新增 `backfill_startup_logs`。插件自身是被加载的，加载完成之前的启动日志本来无法捕获；LogVault 会从 AstrBot 控制台日志缓存里回放这段记录，并用 `.bootstrap_state.json` 记录时间水位，重启后不会重复回填。
- 新增 `include_trace_log`。AstrBot 的 trace 级链路日志量极大，默认过滤；需要排查框架内部流程时再打开。

`/logvault status` 现在会附带一段捕获诊断，直接告诉你当前是哪种模式、写入级别、`astrbot` 的生效级别、已转发/丢弃条数、回填条数与 WebUI 接口数量，配置不生效时不需要猜。

### 2. `send` / `export` 的天数参数真正生效

旧版本 `/logplus send all 1` 会打包全部日志，天数像是被忽略了。有两个独立的原因：

- **参数被解析器吞掉。** 命令参数原本声明为 `int` 并带默认值，AstrBot 的参数解析对这种参数会提前做 `int()` 转换，转换路径与 `send plugin <名> <天数>` 这类位置参数组合冲突，天数没有落到处理函数上。现在 `send` / `export` 的所有参数一律声明为 `str`，由插件自己解析：`send all 3` 的简写、`send plugin dynamic_card_plus 3` 的三段式都能正确识别，`send all abc` 会返回插件自己的提示而不是解析器异常。
- **筛选粒度是文件而不是记录。** 即使天数传对了，旧逻辑也只按文件修改时间筛选整个文件。`all.log` 是一个持续写入的活动文件，修改时间永远是「刚刚」，于是整份数十天的内容都被打包。

现在新增 `slice_by_record_time`（默认开启），按**每行日志自己的时间戳**裁剪：

| 情况 | 行为 |
| --- | --- |
| 文件最早一条记录已在范围内 | 原样打包，`.gz` 保持压缩不解包 |
| 文件跨越时间边界 | 重写为只含范围内的行；`.gz` 会解压后以去掉 `.gz` 后缀的明文存入 |
| 裁剪后为空 | 整个文件跳过，不计入文件数 |
| 文件没有可解析的时间戳 | 整份包含（宁多不漏） |

关闭该开关则退回旧的「按文件修改时间整份打包」行为。zip 内的 `ABOUT.txt` 会写明本次导出的范围。

### 3. 新增 WebUI 日志中心

入口：**AstrBot Dashboard → 插件 → LogVault → 日志中心**。

页面能力：

- **分类树**：按 `全部 / Core / 错误 / 各插件` 分组，并标注每个来源是当前数据目录、只读历史目录还是 AstrBot 共享日志目录。
- **文件列表**：大小、修改时间、是否压缩、是否可删除（活动 `.log` 与共享日志目录受保护，不可删）。
- **内容查看**：按级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL），行级着色，敏感信息按 `sensitive_keywords` 脱敏后再返回。
- **实时跟随**：2 秒增量拉取活动日志，切换到其他标签页时自动暂停，文件轮换会被检测到并重置视图。压缩文件不支持跟随。
- **搜索**：跨来源关键词搜索，带扫描行数上限保护。
- **下载与管理**：单文件下载、按天数打包下载、批量删除轮换日志、手动触发清理。

鉴权沿用 AstrBot Dashboard 自身的登录校验与插件 scope，插件不额外实现认证；页面只在已登录的 Dashboard 会话内可访问。后端共 10 个接口（`overview / files / content / tail / search / download / bundle / delete / clean / capture`），注册在 `/astrbot_plugin_logvault/<接口名>` 下。如果运行的 AstrBot 版本不支持插件页面，注册会安静跳过，插件其余功能照常工作。

界面提供中文与英文两套文案，跟随 Dashboard 语言设置。

## 其他修复与改进

- 修复 Linux 上「日志目录里只有旧 `.gz`、最近日志没有导出」：旧版清理任务可能压缩或删除仍被 `FileHandler` 打开的活动 `plugin.log`，Linux 允许删除已打开文件，后续写入会进到看不见的 inode。LogVault 只处理已轮换、已关闭的文件，绝不删除活动日志。
- 修复 Windows / Linux / 混合路径分隔符下的插件归属识别，插件日志不会被错误归入 Core。
- `send plugin` 会从 AstrBot 插件注册表和 `data/plugins` 解析真实插件 ID（带 30 秒缓存），别名会映射到规范 ID，不再把「尚未产生专属日志」误报为「插件不存在」。
- 日志已写入 `data/logs/astrbot.log` 但还没分流到专属目录时，`send plugin` 会按插件 ID 从共享日志中筛选相关行，而不是打包整份 Core 日志。
- 新增 `clean_interval_minutes`（默认 60，范围 1 分钟 ~ 7 天），非法值回落到 60 分钟；旧版清理间隔是硬编码的。
- 导出、搜索、目录扫描都在后台线程执行，减少大文件阻塞消息处理。
- 压缩日志保留原始修改时间，按天筛选不会把旧内容误判成新日志。
- 同一个 Handler 不会被重复挂载（按对象身份去重），避免重启或热重载后日志写重。
- 日志行统一格式并携带来源标签，便于 WebUI 与导出侧解析。

## 安装

1. 把 `astrbot_plugin_logvault` 文件夹放入 AstrBot 的 `data/plugins/` 目录。
2. 重启 AstrBot，或在 WebUI 中重新加载插件。
3. 在插件配置中确认日志级别、保留策略与脱敏设置。
4. 执行 `/logvault status`，确认捕获模式与生效级别符合预期。

插件不依赖任何第三方包（`requirements.txt` 为空）。

建议先停用旧的 `astrbot_plugin_logplus` 再启用 LogVault。两者数据目录不同，但都会注册 `/logplus`，同时启用时命令归属取决于 AstrBot 的注册顺序。LogVault 无冲突的主命令是 `/logvault`。

## 命令

`/logvault` 是主命令组，`/logplus` 是兼容别名，两种前缀等价。

```text
/logvault status                    查看日志状态与捕获诊断
/logvault search <关键词>            搜索日志
/logvault export [天数]              导出最近 N 天日志（默认 7 天）
/logvault clean                     手动清理旧日志
/logvault help                      显示帮助

/logvault send all [天数]            发送最近 N 天全部日志
/logvault send errors [天数]         发送最近 N 天错误日志
/logvault send plugin <插件名> [天数] 发送指定插件最近 N 天日志
```

示例：

```text
/logvault send all 1
/logplus send plugin dynamic_card_plus 3
/logvault export 14
```

插件名支持不区分大小写的模糊匹配；匹配到多个时会列出候选并要求更具体的名称。天数必须是 `1` 到 `3650` 的整数，省略时默认 7 天，非法值会得到明确提示。

## 日志位置

数据目录由 `StarTools.get_data_dir()` 管理，通常是：

```text
data/plugin_data/astrbot_plugin_logvault/
├── all/all.log                 # 全部记录
├── core/core.log               # Core 记录
├── errors/error.log            # ERROR 及以上
├── plugins/<插件名>/plugin.log # 按插件分类
├── exports/                    # 命令与 WebUI 生成的 zip
└── .bootstrap_state.json       # 启动回填水位
```

旧目录 `data/plugin_data/astrbot_plugin_logplus/` 如果存在，会作为**只读**历史来源参与搜索、导出、发送和 WebUI 浏览，不会被修改或清理。也可以在 `legacy_data_dirs` 中每行填入其他历史目录。

## 配置

### 捕获

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `log_level` | `DEBUG` | LogVault 写入文件的最低级别。注意这只是下限，上游 logger 级别更高时低级别记录根本不会产生，需要配合 `force_source_debug` |
| `capture_mode` | `auto` | `auto` 优先 loguru sink 并在失败时回退；`loguru` 强制 sink；`logging` 只挂 logging 处理器（旧行为） |
| `capture_third_party` | `true` | 仅在 `capture_mode=logging` 时生效，额外挂载 root logger 以捕获第三方库 |
| `force_source_debug` | `false` | 把 `astrbot` 与 `astrbot.plugin.*` logger 级别降到 `log_level`。解决日志不全的关键开关，会让控制台更啰嗦 |
| `include_trace_log` | `false` | 是否包含 AstrBot trace 级链路日志（量极大） |
| `backfill_startup_logs` | `true` | 从控制台缓存回填插件加载前的启动日志，按时间水位去重 |
| `backfill_limit` | `500` | 单次回填的最大条数 |

### 分类与轮换

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `enable_all_log` | `true` | 保存全局 `all.log` |
| `enable_core_log` | `true` | 单独保存 Core 日志 |
| `enable_error_log` | `true` | 单独保存 ERROR 及以上 |
| `enable_plugin_separation` | `true` | 按插件分目录存储 |
| `max_file_size_mb` | `10` | 活动日志达到该大小后轮换 |
| `backup_count` | `5` | 每类日志保留的轮换槽位数 |
| `rotation_strategy` | `size` | `size` 按大小，`time` 按时间；历史值 `hybrid` 按 `size` 兼容处理 |
| `rotation_interval` | `daily` | 时间轮换间隔：`hourly` 或 `daily` |

### 压缩与清理

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `enable_compression` | `true` | 压缩已轮换、已关闭的日志 |
| `compression_after_days` | `1` | 轮换日志超过多少天后压缩 |
| `auto_clean_enabled` | `true` | 启用后台清理 |
| `clean_interval_minutes` | `60` | 清理执行间隔，范围 1 ~ 10080 分钟 |
| `max_total_size_mb` | `500` | 目录总大小上限，超出后删除最旧的轮换文件；活动日志不会被删 |
| `max_age_days` | `30` | 轮换日志最长保留天数 |

### 导出与隐私

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `slice_by_record_time` | `true` | 按日志内记录时间裁剪导出范围；关闭则按文件修改时间整份打包 |
| `enable_sensitive_filter` | `true` | 脱敏 token、password、secret 等键值 |
| `sensitive_keywords` | `token,password,secret,api_key,apikey,access_key,accesskey` | 逗号分隔的自定义敏感关键词 |
| `include_legacy_data` | `true` | 是否读取旧版 `astrbot_plugin_logplus` 目录 |
| `legacy_data_dirs` | `[]` | 额外历史目录，每行一个路径 |
| `host_log_dirs` | `[]` | 额外的 AstrBot 共享日志目录；默认自动读取 `data/logs` 与核心配置中的日志路径 |

脱敏只作用于 LogVault 写入的副本、WebUI 返回的内容和导出包，不会修改 AstrBot 控制台日志或其他 Handler 的输出。发送日志前仍应确认内容不含不该公开的数据。

## 排查「日志不全」

按顺序检查：

1. 执行 `/logvault status`，看「日志捕获」段落。
2. 模式显示 `logging 处理器（兼容模式）` 而你期望全量捕获 → loguru sink 注册失败，诊断里的警告会说明原因。
3. `astrbot 生效级别` 高于 `写入级别`（例如生效 INFO、写入 DEBUG）→ 低级别记录在源头被丢弃，开启 `force_source_debug`，或直接把 AstrBot 控制台级别调低。
4. 缺的是插件加载完成之前那一段 → 确认 `backfill_startup_logs` 已开启，必要时提高 `backfill_limit`。
5. 缺的是 AstrBot 框架内部的细粒度链路 → 开启 `include_trace_log`。
6. 日志确实存在但导出里没有 → 检查天数与 `slice_by_record_time`，或用 WebUI 日志中心直接确认文件内容。

## 从旧版升级

1. 停用旧插件或停止 AstrBot。
2. 安装 LogVault 并启动一次，让它创建新的数据目录。
3. 旧日志仍在默认的 `data/plugin_data/astrbot_plugin_logplus/` 时无需复制，会被自动读取。
4. 旧日志在备份目录时，填入 `legacy_data_dirs` 后再使用 `send` / `export` / WebUI。
5. 重启 AstrBot（或在 WebUI 中完整卸载并重新加载），让目标插件产生一条新日志后再验证。

旧版曾因活动文件被清理而把新日志写入不可见 inode，这部分已丢失的内容无法从文件系统恢复。升级后新产生的日志会写入新的活动文件，且不会再被后台清理删除。

## 开发

```bash
python -m compileall -q main.py core
python -m unittest discover -s tests
```

测试覆盖：日志分流与插件归属、按记录时间切片（含 `.gz` 的原样保留与跨界重写）、天数参数解析、loguru 记录转换与 trace 门槛、启动回填的幂等性、WebUI 分类与文件列表、级别过滤与脱敏、增量跟随与轮换重置、路径穿越与删除保护、清理间隔计算。

## 许可与致谢

本项目遵循 GNU Affero General Public License v3.0（AGPL-3.0）。

项目仓库：[Whereis-Alice/astrbot_plugin_logvault](https://github.com/Whereis-Alice/astrbot_plugin_logvault)

感谢 [lxfight/astrbot_plugin_logplus](https://github.com/lxfight/astrbot_plugin_logplus) 提供原始实现与功能设计。本项目是面向实际部署的兼容性与可靠性改进版，保留上游许可与致谢；这里的问题修复与新增功能不代表上游项目的官方发布。

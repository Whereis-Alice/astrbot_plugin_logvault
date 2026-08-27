# 更新日志

本文件记录 LogVault 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.1.0] - 2026-08-27

本次发布解决三个核心问题：日志采集不完整、导出天数参数失效、缺少可视化管理入口。

### 新增

- **loguru 全量捕获**（`core/loguru_capture.py`）。新增 `capture_mode`，默认 `auto` 直接向 loguru 注册 sink，把记录转换成 `logging.LogRecord` 后交给分流器，使核心、插件与第三方库的日志走同一条通路；注册失败自动回退旧的 `logging` 模式，两种模式互斥不会重复写入。新增 `capture_third_party` 控制兼容模式下是否额外挂载 root logger。
- **`force_source_debug`**。主动把 `astrbot` 与所有 `astrbot.plugin.*` logger 的级别降到 `log_level`。AstrBot 会把控制台级别同步到这些 logger，级别过高时低级别记录在到达任何 Handler 之前就已被丢弃，这是日志缺失最常见的原因。
- **启动期日志回填**。新增 `backfill_startup_logs` 与 `backfill_limit`，从 AstrBot 控制台日志缓存回放插件加载完成之前的记录，以 `.bootstrap_state.json` 保存时间水位，重启后不会重复回填。
- **`include_trace_log`**。按需包含 AstrBot trace 级链路日志，默认关闭。
- **捕获诊断**。`/logvault status` 追加一段诊断输出：当前捕获模式、写入级别、`astrbot` 生效级别、已转发与丢弃条数、回填条数、插件 logger 数量、WebUI 接口数量，以及配置冲突时的警告。
- **`slice_by_record_time`**（默认开启）。按每行日志自身的时间戳裁剪导出范围，取代原先按文件修改时间整份打包的做法。
- **WebUI 日志中心**。新增 `core/web_api.py`（`overview` / `files` / `content` / `tail` / `search` / `download` / `bundle` / `delete` / `clean` / `capture` 共 10 个 Dashboard 接口，导入采用三层回退，AstrBot 版本不支持插件页面时安静跳过）与 `pages/logs/` 页面：分类树、级别过滤、脱敏查看、2 秒增量实时跟随、跨来源搜索、单文件下载、按天打包、批量删除、手动清理。
- **界面多语言**。新增 `.astrbot-plugin/i18n/zh-CN.json` 与 `en-US.json`，跟随 Dashboard 语言设置。
- **`clean_interval_minutes`**。清理任务执行间隔改为可配置，默认 60 分钟，范围 1 分钟至 7 天，非法值回落默认值。
- **统一日志行格式**。新增 `LogVaultFormatter` 与 `logvault_tag` 字段，日志行携带来源标签，便于 WebUI 与导出侧解析。
- **测试** `tests/test_capture_and_webui.py`。覆盖按记录时间切片（含压缩文件的两种语义）、loguru 记录转换与 trace 门槛、回填幂等性、WebUI 分类与文件列表、级别过滤与脱敏、增量跟随与轮换重置、路径穿越拒绝与删除保护、清理间隔计算。测试总数由 17 增至 40。

### 修复

- **`send` / `export` 的天数参数被忽略，总是打包全部日志。** 两个独立原因：命令参数原本声明为 `int` 并带默认值，AstrBot 的参数解析会对其提前做 `int()` 转换，与 `send plugin <插件名> <天数>` 的位置参数组合冲突，天数没有落到处理函数；同时旧逻辑按文件修改时间筛选整份文件，而 `all.log` 是持续写入的活动文件，修改时间永远是最新，导致全部历史内容被打包。现在所有参数一律声明为 `str` 并由插件自行解析，`send all 3` 简写与三段式写法都能正确识别，非法天数返回插件自己的提示而不是解析器异常。
- **压缩归档在按天导出时的处理。** 最早一条记录已在范围内时原样保留 `.gz`；跨越时间边界时解压并重写为去掉 `.gz` 后缀的明文；裁剪后为空的文件整份跳过且不计入文件数；没有可解析时间戳的文件整份包含。
- **`send plugin` 的插件名解析。** 新增 `_resolve_installed_plugin`，把别名映射到规范插件 ID 并缓存 30 秒，减少重复扫描注册表。

### 变更

- README 重写：移除版本叙事，改为纯功能与配置文档，变更记录移入本文件。
- `metadata.yaml` 描述更新，反映 loguru 捕获与 WebUI 能力。
- `requirements.txt` 保持为空，插件仍不依赖任何第三方包。

## [2.0.3] - 2026-08-18

### 修复

- 捕获 AstrBot 4.27+ 的 `astrbot.plugin.<插件名>` 专属 logger，不再依赖旧版本的全局 logger 传播行为。
- 共享日志回退筛选生成的副本同样经过敏感信息脱敏。

### 文档

- 说明 AstrBot 的 logger 路由方式，以及为什么旧版导出中只能看到插件自己的日志。

## [2.0.2] - 2026-08-18

### 修复

- 按插件 ID 路由 AstrBot 后台日志，避免 `send plugin` 打包整份 Core 日志。
- Handler 改用富化后的 record 进行路由，分类结果与实际来源一致。

## [2.0.1] - 2026-08-18

### 修复

- 从 AstrBot 插件注册表与 `data/plugins` 发现已安装插件，不再把「尚未产生专属日志」误报为「插件不存在」。

## [2.0.0] - 2026-08-18

首个 LogVault 版本，从 [lxfight/astrbot_plugin_logplus](https://github.com/lxfight/astrbot_plugin_logplus) fork，改用独立的插件 ID 与数据目录，不与上游混用。

### 修复

- **Linux 上活动日志丢失。** 旧版清理任务可能压缩或删除仍被 `FileHandler` 打开的当前 `plugin.log`；Linux 允许删除已打开的文件，后续写入会进入目录中不可见的 inode。现在只处理已经轮换并关闭的文件，绝不删除活动日志。
- **插件归属识别。** 修复 Windows、Linux 与混合路径分隔符下的判定，插件日志不再被错误归入 Core。
- 压缩日志保留原始修改时间，按天筛选不会把旧内容误判为新日志。

### 新增

- 按插件与天数发送：`send plugin <插件名> [天数]`。
- 只读兼容旧版 `astrbot_plugin_logplus` 数据目录，以及 `legacy_data_dirs`、`host_log_dirs` 两个额外来源配置。
- 导出与搜索移入后台线程，减少大日志文件阻塞消息处理。

[2.1.0]: https://github.com/Whereis-Alice/astrbot_plugin_logvault/compare/610c328...7fee311
[2.0.3]: https://github.com/Whereis-Alice/astrbot_plugin_logvault/compare/a1c23e5...610c328
[2.0.2]: https://github.com/Whereis-Alice/astrbot_plugin_logvault/compare/bd86a34...a1c23e5
[2.0.1]: https://github.com/Whereis-Alice/astrbot_plugin_logvault/compare/aee669d...bd86a34
[2.0.0]: https://github.com/Whereis-Alice/astrbot_plugin_logvault/commit/aee669d

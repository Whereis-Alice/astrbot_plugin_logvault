# AstrBot LogVault

LogVault 为 AstrBot 提供完整的日志留存能力：捕获、分类、轮换、压缩、清理、搜索、按天导出，并在 Dashboard 中提供一个可视化的日志中心页面。

它面向两类场景：日常保留最近日志以便回溯，以及在问题发生时向插件作者提交一份准确、范围可控的日志包。

版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 特性

- **全量捕获**。默认通过 loguru sink 采集，核心、插件与第三方库的日志走同一条通路；环境不支持时自动回退到 `logging` 处理器，两种方式互斥不重复写入。
- **自动分类**。全部记录、Core 记录、ERROR 及以上、按插件分目录，各自独立开关。
- **轮换与压缩**。按大小或时间轮换，只压缩已经关闭的归档文件，活动日志永不被清理任务删除。
- **按记录时间导出**。天数按每行日志自身的时间戳裁剪，持续写入的 `all.log` 也能只导出指定范围。
- **敏感信息脱敏**。token、password、secret 等键值在写入副本、WebUI 返回和导出包中统一遮蔽。
- **WebUI 日志中心**。五页签控制台，六套皮肤（含玻璃荧光）：分类浏览、级别过滤、实时跟随、搜索、下载、打包与清理。
- **只读兼容历史目录**。旧版 `astrbot_plugin_logplus` 数据目录与自定义历史目录参与搜索和导出，但不会被修改。
- **无第三方依赖**。`requirements.txt` 为空。

## 安装

1. 把 `astrbot_plugin_logvault` 文件夹放入 AstrBot 的 `data/plugins/` 目录。
2. 重启 AstrBot，或在 WebUI 中重新加载插件。
3. 在插件配置中确认日志级别、保留策略与脱敏设置。
4. 执行 `/log status`，确认捕获模式与生效级别符合预期。

建议先停用旧的 `astrbot_plugin_logplus` 再启用 LogVault。两者数据目录不同，但都会注册 `/logplus`，同时启用时命令归属取决于 AstrBot 的注册顺序。LogVault 无冲突的主命令是 `/log`。

## 命令

`/log` 是主命令组，`/logvault` 与 `/logplus` 是兼容别名，三种前缀等价。

```text
/log status                    查看日志状态与捕获诊断
/log search <关键词>            搜索日志
/log export [天数]              导出最近 N 天日志（默认 7 天）
/log clean                     手动清理旧日志
/log help                      显示帮助

/log send all [天数]            发送最近 N 天全部日志
/log send errors [天数]         发送最近 N 天错误日志
/log send plugin <插件名> [天数] 发送指定插件最近 N 天日志
```

示例：

```text
/log send all 1
/log send plugin dynamic_card_plus 3
/logvault export 14
```

插件名支持不区分大小写的模糊匹配；匹配到多个时会列出候选并要求更具体的名称。天数必须是 `1` 到 `3650` 的整数，省略时默认 7 天，非法值会得到明确提示。

## WebUI 日志中心

入口：**Dashboard → 插件 → LogVault → 日志中心**。

五个页签：

- **运行总览**：指标卡、采集链路、来源清单、分类占用占比、打包下载。
- **实时日志**：2 秒增量跟随，支持级别、来源标签与关键词过滤，可暂停、复制、清屏；渲染上限 2000 行。压缩文件不支持跟随。
- **日志文件**：来源 → 分类两级树，列出大小、修改时间、是否压缩、是否可删除；点击文件从右侧抽屉打开查看器，可按级别与关键词过滤、跟随、复制、下载。活动 `.log` 与共享日志目录受保护，不可删除。
- **全局搜索**：跨来源关键词搜索，带扫描行数上限保护，结果可直接跳到对应文件。
- **采集诊断**：捕获模式、写入级别与上游生效级别、已挂载 logger、已注册接口、转发与丢弃计数、排查建议。

皮肤：跟随 Dashboard、深空控制台、明昼、玻璃荧光、赛博霓虹、终端绿，另有紧凑 / 宽松两种密度。选择保存在浏览器本地。

返回内容按 `sensitive_keywords` 脱敏。

鉴权沿用 AstrBot Dashboard 自身的登录校验与插件 scope，插件不额外实现认证，页面只在已登录的 Dashboard 会话内可访问。后端接口注册在 `/astrbot_plugin_logvault/<接口名>` 下。如果运行的 AstrBot 版本不支持插件页面，注册会安静跳过，插件其余功能照常工作。

界面提供中文与英文两套文案，跟随 Dashboard 语言设置。

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

## 导出范围如何计算

`slice_by_record_time` 默认开启，天数按**每行日志自己的时间戳**裁剪：

| 情况 | 行为 |
| --- | --- |
| 文件最早一条记录已在范围内 | 原样打包，`.gz` 保持压缩不解包 |
| 文件跨越时间边界 | 重写为只含范围内的行；`.gz` 会解压后以去掉 `.gz` 后缀的明文存入 |
| 裁剪后为空 | 整个文件跳过，不计入文件数 |
| 文件没有可解析的时间戳 | 整份包含 |

关闭该开关则退回按文件修改时间整份打包。zip 内的 `ABOUT.txt` 会写明本次导出的范围。

## 配置

### 捕获

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `log_level` | `DEBUG` | LogVault 写入文件的最低级别。这只是下限，上游 logger 级别更高时低级别记录根本不会产生，需要配合 `force_source_debug` |
| `capture_mode` | `auto` | `auto` 优先 loguru sink 并在失败时回退；`loguru` 强制 sink；`logging` 只挂 logging 处理器 |
| `capture_third_party` | `true` | 仅在 `capture_mode=logging` 时生效，额外挂载 root logger 以捕获第三方库 |
| `force_source_debug` | `false` | 把 `astrbot` 与 `astrbot.plugin.*` logger 级别降到 `log_level`。会让 AstrBot 控制台更啰嗦 |
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
| `clean_interval_minutes` | `60` | 清理执行间隔，范围 1 至 10080 分钟 |
| `max_total_size_mb` | `500` | 目录总大小上限，超出后删除最旧的归档文件；活动日志不会被删 |
| `max_age_days` | `30` | 归档日志最长保留天数 |

### 导出与隐私

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `slice_by_record_time` | `true` | 按日志内记录时间裁剪导出范围 |
| `enable_sensitive_filter` | `true` | 脱敏 token、password、secret 等键值 |
| `sensitive_keywords` | `token,password,secret,api_key,apikey,access_key,accesskey` | 逗号分隔的自定义敏感关键词 |
| `include_legacy_data` | `true` | 是否读取旧版 `astrbot_plugin_logplus` 目录 |
| `legacy_data_dirs` | `[]` | 额外历史目录，每行一个路径 |
| `host_log_dirs` | `[]` | 额外的 AstrBot 共享日志目录；默认自动读取 `data/logs` 与核心配置中的日志路径 |

脱敏只作用于 LogVault 写入的副本、WebUI 返回的内容和导出包，不会修改 AstrBot 控制台日志或其他 Handler 的输出。发送日志前仍应确认内容不含不该公开的数据。

## 排查日志不全

按顺序检查：

1. 执行 `/log status`，查看「日志捕获」段落。
2. 模式显示为兼容模式而你期望全量捕获 → loguru sink 注册失败，诊断中的警告会说明原因。
3. `astrbot 生效级别` 高于 `写入级别`（例如生效 INFO、写入 DEBUG）→ 低级别记录在源头被丢弃，开启 `force_source_debug`，或直接调低 AstrBot 控制台级别。
4. 缺的是插件加载完成之前那一段 → 确认 `backfill_startup_logs` 已开启，必要时提高 `backfill_limit`。
5. 缺的是 AstrBot 框架内部的细粒度链路 → 开启 `include_trace_log`。
6. 日志确实存在但导出里没有 → 检查天数与 `slice_by_record_time`，或用 WebUI 日志中心直接确认文件内容。

## 从旧版迁移

1. 停用旧插件或停止 AstrBot。
2. 安装 LogVault 并启动一次，让它创建新的数据目录。
3. 旧日志仍在默认的 `data/plugin_data/astrbot_plugin_logplus/` 时无需复制，会被自动读取。
4. 旧日志在备份目录时，填入 `legacy_data_dirs` 后再使用 `send` / `export` / WebUI。
5. 重启 AstrBot（或在 WebUI 中完整卸载并重新加载），让目标插件产生一条新日志后再验证。

如果使用了自定义 AstrBot 日志路径，LogVault 会从核心配置自动发现；仍未发现时，把该日志所在目录逐行填入 `host_log_dirs`。

## 开发

```bash
python -m compileall -q main.py core
python -m unittest discover -s tests
```

## 许可与致谢

本项目遵循 GNU Affero General Public License v3.0（AGPL-3.0）。

项目仓库：[Whereis-Alice/astrbot_plugin_logvault](https://github.com/Whereis-Alice/astrbot_plugin_logvault)

感谢 [lxfight/astrbot_plugin_logplus](https://github.com/lxfight/astrbot_plugin_logplus) 提供原始实现与功能设计。本项目是面向实际部署的兼容性与可靠性改进版，保留上游许可与致谢；这里的问题修复与新增功能不代表上游项目的官方发布。

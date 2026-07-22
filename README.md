# MoviePilot 第三方插件

MoviePilot 官方插件市场：[https://github.com/jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)

## 安装说明

MoviePilot 环境变量 `PLUGIN_MARKET` 添加本项目地址，多个插件市场地址使用英文逗号分隔。

```text
https://github.com/byangmath/RecentEpisodeMaintenance
```

MoviePilot V2 也可以在插件页面右上角的插件市场设置中添加上述地址。

添加后刷新插件市场，安装“最近剧集维护”。插件市场仓库地址需使用 GitHub 仓库 `main` 分支。

## 插件说明

### 1. 最近剧集维护

定时维护 MoviePilot 最近整理入库的 Jellyfin 剧集，适用于新剧信息更新滞后以及旧剧重新入库后刮削结果不完整的情况。

主要功能：

- 根据 MoviePilot 最近 N 天的成功整理记录，比较当前整理预览标题与 Jellyfin 标题，仅在两者不一致时刷新元数据和图片；
- 根据同一批整理记录重新执行 MoviePilot 整理，使文件名应用当前的剧集标题；
- 刷新或重新整理后在下一轮优先复查，确认元数据和命名正常后即完成，不再重复占用处理名额；
- 尚未执行过刷新或重新整理的正常记录，按 24、48、72 小时逐步降低复查频率；已入队记录即使超过最近 N 天也会保留到完成；
- 支持选择媒体服务器和维护媒体库；
- 支持试运行、单次最大处理数量和执行摘要通知；单次数量限制刷新与重新整理的合计操作数，每轮最多检查该数量五倍的记录，无实际动作和失败时不发送通知。

配置项：执行周期，最近 N 天，媒体服务器，维护媒体库，刷新模式，是否替换现有图片，试运行模式，单次最大处理数量。

详细配置说明和推荐测试步骤见：[最近剧集维护插件说明](plugins.v2/recentepisodemaintenance/README.md)

## 使用建议

首次使用建议开启试运行模式，并将单次最大处理数量设置为 1，确认日志中的整理记录、Jellyfin 条目和预览文件名正确后再正式启用。

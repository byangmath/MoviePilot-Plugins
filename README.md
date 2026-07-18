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

定时维护 Jellyfin 中最近发布的剧集，用于解决追剧时 TMDb 剧集标题、简介等信息更新滞后的问题。

主要功能：

- 刷新最近 N 天首播剧集的 Jellyfin 元数据；
- 可选调用 MoviePilot 整理链路重新整理剧集文件；
- 支持 Jellyfin 到 MoviePilot 的媒体路径映射；
- 支持试运行、单次最大处理数量和执行摘要通知。

配置项：执行周期，最近 N 天，Jellyfin 地址，API Key，路径映射，试运行模式，单次最大处理数量。

详细配置说明和推荐测试步骤见：[最近剧集维护插件说明](plugins.v2/recentepisodemaintenance/README.md)

## 使用建议

首次使用建议开启试运行模式，并将单次最大处理数量设置为 1，确认 Jellyfin 查询、路径映射和 MoviePilot 整理链路正常后再正式启用。

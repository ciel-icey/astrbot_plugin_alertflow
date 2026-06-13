# ChronoPing - 智能定时提醒插件

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-blue)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/github/license/ciel-icey/ChronoPing)](https://github.com/你的用户名/ChronoPing/blob/main/LICENSE)

**ChronoPing** 是一款为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的智能定时提醒插件。它支持多种时间表达方式，能够在指定时刻向群聊或个人发送提醒，并灵活 @指定用户。通过可视化的配置面板，你还可以设置群组/个人白名单，精准控制功能使用权限。

---

##  功能特性

-  **丰富的时间格式**  
  支持纯数字秒数、相对时间（`5分钟`、`2小时`、`1天`）、中文口语（`今天19点`、`明天下午3点`）、绝对日期时间（`2026-06-12 18:27:00`）等多种表达。

-  **精准 @提醒**  
  可在提醒内容中 @一个或多个用户，确保重要消息及时送达。

-  **完整的提醒管理**  
  提供 `添加`、`查看`、`删除`、`清空`、`立即提醒` 等指令，轻松管理所有待办提醒。

-  **灵活的白名单控制**  
  通过配置面板可分别启用群组白名单或用户白名单，限定可用范围，适配不同场景需求。

-  **持久可靠**  
  基于 `asyncio` 实现异步定时任务，提醒精确到秒，重启前未触发的任务会被自动清理。

---

##  安装

### 方法一：通过 AstrBot 插件市场安装（推荐）
待插件上架市场后，可直接在 AstrBot 管理面板的「插件市场」中搜索 `ChronoPing` 并安装。

### 方法二：手动安装
1. 将本仓库克隆或下载到 AstrBot 的插件目录（通常为 `astrbot/plugins/`）：
   ```bash
   cd astrbot/plugins/
   git clone https://github.com/你的用户名/ChronoPing.git
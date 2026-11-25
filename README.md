# NodeSeek 签到助手

独立的 MoviePilot V3 插件，插件 ID 为 `NodeSeekSignCustom`。支持浏览器签到入口、北京时间每日提醒，以及最近 30 条的签到确认记录。最低要求为 MoviePilot V3.2.12。

## 安装

将本仓库的 `main` 分支发布到 GitHub 后，在 MoviePilot 的插件市场添加仓库地址 `https://github.com/Azhz-Zhong/moviepilot-nodeseek-custom`，搜索「NodeSeek 签到助手」，安装后打开插件详情页。

## 签到和提醒

1. 在插件详情页点「打开 NodeSeek」，在论坛网页完成登录、邮件确认和签到。
2. 签到完成后，回到 MoviePilot 的插件配置，勾选「我已在论坛完成今天的签到」并保存。当天只写一次记录，详情页会显示确认时间。
3. 需要提醒时启用「每日提醒」并设定北京时间。提醒使用 MoviePilot 已配置的插件通知渠道。已经确认签到的那天会跳过提醒。

签到确认是本地记录，论坛上的实际签到结果以 NodeSeek 页面显示为准。登录与验证都在站点的浏览器页面完成。插件数据只保存本地签到日期、确认时间和提醒日期。

## 开发结构

- `package.v3.json` 是插件市场索引。
- `plugins.v3/nodeseeksigncustom/__init__.py` 是插件入口，使用 MoviePilot V3 SDK 加载配置、保存记录和发送提醒。
- `har/` 是本地调试目录，已通过 Git 忽略。开发或测试使用去标识化的数据。

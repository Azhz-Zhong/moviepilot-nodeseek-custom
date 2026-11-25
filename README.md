# NS每日签到

独立的 MoviePilot V3 插件，插件 ID 为 `NodeSeekSignCustom`。最低要求为 MoviePilot V3.2.12。

## 安装

在 MoviePilot 的插件市场添加 `https://github.com/Azhz-Zhong/moviepilot-nodeseek-custom`，搜索「NS每日签到」并安装。已安装时可直接检查插件更新。

## 配置和运行

1. 在浏览器登录 NodeSeek，完成论坛要求的邮件确认或浏览器验证。
2. 打开插件设置，将浏览器复制的完整 Cookie 粘贴到「登录后的 Cookie」。插件仅保留 `session` 和 `pjwt`。输入框保存后恢复为空，Cookie 存放在飞牛的插件数据目录。
3. 默认使用 MoviePilot 系统代理，地址取自「设定 → 系统 → 网络」。如需通知，打开「推送签到结果通知」，并在 MoviePilot 通知渠道中启用「站点」类型。
4. 勾选「保存并立即签到一次」做首次验证，详情页可查看结果。验证通过后开启每日自动签到，设定北京时间和奖励模式。图表显示近 7 天的实际收益。

当站点要求重新登录或浏览器验证，插件暂停提交，直到更新 Cookie。连接中断等无法判断的情况记录为待确认，先在浏览器核对当天结果。已经签到的同一天会跳过后续请求。

图表使用站点实际返回的收益。旧版手动确认记录仍可查看，手动确认时不估算鸡腿数量。

## 开发结构

- `package.v3.json` 是插件市场索引。
- `plugins.v3/nodeseeksigncustom/` 包含插件入口和签到请求逻辑。
- `har/` 属于本地调试，已通过 Git 忽略。测试只用合成数据。
- 插件卡片图标源自 NodeSeek 官网公开的站点图标。

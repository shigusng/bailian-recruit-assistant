# 便携版 · Windows 64 位

**[下载最新便携版 ZIP](https://github.com/shigusng/bailian-recruit-assistant/releases/latest/download/BailianHero-AutoGacha-Windows-x64.zip)** · [查看发布版本](https://github.com/shigusng/bailian-recruit-assistant/releases/latest) · [切换到源码版](source.md)

便携版适合直接使用：自带 Python、OCR 模型、DirectML 运行库、ADB、角色图片和背景，无需安装 Python 或 CUDA。

## 开始使用

1. 下载 `BailianHero-AutoGacha-Windows-x64.zip`，完整解压到可写目录。
2. 双击 `百炼英雄抽取助手.exe`，保留同级 `_internal` 文件夹。
3. 启动模拟器，在游戏中手动打开「常驻招募」。画面需为竖屏 **1080×2400 或相同比例**。
4. 打开左上角「设置」，填写设备序列号。ADB 默认填 `adb`，使用内置版本；也可选择模拟器自己的 `adb.exe`。
5. 点击「环境检测」，检查连接和识别环境。程序优先使用可用独显，否则使用 CPU。
6. 勾选目标，先点击「检测勾选角色」查看匹配结果，再设置抽取上限并点击「开始抽取」。

轮数或金币上限为 `0` 时，对应上限不限制。点击「停止」可停止后续抽取。

```text
百炼英雄抽取助手/
├─ 百炼英雄抽取助手.exe
├─ 使用说明.txt
├─ 抽取角色名单.csv
└─ _internal/
```

不要只复制 EXE，也不要直接在压缩包中运行。换电脑时完整复制这个目录即可，角色图片已包含在 `_internal/assets/characters/` 中，不依赖原电脑上的图片位置。

## 名单、统计和日志

- 编辑 `抽取角色名单.csv`，再通过「导入 CSV」替换名单；新增角色需要手动勾选。CSV 不包含图片，新增 ID 需要有对应的识别模板。
- 「抽中统计 → 抽取记录」查看每次抽取的角色数量、总次数与起止时间。
- 「设置 → 错误日志」查看、复制或导出错误详情。
- 设置和累计统计保存到 EXE 同级的 `settings.json`，历史保存到 `draw_history.json`，日志和诊断截图分别位于 `error_logs/`、`run_logs/`。

便携包不包含个人设置和历史。升级时先关闭程序，再将旧的 `settings.json`、`draw_history.json` 复制到新程序目录；旧 ADB 路径失效时改填 `adb`。

发布包完成构建和文件完整性核对，未执行游戏运行测试。完整功能与抽取策略见 [仓库首页](../README.md)。

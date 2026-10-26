# 源码版

**[下载最新源码 ZIP](https://github.com/shigusng/bailian-recruit-assistant/archive/refs/heads/main.zip)** · [浏览源码](../) · [切换到便携版](portable.md)

源码版适合自行运行、修改代码和重新打包。角色图片、背景、默认名单及构建脚本均包含在仓库中；需要自行准备 Windows 64 位 Python **3.10～3.12** 和 ADB。

## 安装与启动

下载源码 ZIP 并解压，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-gui.txt
.\.venv\Scripts\python.exe auto_gacha.py
```

也可以克隆仓库后执行以上命令：

```powershell
git clone https://github.com/shigusng/bailian-recruit-assistant.git
cd bailian-recruit-assistant
```

双击 `启动抽取面板.bat` 会使用 PATH 中的 Python；使用虚拟环境时请使用以上显式启动命令。

## 连接模拟器

1. 启动模拟器，手动打开游戏的「常驻招募」。无需设置固定分辨率或宽高比，保持界面完整可见、文字清晰。
2. 在「设置」中选择 `adb.exe` 并填写设备序列号；ADB 已加入 PATH 时可填写 `adb`。
3. 点击「环境检测」，再使用「检测勾选角色」检查匹配结果。
4. 勾选目标，设置轮数和金币上限后开始抽取。

也可点击「抽取到保底出红」：抽取前幸运值达到 `24/25` 后，只再完成一次保底抽取即停止。原有倍率策略和抽取上限仍生效；详情见 [保底模式说明](../README.md#抽取到保底出红)。

## 可选 GPU 加速

```powershell
.\.venv\Scripts\python.exe -m pip install --target .gpu-deps --no-deps -r requirements-gpu.txt
```

程序优先使用可用独显，通过 DirectML 运行 OCR；无独显或 GPU 不可用时使用 CPU。无需额外安装 CUDA。

## 本地打包

准备完整的 Android Platform Tools 目录，然后执行：

```powershell
$env:BAILIAN_BUILD_ADB_DIR = 'C:\Android\platform-tools'
.\打包程序.bat
```

输出在 `dist/北京时间戳/`，包括完整程序目录和 `BailianHero-AutoGacha-Windows-x64.zip`。构建脚本不启动程序、不连接模拟器，也不执行游戏测试。

设置、统计和日志保存在项目根目录，已加入 Git 忽略规则。完整功能、CSV 格式和项目结构见 [仓库首页](../README.md)。

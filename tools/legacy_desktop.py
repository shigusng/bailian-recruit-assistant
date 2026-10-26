#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
旧版桌面坐标与颜色校准工具，不参与当前 ADB 抽取流程。

从项目根目录使用 python auto_gacha.py cal / snap / test。
这些工具另需安装 pyautogui。
"""

import sys
from pathlib import Path
import time
import pyautogui
from PIL import ImageGrab, ImageDraw


# =====================================================================
#  【配置区】根据实际情况调整下方参数
# =====================================================================

# --- 游戏画面区域（绝对坐标）---
# 运行 `python auto_gacha.py cal` 进入校准模式来确定这些值
# 游戏内容区域左上角坐标（排除模拟器标题栏和工具栏）
GAME_LEFT = 665   # 游戏画面左边界 X
GAME_TOP  = 63    # 游戏画面上边界 Y（排除标题栏约40px）
GAME_W    = 545   # 游戏画面宽度（像素）
GAME_H    = 858   # 游戏画面高度（像素）

# --- 英雄颜色采样点（相对于游戏画面的比例，范围 0.0~1.0）---
# 采样点位置应指向英雄卡片的彩色边框/光圈处
# 可通过截图标注模式（snap）来验证这些位置是否正确
HERO_SAMPLE_RATIOS = [
    (0.22, 0.56),   # 左侧英雄卡片边框采样点
    (0.50, 0.61),   # 中间英雄卡片边框采样点
    (0.78, 0.56),   # 右侧英雄卡片边框采样点
]

# --- 按钮位置（相对于游戏画面的比例）---
BTN_MAX      = (0.81, 0.78)   # "最大" 按钮
BTN_COIN     = (0.53, 0.85)   # 金币招募按钮（100金币 / 300金币）
BTN_ABANDON  = (0.53, 0.91)   # "放弃" 按钮（与招募按钮同位置，不同界面）

# --- 界面状态检测采样点（判断当前是"选择界面"还是"结果界面"）---
# 放弃按钮特有的颜色区域（结果界面底部按钮颜色不同于招募按钮）
ABANDON_DETECT_RATIO = (0.53, 0.91)   # 放弃按钮区域
# 放弃按钮的绿色特征（招募界面按钮为绿色，结果界面放弃按钮为灰色/蓝色）
ABANDON_BTN_COLOR_THRESHOLD = 150  # 若 G 通道大于此值则认为是绿色招募按钮

# --- 延迟配置（单位：秒）---
DELAY_CLICK      = 0.4   # 普通点击间隔
DELAY_AFTER_MAX  = 0.3   # 点击最大按钮后等待
DELAY_DRAW_ANIM  = 3.0   # 抽卡动画播放等待时间
DELAY_RESULT     = 1.0   # 结果界面稳定等待时间
DELAY_NEXT_ROUND = 0.8   # 返回选择界面等待时间

# --- 程序配置 ---
MAX_ROUNDS       = 0     # 最大轮数（0 = 无限循环）
STARTUP_COUNTDOWN = 5    # 启动倒计时（秒）


# =====================================================================
#  稀有度颜色判断
# =====================================================================

def is_high_rarity(r: int, g: int, b: int) -> bool:
    """
    判断 RGB 颜色是否属于高稀有度（黄色/金色/红色）。
    黄色/金色特征：R 高，G 中等偏高，B 低。
    红色特征：R 高，G 低，B 低。
    """
    # 黄色 / 金色 / 橙色边框
    if r > 170 and g > 110 and b < 100:
        return True
    # 红色边框
    if r > 170 and g < 80 and b < 80:
        return True
    return False


def classify_color(r: int, g: int, b: int) -> str:
    """将 RGB 颜色归类为英雄稀有度标签（用于显示）。"""
    if r > 170 and g > 110 and b < 100:
        return "黄/金色"
    if r > 170 and g < 80 and b < 80:
        return "红色"
    if r < 130 and b > 160:
        return "蓝色"
    if r > 100 and b > 140 and g < 120:
        return "紫色"
    return "未知"


# =====================================================================
#  基础工具函数
# =====================================================================

def to_screen(rx: float, ry: float) -> tuple:
    """将游戏画面内的相对坐标（比例）转换为屏幕绝对坐标。"""
    return (
        GAME_LEFT + int(GAME_W * rx),
        GAME_TOP  + int(GAME_H * ry),
    )


def get_pixel(x: int, y: int) -> tuple:
    """读取屏幕指定坐标的 RGB 像素颜色。"""
    img = ImageGrab.grab(bbox=(x, y, x + 2, y + 2))
    pixel = img.getpixel((0, 0))
    return pixel[:3]  # 返回 (R, G, B)


def click_at(x: int, y: int, delay: float = None):
    """点击屏幕绝对坐标，并等待指定延迟。"""
    pyautogui.click(x, y)
    time.sleep(delay if delay is not None else DELAY_CLICK)


def click_ratio(rx: float, ry: float, delay: float = None):
    """按游戏画面相对坐标点击。"""
    x, y = to_screen(rx, ry)
    click_at(x, y, delay)


# =====================================================================
#  英雄稀有度检测
# =====================================================================

def sample_hero_colors() -> list:
    """
    采样三个英雄卡片的边框颜色。
    返回: [(r,g,b), ...] 列表，每个元素对应一个英雄的采样颜色
    """
    colors = []
    for rx, ry in HERO_SAMPLE_RATIOS:
        x, y = to_screen(rx, ry)
        colors.append(get_pixel(x, y))
    return colors


def check_heroes() -> bool:
    """
    检测当前三位英雄是否包含高稀有度（黄/红）英雄。
    同时打印每个英雄的颜色信息。
    返回: True=有高稀有度，False=全为低稀有度
    """
    has_high = False
    colors = sample_hero_colors()

    for i, (r, g, b) in enumerate(colors):
        rarity = classify_color(r, g, b)
        high = is_high_rarity(r, g, b)
        tag = "★ 高稀有度!" if high else "  普通"
        print(f"    英雄{i+1}: RGB({r:3d},{g:3d},{b:3d})  [{rarity:^6s}]  {tag}")
        if high:
            has_high = True

    return has_high


# =====================================================================
#  界面状态检测
# =====================================================================

def is_result_screen() -> bool:
    """
    检测当前是否为抽卡结果界面（"放弃"按钮所在界面）。
    通过检测底部按钮区域的颜色来区分：
      - 招募界面：绿色大按钮（G通道值高）
      - 结果界面：放弃按钮颜色不同（灰蓝色）
    """
    x, y = to_screen(*ABANDON_DETECT_RATIO)
    r, g, b = get_pixel(x, y)
    # 绿色招募按钮 G 通道明显更高
    return g < ABANDON_BTN_COLOR_THRESHOLD


def wait_for_result_screen(timeout: float = 6.0):
    """等待直到出现结果界面，超时则继续。"""
    start = time.time()
    while time.time() - start < timeout:
        if is_result_screen():
            return True
        time.sleep(0.3)
    return False


def wait_for_select_screen(timeout: float = 6.0):
    """等待直到回到选择界面（招募界面），超时则继续。"""
    start = time.time()
    while time.time() - start < timeout:
        if not is_result_screen():
            return True
        time.sleep(0.3)
    return False


# =====================================================================
#  抽卡操作
# =====================================================================

def set_max_multiplier():
    """点击最大倍数按钮，将倍数调到最高（3倍）。"""
    click_ratio(*BTN_MAX, delay=DELAY_AFTER_MAX)


def do_recruit(use_max: bool = False):
    """
    执行招募操作。
    use_max=True:  先点最大倍数，再点 300金币招募
    use_max=False: 先重置倍数，再点 100金币招募
    """
    if use_max:
        print("    → 点击[最大]按钮...")
        set_max_multiplier()

    btn_label = "300金币×3" if use_max else "100金币"
    print(f"    → 点击[{btn_label}]招募按钮...")
    click_ratio(*BTN_COIN)


def do_abandon():
    """点击结果界面的放弃按钮。"""
    print("    → 点击[放弃]按钮...")
    click_ratio(*BTN_ABANDON)
    time.sleep(DELAY_NEXT_ROUND)


# =====================================================================
#  主抽卡循环
# =====================================================================

def run_auto_gacha():
    """自动抽卡主循环。"""
    print("=" * 56)
    print("       百炼英雄 - 自动常驻招募程序")
    print("=" * 56)
    print(f"  游戏画面区域: 左={GAME_LEFT}, 上={GAME_TOP}, "
          f"宽={GAME_W}, 高={GAME_H}")
    print(f"  最大轮数: {'无限' if MAX_ROUNDS == 0 else MAX_ROUNDS}")
    print("  按 Ctrl+C 可随时安全停止程序")
    print()

    # 启动倒计时（给用户切换到游戏窗口的时间）
    for i in range(STARTUP_COUNTDOWN, 0, -1):
        print(f"  程序将在 {i} 秒后开始，请切换到游戏窗口...", end="\r")
        time.sleep(1)
    print(" " * 60)
    print("  开始运行！\n")

    round_num   = 0
    high_count  = 0
    low_count   = 0
    total_coins = 0

    try:
        while True:
            round_num += 1
            if MAX_ROUNDS > 0 and round_num > MAX_ROUNDS:
                print(f"\n已完成 {MAX_ROUNDS} 轮，程序正常退出。")
                break

            print(f"┌─── 第 {round_num} 轮 ───────────────────────────")
            print(f"│ 检测三位英雄稀有度...")

            # 1. 检测英雄稀有度
            has_high = check_heroes()

            # 2. 执行招募
            if has_high:
                high_count += 1
                total_coins += 300
                print(f"│ 发现高稀有度英雄！→ 使用最大倍数(300金币)招募")
                do_recruit(use_max=True)
            else:
                low_count += 1
                total_coins += 100
                print(f"│ 均为蓝/紫英雄 → 使用100金币招募")
                do_recruit(use_max=False)

            # 3. 等待抽卡动画结束
            print(f"│ 等待抽卡动画({DELAY_DRAW_ANIM}s)...")
            time.sleep(DELAY_DRAW_ANIM)

            # 4. 等待结果界面出现并点击放弃
            if wait_for_result_screen(timeout=5.0):
                time.sleep(DELAY_RESULT)
                do_abandon()
            else:
                # 若未检测到结果界面，仍尝试点击放弃位置
                print(f"│ [警告] 未检测到结果界面，尝试点击放弃...")
                do_abandon()

            # 5. 等待回到选择界面
            wait_for_select_screen(timeout=2.0)

            print(f"│ [统计] 共{round_num}轮 | "
                  f"高稀有:{high_count}次 | 普通:{low_count}次 | "
                  f"花费金币:{total_coins}")
            print(f"└{'─' * 44}")
            print()

    except KeyboardInterrupt:
        print("\n\n用户手动停止程序。")

    print(f"\n{'=' * 56}")
    print(f"  运行结束统计:")
    print(f"    总轮数   : {round_num}")
    print(f"    高稀有   : {high_count} 次")
    print(f"    普通     : {low_count} 次")
    print(f"    消耗金币 : {total_coins}")
    print(f"{'=' * 56}")


# =====================================================================
#  校准工具
# =====================================================================

def calibrate_mode():
    """
    校准模式：实时显示鼠标坐标和该位置的像素颜色。
    移动鼠标到目标位置，即可读取坐标；按 Ctrl+C 退出。
    """
    print("=" * 56)
    print("  校准模式 - 移动鼠标查看坐标和像素颜色")
    print("  按 Ctrl+C 退出校准模式")
    print("=" * 56)
    print()
    try:
        while True:
            x, y = pyautogui.position()
            r, g, b = get_pixel(x, y)
            high = is_high_rarity(r, g, b)
            rarity = classify_color(r, g, b)
            # 计算相对于游戏画面的比例坐标
            rx = (x - GAME_LEFT) / GAME_W
            ry = (y - GAME_TOP) / GAME_H
            print(
                f"\r  屏幕坐标:({x:4d},{y:4d})  "
                f"游戏比例:({rx:.2f},{ry:.2f})  "
                f"RGB({r:3d},{g:3d},{b:3d})  "
                f"[{rarity:^6s}]  {'★高稀有!' if high else '  普通  '}",
                end="",
                flush=True,
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n退出校准模式。")


def snapshot_mode():
    """
    截图模式：截取游戏区域并在图上标注采样点和按钮位置，
    保存为 gacha_debug.png，用于验证坐标是否正确。
    """
    print("正在截取游戏区域并标注采样点...")
    img = ImageGrab.grab(bbox=(
        GAME_LEFT, GAME_TOP,
        GAME_LEFT + GAME_W,
        GAME_TOP + GAME_H,
    ))
    draw = ImageDraw.Draw(img)

    # 标注英雄采样点（红色圆圈）
    for i, (rx, ry) in enumerate(HERO_SAMPLE_RATIOS):
        px = int(GAME_W * rx)
        py = int(GAME_H * ry)
        draw.ellipse([px-6, py-6, px+6, py+6], outline="red", width=2)
        draw.text((px + 8, py - 10), f"Hero{i+1}", fill="red")

    # 标注按钮位置（蓝色圆圈）
    buttons = {
        "MAX":     BTN_MAX,
        "COIN":    BTN_COIN,
        "ABANDON": BTN_ABANDON,
        "DETECT":  ABANDON_DETECT_RATIO,
    }
    for name, (rx, ry) in buttons.items():
        px = int(GAME_W * rx)
        py = int(GAME_H * ry)
        draw.ellipse([px-6, py-6, px+6, py+6], outline="blue", width=2)
        draw.text((px + 8, py - 10), name, fill="blue")

    out_path = Path(__file__).resolve().parents[1] / 'run_logs' / 'legacy_debug.png'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    print(f"已保存标注截图: {out_path}")
    print("  红色圆圈 = 英雄颜色采样点")
    print("  蓝色圆圈 = 按钮位置")


def test_detect_mode():
    """
    单次检测测试：打印当前三个英雄的颜色采样结果。
    """
    print("=" * 56)
    print("  单次检测模式 - 检测当前英雄颜色")
    print("=" * 56)
    print("5秒后开始检测，请切换到游戏窗口...")
    time.sleep(5)

    print("\n检测英雄颜色：")
    has_high = check_heroes()

    print()
    if has_high:
        print(">> 结论：包含高稀有度英雄 → 建议使用最大倍数(300金币)招募")
    else:
        print(">> 结论：均为蓝/紫英雄 → 建议使用100金币招募")

    # 额外显示界面状态检测结果
    detect_x, detect_y = to_screen(*ABANDON_DETECT_RATIO)
    r, g, b = get_pixel(detect_x, detect_y)
    print(f"\n界面状态检测点颜色: RGB({r},{g},{b})")
    print(f"  -> {'结果界面(放弃按钮)' if is_result_screen() else '选择界面(招募按钮)'}")


# =====================================================================
#  程序入口
# =====================================================================

def main(mode=None):
    # 启用鼠标移动到屏幕左上角时的紧急停止
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05

    mode = mode or (sys.argv[1].lower() if len(sys.argv) > 1 else '')

    if mode == "cal":
        calibrate_mode()
    elif mode == "snap":
        snapshot_mode()
    elif mode == "test":
        test_detect_mode()
    else:
        raise SystemExit('旧版校准工具用法：python auto_gacha.py [cal|snap|test]')


if __name__ == '__main__':
    main()

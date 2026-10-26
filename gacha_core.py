"""ADB recruitment, character recognition and explicit 1x/3x selection."""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
import io
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time

from runtime_paths import BASE, RESOURCE_BASE, resolve_adb

if not getattr(sys, 'frozen', False):
    if (BASE / '.deps').is_dir():
        sys.path.insert(0, str(BASE / '.deps'))
    if (BASE / '.gpu-deps').is_dir():
        sys.path.insert(0, str(BASE / '.gpu-deps'))
import cv2
import numpy as np
from screen_geometry import (CaptureGeometry, REFERENCE_REGIONS, find_decrease,
                             recruitment_regions, reference_regions, shard_bar_centers)

ASSETS = RESOURCE_BASE / 'assets'
DEFAULT_ADB = 'adb'
REGIONS = list(REFERENCE_REGIONS)
SCAN_INTERVAL = .1


def read_image(path):
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f'无法读取图像：{path}')
    return image


def load_roles(path):
    """Accept Excel's GB18030 and UTF-8 CSV, including blank rows/headers."""
    data = Path(path).read_bytes()
    for encoding in ('utf-8-sig', 'gb18030', 'utf-16'):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('名单编码无法识别，请保存为 UTF-8 CSV')
    names = {n['id']: n['name'] for n in json.loads((ASSETS / 'hero_names.json').read_text(encoding='utf-8'))}
    roles, seen = [], set()
    for row in csv.reader(io.StringIO(text, newline='')):
        if not row or not any(v.strip() for v in row):
            continue
        match = re.fullmatch(r'(8[1-7]\d{3}(?:\d{3})?)(?:\.png)?', row[0].strip(), re.I)
        if not match:
            if not roles and row[0].strip().lower() in ('id', '角色id', '文件名', '图片', '图片名'):
                continue
            raise ValueError(f'无法识别角色 ID：{row[0]}')
        hero_id = match.group(1)
        if not (ASSETS / 'characters' / f'{hero_id}.png').is_file():
            raise ValueError(f'缺少解包图像：{hero_id}.png')
        if hero_id not in seen:
            roles.append({'id': hero_id, 'name': row[1].strip() if len(row) > 1 and row[1].strip() else names.get(hero_id, hero_id)})
            seen.add(hero_id)
    if not roles:
        raise ValueError('名单中没有角色')
    return roles


def read_settings(path=BASE / 'settings.json'):
    if Path(path).exists():
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        roles = data.get('roles') or load_roles(ASSETS / 'triple_roles.csv')
        data['roles'] = roles
        # An empty selection is intentional: never fall back to all roles.
        data.setdefault('selected_ids', [r['id'] for r in roles])
        data.setdefault('click_jitter', True)
        return data
    roles = load_roles(ASSETS / 'triple_roles.csv')
    return {'roles': roles, 'selected_ids': [r['id'] for r in roles], 'adb': DEFAULT_ADB,
            'serial': 'emulator-5554', 'max_rounds': 20, 'coin_limit': 3000, 'click_jitter': True}


def save_settings(data, path=BASE / 'settings.json'):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def choose_multiplier(visible_ids, selected_ids):
    selected = set(selected_ids)
    # A selected base character also covers its skin IDs; selecting one skin
    # alone does not enable every skin of that base character.
    return 3 if any(hero and (hero in selected or hero[:5] in selected) for hero in visible_ids) else 1


@dataclass
class HeroMatch:
    id: str | None
    name: str
    inliers: int = 0
    score: float = 0.0
    slot: int | None = None


class HeroRecognizer:
    def __init__(self):
        self.names = {n['id']: n['name'] for n in json.loads((ASSETS / 'hero_names.json').read_text(encoding='utf-8'))}
        self.sift = cv2.SIFT_create(nfeatures=500)
        self.matcher = cv2.BFMatcher()
        self.paths = sorted((ASSETS / 'characters').glob('*.png'))
        self.template_cache = {}

    def templates_for(self, selected_ids):
        selected = set(selected_ids)
        templates = []
        for path in self.paths:
            if path.stem not in selected and path.stem[:5] not in selected:
                continue
            if path.stem not in self.template_cache:
                image = read_image(path)
                mask = cv2.erode((image[:, :, 3] > 220).astype(np.uint8) * 255, np.ones((3, 3), np.uint8))
                keys, desc = self.sift.detectAndCompute(cv2.cvtColor(image[:, :, :3], cv2.COLOR_BGR2GRAY), mask)
                self.template_cache[path.stem] = (path.stem, image, keys, desc) if desc is not None else None
            template = self.template_cache[path.stem]
            if template is not None:
                templates.append(template)
        return templates

    def recognize_region(self, region, templates):
        if region.size == 0 or min(region.shape[:2]) < 8:
            return HeroMatch(None, '未匹配到勾选角色')
        keys, desc = self.sift.detectAndCompute(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY), None)
        if desc is None or len(keys) < 8:
            return HeroMatch(None, '未匹配到勾选角色')
        candidates = []
        for hero_id, image, tkeys, tdesc in templates:
            pairs = self.matcher.knnMatch(tdesc, desc, k=2)
            good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .7 * p[1].distance]
            if len(good) < 6:
                continue
            src = np.float32([tkeys[m.queryIdx].pt for m in good])
            dst = np.float32([keys[m.trainIdx].pt for m in good])
            affine, valid = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4)
            count = int(valid.sum()) if valid is not None else 0
            if affine is None or count < 6 or count < len(good) * .6:
                continue
            scale = float(np.hypot(affine[0, 0], affine[1, 0]))
            angle = abs(float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0]))))
            if not .35 <= scale <= 6. or angle > 18:
                continue
            # Geometry plus RGB agreement avoids a few generic eyes/edges
            # making a different hero count as a selected character.
            warped = cv2.warpAffine(image[:, :, :3], affine, (region.shape[1], region.shape[0]))
            mask = cv2.warpAffine((image[:, :, 3] > 220).astype(np.uint8), affine, (region.shape[1], region.shape[0]), flags=cv2.INTER_NEAREST)
            mask = cv2.erode(mask, np.ones((5, 5), np.uint8)).astype(bool)
            if mask.sum() < 500:
                continue
            a, b = warped[mask].astype(float), region[mask].astype(float)
            error = float(np.sum((a - b) ** 2) / max(1., np.sqrt(np.sum(a * a) * np.sum(b * b))))
            score = 1. - error
            if score >= .72:
                candidates.append((count, score, hero_id))
        if not candidates:
            return HeroMatch(None, '未匹配到勾选角色')
        candidates.sort(reverse=True)
        best = candidates[0]
        # Similar source skins can share features. Reject a close competing
        # base character instead of silently choosing a name.
        competing = next((c for c in candidates[1:] if c[2][:5] != best[2][:5]), None)
        if competing and competing[0] >= best[0] - 2 and abs(competing[1] - best[1]) < .05:
            return HeroMatch(None, '识别有歧义', best[0], best[1])
        return HeroMatch(best[2], self.names.get(best[2], self.names.get(best[2][:5], best[2])), best[0], best[1])

    def recognize(self, image, selected_ids, regions=None):
        # Only selected characters (and their skins) participate in matching.
        # Keep only selected matches and their positions for result statistics.
        templates = self.templates_for(selected_ids)
        if not templates:
            return []
        matches = []
        for slot, (x, y, x2, y2) in enumerate(regions or reference_regions(image.shape)):
            match = self.recognize_region(image[y:y2, x:x2], templates)
            if match.id:
                match.slot = slot
                matches.append(match)
        return matches


@dataclass
class Scene:
    kind: str
    multiplier: int | None = None
    price: int | None = None
    coin: tuple | None = None
    maximum: tuple | None = None
    abandon: tuple | None = None
    texts: tuple = ()
    owned_shards: tuple = (None, None, None)
    hero_regions: tuple = ()
    decrease: tuple | None = None
    luck: tuple | None = None


class SceneReader:
    def __init__(self):
        from gpu_ocr import create_ocr
        self.ocr, self.backend = create_ocr()
        self.backend_note = self.ocr.backend_note
        tick = read_image(ASSETS / 'recruited_tick.png')[:, :, :3]
        mask = self.green_mask(tick)
        bounds = cv2.boundingRect(cv2.findNonZero(mask))
        x, y, width, height = bounds
        mask = mask[y:y+height, x:x+width]
        self.tick_templates = [cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                               for scale in (.35, .5, .65, .8, .925, 1., 1.15, 1.35, 1.6)]
        self._layout_shape = None

    @staticmethod
    def green_mask(image):
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        return cv2.inRange(hsv, (35, 120, 140), (85, 255, 255))

    def recruited_slots(self, image, slots, regions=None):
        acquired = set()
        regions = regions or reference_regions(image.shape)
        for slot in slots:
            x, y, x2, y2 = regions[slot]
            cropped = image[round(y + (y2-y) * .3):y2, x:x2]
            if cropped.size == 0:
                continue
            mask = self.green_mask(cropped)
            if not np.any(mask):
                continue
            for template in self.tick_templates:
                if not max(6, (x2-x) * .12) <= template.shape[1] <= (x2-x) * .65:
                    continue
                if template.shape[0] > mask.shape[0] or template.shape[1] > mask.shape[1]:
                    continue
                score = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
                if cv2.minMaxLoc(score)[1] >= .82:
                    acquired.add(slot)
                    break
        return acquired

    def _read_items(self, image, bounds, scale=1.):
        left, top, right, bottom = bounds
        cropped = image[top:bottom, left:right] if image is not None else None
        if cropped is not None and scale != 1.:
            cropped = cv2.resize(cropped, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        try:
            rows, _ = self.ocr(cropped)
        except Exception as error:
            if self.backend.startswith('CPU'):
                raise
            from gpu_ocr import create_ocr
            self.ocr, self.backend = create_ocr(force_cpu=True)
            self.backend = 'CPU · GPU 识别失败后自动切换'
            self.backend_note = str(error)
            rows, _ = self.ocr(cropped)
        items = []
        for box, text, confidence in rows or []:
            if confidence >= .8:
                items.append((re.sub(r'\s', '', text),
                              (left + round(float(np.mean([p[0] for p in box])) / scale),
                               top + round(float(np.mean([p[1] for p in box])) / scale))))
        return items

    @staticmethod
    def read_luck(items, shape, controls_y=None):
        height, width = shape[:2]
        labels = [point for text, point in items if ('幸运值' in text or '幸運值' in text) and point[1] > height * .45]
        candidates = []
        for text, point in items:
            normalized = text.replace('／', '/').replace('\\', '/')
            match = re.search(r'(?<!\d)(\d{1,3})/(\d{1,3})(?!\d)', normalized)
            if not match or not width * .15 < point[0] < width * .85:
                continue
            current, maximum = map(int, match.groups())
            if maximum != 25 or not 0 <= current <= maximum:
                continue
            if controls_y is not None and point[1] <= controls_y + min(width * .12, height * .06):
                continue
            labelled = '幸运值' in text or '幸運值' in text or any(
                abs(point[1] - label[1]) < max(12, min(width, height) * .045) for label in labels)
            if labelled and point[1] > height * .45:
                candidates.append((0, -point[1], (current, maximum)))
            elif point[1] > height * .72:
                # Without the label, accept only the game's /25 footer,
                # never a character fragment fraction above the controls.
                candidates.append((1, -point[1], (current, maximum)))
        return min(candidates)[2] if candidates else None

    def read(self, image, require_luck=False):
        # Preserve the capture's aspect ratio. All OCR coordinates and card
        # regions belong to this frame, rather than a fixed 2400-pixel height.
        shape = image.shape[:2] if image is not None else (2400, 1080)
        height, width = shape
        if getattr(self, '_layout_shape', None) != shape:
            self._layout_shape = shape
            self._hero_regions = reference_regions(shape)
            self._title_y = self._controls_y = None
        bounds = (round(width * .015), round(height * .08),
                  max(1, round(width * .985)), max(1, round(height * .96)))
        if image is None:
            bounds = (0, 0, width, height)
        items = self._read_items(image, bounds)
        title_names = ('常驻招募', '常駐招募')
        if image is not None and self._title_y is None and not any(text in title_names for text, _ in items):
            # A first frame with unusual padding may place the heading beyond
            # the usual band. Only the initial layout needs this wider pass.
            items = self._read_items(image, (0, 0, width, height))
        texts = tuple(text for text, _ in items)
        base_details = {'texts': texts, 'hero_regions': self._hero_regions,
                        'luck': self.read_luck(items, shape)}
        if any('金币不足' in text or '金幣不足' in text for text in texts):
            return Scene('insufficient', **base_details)
        if any('获得英雄碎片' in text or '獲得英雄碎片' in text for text in texts):
            return Scene('reward', **base_details)
        title = next((point for text, point in items if text in title_names and
                      width * .15 < point[0] < width * .85 and 0 < point[1] < height * .75), None)
        if title is None:
            return Scene('unknown', **base_details)

        # A real abandon label remains below the card area. Its location is
        # read from OCR, never confused with the diamond purchase above it.
        abandon = next((point for text, point in items if text in ('放弃', '放棄') and
                        width * .25 < point[0] < width * .75 and
                        point[1] > max(height * .45, title[1] + min(width, height) * .6)), None)
        pairs = []
        for text, point in items:
            if not re.fullmatch(r'[123]倍[奖獎][励勵]', text):
                continue
            if not (width * .18 < point[0] < width * .78 and
                    point[1] > title[1] + min(width, height) * .2):
                continue
            for label, maximum in items:
                if (label == '最大' and maximum[0] > point[0] + width * .04 and
                        abs(maximum[1] - point[1]) < min(width * .1, height * .075)):
                    pairs.append((abs(maximum[1] - point[1]), int(text[0]), point, maximum))
        pair = min(pairs, key=lambda item: item[0]) if pairs else None
        controls_y = (pair[2][1] + pair[3][1]) / 2 if pair else None
        if controls_y is None and abandon is not None:
            controls_y = abandon[1] - min(width * .24, height * .13)

        shards = [None, None, None]
        shard_points = {}
        if controls_y is not None and controls_y > title[1]:
            span = controls_y - title[1]
            bars = shard_bar_centers(image, title[1], controls_y)
            fragments = []
            for text, point in items:
                match = re.fullmatch(r'(\d+)/(\d+)', text)
                if match and title[1] + span * .15 < point[1] < title[1] + span * .85:
                    fragments.append((point, (int(match[1]), int(match[2]))))
            if not bars and len(fragments) == 3:
                ordered = sorted(fragments, key=lambda value: value[0][0])
                if ordered[1][0][0] - ordered[0][0][0] > 5 and ordered[2][0][0] - ordered[1][0][0] > 5:
                    bars = tuple(point for point, _ in ordered)
            shard_points.update(enumerate(bars))
            distances = {}
            for point, counts in fragments:
                if bars:
                    slot = min(range(3), key=lambda index: abs(point[0] - bars[index][0]))
                    distance = abs(point[0] - bars[slot][0]) + abs(point[1] - bars[slot][1])
                else:
                    slot = max(0, min(2, int(point[0] * 3 / width)))
                    expected_y = title[1] + span * .65 + (min(width / 12, span * .09) if slot == 1 else 0)
                    distance = abs(point[1] - expected_y)
                if slot not in distances or distance < distances[slot]:
                    distances[slot] = distance
                    shards[slot] = counts
                    shard_points[slot] = point
            self._hero_regions = recruitment_regions(shape, title[1], controls_y, shard_points)
            self._title_y, self._controls_y = title[1], controls_y
        luck = self.read_luck(items, shape, controls_y)
        if require_luck and image is not None and controls_y is not None:
            # A separate enlarged footer pass is authoritative for the pity
            # mode, where confusing 2/25 with 25/25 would stop too early.
            top = min(height - 1, max(0, round(controls_y + min(width * .08, height * .04))))
            footer_items = self._read_items(image, (0, top, width, height), scale=1.5)
            luck = self.read_luck(footer_items, shape, controls_y)
        details = {'texts': texts, 'owned_shards': tuple(shards), 'hero_regions': self._hero_regions, 'luck': luck}
        if abandon is not None:
            return Scene('result', abandon=abandon, **details)
        if pair is None:
            return Scene('unknown', **details)
        _, bet, multiplier, maximum = pair
        prices = [(int(text), point) for text, point in items if text in ('100', '200', '300') and
                  width * .25 < point[0] < width * .75 and
                  controls_y + max(8, min(width, height) * .015) < point[1] <
                  controls_y + min(width * .38, height * .25)]
        recruit_tip = any('招募其中一名英雄' in text and point[1] > controls_y for text, point in items)
        if len(prices) == 1 and prices[0][0] == bet * 100 and recruit_tip:
            decrease = find_decrease(image, multiplier, maximum, items)
            return Scene('select', bet, prices[0][0], prices[0][1], maximum, decrease=decrease, **details)
        return Scene('unknown', **details)


class Stopped(Exception):
    pass


class AdbDevice:
    def __init__(self, adb=DEFAULT_ADB, serial='emulator-5554', stop=None, on_geometry=None, click_jitter=True):
        self.adb, self.serial = resolve_adb(adb) or str(adb), serial
        self.stop = stop or threading.Event()
        self.actual_size = None
        self.geometry = None
        self.on_geometry = on_geometry
        self.click_jitter = bool(click_jitter)
        self._click_random = random.SystemRandom()
        self._click_offsets = {}

    def command(self, *args, timeout=20):
        if self.stop.is_set():
            raise Stopped()
        result = subprocess.run([self.adb, '-s', self.serial, *args], capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace').strip() or 'ADB 命令失败')
        return result.stdout

    def screenshot(self):
        data = self.command('exec-out', 'screencap', '-p')
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError('设备截图失败')
        geometry = CaptureGeometry.from_image(image)
        changed = geometry != self.geometry
        self.geometry = geometry
        self.actual_size = geometry.original_size
        if changed and self.on_geometry is not None:
            self.on_geometry(geometry)
        return geometry.normalize(image)

    def tap(self, point):
        if self.geometry is None:
            raise RuntimeError('请先获取设备截图')
        x, y = self.geometry.to_device_point(point)
        if self.click_jitter:
            left, top, width, height = self.geometry.viewport
            # Small disk around the recognized point: roughly 3 pixels on
            # a 1080-wide portrait screen, capped at 6 actual device pixels.
            radius = min(6, max(1, round(min(width, height) / 360)))
            if radius not in self._click_offsets:
                self._click_offsets[radius] = tuple((dx, dy)
                    for dx in range(-radius, radius + 1)
                    for dy in range(-radius, radius + 1) if dx * dx + dy * dy <= radius * radius)
            dx, dy = self._click_random.choice(self._click_offsets[radius])
            x = min(left + width - 1, max(left, x + dx))
            y = min(top + height - 1, max(top, y + dy))
        self.command('shell', 'input', 'tap', str(x), str(y))

    def wait(self, seconds):
        if self.stop.wait(seconds):
            raise Stopped()


class RecruitRunner:
    def __init__(self, device, recognizer, reader, selected, log=print, update=lambda *a: None,
                 on_recruited=lambda *a: None, on_draw=lambda *a: None):
        self.device, self.recognizer, self.reader = device, recognizer, reader
        self.selected, self.log, self.update = selected, log, update
        self.rounds, self.coins = 0, 0
        self.detected_selection = frozenset()
        self.on_recruited = on_recruited
        self.on_draw = on_draw
        self.recruited = 0
        self.until_pity = False
        self.pity_completed = False
        self.end_reason = ''

    def read_scene(self, image):
        return self.reader.read(image, require_luck=True) if self.until_pity else self.reader.read(image)

    def recognize_targets(self, image, scene, selected_ids):
        if scene.hero_regions:
            return self.recognizer.recognize(image, selected_ids, regions=scene.hero_regions)
        return self.recognizer.recognize(image, selected_ids)

    def inspect(self):
        image = self.device.screenshot()
        scene = self.read_scene(image)
        self.detected_selection = frozenset(self.selected())
        heroes = self.recognize_targets(image, scene, self.detected_selection) if scene.kind == 'select' and self.detected_selection else []
        multiplier = choose_multiplier([h.id for h in heroes], self.detected_selection)
        self.update(image, scene, heroes, multiplier, self.detected_selection)
        return image, scene, heroes, multiplier

    def require_select(self, scene):
        if scene.kind != 'select':
            raise RuntimeError('未确认常驻招募选择界面，已停止。请手动打开大厅并关闭弹窗')

    def set_multiplier(self, scene, target):
        for _ in range(4):
            self.require_select(scene)
            if scene.multiplier == target and scene.price == target * 100:
                return scene
            point = scene.maximum if target == 3 else scene.decrease
            if point is None:
                raise RuntimeError('未确认倍率按钮，已停止；请保持招募界面完整可见')
            self.device.tap(point)
            self.device.wait(.55)
            scene = self.read_scene(self.device.screenshot())
        raise RuntimeError('倍率未切换成功，已停止')

    def confirm_luck(self, previous, inspection):
        # Both readings come from fresh screenshots; never spend on a cached
        # or single OCR result. Changes of targets are rechecked by run().
        for attempt in range(6):
            image, scene, heroes, target = inspection
            self.require_select(scene)
            if scene.luck is not None and scene.luck == previous:
                return inspection
            previous = scene.luck
            if attempt < 5:
                self.device.wait(SCAN_INTERVAL)
                inspection = self.inspect()
        self.save_diagnostic(image, scene, heroes)
        raise RuntimeError('幸运值未能连续两次确认，已停止；未提交新的抽取，请保持底部幸运值清晰可见')

    def abandon_result(self, scene, target):
        if scene.kind != 'result' or scene.abandon is None:
            raise RuntimeError('未确认放弃按钮，已停止')
        self.log('出现待放弃界面，点击「放弃」后继续招募')
        self.device.tap(scene.abandon)
        deadline = time.monotonic() + 10
        stable = 0
        while time.monotonic() < deadline:
            self.device.wait(SCAN_INTERVAL)
            frame = self.device.screenshot()
            state = self.read_scene(frame)
            self.update(frame, state, [], target)
            if state.kind == 'insufficient':
                raise RuntimeError('金币不足，停止')
            stable = stable + 1 if state.kind == 'select' else 0
            if stable >= 2:
                self.log('已返回金币招募界面')
                return
        self.save_diagnostic(frame, state, [])
        raise RuntimeError('点击放弃后未确认返回招募界面，已停止；不会重复点击或钻石补抽')

    @staticmethod
    def round_marker(scene):
        # Fragment quantities / luck progress indicate a completed draw.
        # The refresh countdown is deliberately excluded.
        markers = [text.lower().replace('×', 'x') for text in scene.texts
                   if re.fullmatch(r'[xX×]\d+', text)]
        if scene.luck is not None:
            markers.append(f'{scene.luck[0]}/{scene.luck[1]}')
        else:
            markers.extend(text for text in scene.texts if re.fullmatch(r'\d+/25', text))
        return tuple(sorted(markers))

    def finish_round(self, before_scene, target, before_image=None, targets=(), selected_ids=()):
        deadline = time.monotonic() + 12
        saw_transition = False
        before_marker = self.round_marker(before_scene)
        stable_scene = None
        targets = [hero for hero in targets if hero.slot is not None]
        slots = {hero.slot for hero in targets}

        def ticks(frame, state):
            regions = state.hero_regions or before_scene.hero_regions
            if regions:
                return self.reader.recruited_slots(frame, slots, regions=regions)
            return self.reader.recruited_slots(frame, slots)

        old_ticks = ticks(before_image, before_scene) if slots and before_image is not None else set()
        counted = False

        def record_result(frame, state):
            nonlocal counted
            if counted or not targets:
                return
            acquired = ticks(frame, state) - old_ticks
            # Fragment progress provides another confirmation when a brief
            # checkmark was missed. On a new selection screen, also confirm
            # that the selected character still occupies that position.
            increased = set()
            for hero in targets:
                before = before_scene.owned_shards[hero.slot]
                after = state.owned_shards[hero.slot]
                if before and after and before[1] == after[1] and after[0] > before[0]:
                    increased.add(hero.slot)
            if increased and state.kind == 'select':
                same_targets = {(hero.slot, hero.id) for hero in self.recognize_targets(frame, state, selected_ids)}
                increased = {hero.slot for hero in targets if hero.slot in increased and (hero.slot, hero.id) in same_targets}
            elif state.kind not in ('result', 'reward'):
                increased.clear()
            acquired.update(increased)
            winners = [hero for hero in targets if hero.slot in acquired]
            # A recruit chooses one hero, even when the multiplier is three.
            if len(winners) != 1:
                return
            hero = winners[0]
            hero_id = hero.id if hero.id in selected_ids else hero.id[:5]
            counted = True
            self.recruited += 1
            self.on_recruited(hero_id, hero.name)
            self.log(f'抽中勾选角色：{hero.name}，本次累计 {self.recruited} 次')

        while time.monotonic() < deadline:
            self.device.wait(SCAN_INTERVAL)
            frame = self.device.screenshot()
            state = self.read_scene(frame)
            self.update(frame, state, [], target)
            if state.kind == 'insufficient':
                raise RuntimeError('金币不足，停止')
            record_result(frame, state)
            if state.kind == 'result':
                self.abandon_result(state, target)
                return
            if state.kind != 'select':
                saw_transition = True
                stable_scene = None
                continue
            # An immediate unchanged frame may precede the draw animation.
            # Confirm progress without identifying any of the three characters.
            marker = self.round_marker(state)
            progressed = bool(before_marker and marker and marker != before_marker)
            if self.until_pity:
                # Missing OCR text is not progress. Without an animation,
                # require a changed, readable luck counter in this mode.
                progressed = bool(before_scene.luck is not None and state.luck is not None and
                                  state.luck != before_scene.luck)
            if saw_transition or progressed:
                signature = (state.multiplier, state.price, marker)
                if signature == stable_scene:
                    return
                stable_scene = signature
            else:
                stable_scene = None
        self.save_diagnostic(frame, state, [])
        raise RuntimeError('招募后未确认结果或下一轮界面，已停止；未补点任何按钮')

    def run(self, max_rounds, coin_limit, until_pity=False):
        self.until_pity = until_pity
        self.pity_completed = False
        self.end_reason = ''
        try:
            while not max_rounds or self.rounds < max_rounds:
                image, scene, heroes, target = self.inspect()
                if scene.kind == 'result':
                    self.abandon_result(scene, target)
                    continue
                self.require_select(scene)
                previous_luck = scene.luck
                scene = self.set_multiplier(scene, target)
                # Recheck actors after animations/bet changes and read current
                # checkbox values immediately before a spending click.
                inspection = self.inspect()
                if until_pity:
                    inspection = self.confirm_luck(previous_luck, inspection)
                image, scene, heroes, latest_target = inspection
                self.require_select(scene)
                if latest_target != target or frozenset(self.selected()) != self.detected_selection:
                    self.log('目标匹配结果或勾选项发生变化，重新检测')
                    continue
                if scene.multiplier != target or scene.price != target * 100:
                    raise RuntimeError('实际倍率/金额与策略不符，已停止')
                if coin_limit and self.coins + scene.price > coin_limit:
                    self.end_reason = '达到本次金币上限'
                    self.log(self.end_reason + '，停止')
                    break
                pity_draw = until_pity and scene.luck[0] >= scene.luck[1] - 1
                if until_pity:
                    current, maximum = scene.luck
                    self.log(f'幸运值 {current}/{maximum}' + ('：本次为保底抽取，完成后停止' if pity_draw else '，继续抽取'))
                match_text = '匹配到勾选角色：' + ' / '.join(h.name for h in heroes) if heroes else '未匹配到勾选角色'
                self.log(f'第 {self.rounds + 1} 轮：{match_text} → {target} 倍，{scene.price} 金币')
                price = scene.price
                self.device.tap(scene.coin)
                # Count a submitted click conservatively, even if confirmation
                # times out, so the local cap cannot silently undercount retries.
                self.coins += price
                self.rounds += 1
                self.on_draw(self.rounds, self.coins)
                self.finish_round(scene, target, before_image=image, targets=heroes,
                                  selected_ids=self.detected_selection)
                if pity_draw:
                    self.pity_completed = True
                    self.end_reason = f'已完成保底出红抽取（抽取前幸运值 {current}/{maximum}）'
                    self.log(self.end_reason + '，停止')
                    break
            if not self.end_reason and max_rounds and self.rounds >= max_rounds:
                self.end_reason = '达到轮数上限'
                self.log(self.end_reason + '，停止')
        except Stopped:
            self.log('已停止')
        finally:
            self.log(f'本次已提交 {self.rounds} 轮，金额累计 {self.coins} 金币，确认抽中勾选角色 {self.recruited} 次')

    def save_diagnostic(self, image, scene, heroes):
        folder = BASE / 'run_logs'
        folder.mkdir(exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        cv2.imencode('.png', image)[1].tofile(str(folder / f'{stamp}.png'))
        geometry = getattr(self.device, 'geometry', None)
        details = {'scene': asdict(scene), 'heroes': [asdict(h) for h in heroes],
                   'capture': asdict(geometry) if geometry is not None else None}
        (folder / f'{stamp}.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')

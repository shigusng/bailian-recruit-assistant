"""Validate selection and spending behavior without connecting to a device."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gacha_core import (ASSETS, HeroMatch, HeroRecognizer, RecruitRunner, Scene, SceneReader, Stopped,
                        choose_multiplier, load_roles, read_image, read_settings, save_settings, cv2, np)


class FakeDevice:
    def __init__(self, multiplier=1):
        self.multiplier = multiplier
        self.result = False
        self.taps = []

    def screenshot(self):
        return None

    def wait(self, seconds):
        pass

    def tap(self, point):
        self.taps.append((point, self.multiplier))
        if point == (900, 1720):
            self.multiplier = 3
        elif point == (343, 1727):
            self.multiplier = max(1, self.multiplier - 1)
        elif point == (500, 1850):
            self.result = True
        elif point == (500, 2000):
            self.result = False


class FakeReader:
    def __init__(self, device):
        self.device = device

    def read(self, image):
        if self.device.result:
            return Scene('result', abandon=(500, 2000))
        return Scene('select', self.device.multiplier, self.device.multiplier * 100,
                     (500, 1850), (900, 1720))


class FakeRecognizer:
    def __init__(self, visible=('83003', '83008', '81003')):
        self.visible = visible
        self.selections = []

    def recognize(self, image, selected_ids):
        self.selections.append(frozenset(selected_ids))
        return [HeroMatch(hero, hero) for hero in self.visible if hero and choose_multiplier([hero], selected_ids) == 3]


class SelectionTests(unittest.TestCase):
    def test_only_checked_roles_enable_three(self):
        self.assertEqual(choose_multiplier(['83003', '84013', '81003'], ['84013']), 3)
        self.assertEqual(choose_multiplier(['83003', '84013', '81003'], ['85001']), 1)
        self.assertEqual(choose_multiplier(['83003', '84013', '81003'], []), 1)
        self.assertEqual(choose_multiplier([None, None, None], ['84013']), 1)

    def test_base_character_covers_skins_but_skin_does_not_cover_base(self):
        self.assertEqual(choose_multiplier(['84013501'], ['84013']), 3)
        self.assertEqual(choose_multiplier(['84013'], ['84013501']), 1)
        self.assertEqual(choose_multiplier(['84013601'], ['84013501']), 1)

    def test_csv_encodings_and_duplicates(self):
        text = '角色ID,名字\r\n83003.png,舞姬·王昭君\r\n\r\n83003.png,重复\r\n84013.png,圣骑士·乌瑟尔\r\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '名单.csv'
            for encoding in ('gb18030', 'utf-8-sig', 'utf-16'):
                with self.subTest(encoding=encoding):
                    path.write_bytes(text.encode(encoding))
                    roles = load_roles(path)
                    self.assertEqual([r['id'] for r in roles], ['83003', '84013'])
                    self.assertEqual(roles[0]['name'], '舞姬·王昭君')

    def test_empty_selection_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            settings = read_settings(path)
            settings['selected_ids'] = []
            save_settings(settings, path)
            self.assertEqual(read_settings(path)['selected_ids'], [])
            self.assertEqual(len(read_settings(path)['roles']), 6)

class RunnerTests(unittest.TestCase):
    def runner(self, device, selected, recognizer=None):
        return RecruitRunner(device, recognizer or FakeRecognizer(), FakeReader(device), selected, log=lambda s: None)

    def test_checked_character_uses_three_and_abandons_missed_result(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: ['83003'])
        runner.run(1, 3000)
        self.assertEqual(device.taps, [((900, 1720), 1), ((500, 1850), 3), ((500, 2000), 3)])
        self.assertEqual((runner.rounds, runner.coins), (1, 300))

    def test_unchecked_character_resets_previous_three(self):
        device = FakeDevice(multiplier=3)
        runner = self.runner(device, lambda: [])
        runner.run(1, 100)
        self.assertEqual(device.taps, [((343, 1727), 3), ((343, 1727), 2), ((500, 1850), 1), ((500, 2000), 1)])
        self.assertEqual(runner.coins, 100)

    def test_selection_change_is_rechecked_before_spending(self):
        device = FakeDevice()
        choices = iter([['83003'], [], [], []])
        runner = self.runner(device, lambda: next(choices, []))
        runner.run(1, 300)
        spending = [multiplier for point, multiplier in device.taps if point == (500, 1850)]
        self.assertEqual(spending, [1])

    def test_coin_cap_prevents_spending(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: ['83003'])
        runner.run(1, 200)
        self.assertFalse(device.result)
        self.assertEqual(runner.coins, 0)

    def test_unidentified_other_characters_do_not_block_target_match(self):
        device = FakeDevice()
        recognizer = FakeRecognizer(('83003', None, None))
        runner = self.runner(device, lambda: ['83003'], recognizer)
        runner.run(1, 300)
        self.assertEqual([bet for pt, bet in device.taps if pt == (500, 1850)], [3])
        self.assertTrue(all(selection == frozenset(['83003']) for selection in recognizer.selections))

    def test_no_target_match_uses_one_without_identifying_others(self):
        device = FakeDevice(multiplier=3)
        runner = self.runner(device, lambda: ['83003'], FakeRecognizer((None, None, None)))
        runner.run(1, 100)
        self.assertEqual([bet for pt, bet in device.taps if pt == (500, 1850)], [1])

    def test_empty_selection_skips_character_recognizer(self):
        device = FakeDevice()
        recognizer = Mock()
        recognizer.recognize.side_effect = AssertionError('No character matching should run')
        runner = self.runner(device, lambda: [], recognizer)
        runner.run(1, 100)
        recognizer.recognize.assert_not_called()
        self.assertEqual(runner.coins, 100)

    def test_selection_changed_during_matching_is_rechecked(self):
        device = FakeDevice()
        selected = {'83003'}
        recognizer = Mock()
        calls = []

        def match(image, snapshot):
            calls.append(snapshot)
            if len(calls) == 2:
                selected.clear()
            return [HeroMatch('83003', 'selected target')]

        recognizer.recognize.side_effect = match
        runner = self.runner(device, lambda: selected, recognizer)
        runner.run(1, 300)
        self.assertEqual([bet for pt, bet in device.taps if pt == (500, 1850)], [1])

    def test_unknown_scene_still_prevents_draw(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        runner.reader.read = Mock(return_value=Scene('unknown'))
        with self.assertRaisesRegex(RuntimeError, '未确认常驻招募'):
            runner.run(1, 100)
        self.assertEqual(device.taps, [])

    def test_continues_after_abandon_and_rechecks_next_round_multiplier(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: ['83003'] if runner.rounds == 0 else [])
        runner.run(2, 400)
        self.assertEqual([bet for pt, bet in device.taps if pt == (500, 1850)], [3, 1])
        self.assertEqual(sum(pt == (500, 2000) for pt, _ in device.taps), 2)
        self.assertEqual((runner.rounds, runner.coins), (2, 400))

    def test_recovers_when_started_on_missed_result(self):
        device = FakeDevice()
        device.result = True
        runner = self.runner(device, lambda: [])
        runner.run(1, 100)
        self.assertEqual(device.taps, [((500, 2000), 1), ((500, 1850), 1), ((500, 2000), 1)])
        self.assertEqual((runner.rounds, runner.coins), (1, 100))

    def test_coin_cap_still_applies_after_abandon(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: ['83003'])
        runner.run(0, 300)
        self.assertEqual((runner.rounds, runner.coins), (1, 300))
        self.assertFalse(device.result)

    def test_abandon_waits_for_stable_return_without_reclicking(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        result = Scene('result', abandon=(500, 2000))
        select = Scene('select', 1, 100)
        runner.reader.read = Mock(side_effect=[result, Scene('unknown'), select, select])
        runner.abandon_result(result, 1)
        self.assertEqual(device.taps, [((500, 2000), 1)])
        self.assertEqual(runner.reader.read.call_count, 4)

    def test_failed_abandon_stops_without_diamond_or_extra_draw(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        result = Scene('result', abandon=(500, 2000))
        runner.reader.read = Mock(return_value=result)
        runner.save_diagnostic = Mock()
        with patch('gacha_core.time.monotonic', side_effect=[0, 0, 11]):
            with self.assertRaisesRegex(RuntimeError, '点击放弃后'):
                runner.abandon_result(result, 1)
        self.assertEqual(device.taps, [((500, 2000), 1)])
        runner.save_diagnostic.assert_called_once()

    def test_normal_return_waits_past_old_frame_and_animation(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        select = Scene('select', 1, 100)
        runner.reader.read = Mock(side_effect=[select, Scene('unknown'), select, select])
        runner.recognizer.recognize = Mock(side_effect=AssertionError('No identities needed while waiting'))
        runner.finish_round(select, 1)
        self.assertEqual(runner.reader.read.call_count, 4)
        runner.recognizer.recognize.assert_not_called()
        self.assertEqual(device.taps, [])

    def test_round_progress_confirms_return_without_identifying_characters(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        before = Scene('select', 1, 100, texts=('x17', '11/25', '29分钟47秒'))
        after = Scene('select', 1, 100, texts=('X18', '12/25', '29分钟45秒'))
        runner.reader.read = Mock(side_effect=[before, after, after])
        runner.recognizer.recognize = Mock(side_effect=AssertionError('No identities needed while waiting'))
        runner.finish_round(before, 1)
        runner.recognizer.recognize.assert_not_called()
        self.assertEqual(runner.reader.read.call_count, 3)

    def test_unchanged_screen_does_not_trigger_another_draw(self):
        device = FakeDevice()
        runner = self.runner(device, lambda: [])
        runner.save_diagnostic = Mock()
        before = Scene('select', 1, 100, texts=('x17', '11/25', '29分钟47秒'))
        runner.reader.read = Mock(return_value=Scene('select', 1, 100, texts=('x17', '11/25', '29分钟45秒')))
        with patch('gacha_core.time.monotonic', side_effect=[0, 0, 1, 13]):
            with self.assertRaisesRegex(RuntimeError, '未确认结果或下一轮'):
                runner.finish_round(before, 1)
        self.assertEqual(device.taps, [])

    def test_stop_interrupts_wait_after_abandon(self):
        device = FakeDevice()
        device.wait = Mock(side_effect=Stopped())
        runner = self.runner(device, lambda: [])
        with self.assertRaises(Stopped):
            runner.abandon_result(Scene('result', abandon=(500, 2000)), 1)
        self.assertEqual(device.taps, [((500, 2000), 1)])


class SceneReaderTests(unittest.TestCase):
    def reader(self, abandon_y=1990):
        def row(text, x, y):
            return ([[x-25, y-15], [x+25, y-15], [x+25, y+15], [x-25, y+15]], text, .99)
        reader = object.__new__(SceneReader)
        reader.ocr = lambda image: ([row('常驻招募', 540, 550), row('73', 540, 1850),
                                     row('放弃', 527, abandon_y)], None)
        return reader

    def test_missed_gold_uses_abandon_below_diamond_button(self):
        scene = self.reader().read(None)
        self.assertEqual(scene.kind, 'result')
        self.assertEqual(scene.abandon, (527, 1990))
        self.assertIsNone(scene.coin)

    def test_abandon_text_outside_button_is_not_actionable(self):
        self.assertEqual(self.reader(abandon_y=900).read(None).kind, 'unknown')


class RecognizerTests(unittest.TestCase):
    def test_only_selected_templates_are_loaded_and_skin_scope_is_preserved(self):
        recognizer = HeroRecognizer()
        self.assertEqual(recognizer.template_cache, {})
        templates = recognizer.templates_for({'84013'})
        self.assertTrue(templates)
        self.assertTrue(all(template[0][:5] == '84013' for template in templates))
        self.assertTrue(all(key[:5] == '84013' for key in recognizer.template_cache))
        skin = recognizer.templates_for({'84013501'})
        self.assertEqual([template[0] for template in skin], ['84013501'])
        other = recognizer.templates_for({'85001'})
        self.assertTrue(other)
        self.assertTrue(all(template[0][:5] == '85001' for template in other))

    def test_empty_selection_skips_image_matching(self):
        recognizer = HeroRecognizer()
        recognizer.recognize_region = Mock(side_effect=AssertionError('Should not inspect character regions'))
        self.assertEqual(recognizer.recognize(None, []), [])
        self.assertEqual(recognizer.template_cache, {})

    def test_selected_target_can_match_while_other_positions_have_no_identity(self):
        recognizer = HeroRecognizer()
        for hero in ('83003', '84009', '84013', '84016', '85001', '84011'):
            with self.subTest(hero=hero):
                sprite = cv2.resize(read_image(ASSETS / 'characters' / (hero + '.png')), None, fx=1.8, fy=1.8)
                screen = np.full((2400, 1080, 3), (80, 45, 100), np.uint8)
                height, width = sprite.shape[:2]
                alpha = sprite[:, :, 3:4] / 255.0
                target = screen[1010:1010+height, 80:80+width]
                target[:] = sprite[:, :, :3] * alpha + target * (1-alpha)
                self.assertEqual([match.id for match in recognizer.recognize(screen, [hero])], [hero])
                unselected = '85001' if hero != '85001' else '84013'
                self.assertEqual(recognizer.recognize(screen, [unselected]), [])


if __name__ == '__main__':
    unittest.main()

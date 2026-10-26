"""常驻招募面板：仅在出现已勾选角色时使用三倍。"""
from __future__ import annotations

import queue
from pathlib import Path
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from gacha_core import (ASSETS, BASE, SCAN_INTERVAL, AdbDevice, HeroRecognizer, RecruitRunner,
                        SceneReader, Stopped, choose_multiplier, load_roles,
                        read_settings, save_settings)
import cv2
from PIL import Image, ImageTk
from recruit_history import RecruitHistory, display_time, now
from panel_theme import (BackdropCanvas, BackdropFrame, SURFACE, TEXT,
                         WindowBackdrop, configure_theme)
from environment_check import check_environment, resolve_adb
from error_journal import ErrorJournal
from adb_connection import (ConnectionCancelled, DeviceChoiceRequired, connect_emulator,
                            discover_emulators)


class RecruitApp:
    def __init__(self, root, settings_path=BASE / 'settings.json'):
        self.root, self.settings_path = root, settings_path
        self.events = queue.Queue()
        self.error_journal = ErrorJournal(Path(settings_path).parent / 'error_logs')
        self.error_window = self.error_tree = None
        self.last_callback_trace = None
        root.report_callback_exception = self.handle_callback_error
        self.settings = read_settings(settings_path)
        self.run_history = RecruitHistory(Path(settings_path).with_name('draw_history.json'))
        self.active_run_id = None
        self.history_window = self.history_tree = self.history_detail_tree = None
        self.settings_window = self.settings_save_button = None
        self.settings_scan_button = self.settings_connect_button = self.settings_serial_entry = None
        self.settings_adb_entry = self.settings_browse_button = None
        self.connection_worker = None
        self.connection_stop = threading.Event()
        self.connection_candidates = []
        self.environment_window = self.environment_tree = None
        self.environment_retry_button = self.environment_cancel_button = None
        self.environment_worker = None
        self.environment_stop = threading.Event()
        self.environment_rows = {}
        self.closing = False
        self.roles = self.settings['roles']
        self.recruited_counts = dict(self.settings.get('recruited_counts', {}))
        self.role_stat_vars = {}
        self.total_recruited_var = tk.StringVar()
        self.selected_ids = frozenset(self.settings['selected_ids'])
        self.selection_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.frames = queue.Queue(maxsize=1)
        self.worker = None
        self.recognizer = self.reader = None
        self.last_frame = None
        self.role_vars, self.role_photos = {}, []

        root.title('百炼英雄 · 抽取角色')
        root.geometry('1100x780')
        root.minsize(990, 720)
        configure_theme(root)
        with Image.open(ASSETS / 'panel_background.jpg') as source:
            self.background_image = source.convert('RGB')
        self.backdrop = WindowBackdrop(root, self.background_image)

        connection = BackdropFrame(root, padding=10, shade=.5)
        connection.pack(fill='x', padx=12, pady=(12, 6))
        self.adb_var = tk.StringVar(value=self.settings.get('adb', ''))
        self.serial_var = tk.StringVar(value=self.settings.get('serial', 'emulator-5554'))
        ttk.Button(connection, text='设置', command=self.show_settings).pack(side='left')
        self.connect_button = ttk.Button(connection, text='一键连接模拟器', command=self.quick_connect)
        self.connect_button.pack(side='left', padx=(8, 0))
        self.environment_button = ttk.Button(connection, text='环境检测', command=self.show_environment)
        self.environment_button.pack(side='left', padx=(8, 12))
        self.environment_status_var = tk.StringVar(value='环境：尚未检测')
        ttk.Label(connection, textvariable=self.environment_status_var, style='Hint.TLabel').pack(side='left')
        ttk.Label(connection, textvariable=self.serial_var).pack(side='right')
        ttk.Label(connection, text='设备：', style='Hint.TLabel').pack(side='right', padx=(12, 3))

        body = BackdropFrame(root, shade=0)
        body.pack(fill='both', expand=True, padx=12, pady=6)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        role_box = BackdropFrame(body, text='三倍抽取角色', padding=10)
        role_box.grid(row=0, column=0, sticky='nsew')
        ttk.Label(role_box, text='勾选的角色出现，才三倍抽取', style='Title.TLabel').pack(anchor='w')
        ttk.Label(role_box, text='未命中 → 一倍；全部取消 → 始终一倍\n修改勾选后自动保存，下一次招募前生效。',
                  style='Hint.TLabel').pack(anchor='w', pady=(6, 10))
        actions = BackdropFrame(role_box, shade=.48)
        actions.pack(fill='x', pady=(0, 6))
        ttk.Button(actions, text='全选', command=lambda: self.select_all(True)).pack(side='left')
        ttk.Button(actions, text='全不选', command=lambda: self.select_all(False)).pack(side='left', padx=5)
        self.import_button = ttk.Button(actions, text='导入 CSV', command=self.import_roles)
        self.import_button.pack(side='left')
        canvas = BackdropCanvas(role_box, highlightthickness=0, width=310)
        scrollbar = ttk.Scrollbar(role_box, orient='vertical', command=canvas.yview)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.role_list = BackdropFrame(canvas, shade=.38)
        window = canvas.create_window((0, 0), window=self.role_list, anchor='nw')
        self.role_list.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox(window)), add='+')
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width), add='+')
        self.count_var = tk.StringVar()
        ttk.Label(role_box, textvariable=self.count_var, style='Hint.TLabel').pack(anchor='w', pady=(8, 0))
        self.render_roles()

        preview_box = BackdropFrame(body, text='最近检测画面', padding=8)
        preview_box.grid(row=0, column=1, sticky='ns', padx=10)
        self.preview = ttk.Label(preview_box, text='点击「检测勾选角色」获取截图', anchor='center')
        self.preview.pack(fill='both', expand=True)

        details = BackdropFrame(body, shade=.5)
        details.grid(row=0, column=2, sticky='nsew')
        self.status_var = tk.StringVar(value='等待检测')
        ttk.Label(details, textvariable=self.status_var, style='Title.TLabel').pack(anchor='w')
        statistics = BackdropFrame(details, text='抽中统计', padding=8)
        statistics.pack(fill='x', pady=(8, 6))
        ttk.Label(statistics, textvariable=self.total_recruited_var).pack(side='left')
        self.clear_stats_button = ttk.Button(statistics, text='清零统计', command=self.clear_statistics)
        self.clear_stats_button.pack(side='right')
        ttk.Button(statistics, text='抽取记录', command=self.show_history).pack(side='right', padx=(0, 5))
        self.backend_var = tk.StringVar(value='OCR：自动选择独显 / CPU')
        ttk.Label(details, textvariable=self.backend_var, wraplength=280, style='Hint.TLabel').pack(anchor='w', pady=5)
        self.geometry_var = tk.StringVar(value='画面：不限分辨率，自动适配')
        ttk.Label(details, textvariable=self.geometry_var, wraplength=280, style='Hint.TLabel').pack(anchor='w', pady=(0, 5))
        self.luck_var = tk.StringVar(value='幸运值：等待检测')
        ttk.Label(details, textvariable=self.luck_var, wraplength=280, style='Hint.TLabel').pack(anchor='w', pady=(0, 5))
        self.match_var = tk.StringVar(value='仅检测已勾选角色，尚未检测。')
        ttk.Label(details, textvariable=self.match_var, wraplength=280).pack(anchor='w', pady=5)
        self.decision_var = tk.StringVar(value='请先手动打开游戏的常驻招募界面。')
        ttk.Label(details, textvariable=self.decision_var, wraplength=280, style='Hint.TLabel').pack(anchor='w', pady=10)
        self.log_box = scrolledtext.ScrolledText(details, width=31, height=14, state='disabled', font=('Microsoft YaHei UI', 9),
                                               background='#192235', foreground=TEXT, insertbackground=TEXT,
                                               selectbackground='#3d5f97', borderwidth=0, highlightthickness=0)
        self.log_box.frame.configure(background=SURFACE)
        self.log_box.vbar.configure(background='#33415c', troughcolor=SURFACE, activebackground='#526180',
                                   borderwidth=0, highlightthickness=0)
        self.log_box.pack(fill='both', expand=True)

        bottom = BackdropFrame(root, padding=(12, 8, 12, 12), shade=.5)
        bottom.pack(fill='x')
        self.rounds_var = tk.StringVar(value=str(self.settings.get('max_rounds', 20)))
        self.coins_var = tk.StringVar(value=str(self.settings.get('coin_limit', 3000)))
        ttk.Label(bottom, text='轮数上限').pack(side='left')
        ttk.Entry(bottom, textvariable=self.rounds_var, width=6).pack(side='left', padx=5)
        ttk.Label(bottom, text='金币上限').pack(side='left')
        ttk.Entry(bottom, textvariable=self.coins_var, width=7).pack(side='left', padx=5)
        ttk.Label(bottom, text='0 = 不限', style='Hint.TLabel').pack(side='left', padx=5)
        self.detect_button = ttk.Button(bottom, text='检测勾选角色', command=lambda: self.start(False))
        self.detect_button.pack(side='left', padx=5)
        self.start_button = ttk.Button(bottom, text='开始抽取', command=lambda: self.start(True))
        self.start_button.pack(side='left', padx=5)
        self.pity_button = ttk.Button(bottom, text='抽取到保底出红', command=lambda: self.start(True, until_pity=True))
        self.pity_button.pack(side='left', padx=5)
        self.stop_button = ttk.Button(bottom, text='停止', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=5)
        ttk.Label(root, text='画面自动适配，请保持招募界面完整可见。检测只截图；出现金色未抽中后的「放弃」时自动放弃，不使用钻石补抽。',
                  style='Hint.TLabel').pack(anchor='w', padx=14, pady=(0, 10))
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = root.after(100, self.poll)
        self.append_log('已载入角色名单。勾选后出现任一目标角色，即使用三倍。')

    def render_roles(self):
        for child in self.role_list.winfo_children():
            if child is not self.role_list.background_label:
                child.destroy()
        self.role_vars.clear()
        self.role_stat_vars.clear()
        self.role_photos.clear()
        for row, role in enumerate(self.roles):
            var = tk.BooleanVar(value=role['id'] in self.selected_ids)
            self.role_vars[role['id']] = var
            with Image.open(ASSETS / 'characters' / f"{role['id']}.png") as source:
                picture = source.convert('RGBA')
                picture.thumbnail((48, 52))
            photo = ImageTk.PhotoImage(picture, master=self.root)
            self.role_photos.append(photo)
            ttk.Label(self.role_list, image=photo).grid(row=row, column=0, padx=(0, 5), pady=5)
            ttk.Checkbutton(self.role_list, text=role['name'], variable=var,
                            command=self.selection_changed).grid(row=row, column=1, sticky='w', pady=5)
            stat_var = tk.StringVar()
            self.role_stat_vars[role['id']] = stat_var
            ttk.Label(self.role_list, textvariable=stat_var, style='Hint.TLabel').grid(row=row, column=2, sticky='e', padx=(8, 0))
        self.update_count()
        self.refresh_statistics()

    def refresh_statistics(self):
        self.total_recruited_var.set(f'累计抽中：{sum(self.recruited_counts.values())} 次')
        for hero_id, var in self.role_stat_vars.items():
            var.set(f"已抽中 {self.recruited_counts.get(hero_id, 0)} 次")

    def clear_statistics(self):
        self.recruited_counts.clear()
        self.refresh_statistics()
        self.persist()
        self.append_log('抽中统计已清零')

    def record_recruited(self, hero_id, name):
        self.recruited_counts[hero_id] = self.recruited_counts.get(hero_id, 0) + 1
        self.refresh_statistics()
        self.persist()

    def show_settings(self):
        if self.settings_window is not None:
            self.settings_window.deiconify()
            self.settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title('百炼英雄 · 设置')
        window.geometry('780x520')
        window.minsize(650, 490)
        window.transient(self.root)
        window.backdrop = WindowBackdrop(window, self.background_image)
        ttk.Label(window, text='连接与点击设置', style='Title.TLabel').pack(anchor='w', padx=18, pady=(16, 12))
        body = BackdropFrame(window, padding=14, shade=.6)
        body.pack(fill='x', padx=16)
        body.columnconfigure(1, weight=1)
        self.settings_adb_var = tk.StringVar(value=self.adb_var.get())
        self.settings_serial_var = tk.StringVar(value=self.serial_var.get())
        self.settings_click_jitter_var = tk.BooleanVar(value=bool(self.settings.get('click_jitter', True)))
        ttk.Label(body, text='ADB 路径').grid(row=0, column=0, sticky='w', padx=(0, 10), pady=7)
        self.settings_adb_entry = ttk.Entry(body, textvariable=self.settings_adb_var)
        self.settings_adb_entry.grid(row=0, column=1, sticky='ew', pady=7)
        self.settings_browse_button = ttk.Button(body, text='浏览…', command=self.browse_adb)
        self.settings_browse_button.grid(row=0, column=2, padx=(8, 0), pady=7)
        ttk.Label(body, text='设备序列号').grid(row=1, column=0, sticky='w', padx=(0, 10), pady=7)
        self.settings_serial_entry = ttk.Combobox(body, textvariable=self.settings_serial_var,
                                                values=[item.description for item in self.connection_candidates])
        self.settings_serial_entry.grid(row=1, column=1, sticky='ew', pady=7)
        self.settings_serial_entry.bind('<<ComboboxSelected>>', self.select_scanned_device)
        actions = BackdropFrame(body, shade=.6)
        actions.grid(row=2, column=0, columnspan=3, sticky='w', pady=(4, 5))
        self.settings_scan_button = ttk.Button(actions, text='扫描模拟器', command=lambda: self.start_connection(False))
        self.settings_scan_button.pack(side='left')
        self.settings_connect_button = ttk.Button(actions, text='一键连接', command=lambda: self.start_connection(True))
        self.settings_connect_button.pack(side='left', padx=8)
        self.settings_connection_status_var = tk.StringVar(value='启动模拟器后点击「一键连接」；多开时先扫描，再从下拉列表选择设备。')
        ttk.Label(body, textvariable=self.settings_connection_status_var, wraplength=680,
                  style='Hint.TLabel').grid(row=3, column=0, columnspan=3, sticky='w', pady=(0, 6))
        ttk.Checkbutton(body, text='点击轻微偏移', variable=self.settings_click_jitter_var).grid(
            row=4, column=0, columnspan=3, sticky='w', pady=(10, 3))
        ttk.Label(body, text='在识别落点附近小幅随机偏移，按画面尺寸缩放；关闭后使用原落点。',
                  wraplength=680, style='Hint.TLabel').grid(row=5, column=0, columnspan=3, sticky='w', pady=(0, 4))
        ttk.Label(window, text='OCR 优先使用可用独显，无可用独显时自动使用 CPU。保存后可点击主面板「环境检测」检查连接和识别环境。',
                  wraplength=720, style='Hint.TLabel').pack(anchor='w', padx=18, pady=12)
        controls = BackdropFrame(window, padding=12, shade=.5)
        controls.pack(side='bottom', fill='x')
        ttk.Button(controls, text='错误日志', command=self.show_error_logs).pack(side='left')
        ttk.Button(controls, text='关闭', command=self.close_settings).pack(side='right')
        self.settings_save_button = ttk.Button(controls, text='保存设置', command=self.save_connection_settings)
        self.settings_save_button.pack(side='right', padx=8)
        self.set_connection_controls_busy(self.worker is not None or self.environment_worker is not None or self.connection_worker is not None)
        window.protocol('WM_DELETE_WINDOW', self.close_settings)

    def browse_adb(self):
        path = filedialog.askopenfilename(parent=self.settings_window, title='选择 adb.exe',
                                          filetypes=[('ADB 程序', 'adb.exe'), ('可执行程序', '*.exe')])
        if path:
            self.settings_adb_var.set(path)

    def save_connection_settings(self):
        if self.worker is not None or self.environment_worker is not None or self.connection_worker is not None:
            return
        adb = self.settings_adb_var.get().strip().strip('"')
        serial = self.settings_serial_var.get().strip()
        click_jitter = bool(self.settings_click_jitter_var.get())
        if not adb or not serial:
            self.record_error('连接设置', 'ADB 路径或设备序列号未填写')
            messagebox.showerror('设置无效', '请填写 ADB 路径和设备序列号。', parent=self.settings_window)
            return
        if adb.lower() not in ('adb', 'adb.exe'):
            adb = resolve_adb(adb) or adb
        previous = self.adb_var.get(), self.serial_var.get(), self.settings.get('click_jitter', True)
        self.adb_var.set(adb)
        self.serial_var.set(serial)
        self.settings.update(adb=adb, serial=serial, click_jitter=click_jitter)
        if not self.persist():
            self.adb_var.set(previous[0])
            self.serial_var.set(previous[1])
            self.settings.update(adb=previous[0], serial=previous[1], click_jitter=previous[2])
            return
        if (adb, serial) != previous[:2]:
            self.environment_status_var.set('环境：连接设置已更新，请重新检测')
        self.append_log('设置已保存；点击轻微偏移：' + ('开启' if click_jitter else '关闭'))
        self.close_settings()

    def close_settings(self):
        if self.settings_window is not None:
            self.settings_window.destroy()
        self.settings_window = self.settings_save_button = None
        self.settings_scan_button = self.settings_connect_button = self.settings_serial_entry = None
        self.settings_adb_entry = self.settings_browse_button = None

    def quick_connect(self):
        if self.worker is not None or self.environment_worker is not None or self.connection_worker is not None or self.closing:
            return
        self.show_settings()
        self.start_connection(True)

    def start_connection(self, connect):
        if self.worker is not None or self.environment_worker is not None or self.connection_worker is not None or self.closing:
            return
        adb, serial = self.settings_adb_var.get().strip(), self.settings_serial_var.get().strip()
        self.connection_stop.clear()
        self.stop_event.clear()
        self.set_busy(True)
        self.status_var.set('连接模拟器中' if connect else '扫描模拟器中')
        self.settings_connection_status_var.set(self.status_var.get() + '……')

        def report(message):
            self.events.put(('connection_status', message))

        def work():
            summary = '已停止'
            status = '已停止'
            try:
                if connect:
                    _, chosen, candidates = connect_emulator(adb, serial, self.connection_stop, report)
                    self.events.put(('connection_candidates', candidates))
                    self.events.put(('connected', {'adb': adb, 'serial': chosen}))
                    summary = '已连接：' + chosen
                    status = '模拟器已连接'
                else:
                    adb_path = resolve_adb(adb)
                    if adb_path is None:
                        raise RuntimeError('未找到 ADB，请选择 adb.exe；便携版可填 adb。')
                    candidates = discover_emulators(adb_path, self.connection_stop, report)
                    self.events.put(('connection_candidates', candidates))
                    summary = (f'发现 {len(candidates)} 个设备，请选择后点击「一键连接」。' if candidates else
                               '未发现模拟器，请先启动模拟器并开启 ADB 调试（本机连接），再扫描。')
                    status = '扫描完成' if candidates else '未发现模拟器'
            except DeviceChoiceRequired as error:
                self.events.put(('connection_candidates', error.candidates))
                summary = str(error)
                status = '请选择模拟器'
            except ConnectionCancelled:
                pass
            except Exception as error:
                summary = str(error)
                status = '连接失败'
                self.record_error('模拟器连接', summary, error=error)
            finally:
                self.events.put(('connection_done', {'summary': summary, 'status': status}))

        self.connection_worker = threading.Thread(target=work, daemon=True)
        self.connection_worker.start()

    def show_connection_candidates(self, candidates):
        self.connection_candidates = candidates
        if self.settings_serial_entry is None:
            return
        serials = [item.serial for item in candidates]
        self.settings_serial_entry.configure(values=[item.description for item in candidates])
        if self.settings_serial_var.get() not in serials and len(serials) == 1:
            self.settings_serial_var.set(serials[0])

    def select_scanned_device(self, event=None):
        description = self.settings_serial_var.get()
        device = next((item for item in self.connection_candidates if item.description == description), None)
        if device is not None:
            self.settings_serial_var.set(device.serial)
            self.settings_connection_status_var.set('已选择 ' + device.description + '，点击「一键连接」即可。')

    def save_connected_device(self, connection):
        previous = self.adb_var.get(), self.serial_var.get()
        self.adb_var.set(connection['adb'])
        self.serial_var.set(connection['serial'])
        self.settings.update(adb=connection['adb'], serial=connection['serial'])
        if not self.persist():
            self.adb_var.set(previous[0])
            self.serial_var.set(previous[1])
            self.settings.update(adb=previous[0], serial=previous[1])
            return
        if self.settings_window is not None:
            self.settings_adb_var.set(connection['adb'])
            self.settings_serial_var.set(connection['serial'])
        self.environment_status_var.set('设备：已连接 ' + connection['serial'])
        self.append_log('已连接并保存设备：' + connection['serial'])

    def record_error(self, source, message, error=None, details='', level='ERROR'):
        self.error_journal.record(source, message, error=error, details=details, level=level)
        self.events.put(('error_log', None))

    def handle_callback_error(self, exception_type, error, trace):
        details = ''.join(traceback.format_exception(exception_type, error, trace))
        if details != self.last_callback_trace:
            self.last_callback_trace = details
            self.record_error('界面回调', str(error), details=details)

    def show_error_logs(self):
        if self.error_window is not None:
            self.error_window.deiconify()
            self.error_window.lift()
            self.refresh_error_logs()
            return
        window = tk.Toplevel(self.root)
        self.error_window = window
        window.title('百炼英雄 · 错误日志')
        window.geometry('1040x700')
        window.minsize(800, 560)
        window.transient(self.root)
        window.backdrop = WindowBackdrop(window, self.background_image)
        ttk.Label(window, text='错误日志', style='Title.TLabel').pack(anchor='w', padx=16, pady=(14, 6))
        self.error_status_var = tk.StringVar()
        ttk.Label(window, textvariable=self.error_status_var, wraplength=980,
                  style='Hint.TLabel').pack(anchor='w', padx=16, pady=(0, 10))
        content = BackdropFrame(window, padding=10, shade=.5)
        content.pack(fill='both', expand=True, padx=12)
        self.error_tree = ttk.Treeview(content, columns=('time', 'level', 'source', 'message'),
                                      show='headings', height=10, selectmode='browse')
        for key, title, width in (('time', '时间', 165), ('level', '级别', 65),
                                   ('source', '来源', 150), ('message', '错误信息', 560)):
            self.error_tree.heading(key, text=title)
            self.error_tree.column(key, width=width, minwidth=60, stretch=key == 'message')
        self.error_tree.tag_configure('ERROR', foreground='#ffaaaa')
        self.error_tree.tag_configure('WARNING', foreground='#f1cf88')
        scrollbar = ttk.Scrollbar(content, orient='vertical', command=self.error_tree.yview)
        self.error_tree.configure(yscrollcommand=scrollbar.set)
        self.error_tree.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.error_tree.bind('<<TreeviewSelect>>', lambda event: self.show_error_details())
        self.error_detail_box = scrolledtext.ScrolledText(window, height=12, state='disabled', wrap='word',
                                                        background='#192235', foreground=TEXT,
                                                        selectbackground='#3d5f97', font=('Consolas', 10),
                                                        borderwidth=0, highlightthickness=0)
        self.error_detail_box.pack(fill='both', expand=True, padx=16, pady=10)
        controls = BackdropFrame(window, padding=12, shade=.5)
        controls.pack(fill='x')
        ttk.Button(controls, text='刷新', command=self.refresh_error_logs).pack(side='left')
        ttk.Button(controls, text='复制详情', command=self.copy_error_details).pack(side='left', padx=8)
        ttk.Button(controls, text='导出日志', command=self.export_error_logs).pack(side='left')
        ttk.Button(controls, text='关闭', command=self.close_error_logs).pack(side='right')
        window.protocol('WM_DELETE_WINDOW', self.close_error_logs)
        self.refresh_error_logs()

    def refresh_error_logs(self):
        if self.error_tree is None:
            return
        entries = self.error_journal.snapshot()
        retained = {entry['id'] for entry in entries}
        for item in self.error_tree.get_children():
            if item not in retained:
                self.error_tree.delete(item)
        for entry in reversed(entries):
            values = (display_time(entry['time']), '错误' if entry['level'] == 'ERROR' else '提示',
                      entry['source'], entry['message'].splitlines()[0] if entry['message'] else '')
            if not self.error_tree.exists(entry['id']):
                self.error_tree.insert('', 0, iid=entry['id'], values=values, tags=(entry['level'],))
        if not self.error_tree.selection() and self.error_tree.get_children():
            self.error_tree.selection_set(self.error_tree.get_children()[0])
        self.error_status_var.set(f'最近 {len(entries)} 条；本地日志：{self.error_journal.path}')
        if self.error_journal.write_error:
            self.error_status_var.set('日志文件暂时无法读写，当前错误仍在窗口中保留：' + self.error_journal.write_error)
        self.show_error_details()

    def show_error_details(self):
        if self.error_tree is None:
            return
        selection = self.error_tree.selection()
        entry = next((entry for entry in self.error_journal.snapshot() if selection and entry['id'] == selection[0]), None)
        text = self.error_journal.format_entry(entry) if entry is not None else '暂无错误日志。'
        self.error_detail_box.configure(state='normal')
        self.error_detail_box.delete('1.0', 'end')
        self.error_detail_box.insert('end', text)
        self.error_detail_box.configure(state='disabled')

    def copy_error_details(self):
        text = self.error_detail_box.get('1.0', 'end').strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.error_status_var.set('详情已复制')

    def export_error_logs(self):
        stamp = now().split('+')[0].replace('-', '').replace(':', '').replace('T', '_')
        path = filedialog.asksaveasfilename(parent=self.error_window, title='导出错误日志',
                                          initialfile=f'错误日志_{stamp}.txt', defaultextension='.txt',
                                          filetypes=[('文本日志', '*.txt')])
        if not path:
            return
        try:
            Path(path).write_text(self.error_journal.export_text() or '暂无错误日志。', encoding='utf-8')
            self.error_status_var.set('日志已导出：' + path)
        except OSError as error:
            self.record_error('导出错误日志', str(error), error=error)
            messagebox.showerror('导出失败', str(error), parent=self.error_window)

    def close_error_logs(self):
        if self.error_window is not None:
            self.error_window.destroy()
        self.error_window = self.error_tree = None

    def show_environment(self):
        if self.worker is not None or self.closing:
            return
        if self.environment_window is not None:
            self.environment_window.deiconify()
            self.environment_window.lift()
            if self.environment_worker is None:
                self.start_environment_check()
            return
        window = tk.Toplevel(self.root)
        self.environment_window = window
        window.title('百炼英雄 · 环境检测')
        window.geometry('920x660')
        window.minsize(760, 560)
        window.transient(self.root)
        window.backdrop = WindowBackdrop(window, self.background_image)
        ttk.Label(window, text='环境检测', style='Title.TLabel').pack(anchor='w', padx=16, pady=(14, 6))
        ttk.Label(window, text='自动检查依赖、独显或 CPU、模型、保存目录、ADB 连接、实际截图尺寸与画面适配；不会点击游戏。',
                  style='Hint.TLabel', wraplength=860).pack(anchor='w', padx=16, pady=(0, 12))
        content = BackdropFrame(window, padding=10, shade=.5)
        content.pack(fill='both', expand=True, padx=12)
        self.environment_tree = ttk.Treeview(content, columns=('name', 'status', 'detail'), show='headings', selectmode='browse', height=13)
        for key, title, width in (('name', '检测项目', 150), ('status', '结果', 75), ('detail', '说明', 600)):
            self.environment_tree.heading(key, text=title)
            self.environment_tree.column(key, width=width, minwidth=65, stretch=key == 'detail')
        for tag, color in (('ok', '#a9dfb4'), ('warn', '#f1cf88'), ('error', '#ffaaaa'), ('running', '#bac3d7')):
            self.environment_tree.tag_configure(tag, foreground=color)
        scrollbar = ttk.Scrollbar(content, orient='vertical', command=self.environment_tree.yview)
        self.environment_tree.configure(yscrollcommand=scrollbar.set)
        self.environment_tree.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.environment_tree.bind('<<TreeviewSelect>>', lambda event: self.show_environment_detail())
        self.environment_detail_box = scrolledtext.ScrolledText(window, height=5, width=1, state='disabled',
                                                               background='#192235', foreground=TEXT,
                                                               selectbackground='#3d5f97', font=('Microsoft YaHei UI', 9),
                                                               borderwidth=0, highlightthickness=0, wrap='word')
        self.environment_detail_box.pack(fill='x', padx=16, pady=10)
        self.set_environment_detail('选择检测项可查看完整说明。')
        controls = BackdropFrame(window, padding=12, shade=.5)
        controls.pack(fill='x', side='bottom')
        ttk.Button(controls, text='关闭', command=self.close_environment).pack(side='right')
        self.environment_retry_button = ttk.Button(controls, text='重新检测', command=self.start_environment_check)
        self.environment_retry_button.pack(side='left')
        self.environment_cancel_button = ttk.Button(controls, text='取消检测', command=self.cancel_environment_check)
        self.environment_cancel_button.pack(side='left', padx=8)
        window.protocol('WM_DELETE_WINDOW', self.close_environment)
        if self.environment_worker is None:
            self.start_environment_check()
        else:
            for row in self.environment_rows.values():
                self.update_environment_row(row)
            self.environment_retry_button.configure(state='disabled')

    def start_environment_check(self):
        if self.worker is not None or self.environment_worker is not None or self.connection_worker is not None or self.closing:
            return
        self.environment_rows.clear()
        if self.environment_tree is not None:
            for item in self.environment_tree.get_children():
                self.environment_tree.delete(item)
        self.environment_stop.clear()
        self.environment_status_var.set('环境：检测中…')
        self.status_var.set('环境检测中')
        self.set_busy(True)
        self.stop_button.configure(state='disabled')
        self.environment_retry_button.configure(state='disabled')
        self.environment_cancel_button.configure(state='normal')
        adb, serial = self.adb_var.get(), self.serial_var.get()
        roles = [dict(role) for role in self.roles]
        settings_directory = Path(self.settings_path).parent

        reported_problems = set()

        def report(row):
            key = (row['name'], row['status'], row['detail'])
            if row['status'] in ('error', 'warn') and key not in reported_problems:
                reported_problems.add(key)
                self.record_error('环境检测 / ' + row['name'], row['detail'],
                                  level='ERROR' if row['status'] == 'error' else 'WARNING')
            self.events.put(('environment_row', row))

        def work():
            try:
                result = check_environment(ASSETS.parent, settings_directory, adb, serial, roles,
                                           report=report, stop=self.environment_stop)
            except Exception as error:
                self.record_error('环境检测', str(error), error=error)
                self.events.put(('environment_row', {'name': '检测异常', 'status': 'error', 'detail': str(error)}))
                result = {'ready': False, 'cancelled': False, 'reader': None}
            self.events.put(('environment_done', result))

        self.environment_worker = threading.Thread(target=work, daemon=True)
        self.environment_worker.start()

    def update_environment_row(self, row):
        self.environment_rows[row['name']] = row
        if self.environment_tree is None:
            return
        statuses = {'ok': '正常', 'warn': '提示', 'error': '失败', 'running': '检测中'}
        values = (row['name'], statuses.get(row['status'], row['status']), row['detail'].replace('\n', ' '))
        if self.environment_tree.exists(row['name']):
            self.environment_tree.item(row['name'], values=values, tags=(row['status'],))
        else:
            self.environment_tree.insert('', 'end', iid=row['name'], values=values, tags=(row['status'],))
        self.show_environment_detail()

    def show_environment_detail(self):
        if self.environment_tree is None or not self.environment_tree.selection():
            return
        row = self.environment_rows.get(self.environment_tree.selection()[0])
        if row is not None:
            self.set_environment_detail(row['detail'])

    def set_environment_detail(self, detail):
        self.environment_detail_box.configure(state='normal')
        self.environment_detail_box.delete('1.0', 'end')
        self.environment_detail_box.insert('end', detail)
        self.environment_detail_box.configure(state='disabled')

    def cancel_environment_check(self):
        self.environment_stop.set()
        self.environment_status_var.set('环境：正在取消检测…')
        if self.environment_cancel_button is not None:
            self.environment_cancel_button.configure(state='disabled')

    def close_environment(self):
        self.environment_stop.set()
        if self.environment_window is not None:
            self.environment_window.destroy()
        self.environment_window = self.environment_tree = None
        self.environment_retry_button = self.environment_cancel_button = None

    def show_history(self):
        if self.history_window is not None:
            self.history_window.deiconify()
            self.history_window.lift()
            self.refresh_history()
            return
        window = tk.Toplevel(self.root)
        self.history_window = window
        window.title('百炼英雄 · 抽取记录')
        window.geometry('1120x680')
        window.minsize(960, 560)
        window.transient(self.root)
        window.backdrop = WindowBackdrop(window, self.background_image)
        ttk.Label(window, text='每次开始抽取单独记录；时间为北京时间（UTC+8）。选择一条记录查看角色明细。',
                  style='Hint.TLabel').pack(anchor='w', padx=12, pady=10)

        records = BackdropFrame(window, padding=(12, 0), shade=.4)
        records.pack(fill='both', expand=True)
        columns = ('start', 'end', 'rounds', 'hits', 'roles', 'status')
        self.history_tree = ttk.Treeview(records, columns=columns, show='headings', height=11, selectmode='browse')
        for column, title, width in zip(columns,
                                        ('开始时间', '结束时间', '抽取总次数', '抽中目标次数', '抽中的勾选角色', '状态'),
                                        (165, 165, 90, 95, 300, 105)):
            self.history_tree.heading(column, text=title)
            self.history_tree.column(column, width=width, minwidth=70, stretch=column == 'roles',
                                     anchor='w' if column in ('start', 'end', 'roles') else 'center')
        vertical = ttk.Scrollbar(records, orient='vertical', command=self.history_tree.yview)
        horizontal = ttk.Scrollbar(records, orient='horizontal', command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.history_tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        records.rowconfigure(0, weight=1)
        records.columnconfigure(0, weight=1)
        self.history_tree.bind('<<TreeviewSelect>>', lambda event: self.show_history_details())

        self.history_summary_var = tk.StringVar(value='暂无抽取记录，开始抽取后自动生成。')
        ttk.Label(window, textvariable=self.history_summary_var, wraplength=1050,
                  style='Hint.TLabel').pack(fill='x', padx=14, pady=10)
        details = BackdropFrame(window, text='本次角色明细', padding=8)
        details.pack(fill='both', expand=True, padx=12, pady=(0, 10))
        self.history_detail_tree = ttk.Treeview(details, columns=('name', 'count'), show='headings', height=6)
        self.history_detail_tree.heading('name', text='勾选角色')
        self.history_detail_tree.heading('count', text='本次抽中次数')
        self.history_detail_tree.column('name', width=650, minwidth=200)
        self.history_detail_tree.column('count', width=150, anchor='center')
        detail_scroll = ttk.Scrollbar(details, orient='vertical', command=self.history_detail_tree.yview)
        self.history_detail_tree.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side='right', fill='y')
        self.history_detail_tree.pack(fill='both', expand=True)
        ttk.Button(window, text='关闭', command=self.close_history).pack(anchor='e', padx=12, pady=(0, 10))
        window.protocol('WM_DELETE_WINDOW', self.close_history)
        self.refresh_history()

    def close_history(self):
        if self.history_window is not None:
            self.history_window.destroy()
        self.history_window = self.history_tree = self.history_detail_tree = None

    def refresh_history(self):
        if self.history_tree is None:
            return
        statuses = {'running': '抽取中', 'stopped': '已停止', 'completed': '已完成',
                    'error': '异常停止', 'interrupted': '上次中断'}
        for run in self.run_history.snapshot():
            won = run['recruited']
            end = display_time(run['ended_at']) or ('进行中' if run['status'] == 'running' else '未知')
            roles = '、'.join(f"{item['name']} ×{item['count']}" for item in won.values()) or '暂无'
            values = (display_time(run['started_at']), end, run['rounds'],
                      sum(item['count'] for item in won.values()), roles, statuses.get(run['status'], run['status']))
            if self.history_tree.exists(run['id']):
                self.history_tree.item(run['id'], values=values)
            else:
                self.history_tree.insert('', 0, iid=run['id'], values=values)
        if not self.history_tree.selection() and self.history_tree.get_children():
            self.history_tree.selection_set(self.history_tree.get_children()[0])
        self.show_history_details()

    def show_history_details(self):
        if self.history_detail_tree is None:
            return
        for item in self.history_detail_tree.get_children():
            self.history_detail_tree.delete(item)
        selection = self.history_tree.selection()
        if not selection:
            return
        run = next((item for item in self.run_history.snapshot() if item['id'] == selection[0]), None)
        if run is None:
            return
        names = {item['id']: item['name'] for item in run['targets']}
        for hero_id, result in run['recruited'].items():
            names.setdefault(hero_id, result['name'])
        for hero_id, name in names.items():
            count = run['recruited'].get(hero_id, {}).get('count', 0)
            self.history_detail_tree.insert('', 'end', values=(name, count))
        won = sum(item['count'] for item in run['recruited'].values())
        reason = run['reason'] or '正在抽取，记录实时更新'
        mode = '保底出红' if run.get('mode') == 'pity' else '普通抽取'
        self.history_summary_var.set(f"{mode}：本次抽取 {run['rounds']} 次，抽中勾选角色 {won} 次，累计 {run['coins']} 金币。{reason}")

    def selected(self):
        with self.selection_lock:
            return self.selected_ids

    def update_count(self):
        count = len(self.selected_ids)
        self.count_var.set(f'已勾选 {count} / {len(self.roles)} 个角色' if count else '未勾选角色：所有招募均使用一倍')

    def selection_changed(self):
        with self.selection_lock:
            self.selected_ids = frozenset(key for key, var in self.role_vars.items() if var.get())
        self.update_count()
        self.persist()
        if self.last_frame:
            self.show_frame(*self.last_frame)

    def select_all(self, checked):
        for var in self.role_vars.values():
            var.set(checked)
        self.selection_changed()

    def persist(self):
        self.settings.update(roles=self.roles, selected_ids=sorted(self.selected()),
                             recruited_counts=self.recruited_counts)
        try:
            save_settings(self.settings, self.settings_path)
            return True
        except OSError as error:
            self.record_error('保存设置或累计统计', str(error), error=error)
            messagebox.showerror('保存失败', str(error), parent=self.root)
            return False

    def import_roles(self):
        path = filedialog.askopenfilename(parent=self.root, title='选择三倍抽取角色名单', filetypes=[('CSV 名单', '*.csv')])
        if not path:
            return
        try:
            roles = load_roles(path)
        except (OSError, ValueError) as error:
            self.record_error('导入角色名单', str(error), error=error)
            messagebox.showerror('名单导入失败', str(error), parent=self.root)
            return
        # Importing a list does not implicitly enable spending on new roles.
        with self.selection_lock:
            self.selected_ids = self.selected_ids.intersection(role['id'] for role in roles)
        self.roles = roles
        self.render_roles()
        self.persist()
        if self.last_frame:
            self.show_frame(*self.last_frame)
        self.append_log(f'已导入 {len(roles)} 个角色，请勾选需要三倍抽取的角色。')

    def append_log(self, message):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', time.strftime('[%H:%M:%S] ') + str(message) + '\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def publish_frame(self, image, scene, heroes, target, selected_ids=None):
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait((image, scene, heroes, selected_ids))

    def show_frame(self, image, scene, heroes, selected_ids=None):
        self.last_frame = (image, scene, heroes, selected_ids)
        picture = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        picture.thumbnail((225, 500))
        photo = ImageTk.PhotoImage(picture, master=self.root)
        self.preview.configure(image=photo, text='')
        self.preview.image = photo
        if scene.luck is not None:
            current, maximum = scene.luck
            self.luck_var.set(f'幸运值：{current}/{maximum}' + ('（下一抽保底）' if current >= maximum - 1 else ''))
        elif scene.kind == 'select':
            self.luck_var.set('幸运值：未识别')
        selected = self.selected()
        if scene.kind == 'select' and (not selected or selected_ids == selected):
            target = choose_multiplier([h.id for h in heroes], selected)
            match_text = '匹配到勾选角色：' + ' / '.join(h.name for h in heroes) if target == 3 else '未匹配到勾选角色'
            self.match_var.set(match_text if selected else '未勾选角色，跳过角色检测。')
            self.decision_var.set(f"{'命中勾选角色' if target == 3 else '未命中勾选角色'} → {target} 倍 / {target * 100} 金币\n当前游戏：{scene.multiplier} 倍 / {scene.price} 金币")
        elif scene.kind == 'select':
            self.match_var.set('等待按当前勾选重新检测。')
            self.decision_var.set('勾选项已更新或正在等待下一轮，检测后显示预计倍率。')
        else:
            self.match_var.set('当前不是金币招募选择界面。')
            self.decision_var.set('当前招募界面尚未确认，等待界面就绪。' if scene.kind != 'result' else '检测到待放弃界面；开始抽取后自动点击放弃并继续。')

    def start(self, recruit, until_pity=False):
        if self.worker is not None or self.environment_worker is not None or self.connection_worker is not None or self.closing:
            return
        try:
            rounds, coins = int(self.rounds_var.get()), int(self.coins_var.get())
            if rounds < 0 or coins < 0:
                raise ValueError
            adb, serial = self.adb_var.get().strip(), self.serial_var.get().strip()
            if not adb or not serial:
                raise ValueError
        except ValueError as error:
            self.record_error('抽取参数', '连接参数或抽取上限无效', error=error)
            messagebox.showerror('参数无效', '请在「设置」中填写 ADB 路径和设备序列号；轮数及金币上限须为非负整数。', parent=self.root)
            return
        self.settings.update(adb=adb, serial=serial, max_rounds=rounds, coin_limit=coins)
        click_jitter = bool(self.settings.get('click_jitter', True))
        if not self.persist():
            return
        run_id = None
        if recruit:
            targets = [dict(role) for role in self.roles if role['id'] in self.selected()]
            try:
                run_id = self.run_history.begin(targets, mode='pity' if until_pity else 'normal')
            except OSError as error:
                self.record_error('创建抽取记录', str(error), error=error)
                messagebox.showerror('保存抽取记录失败', str(error), parent=self.root)
                return
            self.active_run_id = run_id
            self.refresh_history()
        self.stop_event.clear()
        self.set_busy(True)
        self.status_var.set(('保底抽取中' if until_pity else '抽取中') if recruit else '检测中')
        if until_pity:
            self.append_log('保底模式：幸运值达到 24/25 后再抽一次，确认完成即停止；轮数和金币上限仍生效。')

        def submitted(total, coins):
            if run_id is not None:
                self.run_history.record_draw(run_id, total, coins)
                self.events.put(('history', None))

        def recruited(hero_id, name):
            if run_id is not None:
                self.run_history.record_recruited(run_id, hero_id, name)
                self.events.put(('history', None))
            self.events.put(('recruited', (hero_id, name)))

        def work():
            runner = None
            outcome, reason = 'completed', ''
            try:
                adb_path, actual_serial, _ = connect_emulator(adb, serial, self.stop_event,
                    report=lambda message: self.events.put(('log', message)))
                self.events.put(('connected', {'adb': adb, 'serial': actual_serial}))
                if self.recognizer is None:
                    self.events.put(('log', '正在加载目标匹配与 OCR，首次检测需要几秒。'))
                    self.recognizer = HeroRecognizer()
                if self.stop_event.is_set():
                    raise Stopped()
                if self.reader is None:
                    self.reader = SceneReader()
                    self.events.put(('backend', self.reader.backend))
                    self.events.put(('log', f'OCR 已就绪：{self.reader.backend}；扫描等待 {SCAN_INTERVAL:g} 秒'))
                    if self.reader.backend_note:
                        self.record_error('OCR / CPU 回退', self.reader.backend_note, level='WARNING')
                        self.events.put(('log', 'GPU 未启用，已使用 CPU：' + self.reader.backend_note))
                device = AdbDevice(adb=adb_path, serial=actual_serial, stop=self.stop_event,
                                   on_geometry=lambda geometry: self.events.put(('geometry', geometry.describe())),
                                   click_jitter=click_jitter)
                if recruit:
                    self.events.put(('log', '点击轻微偏移：' + ('开启' if click_jitter else '关闭')))
                runner = RecruitRunner(device, self.recognizer, self.reader, self.selected,
                                       log=lambda s: self.events.put(('log', s)), update=self.publish_frame,
                                       on_recruited=recruited, on_draw=submitted)
                if recruit:
                    runner.run(rounds, coins, until_pity=until_pity)
                else:
                    _, scene, heroes, target = runner.inspect()
                    match_text = '匹配到：' + ' / '.join(h.name for h in heroes) if heroes else '未匹配到勾选角色'
                    self.events.put(('log', '检测完成：' + (match_text + f' → {target} 倍' if scene.kind == 'select' else '当前不是招募选择界面')))
            except (Stopped, ConnectionCancelled):
                outcome, reason = 'stopped', '用户停止'
                self.events.put(('log', '已停止'))
            except Exception as error:
                if isinstance(error, DeviceChoiceRequired):
                    self.events.put(('connection_candidates', error.candidates))
                outcome, reason = 'error', str(error)
                self.record_error('自动抽取' if recruit else '检测勾选角色', str(error), error=error)
                self.events.put(('log', f'已停止：{error}'))
            finally:
                if run_id is not None:
                    total = runner.rounds if runner is not None else 0
                    spent = runner.coins if runner is not None else 0
                    if outcome != 'error':
                        if self.stop_event.is_set():
                            outcome, reason = 'stopped', '关闭面板' if self.closing else '用户停止'
                        else:
                            reason = runner.end_reason if runner is not None else reason
                    try:
                        self.run_history.finish(run_id, outcome, reason, total, spent)
                    except OSError as error:
                        self.record_error('保存抽取记录', str(error), error=error)
                        self.events.put(('log', f'保存抽取记录失败：{error}'))
                self.events.put(('done', {'run_id': run_id, 'pity_completed': runner is not None and runner.pity_completed}))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def set_busy(self, busy):
        for button in (self.detect_button, self.start_button, self.pity_button, self.import_button, self.clear_stats_button, self.environment_button, self.connect_button):
            button.configure(state='disabled' if busy else 'normal')
        self.set_connection_controls_busy(busy)
        self.stop_button.configure(state='normal' if busy else 'disabled')

    def set_connection_controls_busy(self, busy):
        for control in (self.settings_save_button, self.settings_scan_button, self.settings_connect_button,
                        self.settings_serial_entry, self.settings_adb_entry, self.settings_browse_button):
            if control is not None:
                control.configure(state='disabled' if busy else 'normal')

    def stop(self):
        self.stop_event.set()
        self.connection_stop.set()
        self.status_var.set('正在停止')

    def poll(self):
        try:
            self.drain_events()
            if self.reader is not None and self.backend_var.get() != 'OCR：' + self.reader.backend:
                self.backend_var.set('OCR：' + self.reader.backend)
                self.append_log('当前 OCR 后端：' + self.reader.backend)
                if self.reader.backend_note:
                    self.record_error('OCR / CPU 回退', self.reader.backend_note, level='WARNING')
            try:
                self.show_frame(*self.frames.get_nowait())
            except queue.Empty:
                pass
        finally:
            if self.root.winfo_exists():
                self.poll_id = self.root.after(100, self.poll)

    def drain_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.append_log(value)
                elif kind == 'error_log':
                    self.refresh_error_logs()
                elif kind == 'recruited':
                    self.record_recruited(*value)
                elif kind == 'backend':
                    self.backend_var.set('OCR：' + value)
                elif kind == 'geometry':
                    self.geometry_var.set('画面：' + value)
                elif kind == 'history':
                    self.refresh_history()
                elif kind == 'connection_candidates':
                    self.show_connection_candidates(value)
                elif kind == 'connected':
                    self.save_connected_device(value)
                elif kind == 'connection_status':
                    if self.settings_window is not None:
                        self.settings_connection_status_var.set(value)
                elif kind == 'connection_done':
                    self.connection_worker = None
                    self.set_busy(False)
                    self.status_var.set('已停止' if self.connection_stop.is_set() else value['status'])
                    self.append_log(value['summary'])
                    if self.settings_window is not None:
                        self.settings_connection_status_var.set(value['summary'])
                elif kind == 'environment_row':
                    self.update_environment_row(value)
                elif kind == 'environment_done':
                    self.environment_worker = None
                    if value.get('connection') is not None and not value.get('cancelled'):
                        self.save_connected_device(value['connection'])
                    if value.get('reader') is not None and not value.get('cancelled'):
                        self.reader = value['reader']
                        self.backend_var.set('OCR：' + self.reader.backend)
                    if value.get('cancelled'):
                        summary = '环境：检测已取消'
                    elif value['ready']:
                        device = 'CPU' if self.reader is None or self.reader.backend.startswith('CPU') else 'GPU'
                        summary = f'环境：就绪（{device}）'
                    else:
                        summary = '环境：存在异常，请查看检测结果'
                    self.environment_status_var.set(summary)
                    self.append_log(summary)
                    self.status_var.set('空闲')
                    self.set_busy(False)
                    if self.environment_retry_button is not None:
                        self.environment_retry_button.configure(state='normal')
                        self.environment_cancel_button.configure(state='disabled')
                        if not self.environment_tree.selection():
                            self.set_environment_detail(summary)
                elif kind == 'done':
                    self.worker = None
                    if self.active_run_id == value['run_id']:
                        self.active_run_id = None
                    self.refresh_history()
                    self.set_busy(False)
                    self.status_var.set('已停止' if self.stop_event.is_set() else
                                        '已完成保底抽取' if value['pity_completed'] else '空闲')
        except queue.Empty:
            pass

    def close(self):
        self.closing = True
        self.stop_event.set()
        self.environment_stop.set()
        self.connection_stop.set()
        if any(worker is not None and worker.is_alive() for worker in (self.worker, self.environment_worker, self.connection_worker)):
            self.status_var.set('正在停止并保存记录')
            self.root.after(100, self.close)
            return
        self.drain_events()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def run_gui():
    root = tk.Tk()
    try:
        RecruitApp(root)
    except Exception as error:
        journal = ErrorJournal(BASE / 'error_logs')
        journal.record('程序启动', str(error), error=error)
        messagebox.showerror('启动失败', f'{error}\n\n错误日志：{journal.path}', parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    run_gui()

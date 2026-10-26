"""Shared wallpaper for Tk recruitment panels, repainted only on layout changes."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import weakref

from PIL import Image, ImageOps, ImageTk


SURFACE = '#171b2b'
TEXT = '#f3f4fa'
MUTED = '#bac3d7'


def configure_theme(root):
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=SURFACE, foreground=TEXT, font=('Microsoft YaHei UI', 9))
    style.configure('TLabel', background=SURFACE, foreground=TEXT)
    style.configure('Title.TLabel', background=SURFACE, foreground=TEXT, font=('Microsoft YaHei UI', 14, 'bold'))
    style.configure('Hint.TLabel', background=SURFACE, foreground=MUTED)
    style.configure('TButton', background='#303c59', foreground=TEXT, bordercolor='#526180', padding=(8, 5))
    style.map('TButton', background=[('disabled', '#242a3b'), ('pressed', '#415e94'), ('active', '#3b507d')],
              foreground=[('disabled', '#8290aa')])
    style.configure('TCheckbutton', background=SURFACE, foreground=TEXT, indicatorbackground='#29354d')
    style.map('TCheckbutton', background=[('active', '#29354d')], foreground=[('disabled', '#8290aa')],
              indicatorbackground=[('selected', '#76a4ef')])
    style.configure('TEntry', fieldbackground='#202a40', foreground=TEXT, insertcolor=TEXT, bordercolor='#526180')
    style.configure('TScrollbar', background='#33415c', troughcolor='#171c2d', bordercolor='#33415c', arrowcolor=MUTED)
    style.configure('Treeview', background='#192235', fieldbackground='#192235', foreground=TEXT,
                    bordercolor='#465574', rowheight=27)
    style.configure('Treeview.Heading', background='#303c59', foreground=TEXT, relief='flat')
    style.map('Treeview', background=[('selected', '#3d5f97')], foreground=[('selected', '#ffffff')])
    style.map('Treeview.Heading', background=[('active', '#3b507d')])


class WindowBackdrop:
    def __init__(self, window, image):
        self.window = window
        self.source = image
        self.rendered = None
        self.size = None
        self.photo = None
        self.pending = None
        self.closed = False
        self.surfaces = weakref.WeakSet()
        window._window_backdrop = self
        window.configure(background=SURFACE)
        self.label = tk.Label(window, borderwidth=0, highlightthickness=0, background=SURFACE, anchor='nw')
        self.label.place(x=0, y=0, relwidth=1, relheight=1)
        self.label.lower()
        window.bind('<Configure>', self._configure, add='+')
        window.bind('<Destroy>', self._destroy, add='+')
        self.request()

    def _configure(self, event):
        if event.widget is self.window:
            self.request()

    def _destroy(self, event):
        if event.widget is self.window:
            self.closed = True
            if self.pending is not None:
                self.window.after_cancel(self.pending)
                self.pending = None

    def request(self, event=None):
        if self.closed or self.pending is not None:
            return
        self.pending = self.window.after(90, self.redraw)

    def register(self, surface):
        self.surfaces.add(surface)
        surface.bind('<Configure>', self.request, add='+')
        self.request()

    def redraw(self):
        self.pending = None
        if self.closed:
            return
        size = (max(1, self.window.winfo_width()), max(1, self.window.winfo_height()))
        if size != self.size:
            # Favor the upper part of a portrait when filling a wide window.
            self.rendered = ImageOps.fit(self.source, size, method=Image.Resampling.LANCZOS, centering=(.5, .22))
            self.size = size
            self.photo = ImageTk.PhotoImage(self.rendered, master=self.window)
            self.label.configure(image=self.photo)
        for surface in tuple(self.surfaces):
            if surface.winfo_exists():
                surface.paint_background(self)

    def crop(self, widget, shade):
        width, height = max(1, widget.winfo_width()), max(1, widget.winfo_height())
        x = widget.winfo_rootx() - self.window.winfo_rootx()
        y = widget.winfo_rooty() - self.window.winfo_rooty()
        crop = self.rendered.crop((x, y, x+width, y+height))
        if shade:
            crop = Image.blend(crop, Image.new('RGB', crop.size, SURFACE), shade)
        return ImageTk.PhotoImage(crop, master=self.window)


def backdrop_for(widget):
    while widget is not None:
        if hasattr(widget, '_window_backdrop'):
            return widget._window_backdrop
        widget = widget.master
    raise RuntimeError('背景尚未初始化')


def padding_pair(padding):
    if isinstance(padding, int):
        return padding, padding
    if len(padding) == 2:
        return padding
    if len(padding) == 4:
        return (padding[0] + padding[2]) // 2, (padding[1] + padding[3]) // 2
    return padding[0], padding[0]


class BackdropFrame(tk.Frame):
    def __init__(self, master, text=None, padding=0, shade=None, **kwargs):
        padx, pady = padding_pair(padding)
        super().__init__(master, background=SURFACE, borderwidth=0, padx=padx, pady=pady, **kwargs)
        self.shade = (.48 if text is not None else .28) if shade is None else shade
        self.background_label = tk.Label(self, borderwidth=0, highlightthickness=0, background=SURFACE, anchor='nw')
        self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.background_label.lower()
        self.background_photo = None
        if text is not None:
            tk.Label(self, text=text, background=SURFACE, foreground=MUTED,
                     font=('Microsoft YaHei UI', 9, 'bold'), anchor='w', borderwidth=0).pack(anchor='w', pady=(0, 7))
        backdrop_for(self).register(self)

    def paint_background(self, backdrop):
        self.background_photo = backdrop.crop(self, self.shade)
        self.background_label.configure(image=self.background_photo)


class BackdropCanvas(tk.Canvas):
    def __init__(self, master, **kwargs):
        super().__init__(master, background=SURFACE, **kwargs)
        self.background_photo = None
        self.background_item = self.create_image(0, 0, anchor='nw')
        backdrop_for(self).register(self)

    def paint_background(self, backdrop):
        self.background_photo = backdrop.crop(self, .38)
        self.itemconfigure(self.background_item, image=self.background_photo)
        self.tag_lower(self.background_item)

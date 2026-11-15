# -*- coding: utf-8 -*-
"""Окно Discord Voice Catcher — тёмный минималистичный дизайн (customtkinter).
Страницы: Обзор, Серверы, Маршруты, Журнал, Настройки. Логика — в discord_voice_catcher."""
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

import customtkinter as ctk

import discord_voice_catcher as core
from discord_voice_catcher import (APP_NAME, APP_VERSION, LIVE_IDLE_AFTER, RECHECK_AFTER_FAIL, STATUS_ORDER,
                                   Checker, LiveState, Store, Tailer, autostart_get, autostart_set, fmt_ago, fmt_ts,
                                   ip_key, region_of_host, resolve_hosts, via_text)

# ---------------------------------------------------------------- палитра
BG = "#0b0d12"
SIDE = "#0f1218"
CARD = "#151922"
CARD2 = "#1c212c"
LINE = "#242a37"
TEXT = "#e9ecf2"
MUTED = "#8b94a7"
DIM = "#5a6274"
ACCENT = "#8b5cf6"
ACCENT_H = "#7c4dea"
PINK = "#ec4899"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"
GRAY = "#6b7280"
SEL = "#2b2346"

LEVEL = {"ok": GREEN, "wait": AMBER, "warn": AMBER, "fail": RED, "idle": GRAY}
STATUS_UI = {"blocked": ("●  Блок", RED), "fixed": ("●  Исправлено", GREEN), "drops": ("●  Обрывы", AMBER),
             "ok": ("●  Работает", TEXT), "silent": ("○  Не отвечает", DIM), "seen": ("○  Не проверен", DIM),
             "gone": ("○  Удалён", DIM)}
AUTO_CHOICES = [("Выкл", 0), ("5 мин", 5), ("10 мин", 10), ("30 мин", 30), ("1 час", 60)]

# Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) — одинаковые коды
I_HOME, I_LIST, I_ROUTE, I_LOG, I_GEAR = "", "", "", "", ""
I_REFRESH, I_SAVE, I_FOLDER, I_SEARCH, I_DEL, I_COPY = "", "", "", "", "", ""
I_MIC, I_TV, I_CHECK, I_WARN, I_INFO, I_ADD = "", "", "", "", "", ""


def blend(c1, c2, t):
    """Смесь двух цветов: t=0 — c1, t=1 — c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def make_icon_image(count, level="idle", size=64):
    """Иконка трея/окна: градиентный кружок с числом проблемных серверов."""
    from PIL import Image, ImageDraw, ImageFont
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if level == "fail":
        c1, c2 = (239, 68, 68), (190, 30, 60)
    elif level == "ok":
        c1, c2 = (34, 197, 94), (16, 140, 110)
    else:
        c1, c2 = (139, 92, 246), (236, 72, 153)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(c1, c2)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((8, 8, s - 8, s - 8), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    text = (str(count) if count < 100 else "99+") if count else ("✓" if level == "ok" else "")
    if text:
        try:
            font = ImageFont.truetype("segoeuib.ttf", int(s * (0.5 if len(text) < 3 else 0.36)))
        except Exception:
            font = ImageFont.load_default()
        bb = d.textbbox((0, 0), text, font=font)
        d.text(((s - bb[2] + bb[0]) / 2 - bb[0], (s - bb[3] + bb[1]) / 2 - bb[1]), text, fill="white", font=font)
    else:   # волна звука
        w = s // 14
        for i, h in enumerate((0.22, 0.42, 0.3, 0.5, 0.26)):
            x = s * (0.26 + i * 0.12)
            d.rounded_rectangle((x - w / 2, s * (0.5 - h / 2), x + w / 2, s * (0.5 + h / 2)), radius=w // 2,
                                fill="white")
    return img.resize((size, size), Image.LANCZOS)


def single_instance():
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DiscordVoiceCatcherMutex")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return None
        return h
    except Exception:
        return True


def fmt_addrs(n):
    return "{:,}".format(n).replace(",", " ")


def plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def run_gui(args):
    if single_instance() is None:
        r = tk.Tk()
        r.withdraw()
        from tkinter import messagebox
        messagebox.showinfo(APP_NAME, "Программа уже запущена — ищите иконку в трее рядом с часами.")
        return

    store = Store(args.data)
    events = queue.Queue()
    live = LiveState()

    def emit(text, new_host=None, level="info", addr=None, check=False):
        events.put((text, new_host, level, addr, check))

    tailer = Tailer(store, args.logdir, emit, live=live)
    checker = Checker(store, emit)

    ctk.set_appearance_mode("dark")
    root = ctk.CTk(fg_color=BG)
    root.title(APP_NAME)
    root.geometry("1260x780")
    root.minsize(1040, 640)
    try:   # своя иконка окна (.ico); CTk ставит свою через ~200 мс — перебиваем позже
        ico_path = os.path.join(store.data_dir, "app.ico")
        make_icon_image(0, size=256).save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)])
        root.after(400, lambda: root.iconbitmap(ico_path))
    except Exception:
        pass

    import tkinter.font as tkfont
    families = set(tkfont.families())
    ICON_FAMILY = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
    DISP = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else "Segoe UI"
    BODY = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
    MONO = "Cascadia Mono" if "Cascadia Mono" in families else "Consolas"

    def F(size, weight="normal", family=None):
        return ctk.CTkFont(family=family or BODY, size=size, weight=weight)

    def FI(size):
        return ctk.CTkFont(family=ICON_FAMILY, size=size)

    scale = root.winfo_fpixels("1i") / 96.0

    state = {"tray": None, "notify": False, "sig": None, "auto_next": None, "recheck_due": {},
             "startup_check": False, "icon": None, "page": None}
    cache = {}

    def setw(w, **kw):
        """configure только при изменении (customtkinter перерисовывается небыстро)."""
        key = id(w)
        old = cache.setdefault(key, {})
        diff = {k: v for k, v in kw.items() if old.get(k) != v}
        if diff:
            w.configure(**diff)
            old.update(diff)

    # ---------------------------------------------------------------- ttk (таблицы) в тёмном стиле
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Dark.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, borderwidth=0,
                    rowheight=int(32 * scale), font=(BODY, 10))
    style.layout("Dark.Treeview", [("Dark.Treeview.treearea", {"sticky": "nswe"})])
    style.map("Dark.Treeview", background=[("selected", SEL)], foreground=[("selected", TEXT)])
    style.configure("Dark.Treeview.Heading", background=CARD, foreground=DIM, borderwidth=0, relief="flat",
                    font=(BODY, 9), padding=(8, int(8 * scale)))
    style.map("Dark.Treeview.Heading", background=[("active", CARD2)], foreground=[("active", MUTED)])

    # ---------------------------------------------------------------- общие элементы
    def card(parent, **kw):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=LINE, **kw)

    icon_cache = {}
    icon_font_file = next((p for p in (r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf")
                           if os.path.exists(p)), None)

    def icon_img(glyph, color, px=16):
        """Глиф системного шрифта иконок → картинка (чтобы иконка и текст жили в одной кнопке)."""
        key = (glyph, color, px)
        if key in icon_cache:
            return icon_cache[key]
        if not icon_font_file:
            return None
        from PIL import Image, ImageDraw, ImageFont
        s = px * 4
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        f = ImageFont.truetype(icon_font_file, int(s * 0.9))
        d = ImageDraw.Draw(img)
        bb = d.textbbox((0, 0), glyph, font=f)
        d.text(((s - bb[2] + bb[0]) / 2 - bb[0], (s - bb[3] + bb[1]) / 2 - bb[1]), glyph, font=f, fill=color)
        im = ctk.CTkImage(img, size=(px, px))
        icon_cache[key] = im
        return im

    def btn(parent, text, icon=None, command=None, kind="primary", width=0, height=38):
        colors = {"primary": dict(fg_color=ACCENT, hover_color=ACCENT_H, text_color="white", border_width=0),
                  "ghost": dict(fg_color=CARD2, hover_color=LINE, text_color=TEXT, border_width=0),
                  "danger": dict(fg_color="transparent", hover_color="#2a1517", text_color=RED, border_width=1,
                                 border_color="#4a2226")}[kind]
        img = icon_img(icon, colors["text_color"] if colors["text_color"].startswith("#") else "#ffffff", 15) \
            if icon else None
        return ctk.CTkButton(parent, text=text, image=img, compound="left", command=command, corner_radius=10,
                             height=height, width=width, font=F(13, "bold" if kind == "primary" else "normal"),
                             **colors)

    def seg(parent, values, command, current):
        s = ctk.CTkSegmentedButton(parent, values=values, command=command, font=F(12), height=34,
                                   corner_radius=10, fg_color=CARD2, selected_color=ACCENT,
                                   selected_hover_color=ACCENT_H, unselected_color=CARD2,
                                   unselected_hover_color=LINE, text_color=TEXT)
        s.set(current)
        return s

    def switch(parent, text, value, command):
        v = tk.BooleanVar(value=value)
        s = ctk.CTkSwitch(parent, text=text, variable=v, command=command, font=F(13), text_color=TEXT,
                          progress_color=ACCENT, button_color="#ffffff", button_hover_color="#e5e5e5",
                          fg_color=LINE, switch_width=42, switch_height=22)
        return s, v

    def page_header(parent, title, sub):
        h = ctk.CTkFrame(parent, fg_color="transparent")
        h.pack(fill="x", padx=32, pady=(28, 18))
        left = ctk.CTkFrame(h, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text=title, font=F(26, "bold", DISP), text_color=TEXT).pack(anchor="w")
        subl = ctk.CTkLabel(left, text=sub, font=F(13), text_color=MUTED)
        subl.pack(anchor="w", pady=(2, 0))
        right = ctk.CTkFrame(h, fg_color="transparent", width=1, height=1)
        right.pack(side="right")
        return right, subl

    def make_tree(parent, cols):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", style="Dark.Treeview",
                            selectmode="extended")
        for c in cols:
            cid, title, w = c[0], c[1], c[2]
            stretch = c[3] if len(c) > 3 else False
            anchor = c[4] if len(c) > 4 else "w"
            tree.heading(cid, text=title.upper(), anchor=anchor)
            tree.column(cid, width=int(w * scale), anchor=anchor, stretch=stretch)
        sb = ctk.CTkScrollbar(wrap, command=tree.yview, fg_color="transparent", button_color=LINE,
                              button_hover_color=DIM, width=12)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(4, 0))
        for st, (_, color) in STATUS_UI.items():
            tree.tag_configure(st, foreground=color)
        tree.tag_configure("ignored", foreground=DIM)
        tree.tag_configure("wide", foreground=AMBER)
        return wrap, tree

    def dark_menu():
        return tk.Menu(root, tearoff=0, bg=CARD2, fg=TEXT, activebackground=ACCENT, activeforeground="white",
                       bd=0, relief="flat", font=(BODY, 10))

    # ---------------------------------------------------------------- диалог и уведомления
    def dialog(title, text, buttons, icon=I_INFO, icon_color=ACCENT):
        """buttons: [(текст, значение, kind)] — первая справа главная. Возвращает значение или None."""
        res = {"v": None}
        w = ctk.CTkToplevel(root, fg_color=CARD)
        w.title(title)
        w.resizable(False, False)
        w.transient(root)
        body = ctk.CTkFrame(w, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=26, pady=(24, 18))
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=icon, font=FI(22), text_color=icon_color, width=34).pack(side="left", anchor="n")
        tx = ctk.CTkFrame(top, fg_color="transparent")
        tx.pack(side="left", fill="x", padx=(10, 0))
        ctk.CTkLabel(tx, text=title, font=F(17, "bold", DISP), text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(tx, text=text, font=F(13), text_color=MUTED, justify="left", anchor="w",
                     wraplength=int(440 * 1)).pack(anchor="w", pady=(6, 0))
        bb = ctk.CTkFrame(body, fg_color="transparent")
        bb.pack(fill="x", pady=(22, 0))

        def done(v):
            res["v"] = v
            w.grab_release()
            w.destroy()
        for label, value, kind in buttons:
            b = btn(bb, label, command=lambda v=value: done(v), kind=kind, height=36)
            b.pack(side="right", padx=(8, 0))
        w.protocol("WM_DELETE_WINDOW", lambda: done(None))
        w.bind("<Escape>", lambda e: done(None))
        w.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - w.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - w.winfo_reqheight()) // 3
        w.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        w.after(50, lambda: (w.lift(), w.focus_force(), w.grab_set()))
        root.wait_window(w)
        return res["v"]

    toast_state = {"job": None}

    def toast(text, color=ACCENT):
        if toast_state["job"]:
            root.after_cancel(toast_state["job"])
        setw(toast_dot, text_color=color)
        setw(toast_lbl, text=text)
        toast_box.place(relx=1.0, rely=1.0, x=-24, y=-20, anchor="se")
        toast_box.lift()
        toast_state["job"] = root.after(4500, toast_box.place_forget)

    def copy(text, what):
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        toast("Скопировано: %s" % what)

    def open_folder(select=None):
        store.save(force=True)
        if select and os.path.exists(select):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(select)])
        else:
            os.startfile(store.data_dir)

    def notify(title, text):
        if state["tray"] is not None and state["notify"] and store.settings.get("notify", True):
            try:
                state["tray"].notify(text[:220], title)
            except Exception:
                pass

    # ================================================================ каркас: меню слева + страницы
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(0, weight=1)
    side = ctk.CTkFrame(root, fg_color=SIDE, corner_radius=0, width=232)
    side.grid(row=0, column=0, sticky="nsw")
    side.grid_propagate(False)
    content = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
    content.grid(row=0, column=1, sticky="nsew")
    content.grid_rowconfigure(0, weight=1)
    content.grid_columnconfigure(0, weight=1)

    logo = ctk.CTkFrame(side, fg_color="transparent")
    logo.pack(fill="x", padx=20, pady=(26, 30))
    logo_img = ctk.CTkImage(make_icon_image(0, size=96), size=(40, 40))
    ctk.CTkLabel(logo, image=logo_img, text="").pack(side="left")
    lt = ctk.CTkFrame(logo, fg_color="transparent")
    lt.pack(side="left", padx=(12, 0))
    ctk.CTkLabel(lt, text="Voice Catcher", font=F(16, "bold", DISP), text_color=TEXT).pack(anchor="w")
    ctk.CTkLabel(lt, text="для Discord", font=F(12), text_color=DIM).pack(anchor="w")

    pages, nav = {}, {}

    def show_page(name):
        for n, (b, bar, icon, badge) in nav.items():
            on = n == name
            setw(b, fg_color=CARD if on else "transparent", text_color=TEXT if on else MUTED,
                 image=icon_img(icon, ACCENT if on else DIM, 18))
            setw(bar, fg_color=ACCENT if on else SIDE)
        pages[name].tkraise()
        state["page"] = name

    def nav_item(name, icon, text):
        row = ctk.CTkFrame(side, fg_color="transparent", height=44)
        row.pack(fill="x", padx=(0, 14), pady=2)
        row.pack_propagate(False)
        bar = ctk.CTkFrame(row, fg_color=SIDE, width=3, height=20, corner_radius=2)
        bar.pack(side="left", pady=12)
        b = ctk.CTkButton(row, text="  " + text, image=icon_img(icon, DIM, 18), compound="left", anchor="w",
                          font=F(14), height=42, corner_radius=10, fg_color="transparent", hover_color=CARD,
                          text_color=MUTED, command=lambda: show_page(name))
        b.pack(side="left", fill="x", expand=True, padx=(11, 0))
        # счётчик проблем справа (пилюля)
        badge = ctk.CTkLabel(row, text="", font=F(11, "bold"), text_color="white", fg_color=RED, corner_radius=9,
                             width=22, height=18)
        nav[name] = (b, bar, icon, badge)
        p = ctk.CTkFrame(content, fg_color=BG, corner_radius=0)
        p.grid(row=0, column=0, sticky="nsew")
        pages[name] = p
        return p

    pg_home = nav_item("home", I_HOME, "Обзор")
    pg_srv = nav_item("srv", I_LIST, "Серверы")
    pg_rt = nav_item("rt", I_ROUTE, "Маршруты")
    pg_log = nav_item("log", I_LOG, "Журнал")
    pg_set = nav_item("set", I_GEAR, "Настройки")

    side_foot = ctk.CTkFrame(side, fg_color="transparent")
    side_foot.pack(side="bottom", fill="x", padx=22, pady=20)
    foot_row = ctk.CTkFrame(side_foot, fg_color="transparent")
    foot_row.pack(fill="x")
    foot_dot = ctk.CTkLabel(foot_row, text="●", font=F(11), text_color=GRAY, width=12)
    foot_dot.pack(side="left")
    foot_lbl = ctk.CTkLabel(foot_row, text="Запуск…", font=F(12), text_color=MUTED)
    foot_lbl.pack(side="left", padx=(6, 0))
    ctk.CTkLabel(side_foot, text="версия %s" % APP_VERSION, font=F(11), text_color=DIM).pack(anchor="w",
                                                                                          pady=(4, 0))

    # тост — поверх контента
    toast_box = ctk.CTkFrame(content, fg_color=CARD2, corner_radius=12, border_width=1, border_color=LINE)
    toast_dot = ctk.CTkLabel(toast_box, text="●", font=F(12), text_color=ACCENT, width=12)
    toast_dot.pack(side="left", padx=(16, 8), pady=12)
    toast_lbl = ctk.CTkLabel(toast_box, text="", font=F(13), text_color=TEXT)
    toast_lbl.pack(side="left", padx=(0, 18), pady=12)

    # ================================================================ ОБЗОР
    hr, home_sub = page_header(pg_home, "Обзор", "Голосовые серверы Discord в реальном времени")
    btn_save_h = btn(hr, "Сохранить маршруты", I_SAVE, lambda: save_routes(), "ghost", width=210)
    btn_save_h.pack(side="right", padx=(10, 0))
    btn_check = btn(hr, "Проверить", I_REFRESH, lambda: run_check("all", "все серверы"), "primary", width=150)
    btn_check.pack(side="right")

    home = ctk.CTkFrame(pg_home, fg_color="transparent")
    home.pack(fill="both", expand=True, padx=32, pady=(0, 28))
    home.grid_columnconfigure((0, 1), weight=1, uniform="c")

    hero = card(home)
    hero.grid(row=0, column=0, columnspan=2, sticky="ew")
    ring = tk.Canvas(hero, width=int(108 * scale), height=int(108 * scale), bg=CARD, highlightthickness=0, bd=0)
    ring.pack(side="left", padx=(26, 18), pady=22)
    hero_txt = ctk.CTkFrame(hero, fg_color="transparent")
    hero_txt.pack(side="left", fill="x", expand=True, pady=22)
    hero_title = ctk.CTkLabel(hero_txt, text="", font=F(28, "bold", DISP), text_color=TEXT, anchor="w")
    hero_title.pack(anchor="w")
    hero_sub = ctk.CTkLabel(hero_txt, text="", font=F(14), text_color=MUTED, anchor="w", justify="left")
    hero_sub.pack(anchor="w", pady=(4, 0))
    hero_prog = ctk.CTkProgressBar(hero_txt, height=6, corner_radius=3, fg_color=CARD2, progress_color=ACCENT)
    hero_prog.set(0)
    hero_net = ctk.CTkFrame(hero, fg_color="transparent")
    hero_net.pack(side="right", padx=26)

    def net_line(parent, title):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(anchor="e", pady=3)
        v = ctk.CTkLabel(f, text="—", font=F(14, "bold"), text_color=TEXT)
        v.pack(side="right")
        ctk.CTkLabel(f, text=title, font=F(12), text_color=DIM).pack(side="right", padx=(0, 10))
        return v
    net_isp = net_line(hero_net, "Провайдер")
    net_vpn = net_line(hero_net, "VPN")

    ring_state = {"k": None}

    def draw_ring(color, glyph):
        if ring_state["k"] == (color, glyph):
            return
        ring_state["k"] = (color, glyph)
        ring.delete("all")
        s = int(108 * scale)
        c = s / 2
        for r, t in ((0.5, 0.88), (0.42, 0.75), (0.34, 0.0)):
            rr = s * r
            ring.create_oval(c - rr, c - rr, c + rr, c + rr, fill=blend(color, CARD, t), outline="")
        ring.create_text(c, c, text=glyph, fill="white", font=(ICON_FAMILY, int(20)))

    # живые карточки
    lives = {}
    for col, (ctx, icon, title) in enumerate((("default", I_MIC, "Голос и звонки"),
                                              ("stream", I_TV, "Трансляция · Go Live"))):
        cf = card(home)
        cf.grid(row=1, column=col, sticky="nsew", pady=(16, 0), padx=(0, 8) if col == 0 else (8, 0))
        top = ctk.CTkFrame(cf, fg_color="transparent")
        top.pack(fill="x", padx=22, pady=(20, 0))
        icw = ctk.CTkLabel(top, text=icon, font=FI(16), text_color=MUTED, width=36, height=36, corner_radius=10,
                           fg_color=CARD2)
        icw.pack(side="left")
        ctk.CTkLabel(top, text=title, font=F(13), text_color=MUTED).pack(side="left", padx=(12, 0))
        dot = ctk.CTkLabel(top, text="●", font=F(12), text_color=GRAY)
        dot.pack(side="right")
        mid = ctk.CTkFrame(cf, fg_color="transparent")
        mid.pack(fill="x", padx=22, pady=(16, 20))
        stl = ctk.CTkLabel(mid, text="Не подключено", font=F(20, "bold", DISP), text_color=TEXT, anchor="w")
        stl.pack(anchor="w")
        det = ctk.CTkLabel(mid, text=" ", font=F(13), text_color=MUTED, anchor="w", justify="left")
        det.pack(anchor="w", pady=(4, 0))
        ping = ctk.CTkLabel(cf, text="", font=F(28, "bold", DISP), text_color=TEXT)
        ping.place(relx=1.0, x=-22, rely=1.0, y=-20, anchor="se")
        lives[ctx] = (icw, dot, stl, det, ping)

    # плитки
    tiles = ctk.CTkFrame(home, fg_color="transparent")
    tiles.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(16, 0))
    tiles.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="t")
    tile_v = {}
    for i, (key, title) in enumerate((("total", "Голосовых серверов"), ("blocked", "Не отвечают"),
                                      ("fixed", "Исправлено"), ("routes", "Маршрутов в файле"))):
        t = card(tiles)
        t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0 if i == 3 else 8))
        v = ctk.CTkLabel(t, text="—", font=F(30, "bold", DISP), text_color=TEXT)
        v.pack(anchor="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(t, text=title, font=F(12), text_color=DIM).pack(anchor="w", padx=20, pady=(0, 16))
        tile_v[key] = v
    for key, page in (("total", "srv"), ("blocked", "srv"), ("fixed", "srv"), ("routes", "rt")):
        w = tile_v[key].master
        for wdg in [w] + list(w.winfo_children()):
            wdg.bind("<Button-1>", lambda e, p=page: show_page(p))
            wdg.configure(cursor="hand2")

    def update_live():
        snap = live.snapshot()
        now = time.time()
        for ctx, (icw, dot, stl, det, ping) in lives.items():
            c = snap.get(ctx)
            ping_txt = ""
            if not c or (c["level"] in ("ok", "warn", "wait") and now - c["last"] > LIVE_IDLE_AFTER):
                level, st_txt, d = "idle", "Не подключено", " "
                if c and c["host"]:
                    d = "Последний раз: %s · %s" % (region_of_host(c["host"]), fmt_ago(c["last"]))
            else:
                level, st_txt = c["level"], c["state"]
                parts = []
                if c["host"]:
                    parts.append(region_of_host(c["host"]))
                if c["ip"]:
                    e = store.entries.get(c["ip"])
                    route = via_text(e.get("check")) if e else ""
                    parts.append(c["ip"] + ((" · " + route) if route else ""))
                d = "  ·  ".join(parts) or " "
                if c["note"]:
                    d += "\n" + c["note"]
                if c["rtt"] is not None and now - c["rtt_ts"] < 30 and level in ("ok", "warn"):
                    ping_txt = "%d мс" % c["rtt"]
            color = LEVEL[level]
            setw(dot, text_color=color)
            setw(icw, text_color=color if level != "idle" else MUTED,
                 fg_color=blend(color, CARD, 0.82) if level != "idle" else CARD2)
            setw(stl, text=st_txt, text_color=TEXT if level in ("idle", "ok") else color)
            setw(det, text=d)
            setw(ping, text=ping_txt)

    # ================================================================ СЕРВЕРЫ
    sr, srv_sub = page_header(pg_srv, "Серверы", "")
    btn_clean = btn(sr, "Очистить ошибки", I_DEL, lambda: clear_errors(), "ghost", width=180)
    btn_clean.pack(side="right")

    sbar = ctk.CTkFrame(pg_srv, fg_color="transparent")
    sbar.pack(fill="x", padx=32, pady=(0, 12))
    var_show = tk.StringVar(value="problem")
    SHOW = {"С проблемами": "problem", "Все серверы": "all"}
    seg_show = seg(sbar, list(SHOW), lambda v: (var_show.set(SHOW[v]), refresh(force=True)), "С проблемами")
    seg_show.pack(side="left")
    # без textvariable — иначе у CTkEntry не показывается подсказка
    sq = ctk.CTkEntry(sbar, placeholder_text="Поиск: IP, регион, сервер…", width=280,
                      height=34, corner_radius=10, fg_color=CARD2, border_color=LINE, border_width=1,
                      text_color=TEXT, placeholder_text_color=DIM, font=F(13))
    sq.pack(side="left", padx=(12, 0))

    class _Q:   # совместимость: var_q.get()
        @staticmethod
        def get():
            return "" if sq._placeholder_text_active else sq.get()
    var_q = _Q()
    sq.bind("<KeyRelease>", lambda e: refresh(force=True))
    lbl_count = ctk.CTkLabel(sbar, text="", font=F(12), text_color=DIM)
    lbl_count.pack(side="right")

    scard = card(pg_srv)
    scard.pack(fill="both", expand=True, padx=32, pady=(0, 8))
    cols = [("addr", "IP-адрес", 130), ("status", "Статус", 130), ("via", "Маршрут", 100),
            ("ping", "Пинг", 80, False, "e"), ("region", "Регион", 110), ("checked", "Проверен", 110),
            ("last_fail", "Последний сбой", 140), ("reason", "Что было", 280, True),
            ("server", "Сервер Discord", 230, True)]
    twrap, tree = make_tree(scard, cols)
    twrap.pack(fill="both", expand=True, padx=12, pady=10)
    empty = ctk.CTkFrame(scard, fg_color=CARD)
    ctk.CTkLabel(empty, text=I_CHECK, font=FI(30), text_color=GREEN).pack()
    ctk.CTkLabel(empty, text="Всё чисто", font=F(18, "bold", DISP), text_color=TEXT).pack(pady=(8, 0))
    ctk.CTkLabel(empty, text="Проблемных серверов нет. Все замеченные — во вкладке «Все серверы».",
                 font=F(13), text_color=MUTED).pack(pady=(4, 0))
    legend = ctk.CTkFrame(pg_srv, fg_color="transparent")
    legend.pack(anchor="w", padx=34, pady=(0, 20))
    for sym, color, txt_ in (("●", RED, "Блок — не отвечает"),
                             ("●", GREEN, "Исправлено — раньше не подключался, теперь работает"),
                             ("○", DIM, "Не отвечает — Discord его выключил, не страшно"),
                             ("", DIM, "Правый клик — действия")):
        if sym:
            ctk.CTkLabel(legend, text=sym, font=F(12), text_color=color).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(legend, text=txt_, font=F(12), text_color=DIM).pack(side="left", padx=(0, 22))

    sort = {"col": None, "rev": False}
    for c in cols:
        tree.heading(c[0], command=lambda cid=c[0]: (
            sort.update(rev=(not sort["rev"]) if sort["col"] == cid else False, col=cid), refresh(force=True)))

    def selected():
        return [tree.item(i, "values")[0] for i in tree.selection()]

    menu = dark_menu()
    menu.add_command(label="  Проверить выделенные", command=lambda: run_check(set(selected()), "выделенные"))
    menu.add_command(label="  Копировать", command=lambda: copy("\n".join(selected()) + "\n", "адреса"))
    menu.add_separator()
    menu.add_command(label="  Очистить ошибки у выделенных", command=lambda: clear_errors(set(selected())))
    menu.add_command(label="  Удалить из списка (сервера больше нет)", command=lambda: remove_selected())
    menu.add_separator()
    menu.add_command(label="  Не включать в маршруты", command=lambda: (store.set_ignored(selected(), True),
                                                                       store.save(), refresh(force=True)))
    menu.add_command(label="  Вернуть в маршруты", command=lambda: (store.set_ignored(selected(), False),
                                                                   store.save(), refresh(force=True)))

    def popup(e):
        row = tree.identify_row(e.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        if tree.selection():
            menu.tk_popup(e.x_root, e.y_root)
    tree.bind("<Button-3>", popup)
    tree.bind("<Control-c>", lambda e: copy("\n".join(selected()) + "\n", "адреса"))
    tree.bind("<Control-a>", lambda e: tree.selection_set(tree.get_children()))
    tree.bind("<Delete>", lambda e: remove_selected())

    def clear_errors(addrs=None):
        if addrs is None and dialog("Очистить все ошибки?",
                                    "Серверы останутся в списке и в маршрутах, журнал очистится.\n"
                                    "Если какой-то сервер действительно не работает — он снова появится "
                                    "при следующем сбое или проверке.",
                                    [("Очистить", True, "primary"), ("Отмена", None, "ghost")],
                                    I_DEL) is not True:
            return
        n = store.forget_problems(addrs, include_blocked=True)
        if addrs is None:
            logbox.configure(state="normal")
            logbox.delete("1.0", "end")
            logbox.configure(state="disabled")
            state["recheck_due"].clear()
        store.save(force=True)
        refresh(force=True)
        toast(("Очищено: %d" % n) if n else "Ошибок не было — очищать нечего", GREEN)

    def remove_selected():
        addrs = selected()
        if not addrs:
            return
        n = store.remove(addrs)
        for a in addrs:
            state["recheck_due"].pop(a, None)
        store.save(force=True)
        refresh(force=True)
        toast("Удалено из списка: %d" % n)

    # ================================================================ МАРШРУТЫ
    rr_, rt_sub = page_header(pg_rt, "Маршруты для Keenetic",
                              "Один файл .bat — загрузи его в Keenetic и выбери VPN-подключение")
    btn(rr_, "Копировать", I_COPY, lambda: copy(store.routes_text(), "маршруты"), "ghost", width=150).pack(
        side="right", padx=(10, 0))
    btn(rr_, "Сохранить файл…", I_SAVE, lambda: save_routes(), "primary", width=190).pack(side="right")

    opts = ctk.CTkFrame(pg_rt, fg_color="transparent")
    opts.pack(fill="x", padx=32)
    opts.grid_columnconfigure((0, 1, 2), weight=1, uniform="o")

    def opt_card(col, title):
        c = card(opts)
        c.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0 if col == 2 else 8))
        ctk.CTkLabel(c, text=title.upper(), font=F(11, "bold"), text_color=DIM).pack(anchor="w", padx=20,
                                                                                    pady=(16, 10))
        return c

    def rt_changed(*_):
        store.settings["route_scope"] = SCOPE[seg_scope.get()]
        store.settings["route_mode"] = MODE[seg_mode.get()]
        store.settings["discord_core"] = bool(var_core.get())
        setw(mode_hint, text=MODE_HINT[store.settings["route_mode"]])
        store.dirty = True
        store.save()
        update_routes()

    SCOPE = {"Все серверы": "all", "Только проблемные": "problem"}
    MODE = {"Умно": "smart", "Точно": "exact"}
    MODE_HINT = {"smart": "Целые блоки Discord с запасом.\nМало строк, новые серверы уже внутри.",
                 "exact": "Только подсети /24 с серверами.\nНичего лишнего, но строк больше."}
    c1 = opt_card(0, "Какие серверы")
    seg_scope = seg(c1, list(SCOPE), rt_changed,
                    "Только проблемные" if store.settings.get("route_scope") == "problem" else "Все серверы")
    seg_scope.pack(anchor="w", padx=20)
    sw_core, var_core = switch(c1, "+ сайт, чат и сети Discord Inc.", bool(store.settings.get("discord_core")),
                               rt_changed)
    sw_core.pack(anchor="w", padx=20, pady=(14, 18))
    c2 = opt_card(1, "Точность")
    seg_mode = seg(c2, list(MODE), rt_changed, "Точно" if store.settings.get("route_mode") == "exact" else "Умно")
    seg_mode.pack(anchor="w", padx=20)
    mode_hint = ctk.CTkLabel(c2, text="", font=F(12), text_color=MUTED, justify="left", anchor="w")
    mode_hint.pack(anchor="w", padx=20, pady=(10, 16))
    setw(mode_hint, text=MODE_HINT["exact" if store.settings.get("route_mode") == "exact" else "smart"])
    c3 = opt_card(2, "Свой список")
    lbl_imp = ctk.CTkLabel(c3, text="", font=F(12), text_color=MUTED, justify="left", anchor="w")
    lbl_imp.pack(anchor="w", padx=20)
    ib = ctk.CTkFrame(c3, fg_color="transparent")
    ib.pack(anchor="w", padx=20, pady=(10, 16))
    btn(ib, "Добавить файл…", I_ADD, lambda: import_file(), "ghost", width=170, height=34).pack(side="left")
    btn_imp_clear = btn(ib, "Убрать", None, lambda: clear_import(), "ghost", width=80, height=34)

    rsum = ctk.CTkFrame(pg_rt, fg_color="transparent")
    rsum.pack(fill="x", padx=34, pady=(20, 10))
    rt_big = ctk.CTkLabel(rsum, text="", font=F(22, "bold", DISP), text_color=TEXT)
    rt_big.pack(side="left")
    rt_small = ctk.CTkLabel(rsum, text="", font=F(13), text_color=MUTED)
    rt_small.pack(side="left", padx=(14, 0), pady=(6, 0))
    rt_warn = ctk.CTkLabel(pg_rt, text="", font=F(12), text_color=AMBER, anchor="w")

    rcard = card(pg_rt)
    rcard.pack(fill="both", expand=True, padx=32, pady=(0, 28))
    rcols = [("net", "Подсеть", 150), ("mask", "Маска", 140), ("size", "Адресов", 100, False, "e"),
             ("cnt", "Серверов", 100, False, "e"), ("what", "Что это", 420, True)]
    rwrap, rtree = make_tree(rcard, rcols)
    rwrap.pack(fill="both", expand=True, padx=12, pady=10)

    def update_routes():
        plan = store.routes_plan()
        rtree.delete(*rtree.get_children())
        for p in plan:
            n = p["net"]
            rtree.insert("", "end", values=(str(n), str(n.netmask), fmt_addrs(n.num_addresses), p["count"] or "—",
                                            p["what"] + ("   ⚠ очень широкая — в VPN пойдёт много чужого"
                                                         if p["wide"] else "")),
                         tags=["wide"] if p["wide"] else [])
        ips_all = store.all_ips(with_dns=False)
        nets = [p["net"] for p in plan]
        cov, miss = core.coverage(nets, ips_all)
        total = sum(n.num_addresses for n in nets)
        setw(rt_big, text="%d %s" % (len(plan), plural(len(plan), "строка", "строки", "строк")))
        setw(rt_small, text="покрыто %d из %d серверов  ·  %s адресов пойдёт через VPN" % (
            cov, len(ips_all), fmt_addrs(total)))
        if miss:
            setw(rt_warn, text="Не покрыто: %s%s" % (", ".join(miss[:6]), " …" if len(miss) > 6 else ""))
            rt_warn.pack(anchor="w", padx=34, pady=(0, 8), before=rcard)
        else:
            rt_warn.pack_forget()
        imp = store.imported_nets()
        if imp:
            icov, _ = core.coverage(imp, ips_all)
            setw(lbl_imp, text="%s · %d %s\nсам по себе покрывает %d из %d" % (
                store.settings.get("import_name", "файл"), len(imp), plural(len(imp), "сеть", "сети", "сетей"),
                icov, len(ips_all)))
            btn_imp_clear.pack(side="left", padx=(8, 0))
        else:
            setw(lbl_imp, text="Можно добавить список из iplist\nили другой .bat — объединю без дублей.")
            btn_imp_clear.pack_forget()
        return len(plan)

    def import_file():
        p = filedialog.askopenfilename(title="Файл со списком маршрутов", initialdir=os.path.expanduser("~/Downloads"),
                                       filetypes=[("Маршруты", "*.bat *.txt *.lst"), ("Все файлы", "*.*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                nets = core.parse_routes_file(f.read())
        except OSError as e:
            dialog("Не удалось прочитать файл", str(e), [("Понятно", None, "primary")], I_WARN, RED)
            return
        if not nets:
            dialog("Маршрутов не найдено", "Ожидаются строки «route add X mask Y …» или CIDR (1.2.3.0/24).",
                   [("Понятно", None, "primary")], I_WARN, AMBER)
            return
        ips_all = store.all_ips(with_dns=False)
        cov, _ = core.coverage(nets, ips_all)
        wide = [n for n in nets if n.num_addresses > core.WIDE_NET]
        msg = "%d %s, всего %s адресов. Покрывает %d из %d замеченных серверов Discord." % (
            len(nets), plural(len(nets), "сеть", "сети", "сетей"), fmt_addrs(sum(n.num_addresses for n in nets)),
            cov, len(ips_all))
        keep = nets
        if wide:
            ans = dialog("Убрать слишком широкие сети?",
                         msg + "\n\nОчень широкие (%d): %s\n\nЧерез VPN по ним пойдёт много чужого трафика "
                         "(Google Cloud, Cloudflare). Нужные серверы Discord из них программа всё равно покроет "
                         "сама точными блоками." % (len(wide), ", ".join(str(n) for n in wide)),
                         [("Убрать широкие", True, "primary"), ("Оставить всё", False, "ghost"),
                          ("Отмена", None, "ghost")], I_WARN, AMBER)
            if ans is None:
                return
            if ans:
                keep = [n for n in nets if n.num_addresses <= core.WIDE_NET]
        store.settings["import_nets"] = [str(n) for n in keep]
        store.settings["import_name"] = os.path.basename(p)
        store.dirty = True
        store.save()
        update_routes()
        toast("Добавлено из файла: %d %s" % (len(keep), plural(len(keep), "сеть", "сети", "сетей")), GREEN)

    def clear_import():
        store.settings["import_nets"] = []
        store.dirty = True
        store.save()
        update_routes()

    def save_routes():
        plan = store.routes_plan()
        if not plan:
            dialog("Пока нечего сохранять", "Серверы Discord ещё не встречались в логах. Зайди в голосовой канал "
                   "и подожди пару минут.", [("Понятно", None, "primary")])
            return
        p = filedialog.asksaveasfilename(title="Куда сохранить маршруты для Keenetic",
                                         initialdir=store.settings.get("last_save_dir") or store.data_dir,
                                         initialfile="discord-routes.bat", defaultextension=".bat",
                                         filetypes=[("Маршруты Keenetic", "*.bat"), ("Все файлы", "*.*")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(core.routes_to_bat(plan))
        except OSError as e:
            dialog("Не удалось сохранить", str(e), [("Понятно", None, "primary")], I_WARN, RED)
            return
        store.settings["last_save_dir"] = os.path.dirname(p)
        store.dirty = True
        store.save()
        if dialog("Сохранено: %d %s" % (len(plan), plural(len(plan), "маршрут", "маршрута", "маршрутов")),
                  "%s\n\nДальше: в веб-интерфейсе Keenetic открой раздел маршрутизации, загрузи этот .bat "
                  "и выбери VPN-подключение." % p,
                  [("Показать в папке", True, "primary"), ("Готово", None, "ghost")], I_CHECK, GREEN):
            open_folder(select=p)

    # ================================================================ ЖУРНАЛ
    lr, _ = page_header(pg_log, "Журнал", "Что происходило с подключениями — сбои, исправления, проверки")

    def clear_log():
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")
    btn(lr, "Очистить журнал", I_DEL, clear_log, "ghost", width=180).pack(side="right")
    lcard = card(pg_log)
    lcard.pack(fill="both", expand=True, padx=32, pady=(0, 28))
    logbox = ctk.CTkTextbox(lcard, fg_color=CARD, text_color=MUTED, font=ctk.CTkFont(family=MONO, size=12),
                            wrap="none", corner_radius=0, scrollbar_button_color=LINE,
                            scrollbar_button_hover_color=DIM, activate_scrollbars=True)
    logbox.pack(fill="both", expand=True, padx=14, pady=12)
    for tag, color in (("fail", RED), ("drop", AMBER), ("fixed", GREEN), ("info", MUTED)):
        logbox.tag_config(tag, foreground=color)
    logbox.configure(state="disabled")

    def log(s, level="info"):
        logbox.configure(state="normal")
        logbox.insert("end", s + "\n", level)
        if int(logbox.index("end-1c").split(".")[0]) > 3000:
            logbox.delete("1.0", "1000.0")
        logbox.configure(state="disabled")
        logbox.see("end")

    # ================================================================ НАСТРОЙКИ
    page_header(pg_set, "Настройки", "Изменения применяются сразу")
    sc = card(pg_set)
    sc.pack(fill="x", padx=32)

    def set_row(parent, title, desc, first=False):
        if not first:
            ctk.CTkFrame(parent, fg_color=LINE, height=1, corner_radius=0).pack(fill="x", padx=20)
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=22, pady=14)
        tx = ctk.CTkFrame(r, fg_color="transparent")
        tx.pack(side="left")
        ctk.CTkLabel(tx, text=title, font=F(14), text_color=TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(tx, text=desc, font=F(12), text_color=DIM, anchor="w").pack(anchor="w")
        right = ctk.CTkFrame(r, fg_color="transparent")
        right.pack(side="right")
        return right

    def save_setting(key, value):
        store.settings[key] = value
        store.dirty = True
        store.save()

    rr1 = set_row(sc, "Автопроверка серверов", "Как часто проверять, отвечают ли все замеченные серверы", True)
    cur_auto = store.settings.get("auto_check_min", 10)

    def on_auto(v):
        save_setting("auto_check_min", dict(AUTO_CHOICES)[v])
        schedule_auto()
        refresh(force=True)
    seg(rr1, [n for n, _ in AUTO_CHOICES], on_auto,
        next((n for n, v in AUTO_CHOICES if v == cur_auto), "10 мин")).pack()

    rr2 = set_row(sc, "Уведомления", "Всплывающее сообщение, когда сервер перестал или начал отвечать")
    sw_n, v_n = switch(rr2, "", bool(store.settings.get("notify", True)),
                       lambda: save_setting("notify", bool(v_n.get())))
    sw_n.pack()

    rr3 = set_row(sc, "Запускать вместе с Windows", "Тихо, свернувшись в трей")

    def on_start():
        try:
            autostart_set(bool(v_s.get()))
        except Exception as e:
            dialog("Не удалось изменить автозапуск", str(e), [("Понятно", None, "primary")], I_WARN, RED)
    sw_s, v_s = switch(rr3, "", autostart_get(), on_start)
    sw_s.pack()
    if not getattr(sys, "frozen", False):
        sw_s.configure(state="disabled")

    rr4 = set_row(sc, "Обрывы — тоже проблема", "Считать проблемой разрывы уже установленной связи")

    def on_drops():
        store.include_drops = bool(v_d.get())
        store.dirty = True
        store.save()
        refresh(force=True)
    sw_d, v_d = switch(rr4, "", store.include_drops, on_drops)
    sw_d.pack()

    sc2 = card(pg_set)
    sc2.pack(fill="x", padx=32, pady=(16, 0))
    rr5 = set_row(sc2, "Папка с результатами", store.data_dir, True)
    btn(rr5, "Открыть", I_FOLDER, open_folder, "ghost", width=130, height=34).pack()
    rr6 = set_row(sc2, "Очистить историю", "Забыть все собранные серверы. Список начнёт собираться заново")

    def clear_all():
        if dialog("Очистить всю историю?", "Все собранные серверы и статусы будут забыты. "
                  "Новые появятся сами, когда Discord снова ими воспользуется.",
                  [("Очистить", True, "primary"), ("Отмена", None, "ghost")], I_WARN, RED):
            store.clear()
            store.save(force=True)
            refresh(force=True)
            toast("История очищена")
    btn(rr6, "Очистить", None, clear_all, "danger", width=130, height=34).pack()

    # ================================================================ проверки
    def run_check(scope, label, quiet=False):
        def done(n_ok, n_fail, total):
            events.put((None, None, "checkdone", None, (label, n_ok, n_fail, total, quiet)))
        if checker.start(scope, label, done):
            btn_check.configure(state="disabled")
            hero_prog.pack(anchor="w", fill="x", pady=(12, 0))
            return True
        if not quiet:
            toast("Проверка уже идёт — пару секунд")
        return False

    def schedule_auto():
        m = store.settings.get("auto_check_min", 10)
        state["auto_next"] = time.time() + m * 60 if m else None

    # ================================================================ таблица серверов
    def main_reason(e):
        if not e["reasons"]:
            st = store.status(e)
            if st == "silent":
                return "не отвечает на проверку — похоже, Discord его выключил"
            if st == "blocked":
                return "не отвечает на проверку 2 раза подряд"
            return ""
        r = max(e["reasons"].items(), key=lambda kv: kv[1])[0]
        return r + ("  (+%d)" % (len(e["reasons"]) - 1) if len(e["reasons"]) > 1 else "")

    def build_rows():
        rows = []
        q = var_q.get().strip().lower()
        show = var_show.get()
        with store.lock:
            items = [(a, e) for a, e in store.entries.items() if e["type"] == "ip"]
        for a, e in items:
            st = store.status(e)
            if show == "problem" and not (store.is_problem(e) or st == "blocked"):
                continue
            c = e.get("check")
            if c and c.get("ok"):
                ping = "%s мс" % c["rtt"]
            elif c and c["ok"] is False:
                ping = "нет ответа"
            else:
                ping = ""
            server = e["related"][-1] if e["related"] else ("тест регионов" if "probe" in e.get("sources", []) else "")
            values = (a, STATUS_UI[st][0] + ("  ⊘" if e.get("ignored") else ""), via_text(c), ping,
                      e.get("region", ""), fmt_ago(c["ts"]) if c else "—", fmt_ts(e.get("last_hard")) or "",
                      main_reason(e), server)
            if q and q not in " ".join(str(v) for v in values).lower():
                continue
            keys = {"addr": ip_key(a), "status": STATUS_ORDER.index(st), "ping": (c or {}).get("rtt") or 99999,
                    "last_fail": e.get("last_hard") or 0, "checked": -(c["ts"] if c else 0)}
            rows.append((a, values, ["ignored"] if e.get("ignored") else [st],
                         dict(keys, _default=(STATUS_ORDER.index(st), ip_key(a)))))
        rows.sort(key=lambda r: r[3]["_default"])
        if sort["col"]:
            colnames = [c[0] for c in cols]
            rows.sort(key=lambda r: r[3].get(sort["col"], str(r[1][colnames.index(sort["col"])])),
                      reverse=sort["rev"])
        return rows

    def sync_tree(rows):
        existing = set(tree.get_children())
        for iid in existing - {r[0] for r in rows}:
            tree.delete(iid)
        for idx, (iid, values, tags, _) in enumerate(rows):
            if iid in existing:
                tree.item(iid, values=values, tags=tags)
                if tree.index(iid) != idx:
                    tree.move(iid, "", idx)
            else:
                tree.insert("", idx, iid=iid, values=values, tags=tags)

    def refresh(force=False):
        sig = (len(store.entries), sum(e["fails"] + e["oks"] + e.get("check_ok", 0) + e.get("check_fails", 0)
                                       for e in list(store.entries.values())), int(time.time() // 30))
        if not force and sig == state["sig"]:
            return
        state["sig"] = sig
        rows = build_rows()
        sync_tree(rows)
        cnt = store.counts()
        blocked, fixed, total = cnt[("ip", "blocked")], cnt[("ip", "fixed")], cnt[("ip", "all")]
        if not rows and var_show.get() == "problem" and not var_q.get().strip():
            empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            empty.place_forget()
        setw(lbl_count, text="показано %d из %d" % (len(rows), total))
        setw(srv_sub, text="%d %s замечено в логах Discord на этом ПК" % (
            total, plural(total, "сервер", "сервера", "серверов")))
        btn_clean.configure(state="normal" if (blocked or fixed or cnt[("ip", "problem")] or cnt[("ip", "silent")])
                            else "disabled")
        bdg = nav["srv"][3]
        if blocked:
            setw(bdg, text=str(blocked))
            bdg.place(relx=1.0, x=-12, rely=0.5, anchor="e")
            bdg.lift()
        else:
            bdg.place_forget()

        n_routes = update_routes()
        for key, val, color in (("total", total, TEXT), ("blocked", blocked, RED if blocked else TEXT),
                                ("fixed", fixed, GREEN if fixed else TEXT), ("routes", n_routes, TEXT)):
            setw(tile_v[key], text=str(val), text_color=color)

        if blocked:
            draw_ring(RED, I_WARN)
            title = "Не %s %d %s" % ("отвечает" if blocked == 1 else "отвечают", blocked,
                                     plural(blocked, "сервер", "сервера", "серверов"))
            tcolor = RED
        elif checker.last_run or fixed:
            draw_ring(GREEN, I_CHECK)
            title, tcolor = "Всё работает", TEXT
        else:
            draw_ring(ACCENT, I_INFO)
            title, tcolor = "Ещё не проверялось", TEXT
        setw(hero_title, text=title, text_color=tcolor)
        nxt = state["auto_next"]
        if checker.busy.is_set():
            sub = hero_sub.cget("text")
        else:
            sub = "%d %s  ·  проверено %s%s" % (
                total, plural(total, "сервер", "сервера", "серверов"),
                fmt_ago(checker.last_run) if checker.last_run else "ещё нет",
                ("  ·  следующая через %d мин" % max(1, int((nxt - time.time()) // 60) + 1)) if nxt else "")
            if fixed:
                sub += "\n%d %s после сбоя — теперь в порядке" % (fixed, plural(fixed, "сервер", "сервера",
                                                                                "серверов"))
        setw(hero_sub, text=sub)
        setw(net_isp, text=store.net.get("direct_ip") or "—")
        setw(net_vpn, text=", ".join(store.net.get("vpn_ips", [])[-1:]) or "—")

        level = "fail" if blocked else ("ok" if fixed or checker.last_run else "idle")
        if state["tray"] is not None and (blocked, level) != state["icon"]:
            try:
                state["tray"].icon = make_icon_image(blocked, level)
                state["tray"].title = ("%s: не отвечает %d, исправлено %d" % (APP_NAME, blocked, fixed))[:120]
            except Exception:
                pass
        state["icon"] = (blocked, level)

    # ================================================================ цикл событий
    pending_resolve = set()

    def pump():
        changed = False
        now = time.time()
        while True:
            try:
                text, new_host, level, addr, extra = events.get_nowait()
            except queue.Empty:
                break
            changed = True
            if level == "checkdone":
                label, n_ok, n_fail, total, quiet = extra
                btn_check.configure(state="normal")
                hero_prog.pack_forget()
                if not quiet:
                    toast("Проверено %d: отвечают %d, не отвечают %d" % (total, n_ok, n_fail),
                          RED if n_fail else GREEN)
                continue
            if text:
                log(text, level)
                if level == "fail":
                    notify("Discord: сервер не отвечает", text[2:].strip())
                elif level == "fixed":
                    notify("Discord: заработало", text[2:].strip())
            if new_host:
                pending_resolve.add(new_host)
            if level == "fail" and addr and not extra and tailer.initial_done.is_set():
                state["recheck_due"][addr] = now + RECHECK_AFTER_FAIL
        if pending_resolve and tailer.initial_done.is_set():
            hosts = list(pending_resolve)
            pending_resolve.clear()

            def work():
                resolve_hosts(store, hosts=hosts)
                store.save()
            threading.Thread(target=work, daemon=True).start()
        if tailer.initial_done.is_set() and not state["notify"]:
            state["notify"] = True
            log("— история логов разобрана, дальше слежу в реальном времени —")
            changed = True
        if tailer.initial_done.is_set() and not checker.busy.is_set():
            due = [a for a, t in state["recheck_due"].items() if t <= now]
            if due:
                if run_check(set(due), "после сбоя", quiet=True):
                    for a in due:
                        state["recheck_due"].pop(a, None)
            elif not state["startup_check"]:
                state["startup_check"] = True
                if store.settings.get("auto_check_min", 10):
                    run_check("all", "при запуске", quiet=True)
                schedule_auto()
            elif state["auto_next"] and now >= state["auto_next"]:
                run_check("all", "автопроверка", quiet=True)
                schedule_auto()
        if checker.busy.is_set():
            i, n, label = checker.progress
            hero_prog.set(i / n if n else 0)
            setw(hero_sub, text=("Проверяю серверы: %d из %d…" % (i, n)) if n else "Определяю IP провайдера…")
            changed = True
        refresh(force=changed)
        update_live()
        nfiles = len(tailer.files_list)
        if not tailer.initial_done.is_set():
            setw(foot_dot, text_color=AMBER)
            setw(foot_lbl, text="Читаю историю логов…")
        elif nfiles:
            setw(foot_dot, text_color=GREEN)
            setw(foot_lbl, text="Слежу за Discord")
        else:
            setw(foot_dot, text_color=RED)
            setw(foot_lbl, text="Логи Discord не найдены")
        root.after(700, pump)

    # ================================================================ трей
    def show_window():
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def quit_app():
        def _q():
            tailer.stop_ev.set()
            store.save(force=True)
            if state["tray"] is not None:
                try:
                    state["tray"].stop()
                except Exception:
                    pass
            root.destroy()
        root.after(0, _q)

    try:
        import pystray
        tray = pystray.Icon(
            "DiscordVoiceCatcher", make_icon_image(0), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("Показать", lambda i, it: show_window(), default=True),
                pystray.MenuItem("Проверить серверы", lambda i, it: root.after(0, lambda: run_check("all", "все"))),
                pystray.MenuItem("Сохранить маршруты для Keenetic…",
                                 lambda i, it: root.after(0, lambda: (show_window(), save_routes()))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda i, it: quit_app()),
            ))
        tray.run_detached()
        state["tray"] = tray
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
    except Exception as e:
        root.protocol("WM_DELETE_WINDOW", quit_app)
        log("Трей недоступен: %r" % e)

    log("Папка с результатами: %s" % store.data_dir)
    if args.minimized and state["tray"] is not None:
        root.withdraw()
    tailer.start()
    show_page(getattr(args, "page", None) or "home")
    refresh(force=True)
    root.after(300, pump)
    root.mainloop()
    tailer.stop_ev.set()
    store.save(force=True)

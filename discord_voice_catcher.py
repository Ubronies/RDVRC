# -*- coding: utf-8 -*-
"""
Discord Voice Catcher — следит за логами Discord на Windows, показывает в реальном
времени состояние голоса/трансляции, собирает адреса голосовых серверов и сам
перепроверяет их доступность. Формирует файлы для Keenetic: список доменов (DNS)
и .bat маршрутов (route add X mask Y 0.0.0.0).

Источники (логи):
  %APPDATA%\\discord*\\logs\\renderer_js.log(.old.log)
      [RTCControlSocket(ctx)] [CONNECT] wss://c-xxx.discord.media:PORT/   - WebSocket к RTC-серверу
      [RTCControlSocket(ctx)] [CONNECTED] wss://...                        - успех
      [RTCControlSocket(ctx)] [WS CLOSED] (clean: false, ... reason: ...)  - сбой
      [RTCControlSocket(ctx)] [HEARTBEAT ACK TIMEOUT]                      - обрыв
      [Connection(ctx)] Creating connection to IP:PORT                     - UDP медиа-сервер
      [RTCConnection(...)] RTC connected to media server: IP:PORT          - UDP успех
  %APPDATA%\\discord*\\logs\\discord-webrtc_*, discord-last-webrtc_*
      Ping timeout from: IP:PORT / Ping received from IP:PORT ... rtt: N   - UDP пинги
      Sending N latency test pings to region X / ... to IP IP:PORT         - серверы регионов
  ctx: default = голосовой канал / звонок, stream = трансляция (Go Live)

Активная проверка:
  IP  — UDP-пакет Discord «IP discovery» (74 байта, type=1). Сервер отвечает без входа в канал
        и сообщает, с какого внешнего IP он нас видит -> понятно, идёт ли трафик через VPN.
  домен — TLS-рукопожатие с SNI на host:port.
  «прямой» IP провайдера — STUN (sipnet.ru, nextcloud, miwifi, google), большинство ответов.

Аргументы: --scan [--check] (консольный режим), --logdir DIR, --data DIR, --minimized, --drops
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import glob
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time

APP_NAME = "Discord Voice Catcher"
APP_VERSION = "1.0"

HOST_TIMEOUT = 25.0         # сек: WebSocket CONNECT без CONNECTED
MEDIA_TIMEOUT = 15.0        # сек: Creating connection без "RTC connected"
PING_TIMEOUTS_FOR_FAIL = 2  # подряд идущих "Ping timeout" = UDP перестал отвечать
LIVE_PENDING_MAX_AGE = 600  # не завершать по часам ожидания старше 10 мин
LIVE_IDLE_AFTER = 45        # сек без heartbeat — считаем, что соединения нет
RECHECK_AFTER_FAIL = 90     # сек: автоперепроверка адреса после сбоя в логе
STALE_SERVER = 24 * 3600    # сек: сервер, через который не звонили сутки, при неответе — «Не отвечает», не «Блок»

TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?\]")
R_CONNECT = re.compile(r"\[RTCControlSocket\((\w+)\)\] \[CONNECT\] wss://([A-Za-z0-9.-]+):(\d+)")
R_CONNECTED = re.compile(r"\[RTCControlSocket\((\w+)\)\] \[CONNECTED\] wss://([A-Za-z0-9.-]+)")
R_WSCLOSED = re.compile(r"\[RTCControlSocket\((\w+)\)\] \[WS CLOSED\] \(clean: (true|false), code: (\d+), reason: ([^)]*)\)")
R_HB_TIMEOUT = re.compile(r"\[RTCControlSocket\((\w+)\)\] \[HEARTBEAT ACK TIMEOUT\]")
R_HB_ACK = re.compile(r"\[RTCControlSocket\((\w+)\)\] Heartbeat ACK received")
R_CLOSE = re.compile(r"\[RTCControlSocket\((\w+)\)\] CLOSE\s*$")
R_MEDIA_CREATE = re.compile(r"\[Connection\((\w+)\)\] Creating connection to (\d+\.\d+\.\d+\.\d+):(\d+)")
R_MEDIA_OK = re.compile(r"\[RTCConnection\([^,]+, (\w+)\)\] RTC connected to media server: (\d+\.\d+\.\d+\.\d+):(\d+)")
R_RTC_STATE = re.compile(r"\[RTCConnection\([^,]+, (\w+)\)\] RTC connection state: (\w+) => (\w+)")
R_DESTROY = re.compile(r"\[RTCConnection\([^,]+, (\w+)\)\] Destroy RTCConnection")
R_PING_TIMEOUT = re.compile(r"Ping timeout from: (\d+\.\d+\.\d+\.\d+):(\d+)")
R_PING_OK = re.compile(r"Ping received from (\d+\.\d+\.\d+\.\d+):(\d+), seq: \d+, rtt: (\d+)")
R_REGION = re.compile(r"latency test pings to region ([\w-]+)")
R_REGION_IP = re.compile(r"latency test ping to IP (\d+\.\d+\.\d+\.\d+):(\d+)")

QUICK_FILTER = ("RTCControlSocket", "Creating connection to", "RTC connected to media",
                "RTC connection state", "Destroy RTCConnection", "Ping timeout", "Ping received",
                "latency test ping")

CTX_NAMES = {"default": "Голос", "stream": "Трансляция"}

REGION_NAMES = {
    "waw": "Варшава", "ams": "Амстердам", "fra": "Франкфурт", "otp": "Бухарест", "hel": "Хельсинки",
    "bru": "Брюссель", "ord": "Чикаго", "gru": "Сан-Паулу", "sin": "Сингапур", "lhr": "Лондон",
    "cdg": "Париж", "arn": "Стокгольм", "cph": "Копенгаген", "mad": "Мадрид", "mil": "Милан",
    "mxp": "Милан", "ist": "Стамбул", "iad": "Вашингтон", "atl": "Атланта", "sea": "Сиэтл",
    "lax": "Лос-Анджелес", "sjc": "Сан-Хосе", "dfw": "Даллас", "mia": "Майами", "hkg": "Гонконг",
    "nrt": "Токио", "syd": "Сидней", "bom": "Мумбаи", "dxb": "Дубай", "jnb": "Йоханнесбург",
    "tlv": "Тель-Авив", "vie": "Вена", "zrh": "Цюрих", "prg": "Прага", "kbp": "Киев",
    "finland": "Финляндия", "helsinki": "Хельсинки", "stockholm": "Стокгольм", "denmark": "Дания",
    "warsaw": "Варшава", "rotterdam": "Роттердам", "brussels": "Брюссель", "london": "Лондон",
    "frankfurt": "Франкфурт", "paris": "Париж", "madrid": "Мадрид", "milan": "Милан",
    "bucharest": "Бухарест", "russia": "Россия", "us-south": "США юг", "us-east": "США восток",
    "us-west": "США запад", "us-central": "США центр", "singapore": "Сингапур", "japan": "Япония",
    "hongkong": "Гонконг", "india": "Индия", "brazil": "Бразилия", "sydney": "Сидней",
    "southafrica": "ЮАР", "dubai": "Дубай", "santiago": "Сантьяго", "atlanta": "Атланта",
    "seattle": "Сиэтл", "newark": "Ньюарк", "amsterdam": "Амстердам", "europe": "Европа",
}


def region_of_host(host):
    h = host.split(".")[0].lower()
    m = re.match(r"c-([a-z]+)\d*-", h)
    code = m.group(1) if m else re.sub(r"\d+$", "", h)
    return REGION_NAMES.get(code, code)


def region_name(code):
    return REGION_NAMES.get(code.lower(), code)


def parse_ts(line):
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    ms = int((m.group(2) or "0").ljust(3, "0"))
    return t.timestamp() + ms / 1000.0


def fmt_ts(t):
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def fmt_ago(t):
    if not t:
        return ""
    s = int(time.time() - t)
    if s < 60:
        return "только что"
    if s < 3600:
        return "%d мин назад" % (s // 60)
    if s < 86400:
        return "%d ч назад" % (s // 3600)
    return "%d дн назад" % (s // 86400)


def ip_key(s):
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (999,)


def is_public_ipv4(ip):
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                or a.is_reserved or a.is_unspecified)


def subnet24(ip):
    return str(ipaddress.IPv4Network(ip + "/24", strict=False))


def aggregate(ips):
    """Укрупняет адреса: в каждой /16 — одна наименьшая подсеть, покрывающая все замеченные /24."""
    groups = collections.defaultdict(list)
    for ip in ips:
        a, b, c, _ = ip.split(".")
        groups[(int(a), int(b))].append(int(c))
    nets = []
    for (a, b), cs in groups.items():
        lo, hi = min(cs), max(cs)
        net = ipaddress.IPv4Network("%d.%d.%d.0/24" % (a, b, lo))
        last = ipaddress.IPv4Address("%d.%d.%d.0" % (a, b, hi))
        while last not in net:
            net = net.supernet()
        nets.append(net)
    return sorted(nets)


# Блоки, проверенные по RIPE (BGP-анонсы и владельцы) 06.11.2026:
#  104.29.126–191.x анонсируются Cloudflare (AS13335) кусками /24 — здесь голос Discord;
#  104.29.0–125 не анонсируются, 104.29.192.0/20 — отдельный чужой блок -> /18 не задевает соседей.
#  35.217.0.0/18 и 34.0.128.0/19 — анонсы Google Cloud (Финляндия / США).
KNOWN_BLOCKS = [
    (ipaddress.IPv4Network("104.29.128.0/18"), "голос Discord (Cloudflare)"),
    (ipaddress.IPv4Network("35.217.0.0/18"), "голос Discord, Финляндия (Google Cloud)"),
    (ipaddress.IPv4Network("34.0.128.0/19"), "голос Discord, США (Google Cloud)"),
]
# discord.com, gateway.discord.gg, cdn.discordapp.com, *.discord.media — все в 162.159.128–138.x
DISCORD_CORE = (ipaddress.IPv4Network("162.159.128.0/20"), "сайт, чат и CDN Discord (Cloudflare)")
# Собственные сети Discord Inc. (AS49544) — чужих сервисов там нет
DISCORD_EXTRA = [
    DISCORD_CORE,
    (ipaddress.IPv4Network("66.22.192.0/18"), "собственная сеть Discord Inc."),
    (ipaddress.IPv4Network("138.128.136.0/21"), "собственная сеть Discord Inc."),
]
WIDE_NET = 65536   # больше — предупреждаем: «через VPN пойдёт много чужого»


def parse_routes_file(text):
    """Читает .bat (route add X mask Y …), CIDR-список или IP-список. Возвращает список IPv4Network."""
    nets = []
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"route\s+add\s+(\d+\.\d+\.\d+\.\d+)\s+mask\s+(\d+\.\d+\.\d+\.\d+)", line, re.I)
        try:
            if m:
                nets.append(ipaddress.IPv4Network("%s/%s" % m.groups(), strict=False))
            elif re.fullmatch(r"\d+\.\d+\.\d+\.\d+(/\d+)?", line):
                nets.append(ipaddress.IPv4Network(line, strict=False))
        except ValueError:
            pass
    return sorted(set(nets))
ROUTE_MODES = [
    ("smart", "Умно — целые блоки Discord с запасом, не задевая соседей (рекомендуется)"),
    ("exact", "Точно — только подсети /24, где замечены серверы (много строк)"),
]
CLUSTER_GAP = 16   # /24 подряд без серверов — разрыв, дальше отдельная подсеть


def plan_routes(ips, mode="smart", core=False, imported=None):
    """Возвращает список маршрутов: dict(net, what, count) — без вложенных друг в друга сетей.
    imported — сети из внешнего файла (iplist): берутся как есть, наши внутри них не дублируются."""
    plan = {}

    def add(net, what, ip=None):
        p = plan.setdefault(net, {"net": net, "what": what, "ips": set()})
        if ip:
            p["ips"].add(ip)

    for n in imported or []:
        add(n, "из импортированного файла")
    extra = DISCORD_EXTRA if core else []
    for n, w in extra:
        add(n, w)
    rest = []
    for ip in ips:
        a = ipaddress.IPv4Address(ip)
        holder = next((n for n in (imported or []) if a in n), None) or \
            next((n for n, _ in extra if a in n), None)
        if holder:
            add(holder, plan[holder]["what"], ip)
            continue
        if mode != "exact":
            kb = next(((n, w) for n, w in KNOWN_BLOCKS if a in n), None)
            if kb:
                add(*kb, ip=ip)
                continue
        rest.append(ip)
    if mode == "exact":
        for ip in rest:
            add(ipaddress.IPv4Network(ip + "/24", strict=False), "замеченные серверы", ip)
    else:
        groups = collections.defaultdict(lambda: collections.defaultdict(list))
        for ip in rest:
            a, b, c, _ = (int(x) for x in ip.split("."))
            groups[(a, b)][c].append(ip)
        for (a, b), by_c in groups.items():
            cs = sorted(by_c)
            clusters, cur = [], [cs[0]]
            for c in cs[1:]:
                if c - cur[-1] > CLUSTER_GAP:
                    clusters.append(cur)
                    cur = []
                cur.append(c)
            clusters.append(cur)
            for cl in clusters:
                net = ipaddress.IPv4Network("%d.%d.%d.0/24" % (a, b, cl[0]))
                last = ipaddress.IPv4Address("%d.%d.%d.0" % (a, b, cl[-1]))
                while last not in net:
                    net = net.supernet()
                if mode == "margin" and net.prefixlen > 16:
                    net = net.supernet(prefixlen_diff=min(2, net.prefixlen - 16))
                what = "укрупнено из %d подсетей /24" % len(cl) if len(cl) > 1 else "замеченные серверы"
                for c in cl:
                    for ip in by_c[c]:
                        add(net, what, ip)
    nets = sorted(plan)
    out = []
    for n in nets:
        if any(o != n and n.subnet_of(o) for o in nets):
            continue
        p = plan[n]
        cnt = len(p["ips"] | set().union(*[plan[m]["ips"] for m in nets if m != n and m.subnet_of(n)]))
        out.append({"net": n, "what": p["what"], "count": cnt, "wide": n.num_addresses > WIDE_NET})
    return out


def coverage(nets, ips):
    """Сколько из ips попадает в nets. Возвращает (покрыто, список непокрытых)."""
    miss = [ip for ip in ips if not any(ipaddress.IPv4Address(ip) in n for n in nets)]
    return len(ips) - len(miss), miss


def routes_to_bat(plan):
    lines = ["route add %s mask %s 0.0.0.0" % (p["net"].network_address, p["net"].netmask) for p in plan]
    return "\r\n".join(lines) + ("\r\n" if lines else "")


# ----------------------------------------------------------------------------
# Активные проверки
# ----------------------------------------------------------------------------

def discord_probe(ip, port, timeout=1.5, tries=2):
    """UDP IP discovery к голосовому серверу Discord. Возвращает dict(ok, rtt, ext_ip, err)."""
    port = int(port)
    for _ in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(timeout)
            ssrc = struct.unpack(">I", os.urandom(4))[0]
            pkt = struct.pack(">HHI", 1, 70, ssrc) + b"\0" * 64 + b"\0\0"
            t0 = time.perf_counter()
            s.sendto(pkt, (ip, port))
            deadline = t0 + timeout
            while True:
                left = deadline - time.perf_counter()
                if left <= 0:
                    break
                s.settimeout(left)
                data, addr = s.recvfrom(2048)
                if addr[0] == ip and len(data) >= 72 and data[:2] == b"\x00\x02":
                    rtt = int((time.perf_counter() - t0) * 1000)
                    ext = data[8:72].split(b"\0")[0].decode("ascii", "replace")
                    return {"ok": True, "rtt": rtt, "ext_ip": ext if is_public_ipv4(ext) else "", "err": ""}
        except socket.timeout:
            pass
        except OSError as e:
            return {"ok": False, "rtt": None, "ext_ip": "", "err": "ошибка сети: %s" % e.strerror}
        finally:
            s.close()
    return {"ok": False, "rtt": None, "ext_ip": "", "err": "нет ответа по UDP"}


def tls_probe(host, port=443, timeout=5):
    """TLS-рукопожатие с SNI. ok=None — домен больше не существует (не резолвится)."""
    try:
        infos = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return {"ok": None, "rtt": None, "ext_ip": "", "err": "домен не резолвится (сервер удалён)"}
    ip = infos[0][4][0]
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as raw:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(raw, server_hostname=host):
                pass
        return {"ok": True, "rtt": int((time.perf_counter() - t0) * 1000), "ext_ip": "", "err": "", "ip": ip}
    except ssl.SSLCertVerificationError:
        return {"ok": False, "rtt": None, "ext_ip": "", "err": "подменён сертификат (DPI?)", "ip": ip}
    except socket.timeout:
        return {"ok": False, "rtt": None, "ext_ip": "", "err": "таймаут TCP/TLS", "ip": ip}
    except (ConnectionResetError, ConnectionAbortedError):
        return {"ok": False, "rtt": None, "ext_ip": "", "err": "соединение сброшено (DPI?)", "ip": ip}
    except OSError as e:
        return {"ok": False, "rtt": None, "ext_ip": "", "err": "ошибка: %s" % (e.strerror or e), "ip": ip}


STUN_SERVERS = [("stun.sipnet.ru", 3478), ("stun.nextcloud.com", 3478), ("stun.miwifi.com", 3478),
                ("stun.l.google.com", 19302), ("stun1.l.google.com", 19302)]


def stun_query(host, port, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        tid = os.urandom(12)
        s.sendto(struct.pack(">HHI", 1, 0, 0x2112A442) + tid, (socket.gethostbyname(host), port))
        d, _ = s.recvfrom(2048)
        i, mapped = 20, None
        while i + 4 <= len(d):
            t, ln = struct.unpack(">HH", d[i:i + 4])
            v = d[i + 4:i + 4 + ln]
            if t == 0x0020 and len(v) >= 8:
                return socket.inet_ntoa(struct.pack(">I", struct.unpack(">I", v[4:8])[0] ^ 0x2112A442))
            if t == 0x0001 and len(v) >= 8:
                mapped = socket.inet_ntoa(v[4:8])
            i += 4 + ln + ((4 - ln % 4) % 4)
        return mapped
    except OSError:
        return None
    finally:
        s.close()


def detect_direct_ip():
    """IP провайдера (как нас видят обычные, не завёрнутые в VPN серверы)."""
    with concurrent.futures.ThreadPoolExecutor(len(STUN_SERVERS)) as ex:
        res = [r for r in ex.map(lambda hp: stun_query(*hp), STUN_SERVERS) if r]
    if not res:
        return None
    return collections.Counter(res).most_common(1)[0][0]


# ----------------------------------------------------------------------------
# Поиск логов
# ----------------------------------------------------------------------------

DISCORD_DIRS = ("discord", "discordptb", "discordcanary", "discorddevelopment")


def discord_roots(extra=None):
    roots = []
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    for name in DISCORD_DIRS:
        for base in (appdata, local):
            if not base:
                continue
            cand = os.path.join(base, name)
            if os.path.isdir(cand) and os.path.normcase(cand) not in [os.path.normcase(r) for r in roots]:
                roots.append(cand)
    for e in extra or []:
        if os.path.isdir(e):
            roots.append(e)
    return roots


def find_log_files(extra=None):
    """Возвращает список (path, kind), kind = 'renderer' | 'webrtc'."""
    found = {}
    for root in discord_roots(extra):
        pats = [
            ("renderer", os.path.join(root, "logs", "renderer_js*.log")),
            ("renderer", os.path.join(root, "renderer_js*.log")),
            ("webrtc", os.path.join(root, "logs", "discord-webrtc*")),
            ("webrtc", os.path.join(root, "logs", "discord-last-webrtc*")),
            ("webrtc", os.path.join(root, "discord-webrtc*")),
            ("webrtc", os.path.join(root, "discord-last-webrtc*")),
            ("webrtc", os.path.join(root, "*", "modules", "discord_voice*", "discord-webrtc*")),
            ("webrtc", os.path.join(root, "*", "modules", "discord_voice*", "discord-last-webrtc*")),
            ("webrtc", os.path.join(root, "*", "modules", "discord_voice*", "*.log")),
        ]
        for kind, pat in pats:
            for p in glob.glob(pat):
                if os.path.isfile(p):
                    found[os.path.normcase(os.path.abspath(p))] = (os.path.abspath(p), kind)
    items = list(found.values())
    items.sort(key=lambda x: (os.path.getmtime(x[0]) if os.path.exists(x[0]) else 0))
    return items


# ----------------------------------------------------------------------------
# Хранилище
# ----------------------------------------------------------------------------

DEFAULT_SETTINGS = {"include_drops": False, "notify": True, "cf_dns": False, "auto_check_min": 10,
                    "discord_core": False, "route_scope": "all", "route_mode": "smart", "settings_v": 3}
DISCORD_CORE_NET = DISCORD_CORE[0]
LEGACY_OUTPUTS = ("subnets.txt", "keenetic-routes.bat", "keenetic-routes-24.bat", "keenetic-routes-auto.bat",
                  "blocked-now-ips.txt", "all-subnets.txt", "keenetic-routes-all-24.bat",
                  "keenetic-routes-all-auto.bat")

STATUS_ORDER = ["blocked", "fixed", "drops", "ok", "silent", "seen", "gone"]
STATUS_RU = {"blocked": "⛔ Блок", "fixed": "✅ Исправлено", "drops": "⚠ Обрывы", "ok": "🟢 Работает",
             "silent": "▫ Не отвечает", "seen": "⚪ Не проверен", "gone": "▫ Удалён"}


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.RLock()
        self.seen = set()
        self.entries = {}      # addr -> dict
        self.dns = {}          # host -> {"ips": [...], "at": ts}
        self.net = {"direct_ip": None, "vpn_ips": [], "at": None}
        self.settings = dict(DEFAULT_SETTINGS)
        self.dirty = False
        self.load()

    @property
    def include_drops(self):
        return bool(self.settings.get("include_drops"))

    @include_drops.setter
    def include_drops(self, v):
        self.settings["include_drops"] = bool(v)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.seen = set(d.get("seen", []))
            self.entries = d.get("entries", {})
            self.dns = d.get("dns", {})
            self.net.update(d.get("net", {}))
            st = d.get("settings", {})
            if st.get("settings_v", 0) < 3:
                st.pop("cf_dns", None)   # раньше по умолчанию было «вкл» — теперь «выкл»
                st["settings_v"] = 3
            self.settings.update(st)
            if "include_drops" in d:
                self.settings["include_drops"] = bool(d["include_drops"])
            self._migrate()
        except FileNotFoundError:
            pass
        except Exception:
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass

    def _migrate(self):
        for a, e in self.entries.items():
            e.setdefault("drops", 0)
            e.setdefault("sources", [])
            e.setdefault("region", "")
            e.setdefault("ignored", False)
            e.setdefault("check", None)
            e.setdefault("check_ok", 0)
            e.setdefault("check_fails", 0)
            if "last_hard" not in e:
                e["last_hard"] = e.get("last_fail") if e.get("fails") else None
            e["last_seen"] = max(e.get("last_seen") or 0, e.get("last_ok") or 0, e.get("last_fail") or 0) or None
            if e["type"] == "ip" and not e["region"] and e.get("related"):
                e["region"] = region_of_host(e["related"][0])
            if e["type"] == "host":
                for r in [r for r in e["reasons"] if r.startswith("медиа-сервер не отвечает")]:
                    e["fails"] = max(0, e["fails"] - e["reasons"].pop(r))
                    if not e["fails"]:
                        e["last_hard"] = None
                if not e["region"]:
                    e["region"] = region_of_host(a)
        self.seen = {k for k in self.seen if not k.startswith("hm|")}

    def save(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            d = {"version": 3, "settings": self.settings, "net": self.net, "seen": sorted(self.seen),
                 "entries": self.entries, "dns": self.dns}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            try:
                self.write_outputs()
            except OSError:
                pass
            self.dirty = False

    def _entry(self, addr, atype):
        e = self.entries.get(addr)
        if not e:
            e = {"type": atype, "fails": 0, "drops": 0, "oks": 0, "first_fail": None, "last_fail": None,
                 "last_hard": None, "last_ok": None, "last_seen": None, "reasons": {}, "ports": [],
                 "related": [], "ctx": [], "sources": [], "check": None, "check_ok": 0, "check_fails": 0,
                 "region": region_of_host(addr) if atype == "host" else "", "ignored": False}
            self.entries[addr] = e
        return e

    @staticmethod
    def _touch(e, ts, source=None, port=None, related=None, ctx=None):
        e["last_seen"] = max(e.get("last_seen") or 0, ts)
        if source and source not in e["sources"]:
            e["sources"].append(source)
        if port and str(port) not in e["ports"]:
            e["ports"].append(str(port))
        if related and related not in e["related"]:
            e["related"].append(related)
        if ctx and ctx not in e["ctx"]:
            e["ctx"].append(ctx)

    def add_fail(self, key, atype, addr, ts, reason, port=None, related=None, ctx=None, soft=False):
        """soft=True — обрыв уже установленного соединения (а не неудачное подключение)."""
        with self.lock:
            if key in self.seen:
                return False
            self.seen.add(key)
            e = self._entry(addr, atype)
            if soft:
                e["drops"] = e.get("drops", 0) + 1
            else:
                e["fails"] += 1
                e["last_hard"] = max(e.get("last_hard") or 0, ts)
            e["first_fail"] = min(e["first_fail"], ts) if e["first_fail"] else ts
            e["last_fail"] = max(e["last_fail"] or 0, ts)
            e["reasons"][reason] = e["reasons"].get(reason, 0) + 1
            self._touch(e, ts, "voice" if atype == "ip" else "ws", port, related, ctx)
            if related and atype == "ip":
                e["region"] = e["region"] or region_of_host(related)
            self.dirty = True
            return True

    def add_ok(self, key, atype, addr, ts, related=None, port=None, ctx=None):
        with self.lock:
            if key in self.seen:
                e = self.entries.get(addr)
                if e is not None and port and str(port) not in e["ports"]:
                    # дозаполняем порт/сервер для записей из старых версий (без повторного счёта)
                    self._touch(e, ts, "voice" if atype == "ip" else "ws", port, related, ctx)
                    self.dirty = True
                return False
            self.seen.add(key)
            e = self._entry(addr, atype)
            e["oks"] += 1
            e["last_ok"] = max(e["last_ok"] or 0, ts)
            self._touch(e, ts, "voice" if atype == "ip" else "ws", port, related, ctx)
            if related and atype == "ip" and not e["region"]:
                e["region"] = region_of_host(related)
            self.dirty = True
            return True

    def add_probe(self, ip, port, ts, region):
        """IP голосового сервера из теста задержки регионов Discord (кандидаты)."""
        if not is_public_ipv4(ip):
            return
        with self.lock:
            new = ip not in self.entries
            e = self._entry(ip, "ip")
            if (e.get("last_seen") or 0) >= ts and not new:
                return
            self._touch(e, ts, "probe", port)
            if region and not e["region"]:
                e["region"] = region_name(region)
            self.dirty = True

    def set_check(self, addr, res):
        """Сохраняет результат активной проверки. Возвращает (старый статус, новый статус)."""
        with self.lock:
            e = self.entries.get(addr)
            if e is None:
                return None, None
            old = self.status(e)
            res = dict(res, ts=time.time())
            ext = res.get("ext_ip")
            direct = self.net.get("direct_ip")
            if ext and direct:
                res["via"] = "direct" if ext == direct else "vpn"
                if ext != direct and ext not in self.net["vpn_ips"]:
                    self.net["vpn_ips"].append(ext)
            else:
                res["via"] = ""
            e["check"] = res
            if res["ok"] is True:
                e["check_ok"] = e.get("check_ok", 0) + 1
                e["check_streak"] = 0
            elif res["ok"] is False:
                e["check_fails"] = e.get("check_fails", 0) + 1
                e["check_streak"] = e.get("check_streak", 0) + 1
            new = self.status(e)
            if new == "blocked":
                e["was_blocked"] = True
            self.dirty = True
            return old, new

    def set_direct_ip(self, ip):
        with self.lock:
            if ip:
                self.net["direct_ip"] = ip
                self.net["at"] = time.time()
                self.net["vpn_ips"] = [v for v in self.net["vpn_ips"] if v != ip]
                self.dirty = True

    def set_dns(self, host, ips):
        with self.lock:
            self.dns[host] = {"ips": ips, "at": time.time()}
            self.dirty = True

    def set_ignored(self, addrs, value):
        with self.lock:
            for a in addrs:
                if a in self.entries:
                    self.entries[a]["ignored"] = bool(value)
            self.dirty = True

    def forget_problems(self, addrs=None, include_blocked=False):
        """Стереть историю сбоев. По умолчанию — только у исправленных; include_blocked — у всех
        (в т.ч. «Блок» и «Не отвечает»: их результат проверки сбрасывается, при следующей проверке
        реально нерабочие появятся снова). Адрес остаётся в списке серверов и в маршрутах.
        Старые строки логов сбой не вернут — они уже в seen."""
        n = 0
        with self.lock:
            for a, e in self.entries.items():
                if addrs is not None and a not in addrs:
                    continue
                st = self.status(e)
                if st in ("blocked", "silent"):
                    if not include_blocked:
                        continue
                elif st != "fixed" and not (addrs is not None and (self.had_problem(e) or e.get("drops", 0))):
                    continue
                if st in ("blocked", "silent"):
                    e["check"] = None
                e.update(fails=0, check_fails=0, check_streak=0, was_blocked=False, drops=0, reasons={},
                         first_fail=None, last_fail=None, last_hard=None)
                n += 1
            if n:
                self.dirty = True
        return n

    def remove(self, addrs):
        """Убрать адреса из списка совсем. Старые строки логов их не вернут (seen);
        вернутся, только если Discord снова упомянет их в новых строках — значит, сервер жив."""
        with self.lock:
            n = 0
            for a in addrs:
                if self.entries.pop(a, None) is not None:
                    self.dns.pop(a, None)
                    n += 1
            if n:
                self.dirty = True
        return n

    def clear(self):
        with self.lock:
            self.entries = {}
            self.dns = {}
            self.dirty = True   # seen оставляем, чтобы старые строки логов не вернулись

    # ----- статусы -----
    @staticmethod
    def had_problem(e):
        # неответы на проверку сами по себе — не «проблема», только если из-за них был «Блок»
        return e["fails"] > 0 or bool(e.get("was_blocked"))

    def status(self, e):
        """blocked / fixed / drops / ok / seen / silent / gone — по самому свежему свидетельству.
        silent: не ответил на проверку, но в логах с ним сбоев не было (обычно Discord его выключил) —
        не тревога. Блок от проверки — только если в логах были сбои или 2 неответа подряд у сервера,
        через который реально был звонок."""
        ev = []
        if e.get("last_hard"):
            ev.append((e["last_hard"], "fail"))
        if e.get("last_ok"):
            ev.append((e["last_ok"], "ok"))
        c = e.get("check")
        if c:
            if c["ok"] is None:
                if not ev or c["ts"] >= max(t for t, _ in ev):
                    return "gone"
            else:
                ev.append((c["ts"], "ok" if c["ok"] else "fail"))
        if not ev:
            return "drops" if e.get("drops", 0) else "seen"
        last_t, last = max(ev)
        if last == "fail":
            if c and c.get("ok") is False and c["ts"] >= last_t and e["type"] == "ip":
                # сбой в логах новее последнего удачного звонка — реальная проблема;
                # иначе блок только если через сервер звонили за последние сутки и 2 неответа подряд
                real = ((e.get("last_hard") or 0) > (e.get("last_ok") or 0)
                        or ((e.get("last_ok") or 0) > c["ts"] - STALE_SERVER and e.get("check_streak", 1) >= 2))
                if not real:
                    return "silent"
            return "blocked"
        if self.had_problem(e):
            return "fixed"
        if e.get("drops", 0) and (e.get("last_fail") or 0) > max(t for t, k in ev if k == "ok"):
            return "drops"
        return "ok"

    def is_problem(self, e):
        """Когда-либо не подключался (вкл. уже исправленные) — то, что нужно держать в маршрутах."""
        return self.had_problem(e) or (self.include_drops and e.get("drops", 0) > 0)

    def counts(self):
        c = collections.Counter()
        with self.lock:
            for e in self.entries.values():
                st = self.status(e)
                c[(e["type"], st)] += 1
                c[(e["type"], "all")] += 1
                if self.is_problem(e):
                    c[(e["type"], "problem")] += 1
        return c

    # ----- выборки -----
    def select(self, atype, scope):
        """scope: blocked | problem | all. Без игнорируемых."""
        out = []
        with self.lock:
            for a, e in self.entries.items():
                if e["type"] != atype or e.get("ignored"):
                    continue
                if scope == "blocked" and self.status(e) != "blocked":
                    continue
                if scope == "problem" and not self.is_problem(e):
                    continue
                if atype == "host" and self.status(e) == "gone" and scope != "all":
                    continue
                out.append(a)
        return out

    def failed(self):
        with self.lock:
            return {a: e for a, e in self.entries.items() if self.is_problem(e)}

    def failed_hosts(self, include_ignored=False):
        with self.lock:
            return sorted(a for a, e in self.entries.items() if e["type"] == "host" and self.is_problem(e)
                          and (include_ignored or not e.get("ignored")))

    def _cf_ips(self, hosts):
        out = set()
        with self.lock:
            for h in hosts:
                out.update(self.dns.get(h, {}).get("ips", []))
        return out

    def ips_for(self, scope, with_dns=None):
        if with_dns is None:
            with_dns = self.settings.get("cf_dns", False)
        ips = set(self.select("ip", scope))
        if with_dns:
            ips |= self._cf_ips(self.select("host", scope))
        return sorted((i for i in ips if is_public_ipv4(i)), key=ip_key)

    def failed_ips(self, with_dns=None):
        return self.ips_for("problem", with_dns)

    def all_ips(self, with_dns=None):
        return self.ips_for("all", with_dns)

    def export_text(self, scope, fmt):
        """Простые списки: fmt = domains | ips (маршруты — routes_text)."""
        items = sorted(self.select("host", scope)) if fmt == "domains" else self.ips_for(scope, with_dns=False)
        return "\n".join(items) + ("\n" if items else "")

    # ----- маршруты (по текущим настройкам) -----
    def imported_nets(self):
        out = []
        for s in self.settings.get("import_nets", []):
            try:
                out.append(ipaddress.IPv4Network(s))
            except ValueError:
                pass
        return out

    def route_ips(self):
        return self.ips_for(self.settings.get("route_scope", "all"), with_dns=False)

    def routes_plan(self):
        return plan_routes(self.route_ips(), self.settings.get("route_mode", "smart"),
                           bool(self.settings.get("discord_core")), self.imported_nets())

    def routes_text(self):
        return routes_to_bat(self.routes_plan())

    def write_outputs(self):
        d = self.data_dir

        def w(name, text, crlf=False):
            data = text.replace("\r\n", "\n")
            if crlf:
                data = data.replace("\n", "\r\n")
            with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
                f.write(data)

        w("discord-routes.bat", self.routes_text(), crlf=True)
        w("domains.txt", self.export_text("problem", "domains"))
        w("ips.txt", self.export_text("problem", "ips"))
        w("all-ips.txt", self.export_text("all", "ips"))
        for name in LEGACY_OUTPUTS:   # файлы прошлых версий программы — больше не нужны
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
        rows = ["address;type;status;region;fails;drops;oks;check;check_rtt;check_via;check_ext_ip;check_time;"
                "first_fail;last_fail;last_ok;last_seen;reasons;ports;related;dns_ips;sources;ignored"]
        with self.lock:
            for a, e in sorted(self.entries.items(), key=lambda kv: (kv[1]["type"], -(kv[1]["last_seen"] or 0))):
                reasons = ", ".join("%s x%d" % (r, c) for r, c in e["reasons"].items())
                dns_ips = " ".join(self.dns.get(a, {}).get("ips", [])) if e["type"] == "host" else ""
                c = e.get("check") or {}
                rows.append(";".join(str(x) for x in [
                    a, e["type"], self.status(e), e.get("region", ""), e["fails"], e.get("drops", 0), e["oks"],
                    {True: "ok", False: "fail", None: "gone"}.get(c.get("ok"), "") if c else "",
                    c.get("rtt") or "", c.get("via", ""), c.get("ext_ip", ""), fmt_ts(c.get("ts")),
                    fmt_ts(e["first_fail"]), fmt_ts(e["last_fail"]), fmt_ts(e["last_ok"]), fmt_ts(e.get("last_seen")),
                    reasons.replace(";", ","), " ".join(e["ports"]), " ".join(e["related"]), dns_ips,
                    " ".join(e.get("sources", [])), "1" if e.get("ignored") else ""]))
        with open(os.path.join(d, "report.csv"), "w", encoding="utf-8-sig", newline="") as f:
            f.write("\r\n".join(rows) + "\r\n")


# ----------------------------------------------------------------------------
# Проверяльщик
# ----------------------------------------------------------------------------

class Checker:
    def __init__(self, store, emit, workers=24):
        self.store = store
        self.emit = emit
        self.workers = workers
        self.busy = threading.Event()
        self.progress = (0, 0, "")
        self.last_run = None

    def targets(self, scope):
        """Список (addr, kind, port) для проверки."""
        out = []
        with self.store.lock:
            for a, e in self.store.entries.items():
                if e.get("ignored"):
                    continue
                if scope == "problem" and not (self.store.is_problem(e) or self.store.status(e) == "blocked"):
                    continue
                if scope == "all" and e["type"] == "host" and not self.store.is_problem(e):
                    continue  # сотни временных доменов проверять бессмысленно
                if isinstance(scope, (list, set, tuple)) and a not in scope:
                    continue
                if e["type"] == "ip":
                    if e["ports"]:
                        out.append((a, "ip", e["ports"][-1]))
                else:
                    out.append((a, "host", e["ports"][-1] if e["ports"] else 443))
        return out

    def start(self, scope, label, done=None):
        if self.busy.is_set():
            return False
        tg = self.targets(scope)
        if not tg:
            if done:
                done(0, 0, 0)
            return True
        self.busy.set()
        threading.Thread(target=self._run, args=(tg, label, done), daemon=True).start()
        return True

    def _run(self, tg, label, done):
        n_ok = n_fail = n_gone = 0
        try:
            self.progress = (0, len(tg), label)
            ip = detect_direct_ip()
            if ip:
                self.store.set_direct_ip(ip)

            def one(t):
                addr, kind, port = t
                return t, (discord_probe(addr, port) if kind == "ip" else tls_probe(addr, port))

            with concurrent.futures.ThreadPoolExecutor(self.workers) as ex:
                for i, (t, res) in enumerate(ex.map(one, tg), 1):
                    addr = t[0]
                    old, new = self.store.set_check(addr, res)
                    if res["ok"]:
                        n_ok += 1
                    elif res["ok"] is False:
                        n_fail += 1
                    else:
                        n_gone += 1
                    self.progress = (i, len(tg), label)
                    if old != new and old is not None:
                        if new == "fixed" or (new == "ok" and old == "blocked"):
                            via = {"vpn": " через VPN", "direct": " напрямую"}.get(res.get("via"), "")
                            self.emit("✅ %s  %s заработал%s (%s мс)" % (fmt_ts(time.time()), addr, via, res["rtt"]),
                                      level="fixed", addr=addr)
                        elif new == "blocked":
                            self.emit("⛔ %s  %s не отвечает при проверке — %s" % (fmt_ts(time.time()), addr,
                                                                                  res["err"]), level="fail", addr=addr,
                                      check=True)
            self.last_run = time.time()
            self.store.save()
        finally:
            self.busy.clear()
            self.progress = (0, 0, "")
            self.emit("Проверка «%s»: отвечают %d, не отвечают %d%s из %d" % (
                label, n_ok, n_fail, (", удалены %d" % n_gone) if n_gone else "", len(tg)), level="info")
            if done:
                done(n_ok, n_fail, len(tg))


# ----------------------------------------------------------------------------
# Живое состояние (что происходит сейчас)
# ----------------------------------------------------------------------------

class LiveState:
    def __init__(self):
        self.lock = threading.Lock()
        self.ctx = {}

    def update(self, ctx, ts, **kw):
        with self.lock:
            c = self.ctx.setdefault(ctx, {"state": "Не подключено", "level": "idle", "host": "", "ip": "",
                                          "port": "", "since": ts, "last": ts, "rtt": None, "rtt_ts": 0,
                                          "note": ""})
            if "state" in kw and kw["state"] != c["state"]:
                c["since"] = ts
            c.update(kw)
            c["last"] = max(c["last"], ts)

    def touch(self, ctx, ts):
        with self.lock:
            if ctx in self.ctx:
                self.ctx[ctx]["last"] = max(self.ctx[ctx]["last"], ts)

    def ping(self, ip, rtt, ts):
        with self.lock:
            for c in self.ctx.values():
                if c["ip"] == ip:
                    c["rtt"], c["rtt_ts"] = rtt, ts
                    c["last"] = max(c["last"], ts)
                    if c["level"] == "warn" and c["note"].startswith("UDP не отвечает"):
                        c["level"], c["note"] = "ok", ""

    def ping_timeout(self, ip, ts):
        with self.lock:
            for c in self.ctx.values():
                if c["ip"] == ip and c["level"] == "ok":
                    c["note"] = "UDP не отвечает (ping timeout)"
                    c["level"] = "warn"
                    c["last"] = max(c["last"], ts)

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.ctx.items()}


# ----------------------------------------------------------------------------
# Разбор логов
# ----------------------------------------------------------------------------

class FileParser:
    """Состояние разбора одного файла (ожидающие подключения по контекстам default/stream)."""

    def __init__(self, path, kind, store, emit, live=None):
        self.path = path
        self.kind = kind
        self.store = store
        self.emit = emit
        self.live = live
        self.reset()

    def reset(self):
        self.pending_host = {}
        self.current_host = {}
        self.pending_media = {}
        self.ping_streak = {}
        self.media_ok = {}
        self.region = None
        self.last_ts = None

    def _live(self, ctx, ts, **kw):
        if self.live is not None and ctx in CTX_NAMES:
            self.live.update(ctx, ts, **kw)

    def _host_fail(self, ctx, host, port, ts, reason, soft=False):
        key = "hf|%s|%s|%s" % (host, ctx, int(ts * 1000))
        if self.store.add_fail(key, "host", host, ts, reason, port=port, ctx=ctx, soft=soft):
            self.emit("%s %s  [%s] %s:%s  — %s" % ("~" if soft else "✖", fmt_ts(ts), CTX_NAMES.get(ctx, ctx),
                                                    host, port, reason), new_host=host,
                      level="drop" if soft else "fail", addr=host)
        if not soft:
            self._live(ctx, ts, state="Сбой подключения к серверу", level="fail", host=host, note=reason)

    def _media_fail(self, ctx, ip, port, ts, reason, host=None):
        key = "mf|%s|%s|%s" % (ip, ctx, int(ts * 1000))
        if self.store.add_fail(key, "ip", ip, ts, reason, port=port, related=host, ctx=ctx):
            self.emit("✖ %s  [%s] %s:%s  — %s%s" % (fmt_ts(ts), CTX_NAMES.get(ctx, "UDP"), ip, port, reason,
                                                   ("  (%s)" % host) if host else ""), level="fail", addr=ip)
        if ctx in CTX_NAMES:
            self._live(ctx, ts, state="UDP не подключился", level="fail", ip=ip, port=port, note=reason)

    def check_timeouts(self, now, wall=False):
        for ctx, p in list(self.pending_host.items()):
            if now - p["ts"] > HOST_TIMEOUT:
                del self.pending_host[ctx]
                if wall and now - p["ts"] > LIVE_PENDING_MAX_AGE:
                    continue
                self._host_fail(ctx, p["host"], p["port"], p["ts"], "WebSocket: нет ответа %d с" % HOST_TIMEOUT)
        for ctx, p in list(self.pending_media.items()):
            if now - p["ts"] > MEDIA_TIMEOUT:
                del self.pending_media[ctx]
                if wall and now - p["ts"] > LIVE_PENDING_MAX_AGE:
                    continue
                self._media_fail(ctx, p["ip"], p["port"], p["ts"], "UDP: нет подключения %d с" % MEDIA_TIMEOUT,
                                 p.get("host"))

    def feed(self, line):
        if not any(k in line for k in QUICK_FILTER):
            if self.pending_host or self.pending_media:
                ts = parse_ts(line)
                if ts:
                    self.last_ts = ts
                    self.check_timeouts(ts)
            return
        ts = parse_ts(line)
        if ts is None:
            return
        self.last_ts = ts
        self.check_timeouts(ts)
        if self.kind == "webrtc":
            self._feed_webrtc(line, ts)
        else:
            self._feed_renderer(line, ts)

    def _feed_webrtc(self, line, ts):
        m = R_PING_OK.search(line)
        if m:
            ip = m.group(1)
            self.ping_streak.pop(ip, None)
            if self.live is not None:
                self.live.ping(ip, int(m.group(3)), ts)
            return
        m = R_PING_TIMEOUT.search(line)
        if m:
            ip, port = m.group(1), m.group(2)
            cnt, rec = self.ping_streak.get(ip, (0, False))
            cnt += 1
            if cnt >= PING_TIMEOUTS_FOR_FAIL and not rec:
                self._media_fail("udp", ip, port, ts, "UDP: сервер перестал отвечать (ping timeout)")
                rec = True
            self.ping_streak[ip] = (cnt, rec)
            if self.live is not None:
                self.live.ping_timeout(ip, ts)
            return
        m = R_REGION.search(line)
        if m:
            self.region = m.group(1)
            return
        m = R_REGION_IP.search(line)
        if m:
            self.store.add_probe(m.group(1), m.group(2), ts, self.region)

    def _feed_renderer(self, line, ts):
        m = R_HB_ACK.search(line)
        if m:
            if self.live is not None:
                self.live.touch(m.group(1), ts)
            return
        m = R_CONNECT.search(line)
        if m:
            ctx, host, port = m.group(1), m.group(2).lower(), m.group(3)
            prev = self.pending_host.get(ctx)
            if prev and prev["host"] != host:
                self._host_fail(ctx, prev["host"], prev["port"], prev["ts"], "WebSocket: не подключился")
            self.pending_host[ctx] = {"host": host, "port": port, "ts": ts}
            self.current_host[ctx] = {"host": host, "port": port, "ts": ts, "dropped": False}
            self._live(ctx, ts, state="Подключение к серверу…", level="wait", host=host, ip="", port="",
                       rtt=None, note="")
            return
        m = R_CONNECTED.search(line)
        if m:
            ctx, host = m.group(1), m.group(2).lower()
            p = self.pending_host.pop(ctx, None)
            self.store.add_ok("ho|%s|%s|%s" % (host, ctx, int(ts * 1000)), "host", host, ts,
                              port=(p or {}).get("port"), ctx=ctx)
            mo = self.media_ok.get(ctx)
            if mo and mo[0] == host:
                self._live(ctx, ts, state="Подключено", level="ok", host=host, ip=mo[1], port=mo[2], note="")
            else:
                self._live(ctx, ts, state="Сервер ответил, ждём UDP…", level="wait", host=host, note="")
            return
        m = R_WSCLOSED.search(line)
        if m:
            ctx, clean, code, reason = m.group(1), m.group(2), m.group(3), m.group(4).strip()
            if clean == "true":
                return
            reason_txt = "WebSocket закрыт: %s" % (reason or ("code %s" % code))
            if "OP_HELLO" in reason:
                reason_txt = "WebSocket: нет OP_HELLO (таймаут)"
            p = self.pending_host.pop(ctx, None)
            if p:
                self._host_fail(ctx, p["host"], p["port"], p["ts"], reason_txt)
            else:
                c = self.current_host.get(ctx)
                if c and not c["dropped"]:
                    c["dropped"] = True
                    self._host_fail(ctx, c["host"], c["port"], ts, "обрыв: " + reason_txt, soft=True)
                    self._live(ctx, ts, state="Обрыв, переподключение…", level="warn", note=reason_txt)
            return
        m = R_HB_TIMEOUT.search(line)
        if m:
            ctx = m.group(1)
            c = self.current_host.get(ctx)
            if c and not c["dropped"]:
                c["dropped"] = True
                self._host_fail(ctx, c["host"], c["port"], ts, "обрыв: heartbeat timeout", soft=True)
                self._live(ctx, ts, state="Обрыв, переподключение…", level="warn", note="heartbeat timeout")
            return
        m = R_CLOSE.search(line)
        if m:
            self.pending_host.pop(m.group(1), None)
            return
        m = R_MEDIA_CREATE.search(line)
        if m:
            ctx, ip, port = m.group(1), m.group(2), m.group(3)
            host = (self.current_host.get(ctx) or {}).get("host")
            self.pending_media[ctx] = {"ip": ip, "port": port, "ts": ts, "host": host}
            self._live(ctx, ts, state="Подключение UDP…", level="wait", ip=ip, port=port)
            return
        m = R_MEDIA_OK.search(line)
        if m:
            ctx, ip, port = m.group(1), m.group(2), m.group(3)
            p = self.pending_media.pop(ctx, None)
            host = (p or {}).get("host") or (self.current_host.get(ctx) or {}).get("host")
            self.store.add_ok("mo|%s|%s|%s" % (ip, ctx, int(ts * 1000)), "ip", ip, ts, related=host,
                              port=port, ctx=ctx)
            c = self.current_host.get(ctx)
            if c:
                c["dropped"] = False
            self.media_ok[ctx] = (host, ip, port)
            self._live(ctx, ts, state="Подключено", level="ok", ip=ip, port=port, note="")
            return
        m = R_RTC_STATE.search(line)
        if m:
            ctx, frm, to = m.group(1), m.group(2), m.group(3)
            if frm == "RTC_CONNECTING" and to != "RTC_CONNECTED":
                p = self.pending_media.pop(ctx, None)
                if p:
                    self._media_fail(ctx, p["ip"], p["port"], ts, "UDP: не подключился (%s)" % to, p.get("host"))
            elif to == "DISCONNECTED" and frm in ("RTC_DISCONNECTED", "RTC_CONNECTED"):
                self.media_ok.pop(ctx, None)
                self._live(ctx, ts, state="Не подключено", level="idle", rtt=None, note="")
            return
        m = R_DESTROY.search(line)
        if m:
            ctx = m.group(1)
            self.pending_host.pop(ctx, None)
            self.pending_media.pop(ctx, None)
            self.media_ok.pop(ctx, None)
            self._live(ctx, ts, state="Не подключено", level="idle", rtt=None, note="")


class Tailer(threading.Thread):
    def __init__(self, store, extra_dirs, emit, interval=1.0, live=None):
        super().__init__(daemon=True)
        self.store = store
        self.extra = extra_dirs
        self.emit = emit
        self.interval = interval
        self.live = live
        self.files = {}
        self.stop_ev = threading.Event()
        self.initial_done = threading.Event()
        self.files_list = []

    def _file_ident(self, path):
        try:
            with open(path, "rb") as f:
                return f.read(64)
        except OSError:
            return b""

    def poll_once(self, initial=False):
        files = find_log_files(self.extra)
        self.files_list = [p for p, _ in files]
        for path, kind in files:
            st = self.files.get(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if st is None:
                st = {"parser": FileParser(path, kind, self.store, self.emit, self.live), "offset": 0,
                      "buf": b"", "ident": self._file_ident(path)}
                self.files[path] = st
            else:
                ident = self._file_ident(path)
                if size < st["offset"] or (ident and st["ident"] and
                                           ident[:len(st["ident"])] != st["ident"][:len(ident)]):
                    st["offset"], st["buf"], st["ident"] = 0, b"", ident
                    st["parser"].reset()
                if not st["ident"]:
                    st["ident"] = ident
            if size == st["offset"]:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(st["offset"])
                    chunk = f.read(size - st["offset"])
            except OSError:
                continue
            st["offset"] += len(chunk)
            data = st["buf"] + chunk
            lines = data.split(b"\n")
            st["buf"] = lines.pop()
            p = st["parser"]
            for raw in lines:
                p.feed(raw.decode("utf-8", "replace").rstrip("\r"))
        now = time.time()
        for st in self.files.values():
            st["parser"].check_timeouts(now, wall=True)

    def run(self):
        try:
            self.poll_once(initial=True)
        except Exception as e:  # pragma: no cover
            self.emit("Ошибка разбора: %r" % e, level="info")
        self.initial_done.set()
        resolve_hosts(self.store)
        self.store.save(force=True)
        last_save = time.time()
        while not self.stop_ev.wait(self.interval):
            try:
                self.poll_once()
            except Exception as e:  # pragma: no cover
                self.emit("Ошибка разбора: %r" % e, level="info")
            if self.store.dirty and time.time() - last_save > 2:
                self.store.save()
                last_save = time.time()


# ----------------------------------------------------------------------------
# DNS
# ----------------------------------------------------------------------------

def resolve_host(host):
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos}, key=ip_key)
    except OSError:
        return []


def resolve_hosts(store, only_missing=True, hosts=None):
    for h in (hosts if hosts is not None else store.failed_hosts(include_ignored=True)):
        cur = store.dns.get(h)
        if only_missing and cur is not None and (cur.get("ips") or time.time() - cur.get("at", 0) < 3600):
            continue
        store.set_dns(h, resolve_host(h))


# ----------------------------------------------------------------------------
# Папка данных, автозапуск
# ----------------------------------------------------------------------------

def default_data_dir():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
            docs = buf.value
    except Exception:
        pass
    return os.path.join(docs, APP_NAME)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DiscordVoiceCatcher"


def autostart_get():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def autostart_set(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, '"%s" --minimized' % sys.executable)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# Консольный режим
# ----------------------------------------------------------------------------

def check_text(c):
    if not c:
        return "—"
    if c["ok"] is None:
        return "▫ " + c["err"]
    if c["ok"]:
        return "✔ %s мс" % c["rtt"]
    return "✖ " + c["err"]


def via_text(c):
    if not c or not c.get("ok"):
        return ""
    return {"vpn": "через VPN", "direct": "напрямую"}.get(c.get("via"), c.get("ext_ip") or "")


def run_scan(args):
    store = Store(args.data)
    if args.drops:
        store.include_drops = True
    msgs = []
    live = LiveState()
    emit = lambda text, **k: text and msgs.append(text)
    t = Tailer(store, args.logdir, emit, live=live)
    t.poll_once(initial=True)
    resolve_hosts(store)
    if args.check:
        done = threading.Event()
        ch = Checker(store, emit)
        ch.start("problem", "проблемные", lambda *a: done.set())
        done.wait()
    store.save(force=True)
    out = sys.stdout
    out.write("Логи Discord:\n")
    for p in t.files_list:
        out.write("  %s\n" % p)
    out.write("\nСобытия: %d (последние 40)\n" % len(msgs))
    for m in msgs[-40:]:
        out.write("  %s\n" % m)
    for typ, title in (("ip", "IP-адреса (проблемные)"), ("host", "Домены (проблемные)")):
        out.write("\n%s:\n" % title)
        rows = [(a, e) for a, e in store.entries.items() if e["type"] == typ and store.is_problem(e)]
        rows.sort(key=lambda kv: (STATUS_ORDER.index(store.status(kv[1])), ip_key(kv[0]) if typ == "ip" else kv[0]))
        for a, e in rows:
            c = e.get("check")
            out.write("  %-32s %-14s %-11s неуд:%-2d усп:%-3d проверка: %s %s\n" % (
                a, STATUS_RU[store.status(e)], e.get("region", "")[:11], e["fails"], e["oks"], check_text(c),
                via_text(c)))
    cnt = store.counts()
    out.write("\nСейчас: %s\n" % {CTX_NAMES[k]: v["state"] for k, v in live.snapshot().items()})
    out.write("IP провайдера: %s, VPN: %s\n" % (store.net.get("direct_ip"), ", ".join(store.net.get("vpn_ips", []))))
    out.write("IP: всего %d, когда-либо проблемных %d, сейчас блок %d, исправлено %d\n" % (
        cnt[("ip", "all")], cnt[("ip", "problem")], cnt[("ip", "blocked")], cnt[("ip", "fixed")]))
    plan = store.routes_plan()
    out.write("\nМаршруты для Keenetic (%d строк, discord-routes.bat):\n" % len(plan))
    for p in plan:
        out.write("  %-18s %-16s серверов: %-4s %s%s\n" % (p["net"], p["net"].netmask, p["count"], p["what"],
                                                           "  ⚠ широкая" if p["wide"] else ""))
    out.write("Файлы: %s\n" % store.data_dir)
    out.flush()


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--scan", action="store_true", help="разобрать логи и вывести результат в консоль")
    ap.add_argument("--check", action="store_true", help="(--scan) перепроверить проблемные адреса")
    ap.add_argument("--logdir", action="append", default=[], help="дополнительная папка с логами")
    ap.add_argument("--data", default=None, help="папка для результатов")
    ap.add_argument("--minimized", action="store_true", help="запустить свернутым в трей")
    ap.add_argument("--drops", action="store_true", help="(--scan) учитывать обрывы установленных соединений")
    ap.add_argument("--page", default=None, choices=["home", "srv", "rt", "log", "set"], help=argparse.SUPPRESS)
    args = ap.parse_args()
    args.data = args.data or default_data_dir()
    if args.scan:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        run_scan(args)
    else:
        from dvc_gui import run_gui
        run_gui(args)


if __name__ == "__main__":
    main()

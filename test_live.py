# Тест слежения: создаёт фальшивую папку Discord, дописывает строки как Discord и проверяет результат.
import datetime as dt, os, shutil, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discord_voice_catcher as m

tmp = tempfile.mkdtemp(prefix="dvc_test_")
root = os.path.join(tmp, "discord"); os.makedirs(os.path.join(root, "logs"))
data = os.path.join(tmp, "data")
m.discord_roots = lambda extra=None: [root]   # только фальшивые логи
m.HOST_TIMEOUT = 3; m.MEDIA_TIMEOUT = 2
ren = os.path.join(root, "logs", "renderer_js.log")
web = os.path.join(root, "logs", "discord-webrtc_0")

def ts(off=0):
    t = dt.datetime.now() + dt.timedelta(seconds=off)
    return t.strftime("[%Y-%m-%d %H:%M:%S.") + "%03d]" % (t.microsecond // 1000)

def app(path, lines):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for l in lines: f.write(l + "\n")

msgs = []
store = m.Store(data)
live = m.LiveState()
t = m.Tailer(store, [], lambda text, **k: text and msgs.append(text), live=live)
app(ren, [ts() + " [info]  [default] start"])
open(web, "w").close()
t.poll_once(initial=True)

# 1) успешное подключение — не должно попасть в список
app(ren, [ts() + " [info]  [RTCControlSocket(default)] [CONNECT] wss://c-ams17-good0001.discord.media:2083/",
          ts() + " [info]  [RTCControlSocket(default)] [CONNECTED] wss://c-ams17-good0001.discord.media:2083/ in 300 ms",
          ts() + " [info]  [Connection(default)] Creating connection to 104.29.153.10:19300 with audio ssrc: 1",
          ts() + " [info]  [RTCConnection(1, default)] RTC connected to media server: 104.29.153.10:19300"])
app(web, [ts() + " [1] (socket_client.cpp:333): Ping received from 104.29.153.10:19300, seq: 1, rtt: 37",
          ts() + " [1] (region_latency_tester.cpp:120): Sending 5 latency test pings to region warsaw",
          ts() + " [1] (region_latency_tester.cpp:139): Sending latency test ping to IP 104.29.199.1:19301"])
t.poll_once()
s = live.snapshot()["default"]
assert s["state"] == "Подключено" and s["ip"] == "104.29.153.10" and s["rtt"] == 37, s
assert "104.29.199.1" in store.all_ips() and "104.29.199.1" not in store.failed_ips()
assert store.entries["104.29.199.1"]["region"] == "Варшава"
assert store.status(store.entries["104.29.153.10"]) == "ok"
# 2) WebSocket не получил OP_HELLO
app(ren, [ts() + " [info]  [RTCControlSocket(stream)] [CONNECT] wss://c-waw04-bad00001.discord.media:2053/",
          ts() + " [warn]  [RTCControlSocket(stream)] [WS CLOSED] (clean: false, code: 0, reason: The connection timed out after 20000 ms - did not receive OP_HELLO in time.) retrying in 3.30 seconds."])
# 3) UDP медиа-сервер не подключился (переход RTC_CONNECTING => DISCONNECTED)
app(ren, [ts() + " [info]  [RTCControlSocket(default)] [CONNECT] wss://c-waw05-bad00002.discord.media:443/",
          ts() + " [info]  [RTCControlSocket(default)] [CONNECTED] wss://c-waw05-bad00002.discord.media:443/ in 200 ms",
          ts() + " [info]  [Connection(default)] Creating connection to 104.29.150.77:19311 with audio ssrc: 2",
          ts() + " [info]  [RTCConnection(1, default)] RTC connection state: RTC_CONNECTING => DISCONNECTED"])
# 4) UDP ping timeout в нативном логе
app(web, [ts() + " [1] (socket_client.cpp:348): Ping timeout from: 104.29.160.5:19320, seq: 2",
          ts() + " [1] (socket_client.cpp:348): Ping timeout from: 104.29.160.5:19320, seq: 3"])
t.poll_once()
# 5) живой таймаут: CONNECT без ответа, новых строк нет — должен сработать по часам
app(ren, [ts() + " [info]  [RTCControlSocket(stream)] [CONNECT] wss://c-otp02-hang0003.discord.media:2087/",
          ts() + " [info]  [RTCControlSocket(default)] [CONNECTED] wss://c-ams17-good0001.discord.media:2083/ in 100 ms",
          ts() + " [info]  [Connection(default)] Creating connection to 104.29.170.9:19333 with audio ssrc: 3"])
t.poll_once()
assert live.snapshot()["stream"]["state"] == "Подключение к серверу…"
time.sleep(3.5)
t.poll_once()
assert live.snapshot()["stream"]["level"] == "fail", live.snapshot()["stream"]
assert live.snapshot()["default"]["level"] == "fail"
# 6) ротация файла: файл пересоздан меньшим — не должно падать
os.remove(ren); app(ren, [ts() + " [info]  [default] new session"]); t.poll_once()
# 7) повторный проход по тем же строкам не создаёт дублей
store.dirty = True; store.save()
before = {a: e["fails"] for a, e in store.failed().items()}
t2 = m.Tailer(m.Store(data), [], lambda *a, **k: None); t2.poll_once(initial=True)

print("\n".join(msgs))
print("hosts:", store.failed_hosts())
print("ips:", store.failed_ips(with_dns=False))
assert "c-ams17-good0001.discord.media" not in store.failed_hosts()
# домен, у которого не ответил только UDP медиа-сервер, сам «проблемным» не считается (его WebSocket работал)
assert set(store.failed_hosts()) == {"c-waw04-bad00001.discord.media", "c-otp02-hang0003.discord.media"}
assert set(store.failed_ips(with_dns=False)) == {"104.29.150.77", "104.29.160.5", "104.29.170.9"}
s2 = m.Store(data)
assert {a: e["fails"] for a, e in s2.failed().items()} == before, "дубли!"
bat = open(os.path.join(data, "discord-routes.bat"), encoding="utf-8").read()
print("--- discord-routes.bat\n" + bat)
print("--- domains.txt\n" + open(os.path.join(data, "domains.txt"), encoding="utf-8").read())
# 104.29.128–191 — известным блоком /18, а 104.29.199.x вне его — отдельной /24
assert bat.split() == "route add 104.29.128.0 mask 255.255.192.0 0.0.0.0 route add 104.29.199.0 mask 255.255.255.0 0.0.0.0".split(), bat
assert b"\r\n" in open(os.path.join(data, "discord-routes.bat"), "rb").read()
assert not os.path.exists(os.path.join(data, "keenetic-routes.bat"))
# точный режим — по /24 на каждую замеченную подсеть
ex = m.plan_routes(store.all_ips(), "exact")
assert "104.29.150.0/24" in {str(p["net"]) for p in ex}
# импорт: узкие сети из iplist + core, без дублей; IP внутри импортированной сети не даёт отдельной строки
imp = m.parse_routes_file("route add 66.22.192.0 mask 255.255.192.0 0.0.0.0\n35.192.0.0/12\n")
assert [str(n) for n in imp] == ["35.192.0.0/12", "66.22.192.0/18"]
pl = m.plan_routes(["104.29.150.77", "35.217.1.1", "8.8.8.8"], "smart", core=True,
                   imported=[n for n in imp if n.num_addresses <= m.WIDE_NET])
nets = {str(p["net"]) for p in pl}
assert {"104.29.128.0/18", "35.217.0.0/18", "66.22.192.0/18", "162.159.128.0/20", "8.8.8.0/24"} <= nets, nets
assert m.coverage([p["net"] for p in pl], ["104.29.150.77", "35.217.1.1", "8.8.8.8"])[0] == 3
# «Убрать исправленные»: исправленный становится «Работает», блок не трогается, адрес остаётся в списке
e = store.entries["104.29.150.77"]
e["check"] = {"ok": True, "ts": time.time() + 10, "rtt": 50}
assert store.status(e) == "fixed"
assert store.status(store.entries["104.29.160.5"]) == "blocked"
assert store.forget_problems() == 1
assert store.status(e) == "ok" and not store.is_problem(e) and "104.29.150.77" in store.all_ips()
assert store.status(store.entries["104.29.160.5"]) == "blocked"
assert store.forget_problems({"104.29.160.5"}) == 0   # пока в блоке — не забываем
# сервер только из теста регионов не ответил на проверку — «Не отвечает», не блок и не проблема
store.set_check("104.29.199.1", {"ok": False, "err": "нет ответа"})
p = store.entries["104.29.199.1"]
assert store.status(p) == "silent" and not store.is_problem(p)
store.set_check("104.29.199.1", {"ok": False, "err": "нет ответа"})
assert store.status(p) == "silent"
store.set_check("104.29.199.1", {"ok": True, "rtt": 40})
assert store.status(p) == "ok"   # не «Исправлено» — проблемы не было
# сервер, через который был звонок: 1 неответ — «Не отвечает», 2 подряд — «Блок»
g = store.entries["104.29.153.10"]
store.set_check("104.29.153.10", {"ok": False, "err": "нет ответа"})
assert store.status(g) == "silent"
store.set_check("104.29.153.10", {"ok": False, "err": "нет ответа"})
assert store.status(g) == "blocked"
# «Очистить ошибки» — убирает всё, включая блок; адреса остаются
n_all = len(store.all_ips())
assert store.forget_problems(include_blocked=True) >= 3
assert not [a for a, e in store.entries.items() if store.status(e) in ("blocked", "fixed", "silent")]
assert not store.failed() and len(store.all_ips()) == n_all
# пункт меню «Очистить ошибки у выделенных» — работает и на «Блоке»
store.set_check("104.29.153.10", {"ok": False, "err": "x"}); store.set_check("104.29.153.10", {"ok": False, "err": "x"})
assert store.status(g) == "blocked"
assert store.forget_problems({"104.29.153.10"}, include_blocked=True) == 1 and store.status(g) != "blocked"
# давно неиспользуемый сервер при неответе — «Не отвечает», даже 5 раз подряд
g["last_ok"] = time.time() - 2 * 86400
for _ in range(5):
    store.set_check("104.29.153.10", {"ok": False, "err": "x"})
assert store.status(g) == "silent", store.status(g)
# «Удалить из списка» — исчезает и не возвращается при повторном разборе старых логов
assert store.remove(["104.29.153.10"]) == 1
store.save(force=True)
s3 = m.Store(data); m.Tailer(s3, [], lambda *a, **k: None).poll_once(initial=True)
assert "104.29.153.10" not in s3.entries
print("OK")
shutil.rmtree(tmp, ignore_errors=True)

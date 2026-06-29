"""JSON API over the local SQLite DB — powers the mobile (Expo) app.

Read-only browsing endpoints (search, feed, collections, stats, plate detail). Favorites are
kept on-device in the MVP; accounts + monitorings/push come later. Pure script, no AI.

Run:  python -m local.api    (uvicorn on 0.0.0.0:8000)
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from local import config, db

app = FastAPI(title="Моніторинг Автономерів API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

PAGE = 20

# Public endpoints that never require the app key.
_OPEN_PATHS = {"/health", "/open", "/pitch", "/features", "/", "/robots.txt", "/sitemap.xml"}
# Simple in-memory per-IP rate limiter (one uvicorn worker). minute-bucket -> {ip: count}.
_rate_bucket: dict = {}
_rate_minute: list = [0]


@app.middleware("http")
async def guard(request: Request, call_next):
    """Protect the API from third-party scraping: per-IP rate limit + app API key."""
    import time

    path = request.url.path
    ip = request.client.host if request.client else "?"

    # Rate limiting (per IP, per minute).
    minute = int(time.time() // 60)
    if _rate_minute[0] != minute:
        _rate_minute[0] = minute
        _rate_bucket.clear()
    n = _rate_bucket.get(ip, 0) + 1
    _rate_bucket[ip] = n
    if n > config.API_RATE_PER_MIN:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    # App API key (skip health/open and the secret-protected ingest/parse-job endpoints).
    if config.API_KEY and path not in _OPEN_PATHS and not path.startswith("/viber") \
            and path not in ("/ingest", "/parse-job", "/stage", "/collect", "/collect-html",
                             "/collector", "/autocheck/register", "/autocheck/load-test",
                             "/autocheck/load-status", "/autocheck/load-wanted",
                             "/autocheck/wanted-status", "/autocheck/poll", "/autocheck/result",
                             "/autocheck/agent-status", "/autocheck/ria-status"):
        if request.headers.get("x-api-key") != config.API_KEY:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.on_event("startup")
async def _warm() -> None:
    """Warm the DB read cache on startup so the first request is fast; register Viber webhook."""
    try:
        await db.warm_cache()
    except Exception:  # noqa: BLE001
        pass
    if config.VIBER_TOKEN:
        try:
            import asyncio
            from local import viber
            await asyncio.to_thread(viber.set_webhook)
        except Exception as exc:  # noqa: BLE001
            print(f"[viber] set_webhook on startup failed: {exc!r}")
    # Авто-завантаження відкритого датасету розшуку (публічний, без секрету) — щоб
    # перевірка «в розшуку / не в розшуку» завжди була достовірною, навіть коли авто
    # шукає PC-агент (у його базі таблиці розшуку немає → доливаємо її на сервері).
    try:
        if _WANTED_STATUS.get("state") not in ("завантаження…", "готово"):
            _threading.Thread(target=_load_wanted, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[wanted] auto-load on startup failed: {exc!r}")


@app.post("/viber/webhook")
async def viber_webhook(request: Request) -> dict:
    """Receive Viber events (signature-verified) and dispatch them in the background."""
    import asyncio
    import json as _json

    from local import viber

    body = await request.body()
    sig = request.headers.get("x-viber-content-signature", "")
    if not viber.valid_signature(body, sig):
        raise HTTPException(403, "bad signature")
    try:
        event = _json.loads(body.decode("utf-8"))
    except ValueError:
        return {"status": "ignored"}
    asyncio.create_task(viber.handle(event))
    return {"status": "ok"}


@app.get("/viber/set-webhook")
async def viber_set_webhook(secret: str = "") -> dict:
    """Manually (re)register the Viber webhook (secret-protected)."""
    if not config.INGEST_SECRET or secret != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local import viber
    import asyncio
    return await asyncio.to_thread(viber.set_webhook)


@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


# Expo Go tunnel deep-link (update if the tunnel restarts).
EXPO_URL = "exp://bah32_a-anonymous-8081.exp.direct"

_PITCH_HTML = '''<!doctype html><html lang="uk"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>База Автономерів — презентація проєкту</title>
<meta property="og:title" content="🇺🇦 База Автономерів">
<meta property="og:description" content="Знайди свій номер. Перевір будь-яке авто. Усе про автономери України — на відкритих даних.">
<meta name="theme-color" content="#0b0e14">
<style>
:root{--bg:#0b0e14;--card:#161b24;--line:#222a36;--text:#f1f4f9;--sub:#8b95a7;--blue:#3b82f6;--green:#22c55e}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto;padding:0 20px}
.hero{position:relative;overflow:hidden;padding:88px 0 64px;text-align:center;background:radial-gradient(1200px 500px at 50% -10%,rgba(59,130,246,.28),transparent 60%)}
.flag{display:inline-flex;width:64px;height:64px;border-radius:20px;align-items:center;justify-content:center;font-size:30px;background:linear-gradient(135deg,#0057b7,#0057b7 50%,#ffd700 50%,#ffd700);margin-bottom:22px}
.hero h1{font-size:clamp(38px,8vw,62px);font-weight:900;letter-spacing:-1px;line-height:1.05}
.hero .tag{margin-top:18px;font-size:clamp(17px,3.4vw,22px);color:#cdd6e6;max-width:620px;margin-left:auto;margin-right:auto}
.kick{margin-top:26px;display:inline-block;padding:9px 16px;border-radius:999px;background:rgba(255,255,255,.06);border:1px solid var(--line);color:var(--sub);font-size:13px}
section{padding:54px 0;border-top:1px solid var(--line)}
.eyebrow{color:var(--blue);font-weight:800;letter-spacing:2px;text-transform:uppercase;font-size:12px;margin-bottom:14px}
h2{font-size:clamp(26px,5vw,38px);font-weight:900;letter-spacing:-.5px;margin-bottom:16px}
.lead{font-size:clamp(17px,3.2vw,20px);color:#d4dbe8}
.soul{font-size:clamp(19px,3.6vw,24px);font-weight:600;color:#eef2f9;line-height:1.5}
.soul b{color:#ffd700;font-weight:800}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-top:26px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
.tile{border-radius:20px;padding:22px;min-height:170px;display:flex;flex-direction:column;justify-content:flex-end;color:#fff}
.t1{background:linear-gradient(135deg,#3b82f6,#1d4ed8)}.t2{background:linear-gradient(135deg,#8b5cf6,#6d28d9)}
.t3{background:linear-gradient(135deg,#14b8a6,#0f766e)}.t4{background:linear-gradient(135deg,#f59e0b,#b45309)}
.tile .ic{font-size:30px;margin-bottom:auto}.tile h3{font-size:21px;font-weight:900;margin-top:14px}.tile p{font-size:14px;opacity:.92;margin-top:4px}
.stats{display:flex;gap:14px;flex-wrap:wrap;margin-top:24px}
.stat{flex:1;min-width:140px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;text-align:center}
.stat b{display:block;font-size:30px;font-weight:900}.stat span{color:var(--sub);font-size:13px}
.note{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;margin-top:24px}
.cta{text-align:center;padding:64px 0 30px}
.btn{display:inline-block;background:var(--blue);color:#fff;text-decoration:none;font-weight:800;font-size:17px;padding:15px 30px;border-radius:14px;margin:8px}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
footer{color:var(--sub);font-size:13px;text-align:center;padding:30px 0 50px}
.pills{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-top:20px}
.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:8px 15px;font-size:13px;color:#cdd6e6}
</style></head><body>

<div class="hero"><div class="wrap">
  <div class="flag">🚘</div>
  <h1>База&nbsp;Автономерів</h1>
  <p class="tag">Знайди свій номер. Перевір будь-яке авто. Усе про автономери України — в одному місці.</p>
  <div class="kick">🇺🇦 на відкритих державних даних · без зливів · без персональних даних</div>
</div></div>

<div class="wrap">

<section>
  <div class="eyebrow">Душа проєкту</div>
  <p class="soul">Номер на авто — це маленька історія. Чиїсь цифри, дата народження сина, дзеркальна краса, або просто бажання <b>не купити кота в мішку</b> перед покупкою авто.<br><br>
  Раніше це були два різні світи: «де знайти красивий вільний номер» і «чи чесне це авто». Ми зібрали їх разом — <b>чесно, легально, прозоро</b>.</p>
</section>

<section>
  <div class="eyebrow">Ідея</div>
  <h2>Єдина платформа про номери</h2>
  <p class="lead">Каталог вільних номерів, перевірка будь-якого авто за номером чи VIN, підбір красивих комбінацій і моніторинг їх появи. У базі — <b>усі</b> номери: і зайняті на авто, і вільні для реєстрації. Telegram-бот уже працює, iOS-додаток — у розробці.</p>
  <div class="stats">
    <div class="stat"><b>20M+</b><span>записів реєстру МВС</span></div>
    <div class="stat"><b>25</b><span>областей України</span></div>
    <div class="stat"><b style="color:var(--green)">∞</b><span>комбінацій під полювання</span></div>
  </div>
</section>

<section>
  <div class="eyebrow">Що вміє</div>
  <h2>Функції</h2>
  <div class="grid">
    <div class="tile t1"><div class="ic">🚗</div><h3>Перевірка авто</h3><p>Номер або VIN: марка, рік, паливо, історія реєстрацій, ринкова ціна, статус розшуку.</p></div>
    <div class="tile t2"><div class="ic">🔢</div><h3>Підбір комбінації</h3><p>Вводиш цифри — бачиш, що доступне в продажу, що зайняте на авто, а що ще вільне.</p></div>
    <div class="tile t3"><div class="ic">🔍</div><h3>Каталог номерів</h3><p>Пошук по всій базі: серія, регіон, ціна, красиві та дзеркальні комбінації.</p></div>
    <div class="tile t4"><div class="ic">🔔</div><h3>Моніторинг</h3><p>Постав номер на стеження — сповіщу тієї ж миті, щойно він зʼявиться у продажу.</p></div>
  </div>
</section>

<section>
  <div class="eyebrow">Чесність даних</div>
  <h2>Тільки відкрите. Тільки легальне.</h2>
  <p class="lead">Усе будується виключно на відкритих державних даних (data.gov.ua): реєстр транспортних засобів МВС, база авто в розшуку, відкриті «краєвидні» номери ГСЦ. <b>Жодних зливів баз. Жодних персональних даних.</b> Прозорість — частина ідеї.</p>
</section>

<section>
  <div class="eyebrow">Запрошення</div>
  <h2>Шукаємо дизайнерів і критиків</h2>
  <p class="lead">Зараз це темна тема, українські акценти, градієнтні плитки — міцний фундамент. Але ми хочемо більшого: щоб <b>кожна функція виділялась</b>, кожен екран був приємний оку, а перший дотик — закохував.<br><br>
  Якщо ти дизайнер, критик або просто небайдужий — долучайся. Критикуй гостро, пропонуй сміливо, твори разом. Цей проєкт робиться з душею, і ми раді кожному, хто зробить його красивішим.</p>
  <div class="pills"><span class="pill">🎨 UI / UX</span><span class="pill">🧭 Продукт</span><span class="pill">✍️ Копірайт</span><span class="pill">🧪 Критика</span><span class="pill">📱 iOS / Expo</span></div>
</section>

</div>

<div class="cta wrap">
  <h2>Долучайся до бази</h2>
  <p class="lead" style="max-width:560px;margin:14px auto 0">Спробуй у Telegram уже зараз — і скажи, що зробив би краще.</p>
  <div style="margin-top:24px">
    <a class="btn" href="https://t.me/nomer_na_avto_bot">▶️ Відкрити Telegram-бот</a>
    <a class="btn ghost" href="/open">📱 iOS (Expo Go)</a>
  </div>
</div>

<footer>База Автономерів · зроблено в Україні з 🤍💙💛 · усі дані — відкриті (data.gov.ua)</footer>
</body></html>'''

_FEATURES_HTML = '''<!doctype html><html lang="uk"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>База Автономерів — функціонал і головне меню</title>
<meta property="og:title" content="База Автономерів — функціонал">
<meta property="og:description" content="Детальний опис функцій і головного меню застосунку.">
<meta name="theme-color" content="#0b0e14">
<style>
:root{--bg:#0b0e14;--card:#161b24;--line:#222a36;--text:#f1f4f9;--sub:#8b95a7;--blue:#3b82f6;--green:#22c55e}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:820px;margin:0 auto;padding:0 20px}
.top{padding:64px 0 36px;text-align:center;background:radial-gradient(900px 380px at 50% -10%,rgba(59,130,246,.22),transparent 60%)}
.top h1{font-size:clamp(32px,7vw,52px);font-weight:900;letter-spacing:-1px}
.top p{margin-top:14px;color:#cdd6e6;font-size:clamp(16px,3.2vw,19px)}
.toc{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:26px}
.toc a{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:7px 14px;font-size:13px;color:#cdd6e6;text-decoration:none}
section{padding:42px 0;border-top:1px solid var(--line)}
.eyebrow{color:var(--blue);font-weight:800;letter-spacing:2px;text-transform:uppercase;font-size:12px;margin-bottom:12px}
h2{font-size:clamp(24px,5vw,34px);font-weight:900;letter-spacing:-.5px;margin-bottom:14px}
h3{font-size:20px;font-weight:800;margin:18px 0 6px}
p{color:#d4dbe8}.muted{color:var(--sub);font-size:14px}
.menu{display:grid;grid-template-columns:1fr;gap:12px;margin-top:18px}
.mi{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
.mi .dot{width:46px;height:46px;border-radius:13px;flex:none;display:flex;align-items:center;justify-content:center;font-size:22px;color:#fff}
.mi h4{font-size:16px;font-weight:800}.mi p{font-size:13.5px;margin-top:3px;color:#cdd6e6}
.b1{background:linear-gradient(135deg,#3b82f6,#1d4ed8)}.b2{background:linear-gradient(135deg,#8b5cf6,#6d28d9)}
.b3{background:linear-gradient(135deg,#14b8a6,#0f766e)}.b4{background:linear-gradient(135deg,#f59e0b,#b45309)}
.b5{background:linear-gradient(135deg,#0ea5e9,#0369a1)}.b6{background:linear-gradient(135deg,#ec4899,#9d174d)}
.steps{list-style:none;margin:12px 0 0;padding:0}
.steps li{position:relative;padding:8px 0 8px 30px;color:#d4dbe8;font-size:15px;border-bottom:1px solid var(--line)}
.steps li:before{content:"";position:absolute;left:6px;top:15px;width:8px;height:8px;border-radius:50%;background:var(--blue)}
.badge{display:inline-block;font-size:12px;font-weight:700;padding:3px 9px;border-radius:8px;margin:2px 4px 2px 0}
.bg{background:rgba(34,197,94,.16);color:#86efac}.br{background:rgba(239,68,68,.16);color:#fca5a5}.bw{background:rgba(148,163,184,.16);color:#cbd5e1}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin-top:14px}
footer{color:var(--sub);font-size:13px;text-align:center;padding:36px 0 50px;border-top:1px solid var(--line)}
a.lnk{color:var(--blue);text-decoration:none}
</style></head><body>

<div class="top"><div class="wrap">
  <h1>Функціонал проєкту</h1>
  <p>Детальний опис можливостей і головного меню «Бази Автономерів».</p>
  <div class="toc">
    <a href="#menu">Головне меню</a><a href="#check">Перевірка авто</a><a href="#combo">Підбір комбінації</a>
    <a href="#catalog">Каталог</a><a href="#monitor">Моніторинг</a><a href="#data">Дані</a>
  </div>
</div></div>

<div class="wrap">

<section id="menu">
  <div class="eyebrow">Головне меню</div>
  <h2>Дашборд «База Автономерів»</h2>
  <p>Головний екран — це вітрина всієї бази. Зверху — бренд і підзаголовок «усі номери України, зайняті й вільні». Далі три показники бази, шість функціональних плиток (кожна з власним градієнтом), а нижче — добірки й популярні комбінації.</p>

  <h3>Показники бази</h3>
  <p class="muted">📦 20M+ — записів у реєстрі МВС · 📋 каталог — відстежувані номери ГСЦ · 🟢 у продажу — доступні зараз для реєстрації.</p>

  <h3>Шість плиток (пункти меню)</h3>
  <div class="menu">
    <div class="mi"><div class="dot b1">🚗</div><div><h4>Перевірка авто</h4><p>Пробити номер або VIN: марка, рік, паливо, історія реєстрацій, ринкова ціна, статус розшуку та доступність для реєстрації.</p></div></div>
    <div class="mi"><div class="dot b2">🔢</div><div><h4>Підбір за комбінацією</h4><p>Ввести цифри й побачити три групи: доступні в продажу, зайняті на авто, вільні для полювання.</p></div></div>
    <div class="mi"><div class="dot b3">🔍</div><div><h4>Каталог номерів</h4><p>Пошук по всій базі за серією, регіоном, типом ТЗ, ціною; маски та красиві комбінації.</p></div></div>
    <div class="mi"><div class="dot b4">🔔</div><div><h4>Моніторинг</h4><p>Поставити номер/серію на стеження — сповіщення тієї ж миті, щойно зʼявиться у продажу.</p></div></div>
    <div class="mi"><div class="dot b5">📰</div><div><h4>Нові / зниклі</h4><p>Стрічка змін: що зʼявилось і що зникло з продажу за добу / тиждень / місяць.</p></div></div>
    <div class="mi"><div class="dot b6">⭐</div><div><h4>Обрані</h4><p>Збережені номери — швидкий доступ до тих, що сподобались.</p></div></div>
  </div>

  <h3>Нижче на головній</h3>
  <p class="muted">✨ Добірки красивих (однакові цифри, дзеркальні, пари, круглі, низькі) · 🔥 Популярні комбінації (за кількістю обраних/полювань).</p>

  <h3>Нижня навігація (таби)</h3>
  <p class="muted">🏠 Головна · 🔢 Комбінація · 🚗 Перевірка · ⭐ Обране · 👤 Профіль (синхронізація з Telegram).</p>
</section>

<section id="check">
  <div class="eyebrow">Функція</div>
  <h2>🚗 Перевірка авто</h2>
  <p>Вводиш номер або VIN — і одразу бачиш повну, але зрозумілу картку. Без зайвого — деталі ховаються за кнопками.</p>
  <div class="card">
    <p><b>Статуси (завжди явно):</b></p>
    <p><span class="badge bg">✅ зареєстрований</span><span class="badge br">⚠️ знятий з обліку</span><span class="badge bw">⚪ ніколи не реєструвався</span></p>
    <p style="margin-top:8px"><b>+ доступність:</b> «🏷 Доступний для реєстрації зараз: ТАК / НІ» — з ціною та датою підтвердження.</p>
  </div>
  <ul class="steps">
    <li><b>📍 Регіон</b> — визначається за літерами номера (Додаток 4 наказу МВС).</li>
    <li><b>📋 Держреєстрація</b> — марка, модель, рік, обʼєм, паливо, колір, VIN, остання операція.</li>
    <li><b>💵 Ринкова ціна</b> — середня з AutoRia (медіана + діапазон + кількість оголошень).</li>
    <li><b>🔢 Історія номера</b> — усі авто, що були на цьому номері.</li>
    <li><b>🚙 Історія авто</b> — уся історія по VIN (усі номери цього авто).</li>
    <li><b>🚨 Розшук</b> — звірка з базою авто в розшуку МВС.</li>
    <li><b>🔗 Офіційні джерела</b> — швидкі переходи: AutoRia, ОСАГО (МТСБУ), обтяження (Мінюст).</li>
  </ul>
</section>

<section id="combo">
  <div class="eyebrow">Функція</div>
  <h2>🔢 Підбір за комбінацією</h2>
  <p>Серце ідеї — обʼєднання двох баз. Вводиш 4 цифри (напр. 0100) і отримуєш повну картину по цій комбінації.</p>
  <ul class="steps">
    <li><span class="badge bg">🟢 В продажу</span> — номери з цією комбінацією, доступні зараз у ГСЦ.</li>
    <li><span class="badge br">🔴 На авто</span> — уже зареєстровані; тап → дані авто.</li>
    <li><span class="badge bw">⚪ Вільні</span> — валідні за постановою, але ще не зустрічались → постав полювання.</li>
    <li><b>Фільтри</b> Тип ТЗ + Регіон діють на всі три групи разом.</li>
    <li><b>Вибір серії</b> — спершу обираєш літеросполуку регіону (усі коди за Додатком 4, включно з резервними), потім бачиш повні номери цієї серії.</li>
  </ul>
</section>

<section id="catalog">
  <div class="eyebrow">Функція</div>
  <h2>🔍 Каталог номерів</h2>
  <p>Пошук по всій базі доступних номерів.</p>
  <ul class="steps">
    <li>За <b>цифрами або маскою</b> (1234, 1**4, 7777).</li>
    <li>За <b>серією / літерами</b>, <b>регіоном</b>, <b>типом ТЗ</b>, <b>ціною</b>.</li>
    <li><b>Слово на номері</b> — перші + останні літери разом (напр. СЕ****КС).</li>
    <li><b>Добірки</b> красивих і <b>популярні</b> комбінації.</li>
    <li>Тап по номеру → картка з усіма деталями та статусом.</li>
  </ul>
</section>

<section id="monitor">
  <div class="eyebrow">Функція</div>
  <h2>🔔 Моніторинг · 📰 Стрічка · ⭐ Обрані</h2>
  <ul class="steps">
    <li><b>Моніторинг</b> — постав номер/серію/комбінацію на стеження; миттєве сповіщення про появу. Працює і без Telegram.</li>
    <li><b>Нові / зниклі</b> — що зʼявилось і зникло з продажу за добу/тиждень/місяць.</li>
    <li><b>Обрані</b> — збережені номери; синхронізуються між ботом і додатком.</li>
  </ul>
</section>

<section id="data">
  <div class="eyebrow">Основа</div>
  <h2>Дані та платформи</h2>
  <p><b>Джерела (тільки відкриті, data.gov.ua):</b> реєстр транспортних засобів МВС (20+ млн записів), база авто в розшуку, відкриті номери ГСЦ. Без зливів, без персональних даних.</p>
  <p style="margin-top:10px"><b>Платформи:</b> Telegram-бот (працює) · iOS-додаток (Expo, у розробці) · веб (далі).</p>
  <p class="muted" style="margin-top:14px">Більше про ідею та душу проєкту — на сторінці <a class="lnk" href="/pitch">/pitch</a>.</p>
</section>

</div>
<footer>База Автономерів · функціонал станом на цю версію · зроблено в Україні 🇺🇦</footer>
</body></html>'''


@app.get("/open", response_class=HTMLResponse)
async def open_app() -> str:
    """A tappable page that redirects into Expo Go (open in the phone's browser).

    The Expo tunnel URL rotates on every dev-server restart (anonymous ngrok), so we
    read it from DB meta `expo_url` (updatable instantly from the Mac, no code deploy).
    """
    expo_url = (await db.get_meta("expo_url")) or EXPO_URL
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Моніторинг Автономерів</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;
text-align:center;padding:40px 20px}}a.btn{{display:inline-block;background:#3b82f6;color:#fff;
text-decoration:none;padding:16px 28px;border-radius:14px;font-size:18px;font-weight:700;margin-top:24px}}
p{{color:#9aa4b2}}</style></head><body>
<h2>🇺🇦 Моніторинг Автономерів</h2>
<p>Натисни кнопку, щоб відкрити застосунок у Expo Go:</p>
<a class="btn" href="{expo_url}">▶️ Відкрити в Expo Go</a>
<p style="margin-top:30px;font-size:13px">Якщо не відкрилось — встанови «Expo Go» з App Store і натисни ще раз.</p>
<script>setTimeout(function(){{window.location.href="{expo_url}";}}, 600);</script>
</body></html>"""


@app.get("/pitch", response_class=HTMLResponse)
async def pitch() -> str:
    """Публічна сторінка-презентація проєкту (для дизайнерів, критиків, інвесторів)."""
    return _PITCH_HTML


@app.get("/features", response_class=HTMLResponse)
async def features() -> str:
    """Детальна презентація функціоналу + опис головного меню."""
    return _FEATURES_HTML


_BOT_URL = "https://t.me/nomer_na_avto_bot"

_LANDING_HTML = '''<!doctype html><html lang="uk"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Перевірка авто за номером і VIN · Підбір красивих номерів — База Автономерів України</title>
<meta name="description" content="Перевірка авто за номером або VIN: марка, рік, історія реєстрацій, розшук, ринкова ціна. Підбір і моніторинг красивих та вільних номерів ГСЦ МВС по всій Україні. Безкоштовно в Telegram.">
<meta name="keywords" content="перевірка авто за номером, перевірити авто за VIN, історія авто, вільні номери, красиві номери, номери ГСЦ МВС, підбір номера, база автономерів, перевірка номера, автономер Україна">
<link rel="canonical" href="https://34.123.136.171.nip.io/">
<meta property="og:type" content="website">
<meta property="og:title" content="База Автономерів України — перевірка авто за номером і підбір красивих номерів">
<meta property="og:description" content="Перевір будь-яке авто за номером/VIN і знайди номер мрії. Безкоштовно в Telegram.">
<meta property="og:locale" content="uk_UA">
<meta name="robots" content="index,follow">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0b0e14;color:#eef1f6;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.wrap{max-width:880px;margin:0 auto;padding:0 18px}
.hero{background:radial-gradient(1200px 500px at 50% -10%,#15366e 0%,#0b0e14 60%);padding:64px 0 48px;text-align:center}
.brand{display:inline-flex;align-items:center;gap:8px;font-weight:900;letter-spacing:.5px;color:#cfe0ff;margin-bottom:18px}
.badge{background:#ffd700;color:#0b0e14;border-radius:6px;padding:2px 7px;font-size:13px;font-weight:900}
h1{font-size:34px;font-weight:900;line-height:1.18;margin-bottom:14px}
h1 span{color:#5b9bff}
.sub{color:#aab6c8;font-size:17px;max-width:620px;margin:0 auto 28px}
.cta{display:inline-flex;align-items:center;gap:9px;background:linear-gradient(135deg,#2f80ff,#1456d8);color:#fff;font-weight:800;font-size:17px;padding:15px 28px;border-radius:14px;box-shadow:0 10px 30px rgba(47,128,255,.35)}
.cta.ghost{background:#161b24;box-shadow:none;border:1px solid #222a36}
.ctas{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.stats{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:34px}
.stat{background:#11161f;border:1px solid #1d2430;border-radius:14px;padding:14px 18px;min-width:140px}
.stat b{display:block;font-size:24px;color:#5b9bff}
.stat span{color:#8b95a7;font-size:13px}
section{padding:46px 0;border-top:1px solid #141a23}
h2{font-size:24px;font-weight:800;margin-bottom:18px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:#11161f;border:1px solid #1d2430;border-radius:16px;padding:18px}
.card h3{font-size:17px;margin-bottom:7px;color:#eaf0fa}
.card p{color:#9aa4b2;font-size:14.5px}
.steps{counter-reset:s;display:grid;gap:12px}
.step{background:#11161f;border:1px solid #1d2430;border-radius:14px;padding:16px 16px 16px 56px;position:relative}
.step:before{counter-increment:s;content:counter(s);position:absolute;left:16px;top:14px;width:28px;height:28px;border-radius:50%;background:#2f80ff;color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center}
.faq dt{font-weight:700;margin-top:16px;color:#eaf0fa}
.faq dd{color:#9aa4b2;font-size:14.5px;margin-top:4px}
footer{padding:34px 0 50px;color:#6b7585;font-size:13px;text-align:center;border-top:1px solid #141a23}
.center{text-align:center;margin-top:26px}
@media(max-width:620px){h1{font-size:27px}.cards{grid-template-columns:1fr}}
</style>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
{"@type":"WebSite","name":"База Автономерів України","url":"https://34.123.136.171.nip.io/","inLanguage":"uk"},
{"@type":"SoftwareApplication","name":"База Автономерів — перевірка авто та підбір номерів","applicationCategory":"UtilitiesApplication","operatingSystem":"Telegram, iOS","offers":{"@type":"Offer","price":"0","priceCurrency":"UAH"}},
{"@type":"FAQPage","mainEntity":[
{"@type":"Question","name":"Як перевірити авто за номером?","acceptedAnswer":{"@type":"Answer","text":"Введіть державний номер або VIN у боті — отримаєте марку, модель, рік, обʼєм, паливо, колір, історію реєстрацій та перевірку на розшук. Дані з відкритого реєстру МВС."}},
{"@type":"Question","name":"Це безкоштовно?","acceptedAnswer":{"@type":"Answer","text":"Так, базова перевірка та пошук номерів — безкоштовні в Telegram-боті."}},
{"@type":"Question","name":"Як знайти або відстежити красивий номер?","acceptedAnswer":{"@type":"Answer","text":"Оберіть серію, регіон, цифри чи ціну — бот покаже доступні номери ГСЦ МВС. Якщо потрібного зараз немає, увімкніть моніторинг і отримаєте сповіщення, щойно він зʼявиться."}},
{"@type":"Question","name":"Звідки беруться дані?","acceptedAnswer":{"@type":"Answer","text":"Лише з відкритих джерел: портал доступних номерів ГСЦ МВС і відкритий реєстр транспортних засобів МВС (data.gov.ua), деперсоналізовано."}}
]}]}
</script>
</head><body>

<div class="hero"><div class="wrap">
<div class="brand">🇺🇦 NOMER <span class="badge">DB</span> · База Автономерів України</div>
<h1>Перевірка авто за номером і VIN.<br><span>Підбір красивих номерів.</span></h1>
<p class="sub">Дізнайся все про авто за держномером або VIN — марка, рік, історія, розшук, ринкова ціна. Знайди і відстеж номер мрії серед усіх номерів ГСЦ МВС по Україні. Безкоштовно в Telegram.</p>
<div class="ctas">
<a class="cta" href="''' + _BOT_URL + '''">🚀 Відкрити в Telegram</a>
<a class="cta ghost" href="#how">Як це працює</a>
</div>
<div class="stats">
<div class="stat"><b>20&nbsp;млн+</b><span>записів реєстру МВС</span></div>
<div class="stat"><b>{{AVAIL}}</b><span>номерів у продажу</span></div>
<div class="stat"><b>25</b><span>регіонів України</span></div>
</div>
</div></div>

<section><div class="wrap">
<h2>🚗 Перевірка авто за номером або VIN</h2>
<div class="cards">
<div class="card"><h3>Марка, модель, рік</h3><p>Повні технічні дані: обʼєм, паливо, колір, тип кузова — за держномером або VIN-кодом.</p></div>
<div class="card"><h3>Історія реєстрацій</h3><p>Коли і де авто реєстрували та перереєстровували — видно «біографію» машини перед купівлею.</p></div>
<div class="card"><h3>Перевірка на розшук</h3><p>Чи не перебуває авто в розшуку — за відкритим датасетом МВС (78&nbsp;000+ авто).</p></div>
<div class="card"><h3>Ринкова ціна</h3><p>Орієнтовна вартість на ринку (за даними оголошень) — щоб не переплатити.</p></div>
</div>
</div></section>

<section><div class="wrap">
<h2>✨ Підбір і моніторинг номерів</h2>
<div class="cards">
<div class="card"><h3>Красиві комбінації</h3><p>Однакові, дзеркальні, пари, круглі, низькі — добірки найгарніших вільних номерів.</p></div>
<div class="card"><h3>Пошук за параметрами</h3><p>Серія, регіон, цифри, ціна, навіть «слово на номері» (перші+останні літери).</p></div>
<div class="card"><h3>Моніторинг</h3><p>Немає потрібного зараз? Увімкни стеження — сповістимо, щойно номер зʼявиться у продажу.</p></div>
<div class="card"><h3>Уся база</h3><p>Зайняті, вільні та номери у продажу по всіх 25 регіонах — навіть архів зниклих.</p></div>
</div>
</div></section>

<section id="how"><div class="wrap">
<h2>Як це працює</h2>
<div class="steps">
<div class="step"><b>Відкрий бота</b> в Telegram — нічого встановлювати не треба.</div>
<div class="step"><b>Введи номер або VIN</b> — для перевірки авто; або обери серію/регіон — для підбору номера.</div>
<div class="step"><b>Отримай результат</b> миттєво. Постав моніторинг — і лови потрібний номер першим.</div>
</div>
<div class="center"><a class="cta" href="''' + _BOT_URL + '''">🚀 Спробувати безкоштовно</a></div>
</div></section>

<section class="faq"><div class="wrap">
<h2>Часті питання</h2>
<dl>
<dt>Як перевірити авто за номером?</dt>
<dd>Введіть держномер або VIN у боті — отримаєте марку, модель, рік, історію реєстрацій і перевірку на розшук. Дані з відкритого реєстру МВС.</dd>
<dt>Це безкоштовно?</dt>
<dd>Так, базова перевірка авто й пошук номерів — безкоштовні.</dd>
<dt>Як знайти красивий номер?</dt>
<dd>Оберіть серію, регіон, цифри або ціну — бот покаже доступні номери ГСЦ МВС. Немає потрібного — увімкніть моніторинг.</dd>
<dt>Звідки дані?</dt>
<dd>Лише відкриті джерела: портал доступних номерів ГСЦ МВС і відкритий реєстр МВС (data.gov.ua), деперсоналізовано. Жодних зливів чи персональних даних.</dd>
</dl>
</div></section>

<footer><div class="wrap">
База Автономерів України · перевірка авто за номером і VIN, підбір та моніторинг номерів ГСЦ МВС.<br>
<a href="''' + _BOT_URL + '''" style="color:#5b9bff">@nomer_na_avto_bot</a> · Дані з відкритих джерел МВС (data.gov.ua)
</div></footer>

</body></html>'''


@app.get("/", response_class=HTMLResponse)
async def landing() -> str:
    """SEO landing page (Ukrainian) — captures search traffic, converts to the Telegram bot."""
    try:
        s = await db.get_stats()
        avail = f"{int(s.get('available') or 0):,}".replace(",", " ")
    except Exception:  # noqa: BLE001
        avail = "260 000+"
    return _LANDING_HTML.replace("{{AVAIL}}", avail)


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> str:
    """Allow crawling + point to the sitemap."""
    return "User-agent: *\nAllow: /\nSitemap: https://34.123.136.171.nip.io/sitemap.xml\n"


@app.get("/sitemap.xml")
async def sitemap() -> Response:
    """Minimal sitemap for the public pages."""
    base = "https://34.123.136.171.nip.io"
    items = "".join(f"<url><loc>{base}{u}</loc></url>" for u in ("/", "/features", "/pitch"))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + items + '</urlset>')
    return Response(content=xml, media_type="application/xml")


@app.get("/stats")
async def stats() -> dict:
    """Aggregate stats + today's new/removed counts."""
    s = await db.get_stats()
    s["new_today"] = await db.feed_count("new", "day")
    s["removed_today"] = await db.feed_count("removed", "day")
    return s


@app.get("/meta")
async def meta(region: Optional[str] = None, vehicle_type: Optional[str] = None) -> dict:
    """Filter options: regions, vehicle types, series and prices (optionally scoped)."""
    return {
        "regions": await db.distinct_regions(),
        "vehicle_types": await db.distinct_vehicle_types(),
        "series": await db.distinct_series(region=region, vehicle_type=vehicle_type),
        "prices": await db.distinct_prices(region=region, vehicle_type=vehicle_type),
        "collections": [{"key": k, "label": v} for k, v in db.COLLECTIONS.items()],
    }


@app.get("/search")
async def search(
    query: Optional[str] = None,
    region: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    series: Optional[str] = None,
    price: Optional[float] = None,
    collection: Optional[str] = None,
    page: int = Query(0, ge=0),
) -> dict:
    """Filtered, paginated plate search."""
    kw = dict(
        query=query, region=region, vehicle_type=vehicle_type, letters_start=series,
        price_min=price, price_max=price, collection=collection,
    )
    total = await db.count_filtered(**kw)
    items = await db.search_filtered(limit=PAGE, offset=page * PAGE, **kw)
    return {"total": total, "page": page, "page_size": PAGE, "items": items}


@app.get("/feed")
async def feed(
    kind: str = "new",
    period: str = "day",
    region: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    page: int = Query(0, ge=0),
) -> dict:
    """New / removed plate events within a period."""
    total = await db.feed_count(kind, period, region, vehicle_type)
    items = await db.feed(kind, period, region, vehicle_type, limit=PAGE, offset=page * PAGE)
    return {"total": total, "page": page, "page_size": PAGE, "items": items}


@app.get("/collections")
async def collections() -> list:
    """Curated collections with counts (cached in the DB layer)."""
    return await db.collection_counts()


@app.get("/popular")
async def popular(limit: int = 15) -> list:
    """Most-favorited digit combinations."""
    return await db.popular_combos(limit)


@app.get("/plate/{plate}")
async def plate_detail(plate: str) -> dict:
    """Full details of one plate number (all locations + popularity)."""
    from local.plate import parse_plate

    locs = await db.plate_locations(plate)
    digits = parse_plate(plate).get("digits")
    return {
        "plate_number": plate,
        "locations": locs,
        "favorites_combo": await db.favorites_combo_count(digits),
        "hunts_combo": await db.hunts_combo_count(digits),
    }


@app.post("/ingest")
async def ingest(request: Request) -> dict:
    """Receive scraped data from the Mac scraper and persist it (secret-protected).

    Body: {secret, rows: [...], ok_scopes: [[region, type], ...]}.
    The scraper runs on a residential IP (Akamai); the server only stores + notifies.
    """
    if not config.INGEST_SECRET:
        raise HTTPException(503, "ingest disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local.persist import apply_scan, notify_new

    rows = body.get("rows") or []
    ok_scopes = {(s[0], s[1]) for s in (body.get("ok_scopes") or [])}
    applied = await apply_scan(rows, ok_scopes, source=body.get("source") or "opendata-exe")
    notified = await notify_new(applied["new_ids"])
    return {
        "scraped": applied["scraped"], "new": len(applied["new_ids"]),
        "removed": applied["removed"], "notified": notified,
    }


# Bookmarklet body (single IIFE). Placeholders @SRV@/@SEC@/@MAP@/@REGS@/@TYPES@ filled per request;
# newlines are collapsed to spaces before serving (a javascript: URL must be one line).
_COLLECTOR_JS = """
(function(){try{
var S='@SRV@',K='@SEC@';
var rs=document.querySelector('#region');
var reg=rs&&rs.selectedIndex>=0?rs.options[rs.selectedIndex].text.trim():'';
var form=rs?rs.closest('form'):document.querySelector('form');
if(!form){alert('Спершу обери регіон + Весь регіон + тип і натисни ПЕРЕГЛЯНУТИ.');return;}
var p=[],es=form.querySelectorAll('input,select,textarea');
for(var i=0;i<es.length;i++){var e=es[i];if(!e.name){continue;}if((e.type=='checkbox'||e.type=='radio')&&!e.checked){continue;}if(e.type=='submit'||e.type=='button'){continue;}p.push(encodeURIComponent(e.name)+'='+encodeURIComponent(e.value));}
var sb=form.querySelector('input[type=submit],button[type=submit]');if(sb&&sb.name){p.push(encodeURIComponent(sb.name)+'='+encodeURIComponent(sb.value||''));}
var act=form.getAttribute('action')||location.href,m=(form.getAttribute('method')||'POST').toUpperCase();
var u=act,o={method:m,credentials:'include'};
if(m=='GET'){u=act+(act.indexOf('?')<0?'?':'&')+p.join('&');}else{o.headers={'Content-Type':'application/x-www-form-urlencoded'};o.body=p.join('&');}
alert('Збираю повний список регіону, зачекай кілька секунд…');
fetch(u,o).then(function(r){return r.text();}).then(function(t){var f=document.createElement('form');f.method='POST';f.action=S+'/collect-html';f.target='_blank';f.acceptCharset='UTF-8';function a(n,v){var x=document.createElement('input');x.type='hidden';x.name=n;x.value=v;f.appendChild(x);}a('secret',K);a('region',reg);a('html',t);document.body.appendChild(f);f.submit();}).catch(function(e){alert('Помилка збору: '+e);});
}catch(e){alert('Помилка: '+e.message);}})();
"""

_COLLECTOR_PAGE = """<!doctype html><html lang=uk><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Збирач для iPhone</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;margin:0;padding:18px;line-height:1.5}
h2{margin:0 0 12px}code{background:#1b1f27;padding:1px 5px;border-radius:5px}
textarea{width:100%;height:120px;background:#1b1f27;color:#9be7a0;border:1px solid #333;border-radius:10px;padding:10px;font-family:ui-monospace,monospace;font-size:11px}
button{background:#3b82f6;color:#fff;border:0;border-radius:10px;padding:11px 16px;font-weight:700;font-size:15px;margin-top:10px}
ol{padding-left:20px}li{margin-bottom:8px}.box{background:#161a22;border:1px solid #232834;border-radius:14px;padding:16px;margin-bottom:16px}</style>
</head><body>
<h2>📲 Збирач номерів для iPhone (Safari)</h2>
<div class=box>
<b>1. Скопіюй код закладки:</b>
<textarea id=bm readonly>@BMTEXT@</textarea>
<button onclick="var t=document.getElementById('bm');t.select();try{navigator.clipboard.writeText(t.value);}catch(e){document.execCommand('copy');}this.textContent='✅ Скопійовано';">📋 Скопіювати код</button>
</div>
<div class=box>
<b>2. Створи закладку в Safari</b>
<ol>
<li>Відкрий будь-яку сторінку → кнопка «Поділитися» <code>⬆️</code> → <b>Додати закладку</b> → Зберегти.</li>
<li>Відкрий <b>Закладки</b> → <b>Змінити</b> → обери цю нову закладку.</li>
<li>Назву постав, напр., <code>📤 Зібрати номери</code>, а в полі <b>адреси</b> зітри все і <b>встав скопійований код</b>. Готово.</li>
</ol>
</div>
<div class=box>
<b>3. Як користуватись</b>
<ol>
<li>Зайди на <code>opendata.hsc.gov.ua/check-leisure-license-plates</code>.</li>
<li>Обери <b>регіон</b> → <b>Весь регіон</b> → тип <b>(будь-який / усі)</b> → натисни <b>ПЕРЕГЛЯНУТИ</b>.</li>
<li>Коли зʼявиться таблиця з номерами — відкрий закладку <b>📤 Зібрати номери</b> (через адресний рядок або меню закладок).</li>
<li>У віконці перевір <b>регіон</b> і натисни <b>Відправити</b>. Дата збору фіксується автоматично, база оновлюється саме по цьому регіону.</li>
</ol>
</div>
<p style="color:#9aa4b2;font-size:13px">Тип ТЗ визначається автоматично по серії номера. Відстежуються і нові, і зниклі номери в межах регіону.</p>
</body></html>"""


# ── iPhone/Safari bookmarklet collector (manual per-region opendata harvest) ──
# Canonical region names (must match the `plates.region` values in the DB so removals reconcile).
_COLLECT_REGIONS = [
    "Вінницька", "Волинська", "Дніпропетровська", "Донецька", "Житомирська", "Закарпатська",
    "Запорізька", "Івано-Франківська", "Київська", "Кіровоградська", "Луганська", "Львівська",
    "Миколаївська", "Одеська", "Полтавська", "Рівненська", "Сумська", "Тернопільська",
    "Харківська", "Херсонська", "Хмельницька", "Черкаська", "Чернівецька", "Чернігівська", "м. Київ",
]
_COLLECT_TYPES = ["Легковий, вантажний", "Електромобіль", "Причіп", "Мотоцикл", "Електромотоцикл"]


async def _series_type_map() -> dict:
    """Majority series→vehicle_type map from the live DB (so the bookmarklet stays current)."""
    try:
        async with db.acquire() as con:
            rows = await con.fetch(
                "SELECT right(plate_number,2) AS s, vehicle_type AS t, count(*) AS n "
                "FROM plates WHERE plate_number ~ '..[0-9]{4}..$' GROUP BY 1,2")
    except Exception:  # noqa: BLE001
        return {}
    best: dict = {}
    for r in rows:
        s, t, n = r["s"], r["t"], r["n"]
        if s not in best or n > best[s][1]:
            best[s] = (t, n)
    return {s: tn[0] for s, tn in best.items()}


# Latin→Cyrillic for plate normalization (opendata prints plates in Latin lookalikes).
_PLATE_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                                "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
import re as _re

_PLATE_RE = _re.compile(r"^\D{2}\d{4}\D{2}$")
_TR_RE = _re.compile(r"<tr[^>]*>(.*?)</tr>", _re.S | _re.I)
_TD_RE = _re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", _re.S | _re.I)
_TAG_RE = _re.compile(r"<[^>]+>")
_NUM_RE = _re.compile(r"[\d.,]+")


def _plate_norm(raw: str) -> str:
    return _re.sub(r"[\s\-]", "", raw or "").strip().upper().translate(_PLATE_LAT2CYR)


# Додаток 5 (офіційний наказ МВС) — серія (КІНЦЕВІ 2 літери) → тип ТЗ. Блоки дослівно з наказу;
# нормалізуємо латиницю→кирилицю під формат нашої бази. Покриває всі 356 офіційних комбо.
_D5_BLOCKS = {
    "Легковий, вантажний": (
        "ААВАСАЕАНАІА КАМАРАТАХАОО АВВВСВЕВНВ ІВ КВМВРВ ТВХВОР АСВСССЕСНС ІС КСМСРС ТСХСОТ "
        "АЕ ВЕ СЕ ЕЕ НЕ ІЕ КЕМЕРЕ ТЕ ХЕОХ АНВНСНЕНННІН КНМНРНТНХН АІ ВІ СІ ЕІ НІ ІІ КІ МІ РІ ТІ ХІ "
        "АКВКСКЕКНК ІК ККМКРК ТКХК АМВМСМЕМНМІМКМММРМТМХМ АОВОСОЕОНОІО КОМОРОТОХО "
        "АР ВР СР ЕР НР ІР КР МР РР ТР ХР АТВТ СТ ЕТ НТ ІТ КТМТРТ ТТ ХТ АХВХСХЕХНХІХ КХМХРХТХХХ "
        "ОА ОВ ОС ОЕ ОН ОІ ОК ОМ"),
    "Причіп": "XFXGXJXLXNXRXSXUXVXYXZ FF FR FSFUFVFYFZ СFСGСJ СLСNСRСSСUСY FG FJ FL FN",
    "Електромобіль": (
        "UAUFUGUHUIUJUKULUMUNUOUP URUSUTUUUХUY QAQBQCQDQEQFQGQHQIQJQKQL QMQNQOQPQQQRQSQTQUQХQY "
        "ZAZBZCZDZEZFZGZHZI ZJZKZL ZMZNZOZPZRZSZTZUZVZXZYZZ YAYBYCYDYEYFYGYHYIYJYKYL "
        "YMYNYOYPYRYSYTYUYVYXYYYZ UB UC UD UE"),
    "Мотоцикл": ("JAJBJCJDJE JFJGJH JI JJ JKJL JMJNJOJPJRJS JTJUJVJXJYJZ "
                 "LELFLGLHLI LJLKLLLMLNLOLP LRLSLTLULVLXLYLZ"),
    "Електромотоцикл": ("RARFRGRHRIRJRKRLRMRNRORP RRRSRTRURVRXRYRZ "
                        "SASBSCSDSESFSGSHSI SJSKSL SMSNSOSPSRSSSTSUSVSXSYSZ"),
}


def _build_official_series() -> dict:
    m = {}
    for vt, block in _D5_BLOCKS.items():
        letters = _re.sub(r"\s+", "", block)
        for i in range(0, len(letters) - 1, 2):
            m[letters[i:i + 2].translate(_PLATE_LAT2CYR)] = vt
    return m


OFFICIAL_SERIES = _build_official_series()


def _vtype_server(plate: str, smap: dict) -> str:
    """Vehicle type from the series (last 2 letters) — офіційний Додаток 5, потім запасне правило."""
    s = plate[-2:]
    if s in OFFICIAL_SERIES:
        return OFFICIAL_SERIES[s]
    if s in (smap or {}):
        return smap[s]
    a, b = s[:1], s[1:2]
    if a == "F" or (a == "Х" and b in "FGJLNRSUV") or (a == "С" and b in "FGJLNRSUVY"):
        return "Причіп"
    if a in ("J", "L"):
        return "Мотоцикл"
    if a in ("R", "S"):
        return "Електромотоцикл"
    if a in ("U", "Y", "Z", "Q"):
        return "Електромобіль"
    return "Легковий, вантажний"


def _canon_region(label: str) -> str:
    """Map an opendata region label to the canonical DB region name."""
    s = _re.sub(r"\s*область$", "", (label or "").strip(), flags=_re.I).strip()
    low = s.lower().replace(".", "").replace(" ", "")
    if low in ("київ", "мкиїв", "містокиїв"):
        return "м. Київ"
    if s in _COLLECT_REGIONS:
        return s
    for c in _COLLECT_REGIONS:  # tolerate minor wording differences
        if c == s or c.startswith(s) or s.startswith(c):
            return c
    return s


def _parse_plate_html(html: str, smap: dict) -> list:
    """Extract plate rows from an opendata results HTML page (server-side, stdlib only)."""
    def _txt(x: str) -> str:
        return _TAG_RE.sub(" ", x).replace("&nbsp;", " ").replace("&amp;", "&").strip()

    rows, seen = [], set()
    for tr in _TR_RE.findall(html or ""):
        tds = _TD_RE.findall(tr)
        if len(tds) < 3:
            continue
        plate = _plate_norm(_txt(tds[0]))
        if not _PLATE_RE.match(plate):
            continue
        tsc = _txt(tds[2]) or None
        key = (plate, tsc)
        if key in seen:
            continue
        seen.add(key)
        pm = _NUM_RE.search(_txt(tds[1]).replace(" ", ""))
        price = None
        if pm:
            try:
                price = float(pm.group(0).replace(",", "."))
            except ValueError:
                price = None
        rows.append({"plate_number": plate, "price": price, "tsc": tsc,
                     "vehicle_type": _vtype_server(plate, smap)})
    return rows


@app.post("/collect-html")
async def collect_html(request: Request):
    """Receive a RAW opendata results page from the tiny bookmarklet; parse + classify + ingest here.

    Body (JSON or x-www-form-urlencoded): {secret, region, html}. The server extracts all plates
    (full list, not the visible 10/page), assigns vehicle_type by series, and applies the snapshot
    for that region (new + removed). Keeps the bookmarklet tiny enough to fit in a Safari bookmark.
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "collect disabled (no secret configured)")
    ctype = request.headers.get("content-type", "")
    is_form = "application/json" not in ctype
    if is_form:
        from urllib.parse import parse_qs

        raw = (await request.body()).decode("utf-8", "replace")
        q = parse_qs(raw, keep_blank_values=True)
        secret = (q.get("secret") or [""])[0]
        region_label = (q.get("region") or [""])[0]
        html = (q.get("html") or [""])[0]
    else:
        body = await request.json()
        secret = body.get("secret", "")
        region_label = body.get("region", "")
        html = body.get("html", "")
    if secret != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")

    smap = await _series_type_map()
    region = _canon_region(region_label)
    rows = _parse_plate_html(html, smap)
    for r in rows:
        r["region"] = region

    if not rows:
        msg = ("Не знайшов жодного номера в сторінці. Переконайся, що відкрита таблиця "
               "результатів (Регіон → Весь регіон → ПЕРЕГЛЯНУТИ).")
        if is_form:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><body style='font-family:-apple-system,sans-serif;"
                f"padding:28px;background:#0f1115;color:#eef1f6'><h2>⚠️ 0 номерів</h2><p>{msg}</p></body>")
        return {"region": region, "scraped": 0, "new": 0, "removed": 0, "notified": 0, "note": msg}

    from local.persist import apply_scan, notify_new

    present = sorted({r["vehicle_type"] for r in rows})
    ok_scopes = {(region, t) for t in present}
    applied = await apply_scan(rows, ok_scopes, source="opendata-web")
    notified = await notify_new(applied["new_ids"])
    result = {"region": region, "scraped": applied["scraped"], "new": len(applied["new_ids"]),
              "removed": applied["removed"], "notified": notified}
    if not is_form:
        return result
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:-apple-system,sans-serif;padding:28px;background:#0f1115;color:#eef1f6'>"
        f"<h2>✅ {region}</h2>"
        f"<p>Усього в знімку: <b>{result['scraped']}</b><br>Нових: <b>{result['new']}</b><br>"
        f"Знято (зникли): <b>{result['removed']}</b></p>"
        "<p style='color:#9aa4b2'>Можеш закрити цю вкладку і повернутись до сайту.</p></body>")


@app.post("/collect")
async def collect(request: Request):
    """Receive a manually-harvested region snapshot from the iPhone bookmarklet and apply it.

    Accepts JSON (fetch) OR form-encoded `payload` (CSP fallback that posts into a new tab).
    Body: {secret, rows:[{plate_number,price,tsc,region,vehicle_type}], ok_scopes:[[region,type]]}.
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "collect disabled (no secret configured)")
    ctype = request.headers.get("content-type", "")
    is_form = "application/json" not in ctype
    if is_form:
        # Parse x-www-form-urlencoded manually (avoids the python-multipart dependency).
        from urllib.parse import parse_qs

        raw = (await request.body()).decode("utf-8", "replace")
        payload = (parse_qs(raw).get("payload") or ["{}"])[0]
        body = _json.loads(payload)
    else:
        body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local.persist import apply_scan, notify_new

    rows = body.get("rows") or []
    ok_scopes = {(s[0], s[1]) for s in (body.get("ok_scopes") or [])}
    applied = await apply_scan(rows, ok_scopes, source="opendata-web")
    notified = await notify_new(applied["new_ids"])
    result = {"scraped": applied["scraped"], "new": len(applied["new_ids"]),
              "removed": applied["removed"], "notified": notified}
    if not is_form:
        return result
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:-apple-system,sans-serif;padding:28px;background:#0f1115;color:#eef1f6'>"
        f"<h2>✅ Відправлено в базу</h2><p>Регіон оброблено.</p>"
        f"<p>Нових: <b>{result['new']}</b><br>Знято (зникли): <b>{result['removed']}</b><br>"
        f"Усього в знімку: <b>{result['scraped']}</b></p>"
        "<p style='color:#9aa4b2'>Можеш закрити цю вкладку і повернутись до сайту.</p></body>")


@app.get("/collector")
async def collector_page(k: str = ""):
    """Token-gated install page: the iPhone Safari bookmarklet + setup steps (k = app key)."""
    import json as _json

    if not config.API_KEY or k != config.API_KEY:
        raise HTTPException(403, "forbidden")
    smap = await _series_type_map()
    one_line = " ".join(ln.strip() for ln in _COLLECTOR_JS.strip().splitlines())
    srv = (config.SERVER_INGEST_URL.rstrip("/") or "https://34.123.136.171.nip.io").rstrip("/")
    if srv.endswith("/ingest"):
        srv = srv[: -len("/ingest")]
    js = (one_line
          .replace("@SRV@", srv)
          .replace("@SEC@", config.INGEST_SECRET)
          .replace("@MAP@", _json.dumps(smap, ensure_ascii=False))
          .replace("@REGS@", _json.dumps(_COLLECT_REGIONS, ensure_ascii=False))
          .replace("@TYPES@", _json.dumps(_COLLECT_TYPES, ensure_ascii=False)))
    bookmarklet = "javascript:" + js
    page = _COLLECTOR_PAGE.replace("@BM@", _html_attr(bookmarklet)).replace("@BMTEXT@", _html_text(bookmarklet))
    return HTMLResponse(page)


def _html_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _html_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── AutoCheck тестова база НА СЕРВЕРІ (2026, без тунелю) ──
import os as _os
import sqlite3 as _sqlite3
import tempfile as _tempfile
import threading as _threading
import urllib.request as _urlreq

_AC_DB = _os.path.join(_tempfile.gettempdir(), "autocheck_test.db")
# Тестовий дамп: 2025 рік (~2.2 млн рядків) — МАЄ і номер (N_REG_NEW), і VIN, і свіжі авто.
# (Файл 2026 — новий формат — номер прибрали; беремо 2025, де є все.)
_AC_TEST_URL = ("https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/"
                "b7e72d22-55f5-4545-87dc-94e6c8ee03ef/download/reestrtz2025.zip")
_AC_STATUS = {"state": "не завантажено", "rows": 0}


def _ac_iso(v):
    v = (v or "").strip()
    m = _re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", v)
    if not m:
        return None
    y = m.group(3)
    y = "20" + y if len(y) == 2 else y
    return f"{y}-{m.group(2)}-{m.group(1)}"


def _ac_int(v):
    v = (v or "").strip()
    try:
        return int(float(v.replace(",", "."))) if v else None
    except ValueError:
        return None


def _load_autocheck_test():
    """Завантажити дамп МВС 2026 і побудувати локальну SQLite для тесту (фоном)."""
    import csv as _csv
    import io as _io
    import zipfile as _zip

    _AC_STATUS["state"] = "завантаження…"
    try:
        with _tempfile.TemporaryDirectory() as tmp:
            zp = _os.path.join(tmp, "x.zip")
            req = _urlreq.Request(_AC_TEST_URL, headers={"User-Agent": "avtonomera/1.0"})
            with _urlreq.urlopen(req, timeout=900) as r, open(zp, "wb") as fh:
                while True:
                    c = r.read(1 << 20)
                    if not c:
                        break
                    fh.write(c)
            _AC_STATUS["state"] = "заливка…"
            con = _sqlite3.connect(_AC_DB)
            con.execute("DROP TABLE IF EXISTS v")
            con.execute("CREATE TABLE v (vin TEXT, plate TEXT, brand TEXT, model TEXT, make_year INT, "
                        "color TEXT, kind TEXT, body TEXT, fuel TEXT, capacity INT, d_reg TEXT, "
                        "oper_name TEXT, dep TEXT)")
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            OP = "CD.OPER_CODE||'-'||CD.OPERAS"
            INS = "INSERT INTO v VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            total = 0
            with _zip.ZipFile(zp) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                for name in names:
                    batch = []
                    with zf.open(name) as raw:  # stream from zip → не розпаковуємо на диск
                        reader = _csv.DictReader(_io.TextIOWrapper(raw, encoding="utf-8", newline=""), delimiter=";")
                        for row in reader:
                            opn = row.get("OPER_NAME")
                            comb = row.get(OP) or ""
                            if not opn and comb:
                                parts = comb.split(" - ", 1)
                                opn = parts[1] if len(parts) > 1 else comb
                            plate = _plate_norm(row.get("N_REG_NEW")) if row.get("N_REG_NEW") else None
                            batch.append((
                                (row.get("VIN") or "").strip() or None, plate,
                                (row.get("BRAND") or "").strip() or None, (row.get("MODEL") or "").strip() or None,
                                _ac_int(row.get("MAKE_YEAR")), (row.get("COLOR") or "").strip() or None,
                                (row.get("KIND") or "").strip() or None, (row.get("BODY") or "").strip() or None,
                                (row.get("FUEL") or "").strip() or None, _ac_int(row.get("CAPACITY")),
                                _ac_iso(row.get("D_REG")), opn, (row.get("DEP") or "").strip() or None))
                            if len(batch) >= 20000:
                                con.executemany(INS, batch); total += len(batch); batch = []
                    if batch:
                        con.executemany(INS, batch); total += len(batch)
            con.commit()
            con.execute("CREATE INDEX ix_vin ON v(vin)")
            con.execute("CREATE INDEX ix_plate ON v(plate)")
            con.commit()
            con.close()
            _AC_STATUS["state"] = "готово"
            _AC_STATUS["rows"] = total
    except Exception as exc:  # noqa: BLE001
        _AC_STATUS["state"] = f"помилка: {exc}"


# ── Джерело 2: авто в РОЗШУКУ (відкритий датасет МВС, по VIN/номеру) ──
_WANTED_URL = ("https://data.gov.ua/dataset/9b0e87e0-eaa3-4f14-9547-03d61b70abb6/resource/"
               "e43a82da-89e1-4bbb-820c-bd04ab7a0c89/download/carswanted.json")
_WANTED_STATUS = {"state": "не завантажено", "rows": 0}


def _load_wanted():
    """Завантажити список авто в розшуку у таблицю wanted (тієї ж тестової SQLite)."""
    import json as _json

    _WANTED_STATUS["state"] = "завантаження…"
    try:
        req = _urlreq.Request(_WANTED_URL, headers={"User-Agent": "avtonomera/1.0"})
        with _urlreq.urlopen(req, timeout=600) as r:
            data = _json.loads(r.read().decode("utf-8"))
        con = _sqlite3.connect(_AC_DB)
        con.execute("DROP TABLE IF EXISTS wanted")
        con.execute("CREATE TABLE wanted (vin TEXT, plate TEXT, brandmodel TEXT, color TEXT, "
                    "cartype TEXT, seizure TEXT, organ TEXT)")
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        rows = []
        for w in (data or []):
            rows.append((
                (w.get("bodynumber") or "").strip().upper() or None,
                _plate_norm(w.get("vehiclenumber")) if w.get("vehiclenumber") else None,
                (w.get("brandmodel") or "").strip() or None, (w.get("color") or "").strip() or None,
                (w.get("cartype") or "").strip() or None, (w.get("illegalseizuredate") or "")[:10] or None,
                (w.get("organunit") or "").strip() or None))
        con.executemany("INSERT INTO wanted VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        con.execute("CREATE INDEX ix_w_vin ON wanted(vin)")
        con.execute("CREATE INDEX ix_w_plate ON wanted(plate)")
        con.commit()
        con.close()
        _WANTED_STATUS["state"] = "готово"
        _WANTED_STATUS["rows"] = len(rows)
    except Exception as exc:  # noqa: BLE001
        _WANTED_STATUS["state"] = f"помилка: {exc}"


@app.post("/autocheck/load-wanted")
async def autocheck_load_wanted(request: Request) -> dict:
    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    if _WANTED_STATUS["state"] != "завантаження…":
        _threading.Thread(target=_load_wanted, daemon=True).start()
    return {"status": _WANTED_STATUS}


@app.get("/autocheck/wanted-status")
async def autocheck_wanted_status() -> dict:
    return dict(_WANTED_STATUS)


def _ac_lookup_local(plate, vin):
    """Пошук у локальній тестовій SQLite (+ перевірка розшуку). None → бази нема."""
    if not _os.path.exists(_AC_DB):
        return None
    con = _sqlite3.connect(_AC_DB)
    con.row_factory = _sqlite3.Row
    try:
        # рядки без дати — в кінці (найсвіжіші переоформлення часто без D_REG) → last = поточне авто
        _ord = " ORDER BY (d_reg IS NULL), d_reg"
        if plate:
            key_plate, key_vin = _plate_norm(plate), None
            rows = con.execute("SELECT * FROM v WHERE plate=?" + _ord, (key_plate,)).fetchall()
        elif vin:
            key_plate, key_vin = None, (vin or "").strip().upper()
            rows = con.execute("SELECT * FROM v WHERE vin=?" + _ord, (key_vin,)).fetchall()
        else:
            return {"found": False}
        # 🚨 розшук — по тому ж ключу (працює навіть якщо в реєстрі не знайдено)
        wanted = []
        has_w = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wanted'").fetchone()
        if has_w:
            if key_plate:
                wr = con.execute("SELECT * FROM wanted WHERE plate=?", (key_plate,)).fetchall()
            else:
                wr = con.execute("SELECT * FROM wanted WHERE vin=?", (key_vin,)).fetchall()
            wanted = [{"brandmodel": w["brandmodel"], "color": w["color"], "seizure": w["seizure"],
                       "organ": w["organ"]} for w in wr]
    finally:
        con.close()
    if not rows:
        return {"found": False, "wanted": wanted} if wanted else {"found": False}
    last = rows[-1]  # найсвіжіша операція = поточне авто
    veh = {k: last[k] for k in ("vin", "plate", "brand", "model", "make_year", "color", "kind", "body", "fuel", "capacity")}
    dates = [r["d_reg"] for r in rows if r["d_reg"]]
    first_reg = min(dates) if dates else None
    history = [{"d_reg": r["d_reg"], "oper_name": r["oper_name"], "dep": r["dep"], "plate": r["plate"]}
               for r in reversed(rows)]  # найновіше зверху
    res = {"found": True, "vehicle": veh, "first_reg": first_reg, "history": history}
    if wanted:
        res["wanted"] = wanted
    return res


# ── PC-агент через ОПИТУВАННЯ (без тунеля): черга запитів + результати ──
_AC_AGENT = {"seen": 0.0}
_AC_QUEUE: list = []
_AC_RESULTS: dict = {}
_AC_NEXT = [1]
_ac_qlock = _threading.Lock()


def _ac_wanted(plate, vin):
    """Розшук по серверній базі (для агентського результату, який без розшуку)."""
    if not _os.path.exists(_AC_DB):
        return []
    con = _sqlite3.connect(_AC_DB)
    con.row_factory = _sqlite3.Row
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wanted'").fetchone():
            return []
        if plate:
            wr = con.execute("SELECT * FROM wanted WHERE plate=?", (_plate_norm(plate),)).fetchall()
        else:
            wr = con.execute("SELECT * FROM wanted WHERE vin=?", ((vin or "").strip().upper(),)).fetchall()
        return [{"brandmodel": w["brandmodel"], "color": w["color"], "seizure": w["seizure"],
                 "organ": w["organ"]} for w in wr]
    finally:
        con.close()


@app.post("/autocheck/poll")
async def autocheck_poll(request: Request) -> dict:
    """PC-агент довго-опитує чергу запитів (~10с). secret-protected."""
    import asyncio
    import time as _time

    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    _AC_AGENT["seen"] = _time.time()
    for _ in range(40):
        with _ac_qlock:
            if _AC_QUEUE:
                return {"req": _AC_QUEUE.pop(0)}
        await asyncio.sleep(0.25)
    return {"req": None}


@app.post("/autocheck/result")
async def autocheck_result(request: Request) -> dict:
    """PC-агент повертає результат пошуку. secret-protected."""
    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    rid = body.get("id")
    if rid is not None:
        with _ac_qlock:
            _AC_RESULTS[str(rid)] = body.get("result") or {"found": False}
    return {"ok": True}


@app.get("/autocheck/ria-status")
async def autocheck_ria_status() -> dict:
    """AutoRia free-tier usage this month (для контролю ліміту 1000/міс)."""
    used = int((await db.get_meta(f"ria_calls_{_ria_month()}")) or 0)
    return {"month": _ria_month(), "used": used, "budget": _RIA_BUDGET, "left": max(0, _RIA_BUDGET - used)}


@app.get("/autocheck/agent-status")
async def autocheck_agent_status() -> dict:
    """Чи опитував PC-агент сервер нещодавно (для діагностики підключення). Без секрету — лише статус."""
    import time as _time

    seen = _AC_AGENT["seen"]
    ago = (_time.time() - seen) if seen else None
    return {"online": bool(seen and ago is not None and ago < 35),
            "seconds_ago": round(ago, 1) if ago is not None else None}


async def _ac_query_agent(plate=None, vin=None, digits=None, series=None, regions=None):
    """Поставити запит у чергу для PC-агента і дочекатись результату (None при таймауті).

    Або точковий пошук (plate/vin), або «зайняті за комбінацією» (digits + опц. series/regions).
    """
    import asyncio

    with _ac_qlock:
        rid = str(_AC_NEXT[0])
        _AC_NEXT[0] += 1
        req = {"id": rid, "plate": plate or "", "vin": vin or ""}
        if digits:
            req["digits"] = digits
            if series:
                req["series"] = series
            if regions:
                req["regions"] = regions
        _AC_QUEUE.append(req)
    for _ in range(80):  # ~20с
        await asyncio.sleep(0.25)
        with _ac_qlock:
            if rid in _AC_RESULTS:
                return _AC_RESULTS.pop(rid)
    with _ac_qlock:  # таймаут — приберемо з черги
        for i, q in enumerate(_AC_QUEUE):
            if q["id"] == rid:
                _AC_QUEUE.pop(i)
                break
    return None


def _ac_occupied_local(digits, series=None, regions=None, limit=400):
    """Зайняті номери з комбінацією — по локальній тестовій БД (LIKE по номеру)."""
    if not _os.path.exists(_AC_DB) or not (digits or "").strip():
        return []
    con = _sqlite3.connect(_AC_DB)
    con.row_factory = _sqlite3.Row
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v'").fetchone():
            return []
        rows = con.execute(
            "SELECT plate, vin, brand, model, make_year FROM v WHERE plate LIKE ?",
            (f"__{digits}__",)).fetchall()
    finally:
        con.close()
    best = {}
    for r in rows:
        p = r["plate"]
        if not p or len(p) < 8:
            continue
        le = p[6:8]
        ls = p[0:2]
        if series and le not in series:
            continue
        if regions and ls not in regions:
            continue
        best[p] = r  # тестова БД — без надійного порядку, лишаємо останній
    out = [{"plate": p, "vin": r["vin"], "brand": r["brand"], "model": r["model"],
            "make_year": r["make_year"]} for p, r in best.items()]
    out.sort(key=lambda x: x["plate"])
    return out[:limit]


@app.get("/autocheck/occupied")
async def autocheck_occupied(digits: str, series: str = "", regions: str = ""):
    """Зайняті (зареєстровані) номери із заданою комбінацією цифр (+ опц. серія/регіон)."""
    import asyncio
    import time as _time

    digits = (digits or "").strip()
    if not digits.isdigit():
        raise HTTPException(400, "digits required")
    ser = [s for s in series.split(",") if s] or None
    reg = [r for r in regions.split(",") if r] or None
    res = None
    if _AC_AGENT["seen"] and (_time.time() - _AC_AGENT["seen"] < 35):
        res = await _ac_query_agent(digits=digits, series=ser, regions=reg)
    if res is not None and "occupied" in res:
        return {"occupied": res["occupied"]}
    occ = await asyncio.to_thread(_ac_occupied_local, digits, ser, reg)
    return {"occupied": occ}


@app.post("/autocheck/load-test")
async def autocheck_load_test(request: Request) -> dict:
    """Запустити завантаження тестової бази 2026 на сервері (secret-protected)."""
    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    if _AC_STATUS["state"] not in ("завантаження…", "заливка…"):
        _threading.Thread(target=_load_autocheck_test, daemon=True).start()
    return {"status": _AC_STATUS}


@app.get("/autocheck/load-status")
async def autocheck_load_status() -> dict:
    return dict(_AC_STATUS)


@app.post("/autocheck/register")
async def autocheck_register(request: Request) -> dict:
    """The AutoCheck PC-agent registers its current Cloudflare tunnel URL here (secret-protected)."""
    import datetime as dt

    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    url = (body.get("url") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(400, "bad url")
    await db.set_meta("autocheck_url", url)
    await db.set_meta("autocheck_ts", dt.datetime.now(dt.timezone.utc).isoformat())
    return {"ok": True, "url": url}


@app.get("/autocheck/lookup")
async def autocheck_lookup(plate: str = "", vin: str = ""):
    """Proxy a vehicle lookup to the registered PC-agent tunnel (so app/bot use a stable URL).

    Behind the app API key (middleware). Returns {found, vehicle, history} or {found:false}.
    """
    import asyncio
    import time as _time

    if not plate and not vin:
        raise HTTPException(400, "plate or vin required")

    res = None
    # 1) PC-агент (повна база) через опитування — якщо опитував нещодавно.
    if _AC_AGENT["seen"] and (_time.time() - _AC_AGENT["seen"] < 35):
        res = await _ac_query_agent(plate, vin)
    # 2) Інакше — локальна тестова база на сервері.
    if res is None:
        res = await asyncio.to_thread(_ac_lookup_local, plate, vin)
    if res is None:
        return {"found": False, "offline": True, "note": "AutoCheck-агент не підключений"}
    # Долити розшук, якщо результат від агента (він без позначки розшуку).
    if "wanted" not in res:
        w = await asyncio.to_thread(_ac_wanted, plate, vin)
        if w:
            res["wanted"] = w
    # Долити орієнтовну ринкову ціну з AutoRia (по марці/моделі/року; VIN — для точного матчу).
    if res.get("found"):
        v = res.get("vehicle") or {}
        mk = await _autoria_price(v.get("brand"), v.get("model"), v.get("make_year"), v.get("vin"))
        if mk:
            res["market"] = mk
    return res


# ── AutoRia: ринкова ціна + VIN-декодер. Безкоштовний ліміт — 1000 запитів/МІСЯЦЬ,
#    тож усе агресивно кешуємо в БД і рахуємо витрачені запити, щоб не перевищити. ──
_RIA_KEY = [None]
_RIA_UID = [None]
_RIA_MARKS: dict = {}          # norm(brand) -> marka_id (легкові), гарячий кеш у памʼяті
_RIA_MODELS: dict = {}         # marka_id -> {norm(model) -> model_id}
_RIA_TTL_HIT = 30 * 86400      # успішну ціну тримаємо місяць
_RIA_TTL_MISS = 3 * 86400      # промах — 3 дні
_RIA_TTL_DICT = 25 * 86400     # словники марок/моделей — майже статичні
_RIA_BUDGET = 950              # запас < 1000/міс


def _rnorm(s) -> str:
    import re as _re
    return _re.sub(r"[\s\-]", "", (s or "").lower())


def _ria_month() -> str:
    import time as _time
    return _time.strftime("%Y%m")


async def _ria_quota_left() -> int:
    """Скільки безкоштовних запитів AutoRia лишилось цього місяця."""
    n = int((await db.get_meta(f"ria_calls_{_ria_month()}")) or 0)
    return max(0, _RIA_BUDGET - n)


async def _ria_bump(n: int = 1) -> None:
    k = f"ria_calls_{_ria_month()}"
    await db.set_meta(k, str(int((await db.get_meta(k)) or 0) + n))


async def _ria_creds():
    if _RIA_KEY[0] is None:
        _RIA_KEY[0] = (await db.get_meta("autoria_key")) or ""
    if _RIA_UID[0] is None:
        _RIA_UID[0] = (await db.get_meta("autoria_user_id")) or ""
    return _RIA_KEY[0], _RIA_UID[0]


def _ria_get(url):
    import json as _json
    import urllib.request

    with urllib.request.urlopen(url, timeout=8) as r:
        return _json.loads(r.read().decode("utf-8"))


async def _ria_cached_json(meta_key, ttl):
    import json as _json
    import time as _time

    raw = await db.get_meta(meta_key)
    if raw:
        try:
            obj = _json.loads(raw)
            if _time.time() - obj.get("ts", 0) < ttl:
                return obj.get("data")
        except Exception:  # noqa: BLE001
            pass
    return None


async def _ria_store_json(meta_key, data):
    import json as _json
    import time as _time

    try:
        await db.set_meta(meta_key, _json.dumps({"ts": _time.time(), "data": data}))
    except Exception:  # noqa: BLE001
        pass


async def _ria_marks_map():
    """norm(brand)->marka_id; памʼять → БД(25д) → API (1 запит)."""
    import asyncio

    if _RIA_MARKS:
        return _RIA_MARKS
    cached = await _ria_cached_json("ria_marks", _RIA_TTL_DICT)
    if cached:
        _RIA_MARKS.update(cached)
        return _RIA_MARKS
    key, _ = await _ria_creds()
    if not key or await _ria_quota_left() <= 0:
        return _RIA_MARKS
    try:
        data = await asyncio.to_thread(_ria_get, f"https://developers.ria.com/auto/categories/1/marks?api_key={key}")
        await _ria_bump(1)
    except Exception:  # noqa: BLE001
        return _RIA_MARKS
    if not isinstance(data, list):  # помилка/ліміт → не список
        return _RIA_MARKS
    m = {_rnorm(x["name"]): x["value"] for x in data}
    _RIA_MARKS.update(m)
    await _ria_store_json("ria_marks", m)
    return _RIA_MARKS


async def _ria_models_map(mid):
    """norm(model)->model_id для марки; памʼять → БД(25д) → API (1 запит)."""
    import asyncio

    if mid in _RIA_MODELS:
        return _RIA_MODELS[mid]
    cached = await _ria_cached_json(f"ria_models_{mid}", _RIA_TTL_DICT)
    if cached:
        _RIA_MODELS[mid] = cached
        return cached
    key, _ = await _ria_creds()
    if not key or await _ria_quota_left() <= 0:
        return {}
    try:
        data = await asyncio.to_thread(
            _ria_get, f"https://developers.ria.com/auto/categories/1/marks/{mid}/models?api_key={key}")
        await _ria_bump(1)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, list):
        return {}
    md = {_rnorm(x["name"]): x["value"] for x in data}
    _RIA_MODELS[mid] = md
    await _ria_store_json(f"ria_models_{mid}", md)
    return md


async def _autoria_vin_decode(vin):
    """VIN → точні id марки/моделі AutoRia (кеш у БД 30д). 1 запит при промаху кешу."""
    import asyncio
    import json as _json
    import urllib.request

    vin = (vin or "").strip().upper()
    if len(vin) < 8:
        return None
    cached = await _ria_cached_json(f"riavin:{vin}", _RIA_TTL_HIT)
    if cached is not None:
        return cached or None
    key, uid = await _ria_creds()
    if not key or not uid or await _ria_quota_left() <= 0:
        return None

    def work():
        try:
            url = f"https://developers.ria.com/auto/params/by/vin-code/?user_id={uid}&api_key={key}"
            body = _json.dumps({"langId": 4, "period": 365, "params": {"omniId": vin}}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                d = _json.loads(r.read().decode("utf-8"))
            chips = {c.get("entity"): c for c in (d.get("chipsData", {}).get("chips") or [])}
            # ВАЖЛИВО: id у VIN-декодері (нове API) НЕ збігаються з id для average_price
            # (старе API). Тому беремо канонічні НАЗВИ ("Mercedes-Benz", "S-Class") і
            # матчимо їх у старих словниках марок/моделей.
            brand = chips.get("brandId", {}).get("name")
            modelnm = chips.get("modelId", {}).get("name")
            yr = chips.get("year", {}).get("value")
            yv = yr.get("gte") if isinstance(yr, dict) else yr
            if not (brand and modelnm):
                return None
            return {"brand": brand, "model": modelnm, "year": yv}
        except Exception:  # noqa: BLE001
            return None

    data = await asyncio.to_thread(work)
    await _ria_bump(1)
    await _ria_store_json(f"riavin:{vin}", data or {})
    return data


async def _autoria_price(brand, model, year, vin=None):
    """Ринкова ціна авто з AutoRia. Кеш у БД на місяць. Економний під ліміт 1000/міс:
    спершу матч по назві; VIN-декодер — лише коли назва не зматчилась."""
    import asyncio

    if not (brand or "").strip():
        return None
    ck = f"ria:{_rnorm(brand)}:{_rnorm(model)}:{year or ''}"
    # Кеш: успіх тримаємо 30д, промах — лише 3д (потім дозволяємо ретрай).
    import json as _json
    import time as _time

    raw = await db.get_meta(ck)
    if raw:
        try:
            obj = _json.loads(raw)
            data = obj.get("data")
            age = _time.time() - obj.get("ts", 0)
            if data and age < _RIA_TTL_HIT:
                return data
            if not data and age < _RIA_TTL_MISS:
                return None
        except Exception:  # noqa: BLE001
            pass

    key, _ = await _ria_creds()
    if not key or await _ria_quota_left() <= 0:
        return None

    marks = await _ria_marks_map()
    mid = marks.get(_rnorm(brand))
    modid = None
    if mid:
        mm = await _ria_models_map(mid)
        t = _rnorm(model)
        modid = mm.get(t)
        if not modid and t:
            for nm, mi in mm.items():
                if nm and (t.startswith(nm) or nm.startswith(t)):
                    modid = mi
                    break
    if not modid and vin:  # точний матч через VIN-декодер (економно — лише при промаху)
        dec = await _autoria_vin_decode(vin)
        if dec and dec.get("brand"):
            dmid = marks.get(_rnorm(dec["brand"]))
            if dmid:
                mid = dmid
                mm2 = await _ria_models_map(dmid)
                dt = _rnorm(dec.get("model"))
                modid = mm2.get(dt)
                if not modid and dt:
                    for nm, mi in mm2.items():
                        if nm and (dt.startswith(nm) or nm.startswith(dt)):
                            modid = mi
                            break
            year = year or dec.get("year")
    if not (mid and modid) or await _ria_quota_left() <= 0:
        await _ria_store_json(ck, {})  # кешуємо промах (на _RIA_TTL_MISS)
        return None

    def work():
        try:
            url = (f"https://developers.ria.com/auto/average_price?api_key={key}"
                   f"&main_category=1&marka_id={mid}&model_id={modid}")
            if year:
                url += f"&yers={year}"
            d = _ria_get(url)
            if not d.get("total"):
                return None
            pct = d.get("percentiles") or {}

            def num(x):
                try:
                    return round(float(x))
                except (TypeError, ValueError):
                    return None

            return {"mean": num(d.get("arithmeticMean")), "median": num(pct.get("50.0")),
                    "p25": num(pct.get("25.0")), "p75": num(pct.get("75.0")),
                    "total": d.get("total"), "currency": "USD"}
        except Exception:  # noqa: BLE001
            return None

    data = await asyncio.to_thread(work)
    await _ria_bump(1)
    await _ria_store_json(ck, data or {})
    return data


@app.post("/stage")
async def stage(request: Request) -> dict:
    """Receive a collected batch into the staging queue (NOT applied to the DB yet).

    Body: {secret, rows:[...], reset?:bool, done?:bool}. The browser extension sends reset=true
    on the first scope, appends rows per scope, and done=true at the end (which marks the queue
    pending and notifies the admin). The admin then commits it (or it auto-commits after N hours).
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "staging disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    if body.get("reset"):
        open(config.STAGE_PATH, "w", encoding="utf-8").close()
    rows = body.get("rows") or []
    if rows:
        with open(config.STAGE_PATH, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(_json.dumps(r, ensure_ascii=False) + "\n")
    total = 0
    try:
        with open(config.STAGE_PATH, encoding="utf-8") as fh:
            total = sum(1 for _ in fh)
    except FileNotFoundError:
        total = 0
    if body.get("done"):
        import datetime as dt
        await db.set_meta("stage_ts", dt.datetime.now(dt.timezone.utc).isoformat())
        await db.set_meta("stage_pending", "1")
        await db.set_meta("stage_count", str(total))
        await db.set_meta("stage_scopes", _json.dumps(body.get("scopes") or []))
        if config.BOT_TOKEN and config.ADMIN_CHAT_ID:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
            try:
                await bot.send_message(
                    config.ADMIN_CHAT_ID,
                    f"📥 <b>Отримано оновлений список</b>: {total} номерів у черзі.\n"
                    f"Оновити базу? Адмінка → <b>🔄 Оновити базу</b>.\n"
                    f"<i>Якщо не оновити вручну — автоматично за {config.STAGE_AUTOCOMMIT_HOURS} год.</i>",
                )
            except Exception:  # noqa: BLE001
                pass
            finally:
                await bot.session.close()
    return {"staged": len(rows), "total": total}


BOT_USERNAME = "nomer_na_avto_bot"


async def _account(request: Request) -> int:
    """Resolve the linked Telegram chat_id from the X-App-Token header (401 if not linked)."""
    token = request.headers.get("x-app-token", "")
    chat_id = await db.token_chat(token) if token else None
    if not chat_id:
        raise HTTPException(401, "not linked")
    return int(chat_id)


@app.post("/app/link/start")
async def app_link_start() -> dict:
    """Begin linking the app to a Telegram account: returns a code, token and a bot deep-link."""
    import secrets
    code = secrets.token_urlsafe(6)
    token = secrets.token_urlsafe(24)
    await db.link_create(code, token)
    return {"code": code, "token": token,
            "deep_link": f"https://t.me/{BOT_USERNAME}?start=link_{code}"}


@app.get("/app/link/status")
async def app_link_status(code: str) -> dict:
    """Poll whether the link code has been confirmed in the bot."""
    st = await db.link_status(code)
    if not st:
        return {"status": "unknown"}
    return {"status": st.get("status"), "linked": st.get("status") == "linked"}


@app.get("/app/me")
async def app_me(request: Request) -> dict:
    """Return the linked account + its synced favorites/monitorings counts."""
    chat_id = await _account(request)
    user = await db.get_user(chat_id)
    return {
        "chat_id": chat_id,
        "username": (user or {}).get("username"),
        "favorites": len(await db.list_favorites(chat_id)),
        "monitorings": len(await db.list_hunts(chat_id)),
    }


@app.get("/app/favorites")
async def app_favorites(request: Request) -> dict:
    """List the account's favorite plate numbers (synced with the bot)."""
    chat_id = await _account(request)
    return {"items": await db.list_favorites(chat_id)}


@app.post("/app/favorites/toggle")
async def app_fav_toggle(request: Request) -> dict:
    """Add/remove a plate from the account's favorites (synced with the bot)."""
    chat_id = await _account(request)
    body = await request.json()
    plate = (body.get("plate") or "").strip()
    if not plate:
        raise HTTPException(400, "no plate")
    if await db.is_favorite(chat_id, plate):
        await db.remove_favorite(chat_id, plate)
        return {"favorite": False}
    await db.add_favorite(chat_id, plate)
    return {"favorite": True}


@app.post("/app/account/anon")
async def app_account_anon() -> dict:
    """Create an anonymous app account so the app works fully without any Telegram link."""
    import secrets
    token = secrets.token_urlsafe(24)
    chat_id = -(3_000_000_000 + secrets.randbelow(1_000_000_000))  # synthetic, app-only namespace
    await db.ensure_user(chat_id, None)
    await db.create_anon_account(token, chat_id)
    return {"token": token}


@app.get("/app/monitorings")
async def app_monitorings(request: Request) -> dict:
    """List the account's monitorings (hunts) with current match counts."""
    chat_id = await _account(request)
    hunts = await db.list_hunts(chat_id)
    out = []
    for h in hunts:
        out.append({
            "id": h.get("id"), "name": h.get("name") or h.get("pattern"),
            "region": h.get("region"), "vehicle_type": h.get("vehicle_type"),
            "matches": await db.count_hunt_matches(h),
        })
    return {"items": out}


@app.post("/app/monitorings/create")
async def app_monitor_create(request: Request) -> dict:
    """Create a monitoring in-app (independent of Telegram). Body: {query, region, vehicle_type}."""
    from local.plate import to_search_like

    chat_id = await _account(request)
    body = await request.json()
    q = (body.get("query") or "").strip().upper()
    region = body.get("region") or None
    vtype = body.get("vehicle_type") or None
    fields = {"match_type": "filters", "region": region, "vehicle_type": vtype}
    if q:
        mode, pattern = to_search_like(q)
        if mode == "digits":
            if "_" in pattern:
                fields["digits_mask"] = pattern
            else:
                fields["digits_exact"] = pattern
        else:
            from local.plate import normalize_plate
            letters = normalize_plate(q)
            import re
            letters = re.sub(r"\d", "", letters)
            if letters:
                fields["letters_start"] = letters[:2]
    label = q or ((region or "всі") + " · " + (vtype or "всі"))
    fields["pattern"] = label
    fields["name"] = label
    hid = await db.add_hunt(chat_id, fields)
    return {"id": hid, "matches": await db.count_hunt_matches(fields)}


@app.post("/app/monitorings/delete")
async def app_monitor_delete(request: Request) -> dict:
    """Delete one of the account's monitorings."""
    chat_id = await _account(request)
    body = await request.json()
    ok = await db.delete_hunt(chat_id, int(body.get("id")))
    return {"deleted": ok}


@app.post("/app/merge")
async def app_merge(request: Request) -> dict:
    """Merge an old (anonymous) account's data into the current one (on linking)."""
    to_chat = await _account(request)
    body = await request.json()
    old_token = body.get("old_token") or ""
    from_chat = await db.token_chat(old_token) if old_token else None
    if from_chat and from_chat != to_chat:
        await db.merge_account(from_chat, to_chat)
        db.invalidate_cache()
    return {"merged": bool(from_chat)}


def main() -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

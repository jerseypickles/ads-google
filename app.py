"""Dashboard local de Jersey Pickles — http://localhost:8765

Sirve el dashboard con los datos más recientes y los mantiene
sincronizados SOLO (sin botones):
  - reportes de campaña y términos de búsqueda: se refrescan si tienen
    más de 12 horas
  - ideas de keywords y métricas históricas: si tienen más de 3 días
    (Google actualiza los volúmenes de búsqueda ~mensualmente)

La página comprueba /api/version cada 3 minutos y se recarga sola
cuando hay datos nuevos.

Uso:
    .venv/bin/python app.py
"""

import glob
import json
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, request

import hashlib
import re

import actions as fable_actions
import db as mongo
import fable
import pixel_health
from dashboard_data import build_data

BASE = Path(__file__).parent
PYTHON = str(BASE / ".venv" / "bin" / "python")

CAMPAIGN_MAX_AGE = 12 * 3600        # 12 horas
KEYWORDS_MAX_AGE = 3 * 24 * 3600    # 3 días
CHECK_EVERY = 15 * 60               # comprobar cada 15 min

app = Flask(__name__)

# --- arranque en la nube (Render): materializar credenciales desde el entorno ---
import base64
import os as _os

for _env, _fname in (("GOOGLE_ADS_YAML", "google-ads.yaml"),
                     ("SHOPIFY_TOKEN", ".shopify_token"),
                     ("MERCHANT_TOKEN_JSON", ".merchant_token.json")):
    _v = _os.environ.get(_env)
    if _v and not (BASE / _fname).exists():
        (BASE / _fname).write_text(_v, encoding="utf-8")

_DASH_PASSWORD = _os.environ.get("DASH_PASSWORD", "").strip()
READ_ONLY = bool(_os.environ.get("READ_ONLY", "").strip())


@app.before_request
def _guard():
    if _DASH_PASSWORD:
        auth = request.headers.get("Authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
                ok = pwd == _DASH_PASSWORD
            except Exception:
                pass
        if not ok:
            return app.response_class(
                "Acceso restringido", 401,
                {"WWW-Authenticate": 'Basic realm="Jersey Pickles Ads"'})
    # espejo en la nube: solo lectura SALVO aplicar acciones de Fable (van directo
    # a Google Ads y se registran en Mongo — sin tocar el estado del Mac)
    if READ_ONLY and request.method == "POST" and request.path != "/api/actions/apply":
        return jsonify(ok=False, reason="modo espejo: esta acción se hace desde la app del Mac"), 403
    return None
_lock = threading.Lock()


def _newest_mtime(pattern: str) -> float:
    files = glob.glob(str(BASE / pattern))
    return max((Path(f).stat().st_mtime for f in files), default=0.0)


def _run(script: str, *args: str) -> int:
    proc = subprocess.run(
        [PYTHON, str(BASE / script), *args],
        cwd=BASE, capture_output=True, text=True, timeout=900,
    )
    status = "ok" if proc.returncode == 0 else f"ERROR {proc.returncode}"
    print(f"[sync] {script} {' '.join(args)} -> {status}", flush=True)
    return proc.returncode


def _watch_age() -> float:
    if fable.WATCH_PATH.exists():
        return time.time() - fable.WATCH_PATH.stat().st_mtime
    return float("inf")


def _regen_watch() -> None:
    try:
        print("[vigía] Fable analizando keywords...", flush=True)
        fable.generate_watch()
        print("[vigía] observaciones actualizadas", flush=True)
    except Exception as exc:
        print(f"[vigía] error: {exc}", flush=True)


def _any_live() -> bool:
    """¿Hay campañas ya subidas/corriendo en Google Ads?"""
    try:
        return any(c.get("status") == "LIVE" for c in _load_camps()["campaigns"])
    except Exception:
        return False


LIVE_METRICS_AGE = 900           # con campañas activas: CSVs cada 15 min
LIVE_REVIEW_AGE = 3 * 3600       # y Fable relee el rendimiento cada 3 horas

# --- métricas EN VIVO para campañas LIVE, con rango tipo Meta (hoy/ayer/7d/14d/30d) ---
_live_cache: dict = {}  # range_key -> {"at": ts, "data": {...}}

RANGES = {"today": 0, "yesterday": 1, "7d": 6, "14d": 13, "30d": 29}


def _account_today():
    """'Hoy' en la zona de la cuenta de Google Ads (NY) — el servidor vive en UTC."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()


def _range_dates(range_key: str):
    from datetime import timedelta

    today = _account_today()
    if range_key == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    days = RANGES.get(range_key, 6)
    return today - timedelta(days=days), today


def _fetch_live_metrics(range_key: str = "7d") -> dict:
    now = time.time()
    ttl = 90 if range_key in ("today", "yesterday") else 300
    # la clave incluye la fecha: al empezar un día nuevo, "hoy"/"ayer" se reinician solos
    cache_key = f"{range_key}:{_account_today().isoformat()}"
    cached = _live_cache.get(cache_key)
    if cached and now - cached["at"] < ttl:
        return cached["data"]

    from google.ads.googleads.client import GoogleAdsClient

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    ga = client.get_service("GoogleAdsService")
    start, end = _range_dates(range_key)
    rng = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    METRICS = ("metrics.impressions, metrics.clicks, metrics.cost_micros, "
               "metrics.conversions, metrics.conversions_value")

    def _acc(bucket, key, met):
        m = bucket.setdefault(key, dict(impr=0, clicks=0, cost=0.0, conv=0.0, value=0.0))
        m["impr"] += met.impressions
        m["clicks"] += met.clicks
        m["cost"] += met.cost_micros / 1e6
        m["conv"] += met.conversions
        m["value"] += met.conversions_value

    def _fin(bucket):
        for m in bucket.values():
            m["cost"] = round(m["cost"], 2)
            m["conv"] = round(m["conv"], 1)
            m["value"] = round(m["value"], 2)
            m["cpa"] = round(m["cost"] / m["conv"], 2) if m["conv"] else None
            m["roas"] = round(m["value"] / m["cost"], 2) if m["cost"] else None
            m["ctr"] = round(m["clicks"] / m["impr"] * 100, 1) if m["impr"] else None
            m["cpc"] = round(m["cost"] / m["clicks"], 2) if m["clicks"] else None

    camps, groups, ads = {}, {}, {}
    q1 = f"""SELECT campaign.name, {METRICS} FROM campaign
             WHERE {rng} AND campaign.status != 'REMOVED'"""
    for b in ga.search_stream(customer_id="4888823590", query=q1):
        for r in b.results:
            _acc(camps, r.campaign.name, r.metrics)
    q2 = f"""SELECT campaign.name, ad_group.name, {METRICS} FROM ad_group
             WHERE {rng} AND campaign.status != 'REMOVED'"""
    for b in ga.search_stream(customer_id="4888823590", query=q2):
        for r in b.results:
            _acc(groups, f"{r.campaign.name}|{r.ad_group.name}", r.metrics)
    q3 = f"""SELECT campaign.name, ad_group.name, {METRICS} FROM ad_group_ad
             WHERE {rng} AND campaign.status != 'REMOVED'"""
    for b in ga.search_stream(customer_id="4888823590", query=q3):
        for r in b.results:
            _acc(ads, f"{r.campaign.name}|{r.ad_group.name}", r.metrics)
    _fin(camps); _fin(groups); _fin(ads)

    out = {"campaigns": camps, "groups": groups, "ads": ads}
    _live_cache[cache_key] = {"at": now, "data": out}
    return out


def sync_if_stale() -> None:
    with _lock:
        now = time.time()
        live = _any_live()
        metrics_age = LIVE_METRICS_AGE if live else CAMPAIGN_MAX_AGE
        if now - _newest_mtime("report_campanas_*.csv") > metrics_age:
            _run("campaign_report.py", "365")
            _run("search_terms.py")
            mirrored = mongo.mirror_csvs()
            if mirrored:
                print(f"[mongo] espejo de datos: {mirrored}", flush=True)
            try:  # foto del dashboard para el espejo en la nube
                mongo.upsert("cache", {"_id": "dashboard_data"}, {"data": build_data()})
            except Exception:
                pass
            if live:
                _kick_review()  # datos frescos → Fable relee el rendimiento
        if now - _newest_mtime("historical_metrics_*.csv") > KEYWORDS_MAX_AGE:
            _run("autocomplete_seeds.py")   # keywords "por los lados"
            _run("keyword_ideas.py")
            _run("historical_metrics.py")
            _regen_watch()                  # Fable observa los datos frescos
        elif live and _watch_age() > 24 * 3600:
            # con campañas activas el vigía trabaja a DIARIO: re-cosecha el
            # autocompletado (nuevas búsquedas laterales) y re-analiza
            _run("autocomplete_seeds.py")
            _regen_watch()
        elif _watch_age() > KEYWORDS_MAX_AGE:
            _regen_watch()
        # crecimiento: si TODO lo propuesto ya está aceptado y el plan tiene edad,
        # Fable propone una expansión nueva él solo (con sus lecciones y el estado real)
        # CRECIMIENTO: sin gatillos por fecha. Fable evalúa el caso de expansión en
        # CADA revisión (cada 3h) y _maybe_expand() dispara la generación cuando él
        # declara conviene=true con confianza alta — inteligencia, no calendario.

        # feed propio de Merchant: stock y precios de Shopify cada 6 horas
        MERCHANT_FEED_AGE = 6 * 3600
        feed_stamp = BASE / ".merchant_feed_synced"
        if (BASE / ".merchant_token.json").exists():
            last = feed_stamp.stat().st_mtime if feed_stamp.exists() else 0
            if now - last > MERCHANT_FEED_AGE:
                if _run("merchant_feed.py") == 0:
                    feed_stamp.touch()

        # el auditor relee las campañas si el gestor cambió o la lectura está vieja
        if _load_camps().get("campaigns"):
            review_mtime = (
                fable.CAMP_REVIEW_PATH.stat().st_mtime
                if fable.CAMP_REVIEW_PATH.exists() else 0
            )
            store_mtime = CAMPS_PATH.stat().st_mtime if CAMPS_PATH.exists() else 0
            review_max = LIVE_REVIEW_AGE if live else CAMPAIGN_MAX_AGE
            if review_mtime < store_mtime or now - review_mtime > review_max:
                _kick_review()


def auto_sync_loop() -> None:
    while True:
        try:
            sync_if_stale()
        except Exception as exc:  # nunca tumbar el hilo de sync
            print(f"[sync] excepción: {exc}", flush=True)
        time.sleep(CHECK_EVERY)


# en la nube (gunicorn no ejecuta __main__): RUN_SYNC=1 enciende el cerebro completo
if _os.environ.get("RUN_SYNC"):
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    print("[sync] hilo de sincronización activo (nube)", flush=True)


@app.route("/")
def index():
    template = (BASE / "dashboard_template.html").read_text(encoding="utf-8")
    try:
        data = build_data()
    except Exception:
        # nube sin CSVs: usar la foto que el Mac espeja en Mongo
        cached = mongo.get_doc("cache", {"_id": "dashboard_data"}) or {}
        data = cached.get("data", {})
    resp = app.response_class(
        template.replace("__DATA__", json.dumps(data, ensure_ascii=False)),
        mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"  # el diseño cambia seguido
    return resp


_plan_state = {"running": False, "error": None}


def _ingest_search_proposals() -> list:
    """Campañas nuevas del plan → gestor como ✨PROPUESTA (se aceptan en Campañas)."""
    plan = fable.load_plan() or {}
    store = _load_camps()
    existing = {c["name"] for c in store["campaigns"]}
    added = []
    for c in plan.get("campaigns", []):
        if c["name"] in existing:
            continue
        entry = dict(c)
        entry["id"] = f"c{int(time.time() * 1000)}_{len(store['campaigns'])}"
        entry["status"] = "PROPUESTA"
        entry["enabled"] = True
        entry["proposed_at"] = time.strftime("%Y-%m-%d %H:%M")
        entry["strategy_summary"] = plan.get("strategy_summary", "")
        for gi, g in enumerate(entry.get("ad_groups", [])):
            g["id"] = f"{entry['id']}_g{gi}"
        store["campaigns"].append(entry)
        added.append(c["name"])
    if added:
        for n in plan.get("negatives", []):
            if n not in store["negatives"]:
                store["negatives"].append(n)
        _save_camps(store)
        _kick_review()
        print(f"[propuestas] nuevas en el gestor: {added}", flush=True)
    return added


def _ingest_shopping_proposal() -> list:
    plan = fable.load_shopping_plan() or {}
    camp = plan.get("campaign") or {}
    if not camp.get("name") or not camp.get("product_groups"):
        return []  # "aún no toca" o plan vacío
    store = _load_camps()
    if any(c["name"] == camp["name"] for c in store["campaigns"]):
        return []
    entry = dict(camp)
    entry["type"] = "SHOPPING"
    entry["id"] = f"c{int(time.time() * 1000)}_{len(store['campaigns'])}"
    entry["status"] = "PROPUESTA"
    entry["enabled"] = True
    entry["proposed_at"] = time.strftime("%Y-%m-%d %H:%M")
    entry["negatives"] = plan.get("negatives", [])
    entry["scaling_plan"] = plan.get("scaling_plan", [])
    entry["strategy_summary"] = plan.get("strategy_summary", "")
    entry["ad_groups"] = [
        dict(id=f"{entry['id']}_g{gi}", name=g["name"], rationale=g.get("rationale", ""),
             products=("todo el catálogo" if g.get("all_products")
                       else f"{len(g.get('item_ids') or [])} productos"),
             cpc_bid_usd=g.get("cpc_bid_usd"), keywords=[])
        for gi, g in enumerate(camp.get("product_groups", []))
    ]
    store["campaigns"].append(entry)
    _save_camps(store)
    _kick_review()
    print(f"[propuestas] nueva shopping en el gestor: {camp['name']}", flush=True)
    return [camp["name"]]


def _run_plan_generation() -> None:
    try:
        fable.generate_plan()
        _plan_state["error"] = None
        _ingest_search_proposals()
    except Exception as exc:
        _plan_state["error"] = str(exc)
        print(f"[fable] error: {traceback.format_exc()}", flush=True)
    finally:
        _plan_state["running"] = False


@app.route("/api/plan")
def get_plan():
    plan = fable.load_plan()
    return jsonify(plan=plan, running=_plan_state["running"], error=_plan_state["error"],
                   started_at=_plan_state.get("started_at"))


@app.route("/api/plan/generate", methods=["POST"])
def generate_plan():
    if _plan_state["running"]:
        return jsonify(ok=False, reason="ya hay una generación en curso"), 409
    _plan_state["running"] = True
    _plan_state["error"] = None
    _plan_state["started_at"] = time.time()
    threading.Thread(target=_run_plan_generation, daemon=True).start()
    return jsonify(ok=True)


_shopping_state = {"running": False, "error": None}


def _run_shopping_generation() -> None:
    try:
        fable.generate_shopping_plan()
        _shopping_state["error"] = None
        _ingest_shopping_proposal()
    except Exception as exc:
        _shopping_state["error"] = str(exc)
        print(f"[fable-shopping] error: {traceback.format_exc()}", flush=True)
    finally:
        _shopping_state["running"] = False


@app.route("/api/shopping/plan")
def get_shopping_plan():
    return jsonify(plan=fable.load_shopping_plan(),
                   running=_shopping_state["running"], error=_shopping_state["error"],
                   started_at=_shopping_state.get("started_at"))


@app.route("/api/shopping/plan/generate", methods=["POST"])
def shopping_plan_generate():
    if _shopping_state["running"]:
        return jsonify(ok=False, reason="ya hay una generación en curso"), 409
    _shopping_state["running"] = True
    _shopping_state["error"] = None
    _shopping_state["started_at"] = time.time()
    threading.Thread(target=_run_shopping_generation, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/shopping/accept", methods=["POST"])
def shopping_accept():
    import validate_plan

    plan = fable.load_shopping_plan()
    if not plan or not plan.get("campaign"):
        return jsonify(ok=False, reason="no hay plan de shopping"), 400
    camp = plan["campaign"]
    store = _load_camps()
    if any(c["name"] == camp["name"] for c in store["campaigns"]):
        return jsonify(ok=False, reason="esa campaña ya está en el gestor"), 400
    entry = dict(camp)
    entry["type"] = "SHOPPING"
    entry["id"] = f"c{int(time.time() * 1000)}_{len(store['campaigns'])}"
    entry["status"] = "DRAFT"
    entry["enabled"] = True
    entry["accepted_at"] = time.strftime("%Y-%m-%d %H:%M")
    entry["negatives"] = plan.get("negatives", [])
    entry["scaling_plan"] = plan.get("scaling_plan", [])
    entry["strategy_summary"] = plan.get("strategy_summary", "")
    # los grupos de productos se reflejan como ad_groups para el gestor
    entry["ad_groups"] = [
        dict(id=f"{entry['id']}_g{gi}", name=g["name"], rationale=g.get("rationale", ""),
             products=("todo el catálogo" if g.get("all_products")
                       else f"{len(g.get('item_ids') or [])} productos"),
             cpc_bid_usd=g.get("cpc_bid_usd"), keywords=[])
        for gi, g in enumerate(camp.get("product_groups", []))
    ]
    errors = [f"{w}: {m}" for lvl, w, m in validate_plan.check_shopping(entry)
              if lvl == "ERROR"]
    if errors:
        return jsonify(ok=False, reason="validador: " + "; ".join(errors[:5])), 400
    store["campaigns"].append(entry)
    _save_camps(store)
    _kick_review()
    return jsonify(ok=True, store=store)


@app.route("/api/watch")
def get_watch():
    return jsonify(watch=fable.load_watch())


# ---------------------------------------------------------------------------
# Gestor de campañas (estilo Meta): listings aceptados viven aquí
# ---------------------------------------------------------------------------

CAMPS_PATH = BASE / "campaigns_local.json"


def _load_camps() -> dict:
    if CAMPS_PATH.exists():
        return json.loads(CAMPS_PATH.read_text(encoding="utf-8"))
    doc = mongo.get_doc("manager", {"_id": "store"})  # espejo en la nube
    if doc and doc.get("store"):
        return doc["store"]
    return {"campaigns": [], "negatives": []}


def _save_camps(data: dict) -> None:
    CAMPS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.upsert("manager", {"_id": "store"}, {"store": data})


_review_state = {"running": False, "error": None}


def _recent_doc(collection: str, filt: dict, days: float) -> bool:
    doc = mongo.latest(collection, filt)
    if not doc or not doc.get("ts"):
        return False
    try:
        then = time.mktime(time.strptime(str(doc["ts"])[:19], "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - then) < days * 86400
    except Exception:
        return False


def _maybe_expand(review: dict) -> None:
    """Fable declaró (o no) un caso de expansión en su revisión — el código solo
    aplica salvaguardas: confianza alta, sin propuesta pendiente del mismo tipo,
    respeto a rechazos recientes del dueño y cooldown anti-ráfaga."""
    exp = (review or {}).get("expansion") or {}
    try:
        pendientes = {(c.get("type") or "SEARCH").upper()
                      for c in _load_camps()["campaigns"] if c.get("status") == "PROPUESTA"}
    except Exception:
        pendientes = set()
    rutas = (
        ("search", "SEARCH", _plan_state, _run_plan_generation),
        ("shopping", "SHOPPING", _shopping_state, _run_shopping_generation),
    )
    for key, tipo, state, runner in rutas:
        e = exp.get(key) or {}
        if not (e.get("conviene") and str(e.get("confianza", "")).lower() == "alta"):
            continue
        if tipo in pendientes or state["running"]:
            continue
        if _recent_doc("rejected_proposals", {"type": tipo}, 7):
            print(f"[expansión] {tipo}: Fable ve caso pero el dueño rechazó hace <7d — se respeta", flush=True)
            continue
        if _recent_doc("expansion_triggers", {"type": tipo}, 3):
            continue  # anti-ráfaga: máx. una propuesta nueva del tipo cada 3 días
        print(f"[expansión] Fable declaró caso {tipo} (confianza alta): {e.get('razon', '')[:140]}", flush=True)
        mongo.save("expansion_triggers", {"type": tipo, "razon": e.get("razon", "")})
        state["running"] = True
        state["error"] = None
        state["started_at"] = time.time()
        threading.Thread(target=runner, daemon=True).start()


def _run_review() -> None:
    try:
        review = fable.generate_campaign_review()
        _review_state["error"] = None
        _autopilot(review)      # Nivel 2: lo seguro se ejecuta solo (verificado)
        _maybe_expand(review)   # y el crecimiento lo decide su análisis, no fechas
    except Exception as exc:
        _review_state["error"] = str(exc)
        print(f"[auditor] error: {exc}", flush=True)
    finally:
        _review_state["running"] = False


def _kick_review(min_age: float = None) -> None:
    """Lanza una revisión respetando la cadencia (3h por defecto) — cada una cuesta API.

    min_age=0 la fuerza (acciones del dueño: aceptar/lanzar/botón manual)."""
    if _review_state["running"]:
        return
    if min_age is None:
        min_age = LIVE_REVIEW_AGE
    if min_age > 0:
        last = (fable.CAMP_REVIEW_PATH.stat().st_mtime
                if fable.CAMP_REVIEW_PATH.exists() else 0)
        if not last:  # nube recién reiniciada: mirar el espejo
            doc = mongo.latest("reviews")
            ts = str((doc or {}).get("ts", ""))[:19]
            try:
                last = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                last = 0
        if time.time() - last < min_age:
            return
    _review_state["running"] = True
    _review_state["error"] = None
    threading.Thread(target=_run_review, daemon=True).start()


@app.route("/api/campaigns")
def campaigns_list():
    store = _load_camps()
    range_key = request.args.get("range", "7d")
    if range_key not in RANGES:
        range_key = "7d"
    live = {"campaigns": {}, "groups": {}, "ads": {}}
    if any(c.get("status") == "LIVE" for c in store["campaigns"]):
        try:
            live = _fetch_live_metrics(range_key)
        except Exception as exc:
            print(f"[live-metrics] error: {exc}", flush=True)
    for c in store["campaigns"]:
        c["metrics"] = live["campaigns"].get(c["name"])
        for g in c.get("ad_groups", []):
            key = f"{c['name']}|{g['name']}"
            g["metrics"] = live["groups"].get(key)
            g["ad_metrics"] = live["ads"].get(key)
    _attach_shopping_items(store)
    store["range"] = range_key
    return jsonify(store)


def _pid_vid(item_id: str):
    try:
        parts = item_id.split("_")
        return int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return None, None


def _attach_shopping_items(store: dict) -> None:
    """En campañas SHOPPING, el creativo son los productos: se resuelven con foto y stock."""
    shop = [c for c in store.get("campaigns", []) if (c.get("type") or "").upper() == "SHOPPING"]
    if not shop:
        return
    try:
        cat = _shopify_catalog()
        for c in shop:
            excl = {(e.get("item_id") or "").lower() for e in c.get("excluded_products", [])}
            excl_pids = {p for p, _ in (_pid_vid(i) for i in excl) if p}
            pg_by_name = {g.get("name"): g for g in c.get("product_groups", [])}
            otros_pids = {p for g in c.get("product_groups", []) if not g.get("all_products")
                          for p, _ in (_pid_vid(i) for i in g.get("item_ids") or []) if p}
            for ag in c.get("ad_groups", []):
                pg = pg_by_name.get(ag.get("name"))
                if not pg:
                    continue
                items = {}
                if pg.get("all_products"):
                    for pid, p in cat["by_product"].items():
                        if p.get("status") != "active" or pid in otros_pids or pid in excl_pids:
                            continue
                        agotadas = sum(1 for v in p.get("variants") or []
                                       if v.get("qty") is not None and v["qty"] <= 0)
                        items[pid] = dict(title=p["title"], img=p.get("img"),
                                          variantes=len(p.get("variants") or []),
                                          agotadas=agotadas)
                else:
                    for iid in pg.get("item_ids") or []:
                        if iid.lower() in excl:
                            continue
                        pid, vid = _pid_vid(iid)
                        p = cat["by_product"].get(pid)
                        if not p:
                            continue
                        e = items.setdefault(pid, dict(title=p["title"], img=p.get("img"),
                                                       variantes=0, agotadas=0))
                        e["variantes"] += 1
                        v = cat["by_variant"].get(vid)
                        if v and v.get("qty") is not None and v["qty"] <= 0:
                            e["agotadas"] += 1
                ag["items"] = list(items.values())
    except Exception as exc:
        print(f"[manager] items shopping: {exc}", flush=True)


@app.route("/api/lessons")
def lessons():
    return jsonify(lessons=fable.load_lessons())


# ---------------------------------------------------------------------------
# Acciones de Fable: sugerencias ejecutables sobre Google Ads
# ---------------------------------------------------------------------------

ACTIONS_LOG = BASE / "actions_log.json"


def _load_actions_log() -> list:
    if ACTIONS_LOG.exists():
        return json.loads(ACTIONS_LOG.read_text(encoding="utf-8"))
    return mongo.all_docs("actions", limit=200)  # espejo en la nube


def _action_key(a: dict) -> str:
    base = json.dumps(
        {k: a.get(k) for k in ("tipo", "campana", "objetivo", "valor")},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.md5(base.encode()).hexdigest()[:12]


@app.route("/api/actions/log")
def actions_log():
    return jsonify(applied=_load_actions_log())


@app.route("/api/actions/apply", methods=["POST"])
def actions_apply():
    a = (request.get_json(silent=True) or {}).get("action") or {}
    return jsonify(**_apply_and_log(a))


def _apply_and_log(a: dict, auto: bool = False) -> dict:
    key = _action_key(a)
    log = _load_actions_log()
    if any(e["key"] == key and e.get("ok") for e in log) \
            or mongo.get_doc("actions", {"key": key, "ok": True}):
        return dict(ok=True, msg="ya estaba aplicada", key=key)
    result = fable_actions.apply_action(a)
    msg = ("🤖 auto · " if auto else "") + result["msg"]
    entry = dict(key=key, action=a, ok=result["ok"], msg=msg,
                 ts=time.strftime("%Y-%m-%d %H:%M"), auto=auto)
    log.append(entry)
    ACTIONS_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.save("actions", entry)
    if result["ok"]:
        modo = "auto-aplicada por el piloto Nivel 2" if auto else "aplicada"
        fable.learn(f"Acción {modo} ({a.get('tipo')}): {result['msg']}. Razón: {a.get('razon', '')[:150]}")
        _pulse_cache.update(at=0)  # refrescar pulso tras el cambio
        _sync_store_after_action(a)  # que el gestor (y el frontend) reflejen el cambio
        if a.get("tipo") == "optimizar_titulo_feed":
            threading.Thread(target=lambda: _run("merchant_feed.py"), daemon=True).start()
    return dict(ok=result["ok"], msg=msg, key=key)


def _sync_store_after_action(a: dict) -> None:
    """El gestor refleja lo que ya cambió en Google Ads (presupuesto, keywords pausadas)."""
    try:
        tipo = a.get("tipo")
        if tipo not in ("ajustar_presupuesto", "pausar_keyword", "reactivar_keyword"):
            return
        store = _load_camps()
        camp = next((c for c in store["campaigns"] if c["name"] == a.get("campana")), None)
        if not camp:
            return
        if tipo == "ajustar_presupuesto":
            camp["daily_budget_usd"] = float(a.get("valor"))
        else:
            obj = fable_actions._clean_kw(a.get("objetivo") or "").lower()
            for g in camp.get("ad_groups", []):
                for k in g.get("keywords", []):
                    if (k.get("text") or "").lower() == obj:
                        k["paused"] = (tipo == "pausar_keyword")
        _save_camps(store)
    except Exception as exc:
        print(f"[store-sync] {exc}", flush=True)


# --- piloto automático Nivel 2: lo seguro se ejecuta solo, verificado por código ---
AUTO_TYPES = {"añadir_negativa", "excluir_producto_shopping", "pausar_keyword"}


def _auto_safe(a: dict) -> bool:
    """El código verifica la evidencia — jamás se auto-aplica por confianza en el modelo."""
    tipo = a.get("tipo")
    if tipo == "añadir_negativa":
        return True  # el ejecutor ya bloquea colisiones con keywords activas
    if tipo == "excluir_producto_shopping":
        try:  # higiene: SOLO si la variante está realmente agotada en Shopify
            from store_catalog import shopify_catalog
            vid = int((a.get("objetivo") or "").split("_")[3])
            v = shopify_catalog()["by_variant"].get(vid)
            return bool(v and v.get("qty") is not None and v["qty"] <= 0)
        except Exception:
            return False
    if tipo == "pausar_keyword":
        stats = fable_actions.keyword_7d_stats(a.get("campana", ""), a.get("objetivo", ""))
        if not stats:
            return False
        cost, conv = stats
        return cost >= 25 and conv == 0  # sangría verificada con números reales
    return False


def _autopilot(review: dict) -> None:
    for a in (review or {}).get("acciones_propuestas") or []:
        if a.get("tipo") not in AUTO_TYPES:
            continue
        if not _auto_safe(a):
            continue
        r = _apply_and_log(a, auto=True)
        print(f"[piloto] {a.get('tipo')} → {r['msg']}", flush=True)


# ---------------------------------------------------------------------------
# Pulso en vivo: hoy en las campañas + latido de Fable + competencia
# ---------------------------------------------------------------------------

_pulse_cache = {"at": 0.0, "data": None}


@app.route("/api/pulse")
def pulse():
    now = time.time()
    if _pulse_cache["data"] and now - _pulse_cache["at"] < 60:
        return jsonify(_pulse_cache["data"])

    out = {"live": [], "funnel_today": {}, "fable": {}, "competitors": [],
           "keywords_7d": [], "terms_7d": []}
    store = _load_camps()
    live_names = {c["name"]: c for c in store["campaigns"] if c.get("status") == "LIVE"}

    if live_names:
        try:
            from google.ads.googleads.client import GoogleAdsClient

            client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
            ga = client.get_service("GoogleAdsService")
            cid = "4888823590"
            q = """SELECT campaign.name, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions, metrics.average_cpc
                   FROM campaign WHERE segments.date DURING TODAY"""
            for b in ga.search_stream(customer_id=cid, query=q):
                for r in b.results:
                    if r.campaign.name in live_names:
                        c = live_names[r.campaign.name]
                        out["live"].append(dict(
                            name=r.campaign.name, role=c.get("match_role", ""),
                            budget=c.get("daily_budget_usd"),
                            impr=r.metrics.impressions, clicks=r.metrics.clicks,
                            cost=round(r.metrics.cost_micros / 1e6, 2),
                            cpc=round(r.metrics.average_cpc / 1e6, 2),
                            conv=round(r.metrics.conversions, 1)))
            q2 = """SELECT segments.conversion_action_name, metrics.all_conversions
                    FROM campaign WHERE segments.date DURING TODAY"""
            for b in ga.search_stream(customer_id=cid, query=q2):
                for r in b.results:
                    if r.metrics.all_conversions:
                        k = r.segments.conversion_action_name
                        out["funnel_today"][k] = out["funnel_today"].get(k, 0) + r.metrics.all_conversions
            deep = fable._live_deep_data()
            out["keywords_7d"] = deep.get("keywords_7d", [])
            out["terms_7d"] = deep.get("terminos_busqueda_7d", [])[:14]
        except Exception as exc:
            out["error"] = str(exc)

    # latido de Fable
    def _age(p):
        return int(now - p.stat().st_mtime) if p.exists() else None

    review_age = _age(fable.CAMP_REVIEW_PATH)
    review_max = LIVE_REVIEW_AGE if _any_live() else CAMPAIGN_MAX_AGE
    all_lessons = fable.load_lessons()
    out["fable"] = dict(
        review_age_s=review_age,
        next_review_s=max(0, review_max - review_age) if review_age is not None else None,
        watch_age_s=_age(fable.WATCH_PATH),
        lessons_total=len(all_lessons),
        last_lessons=all_lessons[-3:][::-1],
        review_running=_review_state["running"],
        plan_running=_plan_state["running"],
    )

    # competencia: marcas que la gente busca (de las métricas validadas)
    import csv as _csv

    brands = ["claussen", "grillo", "vlasic", "mt olive", "mcclure", "best maid"]
    hist = sorted(glob.glob(str(BASE / "historical_metrics_*.csv")))
    comp = []
    if hist:
        with open(hist[-1], newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                kw = r["keyword"]
                if any(b in kw for b in brands):
                    comp.append(dict(kw=kw, vol=int(r["busquedas_mensuales_promedio"] or 0),
                                     low=float(r["puja_top_baja_usd"]),
                                     high=float(r["puja_top_alta_usd"])))
    comp.sort(key=lambda x: -x["vol"])
    out["competitors"] = comp[:8]

    _pulse_cache.update(at=now, data=out)
    if out["live"]:
        # serie temporal en Mongo: una foto por minuto de las campañas en vivo
        mongo.save("pulse_snapshots", {
            "live": out["live"], "funnel_today": out["funnel_today"],
            "lessons_total": out["fable"].get("lessons_total"),
        })
    return jsonify(out)


@app.route("/api/campaigns/review")
def campaigns_review():
    review = fable.load_campaign_review()
    if review:
        # compuerta del dueño aplicada también al servir (limpia lecturas antiguas)
        review = fable.enforce_learning_gate(review)
    return jsonify(
        review=review,
        running=_review_state["running"],
        error=_review_state["error"],
    )


@app.route("/api/campaigns/review/generate", methods=["POST"])
def campaigns_review_generate():
    _kick_review(min_age=0)  # petición explícita del dueño
    return jsonify(ok=True)


@app.route("/api/campaigns/accept", methods=["POST"])
def campaigns_accept():
    body = request.get_json(silent=True) or {}
    plan = fable.load_plan()
    if not plan:
        return jsonify(ok=False, reason="no hay plan"), 400
    store = _load_camps()
    existing = {c["name"] for c in store["campaigns"]}
    wanted = body.get("name")  # None => aceptar todas
    added = []
    for c in plan.get("campaigns", []):
        if c["name"] in existing:
            continue
        if wanted and c["name"] != wanted:
            continue
        entry = dict(c)
        entry["id"] = f"c{int(time.time() * 1000)}_{len(store['campaigns'])}"
        entry["status"] = "DRAFT"
        entry["enabled"] = True
        entry["accepted_at"] = time.strftime("%Y-%m-%d %H:%M")
        for gi, g in enumerate(entry.get("ad_groups", [])):
            g["id"] = f"{entry['id']}_g{gi}"
        store["campaigns"].append(entry)
        added.append(c["name"])
    for n in plan.get("negatives", []):
        if n not in store["negatives"]:
            store["negatives"].append(n)
    _save_camps(store)
    if added:
        _kick_review(min_age=0)  # Fable lee lo recién aceptado
    return jsonify(ok=True, added=added, store=store)


@app.route("/api/campaigns/toggle", methods=["POST"])
def campaigns_toggle():
    body = request.get_json(silent=True) or {}
    store = _load_camps()
    for c in store["campaigns"]:
        if c["id"] == body.get("id"):
            c["enabled"] = bool(body.get("enabled"))
    _save_camps(store)
    return jsonify(ok=True)


@app.route("/api/campaigns/delete", methods=["POST"])
def campaigns_delete():
    body = request.get_json(silent=True) or {}
    store = _load_camps()
    store["campaigns"] = [c for c in store["campaigns"] if c["id"] != body.get("id")]
    _save_camps(store)
    return jsonify(ok=True)


# --- propuestas de Fable: aceptar (=subir a Google Ads) o rechazar, desde Campañas ---
_launch_state: dict = {}  # id -> {running, ok, msg}


@app.route("/api/campaigns/launch", methods=["POST"])
def campaigns_launch():
    body = request.get_json(silent=True) or {}
    cid = body.get("id")
    camp = next((c for c in _load_camps()["campaigns"] if c["id"] == cid), None)
    if not camp:
        return jsonify(ok=False, reason="campaña no encontrada"), 404
    if camp.get("status") == "LIVE":
        return jsonify(ok=True, msg="ya está en Google Ads")
    st = _launch_state.get(cid)
    if st and st.get("running"):
        return jsonify(ok=False, reason="ya se está subiendo"), 409
    _launch_state[cid] = {"running": True}
    name = camp["name"]

    def _go():
        try:
            import push_campaign
            msg = push_campaign.run(name)
            _launch_state[cid] = {"running": False, "ok": True, "msg": msg}
            fable.learn(f"El dueño aceptó y se lanzó la propuesta '{name}'.")
        except Exception as exc:
            _launch_state[cid] = {"running": False, "ok": False, "msg": str(exc)[:300]}
            print(f"[launch] error en '{name}': {exc}", flush=True)
        _live_cache.clear()
        _kick_review(min_age=0)  # campaña recién lanzada: lectura inmediata

    threading.Thread(target=_go, daemon=True).start()
    return jsonify(ok=True, launching=True)


@app.route("/api/campaigns/launch/status")
def campaigns_launch_status():
    return jsonify(_launch_state.get(request.args.get("id"), {}))


@app.route("/api/campaigns/reject", methods=["POST"])
def campaigns_reject():
    body = request.get_json(silent=True) or {}
    cid = body.get("id")
    store = _load_camps()
    camp = next((c for c in store["campaigns"] if c["id"] == cid), None)
    if not camp:
        return jsonify(ok=False, reason="campaña no encontrada"), 404
    if camp.get("status") == "LIVE":
        return jsonify(ok=False, reason="está VIVA en Google Ads — páusala allá, no se rechaza"), 400
    store["campaigns"] = [c for c in store["campaigns"] if c["id"] != cid]
    _save_camps(store)
    mongo.save("rejected_proposals", {"name": camp["name"], "type": camp.get("type", "SEARCH")})
    fable.learn(f"El dueño RECHAZÓ la propuesta '{camp['name']}' — no volver a proponer la misma estructura sin datos nuevos que la justifiquen.")
    return jsonify(ok=True)


_products_cache: dict = {}
MERCHANT_IMG_PATH = BASE / "merchant_images.json"

from store_catalog import shopify_catalog as _shopify_catalog  # noqa: E402


def _product_images() -> dict:
    """Mapa título-normalizado → URL de foto, desde el sitemap de Shopify (caché 7 días)."""
    import urllib.request
    import xml.etree.ElementTree as ET

    try:
        if MERCHANT_IMG_PATH.exists():
            data = json.loads(MERCHANT_IMG_PATH.read_text(encoding="utf-8"))
            if time.time() - data.get("at", 0) < 7 * 86400 and data.get("map"):
                return data["map"]
    except Exception:
        pass

    imgs: dict = {}
    try:
        def _get(url):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
            return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")

        idx = _get("https://jerseypickles.com/sitemap.xml").replace("&amp;", "&")
        murl = re.search(r"<loc>([^<]*sitemap_products[^<]*)</loc>", idx)
        if murl:
            root = ET.fromstring(_get(murl.group(1)))
            ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
                  "i": "http://www.google.com/schemas/sitemap-image/1.1"}
            for u in root.findall("s:url", ns):
                loc = u.find("i:image/i:loc", ns)
                tit = u.find("i:image/i:title", ns)
                if loc is not None and tit is not None and tit.text:
                    key = re.sub(r"[^a-z0-9 ]+", " ", tit.text.lower())
                    key = re.sub(r"\s+", " ", key).strip()
                    if key:
                        imgs[key] = loc.text
        MERCHANT_IMG_PATH.write_text(
            json.dumps({"at": time.time(), "map": imgs}), encoding="utf-8")
        print(f"[merchant] {len(imgs)} fotos de producto cacheadas del sitemap", flush=True)
    except Exception as exc:
        print(f"[merchant] imágenes no disponibles: {exc}", flush=True)
    return imgs


# Relleno SEO del feed de AdNabu y tallas/colores de variantes: no aportan al match.
_IMG_STOP = {
    "buy", "online", "shipping", "to", "us", "usa", "canada", "and", "the",
    "quart", "gallon", "pint", "bucket", "oz", "black", "white", "navy",
    "gray", "grey", "xs", "s", "m", "l", "xl", "xxl",
}


def _img_tokens(text: str) -> set:
    t = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    out = set()
    for w in t.split():
        if w.isdigit() or w in _IMG_STOP:
            continue
        if w == "pickled":
            w = "pickle"
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]                             # plural simple: olives→olive
        out.add(w)
    return out


def _match_image(title: str, imgs: dict):
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    tw = _img_tokens(title)
    best = None
    for k, v in imgs.items():
        if not k:
            continue
        if k in t:
            score = 100 + len(k.split())          # substring exacto: prioridad
        else:
            kw = _img_tokens(k)
            inter = len(kw & tw)
            ratio = inter / min(len(kw), len(tw)) if kw and tw else 0
            if not (ratio >= 0.75 or (inter >= 2 and ratio >= 0.6)):
                continue
            score = ratio * inter                 # cobertura × palabras coincidentes
        if best is None or score > best[0]:
            best = (score, v)
    return best[1] if best else None


# Merchant de Jersey Plastic (frascos PET, ribbons): no es nuestro catálogo.
# La API no permite quitar el link desde el lado Ads — se filtra aquí.
FOREIGN_MERCHANTS = {5564945299}


def _merchant_issues() -> list:
    """Productos NOT_ELIGIBLE con sus issues (vía shopping_product de la Ads API)."""
    try:
        from google.ads.googleads.client import GoogleAdsClient

        client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
        ga = client.get_service("GoogleAdsService")
        q = """SELECT shopping_product.title, shopping_product.status, shopping_product.issues,
               shopping_product.merchant_center_id, shopping_product.channel
               FROM shopping_product LIMIT 500"""
        out = []
        for b in ga.search_stream(customer_id="4888823590", query=q):
            for r in b.results:
                sp = r.shopping_product
                if sp.merchant_center_id in FOREIGN_MERCHANTS:
                    continue
                # Vendemos solo online; el canal LOCAL exige inventario de
                # tiendas físicas que no existen — es ruido, no un problema real.
                if sp.channel.name == "LOCAL":
                    continue
                if sp.status.name == "NOT_ELIGIBLE" or sp.issues:
                    out.append(dict(
                        title=sp.title, status=sp.status.name,
                        issues=[getattr(i, "description", str(i)) for i in sp.issues][:3]))
        return out[:20]
    except Exception as exc:
        print(f"[merchant] issues: {exc}", flush=True)
        return []


@app.route("/api/products")
def products():
    days = request.args.get("days", "365")
    days_n = 30 if days == "30" else 365
    now = time.time()
    cached = _products_cache.get(days_n)
    if cached and now - cached["at"] < 600:
        return jsonify(products=cached["data"], days=days_n,
                       issues=cached.get("issues", []),
                       sin_stock=cached.get("sin_stock", []))
    try:
        from datetime import timedelta

        from google.ads.googleads.client import GoogleAdsClient

        client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
        ga = client.get_service("GoogleAdsService")
        end = _account_today()
        start = end - timedelta(days=days_n)
        q = f"""SELECT segments.product_item_id, segments.product_title,
                segments.product_merchant_id,
                metrics.impressions, metrics.clicks, metrics.cost_micros,
                metrics.conversions, metrics.conversions_value
                FROM shopping_performance_view
                WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"""
        prods: dict = {}
        for b in ga.search_stream(customer_id="4888823590", query=q):
            for r in b.results:
                if r.segments.product_merchant_id in FOREIGN_MERCHANTS:
                    continue
                key = r.segments.product_title or r.segments.product_item_id
                if not key:
                    continue
                m = prods.setdefault(key, dict(title=key, impr=0, clicks=0, cost=0.0, conv=0.0, value=0.0, vids=set(), pids=set()))
                vm = re.search(r"shopify_[a-z]{2}_(\d+)_(\d+)", r.segments.product_item_id or "", re.I)
                if vm:
                    m["pids"].add(int(vm.group(1)))
                    m["vids"].add(int(vm.group(2)))
                m["impr"] += r.metrics.impressions
                m["clicks"] += r.metrics.clicks
                m["cost"] += r.metrics.cost_micros / 1e6
                m["conv"] += r.metrics.conversions
                m["value"] += r.metrics.conversions_value
        # métricas de la CAMPAÑA Shopping actual (7 días con hoy) por PID de producto
        camp7: dict = {}
        camp7_titles: dict = {}
        try:
            s7, e7 = _range_dates("7d")
            q7 = f"""SELECT campaign.id, campaign.status, campaign.advertising_channel_type,
                     segments.product_item_id, segments.product_title,
                     metrics.clicks, metrics.cost_micros, metrics.conversions,
                     metrics.conversions_value
                     FROM shopping_performance_view
                     WHERE campaign.advertising_channel_type = 'SHOPPING'
                       AND campaign.status = 'ENABLED'
                       AND segments.date BETWEEN '{s7.isoformat()}' AND '{e7.isoformat()}'"""
            for b in ga.search_stream(customer_id="4888823590", query=q7):
                for r in b.results:
                    mm = re.search(r"shopify_[a-z]{2}_(\d+)_",
                                   (r.segments.product_item_id or "").lower())
                    if not mm:
                        continue
                    pid7 = int(mm.group(1))
                    d = camp7.setdefault(pid7, dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
                    d["clicks"] += r.metrics.clicks
                    d["cost"] += r.metrics.cost_micros / 1e6
                    d["conv"] += r.metrics.conversions
                    d["value"] += r.metrics.conversions_value
                    camp7_titles.setdefault(pid7, r.segments.product_title)
            for d in camp7.values():
                d["cost"] = round(d["cost"], 2)
                d["conv"] = round(d["conv"], 1)
                d["value"] = round(d["value"], 2)
                d["roas"] = round(d["value"] / d["cost"], 1) if d["cost"] else None
        except Exception as exc:
            print(f"[products] camp7: {exc}", flush=True)

        # productos activos en la campaña sin fila histórica → entran igual
        cubiertos = set()
        for m in prods.values():
            cubiertos |= m.get("pids", set())
        for pid7, t in camp7_titles.items():
            if pid7 not in cubiertos and t:
                prods[t] = dict(title=t, impr=0, clicks=0, cost=0.0, conv=0.0,
                                value=0.0, vids=set(), pids={pid7})
        # UNA card por producto físico: colapsar filas gemelas (títulos de otras eras)
        por_pid: dict = {}
        sueltos = []
        for m in prods.values():
            pids = m.get("pids") or set()
            if not pids:
                sueltos.append(m)
                continue
            key = max(pids)  # el pid más nuevo representa al producto
            t = por_pid.get(key)
            if t is None:
                por_pid[key] = m
            else:
                for f in ("impr", "clicks"):
                    t[f] += m[f]
                for f in ("cost", "conv", "value"):
                    t[f] += m[f]
                t["vids"] |= m.get("vids", set())
                t["pids"] |= pids

        out = []
        for m in list(por_pid.values()) + sueltos:
            m["cost"] = round(m["cost"], 2)
            m["conv"] = round(m["conv"], 1)
            m["value"] = round(m["value"], 2)
            m["roas"] = round(m["value"] / m["cost"], 1) if m["cost"] else None
            hits = [camp7[p] for p in m.get("pids", set()) if p in camp7]
            if hits:
                c7 = dict(clicks=sum(h["clicks"] for h in hits),
                          cost=round(sum(h["cost"] for h in hits), 2),
                          conv=round(sum(h["conv"] for h in hits), 1),
                          value=round(sum(h["value"] for h in hits), 2))
                c7["roas"] = round(c7["value"] / c7["cost"], 1) if c7["cost"] else None
                m["camp7"] = c7
            else:
                m["camp7"] = None
            out.append(m)
        # manda la realidad: valor de campaña, luego clics de campaña, luego histórico
        out.sort(key=lambda x: (-(x["camp7"]["value"] if x["camp7"] else 0),
                                -(x["camp7"]["clicks"] if x["camp7"] else 0),
                                -x["conv"]))
        out = out[:100]

        cat = _shopify_catalog()
        imgs = dict(_product_images())
        imgs.update(cat["by_title"])          # el catálogo real pisa al sitemap
        for m in out:
            infos = [cat["by_variant"][v] for v in m.pop("vids", ()) if v in cat["by_variant"]]
            parents = [cat["by_product"][p] for p in m.pop("pids", ()) if p in cat["by_product"]]
            if parents:  # el nombre ACTUAL del producto, no el de eras pasadas
                m["title"] = parents[0]["title"]
            m["img"] = (next((i["img"] for i in infos if i["img"]), None)
                        or _match_image(m["title"], imgs))
            qtys = [i["qty"] for i in infos if i["qty"] is not None]
            m["stock"] = sum(qtys) if qtys else None
            m["agotado"] = bool(qtys) and sum(qtys) <= 0
            m["en_tienda"] = bool(infos)
            # variantes del producto padre, marcando la que anuncia esta card
            own = {i["t"] for i in infos}
            m["variantes"] = [dict(v, own=v["t"] in own)
                              for p in parents[:1] for v in p["variants"]]
        issues = _merchant_issues()
        _products_cache[days_n] = {"at": now, "data": out, "issues": issues,
                                   "sin_stock": cat["sin_stock"]}
        return jsonify(products=out, days=days_n, issues=issues, sin_stock=cat["sin_stock"])
    except Exception as exc:
        return jsonify(products=[], error=str(exc)[:200]), 500


@app.route("/api/pixel")
def pixel():
    try:
        return jsonify(ok=True, health=pixel_health.get_health())
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500


@app.route("/api/version")
def version():
    stamp = int(max(
        _newest_mtime("report_campanas_*.csv"),
        _newest_mtime("historical_metrics_*.csv"),
        _newest_mtime("search_terms_*.csv"),
    ))
    return jsonify(stamp=stamp)


if __name__ == "__main__":
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Dashboard: http://localhost:{port} (sync automático activo)")
    app.run(host="127.0.0.1", port=port)

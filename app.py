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
REVIEW_FAIL_BACKOFF = 1800       # tras un fallo: 30 min, duplicando hasta la cadencia normal

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
    """Gestor con dos escritores posibles (Mac y nube): gana el de revisión mayor.

    Comparar por reloj no sirve — la nube va en UTC y el Mac en hora local — así
    que cada guardado incrementa `rev` y esa es la única fuente de verdad.
    """
    local = None
    if CAMPS_PATH.exists():
        try:
            local = json.loads(CAMPS_PATH.read_text(encoding="utf-8"))
        except Exception:
            local = None
    doc = mongo.get_doc("manager", {"_id": "store"})
    remoto = (doc or {}).get("store")
    if local is None and remoto is None:
        return {"campaigns": [], "negatives": []}
    if local is None:
        return remoto
    if remoto is None:
        return local
    return remoto if int(remoto.get("rev", 0)) > int(local.get("rev", 0)) else local


def _save_camps(data: dict) -> None:
    data["rev"] = int(data.get("rev", 0)) + 1   # el más nuevo gana en ambos lados
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
    """Auditoría de 3h. Las capas DETERMINISTAS van primero y aisladas.

    Antes todo colgaba detrás de generate_campaign_review() en un solo try: el
    11-ago la auditoría empezó a devolver JSON cortado y se llevó por delante
    audiencias, detector de caídas y escalado durante ~18h sin que se notara.
    Ninguna de esas tres necesita al modelo — sólo leen Google Ads y aplican
    reglas — así que si el LLM cae, la cuenta se sigue defendiendo sola.
    """
    for etiqueta, paso in (
        ("audiencias", fable_actions.asegurar_audiencias_en_observacion),  # nunca restringir
        ("log pendiente", _flush_pending_actions),  # un log incompleto ciega el cooldown
        ("caídas", _detectar_caidas),      # un desplome de tráfico es un fallo, no tendencia
        ("reconciliar estado", _reconciliar_estado),  # Google manda, no lo que recordamos
        ("negativas suicidas", _auditar_negativas_suicidas),  # nunca bloquearse a sí mismo
        ("balance de acciones", _evaluar_acciones),  # ¿sirvió lo que hicimos?
        ("autoscale", _autoscale),         # Nivel 3: presupuestos y pujas, con pruebas
    ):
        try:
            paso()
        except Exception as exc:
            print(f"[auditor] {etiqueta} falló (el resto sigue): {exc}", flush=True)

    try:
        review = fable.generate_campaign_review()
        _review_state["error"] = None
        _review_state["fallos"] = 0
        _autopilot(review)      # Nivel 2: lo seguro se ejecuta solo (verificado)
        _maybe_expand(review)   # y el crecimiento lo decide su análisis, no fechas
    except Exception as exc:
        _review_state["error"] = str(exc)
        _review_state["fallos"] = _review_state.get("fallos", 0) + 1
        print(f"[auditor] error (fallo #{_review_state['fallos']}): {exc}", flush=True)
    finally:
        _review_state["running"] = False


def _kick_review(min_age: float = None) -> None:
    """Lanza una revisión respetando la cadencia (3h por defecto) — cada una cuesta API.

    min_age=0 la fuerza (acciones del dueño: aceptar/lanzar/botón manual)."""
    if _review_state["running"]:
        return
    if min_age is None:
        min_age = LIVE_REVIEW_AGE
    # Una revisión que falla no escribe su marca de tiempo, así que el gate de
    # cadencia la ve siempre vencida y la relanza en CADA sync (~23 min): un
    # fallo persistente son ~60 llamadas/día en vez de 8. El 11-ago costó 18h
    # de reintentos inútiles. Se espacia desde el primer fallo.
    fallos = _review_state.get("fallos", 0)
    if min_age > 0 and fallos:
        espera = min(REVIEW_FAIL_BACKOFF * 2 ** (fallos - 1), LIVE_REVIEW_AGE)
        if time.time() - _review_state.get("ultimo_intento", 0) < espera:
            return
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
    _review_state["ultimo_intento"] = time.time()
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
    """Historial COMPLETO: archivo local + Mongo, fusionados por `key`.

    Antes devolvía sólo el archivo si existía. Como el Mac y la nube escriben
    cada uno el suyo, divergían: el 14-ago el archivo local iba 30 acciones por
    detrás y ahí estaban justo los escalados automáticos del cerebro. Analizar
    (o medir en feedback.py) con medio historial da conclusiones falsas.
    """
    por_key: dict = {}
    for origen in (mongo.all_docs("actions", limit=5000),
                   json.loads(ACTIONS_LOG.read_text(encoding="utf-8"))
                   if ACTIONS_LOG.exists() else []):
        for e in origen:
            k = e.get("key")
            if not k:
                continue
            previo = por_key.get(k)
            # ante duplicado gana el más reciente; un ok=True no se pisa con ok=False
            if previo is None or (str(e.get("ts", "")) > str(previo.get("ts", ""))
                                  and not (previo.get("ok") and not e.get("ok"))):
                por_key[k] = e
    return sorted(por_key.values(), key=lambda e: str(e.get("ts", "")))


_pending_actions: list = []


def _flush_pending_actions() -> None:
    """Reintenta en Mongo las entradas del log que no llegaron a la primera.

    El archivo local NO es duradero en la nube: el contenedor de Render se
    recrea en cada deploy y se lleva `actions_log.json`. Una entrada que sólo
    vivió ahí es una entrada perdida, y eso no es cosmético — `_last_touch()`
    lee este log para el cooldown de 48h del escalado: con entradas ausentes el
    freno deja de frenar y una campaña puede recibir dos pasos seguidos.
    """
    global _pending_actions
    if not _pending_actions:
        return
    quedan = [e for e in _pending_actions if not mongo.save("actions", e)]
    salvadas = len(_pending_actions) - len(quedan)
    if salvadas:
        print(f"[log] {salvadas} entrada(s) pendientes salvadas en Mongo", flush=True)
    if quedan:
        print(f"[log] ATENCIÓN: {len(quedan)} entrada(s) del log siguen sin llegar a Mongo "
              f"— el cooldown del escalado las está ignorando", flush=True)
    _pending_actions = quedan


def _action_key(a: dict) -> str:
    campos = {k: a.get(k) for k in ("tipo", "campana", "objetivo", "valor")}
    # el escalado automático puede repetir un valor con semanas de diferencia
    # (bajar y volver a subir): la fecha evita que se lea como "ya aplicada"
    if a.get("auto_scale_day"):
        campos["auto_scale_day"] = a["auto_scale_day"]
    # Los ajustes de dinero son REVERSIBLES por naturaleza: volver a un valor
    # anterior es una decisión nueva, no una repetición. Sin el día en la clave,
    # revertir es imposible — el 18-ago la vuelta de Discovery a $186 se rechazó
    # como "ya estaba aplicada" porque ese importe se había usado dos días antes.
    elif a.get("tipo") in ("ajustar_presupuesto", "ajustar_puja_grupo", "ajustar_tope_cpc",
                           "cambiar_estrategia_puja"):
        campos["dia"] = _account_today().isoformat()
    base = json.dumps(campos, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(base.encode()).hexdigest()[:12]


@app.route("/api/actions/log")
def actions_log():
    return jsonify(applied=_load_actions_log())


def _yt_id(txt: str) -> str:
    """Saca el id de YouTube de una URL pegada, o devuelve el id si ya lo es."""
    t = (txt or "").strip()
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", t)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"[A-Za-z0-9_-]{11}", t) else ""


@app.route("/api/pmax/videos")
def pmax_videos():
    """Vídeos ya enlazados a un grupo de recursos + los disponibles en la cuenta."""
    gid = (request.args.get("group") or "").replace("ag", "")
    if not gid.isdigit():
        return jsonify(ok=False, msg="grupo inválido"), 400
    from google.ads.googleads.client import GoogleAdsClient

    ga = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml")).get_service(
        "GoogleAdsService")
    enlazados, disponibles = [], []
    for b in ga.search_stream(customer_id="4888823590", query=f"""
            SELECT asset.youtube_video_asset.youtube_video_id,
                   asset.youtube_video_asset.youtube_video_title, asset.resource_name
            FROM asset_group_asset
            WHERE asset_group.id = {gid}
              AND asset_group_asset.field_type = 'YOUTUBE_VIDEO'"""):
        for r in b.results:
            enlazados.append({"id": r.asset.youtube_video_asset.youtube_video_id,
                              "titulo": r.asset.youtube_video_asset.youtube_video_title,
                              "asset": r.asset.resource_name})
    ya = {v["id"] for v in enlazados}
    for b in ga.search_stream(customer_id="4888823590", query="""
            SELECT asset.youtube_video_asset.youtube_video_id,
                   asset.youtube_video_asset.youtube_video_title, asset.resource_name
            FROM asset WHERE asset.type = 'YOUTUBE_VIDEO'"""):
        for r in b.results:
            v = r.asset.youtube_video_asset.youtube_video_id
            if v and v not in ya and not any(d["id"] == v for d in disponibles):
                disponibles.append({"id": v,
                                    "titulo": r.asset.youtube_video_asset.youtube_video_title,
                                    "asset": r.asset.resource_name})
    return jsonify(ok=True, enlazados=enlazados, disponibles=disponibles)


@app.route("/api/pmax/videos", methods=["POST"])
def pmax_videos_edit():
    """Añade o quita vídeos de un grupo de recursos.

    `añadir` acepta ids de YouTube o URLs pegadas: si el vídeo aún no existe como
    asset en la cuenta se crea al vuelo, para poder subir material nuevo del canal
    sin salir del panel.
    """
    from google.ads.googleads.client import GoogleAdsClient
    from google.ads.googleads.errors import GoogleAdsException

    body = request.get_json(silent=True) or {}
    gid = str(body.get("group") or "").replace("ag", "")
    if not gid.isdigit():
        return jsonify(ok=False, msg="grupo inválido"), 400
    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    cid = "4888823590"
    ga = client.get_service("GoogleAdsService")
    ag_rn = f"customers/{cid}/assetGroups/{gid}"
    hechos = []

    # QUITAR: se borra el vínculo con el grupo, no el asset (sigue reutilizable)
    for rn in body.get("quitar") or []:
        op = client.get_type("AssetGroupAssetOperation")
        op.remove = rn
        try:
            client.get_service("AssetGroupAssetService").mutate_asset_group_assets(
                customer_id=cid, operations=[op])
            hechos.append("quitado 1 vídeo")
        except GoogleAdsException as ex:
            return jsonify(ok=False, msg=ex.failure.errors[0].message[:180]), 400

    pendientes = [_yt_id(x) for x in (body.get("añadir") or [])]
    pendientes = [x for x in pendientes if x]
    if pendientes:
        existentes = {}
        for b in ga.search_stream(customer_id=cid, query="""
                SELECT asset.resource_name, asset.youtube_video_asset.youtube_video_id
                FROM asset WHERE asset.type = 'YOUTUBE_VIDEO'"""):
            for r in b.results:
                existentes.setdefault(r.asset.youtube_video_asset.youtube_video_id,
                                      r.asset.resource_name)
        for vid in pendientes:
            rn = existentes.get(vid)
            if not rn:                      # vídeo nuevo del canal: se crea el asset
                op = client.get_type("AssetOperation")
                op.create.youtube_video_asset.youtube_video_id = vid
                op.create.name = f"YT {vid}"
                try:
                    rn = client.get_service("AssetService").mutate_assets(
                        customer_id=cid, operations=[op]).results[0].resource_name
                    hechos.append(f"creado asset para {vid}")
                except GoogleAdsException as ex:
                    return jsonify(ok=False, msg=f"{vid}: {ex.failure.errors[0].message[:150]}"), 400
            op = client.get_type("AssetGroupAssetOperation")
            op.create.asset_group = ag_rn
            op.create.asset = rn
            op.create.field_type = client.enums.AssetFieldTypeEnum.YOUTUBE_VIDEO
            try:
                client.get_service("AssetGroupAssetService").mutate_asset_group_assets(
                    customer_id=cid, operations=[op])
                hechos.append(f"añadido {vid}")
            except GoogleAdsException as ex:
                return jsonify(ok=False, msg=f"{vid}: {ex.failure.errors[0].message[:150]}"), 400

    try:
        _reconciliar_estado()          # que el gestor refleje el cambio al instante
    except Exception as exc:
        print(f"[pmax-videos] reconciliar: {exc}", flush=True)
    return jsonify(ok=True, msg=" · ".join(hechos) or "sin cambios")


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
    # una PROPUESTA aún no existe en Google: se edita el borrador, no la cuenta
    draft = next((c for c in _load_camps()["campaigns"]
                  if c.get("name") == a.get("campana") and c.get("status") == "PROPUESTA"), None)
    result = _apply_to_draft(a, draft) if draft else fable_actions.apply_action(a)
    msg = ("🤖 auto · " if auto else "") + result["msg"]
    entry = dict(key=key, action=a, ok=result["ok"], msg=msg,
                 ts=time.strftime("%Y-%m-%d %H:%M"), auto=auto)
    log.append(entry)
    ACTIONS_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    _flush_pending_actions()
    if not mongo.save("actions", entry):
        _pending_actions.append(entry)
        print(f"[log] la entrada '{entry['key']}' ({a.get('tipo')}) no llegó a Mongo "
              f"— en cola para reintento", flush=True)
    if result["ok"]:
        modo = "auto-aplicada por el piloto Nivel 2" if auto else "aplicada"
        fable.learn(f"Acción {modo} ({a.get('tipo')}): {result['msg']}. Razón: {a.get('razon', '')[:150]}")
        _pulse_cache.update(at=0)  # refrescar pulso tras el cambio
        _sync_store_after_action(a)  # que el gestor (y el frontend) reflejen el cambio
        if a.get("tipo") == "optimizar_titulo_feed":
            threading.Thread(target=lambda: _run("merchant_feed.py"), daemon=True).start()
    return dict(ok=result["ok"], msg=msg, key=key)


def _apply_to_draft(a: dict, draft: dict) -> dict:
    """Ajustes sobre una campaña PROPUESTA: se corrige el plan antes de lanzarla."""
    tipo = a.get("tipo")
    store = _load_camps()
    camp = next(c for c in store["campaigns"] if c["id"] == draft["id"])
    if tipo == "ajustar_presupuesto":
        v = float(a.get("valor") or 0)
        if not 5 <= v <= 300:
            return dict(ok=False, msg=f"presupuesto ${v} fuera de límites (5-300)")
        antes = camp.get("daily_budget_usd")
        camp["daily_budget_usd"] = v
        _save_camps(store)
        return dict(ok=True, msg=f"borrador '{camp['name']}': presupuesto ${antes} → ${v}/día (aún sin lanzar)")
    if tipo in ("pausar_keyword", "quitar_keyword_borrador"):
        obj = fable_actions._clean_kw(a.get("objetivo") or "").lower()
        quitadas = 0
        for g in list(camp.get("ad_groups", [])):
            antes = len(g.get("keywords", []))
            g["keywords"] = [k for k in g.get("keywords", [])
                             if (k.get("text") or "").lower() != obj]
            quitadas += antes - len(g["keywords"])
        # un grupo sin keywords no puede subirse a Google: se elimina del plan
        vacios = [g["name"] for g in camp.get("ad_groups", []) if not g.get("keywords")]
        camp["ad_groups"] = [g for g in camp.get("ad_groups", []) if g.get("keywords")]
        if not quitadas:
            return dict(ok=True, msg=f"'{obj}' no estaba en el borrador")
        _save_camps(store)
        extra = f" (grupo{'s' if len(vacios) > 1 else ''} vacío{'s' if len(vacios) > 1 else ''} eliminado{'s' if len(vacios) > 1 else ''}: {', '.join(vacios)})" if vacios else ""
        return dict(ok=True, msg=f"borrador '{camp['name']}': quitada '{obj}'{extra}")
    if tipo == "ajustar_puja_grupo":
        v = float(a.get("valor") or 0)
        if not 0.20 <= v <= 2.00:
            return dict(ok=False, msg=f"puja ${v} fuera de límites (0.20-2.00)")
        for g in camp.get("product_groups", []) + camp.get("ad_groups", []):
            if g.get("name") == a.get("objetivo"):
                g["cpc_bid_usd"] = v
        _save_camps(store)
        return dict(ok=True, msg=f"borrador '{camp['name']}': puja de '{a.get('objetivo')}' → ${v}")
    return dict(ok=False, msg=f"'{tipo}' no se puede aplicar a un borrador — lánzala primero")


def _sync_store_after_action(a: dict) -> None:
    """El gestor refleja lo que ya cambió en Google Ads (presupuesto, keywords pausadas)."""
    try:
        tipo = a.get("tipo")
        if tipo not in ("ajustar_presupuesto", "pausar_keyword", "reactivar_keyword",
                        "pausar_campana", "activar_campana"):
            return
        store = _load_camps()
        camp = next((c for c in store["campaigns"] if c["name"] == a.get("campana")), None)
        if not camp:
            return
        if tipo in ("pausar_campana", "activar_campana"):
            # Sin esto el gestor sigue diciendo enabled=True tras pausar en Google, y
            # Fable lee ese estado falso: el 16-ago dedicó su alerta de máxima urgencia
            # a denunciar como "quinta reincidencia" dos campañas que SÍ estaban
            # pausadas. Un estado desincronizado no es un detalle cosmético — desvía
            # la atención del auditor hacia un problema que no existe.
            camp["enabled"] = (tipo == "activar_campana")
        elif tipo == "ajustar_presupuesto":
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
    if tipo == "ajustar_puja_grupo":
        # SOLO recortes, y sólo con la sangría confirmada por GAQL. Bajar la puja
        # de un grupo que gastó ≥$25 sin una sola venta no necesita criterio: lo
        # verifica el código. Subirla sí es una apuesta y la decide el dueño.
        stats = fable_actions.grupo_7d_stats(a.get("campana", ""), a.get("objetivo", ""))
        if not stats:
            return False
        cost, conv, puja_actual = stats
        try:
            nueva = float(a.get("valor"))
        except (TypeError, ValueError):
            return False
        if nueva >= puja_actual:
            return False               # subir jamás en automático
        return cost >= 25 and conv == 0
    return False


# --- Nivel 3: Fable escala y recorta solo. Sin techo de gasto: el ROAS es el techo,
#     pero CADA escalón se gana con datos frescos y hay cortacircuitos duros. ---
SCALE_COOLDOWN = 48 * 3600     # un paso por campaña cada 48h, nunca en cadena
SCALE_MIN_ROAS = 3.0           # subir exige rentabilidad probada
SCALE_LOST_IS = 0.20           # ...y demanda real quedando fuera
CUT_MAX_ROAS = 1.0             # recortar si pierde dinero
CUT_MIN_SPEND = 50.0           # ...con evidencia suficiente


def _last_touch(campana: str, tipos: tuple, objetivo: str = None) -> float:
    """Última vez que se movió presupuesto/puja de esa campaña (log completo)."""
    best = 0.0
    for e in _load_actions_log():
        a = e.get("action") or {}
        if not e.get("ok") or a.get("tipo") not in tipos or a.get("campana") != campana:
            continue
        if objetivo is not None and a.get("objetivo") != objetivo:
            continue
        try:
            best = max(best, time.mktime(time.strptime(str(e.get("ts", ""))[:16], "%Y-%m-%d %H:%M")))
        except Exception:
            pass
    return best


def _detectar_caidas() -> list:
    """Desplomes de tráfico: fallo de configuración, no del mercado.

    Compara las impresiones de hoy (hasta la hora actual) con la media de esas
    mismas horas en los 7 días previos. Una campaña sana varía; una rota cae a cero.
    """
    try:
        from datetime import timedelta

        from google.ads.googleads.client import GoogleAdsClient

        client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
        ga = client.get_service("GoogleAdsService")
        hoy = _account_today()
        hora = int(time.strftime("%H"))
        if hora < 6:
            return []                      # muy temprano: aún no hay muestra
        desde = (hoy - timedelta(days=7)).isoformat()
        q = f"""SELECT segments.date, segments.hour, campaign.name, metrics.impressions
                FROM campaign WHERE campaign.status = 'ENABLED'
                  AND segments.date BETWEEN '{desde}' AND '{hoy.isoformat()}'"""
        hoy_s, previos = {}, {}
        for b in ga.search_stream(customer_id="4888823590", query=q):
            for r in b.results:
                if r.segments.hour > hora:
                    continue
                n = r.campaign.name
                if r.segments.date == hoy.isoformat():
                    hoy_s[n] = hoy_s.get(n, 0) + r.metrics.impressions
                else:
                    previos.setdefault(n, {}).setdefault(r.segments.date, 0)
                    previos[n][r.segments.date] += r.metrics.impressions
        alertas = []
        for n, dias in previos.items():
            if len(dias) < 3:
                continue                   # sin historia suficiente
            media = sum(dias.values()) / len(dias)
            actual = hoy_s.get(n, 0)
            if media >= 100 and actual < media * 0.25:
                alertas.append(dict(campana=n, impresiones_hoy=actual,
                                    media_esperada=round(media),
                                    caida_pct=round((1 - actual / media) * 100)))
        for a in alertas:
            print(f"[ALERTA] {a['campana'][:40]}: {a['impresiones_hoy']} impresiones vs "
                  f"{a['media_esperada']} esperadas (−{a['caida_pct']}%) — revisar configuración", flush=True)
        if alertas:
            mongo.save("alertas_caida", {"alertas": alertas})
        return alertas
    except Exception as exc:
        print(f"[caidas] error: {exc}", flush=True)
        return []


def _reconciliar_estado() -> None:
    """El gestor debe reflejar lo que Google dice, no lo que creemos recordar.

    Google Ads es la fuente de verdad: el estado puede cambiar desde la interfaz,
    desde otra máquina o por una pausa que no pasó por _apply_and_log. Cuando el
    gestor y Google discrepan, Fable razona sobre una cuenta que no existe — el
    16-ago gastó su alerta de urgencia ALTA denunciando como "quinta reincidencia"
    dos campañas que llevaban un día correctamente pausadas.
    """
    from google.ads.googleads.client import GoogleAdsClient

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    ga = client.get_service("GoogleAdsService")
    real = {}
    for b in ga.search_stream(customer_id="4888823590", query="""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type, campaign_budget.amount_micros
            FROM campaign WHERE campaign.status IN ('ENABLED', 'PAUSED')"""):
        for r in b.results:
            real[r.campaign.name] = dict(
                id=r.campaign.id,
                enabled=(r.campaign.status.name == "ENABLED"),
                tipo=r.campaign.advertising_channel_type.name,
                budget=r.campaign_budget.amount_micros / 1e6)

    store = _load_camps()
    cambios = []
    for camp in store.get("campaigns", []):
        if camp.get("status") != "LIVE":
            continue          # una PROPUESTA aún no existe en Google
        info = real.get(camp.get("name"))
        if info is None:
            continue
        if camp.get("enabled") is not info["enabled"]:
            cambios.append(f"{camp['name']}: gestor={camp.get('enabled')} → Google={info['enabled']}")
            camp["enabled"] = info["enabled"]

        # El PRESUPUESTO deriva igual que el estado, y ahí duele más: Fable lee
        # `daily_budget_usd` del gestor tanto para razonar como para calcular sus
        # pasos de ±20%. El 18-ago Discovery volvió a $186 en Google sin pasar
        # por el log y el gestor se quedó en $223: una semana de revisiones
        # midiendo cuota perdida contra un presupuesto que no existía, y una
        # propuesta de $267 que el ejecutor iba a rechazar por pasarse del ±30%
        # sobre el importe real. Google manda también en los números.
        try:
            antes = float(camp.get("daily_budget_usd") or 0)
        except (TypeError, ValueError):
            antes = 0.0
        if abs(antes - info["budget"]) >= 0.01:
            cambios.append(f"{camp['name']}: presupuesto gestor=${antes:.2f} → Google=${info['budget']:.2f}")
            camp["daily_budget_usd"] = info["budget"]

    # IMPORTAR lo que existe en Google y falta aquí. Sin esto, una campaña creada
    # fuera del gestor —por API, o a mano en la interfaz de Google— es invisible
    # en el panel: el 17-ago la PMax recién creada no aparecía por ningún lado.
    conocidas = {c.get("name") for c in store.get("campaigns", [])}
    for nombre, info in real.items():
        if nombre in conocidas or not nombre.startswith("JP"):
            continue
        store["campaigns"].append({
            # id ESTABLE derivado del de Google: el panel lo usa para el switch,
            # para abrir la fila y para filtrar. Sin él, al pinchar la campaña se
            # guarda un filtro "undefined" que deja la vista de grupos en cero
            # y sin chip que lo explique (17-ago).
            "id": f"g{info['id']}",
            "name": nombre,
            "status": "LIVE",
            "enabled": info["enabled"],
            "type": info["tipo"],
            "daily_budget_usd": info["budget"],
            "google": {"campaign_id": info["id"]},
            "objective": "Importada desde Google Ads: se creó fuera del gestor.",
            "match_role": info["tipo"],
            "ad_groups": [],
            "cross_negatives": [],
            "pushed_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        cambios.append(f"IMPORTADA desde Google: {nombre} ({info['tipo']}, ${info['budget']:.0f}/día)")

    # PMax no tiene grupos de anuncios ni keywords: tiene GRUPOS DE RECURSOS.
    # Sin traerlos, el panel muestra la campaña vacía por dentro y el dueño no
    # puede ver con qué está compitiendo (17-ago: "solo se ve, pero no se ve
    # cómo está regulada dentro"). Se mapean a `ad_groups` para que el panel los
    # dibuje con la misma estructura que el resto.
    pmax = [c for c in store.get("campaigns", []) if c.get("type") == "PERFORMANCE_MAX"]
    if pmax:
        ids = {c["name"]: c.get("google", {}).get("campaign_id") for c in pmax}
        por_camp: dict = {}
        for b in ga.search_stream(customer_id="4888823590", query="""
                SELECT campaign.name, asset_group.id, asset_group.name,
                       asset_group.final_urls, asset_group.ad_strength,
                       asset_group_asset.field_type, asset.text_asset.text, asset.name,
                       asset.image_asset.full_size.url,
                       asset.image_asset.full_size.width_pixels,
                       asset.image_asset.full_size.height_pixels,
                       asset.youtube_video_asset.youtube_video_id
                FROM asset_group_asset
                WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
                  AND campaign.status IN ('ENABLED', 'PAUSED')"""):
            for r in b.results:
                g = por_camp.setdefault(r.campaign.name, {}).setdefault(r.asset_group.id, {
                    "id": f"ag{r.asset_group.id}", "name": r.asset_group.name,
                    "final_url": (list(r.asset_group.final_urls) or [""])[0],
                    "ad_strength": r.asset_group.ad_strength.name,
                    "headlines": [], "long_headlines": [], "descriptions": [],
                    "imagenes": [], "videos": [], "n_imagenes": 0})
                campo = r.asset_group_asset.field_type.name
                txt = r.asset.text_asset.text
                url = r.asset.image_asset.full_size.url
                vid = r.asset.youtube_video_asset.youtube_video_id
                if txt:
                    destino = {"HEADLINE": "headlines", "LONG_HEADLINE": "long_headlines",
                               "DESCRIPTION": "descriptions"}.get(campo)
                    if destino and txt not in g[destino]:
                        g[destino].append(txt)
                elif url:
                    # se guarda la URL, no sólo el número: el dueño quiere VER con
                    # qué creatividades está compitiendo, no cuántas hay.
                    if not any(i["url"] == url for i in g["imagenes"]):
                        g["imagenes"].append({"url": url, "tipo": campo,
                                              "w": r.asset.image_asset.full_size.width_pixels,
                                              "h": r.asset.image_asset.full_size.height_pixels})
                        g["n_imagenes"] += 1
                elif vid:
                    if not any(v["id"] == vid for v in g["videos"]):
                        g["videos"].append({"id": vid, "titulo": r.asset.name or ""})
        for camp in pmax:
            grupos = list((por_camp.get(camp["name"]) or {}).values())
            if grupos and camp.get("ad_groups") != grupos:
                camp["ad_groups"] = grupos
                camp["products"] = "todos los del feed"
                cambios.append(f"{camp['name']}: {len(grupos)} grupo(s) de recursos sincronizados")

    if cambios:
        _save_camps(store)
        for c in cambios:
            print(f"[reconciliar] {c}", flush=True)


def _evaluar_acciones() -> None:
    """Mide si las acciones ya ejecutadas sirvieron (ver feedback.py).

    Import perezoso: feedback.py importa app para la zona horaria y el log, así
    que a nivel de módulo esto sería un ciclo.
    """
    import feedback

    res = feedback.evaluar()
    nuevas = (res or {}).get("nuevas_esta_vez") or 0
    if nuevas:
        pt = res.get("por_tipo") or {}
        resumen = " · ".join(
            f"{t}: {d['tasa_acierto_pct']}% de {d['juzgadas']}"
            for t, d in pt.items() if d.get("tasa_acierto_pct") is not None)
        print(f"[balance] {nuevas} acciones evaluadas — {resumen}", flush=True)


def _auditar_negativas_suicidas() -> None:
    """Elimina las negativas que bloquean las keywords activas de su propia campaña.

    Ya ha pasado tres veces (dos costaron $149 y $609): el ejecutor tiene guarda
    para las que añade Fable en caliente, pero las que entran al CREAR la campaña
    —listas heredadas de plantilla— no pasan por ahí. El 12-ago 'benefits' [BROAD]
    anulaba 'pickle juice benefits' en Electrolytes: la campaña pagaba por tener
    esa keyword y se la bloqueaba a sí misma, así que era imposible que convirtiera.

    Una negativa que anula una keyword propia no es una decisión discutible, es una
    contradicción — se retira sola y se deja constancia en el log.
    """
    from google.ads.googleads.client import GoogleAdsClient

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    ga = client.get_service("GoogleAdsService")
    cid = "4888823590"

    kws: dict[str, list[tuple]] = {}
    for b in ga.search_stream(customer_id=cid, query="""
            SELECT campaign.name, ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type FROM keyword_view
            WHERE ad_group_criterion.status = 'ENABLED' AND campaign.status = 'ENABLED'"""):
        for r in b.results:
            kws.setdefault(r.campaign.name, []).append(
                (r.ad_group_criterion.keyword.text.lower(),
                 r.ad_group_criterion.keyword.match_type.name))

    quitar = []
    for b in ga.search_stream(customer_id=cid, query="""
            SELECT campaign.name, campaign_criterion.keyword.text,
                   campaign_criterion.keyword.match_type, campaign_criterion.resource_name
            FROM campaign_criterion WHERE campaign_criterion.negative = TRUE
              AND campaign_criterion.type = 'KEYWORD' AND campaign.status = 'ENABLED'"""):
        for r in b.results:
            mias = kws.get(r.campaign.name)
            if not mias:
                continue                      # Shopping no tiene keywords: nada que anular
            texto = r.campaign_criterion.keyword.text.lower()
            match = r.campaign_criterion.keyword.match_type.name
            # El MATCH importa en los dos lados. Una negativa EXACT sobre una
            # keyword PHRASE no la anula: desvía el término literal a la campaña
            # de control y la frase sigue explorando variantes — es el flujo
            # normal entre campañas propias. El 15-ago este auditor borró esa
            # negativa legítima de 'pickled tomatoes' porque sólo comparaba textos.
            if match == "BROAD":               # bloquea si TODAS sus palabras están en la keyword
                choca = [k for k, _ in mias if set(texto.split()) <= set(k.split())]
            elif match == "PHRASE":
                choca = [k for k, _ in mias if texto in k]
            else:
                choca = [k for k, km in mias if texto == k and km == "EXACT"]
            if choca:
                quitar.append((r.campaign.name, texto, match,
                               r.campaign_criterion.resource_name, choca))

    if not quitar:
        return
    svc = client.get_service("CampaignCriterionService")
    for camp, texto, match, rn, choca in quitar:
        op = client.get_type("CampaignCriterionOperation")
        op.remove = rn
        try:
            svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
            msg = (f"🤖 auto · negativa '{texto}' [{match}] retirada de {camp}: "
                   f"anulaba su propia keyword {choca[:2]}")
            print(f"[negativas] {msg}", flush=True)
            # No pasa por _apply_and_log: apply_action no ejecuta este tipo, la
            # retirada ya se hizo aquí. Se escribe la entrada con el mismo formato.
            accion = {"tipo": "quitar_negativa_suicida", "campana": camp,
                      "objetivo": texto,
                      "motivo": f"bloqueaba keywords propias: {choca[:3]}"}
            entry = dict(key=_action_key(accion), action=accion, ok=True, msg=msg,
                         ts=time.strftime("%Y-%m-%d %H:%M"), auto=True)
            log = _load_actions_log()
            log.append(entry)
            ACTIONS_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
            mongo.save("actions", entry)
            fable.learn(f"Negativa suicida retirada automáticamente en {camp}: "
                        f"'{texto}' [{match}] anulaba {choca[:2]}. Al crear campañas, "
                        f"cruzar SIEMPRE la lista de negativas heredadas contra las "
                        f"keywords propias antes de subirla.")
        except Exception as exc:
            print(f"[negativas] no se pudo retirar '{texto}' de {camp}: {exc}", flush=True)


def _autoscale() -> None:
    """Escalado autónomo: presupuestos y pujas, verificado contra Google Ads."""
    try:
        from datetime import timedelta

        from google.ads.googleads.client import GoogleAdsClient

        client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
        ga = client.get_service("GoogleAdsService")
        cid = "4888823590"
        hoy = _account_today()
        d3 = (hoy - timedelta(days=2)).isoformat()
        aprendiendo = set(fable._learning_campaigns())
        ahora = time.time()

        q = f"""SELECT campaign.name, campaign.status, campaign.bidding_strategy_type,
                campaign_budget.amount_micros, metrics.cost_micros, metrics.conversions,
                metrics.conversions_value, metrics.search_budget_lost_impression_share,
                metrics.search_rank_lost_impression_share
                FROM campaign WHERE campaign.status = 'ENABLED'
                  AND segments.date BETWEEN '{d3}' AND '{hoy.isoformat()}'"""
        camps = []
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                m = r.metrics
                camps.append(dict(
                    name=r.campaign.name, estrategia=r.campaign.bidding_strategy_type.name,
                    budget=r.campaign_budget.amount_micros / 1e6,
                    cost=m.cost_micros / 1e6, conv=m.conversions, value=m.conversions_value,
                    perdido_presup=m.search_budget_lost_impression_share,
                    perdido_rank=m.search_rank_lost_impression_share))

        # CORTACIRCUITO: si hay gasto real y CERO conversiones en toda la cuenta,
        # el tracking puede estar roto — no se escala a ciegas.
        gasto_total = sum(c["cost"] for c in camps)
        conv_total = sum(c["conv"] for c in camps)
        if gasto_total > 50 and conv_total == 0:
            print("[autoscale] CORTACIRCUITO: gasto sin ninguna conversión — no se escala", flush=True)
            return

        for c in camps:
            if c["name"] in aprendiendo:
                continue
            if ahora - _last_touch(c["name"], ("ajustar_presupuesto",)) < SCALE_COOLDOWN:
                continue
            roas = c["value"] / c["cost"] if c["cost"] else 0
            manual = c["estrategia"] == "MANUAL_CPC"
            nuevo = razon = None

            # Tras cambiar la estrategia de puja hay 1-2 semanas de reaprendizaje con
            # ROAS inestable. SUBIR con ese dato es amplificar ruido; el recorte
            # defensivo sí se permite, porque un desplome real sigue siendo un desplome.
            reaprendiendo = _recent_doc("strategy_changes", {"campaign": c["name"]}, 14)

            if (roas >= SCALE_MIN_ROAS and c["perdido_presup"] > SCALE_LOST_IS
                    and c["cost"] > 10 and not reaprendiendo):
                paso = 0.50 if manual else 0.30      # el guardián permite más; se va sobrio
                nuevo = round(c["budget"] * (1 + paso), 2)
                razon = (f"ROAS {roas:.1f}x en 3 días con {c['perdido_presup']*100:.0f}% de subastas "
                         f"perdidas por presupuesto — el presupuesto es el cuello, no la demanda.")
            elif roas < CUT_MAX_ROAS and c["cost"] >= CUT_MIN_SPEND:
                nuevo = round(max(5.0, c["budget"] * 0.70), 2)
                razon = (f"ROAS {roas:.1f}x con ${c['cost']:.0f} gastados en 3 días — recorte "
                         f"defensivo del 30% mientras se corrige.")

            if nuevo and abs(nuevo - c["budget"]) >= 1:
                a = dict(tipo="ajustar_presupuesto", campana=c["name"], valor=nuevo,
                         razon=f"🤖 Escalado automático (Nivel 3): {razon}",
                         auto_scale_day=hoy.isoformat())
                r = _apply_and_log(a, auto=True)
                print(f"[autoscale] {c['name'][:35]}: ${c['budget']}→${nuevo} · {r['msg'][:60]}", flush=True)

        # PUJAS de Shopping: atacan lo que se pierde por rank, no por presupuesto
        for c in camps:
            if c["name"] in aprendiendo or c["estrategia"] != "MANUAL_CPC":
                continue
            if c["perdido_rank"] <= SCALE_LOST_IS:
                continue
            qg = f"""SELECT ad_group.name, ad_group.cpc_bid_micros, metrics.cost_micros,
                     metrics.conversions, metrics.conversions_value FROM ad_group
                     WHERE campaign.name = '{c['name']}' AND ad_group.status = 'ENABLED'
                       AND segments.date BETWEEN '{d3}' AND '{hoy.isoformat()}'"""
            for b in ga.search_stream(customer_id=cid, query=qg):
                for r in b.results:
                    g, m = r.ad_group, r.metrics
                    if ahora - _last_touch(c["name"], ("ajustar_puja_grupo",), g.name) < SCALE_COOLDOWN:
                        continue
                    cost = m.cost_micros / 1e6
                    puja = g.cpc_bid_micros / 1e6
                    groas = m.conversions_value / cost if cost else 0
                    if groas >= SCALE_MIN_ROAS and cost > 5:
                        nueva = round(min(2.00, puja * 1.25), 2)
                        motivo = (f"grupo con ROAS {groas:.1f}x y la campaña pierde "
                                  f"{c['perdido_rank']*100:.0f}% de subastas por puja")
                    elif cost >= 25 and m.conversions == 0:
                        nueva = round(max(0.20, puja * 0.75), 2)
                        motivo = f"grupo con ${cost:.0f} y 0 conversiones en 3 días"
                    else:
                        continue
                    if abs(nueva - puja) < 0.05:
                        continue
                    a = dict(tipo="ajustar_puja_grupo", campana=c["name"], objetivo=g.name,
                             valor=nueva, razon=f"🤖 Escalado automático (Nivel 3): {motivo}",
                             auto_scale_day=hoy.isoformat())
                    res = _apply_and_log(a, auto=True)
                    print(f"[autoscale] puja {g.name[:28]}: ${puja}→${nueva} · {res['msg'][:50]}", flush=True)
    except Exception as exc:
        print(f"[autoscale] error: {exc}", flush=True)


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
    activa = bool(body.get("enabled"))
    store = _load_camps()
    msg = None
    for c in store["campaigns"]:
        if c["id"] != body.get("id"):
            continue
        c["enabled"] = activa
        # si ya vive en Google Ads, el switch manda de verdad
        if c.get("status") == "LIVE":
            r = fable_actions.set_campaign_status(c["name"], activa)
            msg = r["msg"]
            if r["ok"]:
                if activa:
                    c.pop("paused_at", None)
                    c.pop("pause_reason", None)
                else:
                    c["paused_at"] = time.strftime("%Y-%m-%d")
                _live_cache.clear()
    _save_camps(store)
    return jsonify(ok=True, msg=msg)


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

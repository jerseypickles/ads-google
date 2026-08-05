"""Fable (claude-fable-5) como motor de estrategia dentro de la app.

Genera el plan de campañas Search de Jersey Pickles a partir de los datos
reales de keywords y rendimiento (dashboard_data). Produce listings completos:
grupos de anuncios, keywords con concordancia, anuncios RSA y negativas.

Rutas de acceso al modelo, en orden:
  1. SDK oficial `anthropic` si hay credenciales (ANTHROPIC_API_KEY, etc.)
  2. CLI `claude` en modo headless (usa la suscripción de Claude Code)

El plan se guarda en fable_plan.json.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import db as mongo
from dashboard_data import build_data

BASE = Path(__file__).parent
PLAN_PATH = BASE / "fable_plan.json"
MODEL = "claude-fable-5"

# ---------------------------------------------------------------------------
# Memoria de Fable: lecciones que acumula durante el proyecto
# ---------------------------------------------------------------------------

MEMORY_PATH = BASE / "fable_memory.json"


def load_lessons() -> list[dict]:
    if MEMORY_PATH.exists():
        return json.loads(MEMORY_PATH.read_text(encoding="utf-8")).get("lessons", [])
    docs = mongo.all_docs("lessons", limit=10000)  # espejo en la nube
    return docs[-40:]


def learn(lesson: str) -> None:
    """Añade una lección nueva (sin duplicados, máx. 40)."""
    lessons = load_lessons()
    if any(lesson.strip() == entry["lesson"].strip() for entry in lessons):
        return
    entry = {"date": datetime.now().strftime("%Y-%m-%d"), "lesson": lesson.strip()}
    lessons.append(entry)
    MEMORY_PATH.write_text(
        json.dumps({"lessons": lessons[-40:]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    mongo.upsert("lessons", {"lesson": entry["lesson"]}, entry)


def _account_today():
    """'Hoy' en la zona de la cuenta (NY) — el servidor de la nube vive en UTC."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date()


def _lessons_block() -> str:
    lessons = load_lessons()
    if not lessons:
        return ""
    txt = "\n".join(f"- ({entry['date']}) {entry['lesson']}" for entry in lessons[-20:])
    return (
        "\n\nLECCIONES QUE HAS APRENDIDO EN ESTE PROYECTO — aplícalas SIEMPRE, "
        f"no repitas errores pasados:\n{txt}\n"
    )


def _manager_state() -> str:
    store = _load_store()
    lines = [
        f"- {c['name']} [{'EN GOOGLE ADS' if c.get('status') == 'LIVE' else 'borrador'}] ${c.get('daily_budget_usd')}/día"
        for c in store.get("campaigns", [])
    ]
    if not lines:
        return ""
    return (
        "\n\nCAMPAÑAS QUE YA EXISTEN (gestor o Google Ads) — NO las dupliques; "
        "un plan nuevo debe aportar SOLO valor incremental (nuevos temas, tendencias "
        "del vigía, expansiones estacionales):\n" + "\n".join(lines)
    )


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy_summary": {"type": "string"},
        "campaigns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "objective": {"type": "string"},
                    "match_role": {"type": "string"},
                    "daily_budget_usd": {"type": "number"},
                    "budget_rationale": {"type": "string"},
                    "cross_negatives": {"type": "array", "items": {"type": "string"}},
                    "ad_groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "rationale": {"type": "string"},
                                "keywords": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "text": {"type": "string"},
                                            "match": {"type": "string", "enum": ["EXACT", "PHRASE", "BROAD"]},
                                        },
                                        "required": ["text", "match"],
                                        "additionalProperties": False,
                                    },
                                },
                                "headlines": {"type": "array", "items": {"type": "string"}},
                                "descriptions": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name", "rationale", "keywords", "headlines", "descriptions"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["name", "objective", "match_role", "daily_budget_usd",
                             "budget_rationale", "cross_negatives", "ad_groups"],
                "additionalProperties": False,
            },
        },
        "negatives": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["strategy_summary", "campaigns", "negatives", "next_steps"],
    "additionalProperties": False,
}


def build_prompt() -> str:
    data = build_data()
    kws = [
        {
            "kw": k["kw"], "vol_ultimo_mes": k.get("vol_last"), "tendencia_3m_pct": k.get("mom3"),
            "vol_prom_12m": k["vol"], "competencia": k["comp"],
            "puja_usd": [k["low"], k["high"]],
        }
        for k in data["keywords"]
    ]
    camps = [
        {"campana": c["name"], "coste": c["cost"], "conversiones": c["conv"], "cpa": c["cpa"]}
        for c in data["campaigns"]
    ]
    context = json.dumps(
        {"keywords_validadas": kws, "historico_campanas_12m": camps},
        ensure_ascii=False,
    )

    return f"""Eres el estratega de Google Ads de Jersey Pickles (jerseypickles.com), \
tienda e-commerce de pickles y fermentados artesanales que envía a todo EE.UU. (base en Nueva Jersey). \
Ticket medio ~$66. Históricamente sus Performance Max convirtieron a ~$2.77/compra con ROAS ~23x. \
Ahora vamos a lanzar su primera campaña de BÚSQUEDA (Search) aprovechando el pico estacional \
ago-sep de la categoría. El dueño acepta competencia alta si el CPC lo justifica.

DATOS REALES (Google Ads API, keywords validadas con volumen del último mes, tendencia 3m, \
competencia y rango de pujas; e histórico de campañas):
{context}

TAREA: diseña el plan de campañas Search listo para crear.

ESTRUCTURA POR CONCORDANCIA — flujo profesional obligatorio:
- Separa las campañas POR TIPO DE CONCORDANCIA, no las mezcles dentro de un grupo:
  · Campaña EXACT ("control"): solo keywords en EXACT — las de mayor intención y volumen. \
Aquí se controla puja y término al milímetro. match_role: "EXACT — control".
  · Campaña PHRASE ("descubrimiento"): las mismas temáticas en PHRASE para captar variantes \
y long-tail que aún no conocemos. match_role: "PHRASE — descubrimiento".
- FLUJO DE TRÁFICO entre ambas: pon TODAS las keywords de la campaña EXACT como negativas \
exactas en cross_negatives de la campaña PHRASE. Así una búsqueda exacta entra SIEMPRE por \
la campaña de control y la PHRASE solo pesca lo nuevo. La campaña EXACT lleva cross_negatives: [].
- En next_steps incluye el ciclo semanal: términos de búsqueda ganadores de PHRASE se \
promueven a EXACT (y se negativizan en PHRASE).
- BROAD: no en el lanzamiento. Solo menciónalo en next_steps como fase 2 si procede.

PRESUPUESTO INTELIGENTE — calcula, no inventes:
- CPC esperado de un grupo ≈ promedio de puja_usd[1] (puja alta) de sus keywords. En competencia \
HIGH asume que pagarás cerca del tope; en LOW/MEDIUM ~60% del tope.
- Un grupo necesita ≥10 clics/día para aprender → mínimo del grupo ≈ 10 × CPC esperado.
- daily_budget_usd de la campaña = suma de los mínimos de sus grupos, redondeado hacia arriba.
- Si una keyword de volumen alto tiene competencia HIGH y puja alta (p. ej. 'pickles' hasta $1.84), \
NO la dejes ahogada con presupuesto simbólico: o le asignas presupuesto suficiente para competir, \
o la mueves a un grupo/campaña de menor prioridad y lo explicas. El dueño ACEPTA invertir más \
cuando el dato lo justifica.
- Escribe el cálculo en budget_rationale con números explícitos \
(ej. "5 grupos × ~10 clics × CPC medio $0.60 ≈ $30/día; el grupo 'pickles' EXACT necesita $18 él solo").
- Total combinado razonable: $30-100/día.

REGLAS RESTANTES:
- 3 a 6 grupos por campaña, temáticamente coherentes; los grupos de la PHRASE espejan los temas de la EXACT.
- Usa SOLO keywords de los datos o variantes muy cercanas; prioriza volumen del último mes, \
tendencia positiva e intención de compra. Excluye informacionales/recetas.
- Por grupo: 8-12 titulares RSA (máx. 30 caracteres CADA UNO, en inglés, orientados a venta: \
envío, artesanal, Nueva Jersey, gift, small-batch...) y 4 descripciones (máx. 90 caracteres c/u, inglés).
- negatives (lista global): recetas/DIY, competidores, geografías fuera de EE.UU., temas virales ajenos.
- next_steps: 3-5 pasos concretos post-lanzamiento con umbrales numéricos (cuándo subir presupuesto, \
cuándo pausar una keyword, ciclo PHRASE→EXACT).
- Textos de anuncios en INGLÉS; todo lo demás en ESPAÑOL.

Responde ÚNICAMENTE con un JSON válido con esta forma exacta (sin markdown, sin texto extra):
{{
  "strategy_summary": "...",
  "campaigns": [{{"name": "...", "objective": "...", "match_role": "EXACT — control",
    "daily_budget_usd": 0, "budget_rationale": "cálculo con números",
    "cross_negatives": ["..."],
    "ad_groups": [{{"name": "...", "rationale": "...",
      "keywords": [{{"text": "...", "match": "EXACT|PHRASE|BROAD"}}],
      "headlines": ["..."], "descriptions": ["..."]}}]}}],
  "negatives": ["..."],
  "next_steps": ["..."]
}}{_lessons_block()}{_manager_state()}"""


def _have_sdk_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _sdk_text(prompt: str, max_tokens: int = 32000) -> str:
    """Llamada por API (streaming para prompts/salidas largas), con reintento simple."""
    from anthropic import Anthropic

    client = Anthropic()
    with client.messages.stream(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("Fable rechazó la petición.")
    return next(b.text for b in response.content if b.type == "text")


def _generate_via_sdk(prompt: str) -> dict:
    return _extract_json(_sdk_text(prompt))


def _extract_json(raw: str) -> dict:
    """Saca el primer objeto JSON válido de una respuesta con texto alrededor.

    raw_decode corta en el primer objeto COMPLETO (ignora texto posterior),
    que es más robusto que el regex codicioso ante '}' sueltos al final.
    """
    start = raw.find("{")
    if start == -1:
        raise RuntimeError(f"Fable no devolvió JSON: {raw[:200]}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        return obj
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.S)  # último intento: bloque codicioso
    if not match:
        raise RuntimeError(f"Fable no devolvió JSON: {raw[:200]}")
    return json.loads(match.group(0))


def _generate_via_cli(prompt: str) -> dict:
    last_err = None
    for intento in range(2):  # el JSON largo a veces sale malformado: reintentar 1 vez
        proc = subprocess.run(
            ["claude", "--model", MODEL, "-p", prompt],
            capture_output=True, text=True, timeout=1200,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI falló ({proc.returncode}): {proc.stderr[:400]}")
        try:
            return _extract_json(proc.stdout.strip())
        except (json.JSONDecodeError, RuntimeError) as exc:
            last_err = exc
            print(f"[fable] JSON inválido (intento {intento + 1}/2): {exc}", flush=True)
    raise RuntimeError(f"Fable devolvió JSON inválido dos veces: {last_err}")


def _call_json(prompt: str, max_tokens: int = 32000) -> dict:
    """Llamada genérica a Fable que devuelve JSON parseado (SDK o CLI)."""
    if _have_sdk_credentials():
        last = None
        for _ in range(2):  # JSON largo a veces malformado: reintentar 1 vez
            try:
                return _extract_json(_sdk_text(prompt, max_tokens))
            except (json.JSONDecodeError, RuntimeError) as exc:
                last = exc
                print(f"[fable] JSON inválido vía SDK, reintento: {exc}", flush=True)
        raise RuntimeError(f"Fable devolvió JSON inválido dos veces (SDK): {last}")
    return _generate_via_cli(prompt)


AUDIT_RULES = """\
- Orden de palabras de CADA keyword tal como se busca realmente (singular/plural: \
'pickle spears', no 'pickles spears'; 'kosher pickles', no 'pickles kosher').
- Coherencia keyword↔grupo↔anuncio; keywords EXACT y sus pares PHRASE espejadas entre campañas.
- Presupuestos vs CPCs; titulares >30 car. o descripciones >90; titulares duplicados o casi iguales.
- Flujo de cross_negatives correcto (la PHRASE bloquea todas las EXACT).
- Negativas informacionales (recetas/DIY/how to/canning) presentes en la lista global."""


def _audit_plan(plan: dict) -> dict:
    campañas = [
        {k: v for k, v in c.items() if k != "_meta"} for c in plan.get("campaigns", [])
    ]
    prompt = f"""Eres el auditor de Google Ads de Jersey Pickles. Audita este plan BORRADOR \
(pre-lanzamiento) con ojo crítico y despiadado.

PLAN: {json.dumps({"campaigns": campañas, "negatives": plan.get("negatives", [])}, ensure_ascii=False)}

QUÉ AUDITAR:
{AUDIT_RULES}

Responde SOLO con JSON válido (sin markdown), en español:
{{"resumen": "...", "por_campana": [{{"name": "nombre exacto", "salud": "ok|atencion|critico", "notas": ["..."]}}], "recomendaciones": ["..."]}}
Si el plan está bien, di "ok" — no inventes problemas.{_lessons_block()}"""
    return _call_json(prompt, max_tokens=8000)


def _refine_plan(plan: dict, review: dict) -> dict:
    plan_sin_meta = {k: v for k, v in plan.items() if k != "_meta"}
    review_sin_meta = {k: v for k, v in review.items() if k != "_meta"}
    prompt = f"""Eres el estratega de Google Ads de Jersey Pickles. Este es TU plan y la \
auditoría interna que encontró problemas. CORRIGE TODO lo señalado sin romper lo que está bien.

REGLAS AL CORREGIR:
- Mantén los NOMBRES de campaña EXACTAMENTE iguales.
- Mantén la estructura EXACT-control / PHRASE-descubrimiento y recalcula cross_negatives si \
cambias keywords EXACT (la PHRASE debe bloquear todas las EXACT finales).
- Titulares ≤30 caracteres, descripciones ≤90, en inglés; el resto en español.
- Devuelve el plan COMPLETO corregido, mismo formato JSON exacto que el plan original \
(strategy_summary, campaigns[name, objective, match_role, daily_budget_usd, budget_rationale, \
cross_negatives, ad_groups[name, rationale, keywords[text, match], headlines, descriptions]], \
negatives, next_steps). SOLO el JSON, sin markdown.

PLAN ACTUAL: {json.dumps(plan_sin_meta, ensure_ascii=False)}

AUDITORÍA: {json.dumps(review_sin_meta, ensure_ascii=False)}{_lessons_block()}"""
    return _call_json(prompt)


def generate_plan() -> dict:
    """Genera el plan con bucle auto-correctivo: escribir → auditarse → corregirse."""
    prompt = build_prompt()
    if _have_sdk_credentials():
        plan = _generate_via_sdk(prompt)
        engine = "anthropic-sdk"
    else:
        plan = _generate_via_cli(prompt)
        engine = "claude-code-cli"

    rounds = 0
    final_health = "ok"
    for _ in range(2):
        try:
            review = _audit_plan(plan)
        except Exception as exc:
            print(f"[fable] auditoría falló ({exc}) — el plan sigue", flush=True)
            final_health = "auditoría no disponible"
            break
        bad = [p for p in review.get("por_campana", []) if p.get("salud") != "ok"]
        if not bad:
            final_health = "ok"
            break
        # aprender de los errores para no repetirlos en futuros planes
        for pc in bad:
            for nota in pc.get("notas", [])[:2]:
                learn(f"El auditor marcó en un plan generado: {nota}")
        try:
            plan = _refine_plan(plan, review)
            final_health = "corregido"
        except Exception as exc:
            print(f"[fable] refinado falló ({exc}) — se conserva el plan previo", flush=True)
            final_health = "corrección falló; se conservó el plan previo"
            break
        rounds += 1

    plan["_meta"] = {
        "model": MODEL,
        "engine": engine,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "self_review_rounds": rounds,
        "self_review_result": final_health,
    }
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.save("plans", plan)
    return plan


def load_plan() -> dict | None:
    if PLAN_PATH.exists():
        return json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    return mongo.latest("plans")  # espejo en la nube


# ---------------------------------------------------------------------------
# El vigía: Fable observa las keywords de forma continua
# ---------------------------------------------------------------------------

WATCH_PATH = BASE / "fable_watch.json"


def _lateral_candidates() -> list[str]:
    """Candidatas del autocompletado que aún no están en la tabla validada."""
    import csv
    import glob

    known: set[str] = set()
    hist = sorted(glob.glob(str(BASE / "historical_metrics_*.csv")))
    if hist:
        with open(hist[-1], newline="", encoding="utf-8") as f:
            known = {r["keyword"] for r in csv.DictReader(f)}

    seeds = sorted(glob.glob(str(BASE / "seed_candidates_*.csv")))
    if not seeds:
        return []
    fresh = []
    with open(seeds[-1], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            kw = r["keyword_candidata"].strip().lower()
            if kw and kw not in known:
                fresh.append(kw)
    return fresh[:40]


SEEDS_DYN_PATH = BASE / "seeds_dynamic.json"


def _dynamic_seeds() -> list[str]:
    try:
        return json.loads(SEEDS_DYN_PATH.read_text(encoding="utf-8")).get("seeds", [])
    except Exception:
        return []


def _clean_seed_terms(raw: str) -> list[str]:
    """De una propuesta libre saca términos de búsqueda limpios (1-5 palabras)."""
    raw = re.sub(r"\([^)]*\)", "", raw or "")  # fuera explicaciones entre paréntesis
    out = []
    for part in raw.split("/"):
        term = re.sub(r"\s+", " ", part).strip(" .,;:-").lower()
        if 2 < len(term) <= 40 and 1 <= len(term.split()) <= 5:
            out.append(term)
    return out


def _add_dynamic_seeds(nuevas: list[str]) -> list[str]:
    seeds = _dynamic_seeds()
    added = []
    for raw in nuevas:
        for s in _clean_seed_terms(raw):
            if s not in seeds:
                seeds.append(s)
                added.append(s)
    SEEDS_DYN_PATH.write_text(
        json.dumps({"seeds": seeds[-60:]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return added


def build_watch_prompt() -> str:
    data = build_data()
    kws = [
        {
            "kw": k["kw"], "vol_ultimo_mes": k.get("vol_last"),
            "tendencia_3m_pct": k.get("mom3"), "competencia": k["comp"],
            "puja_usd": [k["low"], k["high"]],
        }
        for k in data["keywords"]
    ]
    laterales = _lateral_candidates()
    plan = load_plan()
    plan_kws = []
    if plan:
        for c in plan.get("campaigns", []):
            for g in c.get("ad_groups", []):
                plan_kws += [k["text"] for k in g.get("keywords", [])]
    deep = _live_deep_data()
    terminos_reales = deep.get("terminos_busqueda_ayer_y_hoy", [])

    return f"""Eres el vigía de keywords de Jersey Pickles (e-commerce de pickles artesanales y \
encurtidos, EE.UU.; también venden aceitunas). Tu trabajo: observar los datos frescos, avisar de \
lo que importa, y EXPANDIR el territorio de búsqueda — que nunca sean siempre las mismas palabras.

KEYWORDS VALIDADAS (volumen último mes, tendencia 3m %, competencia, puja): \
{json.dumps(kws, ensure_ascii=False)}

CANDIDATAS LATERALES del autocompletado (aún sin validar): {json.dumps(laterales, ensure_ascii=False)}

TÉRMINOS REALES que la gente escribió ayer/hoy en tus anuncios: \
{json.dumps(terminos_reales, ensure_ascii=False)}

KEYWORDS YA EN EL PLAN: {json.dumps(sorted(set(plan_kws)), ensure_ascii=False)}

SEMILLAS DE EXPLORACIÓN YA ACTIVAS (no las repitas): {json.dumps(_dynamic_seeds(), ensure_ascii=False)}

Analiza y responde SOLO con JSON válido (sin markdown), en español:
{{
  "resumen": "2-3 frases: la foto del momento",
  "subiendo": [{{"kw": "...", "nota": "por qué importa, 1 frase"}}],
  "bajando": [{{"kw": "...", "nota": "..."}}],
  "nuevas_laterales": [{{"kw": "...", "nota": "por qué vale la pena validarla o ignorarla"}}],
  "semillas_nuevas": ["SOLO el término de búsqueda limpio, 1-4 palabras, en inglés, sin \
explicaciones ni paréntesis (la justificación va en acciones). Nuevas direcciones: categorías \
adyacentes del catálogo, patrones de los términos reales, regalo/estacional, formatos. Que NO \
estemos siguiendo ya. Máximo 4; sin nada nuevo real → lista vacía."],
  "acciones": ["acción concreta 1", "..."]
}}
Máximo 5 items por lista. Si una keyword que sube NO está en el plan, dilo en acciones.{_lessons_block()}"""


def generate_watch() -> dict:
    prompt = build_watch_prompt()
    if _have_sdk_credentials():
        from anthropic import Anthropic

        client = Anthropic()
        with client.beta.messages.stream(
            model=MODEL, max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError("Fable rechazó la petición del vigía.")
        raw = next(b.text for b in response.content if b.type == "text")
        match = re.search(r"\{.*\}", raw, re.S)
        watch = json.loads(match.group(0))
        engine = "anthropic-sdk"
    else:
        watch = _generate_via_cli(prompt)
        engine = "claude-code-cli"

    # el vigía expande el territorio: sus semillas nuevas entran a la cosecha de mañana
    added = _add_dynamic_seeds(watch.get("semillas_nuevas", []) or [])
    if added:
        learn(f"Vigía: nuevas direcciones de exploración añadidas a las semillas: {', '.join(added)}.")

    watch["_meta"] = {
        "model": MODEL, "engine": engine,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    WATCH_PATH.write_text(json.dumps(watch, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.save("watches", watch)
    return watch


def load_watch() -> dict | None:
    if WATCH_PATH.exists():
        return json.loads(WATCH_PATH.read_text(encoding="utf-8"))
    return mongo.latest("watches")  # espejo en la nube


# ---------------------------------------------------------------------------
# El auditor: Fable lee las campañas del gestor
# ---------------------------------------------------------------------------

CAMPS_PATH = BASE / "campaigns_local.json"


def _load_store() -> dict:
    """Gestor de campañas: archivo local o espejo en Mongo (nube)."""
    if CAMPS_PATH.exists():
        return json.loads(CAMPS_PATH.read_text(encoding="utf-8"))
    doc = mongo.get_doc("manager", {"_id": "store"})
    return (doc or {}).get("store") or {"campaigns": [], "negatives": []}
CAMP_REVIEW_PATH = BASE / "fable_campaigns_review.json"


def _campaign_metrics_by_name() -> dict:
    """Métricas reales por nombre de campaña (del reporte más reciente)."""
    try:
        data = build_data()
        return {c["name"]: c for c in data.get("campaigns", [])}
    except Exception:
        return {}


def _live_deep_data() -> dict:
    """Datos frescos de campañas LIVE: keywords, términos reales y funnel (7 días)."""
    try:
        from datetime import date, timedelta

        from google.ads.googleads.client import GoogleAdsClient

        store = _load_store()
        live_names = {c["name"] for c in store.get("campaigns", []) if c.get("status") == "LIVE"}
        if not live_names:
            return {}
        client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
        ga = client.get_service("GoogleAdsService")
        cid = "4888823590"
        end = _account_today()
        start = end - timedelta(days=7)
        rng = f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"

        kws = []
        q = f"""SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text,
                metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions,
                metrics.conversions_value
                FROM keyword_view WHERE {rng} ORDER BY metrics.cost_micros DESC LIMIT 40"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                if r.campaign.name in live_names and r.metrics.impressions:
                    kws.append(dict(
                        kw=r.ad_group_criterion.keyword.text, grupo=r.ad_group.name,
                        impr=r.metrics.impressions, clics=r.metrics.clicks,
                        coste=round(r.metrics.cost_micros / 1e6, 2),
                        conv=round(r.metrics.conversions, 1),
                        valor=round(r.metrics.conversions_value, 2)))

        grupos = []
        q = f"""SELECT campaign.name, ad_group.name, metrics.impressions, metrics.clicks,
                metrics.cost_micros, metrics.conversions, metrics.conversions_value
                FROM ad_group WHERE {rng}"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                if r.campaign.name in live_names and r.metrics.impressions:
                    cost = r.metrics.cost_micros / 1e6
                    grupos.append(dict(
                        campana=r.campaign.name, grupo=r.ad_group.name,
                        impr=r.metrics.impressions, clics=r.metrics.clicks,
                        ctr=round(r.metrics.clicks / r.metrics.impressions * 100, 1),
                        coste=round(cost, 2), conv=round(r.metrics.conversions, 1),
                        valor=round(r.metrics.conversions_value, 2),
                        roas=round(r.metrics.conversions_value / cost, 2) if cost else None))

        # términos SOLO de ayer y hoy — para juzgar recencia, no arrastrar historia
        rng2 = f"segments.date BETWEEN '{(end - timedelta(days=1)).isoformat()}' AND '{end.isoformat()}'"
        terms = []
        q = f"""SELECT campaign.name, search_term_view.search_term, metrics.impressions,
                metrics.clicks, metrics.cost_micros
                FROM search_term_view WHERE {rng2} ORDER BY metrics.cost_micros DESC LIMIT 30"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                if r.campaign.name in live_names and (r.metrics.clicks or r.metrics.impressions > 5):
                    terms.append(dict(
                        termino=r.search_term_view.search_term, impr=r.metrics.impressions,
                        clics=r.metrics.clicks, coste=round(r.metrics.cost_micros / 1e6, 2)))

        # negativas YA activas por campaña (para no proponer duplicados ni falsas reincidencias)
        negativas_activas = {}
        q = """SELECT campaign.name, campaign_criterion.keyword.text,
               campaign_criterion.keyword.match_type
               FROM campaign_criterion
               WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD'
                 AND campaign.status != 'REMOVED'"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                if r.campaign.name in live_names:
                    negativas_activas.setdefault(r.campaign.name, []).append(
                        f"{r.campaign_criterion.keyword.text} [{r.campaign_criterion.keyword.match_type.name}]")

        funnel = {}
        q = f"""SELECT campaign.name, segments.conversion_action_name, metrics.all_conversions
                FROM campaign WHERE {rng}"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                if r.campaign.name in live_names and r.metrics.all_conversions:
                    funnel[r.segments.conversion_action_name] = (
                        funnel.get(r.segments.conversion_action_name, 0) + r.metrics.all_conversions)

        # aplicadas desde el Mac (archivo) Y desde el espejo en la nube (Mongo)
        acciones_aplicadas, _vistas = [], set()
        alog = BASE / "actions_log.json"
        fuentes = []
        if alog.exists():
            fuentes += [e for e in json.loads(alog.read_text(encoding="utf-8")) if e.get("ok")]
        fuentes += [e for e in mongo.all_docs("actions", limit=200) if e.get("ok")]
        for e in fuentes:
            k = e.get("key") or f"{e.get('ts')}|{e.get('action')}"
            if k in _vistas:
                continue
            _vistas.add(k)
            acciones_aplicadas.append(
                {"ts": e.get("ts"), "accion": e.get("action"), "resultado": e.get("msg")})

        # productos de shopping (7 días): la unidad de decisión del árbol
        productos = {}
        q = f"""SELECT campaign.id, campaign.name, ad_group.name, segments.product_item_id,
                segments.product_title, metrics.clicks, metrics.cost_micros,
                metrics.conversions, metrics.conversions_value
                FROM shopping_performance_view WHERE {rng}"""
        try:
            for b in ga.search_stream(customer_id=cid, query=q):
                for r in b.results:
                    if r.campaign.name not in live_names:
                        continue
                    key = (r.campaign.name, r.ad_group.name,
                           (r.segments.product_item_id or "").lower())
                    d = productos.setdefault(key, dict(
                        campana=key[0], grupo=key[1], item_id=key[2],
                        titulo=r.segments.product_title,
                        clics=0, coste=0.0, conv=0.0, valor=0.0))
                    d["clics"] += r.metrics.clicks
                    d["coste"] += r.metrics.cost_micros / 1e6
                    d["conv"] += r.metrics.conversions
                    d["valor"] += r.metrics.conversions_value
        except Exception:
            pass
        prods = sorted(productos.values(), key=lambda x: -x["coste"])[:40]
        for p in prods:
            p["coste"] = round(p["coste"], 2)
            p["conv"] = round(p["conv"], 1)
            p["valor"] = round(p["valor"], 2)

        # rentabilidad por CANAL: dónde vive mejor cada dólar (la brújula de asignación)
        canal_de = {}
        for c in store.get("campaigns", []):
            if c.get("status") != "LIVE":
                continue
            if (c.get("type") or "").upper() == "SHOPPING":
                canal_de[c["name"]] = "SHOPPING"
            elif (c.get("match_role") or "").upper().startswith("EXACT"):
                canal_de[c["name"]] = "SEARCH-EXACT"
            else:
                canal_de[c["name"]] = "SEARCH-PHRASE"
        por_canal: dict = {}
        for g in grupos:
            canal = canal_de.get(g["campana"])
            if not canal:
                continue
            d = por_canal.setdefault(canal, dict(coste=0.0, valor=0.0, conv=0.0))
            d["coste"] += g["coste"]
            d["valor"] += g["valor"]
            d["conv"] += g["conv"]
        tot = sum(d["coste"] for d in por_canal.values())
        for d in por_canal.values():
            d["coste"] = round(d["coste"], 2)
            d["valor"] = round(d["valor"], 2)
            d["conv"] = round(d["conv"], 1)
            d["roas"] = round(d["valor"] / d["coste"], 2) if d["coste"] else None
            d["cuota_gasto_pct"] = round(d["coste"] / tot * 100) if tot else 0

        # cuota de subastas: cuánta demanda se queda sobre la mesa y por qué
        cuota = []
        q = f"""SELECT campaign.name, metrics.search_impression_share,
                metrics.search_budget_lost_impression_share,
                metrics.search_rank_lost_impression_share
                FROM campaign WHERE campaign.status = 'ENABLED' AND {rng}"""
        try:
            for b in ga.search_stream(customer_id=cid, query=q):
                for r in b.results:
                    if r.campaign.name not in live_names:
                        continue
                    m = r.metrics
                    cuota.append(dict(
                        campana=r.campaign.name,
                        captura_pct=round(m.search_impression_share * 100),
                        perdido_por_presupuesto_pct=round(m.search_budget_lost_impression_share * 100),
                        perdido_por_puja_pct=round(m.search_rank_lost_impression_share * 100)))
        except Exception:
            pass

        return {"keywords_7d": kws, "grupos_7d": grupos,
                "terminos_busqueda_ayer_y_hoy": terms, "funnel_7d": funnel,
                "productos_shopping_7d": prods,
                "rentabilidad_por_canal_7d": por_canal,
                "cuota_de_subastas_7d": cuota,
                "negativas_activas_por_campana": negativas_activas,
                "acciones_ya_aplicadas": acciones_aplicadas}
    except Exception:
        return {}


def build_campaign_review_prompt() -> str:
    store = _load_store()
    if not store.get("campaigns"):
        raise RuntimeError("no hay campañas en el gestor")
    metrics = _campaign_metrics_by_name()
    deep = _live_deep_data()

    resumen_campanas = []
    for c in store.get("campaigns", []):
        dias_activa = None
        if c.get("pushed_at"):
            try:
                pushed = datetime.strptime(c["pushed_at"], "%Y-%m-%d %H:%M")
                dias_activa = max(0, (datetime.now() - pushed).days)
            except ValueError:
                pass
        resumen_campanas.append({
            "name": c["name"], "status": c.get("status"), "enabled": c.get("enabled"),
            "dias_activa": dias_activa,
            "en_aprendizaje": dias_activa is not None and dias_activa < 7,
            "estrategia_puja": "Maximizar clics con tope de CPC $2",
            "match_role": c.get("match_role"), "daily_budget_usd": c.get("daily_budget_usd"),
            "cross_negatives_count": len(c.get("cross_negatives", [])),
            "ad_groups": [
                {
                    "name": g["name"],
                    "keywords": [f"{k['text']} [{k['match']}]" for k in g.get("keywords", [])],
                    "n_headlines": len(g.get("headlines", [])),
                    "headlines_over_30": [h for h in g.get("headlines", []) if len(h) > 30],
                    "n_descriptions": len(g.get("descriptions", [])),
                    "descriptions_over_90": [d for d in g.get("descriptions", []) if len(d) > 90],
                }
                for g in c.get("ad_groups", [])
            ],
            "metricas_reales": metrics.get(c["name"]),
        })

    return f"""Eres el auditor de campañas de Google Ads de Jersey Pickles. Lee las campañas del \
gestor y da tu diagnóstico, corto y accionable, en español.

CAMPAÑAS EN EL GESTOR (status DRAFT = borrador aún no subido a Google Ads; \
metricas_reales = null si aún no ha corrido): {json.dumps(resumen_campanas, ensure_ascii=False)}

DATOS PROFUNDOS DE LAS CAMPAÑAS ACTIVAS (últimos 7 días — keywords con gasto, términos de \
búsqueda REALES de la gente, y funnel de AdNabu): {json.dumps(deep, ensure_ascii=False)}

QUÉ AUDITAR:
- Si son borradores: coherencia keyword↔grupo↔anuncio, presupuestos vs CPCs, titulares/descripciones \
fuera de límite (30/90), duplicados o titulares muy parecidos, flujo de negativas cruzadas correcto \
(la PHRASE debe bloquear las EXACT), y cualquier hueco antes de lanzar.
- Si hay metricas_reales/datos profundos: CPA vs referencia (histórico $2.77, tope aceptable ~$10 \
con ticket $66), CTR, gasto sin conversiones, keywords caras, términos DIY/irrelevantes que se \
cuelan (candidatos a negativa), reparto del gasto entre grupos, señales de funnel (page views → \
add-to-cart → checkout → compra), y qué escalar o pausar.
- ROAS (valor/coste) A PRECISIÓN por campaña, por grupo (grupos_7d) y por keyword (valor en \
keywords_7d): ROAS <1 = pérdida directa; break-even estimado ~1; objetivo inicial ≥3; el histórico \
PMax llegó a ~23x. Señala con nombre y números los grupos/keywords que generan valor y los que \
solo queman — pero recuerda la ventana de 5-7 días antes de sentenciar.

Responde SOLO con JSON válido (sin markdown):
{{
  "resumen": "2-3 frases con la foto general",
  "por_campana": [{{"name": "nombre exacto", "salud": "ok|atencion|critico", "notas": ["nota corta", "..."]}}],
  "recomendaciones": ["consejo general 1", "..."],
  "acciones_propuestas": [{{"tipo": "pausar_keyword|reactivar_keyword|añadir_negativa|ajustar_presupuesto|ajustar_tope_cpc|crear_keyword|ajustar_puja_grupo|excluir_producto_shopping|mover_producto_grupo",
    "campana": "nombre EXACTO de la campaña", "objetivo": "keyword, término, nombre de grupo o item_id según el tipo",
    "valor": 0, "grupo": "solo para crear_keyword: nombre EXACTO del grupo de anuncios",
    "razon": "con los números que lo justifican", "urgencia": "alta|media|baja"}}],
  "lecciones_nuevas": ["APRENDIZAJE DURADERO que valga guardar en tu memoria permanente — patrones, \
errores a no repetir, verdades del negocio descubiertas en los datos. NO coyunturas del día. \
Máximo 3, y solo si de verdad aportan; si no hay nada nuevo que aprender, lista vacía."],
  "expansion": {{"search": {{"conviene": false, "razon": "con los números", "confianza": "alta|media|baja"}},
               "shopping": {{"conviene": false, "razon": "con los números", "confianza": "alta|media|baja"}}}}
}}

EXPANSIÓN — ERES TÚ QUIEN DECIDE CUÁNDO CRECER (no hay calendario; esta evaluación corre en \
cada revisión):
- search: ¿hay demanda validada del vigía sin cubrir (volumen + momentum), clusters ganadores \
en los términos reales que el plan actual no captura, o estacionalidad entrante que exige \
campaña propia?
- shopping: ¿hay productos con ≥2 conversiones probadas que merecen campaña propia con puja \
mayor, presupuesto agotándose con demanda sobrante, o una parte del catálogo que la campaña \
actual no explota?
Reglas de honestidad: la respuesta más común es NO (conviene=false) — crecer sin caso quema \
presupuesto y fragmenta la señal. Con las campañas base en aprendizaje, solo señales EXTERNAS \
(estacionalidad, vigía) justifican expandir, nunca su rendimiento de pocos días. confianza=alta \
SOLO si apostarías tu propio presupuesto. Si declaras conviene=true con confianza alta, el \
sistema te pedirá el plan completo automáticamente y llegará al dueño como ✨propuesta.

CUOTA DE SUBASTAS (cuota_de_subastas_7d) — el medidor de cuánto mercado queda sobre la mesa: \
perdido_por_presupuesto alto + ROAS sano = LA señal de subir presupuesto (día 7+) o de proponer \
expansión; perdido_por_puja alto = pujas cortas, no presupuesto. Un canal con ROAS malo NO \
merece recuperar su cuota perdida — primero se arregla, después se escala.

RENTABILIDAD POR CANAL (rentabilidad_por_canal_7d) — tu brújula de asignación: cada dólar debe \
vivir en el canal que más devuelve. Desde el día 7: si un canal sostiene ROAS claramente \
superior (≥2x el de otro) con ≥$100 de gasto acumulado en la ventana, propone rebalanceo \
GRADUAL de presupuesto hacia el ganador (ajustar_presupuesto en pasos de ±20%, nunca de golpe). \
La expansión hereda el mismo sesgo: campañas nuevas del canal ganador tienen prioridad. Un \
canal perdedor no se mata: se recorta a su núcleo rentable y se corrige la fuga antes de \
moverle plata — matar EXACT entera, por ejemplo, regalaría los términos de control a PHRASE \
sin su precisión de puja.

CONCIENCIA DEL PERÍODO DE APRENDIZAJE (en_aprendizaje=true → campaña con <7 días en Google Ads):
- Durante el aprendizaje, el algoritmo de puja está calibrando. Cambios grandes lo RESETEAN y \
retrasan resultados: NO propongas ajustar_presupuesto (>±20%), NO ajustar_tope_cpc, y evita \
pausar_keyword salvo sangría clara (>$25 gastados, 0 conversiones y CERO señales de funnel).
- Lo que SÍ es seguro (y sano) durante el aprendizaje: añadir negativas de términos irrelevantes \
— no resetean nada y mejoran la calidad del tráfico con el que aprende.
- Menciona en las notas de cada campaña si está en aprendizaje y qué día va.

REGLA CERO v2 — POLÍTICA DEL DUEÑO (actualizada 2026-08-04): el filtro es la EVIDENCIA, no el \
calendario. Con horas de datos no se sugiere nada; pero una sangría SOSTENIDA y verificable no \
se deja correr "porque está en aprendizaje" — esperar no repara un ROAS que lleva 3 días roto. \
Para campañas con en_aprendizaje=true SOLO puedes proponer estas cirugías con evidencia dura:
- añadir_negativa: términos irrelevantes/DIY con gasto real (siempre segura, no resetea nada).
- pausar_keyword: SOLO con ≥$25 gastados en la ventana, 0 conversiones y cero señales de \
funnel — sangría sostenida de días, no ruido de horas. El sistema verifica los números por \
código antes de ejecutar.
- excluir_producto_shopping: por stock agotado (higiene).
PROHIBIDO en aprendizaje, sin excepción: ajustar_presupuesto, ajustar_tope_cpc, \
ajustar_puja_grupo, crear_keyword, mover_producto_grupo — esos SÍ resetean el aprendizaje del \
algoritmo y esperan al día 7. Todo lo demás que observes, anótalo para el paquete del día 7.

REGLAS PARA acciones_propuestas (solo con EVIDENCIA en los datos; sin evidencia → lista vacía):
- pausar_keyword: gastó >$15 con 0 conversiones Y (CTR<1.5% o cero señales de funnel). \
En aprendizaje el umbral sube a >$25 y cero funnel.
- añadir_negativa: término de búsqueda irrelevante/DIY con gasto o >10 impresiones. NUNCA una \
negativa que contenga una keyword activa propia. NUNCA propongas una negativa que ya figure en \
negativas_activas_por_campana (compara texto y campaña).
- REGLA DE NO-REINCIDENCIA: terminos_busqueda_ayer_y_hoy cubre SOLO ayer y hoy; \
acciones_ya_aplicadas lleva fecha. El gasto ANTERIOR a la aplicación de una negativa NO es \
reincidencia — solo es reincidencia si el término gasta DESPUÉS del ts de la acción aplicada. \
No acuses de "sin aplicar" lo que consta en acciones_ya_aplicadas o en negativas activas.
- ajustar_presupuesto (valor=USD/día): PROHIBIDO en aprendizaje. Después: subir SOLO con ROAS \
sano y cuota perdida por presupuesto; bajar si gasto completo sin retorno ≥3 días. TAMAÑO DEL \
PASO según estrategia de puja (el ejecutor lo bloquea si te pasas): campañas MANUAL_CPC \
(shopping) hasta +100% por paso — no hay algoritmo que resetear; Maximizar clics ±30%; cuando \
exista tROAS, ±20% máximo y NUNCA junto a un cambio de objetivo. Entre pasos, 3-4 días de lectura.
- ajustar_tope_cpc (valor=USD): PROHIBIDO en aprendizaje. Después: bajar si el CPC medio lleva \
≥3 días pegado al ≥90% del tope sin compras.
- crear_keyword (objetivo=texto, valor=EXACT|PHRASE, grupo=nombre EXACTO del grupo): solo con \
demanda demostrada en términos reales (≥3 clics o CTR>5% con gasto) Y producto en catálogo que \
la respalde. La keyword nueva hereda el flujo: si es EXACT, recuerda proponer su negativa EXACT \
en la campaña PHRASE.
- ajustar_puja_grupo (SHOPPING; objetivo=nombre del grupo, valor=USD 0.20-2.00): PROHIBIDO en \
aprendizaje. Después: subir si 2+ días con impresiones ~0 o CPC medio ≥85% del tope con buen \
funnel; bajar si el grupo quema sin conversión con ≥$25 de evidencia.
- excluir_producto_shopping (SHOPPING; objetivo=item_id EXACTO del feed, ej. shopify_ZZ_..._...): \
producto con ≥$10 de gasto y 0 conversiones sin señal de funnel, o agotado sin fecha de \
reposición. La higiene por stock NO cuenta como optimización: permitida incluso en aprendizaje.
- mover_producto_grupo (SHOPPING; objetivo=item_id, valor=nombre EXACTO del grupo destino): \
PROHIBIDO en aprendizaje. Después: producto del grupo escoba con ≥2 conversiones merece subir a \
un grupo con puja mayor; producto de Estrellas con ≥$15 y 0 conv baja al escoba (no se excluye \
si aún tiene señales de funnel).
- Con 1-2 días de datos sé conservador: urgencia baja o nada. Máximo 5 acciones.
Máximo 4 notas por campaña y 5 recomendaciones.{_lessons_block()}"""


def _learning_campaigns() -> dict:
    """Campañas en aprendizaje (<7 días desde su subida) → días que llevan."""
    out = {}
    try:
        store = _load_store()
        for c in store.get("campaigns", []):
            if c.get("status") == "LIVE" and c.get("pushed_at"):
                pushed = datetime.strptime(c["pushed_at"], "%Y-%m-%d %H:%M")
                days = (datetime.now() - pushed).days
                if days < 7:
                    out[c["name"]] = days + 1
    except Exception:
        pass
    return out


def enforce_learning_gate(review: dict) -> dict:
    """Compuerta DURA: elimina cualquier acción sobre campañas en aprendizaje.

    Política del dueño: cero sugerencias sin 7 días de data — aunque el modelo
    las haya generado, aquí se retienen (no dependemos del prompt).
    """
    learning = _learning_campaigns()
    if not learning:
        return review
    acts = review.get("acciones_propuestas") or []
    # REGLA CERO v2 (2026-08-04): en aprendizaje pasan SOLO las cirugías con
    # evidencia dura — negativas, pausas por sangría (el piloto verifica ≥$25 y
    # 0 conv por código antes de ejecutar) e higiene de stock. Presupuestos,
    # pujas y estructura siguen bloqueados hasta el día 7.
    PERMITIDAS_EN_APRENDIZAJE = {"añadir_negativa", "pausar_keyword", "excluir_producto_shopping"}
    kept = [a for a in acts
            if a.get("campana") not in learning
            or a.get("tipo") in PERMITIDAS_EN_APRENDIZAJE]
    retained = len(acts) - len(kept)
    review["acciones_propuestas"] = kept
    review["acciones_retenidas_learning"] = retained
    try:
        store = _load_store()
        fin = max(
            datetime.strptime(c["pushed_at"], "%Y-%m-%d %H:%M") + timedelta(days=7)
            for c in store.get("campaigns", [])
            if c.get("status") == "LIVE" and c.get("pushed_at")
        )
        review["learning_until"] = fin.strftime("%Y-%m-%d")
    except Exception:
        pass
    return review


def generate_campaign_review() -> dict:
    prompt = build_campaign_review_prompt()
    if _have_sdk_credentials():
        from anthropic import Anthropic

        client = Anthropic()
        with client.beta.messages.stream(
            model=MODEL, max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError("Fable rechazó la auditoría.")
        raw = next(b.text for b in response.content if b.type == "text")
        review = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        engine = "anthropic-sdk"
    else:
        review = _generate_via_cli(prompt)
        engine = "claude-code-cli"

    review = enforce_learning_gate(review)

    # Fable guarda solo lo que aprendió de los datos
    for lesson in review.get("lecciones_nuevas", []) or []:
        if isinstance(lesson, str) and len(lesson) > 20:
            learn(lesson)

    review["_meta"] = {
        "model": MODEL, "engine": engine,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    CAMP_REVIEW_PATH.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.save("reviews", review)
    return review


def load_campaign_review() -> dict | None:
    if CAMP_REVIEW_PATH.exists():
        return json.loads(CAMP_REVIEW_PATH.read_text(encoding="utf-8"))
    return mongo.latest("reviews")  # espejo en la nube


# ---------------------------------------------------------------------------
# Shopping: primera campaña de Merchant — Fable decide estructura y escalado
# ---------------------------------------------------------------------------

SHOPPING_PLAN_PATH = BASE / "fable_shopping_plan.json"
MERCHANT_ID = 5080357407


def _merchant_intel() -> dict:
    """Lo que Fable necesita del Merchant: ofertas, rendimiento por producto, stock.

    El universo del feed se construye desde Shopify (productos ACTIVOS): AdNabu
    genera los item_id como shopify_ZZ_{producto}_{variante}. La tabla
    shopping_product de la Ads API solo lista productos con issues, no el feed.
    """
    from datetime import date, timedelta

    from google.ads.googleads.client import GoogleAdsClient

    from store_catalog import shopify_catalog

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    ga = client.get_service("GoogleAdsService")
    cid = "4888823590"

    # rendimiento histórico por (producto, variante), dos ventanas
    perf: dict = {}
    for days, key in ((365, "365d"), (90, "90d")):
        end = _account_today()
        start = end - timedelta(days=days)
        q = f"""SELECT segments.product_item_id, metrics.clicks, metrics.cost_micros,
                metrics.conversions, metrics.conversions_value
                FROM shopping_performance_view
                WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"""
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                m = re.search(r"shopify_[a-z]{2}_(\d+)_(\d+)",
                              (r.segments.product_item_id or "").lower())
                if not m:
                    continue
                pv = (int(m.group(1)), int(m.group(2)))
                d = perf.setdefault(pv, {}).setdefault(
                    key, dict(clicks=0, cost=0.0, conv=0.0, value=0.0))
                d["clicks"] += r.metrics.clicks
                d["cost"] += r.metrics.cost_micros / 1e6
                d["conv"] += r.metrics.conversions
                d["value"] += r.metrics.conversions_value

    # issues reportados por Google (lo único que shopping_product sí da)
    issues_feed: dict = {}
    q = f"""SELECT shopping_product.title, shopping_product.channel, shopping_product.issues
            FROM shopping_product
            WHERE shopping_product.merchant_center_id = {MERCHANT_ID} LIMIT 200"""
    try:
        for b in ga.search_stream(customer_id=cid, query=q):
            for r in b.results:
                sp = r.shopping_product
                if sp.channel.name == "LOCAL":
                    continue
                issues_feed[sp.title] = [getattr(i, "description", "") for i in sp.issues][:2]
    except Exception:
        pass

    # universo: catálogo activo de Shopify, item_ids como los genera AdNabu
    cat = shopify_catalog()
    valid_ids: set = set()
    vids_por_pid: dict = {}
    for vid, vinfo in cat["by_variant"].items():
        vids_por_pid.setdefault(vinfo.get("pid"), []).append(vid)

    lines = []
    for pid, p in cat["by_product"].items():
        if p.get("status") and p["status"] != "active":
            continue
        item_ids, var_lines = [], []
        qtys = []
        for vid in vids_por_pid.get(pid, []):
            v = cat["by_variant"][vid]
            iid = f"shopify_ZZ_{pid}_{vid}"
            item_ids.append(iid)
            valid_ids.add(iid.lower())
            estado = "SIN STOCK" if (v["qty"] is not None and v["qty"] <= 0) else "ok"
            var_lines.append(f"{v['t'] or 'única'}: {estado}")
            if v["qty"] is not None:
                qtys.append(v["qty"])
        if not item_ids:
            continue
        agg = dict(cost_365=0.0, value_365=0.0, conv_365=0.0,
                   cost_90=0.0, value_90=0.0, conv_90=0.0)
        for vid in vids_por_pid.get(pid, []):
            for key, suf in (("365d", "365"), ("90d", "90")):
                d = perf.get((pid, vid), {}).get(key)
                if d:
                    agg[f"cost_{suf}"] += d["cost"]
                    agg[f"value_{suf}"] += d["value"]
                    agg[f"conv_{suf}"] += d["conv"]
        entry = dict(
            titulo=p["title"], item_ids=item_ids, variantes_stock=var_lines,
            hist_365d=dict(cost=round(agg["cost_365"], 2), conv=round(agg["conv_365"], 1),
                           roas=round(agg["value_365"] / agg["cost_365"], 1) if agg["cost_365"] else None),
            hist_90d=dict(cost=round(agg["cost_90"], 2), conv=round(agg["conv_90"], 1),
                          roas=round(agg["value_90"] / agg["cost_90"], 1) if agg["cost_90"] else None),
        )
        if qtys and sum(qtys) <= 0:
            entry["sin_stock_total"] = True
        if p["title"] in issues_feed:
            entry["issues_feed"] = issues_feed[p["title"]]
        lines.append(entry)
    lines.sort(key=lambda x: -(x["hist_365d"]["cost"] or 0))

    return dict(productos=lines,
                sin_stock_tienda=[s["title"] for s in cat["sin_stock"]],
                valid_item_ids=valid_ids,
                total_ofertas=len(valid_ids))


def build_shopping_prompt(intel: dict) -> str:
    store = _load_store()
    negativas = store.get("negatives", [])
    shopping_vivas = [c for c in store.get("campaigns", [])
                      if (c.get("type") or "").upper() == "SHOPPING"]
    if shopping_vivas:
        vivas = "; ".join(f"{c['name']} (${c.get('daily_budget_usd')}/día, {c.get('status')})"
                          for c in shopping_vivas)
        mision = f"""Ya tienes Shopping corriendo: {vivas}. Tu misión AHORA es la SIGUIENTE \
JUGADA del catálogo con los datos reales acumulados — NO repetir la primera campaña:
- La campaña existente se optimiza vía acciones en tus revisiones (pujas, mover/excluir \
productos); NO la regeneres aquí.
- Este plan debe ser una campaña NUEVA con nombre DISTINTO y un rol complementario claro. \
Ejemplos del tipo de jugada: "Ganadores" solo con productos de ≥2 conversiones probadas a puja \
agresiva (o tROAS si la campaña nueva tendrá señal suficiente); una estacional; o un \
experimento PMax SOLO si la cuenta supera 30 conversiones/30d.
- Sin solaparse: los productos que muevas a la campaña nueva deben quedar excluidos de la \
vieja — decláralo en next_steps como acciones concretas (excluir_producto_shopping).
- Si los datos aún NO justifican una segunda campaña, dilo con números en strategy_summary y \
devuelve product_groups vacío — proponer por proponer quema presupuesto."""
    else:
        mision = """Vas a diseñar la PRIMERA campaña de Shopping (estándar, NO Performance Max) \
de esta cuenta. Empiezas desde el principio: no hay historia de Shopping estándar — solo el \
histórico de una PMax vieja (ya eliminada) que dejó datos por producto."""
    return f"""Eres Fable, estratega senior de Google Ads de Jersey Pickles (jerseypickles.com, \
EE.UU., pickles artesanales refrigerados con envío nacional). {mision}

TU TRABAJO — construyes para escalar, no para lanzar por lanzar:
1. ESTRUCTURA: decide TÚ cuántos grupos de productos crear y cómo dividirlos (¿todo el catálogo \
en un grupo con puja uniforme? ¿estrellas separadas con puja mayor? ¿grupo de descubrimiento?). \
Justifica con los datos. En la primera campaña, la simplicidad que permita LEER la señal vale \
más que la sofisticación — pero la decisión es tuya.
2. PUJAS: CPC manual por grupo ($0.30–$1.50). El ROAS histórico por producto te dice cuánto \
vale un clic en cada grupo.
3. PRESUPUESTO: calcula uno inteligente ($10–$50/día) coherente con pujas y catálogo; explica el cálculo.
4. EXCLUSIONES: excluye productos marcados sin_stock_total, con issues_feed graves, o con \
histórico claramente malo (gasto alto, cero conversión). Cada exclusión con su razón. Para \
excluir un producto entero incluye TODOS sus item_ids en excluded_products.
5. PLAN DE ESCALADO (lo más importante): fases con CRITERIOS MEDIBLES desde el día 0. Del tipo: \
cuándo dividir un grupo en varios, cuándo subir presupuesto, cuándo pasar a tROAS (necesita ≥15 \
conversiones/30d en la campaña), cuándo NO tocar nada. Es TU hoja de ruta — la seguirás tú mismo \
en las revisiones diarias.
6. NEGATIVAS: en Shopping no hay keywords positivas; el control es por negativas. Propón las \
iniciales usando las ya probadas de Search como referencia.

REGLAS DURAS:
- SOLO item_ids EXACTOS de la lista PRODUCTOS de abajo. Un id inventado rompe el árbol de productos.
- Máximo un grupo con all_products=true (recoge todo lo no excluido); los demás llevan item_ids.
- Un producto en un grupo con item_ids NO puede estar también en otro grupo.
- La campaña tendrá 7 días de aprendizaje: el scaling_plan NO propone cambios antes del día 7 \
(la exclusión por quedarse sin stock es higiene, no optimización — esa sí, siempre).
- ROAS: <1 pérdida; objetivo ≥3; el histórico PMax es techo optimista (auto-atribución de Google) \
— úsalo para ranking RELATIVO entre productos, no como promesa.

PRODUCTOS DEL FEED HOY ({intel['total_ofertas']} ofertas; con item_ids, stock por variante, \
issues del feed si los hay, histórico 365d y 90d — ordenados por gasto histórico):
{json.dumps(intel['productos'], ensure_ascii=False)}

PRODUCTOS SIN STOCK EN LA TIENDA AHORA MISMO:
{json.dumps(intel['sin_stock_tienda'], ensure_ascii=False)}

NEGATIVAS YA PROBADAS EN SEARCH (referencia):
{json.dumps(negativas, ensure_ascii=False)}
{_manager_state()}{_lessons_block()}
Responde SOLO con JSON válido (sin markdown), textos en español:
{{"strategy_summary": "tu tesis de estructura y por qué (3-5 frases)",
 "campaign": {{"name": "JP · Shopping · ...", "objective": "...", "daily_budget_usd": 0,
   "budget_rationale": "...",
   "product_groups": [{{"name": "...", "rationale": "...", "cpc_bid_usd": 0.0,
     "all_products": false, "item_ids": ["..."]}}],
   "excluded_products": [{{"item_id": "...", "title": "...", "reason": "..."}}]}},
 "negatives": ["..."],
 "scaling_plan": [{{"fase": "Fase 1 — ...", "cuando": "criterio medible", "accion": "qué harás"}}],
 "next_steps": ["..."]}}"""


def _shopping_code_checks(plan: dict, valid_ids: set) -> list[str]:
    """Reglas que se verifican con código, no con confianza en el modelo."""
    probs = []
    camp = plan.get("campaign") or {}
    groups = camp.get("product_groups") or []
    # cero grupos es legítimo en regeneraciones ("aún no toca otra campaña");
    # el validador de aceptación/subida sí exige grupos para ejecutar
    if sum(1 for g in groups if g.get("all_products")) > 1:
        probs.append("más de un grupo all_products=true")
    seen: dict = {}
    for g in groups:
        bid = g.get("cpc_bid_usd") or 0
        if not 0.20 <= bid <= 2.00:
            probs.append(f"puja fuera de rango en '{g.get('name')}': ${bid}")
        for iid in g.get("item_ids") or []:
            low = iid.lower()
            if low not in valid_ids:
                probs.append(f"item_id inexistente: {iid} (grupo '{g.get('name')}')")
            if low in seen:
                probs.append(f"item_id repetido en '{seen[low]}' y '{g.get('name')}': {iid}")
            seen[low] = g.get("name")
    for e in camp.get("excluded_products") or []:
        if (e.get("item_id") or "").lower() not in valid_ids:
            probs.append(f"exclusión con item_id inexistente: {e.get('item_id')}")
    b = camp.get("daily_budget_usd") or 0
    if not 5 <= b <= 60:
        probs.append(f"presupuesto fuera de rango: ${b}/día")
    return probs


SHOPPING_AUDIT_RULES = """\
- Estructura: ¿los grupos permiten leer señal limpia? ¿las pujas reflejan el ROAS histórico \
relativo de cada grupo?
- Presupuesto coherente con pujas × alcance del catálogo.
- Exclusiones: productos totalmente sin stock (sin_stock_total) deben estar excluidos; ninguna \
exclusión sin razón de datos.
- scaling_plan: cada fase con criterio MEDIBLE (números, días, conversiones); nada de acciones \
de optimización antes del día 7; el paso a tROAS exige ≥15 conv/30d.
- Negativas: informacionales (recetas, DIY, how to) y marcas de retail ajenas presentes; \
ninguna que bloquee búsquedas genéricas de compra ('pickles' solo, 'buy pickles')."""


def _audit_shopping(plan: dict, code_problems: list[str]) -> dict:
    plan_sin_meta = {k: v for k, v in plan.items() if k != "_meta"}
    extra = ""
    if code_problems:
        extra = ("\n\nPROBLEMAS YA DETECTADOS POR CÓDIGO (son hechos, no opiniones — la campaña "
                 f"tiene salud 'critico' mientras existan):\n" +
                 "\n".join(f"- {p}" for p in code_problems))
    prompt = f"""Eres el auditor de Google Ads de Jersey Pickles. Audita este plan BORRADOR de la \
primera campaña de Shopping con ojo crítico y despiadado.

PLAN: {json.dumps(plan_sin_meta, ensure_ascii=False)}

QUÉ AUDITAR:
{SHOPPING_AUDIT_RULES}{extra}

Responde SOLO con JSON válido (sin markdown), en español:
{{"resumen": "...", "salud": "ok|atencion|critico", "notas": ["..."], "recomendaciones": ["..."]}}
Si el plan está bien, di "ok" — no inventes problemas.{_lessons_block()}"""
    return _call_json(prompt, max_tokens=8000)


def _refine_shopping(plan: dict, review: dict, code_problems: list[str]) -> dict:
    plan_sin_meta = {k: v for k, v in plan.items() if k != "_meta"}
    review_sin_meta = {k: v for k, v in review.items() if k != "_meta"}
    prompt = f"""Eres el estratega de Google Ads de Jersey Pickles. Este es TU plan de la primera \
campaña Shopping y la auditoría interna que encontró problemas. CORRIGE TODO lo señalado sin \
romper lo que está bien.

REGLAS AL CORREGIR:
- Mantén el NOMBRE de la campaña exactamente igual.
- SOLO item_ids que ya aparecían en el plan o en las exclusiones — no inventes ids nuevos.
- Devuelve el plan COMPLETO corregido, mismo formato JSON exacto (strategy_summary, campaign \
{{name, objective, daily_budget_usd, budget_rationale, product_groups[name, rationale, \
cpc_bid_usd, all_products, item_ids], excluded_products[item_id, title, reason]}}, negatives, \
scaling_plan[fase, cuando, accion], next_steps). SOLO el JSON, sin markdown.

PLAN ACTUAL: {json.dumps(plan_sin_meta, ensure_ascii=False)}

AUDITORÍA: {json.dumps(review_sin_meta, ensure_ascii=False)}

PROBLEMAS DETECTADOS POR CÓDIGO (obligatorio corregirlos todos):
{json.dumps(code_problems, ensure_ascii=False)}{_lessons_block()}"""
    return _call_json(prompt)


def generate_shopping_plan() -> dict:
    """Primera campaña Shopping: Fable decide estructura, pujas y hoja de escalado."""
    intel = _merchant_intel()
    if not intel["productos"]:
        raise RuntimeError("el feed de Merchant no devolvió productos")
    prompt = build_shopping_prompt(intel)
    if _have_sdk_credentials():
        plan, engine = _generate_via_sdk(prompt), "anthropic-sdk"
    else:
        plan, engine = _generate_via_cli(prompt), "claude-code-cli"

    rounds, final_health = 0, "ok"
    for _ in range(2):
        code_probs = _shopping_code_checks(plan, intel["valid_item_ids"])
        try:
            review = _audit_shopping(plan, code_probs)
        except Exception as exc:
            print(f"[fable-shopping] auditoría falló ({exc}) — el plan sigue", flush=True)
            final_health = "auditoría no disponible"
            break
        if not code_probs and review.get("salud", "ok") == "ok":
            final_health = "ok"
            break
        for nota in (code_probs + review.get("notas", []))[:3]:
            learn(f"El auditor marcó en el plan de Shopping: {nota}")
        try:
            plan = _refine_shopping(plan, review, code_probs)
            final_health = "corregido"
        except Exception as exc:
            # la corrección falló: mejor el plan original saneado que nada
            print(f"[fable-shopping] refinado falló ({exc}) — se conserva el plan previo", flush=True)
            final_health = "corrección falló; plan previo saneado por código"
            break
        rounds += 1

    # cinturón final: si tras las rondas quedan ids inválidos, se limpian con código
    remaining = _shopping_code_checks(plan, intel["valid_item_ids"])
    if any("inexistente" in p or "repetido" in p for p in remaining):
        seen: set = set()
        for g in plan.get("campaign", {}).get("product_groups", []):
            clean = []
            for iid in g.get("item_ids") or []:
                low = iid.lower()
                if low in intel["valid_item_ids"] and low not in seen:
                    clean.append(iid)
                    seen.add(low)
            g["item_ids"] = clean
        final_health = "corregido+saneado"

    plan["_meta"] = {
        "model": MODEL, "engine": engine, "type": "shopping",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "self_review_rounds": rounds,
        "self_review_result": final_health,
        "total_ofertas_feed": intel["total_ofertas"],
    }
    SHOPPING_PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    mongo.save("shopping_plans", plan)
    return plan


def load_shopping_plan() -> dict | None:
    if SHOPPING_PLAN_PATH.exists():
        return json.loads(SHOPPING_PLAN_PATH.read_text(encoding="utf-8"))
    return mongo.latest("shopping_plans")  # espejo en la nube


if __name__ == "__main__":
    print(json.dumps(generate_plan(), ensure_ascii=False, indent=1)[:2000])

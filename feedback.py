"""Bucle de retroalimentación: ¿sirvió de algo lo que el sistema hizo?

El gestor llevaba 120 acciones ejecutadas y CERO comprobaciones de si alguna
funcionó. Guardaba qué hizo y cuándo, no si sirvió — eso es un diario, no
aprendizaje. Todo lo que "sabía" se lo había enseñado un humano tras diagnosticar
a mano; nunca había aprendido nada de su propia experiencia.

Aquí se mide cada acción contra la ventana de 7 días ANTERIOR y los 7 días
POSTERIORES, y el veredicto entra en la revisión para que Fable deje de proponer
familias de acción que no pagan.

HONESTIDAD DE LA MEDICIÓN — esto no es un experimento controlado:
  - No hay grupo de control. Coinciden estacionalidad, otras acciones y cambios
    de Google en la misma ventana.
  - Por eso cada veredicto lleva `confianza`. Sólo se afirma lo que se puede
    defender con el dato, y lo dudoso se marca como tal en vez de disfrazarlo.
  - Una acción sin 7 días completos por detrás NO se evalúa: se deja para después.

Qué se mide de cada tipo (los 4 cubren 111 de las 120 acciones del log):
  añadir_negativa       lo que ese término gastaba y CONVERTÍA antes de bloquearlo.
                        Si convertía, la negativa fue un error aunque "ahorrara".
  pausar_keyword        igual: gasto detenido vs conversiones que sí traía.
  optimizar_titulo_feed impresiones/clics/CTR/conversiones de ESE producto, antes vs después.
  ajustar_presupuesto   coste, conversiones y ROAS de la campaña, antes vs después.

Uso:
    .venv/bin/python feedback.py            # evalúa y muestra el balance
    .venv/bin/python feedback.py --resumen  # sólo el resumen por tipo
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import db as mongo

BASE = Path(__file__).parent
CUSTOMER_ID = "4888823590"
VENTANA = 7            # días a cada lado
MIN_GASTO = 1.0        # por debajo de esto no hay señal que juzgar


def _ga():
    from google.ads.googleads.client import GoogleAdsClient

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    return client.get_service("GoogleAdsService")


def _hoy():
    try:
        import app
        return app._account_today()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()


def _rango(desde, hasta):
    return f"segments.date BETWEEN '{desde.isoformat()}' AND '{hasta.isoformat()}'"


def _metricas(ga, where: str) -> dict:
    """Suma coste/clics/impresiones/conversiones/valor de una consulta cualquiera."""
    q = f"""SELECT metrics.cost_micros, metrics.clicks, metrics.impressions,
                   metrics.conversions, metrics.conversions_value {where}"""
    t = dict(coste=0.0, clics=0, impr=0, conv=0.0, valor=0.0)
    for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for r in b.results:
            m = r.metrics
            t["coste"] += m.cost_micros / 1e6
            t["clics"] += m.clicks
            t["impr"] += m.impressions
            t["conv"] += m.conversions
            t["valor"] += m.conversions_value
    t["roas"] = round(t["valor"] / t["coste"], 2) if t["coste"] else 0.0
    t["ctr"] = round(t["clics"] / t["impr"] * 100, 2) if t["impr"] else 0.0
    for k in ("coste", "conv", "valor"):
        t[k] = round(t[k], 2)
    return t


def _esc(s: str) -> str:
    return (s or "").replace("'", "\\'")


# ── un evaluador por tipo ────────────────────────────────────────────────────

def _eval_bloqueo(ga, acc, ini, fin_antes, _ini_desp, _fin):
    """Negativas y pausas: lo que importa es qué estábamos comprando ANTES.

    Tras bloquear, el término desaparece del informe — que el gasto posterior sea
    cero no demuestra nada. La pregunta real es si lo que se cortó convertía.
    """
    campana = _esc(acc.get("campana", ""))
    obj = (acc.get("objetivo") or "").lower()
    if not obj or not campana:
        return None
    if acc["tipo"] == "añadir_negativa":
        # términos de búsqueda que contenían la palabra bloqueada
        where = (f"""FROM search_term_view WHERE {_rango(ini, fin_antes)}
                     AND campaign.name = '{campana}'
                     AND search_term_view.search_term LIKE '%{_esc(obj)}%'""")
    else:
        where = (f"""FROM keyword_view WHERE {_rango(ini, fin_antes)}
                     AND campaign.name = '{campana}'
                     AND ad_group_criterion.keyword.text = '{_esc(obj)}'""")
    antes = _metricas(ga, where)

    if antes["coste"] < MIN_GASTO:
        return dict(veredicto="sin_efecto_medible", confianza="baja",
                    detalle=f"apenas gastaba (${antes['coste']}) antes de bloquearlo",
                    antes=antes)
    if antes["conv"] > 0:
        return dict(veredicto="ERROR", confianza="alta",
                    detalle=(f"se bloqueó tráfico que SÍ convertía: {antes['conv']} conv "
                             f"y ${antes['valor']} en 7 días previos"),
                    coste_del_error=antes["valor"], antes=antes)
    return dict(veredicto="acierto", confianza="alta",
                detalle=f"cortó ${antes['coste']}/7d que no producía ninguna venta",
                ahorro_7d=antes["coste"], antes=antes)


def _eval_titulo(ga, acc, ini, fin_antes, ini_desp, fin):
    """Título de feed: se compara ESE producto consigo mismo, antes vs después."""
    obj = (acc.get("objetivo") or "")
    pid = "".join(c for c in obj.split("_")[2] if c.isdigit()) if obj.count("_") >= 2 else ""
    if not pid:
        return None
    base = (f"""FROM shopping_performance_view WHERE {{rango}}
                AND segments.product_item_id LIKE '%{pid}%'""")
    antes = _metricas(ga, base.format(rango=_rango(ini, fin_antes)))
    desp = _metricas(ga, base.format(rango=_rango(ini_desp, fin)))
    if antes["impr"] < 50 and desp["impr"] < 50:
        return dict(veredicto="sin_efecto_medible", confianza="baja",
                    detalle="muy pocas impresiones para juzgar", antes=antes, despues=desp)
    d_ctr = round(desp["ctr"] - antes["ctr"], 2)
    d_conv = round(desp["conv"] - antes["conv"], 1)
    if d_conv > 0 or (d_ctr > 0.15 and desp["conv"] >= antes["conv"]):
        v, conf = "acierto", ("alta" if d_conv > 0 else "media")
    elif d_conv < 0 or d_ctr < -0.15:
        v, conf = "empeoro", "media"
    else:
        v, conf = "neutro", "media"
    return dict(veredicto=v, confianza=conf,
                detalle=(f"CTR {antes['ctr']}% → {desp['ctr']}% ({d_ctr:+}) · "
                         f"conversiones {antes['conv']} → {desp['conv']} ({d_conv:+})"),
                antes=antes, despues=desp)


def _eval_presupuesto(ga, acc, ini, fin_antes, ini_desp, fin):
    """Presupuesto: ¿aguantó el ROAS y crecieron las conversiones?"""
    campana = _esc(acc.get("campana", ""))
    if not campana:
        return None
    base = f"FROM campaign WHERE {{rango}} AND campaign.name = '{campana}'"
    antes = _metricas(ga, base.format(rango=_rango(ini, fin_antes)))
    desp = _metricas(ga, base.format(rango=_rango(ini_desp, fin)))
    if antes["coste"] < MIN_GASTO:
        return dict(veredicto="sin_efecto_medible", confianza="baja",
                    detalle="sin gasto previo con el que comparar", antes=antes, despues=desp)
    subida = desp["coste"] > antes["coste"] * 1.05
    d_roas = round(desp["roas"] - antes["roas"], 2)
    d_conv = round(desp["conv"] - antes["conv"], 1)
    ahorro = 0.0
    if subida:
        # subir sólo vale si trajo MÁS ventas sin hundir la rentabilidad
        v = "acierto" if (d_conv > 0 and desp["roas"] >= 2.0) else "empeoro"
        det = (f"gasto ${antes['coste']}→${desp['coste']}, conversiones {antes['conv']}→"
               f"{desp['conv']} ({d_conv:+}), ROAS {antes['roas']}x→{desp['roas']}x ({d_roas:+})")
    elif antes["roas"] < 1.0:
        # Un recorte NO se juzga por el ROAS que queda después: si se corta a casi
        # cero, el ROAS posterior tiende a 0 por falta de gasto y eso leería como
        # fracaso justo cuando cortar era lo correcto. Se juzga por lo que se
        # dejaba de perder. (Detectado al matar EXACT Control: 0.2x → "empeoró".)
        v = "acierto"
        ahorro = round(antes["coste"] - desp["coste"], 2)
        det = (f"cortó una sangría: rendía {antes['roas']}x antes, "
               f"gasto ${antes['coste']}→${desp['coste']} (${ahorro} menos perdidos/7d)")
    elif antes["roas"] >= 3.0 and d_conv < 0:
        v = "empeoro"
        det = (f"se recortó una campaña que funcionaba ({antes['roas']}x): "
               f"conversiones {antes['conv']}→{desp['conv']} ({d_conv:+})")
    else:
        v = "acierto" if d_roas > 0 else "empeoro"
        det = (f"recorte: gasto ${antes['coste']}→${desp['coste']}, "
               f"ROAS {antes['roas']}x→{desp['roas']}x ({d_roas:+})")
    r = dict(veredicto=v, confianza="media", detalle=det, antes=antes, despues=desp)
    if ahorro:
        r["ahorro_7d"] = ahorro
    return r


EVALUADORES = {
    "añadir_negativa": _eval_bloqueo,
    "pausar_keyword": _eval_bloqueo,
    "optimizar_titulo_feed": _eval_titulo,
    "ajustar_presupuesto": _eval_presupuesto,
}


def evaluar(force: bool = False) -> dict:
    """Evalúa las acciones con 7 días cumplidos y devuelve el balance por tipo."""
    try:
        import app
        log = app._load_actions_log()
    except Exception:
        return {}
    ga = _ga()
    hoy = _hoy()
    ya = {d.get("key") for d in mongo.all_docs("action_outcomes", limit=5000)} if not force else set()

    nuevos = []
    for e in log:
        if not e.get("ok"):
            continue
        acc = e.get("action") or {}
        tipo = acc.get("tipo")
        if tipo not in EVALUADORES or (e.get("key") in ya):
            continue
        try:
            cuando = datetime.strptime(str(e.get("ts"))[:16], "%Y-%m-%d %H:%M").date()
        except Exception:
            continue
        # hace falta que hayan pasado los 7 días de after, y que no sea prehistórico
        if (hoy - cuando).days < VENTANA or (hoy - cuando).days > 60:
            continue
        ini, fin_antes = cuando - timedelta(days=VENTANA), cuando - timedelta(days=1)
        ini_desp, fin = cuando + timedelta(days=1), cuando + timedelta(days=VENTANA)
        try:
            r = EVALUADORES[tipo](ga, acc, ini, fin_antes, ini_desp, fin)
        except Exception as exc:
            print(f"[feedback] {tipo} '{acc.get('objetivo')}': {exc}", flush=True)
            continue
        if not r:
            continue
        doc = dict(key=e.get("key"), tipo=tipo, campana=acc.get("campana"),
                   objetivo=acc.get("objetivo"), fecha=cuando.isoformat(),
                   auto=e.get("auto", False), **r)
        # upsert por `key`: re-evaluar debe CORREGIR el veredicto, no duplicarlo
        mongo.upsert("action_outcomes", {"key": doc["key"]}, doc)
        nuevos.append(doc)

    return resumen(nuevos)


def resumen(nuevos: list = None) -> dict:
    """Balance por tipo de acción, para inyectar en la revisión."""
    docs = mongo.all_docs("action_outcomes", limit=5000)
    por_tipo: dict = {}
    for d in docs:
        t = por_tipo.setdefault(d.get("tipo", "?"), {
            "aciertos": 0, "errores": 0, "empeoro": 0, "neutro": 0,
            "sin_senal": 0, "ahorro_7d": 0.0, "coste_de_errores": 0.0, "ejemplos_malos": []})
        v = d.get("veredicto")
        if v == "acierto":
            t["aciertos"] += 1
            t["ahorro_7d"] += d.get("ahorro_7d") or 0
        elif v == "ERROR":
            t["errores"] += 1
            t["coste_de_errores"] += d.get("coste_del_error") or 0
            if len(t["ejemplos_malos"]) < 3:
                t["ejemplos_malos"].append(f"{d.get('objetivo') or d.get('campana')}: {d.get('detalle')}")
        elif v == "empeoro":
            t["empeoro"] += 1
            if len(t["ejemplos_malos"]) < 3:
                t["ejemplos_malos"].append(f"{d.get('objetivo') or d.get('campana')}: {d.get('detalle')}")
        elif v == "neutro":
            t["neutro"] += 1
        else:
            t["sin_senal"] += 1
    for t in por_tipo.values():
        juzgadas = t["aciertos"] + t["errores"] + t["empeoro"] + t["neutro"]
        t["juzgadas"] = juzgadas
        t["tasa_acierto_pct"] = round(t["aciertos"] / juzgadas * 100) if juzgadas else None
        t["ahorro_7d"] = round(t["ahorro_7d"], 2)
        t["coste_de_errores"] = round(t["coste_de_errores"], 2)
    return {"por_tipo": por_tipo, "evaluadas_en_total": len(docs),
            "nuevas_esta_vez": len(nuevos or []),
            "_lectura": ("Tasa de acierto por tipo de acción, medida contra los 7 días previos y "
                         "posteriores. Si un tipo acumula errores, dejar de proponerlo y decir por "
                         "qué. 'sin_senal' = no había datos suficientes, no es ni bueno ni malo.")}


def _print(res: dict) -> None:
    pt = res.get("por_tipo") or {}
    if not pt:
        print("Todavía no hay acciones con 7 días cumplidos que evaluar.")
        return
    print(f"BALANCE DE {res['evaluadas_en_total']} ACCIONES EVALUADAS "
          f"({res['nuevas_esta_vez']} nuevas)\n")
    print(f"  {'tipo':<24}{'juzg.':>6}{'acierto':>9}{'error':>7}{'peor':>6}{'ahorro/7d':>11}")
    for t, d in sorted(pt.items(), key=lambda x: -(x[1]["juzgadas"])):
        tasa = f"{d['tasa_acierto_pct']}%" if d["tasa_acierto_pct"] is not None else "—"
        print(f"  {t:<24}{d['juzgadas']:>6}{tasa:>9}{d['errores']:>7}{d['empeoro']:>6}"
              f"{d['ahorro_7d']:>10.2f}")
        for ej in d["ejemplos_malos"]:
            print(f"       ⚠️  {ej[:96]}")


if __name__ == "__main__":
    r = evaluar(force="--force" in sys.argv)
    if "--resumen" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=1)[:3000])
    else:
        _print(r)

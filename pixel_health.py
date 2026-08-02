"""Salud del pixel: vigila que las conversiones estén pasando bien.

Consulta la Google Ads API y devuelve un diagnóstico:
  - qué acciones de conversión son PRIMARIAS ahora mismo (deberían ser solo
    las compras de AdNabu y jerseyplastic.com)
  - qué registró cada acción en los últimos 30 días (conversiones y valor)
  - clics recibidos en 30 días, para detectar "hay tráfico pero no hay
    conversiones" = pixel roto
  - alertas automáticas si la configuración se degrada
"""

import time
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient

CUSTOMER_ID = "4888823590"
EXPECTED_PRIMARY_PURCHASES = {"Jersey Pickles <> AdNabu", "Purchase (jerseyplastic.com)"}
BAD_PRIMARY_CATEGORIES = {"PAGE_VIEW", "ADD_TO_CART", "BEGIN_CHECKOUT", "ENGAGEMENT"}

_cache: dict = {"at": 0.0, "data": None}
CACHE_TTL = 3600  # 1 hora


def _check() -> dict:
    client = GoogleAdsClient.load_from_storage("google-ads.yaml")
    service = client.get_service("GoogleAdsService")
    end = date.today()
    start = end - timedelta(days=30)

    # 1) acciones habilitadas y cuáles son primarias
    actions = []
    q1 = """SELECT conversion_action.name, conversion_action.primary_for_goal,
                   conversion_action.category, conversion_action.type
            FROM conversion_action WHERE conversion_action.status = 'ENABLED'"""
    for batch in service.search_stream(customer_id=CUSTOMER_ID, query=q1):
        for r in batch.results:
            a = r.conversion_action
            actions.append(dict(
                name=a.name, primary=a.primary_for_goal,
                category=a.category.name, type=a.type_.name,
            ))

    # 2) conversiones por acción, últimos 30 días
    recent = {}
    q2 = f"""SELECT segments.conversion_action_name, metrics.all_conversions,
                    metrics.all_conversions_value
             FROM customer
             WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"""
    for batch in service.search_stream(customer_id=CUSTOMER_ID, query=q2):
        for r in batch.results:
            t = recent.setdefault(r.segments.conversion_action_name, [0.0, 0.0])
            t[0] += r.metrics.all_conversions
            t[1] += r.metrics.all_conversions_value

    # 3) clics últimos 30 días (contexto: sin clics no puede haber conversiones)
    clicks = 0
    q3 = f"""SELECT metrics.clicks FROM customer
             WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"""
    for batch in service.search_stream(customer_id=CUSTOMER_ID, query=q3):
        for r in batch.results:
            clicks += r.metrics.clicks

    # --- alertas ---
    alerts = []
    primaries = [a for a in actions if a["primary"]]
    bad = [a["name"] for a in primaries if a["category"] in BAD_PRIMARY_CATEGORIES]
    if bad:
        alerts.append(dict(level="critical",
            msg=f"Acciones que NO deberían ser primarias lo son (inflan conversiones): {', '.join(bad)}"))

    purchase_primaries = {a["name"] for a in primaries if a["category"] == "PURCHASE"}
    extra = purchase_primaries - EXPECTED_PRIMARY_PURCHASES
    if extra:
        alerts.append(dict(level="warning",
            msg=f"Compras primarias inesperadas (riesgo de doble conteo): {', '.join(sorted(extra))}"))
    missing = EXPECTED_PRIMARY_PURCHASES - purchase_primaries
    if missing:
        alerts.append(dict(level="critical",
            msg=f"Falta la compra primaria esperada: {', '.join(sorted(missing))}"))

    purchase_conv_30d = sum(
        v[0] for name, v in recent.items()
        if any(name == a["name"] and a["category"] == "PURCHASE" for a in actions)
    )
    if clicks > 50 and purchase_conv_30d == 0:
        alerts.append(dict(level="critical",
            msg=f"{clicks} clics en 30 días y CERO compras registradas — el pixel puede estar roto."))
    elif clicks <= 50:
        alerts.append(dict(level="info",
            msg="Campañas sin tráfico (pausadas): el pixel solo registra conversiones atribuidas a "
                "anuncios, así que la verificación definitiva será en los primeros días tras el "
                "relanzamiento. Vigilar que 'Jersey Pickles <> AdNabu' empiece a registrar compras."))

    return dict(
        checked_at=time.strftime("%Y-%m-%d %H:%M"),
        clicks_30d=clicks,
        primaries=sorted(primaries, key=lambda a: (a["category"] != "PURCHASE", a["name"])),
        recent=sorted(
            [dict(name=k, conv=round(v[0], 1), value=round(v[1], 2)) for k, v in recent.items()],
            key=lambda x: -x["conv"],
        ),
        alerts=alerts,
    )


def get_health(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    data = _check()
    _cache.update(at=now, data=data)
    return data

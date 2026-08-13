"""Ejecutor de acciones de Fable sobre Google Ads.

Tipos soportados (Search):
  - pausar_keyword / reactivar_keyword  (objetivo = texto de la keyword)
  - añadir_negativa                     (objetivo = término; a nivel campaña, BROAD)
  - ajustar_presupuesto                 (valor = USD/día, límites 5-300)
  - ajustar_tope_cpc                    (valor = USD, límites 0.30-5.00)
  - crear_keyword                       (objetivo = texto, valor = EXACT|PHRASE, grupo = nombre)

Tipos soportados (Shopping):
  - ajustar_puja_grupo        (objetivo = nombre del grupo, valor = USD 0.20-2.00)
  - excluir_producto_shopping (objetivo = item_id exacto del feed)
  - mover_producto_grupo      (objetivo = item_id, valor = nombre del grupo destino)

Con salvaguardas: una negativa nunca puede bloquear una keyword activa de la
propia campaña, y los valores de dinero tienen límites duros.
"""

import re
from pathlib import Path

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

BASE = Path(__file__).parent
CUSTOMER_ID = "4888823590"


def _clean_kw(text: str) -> str:
    """Fable a veces etiqueta la concordancia en el texto: 'kw [EXACT]' → 'kw'."""
    t = (text or "").strip()
    t = re.sub(r"\s*\[(EXACT|PHRASE|BROAD)\]\s*$", "", t, flags=re.I)
    return t.strip().strip("\"'“”‘’").strip()


def _client():
    return GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))


def _campaign_by_name(ga, name: str):
    q = f"""SELECT campaign.id, campaign.resource_name, campaign.campaign_budget
            FROM campaign WHERE campaign.name = '{name}' AND campaign.status != 'REMOVED'"""
    for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for r in b.results:
            return r.campaign
    return None


def _keyword_criterion(ga, campaign_id: int, text: str):
    safe = text.replace("'", "\\'")
    q = f"""SELECT ad_group_criterion.resource_name, ad_group_criterion.status
            FROM ad_group_criterion
            WHERE campaign.id = {campaign_id} AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
              AND ad_group_criterion.keyword.text = '{safe}'"""
    for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for r in b.results:
            return r.ad_group_criterion
    return None


def _active_keywords(ga, campaign_id: int) -> list[str]:
    # SOLO las habilitadas: una keyword pausada ya no trae tráfico, así que una
    # negativa que la contenga no bloquea nada nuestro. Si se contaran las pausadas,
    # el guardián impediría para siempre negativizar el término que acaba de pausar.
    q = f"""SELECT ad_group_criterion.keyword.text FROM ad_group_criterion
            WHERE campaign.id = {campaign_id} AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
              AND ad_group_criterion.status = 'ENABLED'"""
    return [r.ad_group_criterion.keyword.text.lower()
            for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q) for r in b.results]


def _ad_group_by_name(ga, campaign_id: int, name: str):
    safe = name.replace("'", "\\'")
    q = f"""SELECT ad_group.id, ad_group.resource_name, ad_group.name, ad_group.cpc_bid_micros
            FROM ad_group WHERE campaign.id = {campaign_id}
              AND ad_group.name = '{safe}' AND ad_group.status != 'REMOVED'"""
    for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for r in b.results:
            return r.ad_group
    return None


def _listing_tree(ga, campaign_id: int) -> dict:
    """Mapa del árbol de shopping: por grupo, su raíz, unidades y nodo 'resto'."""
    q = f"""SELECT ad_group.name, ad_group.resource_name, ad_group.cpc_bid_micros,
            ad_group_criterion.resource_name, ad_group_criterion.negative,
            ad_group_criterion.listing_group.type,
            ad_group_criterion.listing_group.case_value.product_item_id.value
            FROM ad_group_criterion
            WHERE campaign.id = {campaign_id} AND ad_group_criterion.type = LISTING_GROUP"""
    tree: dict = {}
    for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
        for r in b.results:
            g = tree.setdefault(r.ad_group.name, dict(
                ad_group=r.ad_group.resource_name, bid=r.ad_group.cpc_bid_micros,
                root=None, units={}, resto=None, resto_negativo=None))
            lg = r.ad_group_criterion.listing_group
            rn = r.ad_group_criterion.resource_name
            if lg.type_.name == "SUBDIVISION":
                g["root"] = rn
            else:
                val = (lg.case_value.product_item_id.value or "").lower()
                if val:
                    g["units"][val] = dict(rn=rn, negative=r.ad_group_criterion.negative)
                else:
                    g["resto"] = rn
                    g["resto_negativo"] = r.ad_group_criterion.negative
    return tree


def _nueva_unidad(client, ad_group_rn: str, root_rn: str, item_id: str,
                  bid_micros: int | None, negative: bool):
    op = client.get_type("AdGroupCriterionOperation")
    c = op.create
    c.ad_group = ad_group_rn
    if negative:
        c.negative = True
    else:
        c.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        if bid_micros:
            c.cpc_bid_micros = bid_micros
    c.listing_group.type_ = client.enums.ListingGroupTypeEnum.UNIT
    c.listing_group.parent_ad_group_criterion = root_rn
    c.listing_group.case_value.product_item_id.value = item_id
    return op


def asegurar_audiencias_en_observacion(nombres: list = None) -> dict:
    """Blindaje: las audiencias deben ser OBSERVACIÓN, nunca segmentación.

    Sin targeting_setting explícito Google restringe la campaña a esas listas y
    el alcance se desploma (incidente 2026-08-10).
    """
    try:
        client = _client()
        ga = client.get_service("GoogleAdsService")
        q = """SELECT campaign.resource_name, campaign.name,
               campaign.targeting_setting.target_restrictions FROM campaign
               WHERE campaign.status = 'ENABLED'
                 AND campaign.advertising_channel_type = 'SEARCH'"""
        ops, tocadas = [], []
        for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
            for r in b.results:
                if nombres and r.campaign.name not in nombres:
                    continue
                aud = [t for t in r.campaign.targeting_setting.target_restrictions
                       if t.targeting_dimension.name == "AUDIENCE"]
                if aud and all(t.bid_only for t in aud):
                    continue                      # ya está en observación
                op = client.get_type("CampaignOperation")
                op.update.resource_name = r.campaign.resource_name
                tr = client.get_type("TargetRestriction")
                tr.targeting_dimension = client.enums.TargetingDimensionEnum.AUDIENCE
                tr.bid_only = True
                op.update.targeting_setting.target_restrictions.append(tr)
                client.copy_from(op.update_mask, field_mask_pb2.FieldMask(
                    paths=["targeting_setting.target_restrictions"]))
                ops.append(op)
                tocadas.append(r.campaign.name)
        if ops:
            client.get_service("CampaignService").mutate_campaigns(
                customer_id=CUSTOMER_ID, operations=ops)
        return dict(ok=True, msg=f"audiencias en observación; corregidas: {tocadas or 'ninguna'}")
    except Exception as exc:
        return dict(ok=False, msg=str(exc)[:200])


def set_campaign_status(nombre: str, activa: bool) -> dict:
    """Enciende o pausa la campaña EN Google Ads (el switch del panel manda)."""
    try:
        client = _client()
        ga = client.get_service("GoogleAdsService")
        camp = _campaign_by_name(ga, nombre)
        if camp is None:
            return dict(ok=False, msg=f"campaña '{nombre}' no está en Google Ads")
        op = client.get_type("CampaignOperation")
        op.update.resource_name = camp.resource_name
        op.update.status = (client.enums.CampaignStatusEnum.ENABLED if activa
                            else client.enums.CampaignStatusEnum.PAUSED)
        client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["status"]))
        client.get_service("CampaignService").mutate_campaigns(
            customer_id=CUSTOMER_ID, operations=[op])
        return dict(ok=True, msg=f"'{nombre}' → {'ACTIVA' if activa else 'PAUSADA'} en Google Ads")
    except GoogleAdsException as ex:
        return dict(ok=False, msg="; ".join(e.message for e in ex.failure.errors)[:200])
    except Exception as exc:
        return dict(ok=False, msg=str(exc)[:200])


def keyword_7d_stats(campana: str, text: str):
    """(coste, conversiones) de una keyword en los últimos 7 días, o None."""
    try:
        client = _client()
        ga = client.get_service("GoogleAdsService")
        camp = _campaign_by_name(ga, campana)
        if camp is None:
            return None
        safe = _clean_kw(text).replace("'", "\\'")
        q = f"""SELECT metrics.cost_micros, metrics.conversions FROM keyword_view
                WHERE campaign.id = {camp.id} AND segments.date DURING LAST_7_DAYS
                  AND ad_group_criterion.keyword.text = '{safe}'"""
        cost = conv = 0.0
        for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
            for r in b.results:
                cost += r.metrics.cost_micros / 1e6
                conv += r.metrics.conversions
        return cost, conv
    except Exception:
        return None


def grupo_7d_stats(campana: str, grupo: str):
    """(coste, conversiones, puja_actual) de un grupo en 7 días, o None.

    Lo usa el piloto para verificar por código un recorte de puja antes de
    aplicarlo solo: bajar ante una sangría probada es higiene, subir es una
    apuesta y esa la decide el dueño.
    """
    try:
        client = _client()
        ga = client.get_service("GoogleAdsService")
        camp = _campaign_by_name(ga, campana)
        if camp is None:
            return None
        safe = (grupo or "").replace("'", "\\'")
        q = f"""SELECT ad_group.cpc_bid_micros, metrics.cost_micros, metrics.conversions
                FROM ad_group WHERE campaign.id = {camp.id}
                  AND ad_group.name = '{safe}' AND segments.date DURING LAST_7_DAYS"""
        cost = conv = 0.0
        puja = None
        for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
            for r in b.results:
                cost += r.metrics.cost_micros / 1e6
                conv += r.metrics.conversions
                puja = r.ad_group.cpc_bid_micros / 1e6
        return (cost, conv, puja) if puja is not None else None
    except Exception:
        return None


def apply_action(a: dict) -> dict:
    tipo = a.get("tipo")
    campana = a.get("campana", "")
    objetivo = (a.get("objetivo") or "").strip()
    if tipo in ("pausar_keyword", "reactivar_keyword", "añadir_negativa", "crear_keyword"):
        objetivo = _clean_kw(objetivo)
    valor = a.get("valor")

    if tipo == "optimizar_titulo_feed":
        nuevo = str(valor or "").strip()
        if not 10 <= len(nuevo) <= 150:
            return dict(ok=False, msg=f"título de {len(nuevo)} caracteres (10-150)")
        if re.search(r"(free shipping|env[ií]o gratis|% ?off|sale|discount|oferta)", nuevo, re.I):
            return dict(ok=False, msg="texto promocional en el título — política de Google lo prohíbe")
        if nuevo == nuevo.upper():
            return dict(ok=False, msg="título todo en mayúsculas — política de Google")
        # ATRIBUTOS INVENTADOS: un título no puede afirmar algo que la ficha no dice.
        # Google desaprueba por tergiversación y el cliente pide reembolso.
        # 'refrigerated' se añadió el 12-ago: se propuso (y se escribió a mano) un
        # "Fresh Refrigerated" que ni la ficha ni la política de envíos respaldan.
        # Prometer cadena de frío que no existe es una devolución segura escondida
        # tras una palabra que suena inofensiva.
        ATRIBUTOS = ("in oil", "en aceite", "marinated", "marinado", "organic", "orgánico",
                     "gluten free", "sin gluten", "sugar free", "sin azúcar", "vegan",
                     "kosher certified", "non gmo", "raw", "unpasteurized", "probiotic",
                     "smoked", "ahumado", "spicy", "hot ", "sweet ", "fermented",
                     "refrigerated", "refrigerado", "keep cold", "small batch",
                     "handmade", "hecho a mano", "artisan", "artesanal")
        try:
            import json as _json
            import urllib.request as _url
            mm = re.search(r"shopify_[a-z]{2}_(\d+)_", objetivo, re.I) or re.search(r"(\d{10,})", objetivo)
            if mm:
                tok = (BASE / ".shopify_token").read_text().strip()
                rq = _url.Request(
                    f"https://113e43-2.myshopify.com/admin/api/2025-07/products/{mm.group(1)}.json",
                    headers={"X-Shopify-Access-Token": tok})
                prod = _json.loads(_url.urlopen(rq, timeout=25).read())["product"]
                ficha = " ".join([prod.get("title", ""), prod.get("body_html", "") or "",
                                  prod.get("tags", "") or "", prod.get("product_type", "") or ""]).lower()
                ficha = re.sub(r"<[^>]+>", " ", ficha)
                inventados = [a.strip() for a in ATRIBUTOS
                              if a in nuevo.lower() and a.strip() not in ficha]
                if inventados:
                    return dict(ok=False, msg=(
                        f"BLOQUEADO: el título afirma {inventados} y la ficha del producto no lo "
                        f"respalda — riesgo de desaprobación por tergiversación. Verifícalo primero."))
        except Exception:
            pass   # si no se puede verificar, no se bloquea (el dueño decide)

        m = re.search(r"shopify_[a-z]{2}_(\d+)_\d+", objetivo, re.I) or re.search(r"(\d{10,})", objetivo)
        if not m:
            return dict(ok=False, msg=f"objetivo debe ser item_id del feed o product_id: '{objetivo}'")
        pid = int(m.group(1))
        # el título es de PRODUCTO: el feed añade la variante solo. Un sufijo de
        # talla en el override saldría duplicado (o con la talla equivocada).
        try:
            from store_catalog import shopify_catalog
            prod = shopify_catalog()["by_product"].get(pid) or {}
            for v in prod.get("variants", []):
                vt = (v.get("t") or "").strip()
                if vt and vt.lower() not in ("default title"):
                    for sep in (" - ", " – ", " · ", " "):
                        if nuevo.lower().endswith((sep + vt).lower()):
                            nuevo = nuevo[: -len(sep + vt)].strip(" -–·")
                            break
        except Exception:
            pass
        try:
            import db
            db.upsert("feed_overrides", {"pid": pid}, {"pid": pid, "title": nuevo})
        except Exception as exc:
            return dict(ok=False, msg=f"no se pudo guardar el override: {exc}")
        return dict(ok=True, msg=f"título del producto {pid} → “{nuevo}” (el feed se re-sincroniza)")

    try:
        client = _client()
        ga = client.get_service("GoogleAdsService")
        camp = _campaign_by_name(ga, campana)
        if camp is None:
            return dict(ok=False, msg=f"campaña '{campana}' no encontrada")

        if tipo in ("pausar_keyword", "reactivar_keyword"):
            crit = _keyword_criterion(ga, camp.id, objetivo)
            if crit is None:
                return dict(ok=False, msg=f"keyword '{objetivo}' no encontrada en la campaña")
            svc = client.get_service("AdGroupCriterionService")
            op = client.get_type("AdGroupCriterionOperation")
            op.update.resource_name = crit.resource_name
            op.update.status = (
                client.enums.AdGroupCriterionStatusEnum.PAUSED
                if tipo == "pausar_keyword"
                else client.enums.AdGroupCriterionStatusEnum.ENABLED
            )
            client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["status"]))
            svc.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=[op])
            verbo = "pausada" if tipo == "pausar_keyword" else "reactivada"
            return dict(ok=True, msg=f"keyword '{objetivo}' {verbo} en {campana}")

        if tipo == "añadir_negativa":
            neg = objetivo.lower()
            match = str(a.get("match") or "BROAD").upper()
            if match not in ("BROAD", "PHRASE", "EXACT"):
                match = "BROAD"
            # Por PALABRAS, no por subcadena: 'beet' está dentro de 'sweet pickles'
            # y con `neg in kw` se rechazaba una negativa perfectamente legítima.
            pal_neg = set(neg.split())
            for kw in _active_keywords(ga, camp.id):
                if neg == kw or pal_neg <= set(kw.split()):
                    return dict(ok=False, msg=f"BLOQUEADO: '{objetivo}' chocaría con la keyword activa '{kw}'")
            # una BROAD que contiene una keyword de OTRA campaña propia mata su tráfico:
            # el flujo entre campañas se hace con negativas EXACT (lección del proyecto)
            if match == "BROAD":
                q = """SELECT campaign.name, ad_group_criterion.keyword.text
                       FROM ad_group_criterion
                       WHERE ad_group_criterion.type = 'KEYWORD'
                         AND ad_group_criterion.negative = FALSE
                         AND ad_group_criterion.status = 'ENABLED'
                         AND campaign.status = 'ENABLED'"""
                for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
                    for r in b.results:
                        propia = r.ad_group_criterion.keyword.text.lower()
                        if propia and (neg in propia or propia in neg):
                            match = "EXACT"   # se degrada sola: bloquea el término, no la familia
                            break
            q = f"""SELECT campaign_criterion.keyword.text FROM campaign_criterion
                    WHERE campaign.id = {camp.id} AND campaign_criterion.negative = TRUE
                      AND campaign_criterion.type = 'KEYWORD'"""
            for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
                for r in b.results:
                    if r.campaign_criterion.keyword.text.lower() == neg:
                        return dict(ok=True, msg=f"'{objetivo}' ya estaba como negativa en {campana}")
            svc = client.get_service("CampaignCriterionService")
            op = client.get_type("CampaignCriterionOperation")
            op.create.campaign = camp.resource_name
            op.create.negative = True
            op.create.keyword.text = objetivo
            op.create.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match)
            svc.mutate_campaign_criteria(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"negativa '{objetivo}' añadida ({match}) a {campana}")

        if tipo == "ajustar_presupuesto":
            v = float(valor)
            if not 5 <= v <= 300:
                return dict(ok=False, msg=f"presupuesto ${v} fuera de límites (5-300)")
            # protección del learning: el tamaño del paso depende de la estrategia de puja
            q = f"""SELECT campaign.bidding_strategy_type, campaign_budget.amount_micros
                    FROM campaign WHERE campaign.id = {camp.id}"""
            actual, estrategia = None, ""
            for b in ga.search_stream(customer_id=CUSTOMER_ID, query=q):
                for r in b.results:
                    actual = r.campaign_budget.amount_micros / 1e6
                    estrategia = r.campaign.bidding_strategy_type.name
            if actual:
                paso = abs(v - actual) / actual
                # manual = sin algoritmo que resetear (tope 100%); smart bidding = ±30%
                tope = 1.00 if estrategia == "MANUAL_CPC" else 0.30
                if paso > tope + 0.005:  # margen para redondeos
                    seguro = round(actual * (1 + tope) if v > actual else actual * (1 - tope), 2)
                    return dict(ok=False, msg=(
                        f"BLOQUEADO: cambio de ${actual:.0f}→${v:.0f} es {paso*100:.0f}% y la "
                        f"estrategia {estrategia} tolera ±{tope*100:.0f}% por paso sin perder "
                        f"learning — máximo seguro ahora: ${seguro}"))
            svc = client.get_service("CampaignBudgetService")
            op = client.get_type("CampaignBudgetOperation")
            op.update.resource_name = camp.campaign_budget
            op.update.amount_micros = int(v * 1_000_000)
            client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["amount_micros"]))
            svc.mutate_campaign_budgets(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"presupuesto de {campana} → ${v}/día")

        if tipo == "ajustar_tope_cpc":
            v = float(valor)
            if not 0.30 <= v <= 5.00:
                return dict(ok=False, msg=f"tope CPC ${v} fuera de límites (0.30-5.00)")
            svc = client.get_service("CampaignService")
            op = client.get_type("CampaignOperation")
            op.update.resource_name = camp.resource_name
            op.update.target_spend.cpc_bid_ceiling_micros = int(v * 1_000_000)
            client.copy_from(op.update_mask,
                             field_mask_pb2.FieldMask(paths=["target_spend.cpc_bid_ceiling_micros"]))
            svc.mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"tope de CPC de {campana} → ${v}")

        if tipo == "crear_keyword":
            match = (str(valor) or "PHRASE").strip().upper()
            if match not in ("EXACT", "PHRASE"):
                return dict(ok=False, msg=f"concordancia inválida: {valor} (EXACT|PHRASE)")
            if len(objetivo) > 80 or len(objetivo.split()) > 10 or set("!@%,*;~^()<>[]{}|?=") & set(objetivo):
                return dict(ok=False, msg=f"keyword inválida para Google: '{objetivo}'")
            if objetivo.lower() in _active_keywords(ga, camp.id):
                return dict(ok=True, msg=f"'{objetivo}' ya estaba activa en {campana}")
            gname = (a.get("grupo") or "").strip()
            ag = _ad_group_by_name(ga, camp.id, gname) if gname else None
            if ag is None:
                return dict(ok=False, msg=f"grupo '{gname}' no encontrado en {campana} (campo 'grupo' obligatorio)")
            svc = client.get_service("AdGroupCriterionService")
            op = client.get_type("AdGroupCriterionOperation")
            op.create.ad_group = ag.resource_name
            op.create.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            op.create.keyword.text = objetivo
            op.create.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match)
            svc.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"keyword '{objetivo}' [{match}] creada en {campana} › {gname}")

        if tipo == "ajustar_puja_grupo":
            v = float(valor)
            if not 0.20 <= v <= 2.00:
                return dict(ok=False, msg=f"puja ${v} fuera de límites (0.20-2.00)")
            ag = _ad_group_by_name(ga, camp.id, objetivo)
            if ag is None:
                return dict(ok=False, msg=f"grupo '{objetivo}' no encontrado en {campana}")
            svc = client.get_service("AdGroupService")
            op = client.get_type("AdGroupOperation")
            op.update.resource_name = ag.resource_name
            op.update.cpc_bid_micros = int(v * 1_000_000)
            client.copy_from(op.update_mask, field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]))
            svc.mutate_ad_groups(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"puja del grupo '{objetivo}' → ${v} (antes ${ag.cpc_bid_micros/1e6:.2f})")

        if tipo in ("excluir_producto_shopping", "mover_producto_grupo"):
            item = objetivo.lower()
            if not item.startswith("shopify_"):
                return dict(ok=False, msg=f"objetivo debe ser el item_id exacto del feed: '{objetivo}'")
            tree = _listing_tree(ga, camp.id)
            if not tree:
                return dict(ok=False, msg=f"'{campana}' no tiene árbol de productos (¿no es shopping?)")
            svc = client.get_service("AdGroupCriterionService")
            ops, notas = [], []
            escoba = next((g for g in tree.values() if g["resto"] and not g["resto_negativo"]), None)

            if tipo == "excluir_producto_shopping":
                for gname, g in tree.items():
                    u = g["units"].get(item)
                    if u and not u["negative"]:
                        op = client.get_type("AdGroupCriterionOperation")
                        op.remove = u["rn"]
                        ops.append(op)
                        notas.append(f"quitado de '{gname}'")
                if escoba and item not in escoba["units"]:
                    ops.append(_nueva_unidad(client, escoba["ad_group"], escoba["root"], objetivo, None, True))
                    notas.append("negado en el grupo escoba")
                if not ops:
                    return dict(ok=True, msg=f"'{objetivo}' ya estaba excluido en {campana}")
                svc.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=ops)
                return dict(ok=True, msg=f"producto excluido: {'; '.join(notas)}")

            destino = (str(valor) or "").strip()
            gd = tree.get(destino)
            if gd is None or not gd["root"]:
                return dict(ok=False, msg=f"grupo destino '{destino}' no encontrado o sin subdivisión")
            for gname, g in tree.items():
                u = g["units"].get(item)
                if u and gname != destino:
                    op = client.get_type("AdGroupCriterionOperation")
                    op.remove = u["rn"]
                    ops.append(op)
                    notas.append(f"quitado de '{gname}'")
            ya = gd["units"].get(item)
            if ya and not ya["negative"]:
                return dict(ok=True, msg=f"'{objetivo}' ya estaba en '{destino}'")
            if ya and ya["negative"]:
                op = client.get_type("AdGroupCriterionOperation")
                op.remove = ya["rn"]
                ops.append(op)
            ops.append(_nueva_unidad(client, gd["ad_group"], gd["root"], objetivo, gd["bid"], False))
            notas.append(f"añadido a '{destino}' (puja del grupo ${gd['bid']/1e6:.2f})")
            # sin solaparse: si el escoba lo servía por 'resto', se niega ahí
            if escoba and destino != next(n for n, g in tree.items() if g is escoba) and item not in escoba["units"]:
                ops.append(_nueva_unidad(client, escoba["ad_group"], escoba["root"], objetivo, None, True))
                notas.append("negado en el grupo escoba")
            svc.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=ops)
            return dict(ok=True, msg="; ".join(notas))

        return dict(ok=False, msg=f"tipo de acción desconocido: {tipo}")
    except GoogleAdsException as ex:
        return dict(ok=False, msg="; ".join(e.message for e in ex.failure.errors)[:300])
    except Exception as exc:
        return dict(ok=False, msg=str(exc)[:300])

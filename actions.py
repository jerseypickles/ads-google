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

from pathlib import Path

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

BASE = Path(__file__).parent
CUSTOMER_ID = "4888823590"


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
    q = f"""SELECT ad_group_criterion.keyword.text FROM ad_group_criterion
            WHERE campaign.id = {campaign_id} AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE"""
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


def apply_action(a: dict) -> dict:
    tipo = a.get("tipo")
    campana = a.get("campana", "")
    objetivo = (a.get("objetivo") or "").strip()
    valor = a.get("valor")

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
            for kw in _active_keywords(ga, camp.id):
                if neg == kw or neg in kw:
                    return dict(ok=False, msg=f"BLOQUEADO: '{objetivo}' chocaría con la keyword activa '{kw}'")
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
            op.create.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            svc.mutate_campaign_criteria(customer_id=CUSTOMER_ID, operations=[op])
            return dict(ok=True, msg=f"negativa '{objetivo}' añadida (BROAD) a {campana}")

        if tipo == "ajustar_presupuesto":
            v = float(valor)
            if not 5 <= v <= 300:
                return dict(ok=False, msg=f"presupuesto ${v} fuera de límites (5-300)")
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

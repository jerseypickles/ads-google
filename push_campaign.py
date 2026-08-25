"""Sube una campaña del gestor a Google Ads (SIEMPRE en pausado).

Crea: presupuesto → campaña Search (PAUSED, Maximizar clics con tope de CPC $2)
→ geo EE.UU. + idioma inglés + negativas de campaña → grupos de anuncios
→ keywords con su concordancia → anuncios RSA.

Uso:
    .venv/bin/python push_campaign.py "NOMBRE EXACTO DE LA CAMPAÑA"
"""

import json
import sys
import time
from pathlib import Path

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

BASE = Path(__file__).parent
CUSTOMER_ID = "4888823590"
FINAL_URL = "https://jerseypickles.com/collections/pickles"  # landing por defecto
# landing específica por tema de grupo (se aplica si el nombre del grupo contiene la clave)
GROUP_URLS = {
    "vegetable": "https://jerseypickles.com/collections/all-products",
    "vegetales": "https://jerseypickles.com/collections/all-products",
}


def _group_url(group_name: str) -> str:
    low = group_name.lower()
    for key, url in GROUP_URLS.items():
        if key in low:
            return url
    return FINAL_URL
GEO_US = "geoTargetConstants/2840"
LANG_EN = "languageConstants/1000"
CPC_CEILING_MICROS = 2_000_000  # tope $2.00 por clic

MATCH = {"EXACT": "EXACT", "PHRASE": "PHRASE", "BROAD": "BROAD"}


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


STORE_PATH = BASE / "campaigns_local.json"


def _load_store() -> dict:
    if STORE_PATH.exists():
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    import db as mongo
    doc = mongo.get_doc("manager", {"_id": "store"})
    return (doc or {}).get("store") or {"campaigns": [], "negatives": []}


def _save_store(store: dict) -> None:
    STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import db as mongo
        mongo.upsert("manager", {"_id": "store"}, {"store": store})
    except Exception:
        pass


MERCHANT_ID = 5080357407  # Jersey Pickles (5564945299 es Jersey Plastic — ajeno)


def _listing_tree_ops(client, ag_rn: str, include_ids: list, exclude_ids: list,
                      all_mode: bool, bid_micros: int) -> list:
    """Árbol de listing groups de un ad group Shopping, en UNA sola mutación.

    - all_mode: raíz que recoge todo, con exclusiones explícitas (negative units).
    - si no: subdivisión con UNIT por item_id y el nodo 'resto' EXCLUIDO.
    """
    enums = client.enums
    cid = CUSTOMER_ID
    ag_id = ag_rn.split("/")[-1]
    ops = []
    temp = [-1]

    def _new(parent_rn: str | None, negative: bool = False):
        op = client.get_type("AdGroupCriterionOperation")
        c = op.create
        c.ad_group = ag_rn
        if negative:
            c.negative = True
        else:
            c.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        c.resource_name = f"customers/{cid}/adGroupCriteria/{ag_id}~{temp[0]}"
        temp[0] -= 1
        if parent_rn:
            c.listing_group.parent_ad_group_criterion = parent_rn
        ops.append(op)
        return c

    if all_mode and not exclude_ids:
        root = _new(None)
        root.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
        root.cpc_bid_micros = bid_micros
        return ops

    root = _new(None)
    root.listing_group.type_ = enums.ListingGroupTypeEnum.SUBDIVISION
    root_rn = root.resource_name

    for iid in include_ids:
        u = _new(root_rn)
        u.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
        u.listing_group.case_value.product_item_id.value = iid
        u.cpc_bid_micros = bid_micros
    for iid in exclude_ids:
        u = _new(root_rn, negative=True)
        u.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
        u.listing_group.case_value.product_item_id.value = iid

    # nodo obligatorio 'resto de productos': incluido en all_mode, excluido si el
    # grupo apunta a items concretos
    rest = _new(root_rn, negative=not all_mode)
    rest.listing_group.type_ = enums.ListingGroupTypeEnum.UNIT
    client.copy_from(rest.listing_group.case_value.product_item_id,
                     client.get_type("ProductItemIdInfo"))
    if all_mode:
        rest.cpc_bid_micros = bid_micros
    return ops


def push_shopping(camp: dict, store: dict) -> str:
    """Sube una campaña Shopping estándar del gestor a Google Ads."""
    import validate_plan
    issues = [i for i in validate_plan.check_shopping(camp) if i[0] == "ERROR"]
    if issues:
        detalle = "; ".join(f"{w}: {m}" for _, w, m in issues)
        raise RuntimeError(f"validador: {detalle}")
    print("✓ Validador pre-vuelo (shopping): sin errores")

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    enums = client.enums

    # 1) presupuesto
    op = client.get_type("CampaignBudgetOperation")
    b = op.create
    b.name = f"{camp['name']} · budget {int(time.time())}"
    b.amount_micros = int(camp["daily_budget_usd"] * 1_000_000)
    b.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    budget_rn = (
        client.get_service("CampaignBudgetService")
        .mutate_campaign_budgets(customer_id=CUSTOMER_ID, operations=[op])
        .results[0].resource_name
    )
    print(f"✓ Presupuesto ${camp['daily_budget_usd']}/día → {budget_rn}")

    # 2) campaña Shopping — CPC manual (primera campaña: leer señal limpia)
    launch_on = bool(camp.get("enabled", True))
    op = client.get_type("CampaignOperation")
    c = op.create
    c.name = camp["name"]
    c.status = (
        enums.CampaignStatusEnum.ENABLED if launch_on else enums.CampaignStatusEnum.PAUSED
    )
    c.advertising_channel_type = enums.AdvertisingChannelTypeEnum.SHOPPING
    c.campaign_budget = budget_rn
    c.shopping_setting.merchant_id = MERCHANT_ID
    c.shopping_setting.campaign_priority = 0
    c.shopping_setting.enable_local = False
    client.copy_from(c.manual_cpc, client.get_type("ManualCpc"))
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = False
    c.network_settings.target_partner_search_network = False
    if hasattr(c, "contains_eu_political_advertising"):
        c.contains_eu_political_advertising = (
            enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    campaign_rn = (
        client.get_service("CampaignService")
        .mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
        .results[0].resource_name
    )
    print(f"✓ Campaña Shopping {'ENCENDIDA' if launch_on else 'PAUSADA'} → {campaign_rn}")

    # 3) geo EE.UU. + negativas (globales del gestor + propias del plan, BROAD)
    crit_ops = []
    o = client.get_type("CampaignCriterionOperation")
    o.create.campaign = campaign_rn
    o.create.location.geo_target_constant = GEO_US
    crit_ops.append(o)
    negativas = sorted(set(store.get("negatives", [])) | set(camp.get("negatives", [])))
    for n in negativas:
        o = client.get_type("CampaignCriterionOperation")
        o.create.campaign = campaign_rn
        o.create.negative = True
        o.create.keyword.text = n
        o.create.keyword.match_type = enums.KeywordMatchTypeEnum.BROAD
        crit_ops.append(o)
    client.get_service("CampaignCriterionService").mutate_campaign_criteria(
        customer_id=CUSTOMER_ID, operations=crit_ops
    )
    print(f"✓ Geo EE.UU. + {len(negativas)} negativas BROAD")

    # 4) grupos de productos: ad group + árbol de listing groups + anuncio shopping
    ag_service = client.get_service("AdGroupService")
    crit_service = client.get_service("AdGroupCriterionService")
    ad_service = client.get_service("AdGroupAdService")
    google_ids = {"campaign": campaign_rn, "budget": budget_rn, "ad_groups": {}}

    excluded = [e["item_id"] for e in camp.get("excluded_products", [])]
    groups = camp.get("product_groups", [])
    otros_ids = [i for g in groups if not g.get("all_products")
                 for i in (g.get("item_ids") or [])]

    for g in groups:
        bid_micros = int(round(g["cpc_bid_usd"] * 1_000_000))
        op = client.get_type("AdGroupOperation")
        ag = op.create
        ag.name = g["name"]
        ag.campaign = campaign_rn
        ag.status = enums.AdGroupStatusEnum.ENABLED
        ag.type_ = enums.AdGroupTypeEnum.SHOPPING_PRODUCT_ADS
        ag.cpc_bid_micros = bid_micros
        ag_rn = ag_service.mutate_ad_groups(
            customer_id=CUSTOMER_ID, operations=[op]
        ).results[0].resource_name
        google_ids["ad_groups"][g["name"]] = ag_rn

        if g.get("all_products"):
            # el grupo escoba excluye lo excluido Y lo que ya apuntan otros grupos
            tree = _listing_tree_ops(client, ag_rn, [],
                                     sorted(set(excluded) | set(otros_ids)),
                                     True, bid_micros)
        else:
            ids = [i for i in (g.get("item_ids") or []) if i not in excluded]
            tree = _listing_tree_ops(client, ag_rn, ids, [], False, bid_micros)
        crit_service.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=tree)

        op = client.get_type("AdGroupAdOperation")
        aga = op.create
        aga.ad_group = ag_rn
        aga.status = enums.AdGroupAdStatusEnum.ENABLED
        client.copy_from(aga.ad.shopping_product_ad,
                         client.get_type("ShoppingProductAdInfo"))
        ad_service.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[op])
        n_items = "todo el catálogo" if g.get("all_products") else f"{len(g.get('item_ids') or [])} items"
        print(f"✓ Grupo '{g['name']}': {n_items}, puja ${g['cpc_bid_usd']}")

    # 5) marcar en el gestor
    camp["status"] = "LIVE"
    camp["google"] = google_ids
    camp["pushed_at"] = time.strftime("%Y-%m-%d %H:%M")
    _save_store(store)
    estado = "ENCENDIDA — sirve en cuanto Google apruebe" if launch_on else "EN PAUSADO"
    msg = f"🚀 SUBIDA COMPLETA — campaña Shopping en Google Ads, {estado}."
    print("\n" + msg)
    return msg


def run(target_name: str) -> str:
    """Sube una campaña por nombre. Lanzable desde el panel; lanza RuntimeError si algo bloquea."""
    store = _load_store()
    camp = next((c for c in store["campaigns"] if c["name"] == target_name), None)
    if not camp:
        raise RuntimeError(f"'{target_name}' no está en el gestor")
    if camp.get("status") == "LIVE":
        raise RuntimeError("esa campaña ya está en Google Ads")
    if (camp.get("type") or "").upper() == "SHOPPING":
        return push_shopping(camp, store)
    return push_search(camp, store)


def push_search(camp: dict, store: dict) -> str:
    # pre-flight: validador de reglas duras
    import validate_plan
    issues = [i for i in validate_plan.check({"campaigns": [camp]}) if i[0] == "ERROR"]
    if issues:
        detalle = "; ".join(f"{w}: {m}" for _, w, m in issues)
        raise RuntimeError(f"validador: {detalle}")
    print("✓ Validador pre-vuelo: sin errores")

    client = GoogleAdsClient.load_from_storage(str(BASE / "google-ads.yaml"))
    enums = client.enums

    # 1) presupuesto
    op = client.get_type("CampaignBudgetOperation")
    b = op.create
    b.name = f"{camp['name']} · budget {int(time.time())}"
    b.amount_micros = int(camp["daily_budget_usd"] * 1_000_000)
    b.delivery_method = enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False
    budget_rn = (
        client.get_service("CampaignBudgetService")
        .mutate_campaign_budgets(customer_id=CUSTOMER_ID, operations=[op])
        .results[0].resource_name
    )
    print(f"✓ Presupuesto ${camp['daily_budget_usd']}/día → {budget_rn}")

    # 2) campaña — el switch del gestor decide: enabled=True lanza ENCENDIDA
    launch_on = bool(camp.get("enabled", True))
    op = client.get_type("CampaignOperation")
    c = op.create
    c.name = camp["name"]
    c.status = (
        enums.CampaignStatusEnum.ENABLED if launch_on else enums.CampaignStatusEnum.PAUSED
    )
    c.advertising_channel_type = enums.AdvertisingChannelTypeEnum.SEARCH
    c.campaign_budget = budget_rn
    # Una campaña recién nacida no tiene historial con el que pujar por valor, así
    # que Maximizar clics + tope sigue siendo el arranque correcto. Lo que faltaba
    # era la SALIDA: nada la graduaba nunca, y campañas ya maduras se quedaban
    # comprando clics (Winners el 23-ago: CPC $1,15 en concordancia exacta, 0,3x).
    # El plan puede pedir otra cosa; si no, se arranca barato y se gradúa con
    # cambiar_estrategia_puja cuando haya ≥15 conversiones en 30 días.
    troas_pct = camp.get("target_roas_pct")
    if troas_pct:
        c.maximize_conversion_value.target_roas = float(troas_pct) / 100.0
    else:
        techo = float(camp.get("cpc_ceiling_usd") or 0)
        c.target_spend.cpc_bid_ceiling_micros = (
            int(techo * 1_000_000) if techo else CPC_CEILING_MICROS)
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = False
    c.network_settings.target_partner_search_network = False
    if hasattr(c, "contains_eu_political_advertising"):
        c.contains_eu_political_advertising = (
            enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    campaign_rn = (
        client.get_service("CampaignService")
        .mutate_campaigns(customer_id=CUSTOMER_ID, operations=[op])
        .results[0].resource_name
    )
    print(f"✓ Campaña {'ENCENDIDA' if launch_on else 'PAUSADA'} → {campaign_rn}")

    # 3) geo + idioma + negativas de campaña
    crit_ops = []
    o = client.get_type("CampaignCriterionOperation")
    o.create.campaign = campaign_rn
    o.create.location.geo_target_constant = GEO_US
    crit_ops.append(o)
    o = client.get_type("CampaignCriterionOperation")
    o.create.campaign = campaign_rn
    o.create.language.language_constant = LANG_EN
    crit_ops.append(o)
    # negativas globales (informacionales/competidores) → BROAD;
    # negativas cruzadas (flujo EXACT↔PHRASE) → EXACT, para no matar el descubrimiento
    globales = sorted(set(store.get("negatives", [])))
    cruzadas = sorted(set(camp.get("cross_negatives", [])))
    for n in globales:
        o = client.get_type("CampaignCriterionOperation")
        o.create.campaign = campaign_rn
        o.create.negative = True
        o.create.keyword.text = n
        o.create.keyword.match_type = enums.KeywordMatchTypeEnum.BROAD
        crit_ops.append(o)
    for n in cruzadas:
        o = client.get_type("CampaignCriterionOperation")
        o.create.campaign = campaign_rn
        o.create.negative = True
        o.create.keyword.text = n
        o.create.keyword.match_type = enums.KeywordMatchTypeEnum.EXACT
        crit_ops.append(o)
    client.get_service("CampaignCriterionService").mutate_campaign_criteria(
        customer_id=CUSTOMER_ID, operations=crit_ops
    )
    print(f"✓ Geo EE.UU. + inglés + {len(globales)} negativas BROAD + {len(cruzadas)} cruzadas EXACT")

    # 4) grupos, keywords y anuncios
    ag_service = client.get_service("AdGroupService")
    kw_service = client.get_service("AdGroupCriterionService")
    ad_service = client.get_service("AdGroupAdService")
    google_ids = {"campaign": campaign_rn, "budget": budget_rn, "ad_groups": {}}

    for g in camp.get("ad_groups", []):
        op = client.get_type("AdGroupOperation")
        ag = op.create
        ag.name = g["name"]
        ag.campaign = campaign_rn
        ag.status = enums.AdGroupStatusEnum.ENABLED
        ag.type_ = enums.AdGroupTypeEnum.SEARCH_STANDARD
        ag_rn = ag_service.mutate_ad_groups(
            customer_id=CUSTOMER_ID, operations=[op]
        ).results[0].resource_name
        google_ids["ad_groups"][g["name"]] = ag_rn

        kw_ops = []
        for k in g.get("keywords", []):
            o = client.get_type("AdGroupCriterionOperation")
            o.create.ad_group = ag_rn
            o.create.status = enums.AdGroupCriterionStatusEnum.ENABLED
            o.create.keyword.text = k["text"]
            o.create.keyword.match_type = getattr(
                enums.KeywordMatchTypeEnum, MATCH[k["match"]]
            )
            kw_ops.append(o)
        kw_service.mutate_ad_group_criteria(customer_id=CUSTOMER_ID, operations=kw_ops)

        op = client.get_type("AdGroupAdOperation")
        aga = op.create
        aga.ad_group = ag_rn
        aga.status = enums.AdGroupAdStatusEnum.ENABLED
        aga.ad.final_urls.append(g.get("final_url") or _group_url(g["name"]))
        for h in g.get("headlines", [])[:15]:
            asset = client.get_type("AdTextAsset")
            asset.text = h
            aga.ad.responsive_search_ad.headlines.append(asset)
        for d in g.get("descriptions", [])[:4]:
            asset = client.get_type("AdTextAsset")
            asset.text = d
            aga.ad.responsive_search_ad.descriptions.append(asset)
        ad_service.mutate_ad_group_ads(customer_id=CUSTOMER_ID, operations=[op])
        print(f"✓ Grupo '{g['name']}': {len(g.get('keywords', []))} keywords + 1 RSA")

    # 5) marcar en el gestor
    camp["status"] = "LIVE"
    camp["google"] = google_ids
    camp["pushed_at"] = time.strftime("%Y-%m-%d %H:%M")
    _save_store(store)
    estado = "ENCENDIDA — empieza a servir en cuanto Google apruebe los anuncios" if launch_on \
        else "EN PAUSADO — enciéndela cuando quieras"
    msg = f"🚀 SUBIDA COMPLETA — la campaña está en Google Ads, {estado}."
    print("\n" + msg)
    return msg


def main() -> None:
    if len(sys.argv) < 2:
        die("falta el nombre de la campaña")
    try:
        run(sys.argv[1])
    except RuntimeError as exc:
        die(str(exc))


if __name__ == "__main__":
    try:
        main()
    except GoogleAdsException as ex:
        for e in ex.failure.errors:
            loc = ".".join(p.field_name for p in e.location.field_path_elements) if e.location else ""
            print(f"API ERROR: {e.message} ({loc})", file=sys.stderr)
        sys.exit(1)

"""Feed propio: Shopify → Google Merchant (Merchant API), sin AdNabu.

Sube TODO el catálogo activo con offerId = shopify_ZZ_{producto}_{variante} —
el formato exacto que apuntan los árboles de la campaña Shopping — con precio,
foto, stock real de Shopify y link corregido para los builders.

Uso:
    .venv/bin/python merchant_feed.py           # sync completo
    .venv/bin/python merchant_feed.py --status  # ver estado de la fuente
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

BASE = Path(__file__).parent
ACCOUNT = "5080357407"
API = "https://merchantapi.googleapis.com"
DS_NAME_FILE = BASE / ".merchant_datasource"
SHOPIFY_DOMAIN = "113e43-2.myshopify.com"

# los builders venden por su página propia, no por la página de producto
BUILDER_LINKS = {
    9777050190099: "https://jerseypickles.com/pages/build-you-box",   # Build-your-Box
    10258522177811: "https://jerseypickles.com/pages/build-you-box",  # Build Your Juice Box
}


def _session() -> AuthorizedSession:
    t = json.load(open(BASE / ".merchant_token.json"))
    creds = Credentials(None, refresh_token=t["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=t["client_id"], client_secret=t["client_secret"],
                        scopes=t["scopes"])
    creds.refresh(Request())
    return AuthorizedSession(creds)


def _shopify_products() -> list:
    token = (BASE / ".shopify_token").read_text().strip()
    out, url = [], (f"https://{SHOPIFY_DOMAIN}/admin/api/2025-07/products.json"
                    "?limit=250&status=active")
    while url:
        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
        resp = urllib.request.urlopen(req, timeout=30)
        out.extend(json.loads(resp.read().decode()).get("products", []))
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("Link", "") or "")
        url = nxt.group(1) if nxt else None
    return out


def _ensure_datasource(s: AuthorizedSession) -> str:
    if DS_NAME_FILE.exists():
        return DS_NAME_FILE.read_text().strip()
    r = s.get(f"{API}/datasources/v1/accounts/{ACCOUNT}/dataSources")
    r.raise_for_status()
    for ds in r.json().get("dataSources", []):
        if ds.get("displayName") == "Jersey Pickles · feed propio (API)":
            DS_NAME_FILE.write_text(ds["name"])
            return ds["name"]
    r = s.post(f"{API}/datasources/v1/accounts/{ACCOUNT}/dataSources", json={
        "displayName": "Jersey Pickles · feed propio (API)",
        "primaryProductDataSource": {
            "contentLanguage": "en",
            "feedLabel": "US",
            "countries": ["US"],
        },
    })
    r.raise_for_status()
    name = r.json()["name"]
    DS_NAME_FILE.write_text(name)
    print(f"✓ Fuente de datos creada: {name}")
    return name


def _strip_html(html: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()[:4900]


def build_offers(products: list) -> list:
    offers = []
    for p in products:
        pid = int(p["id"])
        imgs = {i["id"]: i["src"] for i in p.get("images", []) if i.get("src")}
        pimg = (p.get("image") or {}).get("src")
        desc = _strip_html(p.get("body_html")) or p["title"]
        link = BUILDER_LINKS.get(pid)
        for v in p.get("variants", []):
            vid = int(v["id"])
            vt = v.get("title") or ""
            title = p["title"] if vt in ("", "Default Title") else f"{p['title']} - {vt}"
            qty = v.get("inventory_quantity")
            tracked = bool(v.get("inventory_management"))
            in_stock = (not tracked) or (qty or 0) > 0
            attrs = {
                "title": title[:150],
                "description": desc,
                "link": link or f"https://jerseypickles.com/products/{p['handle']}?variant={vid}",
                "imageLink": imgs.get(v.get("image_id")) or pimg,
                "availability": "IN_STOCK" if in_stock else "OUT_OF_STOCK",
                "price": {"amountMicros": str(int(round(float(v["price"]) * 1_000_000))),
                          "currencyCode": "USD"},
                "condition": "NEW",
                "brand": "Jersey Pickles",
            }
            # apparel: Google exige color/talla/edad/género en EE.UU.
            tl = p["title"].lower()
            if any(w in tl for w in ("tee", "t-shirt", "shirt", "hoodie")):
                names = [(o.get("name") or "").lower() for o in p.get("options", [])]
                vals = [v.get("option1"), v.get("option2"), v.get("option3")]
                color = size = None
                for n, val in zip(names, vals):
                    if not val:
                        continue
                    if "color" in n:
                        color = val
                    elif "size" in n or "talla" in n:
                        size = val
                # variantes tipo "Black / M" sin nombres de opción claros
                if not (color or size) and "/" in vt:
                    parts = [x.strip() for x in vt.split("/")]
                    if len(parts) == 2:
                        color, size = parts
                elif not size and vt in ("XS", "S", "M", "L", "XL", "XXL"):
                    size = vt
                attrs["color"] = color or "Black"
                if size:
                    attrs["size"] = size
                attrs["ageGroup"] = "ADULT"
                attrs["gender"] = "UNISEX"
            barcode = (v.get("barcode") or "").strip()
            if re.fullmatch(r"\d{8}|\d{12,14}", barcode):
                attrs["gtins"] = [barcode]
            else:
                attrs["identifierExists"] = False
            if p.get("product_type"):
                attrs["productTypes"] = [p["product_type"]]
            offers.append({
                "offerId": f"shopify_ZZ_{pid}_{vid}",
                "contentLanguage": "en",
                "feedLabel": "US",
                "productAttributes": attrs,
            })
    return offers


def sync() -> None:
    s = _session()
    ds = _ensure_datasource(s)
    products = _shopify_products()
    offers = build_offers(products)
    print(f"{len(products)} productos activos → {len(offers)} ofertas")
    ok = fail = 0
    for o in offers:
        r = s.post(f"{API}/products/v1/accounts/{ACCOUNT}/productInputs:insert",
                   params={"dataSource": ds}, json=o)
        if r.status_code == 200:
            ok += 1
        else:
            fail += 1
            if fail <= 5:
                print(f"  ✗ {o['offerId']}: {r.status_code} {r.text[:160]}")
        if (ok + fail) % 40 == 0:
            print(f"  … {ok + fail}/{len(offers)}")
            time.sleep(1)
    print(f"✓ Subidas {ok} ofertas, {fail} fallos")


def status() -> None:
    s = _session()
    r = s.get(f"{API}/products/v1/accounts/{ACCOUNT}/products", params={"pageSize": 250})
    prods = r.json().get("products", [])
    print(f"productos en Merchant (vista procesada): {len(prods)}")
    from collections import Counter
    cnt = Counter()
    for p in prods[:250]:
        for d in (p.get("productStatus", {}) or {}).get("destinationStatuses", []):
            if d.get("reportingContext") == "SHOPPING_ADS":
                if d.get("approvedCountries"):
                    cnt["aprobados"] += 1
                elif d.get("pendingCountries"):
                    cnt["pendientes"] += 1
                elif d.get("disapprovedCountries"):
                    cnt["desaprobados"] += 1
    print(dict(cnt))


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        sync()

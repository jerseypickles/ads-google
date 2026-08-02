"""Catálogo real de la tienda vía Shopify Admin API (compartido app ↔ fable).

El feed de Merchant usa ids shopify_XX_{producto}_{variante}, así que el
match por variante es exacto — el título solo queda como fallback.
"""

import json
import re
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
SHOPIFY_DOMAIN = "113e43-2.myshopify.com"
SHOPIFY_TOKEN_PATH = BASE / ".shopify_token"
_cache: dict = {"at": 0.0, "data": None}


def shopify_catalog() -> dict:
    """Variante → foto/stock + productos sin stock. Caché 15 min."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < 900:
        return _cache["data"]
    out = {"by_variant": {}, "by_product": {}, "by_title": {}, "sin_stock": []}
    try:
        token = SHOPIFY_TOKEN_PATH.read_text(encoding="utf-8").strip()
        url = (f"https://{SHOPIFY_DOMAIN}/admin/api/2025-07/products.json"
               "?limit=250&status=active,draft,archived")
        while url:
            req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
            resp = urllib.request.urlopen(req, timeout=25)
            for p in json.loads(resp.read().decode("utf-8", "ignore")).get("products", []):
                imgs = {i["id"]: i["src"] for i in p.get("images", []) if i.get("src")}
                pimg = (p.get("image") or {}).get("src")
                key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", p["title"].lower())).strip()
                if pimg and key:
                    out["by_title"].setdefault(key, pimg)
                agotadas, varlist = [], []
                for v in p.get("variants", []):
                    qty = v.get("inventory_quantity")
                    tracked = bool(v.get("inventory_management"))
                    vt = v.get("title") or ""
                    out["by_variant"][int(v["id"])] = dict(
                        img=imgs.get(v.get("image_id")) or pimg,
                        qty=qty if tracked else None, t=vt,
                        pid=int(p["id"]), status=p.get("status"))
                    varlist.append(dict(t=vt, qty=qty if tracked else None))
                    if tracked and (qty or 0) <= 0 and p.get("status") == "active":
                        agotadas.append(vt)
                out["by_product"][int(p["id"])] = dict(
                    title=p["title"], img=pimg, variants=varlist,
                    status=p.get("status"))
                if agotadas:
                    out["sin_stock"].append(dict(title=p["title"], variantes=agotadas, img=pimg))
            link = resp.headers.get("Link", "") or ""
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = nxt.group(1) if nxt else None
        _cache.update(at=now, data=out)
        print(f"[shopify] catálogo: {len(out['by_variant'])} variantes, "
              f"{len(out['sin_stock'])} productos sin stock", flush=True)
    except Exception as exc:
        print(f"[shopify] catálogo no disponible: {exc}", flush=True)
    return out

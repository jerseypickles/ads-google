"""Validador pre-vuelo: reglas duras de Google Ads sobre las campañas del gestor.

Comprueba lo que haría FALLAR la creación por API o la revisión de anuncios:
  - RSA: 3-15 titulares (≤30 car.), 2-4 descripciones (≤90 car.), sin duplicados
  - Política: sin '!' en titulares, sin puntuación repetida, sin MAYÚSCULAS gritadas
  - Keywords: sin caracteres prohibidos (! @ % , * ; etc.), ≤80 car., ≤10 palabras
  - Presupuesto > 0 y flujo de negativas cruzadas presente en la campaña PHRASE

Uso:  .venv/bin/python validate_plan.py
"""

import json
import re
import sys
from pathlib import Path

STORE = Path(__file__).parent / "campaigns_local.json"
KW_FORBIDDEN = set("!@%,*;~^()<>[]{}|?=")
CAPS_OK = {"US", "USA", "NJ", "RSA", "JP"}


def check_shopping(camp: dict) -> list[tuple[str, str, str]]:
    """Reglas duras de una campaña Shopping estándar (entrada del gestor)."""
    issues = []
    name = camp.get("name") or "(sin nombre)"
    b = camp.get("daily_budget_usd") or 0
    if not 5 <= b <= 60:
        issues.append(("ERROR", name, f"presupuesto ${b}/día fuera de rango (5-60)"))
    groups = camp.get("product_groups") or []
    if not groups:
        issues.append(("ERROR", name, "sin product_groups"))
    if sum(1 for g in groups if g.get("all_products")) > 1:
        issues.append(("ERROR", name, "más de un grupo all_products"))
    seen: dict = {}
    for g in groups:
        gname = f"{name} › {g.get('name')}"
        bid = g.get("cpc_bid_usd") or 0
        if not 0.20 <= bid <= 2.00:
            issues.append(("ERROR", gname, f"puja CPC ${bid} fuera de rango (0.20-2.00)"))
        ids = g.get("item_ids") or []
        if not g.get("all_products") and not ids:
            issues.append(("ERROR", gname, "grupo sin all_products y sin item_ids"))
        for iid in ids:
            low = iid.lower()
            if not low.startswith("shopify_"):
                issues.append(("ERROR", gname, f"item_id sospechoso: {iid}"))
            if low in seen:
                issues.append(("ERROR", gname, f"item_id repetido (también en '{seen[low]}'): {iid}"))
            seen[low] = g.get("name")
    return issues


def check(store: dict) -> list[tuple[str, str, str]]:
    issues = []  # (nivel, campaña, mensaje)
    for c in store.get("campaigns", []):
        if (c.get("type") or "").upper() == "SHOPPING":
            issues.extend(check_shopping(c))
            continue
        name = c["name"]
        if not c.get("daily_budget_usd") or c["daily_budget_usd"] <= 0:
            issues.append(("ERROR", name, "presupuesto diario inválido"))
        role = (c.get("match_role") or "").upper()
        if role.startswith("PHRASE") and not c.get("cross_negatives"):
            issues.append(("ERROR", name, "campaña PHRASE sin negativas cruzadas (flujo roto)"))

        for g in c.get("ad_groups", []):
            gname = f"{name} › {g['name']}"
            hs, ds = g.get("headlines", []), g.get("descriptions", [])

            if not 3 <= len(hs) <= 15:
                issues.append(("ERROR", gname, f"{len(hs)} titulares (Google exige 3-15)"))
            if not 2 <= len(ds) <= 4:
                issues.append(("ERROR", gname, f"{len(ds)} descripciones (Google exige 2-4)"))
            if len(set(h.lower() for h in hs)) != len(hs):
                issues.append(("ERROR", gname, "titulares duplicados en el mismo anuncio"))

            for h in hs:
                if len(h) > 30:
                    issues.append(("ERROR", gname, f"titular >30 car.: “{h}” ({len(h)})"))
                if "!" in h:
                    issues.append(("ERROR", gname, f"'!' en titular (política): “{h}”"))
                for w in re.findall(r"[A-Z]{2,}", h):
                    if w not in CAPS_OK:
                        issues.append(("AVISO", gname, f"mayúsculas sostenidas “{w}” en: “{h}”"))
            for d in ds:
                if len(d) > 90:
                    issues.append(("ERROR", gname, f"descripción >90 car. ({len(d)})"))
                if d.count("!") > 1:
                    issues.append(("ERROR", gname, f"más de un '!' en descripción: “{d[:50]}…”"))
                if re.search(r"[!?.]{2,}", d):
                    issues.append(("AVISO", gname, f"puntuación repetida en: “{d[:50]}…”"))

            for k in g.get("keywords", []):
                kw = k["text"]
                bad = KW_FORBIDDEN & set(kw)
                if bad:
                    issues.append(("ERROR", gname, f"keyword con caracteres prohibidos {bad}: “{kw}”"))
                if len(kw) > 80:
                    issues.append(("ERROR", gname, f"keyword >80 car.: “{kw}”"))
                if len(kw.split()) > 10:
                    issues.append(("ERROR", gname, f"keyword >10 palabras: “{kw}”"))
                if k.get("match") not in ("EXACT", "PHRASE", "BROAD"):
                    issues.append(("ERROR", gname, f"concordancia inválida: {k.get('match')}"))
    return issues


def main() -> None:
    if not STORE.exists():
        print("No hay campañas en el gestor.")
        sys.exit(0)
    store = json.loads(STORE.read_text(encoding="utf-8"))
    issues = check(store)
    errors = [i for i in issues if i[0] == "ERROR"]
    warns = [i for i in issues if i[0] == "AVISO"]
    print(f"Campañas: {len(store.get('campaigns', []))} | ERRORES: {len(errors)} | avisos: {len(warns)}\n")
    for lvl, where, msg in issues:
        print(f"[{lvl}] {where}: {msg}")
    if not issues:
        print("✓ TODO LIMPIO — nada bloquearía la creación por API ni la revisión estándar.")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

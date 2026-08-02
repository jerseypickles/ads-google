"""Promociones de Merchant (Merchant API v1): el arma contra el cuello del checkout.

La promo aparece como insignia en los anuncios de Shopping ("Free shipping over $X")
— sube CTR y ataca el shock del costo de envío ANTES del clic.

REGLA DE ORO: la promoción debe ser real en la tienda. Antes de activarla, el
umbral tiene que existir como regla de envío gratis en Shopify. Google verifica.

Uso:
    .venv/bin/python merchant_promo.py envio-gratis 59        # crear (umbral $59, 90 días)
    .venv/bin/python merchant_promo.py envio-gratis 59 180    # con duración en días
    .venv/bin/python merchant_promo.py --list                 # ver promociones y estado
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

BASE = Path(__file__).parent
ACCOUNT = "5080357407"
API = "https://merchantapi.googleapis.com"
DS_FILE = BASE / ".merchant_promo_ds"


def _session() -> AuthorizedSession:
    t = json.load(open(BASE / ".merchant_token.json"))
    creds = Credentials(None, refresh_token=t["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=t["client_id"], client_secret=t["client_secret"],
                        scopes=t["scopes"])
    creds.refresh(Request())
    return AuthorizedSession(creds)


def _ensure_ds(s: AuthorizedSession) -> str:
    if DS_FILE.exists():
        return DS_FILE.read_text().strip()
    r = s.get(f"{API}/datasources/v1/accounts/{ACCOUNT}/dataSources")
    r.raise_for_status()
    for ds in r.json().get("dataSources", []):
        if ds.get("displayName") == "Jersey Pickles · promociones (API)":
            DS_FILE.write_text(ds["name"])
            return ds["name"]
    r = s.post(f"{API}/datasources/v1/accounts/{ACCOUNT}/dataSources", json={
        "displayName": "Jersey Pickles · promociones (API)",
        "promotionDataSource": {"targetCountry": "US", "contentLanguage": "en"},
    })
    r.raise_for_status()
    name = r.json()["name"]
    DS_FILE.write_text(name)
    print(f"✓ Fuente de promociones creada: {name}")
    return name


def envio_gratis(umbral_usd: float, dias: int = 90) -> None:
    s = _session()
    ds = _ensure_ds(s)
    inicio = datetime.now(timezone.utc)
    fin = inicio + timedelta(days=min(dias, 180))  # Google limita a ~6 meses
    promo_id = f"envio-gratis-{int(umbral_usd)}"
    body = {
        "dataSource": ds,
        "promotion": {
            "promotionId": promo_id,
            "contentLanguage": "en",
            "targetCountry": "US",
            "redemptionChannel": ["ONLINE"],
            "attributes": {
                "longTitle": f"Free standard shipping on orders over ${int(umbral_usd)}",
                "offerType": "NO_CODE",
                "couponValueType": "FREE_SHIPPING_STANDARD",
                "minimumPurchaseAmount": {
                    "amountMicros": str(int(umbral_usd * 1_000_000)),
                    "currencyCode": "USD",
                },
                "productApplicability": "ALL_PRODUCTS",
                "promotionDestinations": ["SHOPPING_ADS", "FREE_LISTINGS"],
                "promotionEffectiveTimePeriod": {
                    "startTime": inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": fin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            },
        },
    }
    r = s.post(f"{API}/promotions/v1/accounts/{ACCOUNT}/promotions:insert", json=body)
    if r.status_code != 200:
        print(f"✗ error {r.status_code}: {r.text[:400]}")
        sys.exit(1)
    print(f"✓ Promoción '{promo_id}' enviada — Google la revisa (horas-2 días).")
    print("  RECUERDA: el envío gratis sobre ese umbral debe estar configurado en Shopify.")


def listar() -> None:
    s = _session()
    r = s.get(f"{API}/promotions/v1/accounts/{ACCOUNT}/promotions", params={"pageSize": 50})
    promos = r.json().get("promotions", [])
    if not promos:
        print("Sin promociones.")
        return
    for p in promos:
        st = p.get("promotionStatus", {})
        dest = "; ".join(f"{d.get('reportingContext')}: {d.get('status')}"
                         for d in st.get("destinationStatuses", []))
        issues = "; ".join(i.get("detail", "") for i in st.get("itemLevelIssues", [])[:2])
        print(f"· {p.get('promotionId')} | {p.get('attributes', {}).get('longTitle', '')[:50]}")
        print(f"  {dest or 'procesando'}{(' | ' + issues) if issues else ''}")


if __name__ == "__main__":
    if "--list" in sys.argv:
        listar()
    elif len(sys.argv) >= 3 and sys.argv[1] == "envio-gratis":
        envio_gratis(float(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 90)
    else:
        print(__doc__)

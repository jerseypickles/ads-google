"""Capa de datos MongoDB Atlas para Jersey Pickles Ads.

Base: `googleads`. Colecciones:
  - manager           documento único con el gestor de campañas (espejo)
  - plans             histórico de planes de Fable (uno por generación)
  - watches           histórico del vigía de keywords
  - reviews           histórico de lecturas/auditorías de campañas
  - lessons           memoria de Fable (una lección por documento)
  - pulse_snapshots   serie temporal: foto del pulso en vivo (~1/min con campañas activas)
  - campaign_daily    métricas diarias por campaña (upsert por campaña+fecha)
  - keyword_snapshots keywords validadas por fecha de investigación
  - search_terms      términos de búsqueda reales por fecha

Degradación elegante: si Atlas no responde, todo sigue funcionando con los
archivos locales; se reintenta cada 5 minutos.
"""

import os
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
URI_PATH = BASE / ".mongo_uri"
DB_NAME = "googleads"

_client = None
_last_fail = 0.0


def get_db():
    """Devuelve la base o None si Atlas no está disponible (reintento cada 5 min)."""
    global _client, _last_fail
    if _client is not None:
        return _client[DB_NAME]
    if time.time() - _last_fail < 300:
        return None
    try:
        from pymongo import MongoClient

        uri = os.environ.get("MONGO_URI") or URI_PATH.read_text(encoding="utf-8").strip()
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        _client = client
        return _client[DB_NAME]
    except Exception as exc:
        print(f"[mongo] sin conexión ({exc.__class__.__name__}) — usando archivos locales", flush=True)
        _last_fail = time.time()
        return None


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save(collection: str, doc: dict) -> bool:
    """Inserta un documento con timestamp. True si llegó a Mongo."""
    db = get_db()
    if db is None:
        return False
    try:
        db[collection].insert_one({**doc, "ts": doc.get("ts") or _ts()})
        return True
    except Exception as exc:
        print(f"[mongo] error insertando en {collection}: {exc}", flush=True)
        return False


def upsert(collection: str, filt: dict, doc: dict) -> bool:
    db = get_db()
    if db is None:
        return False
    try:
        db[collection].update_one(filt, {"$set": {**doc, "updated_ts": _ts()}}, upsert=True)
        return True
    except Exception as exc:
        print(f"[mongo] error upsert en {collection}: {exc}", flush=True)
        return False


def upsert_many(collection: str, key_fields: list[str], docs: list[dict]) -> int:
    """Upsert masivo con clave compuesta. Devuelve cuántos llegaron."""
    db = get_db()
    if db is None or not docs:
        return 0
    try:
        from pymongo import UpdateOne

        ops = [
            UpdateOne(
                {k: d[k] for k in key_fields},
                {"$set": {**d, "updated_ts": _ts()}},
                upsert=True,
            )
            for d in docs
        ]
        result = db[collection].bulk_write(ops, ordered=False)
        return result.upserted_count + result.modified_count + result.matched_count
    except Exception as exc:
        print(f"[mongo] error bulk en {collection}: {exc}", flush=True)
        return 0


def get_doc(collection: str, filt: dict) -> dict | None:
    """Un documento (sin _id) o None."""
    db = get_db()
    if db is None:
        return None
    try:
        return db[collection].find_one(filt, {"_id": 0})
    except Exception:
        return None


def latest(collection: str, filt: dict | None = None) -> dict | None:
    """El documento más reciente de la colección (sin _id) o None."""
    db = get_db()
    if db is None:
        return None
    try:
        return db[collection].find_one(filt or {}, {"_id": 0}, sort=[("_id", -1)])
    except Exception:
        return None


def all_docs(collection: str, limit: int = 100) -> list[dict]:
    db = get_db()
    if db is None:
        return []
    try:
        return list(db[collection].find({}, {"_id": 0}).sort("_id", 1).limit(limit))
    except Exception:
        return []


def mirror_csvs() -> dict:
    """Sube los CSVs más recientes como datos estructurados (idempotente)."""
    import csv
    import glob

    counts = {}

    def newest(pattern):
        files = sorted(glob.glob(str(BASE / pattern)))
        return files[-1] if files else None

    f = newest("report_campanas_*.csv")
    if f:
        rows = []
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append(dict(
                    campana=r["campana"], fecha=r["fecha"], estado=r["estado"],
                    impresiones=int(r["impresiones"]), clics=int(r["clics"]),
                    coste_usd=float(r["coste_usd"]), conversiones=float(r["conversiones"]),
                ))
        counts["campaign_daily"] = upsert_many("campaign_daily", ["campana", "fecha"], rows)

    f = newest("historical_metrics_*.csv")
    if f:
        snap_date = f.rsplit("_", 1)[-1].replace(".csv", "")
        rows = []
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append(dict(
                    snapshot=snap_date, keyword=r["keyword"],
                    vol_promedio=int(r["busquedas_mensuales_promedio"] or 0),
                    competencia=r["competencia"],
                    puja_baja=float(r["puja_top_baja_usd"]), puja_alta=float(r["puja_top_alta_usd"]),
                    volumen_mensual=r["volumen_por_mes_12m"],
                ))
        counts["keyword_snapshots"] = upsert_many("keyword_snapshots", ["snapshot", "keyword"], rows)

    f = newest("search_terms_*.csv")
    if f:
        snap_date = f.rsplit("_", 1)[-1].replace(".csv", "")
        rows = []
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                rows.append(dict(
                    snapshot=snap_date, termino=r["termino_de_busqueda"],
                    campana=r["campana"], grupo=r["grupo_de_anuncios"],
                    impresiones=int(r["impresiones"]), clics=int(r["clics"]),
                    coste_usd=float(r["coste_usd"]), conversiones=float(r["conversiones"]),
                ))
        counts["search_terms"] = upsert_many("search_terms", ["snapshot", "termino", "campana"], rows)

    return counts

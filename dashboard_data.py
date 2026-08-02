"""Construye los datos del dashboard a partir de los CSV más recientes."""

import csv
import glob
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent

EXCLUDE = [
    "comic", "pub", "claussen", "rugrats", "chamoy", "walmart", "grillo",
    "vlasic", "in spanish", "song", "movie", "cartoon", "meme", "jellycat",
    "picklesburgh", "kool aid",
]
SEASONAL_PICK = [
    "pickles refrigerated", "pickles cucumber",
    "fermented pickles", "bread and butter pickles",
]


def _newest(pattern: str) -> str | None:
    files = sorted(glob.glob(str(BASE / pattern)))
    return files[-1] if files else None


def build_data() -> dict:
    out = {}

    # Campañas: agregado y por mes
    camp = defaultdict(lambda: dict(impr=0, clicks=0, cost=0.0, conv=0.0))
    monthly = defaultdict(lambda: dict(cost=0.0, conv=0.0, clicks=0))
    with open(_newest("report_campanas_*.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            c = camp[r["campana"]]
            c["impr"] += int(r["impresiones"])
            c["clicks"] += int(r["clics"])
            c["cost"] += float(r["coste_usd"])
            c["conv"] += float(r["conversiones"])
            m = monthly[r["fecha"][:7]]
            m["cost"] += float(r["coste_usd"])
            m["conv"] += float(r["conversiones"])
            m["clicks"] += int(r["clics"])
    out["campaigns"] = [
        dict(
            name=k, impr=v["impr"], clicks=v["clicks"],
            cost=round(v["cost"], 2), conv=round(v["conv"], 2),
            cpa=round(v["cost"] / v["conv"], 2) if v["conv"] else None,
        )
        for k, v in sorted(camp.items(), key=lambda x: -x[1]["cost"])
    ]
    out["monthly"] = [
        dict(month=m, cost=round(v["cost"], 2), conv=round(v["conv"], 1), clicks=v["clicks"])
        for m, v in sorted(monthly.items())
    ]

    # Keywords validadas
    rows = []
    n_validated = 0
    with open(_newest("historical_metrics_*.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            n_validated += 1
            kw = r["keyword"]
            if any(b in kw for b in EXCLUDE):
                continue
            vol = int(r["busquedas_mensuales_promedio"] or 0)
            if vol < 500:
                continue
            series = sorted(
                [p.rsplit(":", 1)[0], int(p.rsplit(":", 1)[1])]
                for p in r["volumen_por_mes_12m"].split("; ")
                if ":" in p
            )
            vals = [v for _, v in series]
            vol_last = vals[-1] if vals else 0
            mom3 = None
            if len(vals) >= 6:
                prev = sum(vals[-6:-3]) / 3
                rec = sum(vals[-3:]) / 3
                mom3 = round((rec / prev - 1) * 100) if prev else None
            rows.append(dict(
                kw=kw, vol=vol, vol_last=vol_last, mom3=mom3,
                comp=r["competencia"],
                low=float(r["puja_top_baja_usd"]), high=float(r["puja_top_alta_usd"]),
                series=series,
            ))
    rows.sort(key=lambda x: -x["vol_last"])
    out["keywords"] = rows[:30]
    out["last_month"] = rows[0]["series"][-1][0] if rows and rows[0]["series"] else None
    out["seasonal"] = [r for r in rows if r["kw"] in SEASONAL_PICK]

    # Search terms
    st = []
    with open(_newest("search_terms_*.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            st.append(dict(
                term=r["termino_de_busqueda"], impr=int(r["impresiones"]),
                clicks=int(r["clics"]), cost=float(r["coste_usd"]),
                conv=float(r["conversiones"]),
            ))
    st.sort(key=lambda x: -x["cost"])
    out["search_terms"] = st[:10]

    # keywords ya en campañas (para el switch de la tabla de oportunidades)
    campaign_kws = set()
    camps_path = BASE / "campaigns_local.json"
    if camps_path.exists():
        import json as _json
        store = _json.loads(camps_path.read_text(encoding="utf-8"))
        for c in store.get("campaigns", []):
            for g in c.get("ad_groups", []):
                campaign_kws.update(k["text"].lower() for k in g.get("keywords", []))
    out["campaign_kws"] = sorted(campaign_kws)

    # exploración: candidatas de la última cosecha aún SIN validar con la API
    known = set()
    hist = _newest("historical_metrics_*.csv")
    if hist:
        with open(hist, newline="", encoding="utf-8") as f:
            known = {r["keyword"] for r in csv.DictReader(f)}
    exploration = []
    seeds_csv = _newest("seed_candidates_*.csv")
    if seeds_csv:
        with open(seeds_csv, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                kw = r["keyword_candidata"].strip().lower()
                if kw and kw not in known and not any(b in kw for b in EXCLUDE):
                    exploration.append(dict(kw=kw, origen=r.get("query_origen", "")))
    out["exploration"] = exploration[:60]

    # KPIs
    tot_cost = sum(c["cost"] for c in out["campaigns"])
    tot_conv = sum(c["conv"] for c in out["campaigns"])
    ideas_file = _newest("keyword_ideas_*.csv")
    n_ideas = sum(1 for _ in open(ideas_file, encoding="utf-8")) - 1 if ideas_file else 0
    out["kpi"] = dict(
        cost=round(tot_cost, 2), conv=round(tot_conv, 1),
        clicks=sum(c["clicks"] for c in out["campaigns"]),
        cpa=round(tot_cost / tot_conv, 2) if tot_conv else 0,
        ideas=n_ideas, validated=n_validated,
    )
    return out

"""Auditoría de calidad de datos — Capa 2 (framework DAMA).

Mientras la Capa 1 (dentro del cleaner) responde "¿la corrida de hoy terminó
bien y los números cierran?", esta Capa 2 responde una pregunta distinta:
"¿el dataset publicado es defendible para análisis?".

Aplica 12 reglas repartidas en las 6 dimensiones DAMA, calcula un score 0-100
por dimensión y global, y emite un veredicto SALUDABLE / DRIFT / CRÍTICO,
comparando además contra un baseline para detectar deterioro relativo.

Dimensiones evaluadas:
    Completeness   — cobertura de datos en campos clave.
    Validity       — valores dentro de dominios y rangos válidos.
    Reasonability  — coherencia con la lógica del negocio.
    Uniqueness     — ausencia de duplicados por clave.
    Consistency    — integridad referencial entre tablas.
    Currency       — frescura del dato publicado.

Códigos de salida (impiden consumir aguas abajo un dataset degradado):
    0  SALUDABLE
    1  DRIFT
    2  CRÍTICO

Uso:
    python -m src.auditoria_calidad
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("auditoria")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
ESTADOS_ORDEN = {"pendiente", "aprobada", "recibida", "anulada"}
BASELINE_PATH = config.RAIZ / "baseline.json"


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════
def _num(serie: pd.Series) -> pd.Series:
    return pd.to_numeric(serie, errors="coerce")


def _resultado(ok: bool, valor: Any, criterio: str, evidencia: str = "") -> dict:
    return {"resultado": PASS if ok else FAIL, "valor_observado": valor,
            "criterio": criterio, "evidencia": evidencia}


def _skip(criterio: str, motivo: str) -> dict:
    return {"resultado": SKIP, "valor_observado": None, "criterio": criterio, "evidencia": motivo}


# ═════════════════════════════════════════════════════════════════════════════
# Reglas (una función por regla; reciben el contexto con los DataFrames)
# ═════════════════════════════════════════════════════════════════════════════
def regla_c1(ctx: dict) -> dict:
    df = ctx["ordenes"]
    crit = "≥ 99% de órdenes con id_proveedor informado"
    if df.empty:
        return _skip(crit, "sin datos de órdenes")
    pct = 100.0 * df["id_proveedor"].astype(str).str.strip().ne("").mean()
    return _resultado(pct >= 99, round(pct, 2), crit, f"{pct:.2f}% informado")


def regla_c2(ctx: dict) -> dict:
    df = ctx["transito"]
    crit = "≥ 99% de envíos con fecha_despacho informada"
    if df.empty:
        return _skip(crit, "sin datos de tránsito")
    pct = 100.0 * df["fecha_despacho"].astype(str).str.strip().ne("").mean()
    return _resultado(pct >= 99, round(pct, 2), crit, f"{pct:.2f}% informado")


def regla_v1(ctx: dict) -> dict:
    df = ctx["ordenes"]
    crit = "100% de estados de orden dentro del dominio válido"
    if df.empty:
        return _skip(crit, "sin datos de órdenes")
    fuera = sorted(set(df["estado"]) - ESTADOS_ORDEN)
    pct = 100.0 * df["estado"].isin(ESTADOS_ORDEN).mean()
    return _resultado(not fuera, round(pct, 2), crit, f"fuera de dominio: {fuera}")


def regla_v2(ctx: dict) -> dict:
    df = ctx["ordenes"]
    crit = "≥ 99% de órdenes no anuladas con monto_total_usd > 0"
    activas = df[df["estado"] != "anulada"]
    if activas.empty:
        return _skip(crit, "sin órdenes activas")
    pct = 100.0 * (_num(activas["monto_total_usd"]) > 0).mean()
    return _resultado(pct >= 99, round(pct, 2), crit, f"{pct:.2f}% con monto positivo")


def regla_v3(ctx: dict) -> dict:
    df = ctx["transito"]
    crit = "100% de montos de tránsito (FOB/flete/seguro) no negativos"
    if df.empty:
        return _skip(crit, "sin datos de tránsito")
    cols = ["valor_fob_usd", "flete_usd", "seguro_usd"]
    negativos = sum(int((_num(df[c]) < 0).sum()) for c in cols)
    return _resultado(negativos == 0, negativos, crit, f"{negativos} valores negativos")


def regla_r1(ctx: dict) -> dict:
    df = ctx["transito"]
    crit = "≥ 95% de lead times (entregados) entre 1 y 365 días"
    entregados = df[(df["estado_transito"] == "entregado") &
                    df["fecha_arribo_real"].astype(str).str.strip().ne("")]
    if entregados.empty:
        return _skip(crit, "sin envíos entregados con arribo real")
    desp = pd.to_datetime(entregados["fecha_despacho"], errors="coerce")
    arr = pd.to_datetime(entregados["fecha_arribo_real"], errors="coerce")
    lead = (arr - desp).dt.days
    en_rango = lead.between(1, 365)
    pct = 100.0 * en_rango.mean()
    return _resultado(pct >= 95, round(pct, 2), crit, f"{pct:.2f}% en rango")


def regla_r2(ctx: dict) -> dict:
    df = ctx["ordenes"]
    crit = "≥ 99% de líneas con monto_total ≈ cantidad × precio (±1%)"
    if df.empty:
        return _skip(crit, "sin datos de órdenes")
    monto = _num(df["monto_total_usd"])
    esperado = _num(df["cantidad"]) * _num(df["precio_unitario_usd"])
    valido = monto > 0
    if valido.sum() == 0:
        return _skip(crit, "sin montos positivos")
    error_rel = (monto - esperado).abs() / monto.where(monto != 0)
    coherentes = (error_rel <= 0.01)[valido]
    pct = 100.0 * coherentes.mean()
    return _resultado(pct >= 99, round(pct, 2), crit, f"{pct:.2f}% coherentes")


def regla_u1(ctx: dict) -> dict:
    df = ctx["ordenes"]
    crit = "clave (id_orden, linea) única en hechos_ordenes"
    if df.empty:
        return _skip(crit, "sin datos de órdenes")
    dups = int(df.duplicated(subset=["id_orden", "linea"]).sum())
    return _resultado(dups == 0, dups, crit, f"{dups} duplicados de clave")


def regla_u2(ctx: dict) -> dict:
    df = ctx["dim_proveedor"]
    crit = "id_proveedor único en dim_proveedor"
    if df.empty:
        return _skip(crit, "sin datos de proveedores")
    dups = int(df.duplicated(subset=["id_proveedor"]).sum())
    return _resultado(dups == 0, dups, crit, f"{dups} proveedores duplicados")


def regla_k1(ctx: dict) -> dict:
    ordenes, prov = ctx["ordenes"], ctx["dim_proveedor"]
    crit = "≥ 98% de órdenes con id_proveedor existente en dim_proveedor"
    if ordenes.empty or prov.empty:
        return _skip(crit, "faltan órdenes o proveedores")
    validos = set(prov["id_proveedor"])
    pct = 100.0 * ordenes["id_proveedor"].isin(validos).mean()
    return _resultado(pct >= 98, round(pct, 2), crit, f"{pct:.2f}% con FK válida")


def regla_k2(ctx: dict) -> dict:
    transito, ordenes = ctx["transito"], ctx["ordenes"]
    crit = "≥ 95% de envíos con id_orden existente en hechos_ordenes"
    if transito.empty or ordenes.empty:
        return _skip(crit, "faltan tránsito u órdenes")
    validos = set(ordenes["id_orden"])
    pct = 100.0 * transito["id_orden"].isin(validos).mean()
    return _resultado(pct >= 95, round(pct, 2), crit, f"{pct:.2f}% con FK válida")


def regla_t1(ctx: dict) -> dict:
    crit = f"snapshot fresco: dato más reciente dentro de {config.DIAS_FRESCURA} días"
    fechas = []
    if not ctx["ordenes"].empty:
        fechas.append(pd.to_datetime(ctx["ordenes"]["fecha_emision"], errors="coerce").max())
    if not ctx["transito"].empty:
        fechas.append(pd.to_datetime(ctx["transito"]["fecha_despacho"], errors="coerce").max())
    fechas = [f for f in fechas if pd.notna(f)]
    if not fechas:
        return _skip(crit, "sin fechas evaluables")
    mas_reciente = max(fechas).date()
    dias = (date.today() - mas_reciente).days
    return _resultado(dias <= config.DIAS_FRESCURA, dias, crit, f"hace {dias} días ({mas_reciente})")


# (id, dimensión, descripción, severidad, peso, función)
REGLAS: list[tuple[str, str, str, str, float, Callable[[dict], dict]]] = [
    ("CMP-1", "Completeness", "Cobertura de id_proveedor en órdenes", "alta", 1.0, regla_c1),
    ("CMP-2", "Completeness", "Cobertura de fecha_despacho en tránsito", "alta", 1.0, regla_c2),
    ("VAL-1", "Validity", "Estados de orden dentro de dominio", "alta", 1.0, regla_v1),
    ("VAL-2", "Validity", "Montos positivos en órdenes activas", "media", 1.0, regla_v2),
    ("VAL-3", "Validity", "Montos de tránsito no negativos", "alta", 1.0, regla_v3),
    ("RZN-1", "Reasonability", "Lead time de entrega en rango plausible", "media", 1.0, regla_r1),
    ("RZN-2", "Reasonability", "Coherencia aritmética monto = cantidad×precio", "media", 1.0, regla_r2),
    ("UNQ-1", "Uniqueness", "Unicidad de clave de orden", "alta", 1.0, regla_u1),
    ("UNQ-2", "Uniqueness", "Unicidad de proveedor", "alta", 1.0, regla_u2),
    ("CNS-1", "Consistency", "Integridad referencial orden→proveedor", "alta", 1.0, regla_k1),
    ("CNS-2", "Consistency", "Integridad referencial tránsito→orden", "media", 1.0, regla_k2),
    ("CUR-1", "Currency", "Frescura del snapshot publicado", "media", 1.0, regla_t1),
]
DIMENSIONES = ["Completeness", "Validity", "Reasonability", "Uniqueness", "Consistency", "Currency"]


# ═════════════════════════════════════════════════════════════════════════════
# Evaluación y scoring (puro y testeable)
# ═════════════════════════════════════════════════════════════════════════════
def evaluar(ctx: dict) -> list[dict]:
    """Corre las 12 reglas y devuelve la lista de resultados enriquecidos."""
    resultados = []
    for rid, dim, desc, sev, peso, fn in REGLAS:
        try:
            base = fn(ctx)
        except Exception as exc:  # noqa: BLE001 — una regla que falla no tumba la auditoría
            base = _skip("error de evaluación", str(exc))
        resultados.append({"id": rid, "dimension": dim, "descripcion": desc,
                           "severidad": sev, "peso": peso, **base})
    return resultados


def calcular_scores(resultados: list[dict]) -> dict[str, Any]:
    """Score 0-100 por dimensión y global, ponderado por peso (ignora SKIP)."""
    scores: dict[str, Any] = {}
    for dim in DIMENSIONES:
        evaluadas = [r for r in resultados if r["dimension"] == dim and r["resultado"] != SKIP]
        peso_total = sum(r["peso"] for r in evaluadas)
        if peso_total == 0:
            scores[dim] = None  # dimensión no evaluada en esta corrida
            continue
        peso_ok = sum(r["peso"] for r in evaluadas if r["resultado"] == PASS)
        scores[dim] = round(100.0 * peso_ok / peso_total, 2)
    globales = [r for r in resultados if r["resultado"] != SKIP]
    peso_total = sum(r["peso"] for r in globales)
    peso_ok = sum(r["peso"] for r in globales if r["resultado"] == PASS)
    scores["global"] = round(100.0 * peso_ok / peso_total, 2) if peso_total else None
    return scores


def clasificar_veredicto(score_global: float | None) -> str:
    """Traduce el score global a una banda categórica."""
    if score_global is None:
        return "NO_EVALUADO"
    if score_global >= config.UMBRAL_SALUDABLE:
        return "SALUDABLE"
    if score_global >= config.UMBRAL_DRIFT:
        return "DRIFT"
    return "CRÍTICO"


def ajustar_por_baseline(veredicto: str, score_global: float | None, baseline: dict) -> tuple[str, float | None]:
    """Degrada SALUDABLE a DRIFT si cayó frente al baseline más que la tolerancia."""
    base_global = baseline.get("scores", {}).get("global") if baseline else None
    if score_global is None or base_global is None:
        return veredicto, None
    delta = round(score_global - base_global, 2)
    if veredicto == "SALUDABLE" and delta <= -config.TOLERANCIA_DRIFT_BASELINE:
        return "DRIFT", delta
    return veredicto, delta


# ═════════════════════════════════════════════════════════════════════════════
# Carga de datos y orquestación
# ═════════════════════════════════════════════════════════════════════════════
def _leer(nombre: str) -> pd.DataFrame:
    ruta = config.PROCESSED / f"{nombre}.csv"
    if ruta.exists():
        return pd.read_csv(ruta, dtype=str, keep_default_na=False)
    return pd.DataFrame()


def cargar_contexto() -> dict:
    return {
        "ordenes": _leer("hechos_ordenes"),
        "transito": _leer("hechos_transito"),
        "dim_proveedor": _leer("dim_proveedor"),
        "dim_pais": _leer("dim_pais"),
        "dim_calendario": _leer("dim_calendario"),
    }


def _cargar_baseline() -> dict:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {}


def auditar() -> tuple[int, dict]:
    """Corre la auditoría completa. Devuelve (exit_code, informe)."""
    ctx = cargar_contexto()
    if ctx["ordenes"].empty and ctx["transito"].empty:
        logger.error("No hay datos en processed/. Corré primero el cleaner.")
        return 2, {"veredicto": "NO_EVALUADO", "error": "processed/ vacío"}

    resultados = evaluar(ctx)
    scores = calcular_scores(resultados)
    veredicto = clasificar_veredicto(scores["global"])
    baseline = _cargar_baseline()
    veredicto, delta = ajustar_por_baseline(veredicto, scores["global"], baseline)

    exit_code = {"SALUDABLE": 0, "DRIFT": 1, "CRÍTICO": 2}.get(veredicto, 2)
    informe = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "veredicto": veredicto,
        "score_global": scores["global"],
        "delta_vs_baseline": delta,
        "scores_por_dimension": {d: scores[d] for d in DIMENSIONES},
        "reglas": resultados,
        "exit_code": exit_code,
    }
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    ruta = config.QUALITY_REPORTS / f"auditoria_dama_{ts}.json"
    config.QUALITY_REPORTS.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    _imprimir_resumen(informe)
    logger.info("Informe de auditoría: %s", ruta)
    return exit_code, informe


def _imprimir_resumen(informe: dict) -> None:
    print("\n" + "═" * 64)
    print(f"  AUDITORÍA DAMA — veredicto: {informe['veredicto']}  "
          f"(score global {informe['score_global']})")
    print("═" * 64)
    for dim, score in informe["scores_por_dimension"].items():
        print(f"  {dim:<16} {score if score is not None else 'n/e'}")
    print("─" * 64)
    for r in informe["reglas"]:
        marca = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}[r["resultado"]]
        print(f"  {marca} {r['id']:<6} {r['descripcion']:<46} {r['resultado']}")
    print("═" * 64 + "\n")


def main() -> None:
    exit_code, _ = auditar()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

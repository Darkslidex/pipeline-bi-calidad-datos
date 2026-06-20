"""Pruebas de la auditoría DAMA: scoring, veredicto, baseline y reglas."""

import pandas as pd

from src.auditoria_calidad import (
    PASS,
    FAIL,
    SKIP,
    ajustar_por_baseline,
    calcular_scores,
    clasificar_veredicto,
    regla_u1,
)


def _resultado(rid, dim, res, peso=1.0):
    return {"id": rid, "dimension": dim, "descripcion": rid,
            "severidad": "alta", "peso": peso, "resultado": res,
            "valor_observado": None, "criterio": "", "evidencia": ""}


def test_calcular_scores_todo_pass_da_100():
    resultados = [
        _resultado("CMP-1", "Completeness", PASS),
        _resultado("CMP-2", "Completeness", PASS),
        _resultado("VAL-1", "Validity", PASS),
    ]
    scores = calcular_scores(resultados)
    assert scores["Completeness"] == 100.0
    assert scores["Validity"] == 100.0
    assert scores["global"] == 100.0


def test_calcular_scores_con_fail_baja_la_dimension():
    resultados = [
        _resultado("CMP-1", "Completeness", PASS),
        _resultado("CMP-2", "Completeness", FAIL),
    ]
    scores = calcular_scores(resultados)
    assert scores["Completeness"] == 50.0


def test_calcular_scores_ignora_skip():
    # Una regla SKIP no debe contar como fallo ni bajar el score.
    resultados = [
        _resultado("VAL-1", "Validity", PASS),
        _resultado("VAL-2", "Validity", SKIP),
    ]
    scores = calcular_scores(resultados)
    assert scores["Validity"] == 100.0


def test_clasificar_veredicto_por_bandas():
    assert clasificar_veredicto(95.0) == "SALUDABLE"
    assert clasificar_veredicto(80.0) == "DRIFT"
    assert clasificar_veredicto(50.0) == "CRÍTICO"
    assert clasificar_veredicto(None) == "NO_EVALUADO"


def test_ajustar_por_baseline_degrada_a_drift():
    baseline = {"scores": {"global": 100.0}}
    # Caída de 6 puntos supera la tolerancia (5) -> degrada SALUDABLE a DRIFT.
    veredicto, delta = ajustar_por_baseline("SALUDABLE", 94.0, baseline)
    assert veredicto == "DRIFT"
    assert delta == -6.0


def test_ajustar_por_baseline_mantiene_si_caida_menor():
    baseline = {"scores": {"global": 100.0}}
    veredicto, delta = ajustar_por_baseline("SALUDABLE", 98.0, baseline)
    assert veredicto == "SALUDABLE"
    assert delta == -2.0


def test_regla_u1_detecta_duplicado_de_clave():
    con_dup = {"ordenes": pd.DataFrame(
        {"id_orden": ["OC-1", "OC-1"], "linea": ["1", "1"]}
    )}
    assert regla_u1(con_dup)["resultado"] == FAIL

    sin_dup = {"ordenes": pd.DataFrame(
        {"id_orden": ["OC-1", "OC-1"], "linea": ["1", "2"]}
    )}
    assert regla_u1(sin_dup)["resultado"] == PASS

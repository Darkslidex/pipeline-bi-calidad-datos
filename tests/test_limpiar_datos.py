"""Pruebas del cleaner: normalización, contrato, dedup, modo de carga y validación."""

from datetime import date

import pandas as pd
import pytest

from src.limpiar_datos import (
    aplicar_aliases,
    aplicar_modo,
    deduplicar,
    limpiar_ordenes,
    normalizar_estado,
    normalizar_fecha,
    normalizar_numero,
    validar_contrato,
    validar_filas,
)
from src.modelos import OrdenLimpia


@pytest.mark.parametrize(
    "crudo,esperado",
    [
        ("12,345.67", 12345.67),   # anglosajón con separador de miles
        ("1,000", 1000.0),          # miles sin decimales
        ("12345,67", 12345.67),     # decimal europeo
        ("$ 1,234.50", 1234.50),    # con símbolo y espacio
        ("", None),                  # vacío
        ("N/D", None),               # token nulo
    ],
)
def test_normalizar_numero_formatos_mezclados(crudo, esperado):
    assert normalizar_numero(crudo) == esperado


@pytest.mark.parametrize(
    "crudo",
    ["2026-06-19", "19/06/2026", "19-06-2026", "2026/06/19"],
)
def test_normalizar_fecha_acepta_formatos_mezclados(crudo):
    assert normalizar_fecha(crudo) == date(2026, 6, 19)


def test_normalizar_fecha_invalida_devuelve_none():
    assert normalizar_fecha("N/D") is None
    assert normalizar_fecha("no es fecha") is None


def test_normalizar_estado_a_snake_case():
    assert normalizar_estado("  En Transito ") == "en_transito"
    assert normalizar_estado("ENTREGADO") == "entregado"


def test_deduplicar_por_clave_compuesta_conserva_ultima():
    df = pd.DataFrame(
        {
            "id_orden": ["OC-1", "OC-1", "OC-2"],
            "linea": [1, 1, 1],
            "monto_total_usd": [100, 200, 300],
        }
    )
    limpio, removidos = deduplicar(df, ["id_orden", "linea"])
    assert removidos == 1
    assert len(limpio) == 2
    # keep="last": la fila (OC-1, 1) que queda es la de monto 200.
    fila = limpio[(limpio["id_orden"] == "OC-1") & (limpio["linea"] == 1)]
    assert fila.iloc[0]["monto_total_usd"] == 200


def test_validar_contrato_detecta_columna_ausente():
    completo = pd.DataFrame(columns=[
        "id_orden", "linea", "fecha_emision", "id_proveedor", "id_pais_origen",
        "codigo_producto", "cantidad", "precio_unitario_usd", "monto_total_usd", "estado",
    ])
    assert validar_contrato(completo, "ordenes") == []
    incompleto = completo.drop(columns=["estado"])
    assert validar_contrato(incompleto, "ordenes") == ["estado"]


def test_aplicar_aliases_renombra_header_variante():
    df = pd.DataFrame(columns=["id_envio", "valor_fob_us"])
    renombrado = aplicar_aliases(df, "transito")
    assert "valor_fob_usd" in renombrado.columns
    assert "valor_fob_us" not in renombrado.columns


def test_aplicar_modo_full_reemplaza_y_delta_acumula():
    previo = pd.DataFrame({"id_orden": ["OC-1"], "linea": ["1"], "monto_total_usd": ["100"]})
    nuevo = pd.DataFrame({"id_orden": ["OC-1", "OC-2"], "linea": ["1", "1"],
                          "monto_total_usd": ["150", "300"]})
    # full: ignora el previo.
    full = aplicar_modo(nuevo, previo, "full", ["id_orden", "linea"])
    assert len(full) == 2
    # delta: acumula y el nuevo pisa la clave repetida (OC-1 pasa de 100 a 150).
    delta = aplicar_modo(nuevo, previo, "delta", ["id_orden", "linea"])
    assert len(delta) == 2
    fila = delta[(delta["id_orden"] == "OC-1")]
    assert fila.iloc[0]["monto_total_usd"] == "150"


def test_validar_filas_deriva_invalidas_a_cuarentena():
    crudo = pd.DataFrame({
        "id_orden": ["OC-1", "OC-2"],
        "linea": ["1", "1"],
        "fecha_emision": ["2026-06-01", "2026-06-02"],
        "id_proveedor": ["PRV-1", "PRV-2"],
        "id_pais_origen": ["CN", "DE"],
        "codigo_producto": ["SKU-1", "SKU-2"],
        "cantidad": ["10", "5"],
        "precio_unitario_usd": ["100.00", "50.00"],
        "monto_total_usd": ["1000.00", "250.00"],
        "estado": ["aprobada", "estado_invalido"],  # la 2da viola el dominio
    })
    limpio = limpiar_ordenes(crudo)
    validas, invalidas = validar_filas(limpio, OrdenLimpia)
    assert len(validas) == 1
    assert len(invalidas) == 1
    assert invalidas.iloc[0]["id_orden"] == "OC-2"

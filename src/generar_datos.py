"""Generador de datos sintéticos con ruido realista.

Produce los 3 exports crudos ficticios que alimentan el pipeline, imitando
cómo llegan los datos de un sistema fuente real: fechas en formatos mezclados,
números en formato anglosajón con separador de miles, valores faltantes,
duplicados y texto con capitalización/espacios inconsistentes.

Los datos son 100% inventados y generados por código (semilla reproducible).
No representan a ninguna empresa ni cliente real.

Uso:
    python -m src.generar_datos            # genera con la semilla por defecto
    python -m src.generar_datos --seed 7   # genera otra variante

Salida: 3 archivos CSV en data/inbox/ con prefijo de timestamp.
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("generador")

# ── Catálogos del dominio ficticio ───────────────────────────────────────────
PAISES_ORIGEN = ["CN", "DE", "BR", "US", "IT", "IN", "MX", "ES"]
CATEGORIAS = ["electronica", "textil", "quimica", "maquinaria", "alimentos"]
ESTADOS_ORDEN = ["pendiente", "aprobada", "recibida", "anulada"]
ESTADOS_TRANSITO = ["en_origen", "en_transito", "en_aduana", "entregado"]

# Parámetros de volumen (chicos: la demo corre en segundos).
N_PROVEEDORES = 25
N_ORDENES = 120          # cada orden tiene entre 1 y 4 líneas
N_ENVIOS = 180

# Proporciones de ruido.
PCT_DUPLICADOS = 0.06    # filas que se reinsertan duplicadas
PCT_FALTANTE_DURO = 0.015  # campos clave en blanco -> la fila irá a cuarentena


def _fmt_fecha(d: date, rnd: random.Random) -> str:
    """Devuelve la fecha en uno de tres formatos mezclados (ruido realista)."""
    return d.strftime(rnd.choice(["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]))


def _fmt_num(x: float, rnd: random.Random) -> str:
    """Devuelve el número en formato anglosajón, plano o europeo (ruido)."""
    estilo = rnd.choices(["anglo", "plano", "euro"], weights=[70, 20, 10])[0]
    if estilo == "anglo":
        return f"{x:,.2f}"              # 12,345.67  (coma de miles, punto decimal)
    if estilo == "plano":
        return f"{x:.2f}"              # 12345.67
    return f"{x:.2f}".replace(".", ",")  # 12345,67  (decimal europeo)


def _ruido_texto(s: str, rnd: random.Random) -> str:
    """Introduce inconsistencias de capitalización y espacios."""
    return rnd.choice([s, s.upper(), s.capitalize(), f"  {s} ", s.replace("_", " ")])


def _generar_proveedores(rnd: random.Random) -> list[dict]:
    proveedores = []
    for i in range(1, N_PROVEEDORES + 1):
        proveedores.append(
            {
                "id_proveedor": f"PRV-{i:03d}",
                "nombre_proveedor": _ruido_texto(f"Proveedor Ficticio {i:02d}", rnd),
                "id_pais": rnd.choice(PAISES_ORIGEN),
                "categoria": _ruido_texto(rnd.choice(CATEGORIAS), rnd),
                "activo": rnd.choice(["true", "false", "1", "0", "True"]),
            }
        )
    return proveedores


def _generar_ordenes(rnd: random.Random, proveedores: list[dict], hoy: date) -> list[dict]:
    filas = []
    ids_prov = [p["id_proveedor"] for p in proveedores]
    for n in range(1, N_ORDENES + 1):
        id_orden = f"OC-{n:05d}"
        # La orden más reciente cae HOY (garantiza frescura para Currency).
        dias_atras = 0 if n == N_ORDENES else rnd.randint(1, 120)
        fecha = hoy - timedelta(days=dias_atras)
        prov = rnd.choice(ids_prov)
        pais = rnd.choice(PAISES_ORIGEN)
        for linea in range(1, rnd.randint(1, 4) + 1):
            cantidad = rnd.randint(1, 500)
            precio = round(rnd.uniform(5, 2000), 2)
            monto = round(cantidad * precio, 2)
            filas.append(
                {
                    "id_orden": id_orden,
                    "linea": str(linea),
                    "fecha_emision": _fmt_fecha(fecha, rnd),
                    "id_proveedor": prov,
                    "id_pais_origen": pais,
                    "codigo_producto": f"SKU-{rnd.randint(1000, 9999)}",
                    "cantidad": str(cantidad),
                    "precio_unitario_usd": _fmt_num(precio, rnd),
                    "monto_total_usd": _fmt_num(monto, rnd),
                    "estado": _ruido_texto(rnd.choice(ESTADOS_ORDEN), rnd),
                }
            )
    return filas


def _generar_transito(rnd: random.Random, ordenes: list[dict], hoy: date) -> list[dict]:
    filas = []
    ids_orden = sorted({o["id_orden"] for o in ordenes})
    for n in range(1, N_ENVIOS + 1):
        id_orden = rnd.choice(ids_orden)
        despacho = hoy - timedelta(days=rnd.randint(5, 150))
        arribo_est = despacho + timedelta(days=rnd.randint(10, 60))
        estado = rnd.choice(ESTADOS_TRANSITO)
        # Solo los envíos entregados tienen arribo real (el resto es NULL legítimo).
        if estado == "entregado":
            arribo_real = despacho + timedelta(days=rnd.randint(8, 70))
            arribo_real_str = _fmt_fecha(arribo_real, rnd)
        else:
            arribo_real_str = rnd.choice(["", "N/D"])
        fob = round(rnd.uniform(500, 80000), 2)
        filas.append(
            {
                "id_envio": f"ENV-{n:05d}",
                "id_orden": id_orden,
                "fecha_despacho": _fmt_fecha(despacho, rnd),
                "fecha_arribo_estimada": _fmt_fecha(arribo_est, rnd),
                "fecha_arribo_real": arribo_real_str,
                "id_pais_origen": rnd.choice(PAISES_ORIGEN),
                "valor_fob_us": _fmt_num(fob, rnd),  # OJO: header con typo a propósito
                "flete_usd": _fmt_num(round(fob * rnd.uniform(0.02, 0.15), 2), rnd),
                "seguro_usd": _fmt_num(round(fob * rnd.uniform(0.005, 0.03), 2), rnd),
                "estado_transito": _ruido_texto(estado, rnd),
            }
        )
    return filas


def _inyectar_duplicados(filas: list[dict], rnd: random.Random) -> list[dict]:
    """Reinserta una proporción de filas como duplicados exactos."""
    n_dup = int(len(filas) * PCT_DUPLICADOS)
    if n_dup == 0:
        return filas
    duplicadas = rnd.sample(filas, n_dup)
    resultado = filas + [dict(f) for f in duplicadas]
    rnd.shuffle(resultado)
    return resultado


def _inyectar_faltantes(filas: list[dict], campo: str, rnd: random.Random) -> None:
    """Pone en blanco un campo clave en una fracción mínima de filas."""
    n_falt = int(len(filas) * PCT_FALTANTE_DURO)
    for fila in rnd.sample(filas, max(1, n_falt)):
        fila[campo] = rnd.choice(["", "N/D"])


def _escribir_csv(filas: list[dict], destino: Path, columnas: list[str]) -> None:
    with destino.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columnas)
        writer.writeheader()
        writer.writerows(filas)


def generar(seed: int | None = None) -> dict[str, Path]:
    """Genera los 3 exports crudos y los deja en data/inbox/. Devuelve sus rutas."""
    config.asegurar_directorios()
    rnd = random.Random(seed if seed is not None else config.SEED)
    hoy = date.today()
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    proveedores = _generar_proveedores(rnd)
    ordenes = _generar_ordenes(rnd, proveedores, hoy)
    transito = _generar_transito(rnd, ordenes, hoy)

    # Ruido: duplicados en hechos y faltantes duros en una clave foránea.
    ordenes = _inyectar_duplicados(ordenes, rnd)
    transito = _inyectar_duplicados(transito, rnd)
    _inyectar_faltantes(ordenes, "id_proveedor", rnd)

    rutas = {
        "proveedores": config.INBOX / f"{ts}_proveedores.csv",
        "ordenes": config.INBOX / f"{ts}_ordenes_compra.csv",
        "transito": config.INBOX / f"{ts}_transito_mercaderia.csv",
    }
    _escribir_csv(
        proveedores, rutas["proveedores"],
        ["id_proveedor", "nombre_proveedor", "id_pais", "categoria", "activo"],
    )
    _escribir_csv(
        ordenes, rutas["ordenes"],
        ["id_orden", "linea", "fecha_emision", "id_proveedor", "id_pais_origen",
         "codigo_producto", "cantidad", "precio_unitario_usd", "monto_total_usd", "estado"],
    )
    _escribir_csv(
        transito, rutas["transito"],
        ["id_envio", "id_orden", "fecha_despacho", "fecha_arribo_estimada",
         "fecha_arribo_real", "id_pais_origen", "valor_fob_us", "flete_usd",
         "seguro_usd", "estado_transito"],
    )

    logger.info("Proveedores: %d filas -> %s", len(proveedores), rutas["proveedores"].name)
    logger.info("Órdenes:     %d filas -> %s", len(ordenes), rutas["ordenes"].name)
    logger.info("Tránsito:    %d filas -> %s", len(transito), rutas["transito"].name)
    return rutas


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera los exports crudos sintéticos.")
    parser.add_argument("--seed", type=int, default=None, help="semilla de reproducibilidad")
    args = parser.parse_args()
    generar(seed=args.seed)


if __name__ == "__main__":
    main()

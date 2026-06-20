"""Cleaner principal del pipeline.

Lee los exports crudos de `data/inbox/`, los normaliza, deduplica por claves
compuestas, valida el contrato de columnas canónicas, valida cada fila contra
su modelo Pydantic (las inválidas van a `quarantine/`), aplica el modo de carga
(delta/full) por entidad, deriva las dimensiones del modelo estrella, publica
los datasets limpios en `data/processed/`, deja un snapshot diario inmutable y
emite un quality report JSON con códigos de salida diferenciados.

Capa 1 de la auditoría (integridad por corrida) vive acá dentro: conteos de
entrada/salida, duplicados removidos, claves únicas, sumas de control y
detección de columnas canónicas ausentes o vacías.

Códigos de salida (impiden usar datos corruptos aguas abajo):
    0  OK        — corrida limpia, datos publicados.
    1  WARNING   — datos publicados con observaciones (reuso, duplicados,
                   filas en cuarentena o columna canónica vacía).
    2  SCHEMA    — falta una columna canónica: NO se publica nada.
    3  ERROR     — fallo no controlado: NO se publica nada.

Uso:
    python -m src.limpiar_datos
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
from pydantic import BaseModel, ValidationError

from . import config
from .modelos import OrdenLimpia, ProveedorLimpio, TransitoLimpio

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cleaner")

# ── Códigos de salida ────────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_WARNING = 1
EXIT_SCHEMA = 2
EXIT_ERROR = 3

# ── Tokens que representan "dato ausente" en el origen ───────────────────────
NULOS = {"", "N/D", "NA", "NAN", "NULL", "NONE", "-", "S/D"}
FORMATOS_FECHA = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"]

# ── Catálogo de países (para construir dim_pais) ─────────────────────────────
CATALOGO_PAISES = {
    "CN": ("China", "Asia"),
    "DE": ("Alemania", "Europa"),
    "BR": ("Brasil", "América"),
    "US": ("Estados Unidos", "América"),
    "IT": ("Italia", "Europa"),
    "IN": ("India", "Asia"),
    "MX": ("México", "América"),
    "ES": ("España", "Europa"),
}
NOMBRES_MES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]
DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# ── Contrato por entidad ─────────────────────────────────────────────────────
CANONICAS = {
    "proveedores": ["id_proveedor", "nombre_proveedor", "id_pais", "categoria", "activo"],
    "ordenes": ["id_orden", "linea", "fecha_emision", "id_proveedor", "id_pais_origen",
                "codigo_producto", "cantidad", "precio_unitario_usd", "monto_total_usd", "estado"],
    "transito": ["id_envio", "id_orden", "fecha_despacho", "fecha_arribo_estimada",
                 "fecha_arribo_real", "id_pais_origen", "valor_fob_usd", "flete_usd",
                 "seguro_usd", "estado_transito"],
}
# Alias conocidos del sistema fuente -> nombre canónico. Tolera variantes de
# header sin tratarlas como violación de esquema.
ALIASES = {
    "transito": {"valor_fob_us": "valor_fob_usd"},
}
CLAVES = {
    "proveedores": ["id_proveedor"],
    "ordenes": ["id_orden", "linea"],
    "transito": ["id_envio", "id_orden"],
}
MODOS = {"proveedores": "full", "ordenes": "delta", "transito": "full"}
SALIDA = {"proveedores": "dim_proveedor", "ordenes": "hechos_ordenes", "transito": "hechos_transito"}
COL_SUMA = {"proveedores": None, "ordenes": "monto_total_usd", "transito": "valor_fob_usd"}
PATRON_ARCHIVO = {"proveedores": "proveedores", "ordenes": "ordenes_compra", "transito": "transito_mercaderia"}


# ═════════════════════════════════════════════════════════════════════════════
# Normalizadores (funciones puras, testeables)
# ═════════════════════════════════════════════════════════════════════════════
def normalizar_texto(valor: Any) -> str:
    """Recorta, colapsa espacios y mapea tokens nulos a cadena vacía."""
    if valor is None:
        return ""
    s = str(valor).strip()
    if s.upper() in NULOS:
        return ""
    return " ".join(s.split())


def normalizar_estado(valor: Any) -> str:
    """Normaliza un estado a snake_case en minúsculas ('En Transito' -> 'en_transito')."""
    s = normalizar_texto(valor).lower()
    return s.replace(" ", "_")


def normalizar_booleano(valor: Any) -> bool:
    """Interpreta variantes de verdadero ('true', '1', 'si', ...) como bool."""
    return normalizar_texto(valor).lower() in {"true", "1", "si", "sí", "verdadero", "t", "yes"}


def normalizar_fecha(valor: Any) -> Optional[date]:
    """Parsea una fecha probando varios formatos. Devuelve None si no se puede."""
    if valor is None:
        return None
    s = str(valor).strip()
    if s.upper() in NULOS:
        return None
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def normalizar_numero(valor: Any) -> Optional[float]:
    """Normaliza números en formato anglosajón, plano o europeo a float."""
    if valor is None:
        return None
    s = str(valor).strip().replace("$", "").replace(" ", "")
    if s.upper() in NULOS:
        return None
    if "," in s and "." in s:
        # Anglosajón: coma de miles, punto decimal -> quitar comas.
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        partes = s.split(",")
        # Si lo que sigue a la coma tiene 2 dígitos es decimal europeo; si no, miles.
        s = s.replace(",", ".") if len(partes[-1]) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def normalizar_entero(valor: Any) -> Optional[int]:
    """Normaliza a entero reutilizando el parser numérico."""
    n = normalizar_numero(valor)
    return int(n) if n is not None else None


# ═════════════════════════════════════════════════════════════════════════════
# Operaciones de contrato, dedup y modo de carga (puras, testeables)
# ═════════════════════════════════════════════════════════════════════════════
def aplicar_aliases(df: pd.DataFrame, entidad: str) -> pd.DataFrame:
    """Renombra headers variantes del origen al nombre canónico."""
    mapa = ALIASES.get(entidad, {})
    return df.rename(columns={k: v for k, v in mapa.items() if k in df.columns})


def validar_contrato(df: pd.DataFrame, entidad: str) -> list[str]:
    """Devuelve la lista de columnas canónicas ausentes (vacía = contrato OK)."""
    return [c for c in CANONICAS[entidad] if c not in df.columns]


def columnas_canonicas_vacias(df: pd.DataFrame, entidad: str) -> list[str]:
    """Columnas canónicas presentes pero 100% vacías (señal de drift de origen)."""
    vacias = []
    for col in CANONICAS[entidad]:
        if col in df.columns and df[col].map(lambda v: normalizar_texto(v) == "").all():
            vacias.append(col)
    return vacias


def deduplicar(df: pd.DataFrame, claves: list[str]) -> tuple[pd.DataFrame, int]:
    """Elimina duplicados por clave compuesta conservando la última aparición."""
    antes = len(df)
    limpio = df.drop_duplicates(subset=claves, keep="last").reset_index(drop=True)
    return limpio, antes - len(limpio)


def aplicar_modo(
    df_nuevo: pd.DataFrame, df_previo: Optional[pd.DataFrame], modo: str, claves: list[str]
) -> pd.DataFrame:
    """full = reemplaza; delta = acumula sobre el previo (el nuevo pisa por clave)."""
    if modo == "full" or df_previo is None or df_previo.empty:
        return df_nuevo.reset_index(drop=True)
    combinado = pd.concat([df_previo, df_nuevo], ignore_index=True)
    return combinado.drop_duplicates(subset=claves, keep="last").reset_index(drop=True)


def hash_archivo(ruta: Path) -> str:
    """SHA-256 del contenido (para detectar reuso de un archivo ya procesado)."""
    h = hashlib.sha256()
    h.update(ruta.read_bytes())
    return h.hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Limpieza por entidad
# ═════════════════════════════════════════════════════════════════════════════
def limpiar_proveedores(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["id_proveedor"] = df["id_proveedor"].map(normalizar_texto)
    out["nombre_proveedor"] = df["nombre_proveedor"].map(normalizar_texto)
    out["id_pais"] = df["id_pais"].map(lambda v: normalizar_texto(v).upper())
    out["categoria"] = df["categoria"].map(normalizar_estado)
    out["activo"] = df["activo"].map(normalizar_booleano)
    return out


def limpiar_ordenes(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["id_orden"] = df["id_orden"].map(normalizar_texto)
    out["linea"] = df["linea"].map(normalizar_entero)
    out["fecha_emision"] = df["fecha_emision"].map(normalizar_fecha)
    out["id_proveedor"] = df["id_proveedor"].map(normalizar_texto)
    out["id_pais_origen"] = df["id_pais_origen"].map(lambda v: normalizar_texto(v).upper())
    out["codigo_producto"] = df["codigo_producto"].map(normalizar_texto)
    out["cantidad"] = df["cantidad"].map(normalizar_entero)
    out["precio_unitario_usd"] = df["precio_unitario_usd"].map(normalizar_numero)
    out["monto_total_usd"] = df["monto_total_usd"].map(normalizar_numero)
    out["estado"] = df["estado"].map(normalizar_estado)
    return out


def limpiar_transito(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["id_envio"] = df["id_envio"].map(normalizar_texto)
    out["id_orden"] = df["id_orden"].map(normalizar_texto)
    out["fecha_despacho"] = df["fecha_despacho"].map(normalizar_fecha)
    out["fecha_arribo_estimada"] = df["fecha_arribo_estimada"].map(normalizar_fecha)
    out["fecha_arribo_real"] = df["fecha_arribo_real"].map(normalizar_fecha)
    out["id_pais_origen"] = df["id_pais_origen"].map(lambda v: normalizar_texto(v).upper())
    out["valor_fob_usd"] = df["valor_fob_usd"].map(normalizar_numero)
    out["flete_usd"] = df["flete_usd"].map(normalizar_numero)
    out["seguro_usd"] = df["seguro_usd"].map(normalizar_numero)
    out["estado_transito"] = df["estado_transito"].map(normalizar_estado)
    return out


LIMPIADORES: dict[str, Callable[[pd.DataFrame], pd.DataFrame]] = {
    "proveedores": limpiar_proveedores,
    "ordenes": limpiar_ordenes,
    "transito": limpiar_transito,
}
MODELOS: dict[str, type[BaseModel]] = {
    "proveedores": ProveedorLimpio,
    "ordenes": OrdenLimpia,
    "transito": TransitoLimpio,
}


# ═════════════════════════════════════════════════════════════════════════════
# Validación Pydantic fila a fila
# ═════════════════════════════════════════════════════════════════════════════
def validar_filas(
    df: pd.DataFrame, modelo: type[BaseModel]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separa filas válidas de inválidas según el contrato Pydantic."""
    validas: list[dict] = []
    invalidas: list[dict] = []
    for registro in df.to_dict(orient="records"):
        registro = {k: (None if pd.isna(v) else v) for k, v in registro.items()}
        try:
            modelo(**registro)
            validas.append(registro)
        except ValidationError as exc:
            registro["_motivo_cuarentena"] = "; ".join(e["msg"] for e in exc.errors())
            invalidas.append(registro)
    cols = list(df.columns)
    df_validas = pd.DataFrame(validas, columns=cols) if validas else pd.DataFrame(columns=cols)
    df_invalidas = pd.DataFrame(invalidas) if invalidas else pd.DataFrame()
    return df_validas, df_invalidas


# ═════════════════════════════════════════════════════════════════════════════
# Serialización a formato de salida canónico
# ═════════════════════════════════════════════════════════════════════════════
def _celda_a_str(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def serializar_a_str(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].map(_celda_a_str)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Dimensiones del modelo estrella
# ═════════════════════════════════════════════════════════════════════════════
def construir_dim_pais(codigos: set[str]) -> pd.DataFrame:
    filas = []
    for cod in sorted(c for c in codigos if c):
        nombre, region = CATALOGO_PAISES.get(cod, ("Desconocido", "Desconocida"))
        filas.append({"id_pais": cod, "nombre_pais": nombre, "region": region})
    return pd.DataFrame(filas, columns=["id_pais", "nombre_pais", "region"])


def construir_dim_calendario(fechas: set[date]) -> pd.DataFrame:
    fechas_validas = {f for f in fechas if isinstance(f, date)}
    if not fechas_validas:
        return pd.DataFrame(columns=["fecha", "anio", "mes", "nombre_mes", "trimestre",
                                     "dia_semana", "es_fin_semana"])
    rango = pd.date_range(min(fechas_validas), max(fechas_validas), freq="D")
    filas = []
    for ts in rango:
        d = ts.date()
        filas.append({
            "fecha": d.isoformat(),
            "anio": d.year,
            "mes": d.month,
            "nombre_mes": NOMBRES_MES[d.month],
            "trimestre": (d.month - 1) // 3 + 1,
            "dia_semana": DIAS_SEMANA[d.weekday()],
            "es_fin_semana": d.weekday() >= 5,
        })
    return pd.DataFrame(filas)


# ═════════════════════════════════════════════════════════════════════════════
# Orquestación de la corrida
# ═════════════════════════════════════════════════════════════════════════════
def _ultimo_archivo(patron: str) -> Optional[Path]:
    candidatos = sorted(config.INBOX.glob(f"*{patron}*.csv"))
    return candidatos[-1] if candidatos else None


def _leer_acumulado(entidad: str) -> Optional[pd.DataFrame]:
    ruta = config.SNAPSHOTS / f"{entidad}_full_latest.csv"
    if ruta.exists():
        return pd.read_csv(ruta, dtype=str, keep_default_na=False)
    return None


def _estado_reuso() -> dict:
    ruta = config.MARKERS / "reuse_state.json"
    if ruta.exists():
        return json.loads(ruta.read_text(encoding="utf-8"))
    return {}


def procesar() -> tuple[int, dict]:
    """Ejecuta una corrida completa. Devuelve (exit_code, quality_report)."""
    inicio = time.time()
    config.asegurar_directorios()
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    reporte: dict[str, Any] = {
        "run_timestamp": datetime.now().isoformat(timespec="seconds"),
        "health_status": "ok",
        "exit_code": EXIT_OK,
        "source_files": {},
        "files_processed": [],
        "reuse": {},
        "warnings": [],
        "errors": [],
        "schema_validation": {},
    }
    estado_reuso = _estado_reuso()
    nuevo_estado_reuso: dict[str, Any] = {}

    # ── Fase 1: localizar archivos y validar contrato ANTES de publicar nada ──
    crudos: dict[str, pd.DataFrame] = {}
    for entidad in ("proveedores", "ordenes", "transito"):
        archivo = _ultimo_archivo(PATRON_ARCHIVO[entidad])
        if archivo is None:
            reporte["errors"].append(f"no se encontró archivo de '{entidad}' en inbox/")
            reporte["schema_validation"][entidad] = "archivo_ausente"
            continue
        reporte["source_files"][entidad] = archivo.name
        df = pd.read_csv(archivo, dtype=str, keep_default_na=False)
        df = aplicar_aliases(df, entidad)
        ausentes = validar_contrato(df, entidad)
        if ausentes:
            reporte["schema_validation"][entidad] = f"faltan columnas: {ausentes}"
            reporte["errors"].append(f"[{entidad}] columnas canónicas ausentes: {ausentes}")
        else:
            reporte["schema_validation"][entidad] = "ok"
            crudos[entidad] = df

        # Detección de reuso por hash de contenido.
        h = hash_archivo(archivo)
        previo = estado_reuso.get(entidad, {})
        count = previo.get("count", 0) + 1 if previo.get("hash") == h else 0
        nuevo_estado_reuso[entidad] = {"archivo": archivo.name, "hash": h, "count": count}
        reporte["reuse"][entidad] = count
        if count > 0:
            reporte["warnings"].append(f"[{entidad}] archivo reusado (idéntico al previo), count={count}")

    # Gate de esquema: si falta una canónica o un archivo, NO se publica nada.
    if any(v != "ok" for v in reporte["schema_validation"].values()) or len(crudos) < 3:
        reporte["health_status"] = "error"
        reporte["exit_code"] = EXIT_SCHEMA
        _persistir_reporte(reporte, ts)
        _persistir_estado_reuso(nuevo_estado_reuso)
        return EXIT_SCHEMA, reporte

    # ── Fase 2: limpiar, deduplicar, validar y aplicar modo ──
    resultados: dict[str, pd.DataFrame] = {}
    for entidad, df_crudo in crudos.items():
        filas_entrada = len(df_crudo)
        df_limpio = LIMPIADORES[entidad](df_crudo)
        df_dedup, n_dups = deduplicar(df_limpio, CLAVES[entidad])
        df_valido, df_cuarentena = validar_filas(df_dedup, MODELOS[entidad])
        vacias = columnas_canonicas_vacias(df_crudo, entidad)

        # Persistir cuarentena (filas que no superan el contrato Pydantic).
        if not df_cuarentena.empty:
            ruta_q = config.QUARANTINE / f"{entidad}_{ts}.csv"
            df_cuarentena.to_csv(ruta_q, index=False)

        # Serializar y aplicar modo de carga (delta acumula, full reemplaza).
        df_str = serializar_a_str(df_valido)
        df_previo = _leer_acumulado(entidad) if MODOS[entidad] == "delta" else None
        df_resultado = aplicar_modo(df_str, df_previo, MODOS[entidad], CLAVES[entidad])
        resultados[entidad] = df_resultado

        # Guardar acumulado (snapshot full por entidad + puntero latest).
        df_resultado.to_csv(config.SNAPSHOTS / f"{entidad}_full_{ts}.csv", index=False)
        df_resultado.to_csv(config.SNAPSHOTS / f"{entidad}_full_latest.csv", index=False)

        # Integridad de corrida (capa 1).
        col_suma = COL_SUMA[entidad]
        suma_control = (
            float(pd.to_numeric(df_valido[col_suma], errors="coerce").sum()) if col_suma else None
        )
        reporte["files_processed"].append({
            "entidad": entidad,
            "archivo_salida": f"{SALIDA[entidad]}.csv",
            "modo": MODOS[entidad],
            "filas_entrada": filas_entrada,
            "filas_validas": len(df_valido),
            "filas_publicadas": len(df_resultado),
            "duplicados_removidos": n_dups,
            "filas_cuarentena": len(df_cuarentena),
            "claves_unicas_validadas": bool(not df_resultado.duplicated(subset=CLAVES[entidad]).any()),
            "columnas_canonicas_vacias": vacias,
            "suma_control": suma_control,
        })
        if n_dups:
            reporte["warnings"].append(f"[{entidad}] {n_dups} duplicados removidos")
        if not df_cuarentena.empty:
            reporte["warnings"].append(f"[{entidad}] {len(df_cuarentena)} filas en cuarentena")
        if vacias:
            reporte["warnings"].append(f"[{entidad}] columnas canónicas vacías: {vacias}")

    # ── Fase 3: publicar processed/ (hechos + dimensiones) ──
    config.PROCESSED.mkdir(parents=True, exist_ok=True)
    for entidad, df_res in resultados.items():
        df_res.to_csv(config.PROCESSED / f"{SALIDA[entidad]}.csv", index=False)

    # Derivar dimensiones a partir de los datasets publicados.
    dim_pais, dim_cal = _construir_dimensiones(resultados)
    dim_pais.to_csv(config.PROCESSED / "dim_pais.csv", index=False)
    dim_cal.to_csv(config.PROCESSED / "dim_calendario.csv", index=False)

    # Metadata de la corrida.
    _escribir_metadata(reporte, ts)

    # ── Fase 4: snapshot diario inmutable + marca del día ──
    _snapshot_diario()
    (config.MARKERS / f"{date.today().isoformat()}.flag").write_text(
        reporte["run_timestamp"], encoding="utf-8"
    )
    _persistir_estado_reuso(nuevo_estado_reuso)

    # ── Determinar exit code final ──
    if reporte["warnings"]:
        reporte["health_status"] = "warning"
        reporte["exit_code"] = EXIT_WARNING
    reporte["run_duration_seconds"] = round(time.time() - inicio, 2)
    _persistir_reporte(reporte, ts)
    return reporte["exit_code"], reporte


def _construir_dimensiones(resultados: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    codigos: set[str] = set()
    fechas: set[date] = set()
    if "ordenes" in resultados:
        ordenes = resultados["ordenes"]
        codigos |= set(ordenes["id_pais_origen"])
        fechas |= {normalizar_fecha(v) for v in ordenes["fecha_emision"]}
    if "transito" in resultados:
        transito = resultados["transito"]
        codigos |= set(transito["id_pais_origen"])
        for col in ("fecha_despacho", "fecha_arribo_estimada", "fecha_arribo_real"):
            fechas |= {normalizar_fecha(v) for v in transito[col]}
    if "proveedores" in resultados:
        codigos |= set(resultados["proveedores"]["id_pais"])
    fechas.discard(None)
    return construir_dim_pais(codigos), construir_dim_calendario(fechas)


def _snapshot_diario() -> None:
    destino = config.HISTORICO / date.today().isoformat()
    destino.mkdir(parents=True, exist_ok=True)
    for csv_file in config.PROCESSED.glob("*.csv"):
        shutil.copy2(csv_file, destino / csv_file.name)


def _escribir_metadata(reporte: dict, ts: str) -> None:
    filas = [{
        "run_timestamp": reporte["run_timestamp"],
        "exit_code": reporte["exit_code"],
        "health_status": reporte["health_status"],
        "entidades_publicadas": len(reporte["files_processed"]),
        "warnings": len(reporte["warnings"]),
    }]
    pd.DataFrame(filas).to_csv(config.PROCESSED / "metadata_corrida.csv", index=False)


def _persistir_reporte(reporte: dict, ts: str) -> None:
    ruta = config.QUALITY_REPORTS / f"quality_report_{ts}.json"
    ruta.write_text(json.dumps(reporte, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Quality report: %s", ruta)


def _persistir_estado_reuso(estado: dict) -> None:
    (config.MARKERS / "reuse_state.json").write_text(
        json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> None:
    try:
        exit_code, reporte = procesar()
    except Exception as exc:  # noqa: BLE001 — el cleaner nunca debe publicar ante un fallo
        logger.exception("Fallo no controlado en el cleaner")
        config.asegurar_directorios()
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        _persistir_reporte(
            {"health_status": "error", "exit_code": EXIT_ERROR, "errors": [str(exc)]}, ts
        )
        sys.exit(EXIT_ERROR)
    logger.info("Corrida finalizada: health=%s exit=%s", reporte["health_status"], exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

"""Configuración central del pipeline.

Toda la configuración se lee desde variables de entorno (cargadas de `.env`
si existe) con valores por defecto sensatos, de modo que el repo funcione
recién clonado sin ningún paso de configuración. La separación
configuración/código es deliberada (ver README, nota de seguridad).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Carga `.env` si está presente. Si no existe, se usan los defaults de abajo;
# nunca se requiere un `.env` para la demo (no contiene secretos reales).
load_dotenv()

# Raíz del repositorio (este archivo vive en <repo>/src/config.py).
RAIZ = Path(__file__).resolve().parents[1]

# Directorio raíz de los datos. Configurable para apuntar a un volumen externo.
DATA_DIR = Path(os.environ.get("PIPELINE_DATA_DIR", str(RAIZ / "data")))

# Subdirectorios del pipeline (estilo "carpeta por etapa").
INBOX = DATA_DIR / "inbox"                 # exports crudos de entrada
PROCESSED = DATA_DIR / "processed"         # datasets limpios (modelo estrella)
HISTORICO = PROCESSED / "historico"        # snapshot diario inmutable
SNAPSHOTS = DATA_DIR / "snapshots"         # acumulados full por entidad (delta)
QUALITY_REPORTS = DATA_DIR / "quality_reports"  # reportes de corrida y auditoría
MARKERS = DATA_DIR / "markers"             # marcas de corrida y estado de reuso
QUARANTINE = DATA_DIR / "quarantine"       # filas que no superan la validación

# Semilla del generador sintético (reproducibilidad).
SEED = int(os.environ.get("SEED_DATOS", "42"))

# Umbrales de la auditoría DAMA.
UMBRAL_SALUDABLE = float(os.environ.get("UMBRAL_SALUDABLE", "90"))
UMBRAL_DRIFT = float(os.environ.get("UMBRAL_DRIFT", "75"))
TOLERANCIA_DRIFT_BASELINE = float(os.environ.get("TOLERANCIA_DRIFT_BASELINE", "5"))

# Ventana de frescura (días) para la dimensión Currency.
DIAS_FRESCURA = int(os.environ.get("DIAS_FRESCURA", "3"))


def asegurar_directorios() -> None:
    """Crea la estructura de directorios de datos si no existe (idempotente)."""
    for carpeta in (
        INBOX,
        PROCESSED,
        HISTORICO,
        SNAPSHOTS,
        QUALITY_REPORTS,
        MARKERS,
        QUARANTINE,
    ):
        carpeta.mkdir(parents=True, exist_ok=True)

"""Contratos de datos (Pydantic v2).

Estos modelos definen la forma válida de cada entidad DESPUÉS de la limpieza.
El cleaner construye un modelo por fila antes de publicar: una fila que no
valida no se escribe en `processed/`, se deriva a `quarantine/`. Es la barrera
que impide que un dato malformado contamine el modelo estrella aguas abajo.

Dominio: supply chain de importaciones (ficticio). Entidades:
- Orden de compra (hecho)
- Tránsito de mercadería (hecho)
- Proveedor (dimensión)
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# ── Dominios cerrados (listas de valores permitidos) ─────────────────────────
ESTADOS_ORDEN = {"pendiente", "aprobada", "recibida", "anulada"}
ESTADOS_TRANSITO = {"en_origen", "en_transito", "en_aduana", "entregado"}
CATEGORIAS_PROVEEDOR = {"electronica", "textil", "quimica", "maquinaria", "alimentos"}


class OrdenLimpia(BaseModel):
    """Una línea de una orden de compra ya normalizada."""

    model_config = ConfigDict(extra="forbid")

    id_orden: str
    linea: int
    fecha_emision: date
    id_proveedor: str
    id_pais_origen: str
    codigo_producto: str
    cantidad: int
    precio_unitario_usd: float
    monto_total_usd: float
    estado: str

    @field_validator("id_orden", "id_proveedor", "id_pais_origen", "codigo_producto")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("identificador vacío")
        return v.strip()

    @field_validator("linea", "cantidad")
    @classmethod
    def _entero_positivo(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("debe ser un entero positivo")
        return v

    @field_validator("precio_unitario_usd", "monto_total_usd")
    @classmethod
    def _monto_no_negativo(cls, v: float) -> float:
        if v < 0:
            raise ValueError("el monto no puede ser negativo")
        return v

    @field_validator("estado")
    @classmethod
    def _estado_valido(cls, v: str) -> str:
        if v not in ESTADOS_ORDEN:
            raise ValueError(f"estado fuera de dominio: {v}")
        return v


class TransitoLimpio(BaseModel):
    """Un envío de mercadería ya normalizado."""

    model_config = ConfigDict(extra="forbid")

    id_envio: str
    id_orden: str
    fecha_despacho: date
    fecha_arribo_estimada: date
    fecha_arribo_real: Optional[date]
    id_pais_origen: str
    valor_fob_usd: float
    flete_usd: float
    seguro_usd: float
    estado_transito: str

    @field_validator("id_envio", "id_orden", "id_pais_origen")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("identificador vacío")
        return v.strip()

    @field_validator("valor_fob_usd", "flete_usd", "seguro_usd")
    @classmethod
    def _monto_no_negativo(cls, v: float) -> float:
        if v < 0:
            raise ValueError("el monto no puede ser negativo")
        return v

    @field_validator("estado_transito")
    @classmethod
    def _estado_valido(cls, v: str) -> str:
        if v not in ESTADOS_TRANSITO:
            raise ValueError(f"estado de tránsito fuera de dominio: {v}")
        return v

    @model_validator(mode="after")
    def _coherencia_fechas(self) -> "TransitoLimpio":
        # El arribo real, si existe, no puede ser anterior al despacho.
        if self.fecha_arribo_real and self.fecha_arribo_real < self.fecha_despacho:
            raise ValueError("fecha_arribo_real anterior a fecha_despacho")
        # Un envío entregado debe tener arribo real informado.
        if self.estado_transito == "entregado" and self.fecha_arribo_real is None:
            raise ValueError("envío entregado sin fecha_arribo_real")
        return self


class ProveedorLimpio(BaseModel):
    """Un proveedor del catálogo ya normalizado (dimensión)."""

    model_config = ConfigDict(extra="forbid")

    id_proveedor: str
    nombre_proveedor: str
    id_pais: str
    categoria: str
    activo: bool

    @field_validator("id_proveedor", "nombre_proveedor", "id_pais")
    @classmethod
    def _no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("campo obligatorio vacío")
        return v.strip()

    @field_validator("categoria")
    @classmethod
    def _categoria_valida(cls, v: str) -> str:
        if v not in CATEGORIAS_PROVEEDOR:
            raise ValueError(f"categoría fuera de dominio: {v}")
        return v

"""Pipeline de calidad de datos para reporting BI (demo de portfolio).

Paquete con los componentes del pipeline:
- generar_datos:    generador de datos sintéticos con ruido realista.
- limpiar_datos:    cleaner principal (normaliza, deduplica, valida, publica).
- auditoria_calidad: auditoría DAMA en 6 dimensiones sobre los datos limpios.
- modelos:          contratos de datos (Pydantic) usados como validación previa.
- config:           configuración central leída desde .env con defaults.
"""

__version__ = "1.0.0"

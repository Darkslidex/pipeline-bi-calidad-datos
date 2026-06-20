# Pipeline de Calidad de Datos para Reporting BI

Pipeline de ingeniería de datos que toma exports crudos de un sistema fuente
(dominio: *supply chain* de importaciones), los limpia y normaliza, los modela
en un esquema estrella listo para BI, y los somete a una **auditoría de calidad
de datos en dos capas** bajo el marco **DAMA-DMBOK**.

> **Proyecto demostrativo de portfolio.** El dominio de negocio es ficticio y
> todos los datos son **100% sintéticos, generados por código**. No representa
> a ninguna empresa, cliente ni dato real.

---

## El problema que resuelve

Los datos que alimentan un dashboard rara vez llegan limpios. Un export típico
de un ERP/sistema fuente trae:

- fechas en **formatos mezclados** (`2026-06-19`, `19/06/2026`, `19-06-2026`),
- números en **formato anglosajón** con separador de miles (`12,345.67`) conviviendo con formato europeo (`12345,67`),
- **valores faltantes** codificados de mil maneras (`""`, `N/D`, `-`),
- **duplicados** por reenvíos del sistema fuente,
- **headers que cambian** entre exports,
- texto con **capitalización y espacios inconsistentes**.

Publicar eso directo a Power BI produce métricas erróneas y refrescos rotos.
Este pipeline interpone una etapa de **limpieza + validación + auditoría** entre
el dato crudo y el modelo analítico, con **gates que impiden que un dataset
corrupto llegue al dashboard**.

---

## Flujo end-to-end

```
   ┌──────────────────┐
   │  Sistema fuente  │   (simulado por el generador sintético)
   └────────┬─────────┘
            │  3 exports crudos con ruido realista
            ▼
   data/inbox/                          ordenes_compra · transito_mercaderia · proveedores
            │
            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  CLEANER  (src/limpiar_datos.py)                             │
   │  1. Alias de headers  → nombre canónico                     │
   │  2. Validación de contrato de columnas  → gate (exit 2)     │
   │  3. Normalización  (fechas, números, texto, estados)        │
   │  4. Deduplicación  por clave compuesta                      │
   │  5. Validación Pydantic fila a fila → quarantine/           │
   │  6. Modo de carga  delta (acumula) / full (reemplaza)       │
   │  7. Derivación de dimensiones  (país, calendario)           │
   │  ─ CAPA 1: chequeo de integridad por corrida ─              │
   └────────┬────────────────────────────────────────────────────┘
            │  publica si exit ∈ {0,1}
            ▼
   data/processed/      hechos_ordenes · hechos_transito
                        dim_proveedor · dim_pais · dim_calendario · metadata_corrida
            │                                   │
            │  snapshot diario inmutable        │
            ▼                                   ▼
   data/processed/historico/<fecha>/   ┌─────────────────────────────────────┐
                                       │  AUDITORÍA DAMA (Capa 2)            │
                                       │  src/auditoria_calidad.py          │
                                       │  12 reglas · 6 dimensiones         │
                                       │  score 0-100 · vs baseline         │
                                       │  → SALUDABLE / DRIFT / CRÍTICO     │
                                       └─────────────────────────────────────┘
                                                       │
                                                       ▼
                              data/quality_reports/  (JSON de corrida + auditoría)
```

---

## Cómo clonarlo y correrlo

Requisitos: **Python 3.10+**. El pipeline funciona recién clonado, sin
configuración previa (la config tiene defaults; `.env` es opcional).

```bash
# 1. Clonar y entrar
git clone <url-del-repo> portfolio-pipeline-bi
cd portfolio-pipeline-bi

# 2. Entorno virtual e instalación
python3 -m venv .venv
source .venv/bin/activate          # en Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. (Opcional) configuración local
cp .env.example .env               # editar si se quieren cambiar umbrales/rutas

# 4. Generar los datos sintéticos crudos  → data/inbox/
python -m src.generar_datos

# 5. Correr el pipeline de limpieza        → data/processed/
python -m src.limpiar_datos

# 6. Correr la auditoría de calidad DAMA    → veredicto en pantalla + JSON
python -m src.auditoria_calidad

# 7. Correr las pruebas
pytest -q
```

### Qué se ve al correrlo

- El **cleaner** termina con `exit 1 (WARNING)` en una corrida normal: publica
  los datos pero avisa que removió duplicados y mandó algunas filas a cuarentena.
- La **auditoría** imprime una tabla con el score por dimensión y el veredicto
  global (`SALUDABLE` en una corrida sana).

Para ver los **gates en acción**: si se elimina una columna canónica de un
export crudo y se vuelve a correr el cleaner, este termina con `exit 2 (SCHEMA)`
y **no publica nada** en `processed/`, evitando contaminar el dashboard.

---

## Modelo de datos (esquema estrella)

| Tabla | Tipo | Grano | Clave |
|---|---|---|---|
| `hechos_ordenes` | Hecho | línea de orden de compra | `(id_orden, linea)` |
| `hechos_transito` | Hecho | envío de mercadería | `(id_envio, id_orden)` |
| `dim_proveedor` | Dimensión | proveedor | `id_proveedor` |
| `dim_pais` | Dimensión | país de origen | `id_pais` |
| `dim_calendario` | Dimensión | día | `fecha` |

---

## Las 6 dimensiones DAMA evaluadas

La auditoría de Capa 2 aplica 12 reglas repartidas en las dimensiones de calidad
del marco **DAMA-DMBOK**:

| Dimensión | Pregunta | Reglas |
|---|---|---|
| **Completeness** | ¿Están los datos clave presentes? | cobertura de `id_proveedor` y `fecha_despacho` |
| **Validity** | ¿Los valores caen en dominios/rangos válidos? | estados en dominio, montos positivos, montos no negativos |
| **Reasonability** | ¿El dato es plausible para el negocio? | lead time de entrega en rango, coherencia `monto = cantidad × precio` |
| **Uniqueness** | ¿Hay duplicados por clave? | unicidad de orden, unicidad de proveedor |
| **Consistency** | ¿Las tablas son referencialmente coherentes? | FK orden→proveedor, FK tránsito→orden |
| **Currency** | ¿El dato está fresco? | antigüedad del registro más reciente |

Cada regla devuelve `PASS` / `FAIL` / `SKIP`. El score por dimensión es el
porcentaje ponderado de reglas que pasan (las `SKIP` no penalizan). El veredicto
sale de bandas configurables (`SALUDABLE ≥ 90`, `DRIFT ≥ 75`, `CRÍTICO < 75`) y
se **degrada a DRIFT** si el score cae respecto al `baseline.json` más que la
tolerancia configurada.

---

## Decisiones técnicas

### ¿Por qué modelo estrella?
Separar **hechos** (eventos medibles: órdenes, envíos) de **dimensiones**
(contexto descriptivo: proveedor, país, tiempo) es el patrón estándar para BI.
Optimiza las consultas analíticas (agregaciones y filtros por dimensión),
desnormaliza lo justo para que la herramienta (Power BI, Looker, etc.) arme
relaciones `1:*` dimensión→hecho sin joins costosos, y mantiene una
`dim_calendario` propia para análisis temporal (YTD, trimestres, estacionalidad).

### ¿Por qué modos delta y full?
No todas las entidades se cargan igual:
- **full** (reemplazo total) para entidades que el sistema fuente exporta
  completas cada vez: el catálogo de proveedores y el tránsito acumulado.
  Simple y siempre consistente con el origen.
- **delta** (acumulativo, el nuevo pisa por clave) para los hechos que llegan
  de forma incremental, como las órdenes: se mantiene el histórico sin
  reprocesar todo, y un reenvío corregido del sistema fuente **actualiza** la
  fila existente (idempotencia + corrección retroactiva) en vez de duplicarla.

### ¿Por qué exit codes diferenciados?
Un único “falló / no falló” es insuficiente para un orquestador. Distinguir:

| Código | Significado | ¿Publica? | Acción aguas arriba |
|---|---|---|---|
| `0` OK | corrida limpia | sí | refrescar BI |
| `1` WARNING | publicada con observaciones | sí | refrescar BI + registrar aviso |
| `2` SCHEMA | falta una columna canónica | **no** | **frenar refresco** + alertar |
| `3` ERROR | fallo no controlado | **no** | **frenar refresco** + alertar |

Esto convierte al exit code en un **contrato operativo**: el job que dispara el
refresco del dashboard solo avanza con `0` o `1`. Un `2`/`3` corta el flujo
**antes** de que un dataset incompleto llegue a producción.

### ¿Por qué auditoría en dos capas?
Responden preguntas distintas, en momentos distintos, para públicos distintos:

- **Capa 1 — integridad por corrida** (dentro del cleaner). *“¿La corrida de hoy
  terminó bien y los números cierran?”* Operacional, automática en cada
  ejecución, mira conteos, duplicados, sumas de control y esquema.
- **Capa 2 — auditoría DAMA** (script aparte). *“¿El dataset publicado es
  defendible para análisis?”* Analítica, periódica, mide la calidad del dato
  contra reglas de negocio y un baseline, y emite un veredicto comunicable.

Mezclarlas oculta señal: un pipeline puede correr perfecto (Capa 1 verde) y aun
así publicar un dato que driftó respecto a su baseline (Capa 2 en DRIFT).

---

## Nota de seguridad sobre el manejo de datos

La seguridad está integrada en el diseño, no agregada al final:

- **Los datos nunca se versionan.** `.gitignore` excluye `data/`, snapshots,
  reportes y cualquier `.csv`/`.xlsx`. El repositorio lleva *código*, no datos.
- **Configuración separada del código.** Los parámetros viven en `.env`
  (excluido del control de versiones); el repo solo incluye `.env.example` con
  **placeholders, nunca secretos**. En un despliegue real, el `.env` se protege
  con permisos `600`. Las credenciales jamás se hardcodean.
- **Validación de entrada como barrera.** Todo dato externo pasa por modelos
  **Pydantic** antes de escribirse: una fila que no cumple el contrato se deriva
  a `quarantine/`, no contamina el modelo publicado.
- **Gates que frenan aguas abajo.** Los exit codes `2`/`3` impiden que un
  dataset corrupto o incompleto sea consumido por el reporting.
- **Datos sintéticos.** La demo no contiene ninguna información personal o
  sensible: todo se genera por código con una semilla reproducible.

---

## Estructura del proyecto

```
portfolio-pipeline-bi/
├── README.md
├── requirements.txt
├── .env.example              # plantilla de configuración (sin secretos)
├── .gitignore
├── baseline.json             # scores de referencia para la auditoría
├── conftest.py               # hace importable `src` en pytest
├── src/
│   ├── config.py             # configuración central (lee .env con defaults)
│   ├── modelos.py            # contratos de datos (Pydantic v2)
│   ├── generar_datos.py      # generador de datos sintéticos con ruido
│   ├── limpiar_datos.py      # cleaner principal + auditoría Capa 1
│   └── auditoria_calidad.py  # auditoría DAMA Capa 2
└── tests/
    ├── test_limpiar_datos.py
    └── test_auditoria_calidad.py
```

> `data/` no está en el repositorio: lo crean los scripts en tiempo de
> ejecución (`inbox/`, `processed/`, `snapshots/`, `quality_reports/`,
> `markers/`, `quarantine/`).

---

## Stack

Python · pandas · Pydantic v2 · pytest · python-dotenv

"""Configuración de pytest a nivel raíz.

Inserta la raíz del repositorio en sys.path para que `import src` funcione al
correr `pytest` sin necesidad de instalar el paquete.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

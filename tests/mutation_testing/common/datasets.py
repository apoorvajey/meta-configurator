"""The datasets a run can use: schema/data pairs."""
from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "test_data"


# Schema/data pairs for a run: the schema name must match the option in MetaConfigurator's "Example
# Schemas" dropdown, the data file is uploaded in the Text View. run_config.json's "schema" picks one
# (or "random").
SCHEMA_DATA_PAIRS: list[tuple[str, Path]] = [
    ("Autonomous Vehicle Schema", DATA_DIR / "self_driving_vehicle_data.json"),
    ("Feature Testing Schema",    DATA_DIR / "person_data.json"),
    ("Strenda Schema",            DATA_DIR / "strenda_data.json"),
    ("EnzymeML Schema",           DATA_DIR / "enzymeml_data.json"),
]

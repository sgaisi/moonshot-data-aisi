from pydantic import BaseModel, Field
from pydantic.config import ConfigDict
from typing import List, Annotated, Optional, Any
from pathlib import Path
import json
import pytest

DATASETS_DIR = (Path(__file__).parent / ".." / "datasets").resolve()

class SampleSchema(BaseModel):
    id: Annotated[str, Field(min_length=1)]
    input: Annotated[str, Field(min_length=1)]
    tools: List[str]
    target: Optional[Any] = None

    model_config = ConfigDict(extra="forbid")

class DatasetSchema(BaseModel):
    name: str
    description: str
    license: str
    reference: str
    examples: List[SampleSchema]

    model_config = ConfigDict(extra="forbid")

@pytest.mark.parametrize("json_file", DATASETS_DIR.glob("jt3-*.json"))
def test_jt3_dataset_schema(json_file):
    with open(json_file, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            DatasetSchema(**data)
        except Exception as e:
            raise AssertionError(f"Validation failed for {json_file}:\n{e}")

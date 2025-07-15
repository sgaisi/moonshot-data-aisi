from pydantic import BaseModel, Field
from pydantic.config import ConfigDict
from typing import Annotated
from pathlib import Path
import json
import pytest

DATASETS_DIR = (Path(__file__).parent / ".." / "recipes").resolve()


class RecipeSchema(BaseModel):
    name: str
    description: str
    tags: list
    categories: list
    datasets: Annotated[list, Field(min_length=1)]
    prompt_templates: list
    metrics: Annotated[list, Field(min_length=1)]
    tools: list
    grading_scale: dict

    model_config = ConfigDict(extra="forbid")


@pytest.mark.parametrize("json_file", DATASETS_DIR.glob("jt3-*.json"))
def test_jt3_dataset_schema(json_file):
    with open(json_file, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            RecipeSchema(**data)
        except Exception as e:
            raise AssertionError(f"Validation failed for {json_file}:\n{e}")

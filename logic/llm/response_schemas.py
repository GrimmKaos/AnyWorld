"""Cached response schemas constrained to the current participants."""

import json
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, create_model

from core.schemas import ChanceEvent, ContextSummary, DicePlan, RoundResolution


@lru_cache(maxsize=32)
def participant_schema(
    base: type[BaseModel],
    names: tuple[str, ...],
    allow_hidden: bool = False,
    provider: str = "compatible",
    private_sources: tuple[str, ...] | None = None,
) -> type[BaseModel]:
    """Constrain generated object keys to the actual party, including empty openings."""
    field = (
        "rolls"
        if base is DicePlan
        else "player_resolutions" if base is RoundResolution else "player_states"
    )
    value = bool if base is DicePlan else str
    kind = "boolean" if base is DicePlan else "string"
    fields = {
        field: (
            dict[str, value],
            Field(
                json_schema_extra={
                    "properties": {
                        name: {"type": kind, **({"minLength": 1} if kind == "string" else {})}
                        for name in names
                    },
                    "required": list(names),
                    "additionalProperties": False,
                }
            ),
        )
    }
    if base is DicePlan:
        fields["chance_events"] = (
            list[ChanceEvent],
            Field(max_length=0),
        )
        fields["hidden_roll_sources"] = (
            dict[str, str],
            Field(
                json_schema_extra={
                    "properties": {
                        name: {
                            "type": "string",
                            **(
                                {"enum": ["", *private_sources]}
                                if private_sources is not None
                                else {}
                            ),
                        }
                        for name in names
                    },
                    "additionalProperties": False,
                }
            ),
        )
        fields["hidden_rolls"] = (
            list[str],
            Field(
                json_schema_extra={
                    "items": (
                        {"type": "string", "enum": list(names)} if names else {"type": "string"}
                    ),
                    "maxItems": len(names) if allow_hidden else 0,
                    **({} if provider == "openai" else {"uniqueItems": True}),
                }
            ),
        )
    elif base is RoundResolution and names:
        fields["global_narrative"] = (str, Field(min_length=1))
    elif base is ContextSummary:
        fields["world_state"] = (str, Field(min_length=1))
    schema = create_model(base.__name__, __base__=base, **fields)
    if provider == "openai":
        original_model_json_schema = schema.model_json_schema

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            """Remove JSON Schema keywords unsupported by OpenAI strict schemas."""
            result = original_model_json_schema(*args, **kwargs)
            unsupported = {
                "uniqueItems",
                "minItems",
                "maxItems",
                "minLength",
                "maxLength",
                "pattern",
                "format",
                "minimum",
                "maximum",
                "multipleOf",
            }

            def strip(node: Any) -> Any:
                """Recursively remove keywords rejected by OpenAI strict schemas."""
                if isinstance(node, dict):
                    return {
                        key: (
                            {name: strip(child) for name, child in value.items()}
                            if key in {"properties", "$defs", "definitions", "patternProperties"}
                            else strip(value)
                        )
                        for key, value in node.items()
                        if key not in unsupported
                    }
                if isinstance(node, list):
                    return [strip(value) for value in node]
                return node

            return strip(result)

        schema.model_json_schema = model_json_schema
    return schema


@lru_cache(maxsize=64)
def schema_text(schema: type[BaseModel]) -> str:
    """Return the compact JSON schema text for a response model."""
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))

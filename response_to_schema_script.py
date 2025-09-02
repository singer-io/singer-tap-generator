import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Union


def infer_type(value: Any, name: str = "") -> Union[List[str], tuple]:
    """
    Infers the JSON Schema type for a given value and field name.
    Uses regex and value checks to detect 'date-time' fields.
    """
    date_field_pattern = re.compile(
        r"(date|timestamp|time|modified|created)$",  # Ends with these words
        re.IGNORECASE
    )
    is_potential_date = bool(date_field_pattern.search(name))

    if is_potential_date:
        if value is None:
            return ["null", "string"], "date-time"
        if isinstance(value, (int, float, Decimal)):
            return ["null", "string"], "date-time"
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                return ["null", "string"], "date-time"
            except ValueError:
                return ["null", "string"], "date-time"
        return ["null", "string"], "date-time"

    # Default type inference
    if value is None:
        return ["null", "string"]
    if isinstance(value, bool):
        return ["null", "boolean"]
    if isinstance(value, int):
        return ["null", "integer"]
    if isinstance(value, float) or isinstance(value, Decimal):
        return ["null", "number"]
    if isinstance(value, list):
        return ["null", "array"]
    if isinstance(value, dict):
        return ["null", "object"]
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ["null", "string"], "date-time"
        except ValueError:
            return ["null", "string"]

    return ["null", "string"]


def generate_schema(data: dict) -> dict:
    """
    Recursively generates a JSON schema from a Python dictionary.
    """
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {}
    }

    for key, value in data.items():
        if isinstance(value, list):
            if value and isinstance(value[0], dict):
                items_schema = generate_schema(value[0])
                schema["properties"][key] = {
                    "type": ["null", "array"],
                    "items": items_schema
                }
            else:
                item_type = infer_type(value[0], key) if value else "string"
                if isinstance(item_type, tuple):
                    types, fmt = item_type
                    schema["properties"][key] = {
                        "type": ["null", "array"],
                        "items": {
                            "type": types,
                            "format": fmt
                        }
                    }
                else:
                    schema["properties"][key] = {
                        "type": ["null", "array"],
                        "items": {
                            "type": item_type
                        }
                    }

        elif isinstance(value, dict):
            schema["properties"][key] = generate_schema(value)

        else:
            inferred = infer_type(value, key)
            if isinstance(inferred, tuple):
                types, format_type = inferred
                schema["properties"][key] = {
                    "type": types,
                    "format": format_type
                }
            else:
                schema["properties"][key] = {
                    "type": inferred
                }

    return schema


def find_largest_dict(obj: Any) -> Union[Dict, None]:
    """
    Recursively find the dictionary with the most keys (i.e., the most structured part of the JSON).
    """
    max_dict = None
    max_keys = 0

    def recurse(o: Any):
        nonlocal max_dict, max_keys
        if isinstance(o, dict):
            if len(o) > max_keys:
                max_dict = o
                max_keys = len(o)
            for v in o.values():
                recurse(v)
        elif isinstance(o, list):
            for item in o:
                recurse(item)

    recurse(obj)
    return max_dict


def convert_to_schema_from_file(filepath: str) -> dict:
    """
    Main function to read a JSON file and convert its structure into a schema.
    It uses the most structured dictionary found in the JSON.
    """
    with open(filepath, "r") as f:
        response_data = json.load(f)

    sample = find_largest_dict(response_data)
    if not sample:
        raise ValueError("No usable dictionary structure found in the input JSON.")

    return generate_schema(sample)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate JSON schema from any nested JSON structure.")
    parser.add_argument("input", help="Path to the input JSON file")
    parser.add_argument("output", help="Path to save the generated schema")
    args = parser.parse_args()

    schema = convert_to_schema_from_file(args.input)

    with open(args.output, "w") as f:
        json.dump(schema, f, indent=2)

    print(f"Schema generated and saved to '{args.output}'")

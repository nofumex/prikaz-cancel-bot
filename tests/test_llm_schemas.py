from app.services.llm import AMOUNTS_JSON_SCHEMA, ENVELOPE_JSON_SCHEMA, NAME_JSON_SCHEMA, ORDER_JSON_SCHEMA


def assert_openai_strict_schema(schema: dict) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_order_schema_is_openai_strict():
    assert_openai_strict_schema(ORDER_JSON_SCHEMA)


def test_envelope_schema_is_openai_strict():
    assert_openai_strict_schema(ENVELOPE_JSON_SCHEMA)


def test_name_schema_is_openai_strict():
    assert_openai_strict_schema(NAME_JSON_SCHEMA)


def test_amounts_schema_is_strict():
    assert_openai_strict_schema(AMOUNTS_JSON_SCHEMA)
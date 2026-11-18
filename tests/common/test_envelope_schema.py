"""The committed JSON Schema stays in step with the Envelope model (WG-07)."""

import json
from pathlib import Path

from webhook_gateway.common.envelope import Envelope, envelope_json_schema
from webhook_gateway.common.write_schema import main

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILE = ROOT / "schemas" / "envelope.v1.json"


def test_committed_schema_matches_the_model() -> None:
    assert SCHEMA_FILE.read_text() == envelope_json_schema(), (
        "Envelope changed but schemas/envelope.v1.json didn't: run make schema and commit it"
    )


def test_schema_requires_every_field_and_allows_no_others() -> None:
    schema = json.loads(SCHEMA_FILE.read_text())

    assert set(schema["required"]) == set(Envelope.model_fields)
    assert schema["additionalProperties"] is False


def test_write_schema_writes_the_generated_text(tmp_path: Path) -> None:
    target = tmp_path / "schemas" / "envelope.v1.json"

    main([str(target)])

    assert target.read_text() == envelope_json_schema()

"""Write the envelope's JSON Schema to the path given: `make schema` runs this."""

import sys
from pathlib import Path

from webhook_gateway.common.envelope import envelope_json_schema


def main(argv: list[str]) -> None:
    (target,) = argv
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(envelope_json_schema())


if __name__ == "__main__":
    main(sys.argv[1:])

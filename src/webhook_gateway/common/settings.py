"""Runtime settings, read from environment variables that the SAM template sets.

.env.example documents every one. Nothing here is secret: the signing secrets stay
in SSM Parameter Store, under parameter_path.
"""

from typing import Annotated, Self

from pydantic import StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Stage = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$")]
ParameterPath = Annotated[str, StringConstraints(pattern=r"^(/[A-Za-z0-9_.-]+)+$")]
QueueUrl = Annotated[
    str,
    StringConstraints(
        pattern=r"^https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+$"
    ),
]
Name = Annotated[str, StringConstraints(min_length=1)]


class Settings(BaseSettings):
    """Each field reads the environment variable of the same name in capitals."""

    model_config = SettingsConfigDict(frozen=True)

    stage: Stage
    parameter_path: ParameterPath
    queue_url: QueueUrl
    dead_letter_queue_url: QueueUrl
    table_name: Name
    archive_bucket: Name

    @model_validator(mode="after")
    def _parameter_path_belongs_to_the_stage(self) -> Self:
        if not self.parameter_path.endswith(f"/{self.stage}"):
            raise ValueError("parameter_path must end with /<stage>")
        return self

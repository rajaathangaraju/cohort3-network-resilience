"""Environment-based settings; boto3 resolves credentials via standard provider chain."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    aws_region: str = "us-east-2"
    bedrock_model_id: str | None = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    bedrock_enabled: bool = False
    bedrock_timeout_seconds: int = 10
    bedrock_max_tokens: int = 512
    bedrock_temperature: float = 0.0
    agentcore_runtime_name: str | None = None
    agent1_runtime_arn: str = (
        "arn:aws:bedrock-agentcore:us-east-2:835156611439:runtime/"
        "cohort3_weather_network_intelligence-aRoVQk71fj"
    )
    agent2_runtime_arn: str | None = None
    command_center_data_mode: str = "mock"
    log_level: str = "INFO"
    use_mock_weather: bool = True
    use_mock_inventory: bool = True
    use_mock_remediation: bool = True
    require_human_approval: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

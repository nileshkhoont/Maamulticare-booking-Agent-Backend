from app.core.config import settings
from app.core.exceptions import AppError
from app.integrations.edesy.client import edesy_client
from app.integrations.edesy.schemas import EdesyAgentResponse, EdesyAgentUpdateRequest


class AgentConfigService:
    """Manages the Edesy agent (prompt, greeting, language, voice) — the backend proxy behind the
    frontend's settings/voice-agent page. The frontend never talks to Edesy directly.
    """

    def _agent_id(self) -> str:
        if not settings.edesy_agent_id:
            raise AppError(
                "AGENT_ID is not configured — create the voice agent and set its id in .env"
            )
        return settings.edesy_agent_id

    async def get_agent_config(self) -> EdesyAgentResponse:
        return await edesy_client.get_agent(self._agent_id())

    async def update_agent_config(
        self,
        prompt: str | None = None,
        greeting_message: str | None = None,
        language: str | None = None,
        voice: str | None = None,
    ) -> EdesyAgentResponse:
        payload = EdesyAgentUpdateRequest(
            prompt=prompt, greetingMessage=greeting_message, language=language, voice=voice
        )
        return await edesy_client.update_agent(self._agent_id(), payload)


agent_config_service = AgentConfigService()

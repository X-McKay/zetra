"""Gateway authentication around the SDK's single admitted model converter."""

from openai import AsyncOpenAI
from zetra._runtime.chat_model import ChatExecutor
from zetra._runtime.dispatch import ExecutionRefused

from agent_runtime.gateway import async_client_for
from agent_runtime.identity import BearerAuth, gateway_url, identity


class GatewayModel:
    def __init__(self, binding):
        self.binding = binding
        self.name = "openai/" + binding.name
        # The hash-bound envelope selects a backend; the gateway enforces its
        # signed policy. AGENT_MODEL_ROUTE is only the legacy default backend.
        self.base_url = f"{gateway_url()}/models/{binding.backend}/v1"

    async def __call__(self, payload, state, key):
        auth = BearerAuth(identity())

        def client():
            return AsyncOpenAI(
                base_url=self.base_url,
                api_key="gateway-managed",
                max_retries=0,
                timeout=30,
                http_client=async_client_for("AGENT_INFERENCE_HOST", 30, auth=auth),
            )

        execute = ChatExecutor(
            base_url=self.base_url,
            model=self.name,
            api_key="gateway-managed",
            max_tokens=self.binding.max_tokens,
            temperature=self.binding.temperature,
            timeout=30,
            client_factory=client,
        )
        try:
            return await execute(payload, state, key)
        except Exception:
            if auth.status in (403, 429):
                raise ExecutionRefused("policy" if auth.status == 403 else "budget") from None
            raise

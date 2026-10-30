"""OpenAI-compatible chat client bound to the inference route (spec 11.4)."""

from openai import OpenAI

from agent_runtime.gateway import inference
from agent_runtime.identity import env, gateway_url


def chat_client() -> OpenAI:
    # The model is selected by route, not by the body: AGENT_MODEL_ROUTE is /models/<backend>.
    # api_key is a placeholder; BearerAuth on the shared httpx client overwrites Authorization.
    base = f"{gateway_url()}{env('AGENT_MODEL_ROUTE')}/v1"
    return OpenAI(base_url=base, api_key="gateway-bearer", http_client=inference(), max_retries=0)

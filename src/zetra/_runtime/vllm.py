"""Pinned live profile using ADK's official LiteLlm conversion and client seam."""

import json
import os
from contextlib import aclosing

from google.adk.models.llm_request import LlmRequest

from .records import canonical_bytes, decode_model, digest, encode_model, thaw

API_BASE = "https://llm.almckay.io/v1"
MODEL = "openai/Qwen3.6-35B-A3B-NVFP4"
SETTINGS = {
    "api_base": API_BASE,
    "model": MODEL,
    "max_tokens": 512,
    "timeout": 20,
    "num_retries": 0,
    "temperature": 0,
    "parallel_tool_calls": False,
    "enable_thinking": False,
}
MANIFEST = "vllm-v1-" + digest(SETTINGS)


class VllmExecutor:
    def __init__(self, *, base_url=API_BASE):
        # An alternate URL is a transport seam for local HTTP conformance tests.
        # Session provisioning exposes only the pinned profile above.
        self.base_url = base_url

    async def __call__(self, payload, state, key):
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        litellm.telemetry = False
        from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
        from openai import AsyncOpenAI

        request = decode_model(LlmRequest, thaw(payload))
        if request.model != MODEL or request.config.http_options is not None:
            raise ValueError("unapproved model or transport options")
        if any(
            value is not None and name not in {"system_instruction", "tools", "labels"}
            for name, value in request.config.model_dump().items()
        ):
            raise ValueError("unapproved generation configuration")
        names = {
            d.name for tool in request.config.tools or [] for d in tool.function_declarations or []
        }

        class StrictClient(LiteLLMClient):
            async def acompletion(self, **kwargs):
                raw = await super().acompletion(**kwargs)
                if len(raw.choices) != 1 or raw.choices[0].finish_reason not in (
                    "stop",
                    "tool_calls",
                ):
                    raise ValueError("incomplete provider response")
                message = raw.choices[0].message
                calls = message.tool_calls or []
                if len(calls) > 1 or (not calls and not message.content):
                    raise ValueError("parallel or empty provider response")
                for call in calls:
                    if call.function.name not in names or not call.id:
                        raise ValueError("unknown tool or missing call identity")
                    args = json.loads(call.function.arguments)
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be an object")  # noqa: TRY004 -- protocol validation
                    canonical_bytes(args)  # Reject non-finite values before ADK can repair JSON.
                return raw

        async with AsyncOpenAI(
            base_url=self.base_url, api_key="local-vllm-no-auth", max_retries=0, timeout=20
        ) as client:
            model = LiteLlm(
                model=MODEL,
                llm_client=StrictClient(),
                client=client,
                api_base=self.base_url,
                api_key="local-vllm-no-auth",
                num_retries=0,
                timeout=20,
                max_tokens=512,
                temperature=0,
                parallel_tool_calls=False,
                drop_params=False,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            async with aclosing(model.generate_content_async(request, stream=False)) as responses:
                result = await anext(responses)
                if result.error_code or result.partial or result.interrupted:
                    raise ValueError("non-definitive model response")
                return encode_model(result)

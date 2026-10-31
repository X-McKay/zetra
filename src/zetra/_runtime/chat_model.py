"""Explicit OpenAI-compatible model execution through ADK's official converter."""

import json
import os
from contextlib import aclosing

from google.adk.models.llm_request import LlmRequest

from .records import canonical_bytes, decode_model, encode_model, thaw


class ChatExecutor:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        max_tokens: int = 512,
        timeout: float = 20,
        temperature: float = 0,
        extra_body: dict | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.temperature = temperature
        self.extra_body = extra_body

    async def __call__(self, payload, state, key):
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        litellm.telemetry = False
        from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
        from openai import AsyncOpenAI

        request = decode_model(LlmRequest, thaw(payload))
        if request.model != self.model or request.config.http_options is not None:
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
            base_url=self.base_url, api_key=self.api_key, max_retries=0, timeout=self.timeout
        ) as client:
            model = LiteLlm(
                model=self.model,
                llm_client=StrictClient(),
                client=client,
                api_base=self.base_url,
                api_key=self.api_key,
                num_retries=0,
                timeout=self.timeout,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                parallel_tool_calls=False,
                drop_params=False,
                extra_body=self.extra_body,
            )
            async with aclosing(model.generate_content_async(request, stream=False)) as responses:
                result = await anext(responses)
                if result.error_code or result.partial or result.interrupted:
                    raise ValueError("non-definitive model response")
                return encode_model(result)

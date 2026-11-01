"""Explicit OpenAI-compatible model execution through ADK's official converter."""

import json
import os
from contextlib import aclosing

from google.adk.models.llm_request import LlmRequest

from .model_diagnostics import ModelCallError
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
        client_factory=None,
    ):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.temperature = temperature
        self.extra_body = extra_body
        self.client_factory = client_factory

    async def __call__(self, payload, state, key):
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        litellm.telemetry = False
        from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

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
                try:
                    raw = await super().acompletion(**kwargs)
                except (APITimeoutError, litellm.Timeout) as exc:
                    raise ModelCallError("provider_timeout") from exc
                except (APIConnectionError, litellm.APIConnectionError) as exc:
                    raise ModelCallError("connection_error") from exc
                except (APIStatusError, litellm.APIError) as exc:
                    # LiteLLM also wraps connection/JSON failures in synthetic HTTP
                    # 500s. Only an underlying OpenAI HTTP exception proves status.
                    cause, seen = exc, set()
                    for _ in range(8):
                        if cause is None or id(cause) in seen:
                            break
                        seen.add(id(cause))
                        if isinstance(cause, APITimeoutError):
                            raise ModelCallError("provider_timeout") from exc
                        if isinstance(cause, APIConnectionError):
                            raise ModelCallError("connection_error") from exc
                        if isinstance(cause, json.JSONDecodeError):
                            raise ModelCallError("invalid_provider_response") from exc
                        if type(cause).__module__ == "openai" and isinstance(cause, APIStatusError):
                            status = cause.status_code
                            if type(status) is int and 100 <= status <= 599:
                                raise ModelCallError("http_error", http_status=status) from exc
                        cause = cause.__cause__ or cause.__context__
                    raise ModelCallError("provider_error") from exc
                if len(raw.choices) != 1:
                    raise ModelCallError("invalid_choices")
                finish = raw.choices[0].finish_reason
                if finish not in ("stop", "tool_calls"):
                    raise ModelCallError(
                        {"length": "response_truncated", "content_filter": "response_filtered"}.get(
                            finish, "invalid_finish_reason"
                        )
                    )
                message = raw.choices[0].message
                calls = message.tool_calls or []
                if len(calls) > 1:
                    raise ModelCallError("parallel_tool_calls")
                if not calls and not message.content:
                    raise ModelCallError("empty_response")
                for call in calls:
                    if call.function.name not in names:
                        raise ModelCallError("unknown_tool")
                    if not call.id:
                        raise ModelCallError("missing_tool_call_id")
                    try:
                        args = json.loads(call.function.arguments)
                        if not isinstance(args, dict):
                            raise ValueError("tool arguments must be an object")  # noqa: TRY004
                        canonical_bytes(args)  # Reject non-finite values before ADK repairs JSON.
                    except (TypeError, ValueError) as exc:
                        raise ModelCallError("invalid_tool_arguments") from exc
                return raw

        client_context = (
            self.client_factory()
            if self.client_factory is not None
            else AsyncOpenAI(
                base_url=self.base_url, api_key=self.api_key, max_retries=0, timeout=self.timeout
            )
        )
        async with client_context as client:
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
                    raise ModelCallError("non_definitive_response")
                return encode_model(result)

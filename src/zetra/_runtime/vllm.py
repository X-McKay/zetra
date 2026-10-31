"""Pinned live model profile; configuration remains production-owned."""

from .chat_model import ChatExecutor
from .records import digest

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


class VllmExecutor(ChatExecutor):
    def __init__(self, *, base_url=API_BASE):
        # An alternate URL is a transport seam for local HTTP conformance tests.
        # Session provisioning exposes only the pinned profile above.
        super().__init__(
            base_url=base_url,
            model=MODEL,
            api_key="local-vllm-no-auth",
            max_tokens=512,
            timeout=20,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

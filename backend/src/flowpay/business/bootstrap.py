"""业务工作流装配；SDK 和认证配置不进入编排层。"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

from flowpay.core.config import Settings
from flowpay.core.llm.embedding.ollama import retrieval_input
from flowpay.workflows.langchain_support import LangChainSupportWorkflow


def create_langchain_workflow(settings: Settings) -> LangChainSupportWorkflow:
    """按协议装配客户端；认证信息仅送往配置端点，禁用 SDK 自动重试。"""
    key = os.environ[settings.model.api_key_env]
    options = dict(settings.model.request_options)
    maximum = options.pop("max_tokens", 2048)
    if settings.model.provider == "anthropic-compatible":
        model = ChatAnthropic(model_name=settings.model.name, api_key=key,
            base_url=settings.model.base_url, max_retries=0,
            timeout=settings.model.timeout_seconds, max_tokens=maximum,
            thinking=options.pop("thinking", None), model_kwargs=options, streaming=False)
    elif settings.model.provider == "openai-compatible":
        headers = {"api-key": key} if urlsplit(settings.model.base_url).hostname == "api.hku.hk" else {}
        model = ChatOpenAI(model=settings.model.name, api_key=key, base_url=settings.model.base_url,
            default_headers=headers, max_retries=0, timeout=settings.model.timeout_seconds,
            max_tokens=maximum, extra_body=options, streaming=False, use_responses_api=False)
    else:
        raise ValueError("LangChain requires openai-compatible or anthropic-compatible model provider")
    return LangChainSupportWorkflow(model, retrieval_input)

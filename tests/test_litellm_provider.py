from nanobot.providers.litellm_provider import LiteLLMProvider


def test_litellm_provider_initializes_langsmith_flag(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")

    provider = LiteLLMProvider(default_model="openai/gpt-4o-mini")

    assert provider._langsmith_enabled is True


def test_litellm_provider_build_chat_kwargs_adds_langsmith_callback(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")

    provider = LiteLLMProvider(default_model="openai/gpt-4o-mini")
    kwargs, _ = provider._build_chat_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model=None,
        max_tokens=128,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    assert kwargs["callbacks"] == ["langsmith"]

from nanobot.channels.registry import discover_channel_names, load_channel_class


def test_builtin_channels_expose_default_config_dicts() -> None:
    for module_name in sorted(discover_channel_names()):
        try:
            channel_cls = load_channel_class(module_name)
        except ImportError:
            continue
        payload = channel_cls.default_config()
        assert isinstance(payload, dict), module_name
        assert "enabled" in payload, module_name

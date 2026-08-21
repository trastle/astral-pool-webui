from config import _str_or_none


def test_str_or_none_passes_through_strings():
    assert _str_or_none("hello") == "hello"


def test_str_or_none_stringifies_numeric_values():
    """Dynaconf auto-casts numeric-looking YAML/env values to int/float -
    this must convert them back to str for fields that need text."""
    assert _str_or_none(1234) == "1234"


def test_str_or_none_returns_none_for_none():
    """The case a code review caught: Dynaconf's settings.get(key, default)
    only applies `default` when the key is entirely absent, not when it's
    present but left blank (e.g. `mqtt.last_known_good_cache_file:` with
    nothing after it - the "leave blank for default" convention this repo
    uses elsewhere) - a present-but-blank key comes back as None, same as
    a genuinely absent one. Every settings.get() call in config.py that
    wants a real default must go through _str_or_none(...) or default
    rather than settings.get(key, default) directly, so None (blank or
    absent) always falls through to the same default."""
    assert _str_or_none(None) is None

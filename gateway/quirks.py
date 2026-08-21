"""Workarounds for pychlorinator parsing quirks, shared across modules."""


def decode_pool_volume(raw):
    """pychlorinator doesn't finish parsing this field - it comes back as raw
    little-endian bytes. Decoded manually; matches the value shown in the app."""
    if isinstance(raw, (bytes, bytearray)):
        return int.from_bytes(raw, byteorder="little")
    return raw


def format_time_of_day(td) -> str:
    """pychlorinator's pump timer start_time/stop_time are timedeltas
    representing time-since-midnight, not JSON-serializable as-is. Formats
    as 'HH:MM:SS' - the ISO time format Home Assistant's own `time` entity
    platform expects, so this stays reusable once timers become writable."""
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

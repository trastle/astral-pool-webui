"""Workarounds for pychlorinator parsing quirks, shared across modules."""


def decode_pool_volume(raw):
    """pychlorinator doesn't finish parsing this field - it comes back as raw
    little-endian bytes. Decoded manually; matches the value shown in the app."""
    if isinstance(raw, (bytes, bytearray)):
        return int.from_bytes(raw, byteorder="little")
    return raw

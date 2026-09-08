"""Small service boundary used by the real security scanner profile."""

from urllib.parse import urlsplit


def allowed_callback(url: str, allowed_hosts: set[str]) -> bool:
    """Accept HTTPS callbacks only when their normalized hostname is registered."""
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in allowed_hosts and parsed.username is None

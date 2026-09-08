"""Small dependency-free catalog library used by stage orchestration acceptance."""


def normalize_key(value: str) -> str:
    normalized = "-".join(value.strip().lower().split())
    if not normalized or not all(
        character.isalnum() or character == "-" for character in normalized
    ):
        raise ValueError("catalog keys need letters, digits, spaces, or hyphens")
    return normalized


def index(values: list[str]) -> dict[str, str]:
    result = {normalize_key(value): value.strip() for value in values}
    if len(result) != len(values):
        raise ValueError("catalog keys must be unique after normalization")
    return result

import shlex


def summary_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("summary:")]


def parse_summary_line(line: str) -> dict[str, str]:
    assert line.startswith("summary:")
    payload = line[len("summary:") :].strip()
    parsed: dict[str, str] = {}
    for token in shlex.split(payload):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parsed[key] = value
    return parsed


def assert_summary_fields(text: str, expected: dict[str, str], *, index: int = 0) -> None:
    lines = summary_lines(text)
    assert len(lines) > index, f"expected summary line at index {index}, got {lines!r}"
    parsed = parse_summary_line(lines[index])
    for key, expected_value in expected.items():
        assert parsed.get(key) == expected_value, (
            f"summary field mismatch for {key!r}: expected {expected_value!r}, got {parsed.get(key)!r}. "
            f"parsed={parsed!r}"
        )

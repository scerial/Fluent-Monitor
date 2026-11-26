import runpy
import sys

import pytest

from fluentmon import cli


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["fluentmon"], [["serve"]]),
        (["fluentmon", "--config", "x.toml"], [["serve", "--config", "x.toml"]]),
    ],
)
def test_main_module_starts_the_server(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: list[list[str]]
) -> None:
    calls: list[list[str]] = []

    def fake_main(values: list[str] | None = None) -> int:
        calls.append(list(values or []))
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_module("fluentmon", run_name="__main__")
    assert calls == expected

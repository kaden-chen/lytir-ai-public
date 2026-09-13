import pytest

from utils import is_truthy


@pytest.mark.parametrize(
    "value",
    [True, 1, "true", "TRUE", " 1 ", "yes", "enabled", "allowed", "approved"],
)
def test_is_truthy_accepts_truthy_values(value: object) -> None:
    assert is_truthy(value)


@pytest.mark.parametrize("value", [False, 0, "false", "FALSE", " 0 ", "no"])
def test_is_truthy_rejects_falsy_values(value: object) -> None:
    assert not is_truthy(value)


@pytest.mark.parametrize("value", [None, 2, 1.0, "", "sometimes", [], {}])
def test_is_truthy_treats_unsupported_values_as_false(value: object) -> None:
    assert not is_truthy(value)

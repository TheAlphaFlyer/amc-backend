import pytest

from amc.utils import compass_direction


@pytest.mark.parametrize(
    "dx,dy,expected",
    [
        (0, -100, "N"),   # North is −Y
        (-100, 0, "W"),   # West is −X
        (100, 0, "E"),    # East is +X
        (0, 100, "S"),    # South is +Y
        (100, -100, "NE"),  # diagonal
    ],
)
def test_compass_direction(dx, dy, expected):
    assert compass_direction(dx, dy) == expected

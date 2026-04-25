import pytest

from amc.utils import compass_direction, compass_heading


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


@pytest.mark.parametrize(
    "dx,dy,expected",
    [
        (0, -100, "0°N"),     # North is −Y
        (-100, 0, "270°W"),   # West is −X
        (100, 0, "90°E"),     # East is +X
        (0, 100, "180°S"),    # South is +Y
        (100, -100, "45°NE"), # diagonal
    ],
)
def test_compass_heading(dx, dy, expected):
    assert compass_heading(dx, dy) == expected

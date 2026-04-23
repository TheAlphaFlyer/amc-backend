from django.test import TestCase

from amc.api.player_positions_common import is_player_hidden, build_player_positions


class IsPlayerHiddenTest(TestCase):
    def test_star_player_is_hidden(self):
        player = {"PlayerName": "[*****C5] Yuuka", "VehicleKey": ""}
        self.assertTrue(is_player_hidden(player, has_star=False))
        self.assertTrue(is_player_hidden(player, has_star=True))

    def test_single_star_is_hidden(self):
        player = {"PlayerName": "[*] Test", "VehicleKey": ""}
        self.assertTrue(is_player_hidden(player, has_star=False))

    def test_p_tag_with_star_present_is_hidden(self):
        player = {"PlayerName": "[P10] GATE", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertTrue(is_player_hidden(player, has_star=True))

    def test_p_tag_without_star_present_is_visible(self):
        player = {"PlayerName": "[P1] Hello", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))

    def test_normal_player_is_visible(self):
        player = {"PlayerName": "Normal Player", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertFalse(is_player_hidden(player, has_star=True))

    def test_bracket_without_star_is_visible(self):
        player = {"PlayerName": "[Star] Player", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertFalse(is_player_hidden(player, has_star=True))

    def test_no_space_after_bracket_is_visible(self):
        player = {"PlayerName": "[P10]FakeYuuka", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertFalse(is_player_hidden(player, has_star=True))

    def test_star_no_space_after_bracket_is_visible(self):
        player = {"PlayerName": "[*****C5]Fake", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertFalse(is_player_hidden(player, has_star=True))

    def test_p_tag_with_only_space_after_bracket(self):
        player = {"PlayerName": "[P10] ", "VehicleKey": ""}
        self.assertFalse(is_player_hidden(player, has_star=False))
        self.assertTrue(is_player_hidden(player, has_star=True))

    def test_star_with_only_space_after_bracket(self):
        player = {"PlayerName": "[*****C5] ", "VehicleKey": ""}
        self.assertTrue(is_player_hidden(player, has_star=False))


class BuildPlayerPositionsTest(TestCase):
    def _make_player(self, name, x=1.0, y=2.0, z=3.0, vehicle_key=""):
        return {
            "UniqueID": 12345,
            "PlayerName": name,
            "Location": {"X": x, "Y": y, "Z": z},
            "VehicleKey": vehicle_key,
        }

    def test_visible_player_has_coords(self):
        players = [self._make_player("Normal Player", x=10.0, y=20.0, z=30.0)]
        result = build_player_positions(players)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["player_name"], "Normal Player")
        self.assertEqual(result[0]["x"], 10.0)
        self.assertEqual(result[0]["y"], 20.0)
        self.assertEqual(result[0]["z"], 30.0)
        self.assertFalse(result[0]["hidden"])

    def test_star_player_gets_zero_coords(self):
        players = [self._make_player("[*****C5] Yuuka", x=10.0, y=20.0, z=30.0)]
        result = build_player_positions(players)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["player_name"], "[*****C5] Yuuka")
        self.assertEqual(result[0]["x"], 0)
        self.assertEqual(result[0]["y"], 0)
        self.assertEqual(result[0]["z"], 0)
        self.assertTrue(result[0]["hidden"])

    def test_p_tag_hidden_when_star_present(self):
        players = [
            self._make_player("[*****C5] Yuuka", x=1.0, y=1.0, z=1.0),
            self._make_player("[P10] GATE", x=10.0, y=20.0, z=30.0),
        ]
        result = build_player_positions(players)
        self.assertEqual(len(result), 2)

        # Star player is hidden
        self.assertTrue(result[0]["hidden"])
        self.assertEqual(result[0]["x"], 0)

        # P-tag player is also hidden because star exists
        self.assertTrue(result[1]["hidden"])
        self.assertEqual(result[1]["x"], 0)
        self.assertEqual(result[1]["y"], 0)
        self.assertEqual(result[1]["z"], 0)

    def test_p_tag_visible_when_no_star(self):
        players = [
            self._make_player("[P10] GATE", x=10.0, y=20.0, z=30.0),
            self._make_player("Normal Player", x=5.0, y=5.0, z=5.0),
        ]
        result = build_player_positions(players)
        self.assertEqual(len(result), 2)

        # P-tag player is visible when no star present
        self.assertFalse(result[0]["hidden"])
        self.assertEqual(result[0]["x"], 10.0)

        # Normal player is also visible
        self.assertFalse(result[1]["hidden"])
        self.assertEqual(result[1]["x"], 5.0)

    def test_mixed_players(self):
        players = [
            self._make_player("Normal", x=1.0, y=1.0, z=1.0),
            self._make_player("[*****C5] Star", x=2.0, y=2.0, z=2.0),
            self._make_player("[P1] Tagged", x=3.0, y=3.0, z=3.0),
            self._make_player("AlsoNormal", x=4.0, y=4.0, z=4.0),
        ]
        result = build_player_positions(players)

        # Normal players visible
        self.assertFalse(result[0]["hidden"])
        self.assertEqual(result[0]["x"], 1.0)
        self.assertFalse(result[3]["hidden"])
        self.assertEqual(result[3]["x"], 4.0)

        # Star player hidden
        self.assertTrue(result[1]["hidden"])
        self.assertEqual(result[1]["x"], 0)

        # P-tag player hidden because star exists
        self.assertTrue(result[2]["hidden"])
        self.assertEqual(result[2]["x"], 0)

    def test_vehicle_key_preserved(self):
        players = [self._make_player("Normal", vehicle_key="TUSCAN")]
        result = build_player_positions(players)
        self.assertEqual(result[0]["vehicle_key"], "TUSCAN")

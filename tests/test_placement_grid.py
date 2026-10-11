import unittest

from placement_grid import assign_positions, build_layout, swap_positions


class PlacementGridTests(unittest.TestCase):
    def test_assign_positions_reconstructs_existing_neighbor_layout(self):
        positions = assign_positions(
            ["10.0.0.2", "10.0.0.3"],
            {
                "server": {"right": "10.0.0.2"},
                "10.0.0.2": {"left": "server", "top": "10.0.0.3"},
                "10.0.0.3": {"bottom": "10.0.0.2"},
            },
        )

        self.assertEqual(positions["server"], (1, 1))
        self.assertEqual(positions["10.0.0.2"], (1, 2))
        self.assertEqual(positions["10.0.0.3"], (0, 2))

    def test_assign_positions_uses_default_neighbor_cells_when_no_layout_exists(self):
        positions = assign_positions(["a", "b", "c"])

        self.assertEqual(positions, {
            "server": (1, 1),
            "a": (1, 2),
            "b": (1, 0),
            "c": (0, 1),
        })

    def test_existing_positions_are_preserved_and_stale_machines_removed(self):
        positions = assign_positions(
            ["a", "b"],
            previous={"server": (1, 1), "a": (2, 2), "gone": (0, 0)},
        )

        self.assertEqual(positions["a"], (2, 2))
        self.assertNotIn("gone", positions)
        self.assertIn("b", positions)

    def test_build_layout_contains_only_adjacent_machine_pairs(self):
        layout = build_layout({
            "server": (1, 1),
            "right": (1, 2),
            "top": (0, 1),
            "corner": (0, 0),
        })

        self.assertEqual(layout["server"], {"right": "right", "top": "top"})
        self.assertEqual(layout["right"]["left"], "server")
        self.assertEqual(layout["top"]["bottom"], "server")
        self.assertNotIn("corner", layout.get("server", {}).values())

    def test_swap_positions_swaps_occupant(self):
        positions = {"server": (1, 1), "a": (1, 2), "b": (0, 1)}

        swapped = swap_positions(positions, "a", (0, 1))

        self.assertEqual(swapped["a"], (0, 1))
        self.assertEqual(swapped["b"], (1, 2))
        self.assertEqual(positions["a"], (1, 2))

    def test_swap_positions_rejects_out_of_range_and_unknown_machine(self):
        positions = {"server": (1, 1)}

        with self.assertRaises(ValueError):
            swap_positions(positions, "server", (3, 1))
        with self.assertRaises(KeyError):
            swap_positions(positions, "unknown", (1, 1))


if __name__ == "__main__":
    unittest.main()
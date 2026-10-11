"""Pure helpers for the control panel's 3x3 screen-placement editor."""

from collections.abc import Iterable, Mapping

GRID_SIZE = 3
CENTER_CELL = (1, 1)
DEFAULT_CLIENT_CELLS = (
    (1, 2),
    (1, 0),
    (0, 1),
    (2, 1),
    (0, 0),
    (0, 2),
    (2, 0),
    (2, 2),
)
DIRECTIONS = {
    "left": (0, -1),
    "right": (0, 1),
    "top": (-1, 0),
    "bottom": (1, 0),
}

Cell = tuple[int, int]


def _valid_cell(cell: Cell) -> bool:
    return (
        isinstance(cell, tuple)
        and len(cell) == 2
        and all(isinstance(value, int) for value in cell)
        and 0 <= cell[0] < GRID_SIZE
        and 0 <= cell[1] < GRID_SIZE
    )


def assign_positions(
    machine_ids: Iterable[str],
    layout: Mapping[str, Mapping[str, str]] | None = None,
    previous: Mapping[str, Cell] | None = None,
) -> dict[str, Cell]:
    """Restore known positions, infer neighbors, then fill the first free cells.

    The server is placed at center by default. Existing in-memory positions are
    kept across state broadcasts/reconnects, while a first connection can infer
    positions from the server's current adjacency layout.
    """
    ordered_ids = ["server"]
    ordered_ids.extend(machine_id for machine_id in machine_ids if machine_id != "server")
    ordered_ids = list(dict.fromkeys(ordered_ids))
    known = set(ordered_ids)
    layout = layout or {}
    previous = previous or {}

    positions: dict[str, Cell] = {}
    occupied: dict[Cell, str] = {}
    for machine_id in ordered_ids:
        cell = previous.get(machine_id)
        if cell is None or not _valid_cell(cell) or cell in occupied:
            continue
        positions[machine_id] = cell
        occupied[cell] = machine_id

    if "server" not in positions:
        positions["server"] = CENTER_CELL
        occupied[CENTER_CELL] = "server"

    # Recover the grid from the server's adjacency graph, keeping any already
    # known UI positions stable when subsequent state messages arrive.
    frontier = ["server"]
    visited = set()
    while frontier:
        machine_id = frontier.pop(0)
        if machine_id in visited:
            continue
        visited.add(machine_id)
        origin = positions.get(machine_id)
        if origin is None:
            continue
        for direction, (dr, dc) in DIRECTIONS.items():
            neighbor = layout.get(machine_id, {}).get(direction)
            if neighbor not in known or neighbor in positions:
                continue
            cell = (origin[0] + dr, origin[1] + dc)
            if _valid_cell(cell) and cell not in occupied:
                positions[neighbor] = cell
                occupied[cell] = neighbor
                frontier.append(neighbor)

    for machine_id in ordered_ids:
        if machine_id in positions:
            continue
        cell = next((candidate for candidate in DEFAULT_CLIENT_CELLS if candidate not in occupied), None)
        if cell is None:
            break
        positions[machine_id] = cell
        occupied[cell] = machine_id

    return positions


def build_layout(positions: Mapping[str, Cell]) -> dict[str, dict[str, str]]:
    """Create the server protocol's directional adjacency map from grid cells."""
    cell_occupants = {cell: machine_id for machine_id, cell in positions.items()}
    result: dict[str, dict[str, str]] = {}
    for machine_id, (row, column) in positions.items():
        for direction, (dr, dc) in DIRECTIONS.items():
            neighbor = cell_occupants.get((row + dr, column + dc))
            if neighbor is not None:
                result.setdefault(machine_id, {})[direction] = neighbor
    return result


def swap_positions(
    positions: Mapping[str, Cell],
    machine_id: str,
    target: Cell,
) -> dict[str, Cell]:
    """Move one machine to a cell, swapping with its occupant when necessary."""
    if machine_id not in positions:
        raise KeyError(machine_id)
    if not _valid_cell(target):
        raise ValueError(f"Cell must be inside the {GRID_SIZE}x{GRID_SIZE} grid.")

    updated = dict(positions)
    source = updated[machine_id]
    occupant = next(
        (other for other, cell in updated.items() if other != machine_id and cell == target),
        None,
    )
    updated[machine_id] = target
    if occupant is not None:
        updated[occupant] = source
    return updated

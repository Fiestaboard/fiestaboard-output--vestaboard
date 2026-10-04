"""The Vestaboard's board-settings actions (``manifest.json`` ``output.actions``).

FiestaBoard runs a declared action through :meth:`VestaboardOutput.handle_action
<.output.VestaboardOutput.handle_action>` with an action context: the board
(saved, or a draft before it exists), its ``output_config`` with every
secret restored, the action's input, and what core lends an action — a
throwaway instance built from the settings (or from other settings: one
Note of an array), the board's live instance under its send lock, a reader
of what the board shows, and a way to make core re-send the board's content.

The same runner answers FiestaBoard's legacy routes, which predate the
action envelope — ``POST /config/board/test``, ``/config/board/enable-local-api``,
``/settings/board/{id}/identify`` and ``/settings/board/{id}/detect-size``:
each outcome carries its raw answer in ``detail`` for them (never rendered,
never logged), so one implementation serves both doors.

Refusals made before any device is contacted are ``OutputActionError`` (a
4xx, the message the legacy routes have always given); what the device said
is the outcome.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from src.plugins import (
    ActionField,
    ActionOutcome,
    CancelToken,
    OutputActionError,
    validate_board_host,
    validate_board_host_is_local_network,
)

from . import discovery as _discovery
from . import local_api as _local_api
from .settings import VestaboardSettings, is_note_array
from .tiles import identify_pattern
from .transport import LOCAL_API_PORT

logger = logging.getLogger(__name__)

#: Scan length bounds, the same as ``POST /config/board/scan``.
_SCAN_DEFAULT_S, _SCAN_MIN_S, _SCAN_MAX_S = 4.0, 1.0, 15.0


def _settings(ctx: Any) -> tuple[VestaboardSettings, str, int, int]:
    board = ctx.board
    device_type = board.get("device_type") or ctx.config.get("device_type") or "flagship"
    settings = VestaboardSettings.from_config(ctx.config, device_type)

    def axis(name: str) -> int:
        value = board.get(name, ctx.config.get(name))
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1

    return settings, device_type, axis("notes_wide"), axis("notes_tall")


def _not_configured(ctx: Any) -> OutputActionError:
    if ctx.board_id is not None:
        return OutputActionError(400, f"Board {ctx.board_id} is not configured (missing credentials)")
    return OutputActionError(400, "Enter the board's connection details first.")


def _outcome_from_check(check: Any) -> ActionOutcome:
    outcome = ActionOutcome.from_check(check)
    return ActionOutcome(
        status=outcome.status,
        message=outcome.message,
        guidance=outcome.guidance,
        detail=check.to_verdict(),
    )


# --- test_connection --------------------------------------------------------------------


def test_connection(ctx: Any) -> ActionOutcome:
    """Probe the board once. A Flagship or Note is refused before anything is
    contacted when the selected mode's credential (or the host) is missing;
    a note array is probed however it is reached."""
    settings, device_type, _, _ = _settings(ctx)
    if not is_note_array(device_type):
        if settings.api_mode == "cloud":
            if not settings.cloud_key:
                raise OutputActionError(400, "Cloud API key is required")
        else:
            if not settings.local_api_key:
                raise OutputActionError(400, "Local API key is required")
            if not settings.host:
                raise OutputActionError(400, "Board host/IP is required for Local API")
            # The host guard is why this cannot be pointed at an arbitrary
            # URL; its 400 propagates unchanged (#1887).
            validate_board_host(settings.host)
    try:
        instance = ctx.instance()
    except ValueError as exc:
        raise OutputActionError(400, "Board connection configuration is invalid.") from exc
    if instance is None:
        raise _not_configured(ctx)
    return _outcome_from_check(instance.check_connection())


def test_tile(ctx: Any) -> ActionOutcome:
    """Probe one Note of a local array: the address and key typed for it, or
    (its key saved, so masked) the stored tile at that position."""
    inputs = ctx.inputs
    host, key = inputs.get("host"), inputs.get("local_api_key")
    port = inputs.get("port")
    if not key:
        settings, _, _, _ = _settings(ctx)
        stored = next(
            (t for t in settings.tiles if t["row"] == inputs.get("row") and t["col"] == inputs.get("col")),
            None,
        )
        if stored is None or not stored["local_api_key"]:
            raise OutputActionError(400, "Local API key is required")
        host = host or stored["host"]
        key = stored["local_api_key"]
        port = port or stored["port"]
    if not host:
        raise OutputActionError(400, "Board host/IP is required for Local API")
    validate_board_host(host)
    # A host and a key always make a connection: the instance is never None.
    instance = ctx.instance(
        {"api_mode": "local", "device_type": "note", "host": host, "port": port, "local_api_key": key}
    )
    return _outcome_from_check(instance.check_connection())


# --- discover ---------------------------------------------------------------------------


def discover(ctx: Any) -> ActionOutcome:
    """Vestaboards on this network (mDNS, then a subnet probe of the Local API port)."""
    raw = ctx.inputs.get("timeout", _SCAN_DEFAULT_S)
    timeout = min(max(float(raw), _SCAN_MIN_S), _SCAN_MAX_S)
    devices = _discovery.discover(timeout)
    message = f"Found {len(devices)} board(s)." if devices else "No boards found."
    return ActionOutcome(message=message, devices=tuple(devices))


# --- identify ---------------------------------------------------------------------------


def _identify_targets(
    settings: VestaboardSettings, notes_wide: int, notes_tall: int, inputs: Mapping[str, Any]
) -> tuple[list[dict], bool]:
    """``(targets, override)``: the tiles to flash and whether they are unsaved."""
    target = inputs.get("target", "tile")
    if target not in ("tile", "all"):
        raise OutputActionError(400, 'target must be "tile" or "all"')
    row, col = inputs.get("row"), inputs.get("col")
    if inputs.get("host") is not None or inputs.get("local_api_key") is not None:
        # An unsaved tile, from its assignment dialog.
        if target != "tile" or row is None or col is None:
            raise OutputActionError(400, "Credential override requires target='tile' with row and col")
        if not inputs.get("host") or not inputs.get("local_api_key"):
            raise OutputActionError(400, "host and local_api_key are both required")
        validate_board_host(inputs["host"])
        validate_board_host_is_local_network(inputs["host"])
        tile = {
            "row": row,
            "col": col,
            "host": inputs["host"],
            "port": inputs.get("port") or LOCAL_API_PORT,
            "local_api_key": inputs["local_api_key"],
        }
        return [tile], True
    configured = settings.configured_tiles(notes_wide, notes_tall)
    if target == "tile":
        if row is None or col is None:
            raise OutputActionError(400, "row and col are required for target='tile'")
        configured = [t for t in configured if t["row"] == row and t["col"] == col]
        if not configured:
            raise OutputActionError(400, f"No configured tile at row={row}, col={col}")
    if not configured:
        raise OutputActionError(400, "Board has no configured tiles to identify")
    return configured, False


def _flash_unsaved(ctx: Any, tile: dict, notes_wide: int) -> bool:
    note = {
        "api_mode": "local",
        "device_type": "note",
        "host": tile["host"],
        "port": tile.get("port"),
        "local_api_key": tile["local_api_key"],
    }
    try:
        instance = ctx.instance(note)  # a host and a key: never None
        instance.forced = True
        pattern = identify_pattern(tile["row"], tile["col"], notes_wide)
        return bool(instance.write(pattern, native=None, cancel=CancelToken()).success)
    except Exception as exc:  # a failed flash is reported, not raised
        logger.error("Identify failed for tile (%s,%s): %s", tile["row"], tile["col"], type(exc).__name__)
        return False


def _flash_saved(ctx: Any, live: Any, positions: list[tuple[int, int]]) -> dict:
    flash = getattr(live, "identify_tiles", None)
    if flash is None:  # the live board is not (yet) a local tile array
        raise OutputActionError(503, f"Board client not initialized: {ctx.board_id}")
    return flash(positions)


def identify(ctx: Any) -> ActionOutcome:
    """Flash slot positions onto local note-array tiles.

    Saved tiles flash through the board's live instance, as a write of its
    runtime (it holds the send lock, so it never interleaves with the
    engine's sends); an unsaved tile through a throwaway instance. Either
    way core then re-sends the board's real content on its next cycle.
    """
    settings, device_type, notes_wide, notes_tall = _settings(ctx)
    if not is_note_array(device_type) or settings.api_mode != "local":
        raise OutputActionError(400, "Identify is only available for note arrays in local API mode")
    targets, override = _identify_targets(settings, notes_wide, notes_tall, ctx.inputs)
    positions = [(t["row"], t["col"]) for t in targets]
    if override:
        flashed = {(t["row"], t["col"]): _flash_unsaved(ctx, t, notes_wide) for t in targets}
    else:
        flashed = ctx.with_live(lambda live: _flash_saved(ctx, live, positions))
    ctx.invalidate()
    results = [{"row": r, "col": c, "success": bool(flashed.get((r, c)))} for r, c in positions]
    failed = [f"Tile {r['row'] + 1},{r['col'] + 1} did not respond." for r in results if not r["success"]]
    detail = {"results": results}
    if failed and len(failed) == len(results):
        return ActionOutcome(status="error", message="No tile responded.", guidance=tuple(failed), detail=detail)
    if failed:
        return ActionOutcome(
            status="warning", message="Some tiles did not respond.", guidance=tuple(failed), detail=detail
        )
    return ActionOutcome(message=f"Identified {len(results)} tile(s).", detail=detail)


# --- detect_geometry ----------------------------------------------------------------------


def detect_geometry(ctx: Any) -> ActionOutcome:
    """Read what the board shows and say what size of board that is.

    A local note array's size is defined by its tile assignments — a local
    read can only re-stitch the configured W×H — so it is refused. ``detail``
    carries the read grid's ``rows``/``cols`` when the size is not a
    Vestaboard's.
    """
    settings, device_type, _, _ = _settings(ctx)
    if settings.uses_local_tiles(device_type):
        raise OutputActionError(
            400,
            "Auto-detect is not available for local-mode note arrays — "
            "the array's size is defined by its tile assignments",
        )
    from .output import classify_grid

    read = ctx.reader()
    if read is None:
        raise _not_configured(ctx)
    grid = read()
    if grid is None:
        return ActionOutcome(status="error", message="The board returned no layout — it may be blank or unreachable.")
    rows = len(grid)
    cols = len(grid[0]) if rows > 0 else 0
    geometry = classify_grid(rows, cols)
    if geometry is None:
        return ActionOutcome(
            status="error",
            message=f"The board returned a grid FiestaBoard cannot classify ({rows}×{cols}).",
            detail={"rows": rows, "cols": cols},
        )
    return ActionOutcome(message="Size detected.", geometry=geometry)


# --- enable_local_api -----------------------------------------------------------------------


def enable_local_api(ctx: Any) -> ActionOutcome:
    """Exchange a Local API enablement token for a Local API key (the key
    comes back secret, and fills the key field)."""
    host = ctx.inputs.get("host") or ctx.config.get("host") or ""
    request = SimpleNamespace(host=host, enablement_token=ctx.inputs.get("enablement_token") or "")
    verdict = asyncio.run(_local_api.exchange_enablement_token(request))
    if not verdict.get("success"):
        guidance = (verdict["error"],) if verdict.get("error") else ()
        return ActionOutcome(status="error", message=verdict.get("message", ""), guidance=guidance, detail=verdict)
    return ActionOutcome(
        message=verdict.get("message", ""),
        fields={"api_key": ActionField(value=verdict.get("api_key"), secret=True)},
        detail=verdict,
    )


RUNNERS = {
    "test_connection": test_connection,
    "test_tile": test_tile,
    "discover": discover,
    "identify": identify,
    "detect_geometry": detect_geometry,
    "enable_local_api": enable_local_api,
}

"""A Vestaboard board's settings, as FiestaBoard stores and shows them.

Settings v4 keeps a Vestaboard's connection — ``api_mode``, ``host``,
``port``, ``local_api_key``, ``cloud_key``, ``note_array_token`` and a local
note array's ``tiles`` — in the board's ``output_config``. FiestaBoard core
asks this module (through :class:`~.output.VestaboardOutput`'s settings
hooks) everything it needs to know about them, so core holds no Vestaboard
rules of its own:

- :func:`normalize` — the stored form of a board's config (what
  ``BoardInstance`` applied to its flat fields through settings v3);
- :func:`status` — whether the board has the details a driver needs
  (the board card's Connected / Not configured badge, and first-run
  detection: any detail at all means the install is set up);
- :func:`mask` / :func:`restore` — the ``"***"`` rules for its credentials,
  including each tile's key, matched to the stored tile by endpoint and then
  by grid position (tiles carry no id, so a schema-driven unmask cannot);
- :data:`LEGACY_FLAT_FIELDS` — the settings-v3 flat fields every API view of
  a board still carries, with their defaults.

Nothing here talks to a board.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .connection import CONNECTION_FIELDS, _notes, normalize_tiles
from .transport import LOCAL_API_PORT

#: The board-level credentials (masked as ``"***"`` in every API view).
SECRET_FIELDS = frozenset({"local_api_key", "cloud_key", "note_array_token"})

#: The per-tile credential of a local note array.
TILE_SECRET_FIELDS = frozenset({"local_api_key"})

#: Every connection field's value when unset, in the settings-v3 order:
#: the flat view FiestaBoard's APIs still answer with.
LEGACY_FLAT_FIELDS: dict[str, Any] = {
    "api_mode": "local",
    "host": "",
    "port": LOCAL_API_PORT,
    "local_api_key": "",
    "cloud_key": "",
    "note_array_token": "",
    "tiles": [],
}
assert tuple(LEGACY_FLAT_FIELDS) == CONNECTION_FIELDS

#: The ``api_mode`` values a board may store; anything else reads as local.
API_MODES = ("local", "cloud", "virtual")

MASKED = "***"


def _port(value: Any) -> int:
    if value is None:
        return LOCAL_API_PORT
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return LOCAL_API_PORT


def _text(config: Mapping[str, Any], name: str) -> str:
    value = config.get(name, "")
    return value if value is not None else ""


def is_note_array(device_type: Any) -> bool:
    return device_type == "note_array"


@dataclass
class VestaboardSettings:
    """One Vestaboard board's normalised connection settings."""

    api_mode: str = "local"
    host: str = ""
    port: int = LOCAL_API_PORT
    local_api_key: str = ""
    cloud_key: str = ""
    note_array_token: str = ""
    tiles: list = field(default_factory=list)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None, device_type: str) -> VestaboardSettings:
        """The settings a board's ``output_config`` describes, normalised.

        An unknown ``api_mode`` is ``"local"``; ``port`` is an int (the Local
        API's when unusable); the note-array token is stripped; tiles are
        kept only on a note array and normalised.
        """
        config = config if isinstance(config, Mapping) else {}
        api_mode = config.get("api_mode", "local")
        return cls(
            api_mode=api_mode if api_mode in API_MODES else "local",
            host=_text(config, "host"),
            port=_port(config.get("port")),
            local_api_key=_text(config, "local_api_key"),
            cloud_key=_text(config, "cloud_key"),
            note_array_token=(config.get("note_array_token") or "").strip(),
            tiles=normalize_tiles(config.get("tiles") or []) if is_note_array(device_type) else [],
        )

    def to_config(self) -> dict[str, Any]:
        """The ``output_config`` these settings are stored as."""
        return {
            "api_mode": self.api_mode,
            "host": self.host,
            "port": self.port,
            "local_api_key": self.local_api_key,
            "cloud_key": self.cloud_key,
            "note_array_token": self.note_array_token,
            "tiles": [dict(t) for t in self.tiles],
        }

    def uses_local_tiles(self, device_type: str) -> bool:
        """True when this note array is driven tile by tile over the Local API.

        Requires BOTH ``api_mode == "local"`` and at least one saved tile:
        array boards created without an explicit ``api_mode`` default to
        ``"local"`` but carry only a cloud token — those keep driving through
        the cloud.
        """
        return is_note_array(device_type) and self.api_mode == "local" and bool(self.tiles)

    def configured_tiles(self, notes_wide: int, notes_tall: int) -> list[dict]:
        """Tiles in range for the W×H, enabled, and credentialed: the ones a
        driver can actually drive (and identify can flash)."""
        return [
            t
            for t in self.tiles
            if t["row"] < notes_tall and t["col"] < notes_wide and t["enabled"] and t["host"] and t["local_api_key"]
        ]

    def is_configured(self, device_type: str, notes_wide: int, notes_tall: int) -> bool:
        """Whether a driver can be built from these settings."""
        if self.api_mode == "virtual":
            return True
        if is_note_array(device_type):
            if self.uses_local_tiles(device_type):
                # A partial array is usable: assigned tiles receive their
                # slice, unassigned slots simply stay dark. Requiring every
                # slot would flip a half-assembled array back to
                # "unconfigured" and could re-trigger first-run detection.
                return bool(self.configured_tiles(notes_wide, notes_tall))
            # notes_wide/notes_tall are always >= 1, so configuration
            # hinges solely on having a token.
            return bool(self.note_array_token)
        if self.api_mode == "cloud":
            return bool(self.cloud_key)
        return bool(self.local_api_key and self.host)

    def has_attempt(self) -> bool:
        """True when the user has entered ANY connection detail.

        Deliberately weaker than :meth:`is_configured`: a board with a host
        but no key (or a note array missing its token) is *misconfigured*,
        not *unconfigured* — it must surface as a per-board error, never flip
        a working install back into the setup wizard.
        """
        if self.api_mode == "virtual":
            return True
        return bool(self.host or self.local_api_key or self.cloud_key or self.note_array_token or self.tiles)


def _geometry(board: Mapping[str, Any]) -> tuple[str, int, int]:
    return board.get("device_type") or "flagship", _notes(board.get("notes_wide")), _notes(board.get("notes_tall"))


def normalize(config: Mapping[str, Any] | None, board: Mapping[str, Any]) -> dict[str, Any]:
    """The ``output_config`` a board stores: every connection field, normalised."""
    device_type, _, _ = _geometry(board)
    return VestaboardSettings.from_config(config, device_type).to_config()


def status(config: Mapping[str, Any] | None, board: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(configured, attempted)`` for a board: whether a driver can be built
    from its settings, and whether the user entered any connection detail."""
    device_type, notes_wide, notes_tall = _geometry(board)
    settings = VestaboardSettings.from_config(config, device_type)
    return settings.is_configured(device_type, notes_wide, notes_tall), settings.has_attempt()


def restore(target: dict, stored: Mapping[str, Any]) -> dict:
    """Restore every echoed ``"***"`` credential in *target* from *stored*, in place.

    Both are Vestaboard connections (an ``output_config``, or the flat
    fields of a legacy write). A board-level secret echoed as ``"***"``
    takes the stored value (``""`` when there is none). Each tile's key is
    matched to the stored tile by host:port FIRST, so the key follows the
    physical board when tiles are moved or swapped to new grid positions (the
    settings screen's "Move to" sends masked keys at the NEW coordinates — a
    position-only match would pair each host with the OTHER board's key),
    then by (row, col) for the change-the-IP-keep-the-key flow.
    """
    for key in SECRET_FIELDS:
        if target.get(key) == MASKED:
            target[key] = stored.get(key, "") or ""
    incoming_tiles = target.get("tiles")
    if isinstance(incoming_tiles, list):
        stored_tiles = [t for t in stored.get("tiles") or [] if isinstance(t, dict)]
        by_pos = {(t.get("row"), t.get("col")): t for t in stored_tiles}
        by_endpoint: dict = {}
        for t in stored_tiles:
            by_endpoint.setdefault((t.get("host"), t.get("port")), t)
        for tile in incoming_tiles:
            if not isinstance(tile, dict):
                continue
            stored_tile = by_endpoint.get((tile.get("host"), tile.get("port"))) or by_pos.get(
                (tile.get("row"), tile.get("col")), {}
            )
            for key in TILE_SECRET_FIELDS:
                if tile.get(key) == MASKED:
                    tile[key] = stored_tile.get(key, "")
    return target


def mask(config: Mapping[str, Any]) -> dict:
    """A copy of a Vestaboard connection with every set credential ``"***"``.

    Tiles are rebuilt, never mutated, so masking a shallow copy cannot
    corrupt the stored dicts.
    """
    masked = dict(config)
    for key in SECRET_FIELDS:
        if masked.get(key):
            masked[key] = MASKED
    tiles = masked.get("tiles")
    if isinstance(tiles, list):
        masked["tiles"] = [
            {**tile, **{k: MASKED for k in TILE_SECRET_FIELDS if tile.get(k)}} if isinstance(tile, dict) else tile
            for tile in tiles
        ]
    return masked


def masked_paths(config: Mapping[str, Any]) -> list[str]:
    """Every credential in *config* that is ``"***"`` — a draft has nothing to restore it from."""
    found = [key for key in sorted(SECRET_FIELDS) if config.get(key) == MASKED]
    for i, tile in enumerate(config.get("tiles") or []):
        if isinstance(tile, Mapping):
            found += [f"tiles[{i}].{key}" for key in sorted(TILE_SECRET_FIELDS) if tile.get(key) == MASKED]
    return found

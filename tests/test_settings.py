"""A Vestaboard board's settings as FiestaBoard stores and shows them.

FiestaBoard core interprets none of a Vestaboard's settings: it asks the
plugin class (``normalize_config``, ``board_status``, ``mask_config``,
``restore_config``, ``masked_config_paths``, ``legacy_flat_fields``). These
pin the rules core used to hold itself (``src/outputs/vestaboard/connection.py``
before FiestaBoard Phase 4 P4e), moved here unchanged.
"""

from __future__ import annotations

from plugins.vestaboard import VestaboardOutput
from plugins.vestaboard.connection import CONNECTION_FIELDS, normalize_tiles
from plugins.vestaboard.settings import (
    LEGACY_FLAT_FIELDS,
    VestaboardSettings,
    mask,
    masked_paths,
    normalize,
    restore,
    status,
)
from src.plugins import OutputStatus


def _tile(row=0, col=0, **kw):
    return {"row": row, "col": col, "host": "10.0.0.1", "port": 7000, "local_api_key": "key", "enabled": True, **kw}


def _array(**config):
    return {"device_type": "note_array", "notes_wide": 2, "notes_tall": 1}, {"api_mode": "local", **config}


# --- tile normalisation (moved from FiestaBoard core's tests/test_devices.py) -----------


class TestNormalizeTiles:
    def test_non_list_returns_empty(self):
        assert normalize_tiles(None) == []
        assert normalize_tiles("nope") == []
        assert normalize_tiles({"row": 0}) == []

    def test_drops_malformed_entries(self):
        tiles = [
            "not-a-dict",
            {"host": "10.0.0.1"},  # missing row/col
            {"row": "x", "col": 0},  # non-numeric row
            {"row": -1, "col": 0, "host": "10.0.0.1"},  # negative
            {"row": True, "col": 0, "host": "10.0.0.1"},  # bool row
        ]
        assert normalize_tiles(tiles) == []

    def test_coerces_types_and_defaults(self):
        [tile] = normalize_tiles(
            [{"row": "1", "col": "0", "host": " 10.0.0.5 ", "port": "7001", "local_api_key": " k "}]
        )
        assert tile == {"row": 1, "col": 0, "host": "10.0.0.5", "port": 7001, "local_api_key": "k", "enabled": True}

    def test_bad_port_defaults_to_7000(self):
        [tile] = normalize_tiles([{"row": 0, "col": 0, "port": "abc"}])
        assert tile["port"] == 7000

    def test_dedupes_by_position_last_wins(self):
        tiles = normalize_tiles([{"row": 0, "col": 0, "host": "old"}, {"row": 0, "col": 0, "host": "new"}])
        assert len(tiles) == 1
        assert tiles[0]["host"] == "new"

    def test_out_of_range_positions_preserved(self):
        """Tiles beyond the current W×H are kept — resize must not destroy keys."""
        assert len(normalize_tiles([{"row": 5, "col": 7, "host": "10.0.0.9", "local_api_key": "k"}])) == 1


# --- the stored form ------------------------------------------------------------------------


class TestNormalize:
    def test_every_connection_field_is_stored_in_order(self):
        stored = normalize({}, {"device_type": "flagship"})
        assert tuple(stored) == CONNECTION_FIELDS
        assert stored == LEGACY_FLAT_FIELDS

    def test_an_unknown_mode_is_local_and_a_bad_port_the_local_apis(self):
        stored = normalize({"api_mode": "carrier-pigeon", "port": "x", "host": None}, {"device_type": "flagship"})
        assert (stored["api_mode"], stored["port"], stored["host"]) == ("local", 7000, "")

    def test_the_note_array_token_is_stripped(self):
        assert normalize({"note_array_token": " tok "}, {"device_type": "note_array"})["note_array_token"] == "tok"

    def test_tiles_are_kept_only_on_a_note_array(self):
        assert normalize({"tiles": [_tile()]}, {"device_type": "flagship"})["tiles"] == []
        assert normalize({"tiles": [_tile(), "junk"]}, {"device_type": "note_array"})["tiles"] == [_tile()]

    def test_the_class_hook_is_the_same_rule(self):
        board = {"device_type": "note_array"}
        assert VestaboardOutput.normalize_config({"tiles": [_tile()]}, board) == normalize({"tiles": [_tile()]}, board)

    def test_the_flat_fields_are_the_connection_fields_with_their_defaults(self):
        assert tuple(VestaboardOutput.legacy_flat_fields) == CONNECTION_FIELDS
        assert VestaboardOutput.legacy_flat_fields["port"] == 7000
        assert VestaboardOutput.legacy_flat_fields["api_mode"] == "local"


# --- configured or not ----------------------------------------------------------------------


class TestTiles:
    """Which tiles a board drives (moved from core's ``BoardInstance`` tests)."""

    def test_configured_tiles_filters_out_of_range(self):
        settings = VestaboardSettings.from_config(
            {"tiles": [_tile(col=0), _tile(col=1), _tile(col=5), _tile(row=3)]}, "note_array"
        )
        assert {(t["row"], t["col"]) for t in settings.configured_tiles(2, 1)} == {(0, 0), (0, 1)}

    def test_configured_tiles_requires_host_key_enabled(self):
        settings = VestaboardSettings.from_config(
            {
                "tiles": [
                    _tile(col=0),
                    _tile(col=1, host=""),
                    _tile(col=2, local_api_key=""),
                    _tile(col=3, enabled=False),
                ]
            },
            "note_array",
        )
        assert [(t["row"], t["col"]) for t in settings.configured_tiles(4, 1)] == [(0, 0)]

    def test_a_local_array_with_tiles_is_driven_tile_by_tile(self):
        assert VestaboardSettings.from_config({"api_mode": "local", "tiles": [_tile()]}, "note_array").uses_local_tiles(
            "note_array"
        )

    def test_a_legacy_array_without_tiles_keeps_token_semantics(self):
        settings = VestaboardSettings.from_config({"note_array_token": "tok"}, "note_array")
        assert settings.api_mode == "local"
        assert not settings.uses_local_tiles("note_array")
        assert settings.is_configured("note_array", 1, 1)

    def test_a_cloud_array_ignores_its_tiles(self):
        settings = VestaboardSettings.from_config(
            {"api_mode": "cloud", "note_array_token": "tok", "tiles": [_tile()]}, "note_array"
        )
        assert not settings.uses_local_tiles("note_array")
        assert settings.is_configured("note_array", 2, 1)


class TestStatus:
    def test_a_local_board_needs_its_key_and_host(self):
        assert status({"host": "192.0.2.10", "local_api_key": "k"}, {"device_type": "flagship"}) == (True, True)
        assert status({"host": "192.0.2.10"}, {"device_type": "flagship"}) == (False, True)
        assert status({}, {"device_type": "flagship"}) == (False, False)

    def test_a_cloud_board_needs_its_key(self):
        assert status({"api_mode": "cloud", "cloud_key": "k"}, {"device_type": "note"}) == (True, True)
        assert status({"api_mode": "cloud"}, {"device_type": "note"}) == (False, False)

    def test_a_partial_local_array_is_configured_with_one_usable_tile(self):
        board, config = _array(tiles=[_tile(), _tile(col=1, host="")])
        assert status(config, board) == (True, True)
        board, config = _array(tiles=[_tile(host="")])
        assert status(config, board) == (False, True)

    def test_a_tile_beyond_the_boards_layout_does_not_count(self):
        board, config = _array(tiles=[_tile(col=5)])
        assert status(config, board)[0] is False

    def test_a_virtual_mode_is_always_configured(self):
        assert status({"api_mode": "virtual"}, {}) == (True, True)

    def test_the_class_hook_answers_an_output_status(self):
        verdict = VestaboardOutput.board_status({"api_mode": "cloud", "cloud_key": "k"}, {"device_type": "flagship"})
        assert verdict == OutputStatus(configured=True, attempted=True)
        assert verdict.state == "connected"
        assert VestaboardOutput.board_status({}, {"device_type": "flagship"}).state == "not_configured"


# --- secrets ---------------------------------------------------------------------------------


class TestSecrets:
    def test_every_set_credential_is_masked_and_an_unset_one_is_not(self):
        config = {"local_api_key": "k", "cloud_key": "", "note_array_token": "t", "host": "h", "tiles": [_tile()]}
        masked = mask(config)
        assert (masked["local_api_key"], masked["cloud_key"], masked["note_array_token"]) == ("***", "", "***")
        assert masked["host"] == "h"
        assert masked["tiles"][0]["local_api_key"] == "***"
        assert config["tiles"][0]["local_api_key"] == "key", "masking never touches the stored dicts"

    def test_an_echoed_secret_is_restored_and_an_edit_kept(self):
        stored = {"local_api_key": "stored", "cloud_key": "c"}
        restored = restore({"local_api_key": "***", "cloud_key": "new"}, stored)
        assert (restored["local_api_key"], restored["cloud_key"]) == ("stored", "new")

    def test_a_secret_with_nothing_stored_restores_empty(self):
        assert restore({"note_array_token": "***"}, {})["note_array_token"] == ""

    def test_a_tiles_key_follows_its_endpoint_when_tiles_are_swapped(self):
        stored = {
            "tiles": [
                _tile(col=0, host="10.0.0.1", local_api_key="A"),
                _tile(col=1, host="10.0.0.2", local_api_key="B"),
            ]
        }
        swapped = {
            "tiles": [
                _tile(col=0, host="10.0.0.2", local_api_key="***"),
                _tile(col=1, host="10.0.0.1", local_api_key="***"),
            ]
        }
        restored = restore(swapped, stored)
        assert [t["local_api_key"] for t in restored["tiles"]] == ["B", "A"]

    def test_a_tiles_key_follows_its_position_when_its_address_changes(self):
        stored = {"tiles": [_tile(host="10.0.0.1", local_api_key="A")]}
        restored = restore({"tiles": [_tile(host="10.0.0.9", local_api_key="***"), "junk"]}, stored)
        assert restored["tiles"][0]["local_api_key"] == "A"

    def test_masked_paths_name_every_unrestored_secret(self):
        assert masked_paths({"cloud_key": "***", "tiles": [_tile(local_api_key="***"), "junk"]}) == [
            "cloud_key",
            "tiles[0].local_api_key",
        ]

    def test_the_class_hooks_are_the_same_rules(self):
        assert VestaboardOutput.mask_config({"cloud_key": "k"}, None)["cloud_key"] == "***"
        assert VestaboardOutput.restore_config({"cloud_key": "***"}, {"cloud_key": "k"}, None) == {"cloud_key": "k"}
        assert VestaboardOutput.restore_config("***", {"cloud_key": "k"}, None) == "***"
        assert VestaboardOutput.masked_config_paths({"cloud_key": "***"}, None) == ["cloud_key"]
        assert VestaboardOutput.masked_config_paths("***", None) == []

"""The board-settings actions (``manifest.json`` ``output.actions``).

FiestaBoard runs each through ``VestaboardOutput.handle_action`` with an
``ActionContext`` — the real one from the plugin API, built here with the
pieces core lends: throwaway instances on a scripted device, the board's
live instance, a reader, an invalidation counter. FiestaBoard's own tests
drive the same actions through its routes, legacy ones included.
"""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi import HTTPException

from plugins.vestaboard import VestaboardOutput
from plugins.vestaboard.tiles import identify_pattern
from src.plugins import ActionContext, OutputActionError

from .conftest import Device, grid, response

FLAGSHIP_GRID = {"message": [[0] * 22 for _ in range(6)]}


class Lender:
    """What core lends an action, scripted: every instance it builds talks to
    one :class:`Device`; ``live`` is the board's live instance (or ``None``)."""

    def __init__(self, manifest, *, live=None, read=None) -> None:
        self.manifest = manifest
        self.device = Device()
        self.built: list[dict] = []
        self.live = live
        self.read = read
        self.invalidated = 0

    def instance(self, board, config):
        source = board if config is None else {"output": "vestaboard", "output_config": dict(config)}
        settings = VestaboardOutput.config_from_board(source)
        if settings is None:
            return None
        self.built.append(settings)
        plugin = VestaboardOutput(board.get("id"), settings)
        plugin.bind_manifest(self.manifest.output)
        plugin.http.use_transport(self.device)
        return plugin

    def with_live(self, board_id, fn):
        if self.live is None:
            raise OutputActionError(503, f"Board client not initialized: {board_id}")
        return fn(self.live)

    def context(self, action, board, *, inputs=None, board_id=None):
        def invalidate():
            self.invalidated += 1

        return ActionContext(
            action=action,
            board=board,
            board_id=board_id,
            inputs=inputs or {},
            instance=lambda config: self.instance(board, config),
            with_live=lambda fn: self.with_live(board_id, fn),
            reader=lambda: self.read,
            invalidate=invalidate,
        )

    def run(self, action, board, **kw):
        return VestaboardOutput.handle_action(self.context(action, board, **kw))


def _board(device_type="flagship", **config):
    return {"id": "b1", "device_type": device_type, "notes_wide": 2, "notes_tall": 1, "output_config": config}


@pytest.fixture
def lender(manifest):
    return Lender(manifest)


# --- test_connection --------------------------------------------------------------------


class TestTestConnection:
    @pytest.mark.parametrize(
        ("config", "detail"),
        [
            ({"api_mode": "cloud"}, "Cloud API key is required"),
            ({"api_mode": "local", "host": "192.0.2.10"}, "Local API key is required"),
            ({"api_mode": "local", "local_api_key": "k"}, "Board host/IP is required for Local API"),
        ],
    )
    def test_a_missing_credential_is_refused_before_anything_is_contacted(self, lender, config, detail):
        with pytest.raises(OutputActionError) as refused:
            lender.run("test_connection", _board(**config))
        assert (refused.value.status_code, refused.value.detail) == (400, detail)
        assert lender.device.requests == []

    def test_a_local_board_missing_both_is_told_about_the_key_first(self, lender):
        """The recorded order of ``POST /config/board/test``'s refusals."""
        with pytest.raises(OutputActionError) as refused:
            lender.run("test_connection", _board(api_mode="local"))
        assert refused.value.detail == "Local API key is required"

    def test_a_host_that_is_not_a_bare_address_is_refused(self, lender):
        with pytest.raises(HTTPException) as refused:
            lender.run("test_connection", _board(local_api_key="k", host="http://example.com/x"))
        assert refused.value.status_code == 400
        assert lender.device.requests == []

    def test_a_reachable_board_is_ok_with_the_verdict_in_detail(self, lender):
        lender.device.answer = lambda request: response(200, FLAGSHIP_GRID)
        outcome = lender.run("test_connection", _board(local_api_key="k", host="192.0.2.10"))
        assert outcome.status == "ok"
        assert outcome.detail["success"] is True
        assert outcome.detail["api_mode"] == "local"
        assert lender.device.urls == ["http://192.0.2.10:7000/local-api/message"]

    def test_a_rejected_key_is_an_error_with_guidance(self, lender):
        lender.device.answer = lambda request: response(401)
        outcome = lender.run("test_connection", _board(api_mode="cloud", cloud_key="k"))
        assert (outcome.status, outcome.detail["error"]) == ("error", "HTTP 401")
        assert outcome.guidance

    def test_a_note_array_with_nothing_to_drive_is_not_configured(self, lender):
        with pytest.raises(OutputActionError) as draft:
            lender.run("test_connection", _board("note_array", api_mode="cloud"))
        assert draft.value.detail == "Enter the board's connection details first."
        with pytest.raises(OutputActionError) as saved:
            lender.run("test_connection", _board("note_array", api_mode="cloud"), board_id="b1")
        assert saved.value.detail == "Board b1 is not configured (missing credentials)"

    def test_an_unusable_credential_is_a_configuration_refusal(self, lender, monkeypatch):
        def refuse(*args, **kwargs):
            raise ValueError("no")

        monkeypatch.setattr(lender, "instance", refuse)
        with pytest.raises(OutputActionError) as refused:
            lender.run("test_connection", _board(local_api_key="k", host="192.0.2.10"))
        assert refused.value.detail == "Board connection configuration is invalid."


class TestTestTile:
    def test_the_typed_address_and_key_are_probed(self, lender):
        lender.device.answer = lambda request: response(200, {"message": [[0] * 15] * 3})
        outcome = lender.run(
            "test_tile",
            _board("note_array"),
            inputs={"row": 0, "col": 1, "host": "192.0.2.22", "port": 7001, "local_api_key": "k"},
        )
        assert outcome.status == "ok"
        assert lender.device.urls == ["http://192.0.2.22:7001/local-api/message"]
        assert lender.device.requests[0].kwargs["headers"]["X-Vestaboard-Local-Api-Key"] == "k"

    def test_a_saved_tiles_masked_key_is_its_stored_one(self, lender):
        tiles = [{"row": 0, "col": 1, "host": "192.0.2.22", "local_api_key": "stored"}]
        lender.run("test_tile", _board("note_array", tiles=tiles), inputs={"row": 0, "col": 1})
        assert lender.device.urls == ["http://192.0.2.22:7000/local-api/message"]
        assert lender.device.requests[0].kwargs["headers"]["X-Vestaboard-Local-Api-Key"] == "stored"

    def test_a_tile_without_a_key_is_refused(self, lender):
        with pytest.raises(OutputActionError) as refused:
            lender.run("test_tile", _board("note_array"), inputs={"row": 0, "col": 0, "host": "192.0.2.22"})
        assert refused.value.detail == "Local API key is required"

    def test_a_tile_without_an_address_is_refused(self, lender):
        with pytest.raises(OutputActionError) as refused:
            lender.run("test_tile", _board("note_array"), inputs={"row": 0, "col": 0, "local_api_key": "k"})
        assert refused.value.detail == "Board host/IP is required for Local API"
        assert lender.device.requests == []


# --- discover ---------------------------------------------------------------------------


class TestDiscover:
    @pytest.mark.parametrize(("asked", "used"), [(None, 4.0), (0.1, 1.0), (60, 15.0), (2, 2.0)])
    def test_the_scan_length_is_clamped(self, lender, asked, used):
        inputs = {} if asked is None else {"timeout": asked}
        with mock.patch("plugins.vestaboard.discovery.discover", return_value=[]) as discover:
            outcome = lender.run("discover", _board(), inputs=inputs)
        discover.assert_called_once_with(used)
        assert (outcome.message, outcome.devices) == ("No boards found.", ())

    def test_found_boards_are_the_devices(self, lender):
        found = [{"ip": "192.0.2.40", "port": 7000, "hostname": "vb.local", "source": "mdns"}]
        with mock.patch("plugins.vestaboard.discovery.discover", return_value=found):
            outcome = lender.run("discover", _board())
        assert (outcome.message, outcome.devices) == ("Found 1 board(s).", tuple(found))


# --- identify ---------------------------------------------------------------------------


TILES = [
    {"row": 0, "col": 0, "host": "192.0.2.21", "local_api_key": "test_key_a"},
    {"row": 0, "col": 1, "host": "192.0.2.22", "local_api_key": "test_key_b"},
]


class TestIdentify:
    @pytest.mark.parametrize(
        ("board", "inputs", "detail"),
        [
            (_board("flagship"), {}, "Identify is only available for note arrays in local API mode"),
            (
                _board("note_array", api_mode="cloud"),
                {},
                "Identify is only available for note arrays in local API mode",
            ),
            (_board("note_array", tiles=TILES), {"target": "some"}, 'target must be "tile" or "all"'),
            (
                _board("note_array"),
                {"target": "all", "host": "192.168.0.5", "local_api_key": "k"},
                "Credential override requires target='tile' with row and col",
            ),
            (
                _board("note_array"),
                {"row": 0, "col": 0, "host": "192.168.0.5"},
                "host and local_api_key are both required",
            ),
            (_board("note_array", tiles=TILES), {"target": "tile"}, "row and col are required for target='tile'"),
            (_board("note_array", tiles=TILES), {"row": 0, "col": 7}, "No configured tile at row=0, col=7"),
            (_board("note_array"), {"target": "all"}, "Board has no configured tiles to identify"),
        ],
    )
    def test_what_is_refused_before_a_tile_is_flashed(self, lender, board, inputs, detail):
        with pytest.raises(OutputActionError) as refused:
            lender.run("identify", board, inputs=inputs)
        assert (refused.value.status_code, refused.value.detail) == (400, detail)
        assert lender.device.requests == []

    def test_an_unsaved_tile_flashes_through_a_throwaway_instance(self, lender):
        outcome = lender.run(
            "identify",
            _board("note_array"),
            inputs={"row": 0, "col": 1, "host": "192.168.0.22", "local_api_key": "k"},
            board_id="b1",
        )
        assert lender.device.urls == ["http://192.168.0.22:7000/local-api/message"]
        assert lender.device.requests[0].json == {"characters": identify_pattern(0, 1, 2)}
        assert outcome.detail == {"results": [{"row": 0, "col": 1, "success": True}]}
        assert lender.invalidated == 1, "the board's real content is re-sent after the flash"

    def test_an_unsaved_tile_that_breaks_mid_flash_is_reported_not_raised(self, lender):
        lender.device.answer = lambda request: RuntimeError("socket gone")
        outcome = lender.run(
            "identify",
            _board("note_array"),
            inputs={"row": 0, "col": 0, "host": "192.168.0.21", "local_api_key": "k"},
        )
        assert (outcome.status, outcome.detail["results"]) == ("error", [{"row": 0, "col": 0, "success": False}])

    def test_an_unsaved_tile_on_a_public_address_is_refused(self, lender):
        with pytest.raises(HTTPException):
            lender.run(
                "identify",
                _board("note_array"),
                inputs={"row": 0, "col": 0, "host": "93.184.216.34", "local_api_key": "k"},
            )

    def test_saved_tiles_flash_through_the_live_board(self, manifest):
        live = mock.Mock()
        live.identify_tiles.return_value = {(0, 0): True, (0, 1): False}
        lender = Lender(manifest, live=live)
        outcome = lender.run("identify", _board("note_array", tiles=TILES), inputs={"target": "all"}, board_id="b1")
        live.identify_tiles.assert_called_once_with([(0, 0), (0, 1)])
        assert outcome.status == "warning"
        assert outcome.guidance == ("Tile 1,2 did not respond.",)
        assert outcome.detail["results"] == [
            {"row": 0, "col": 0, "success": True},
            {"row": 0, "col": 1, "success": False},
        ]
        assert lender.invalidated == 1

    def test_no_tile_answering_is_an_error(self, manifest):
        live = mock.Mock()
        live.identify_tiles.return_value = {(0, 1): False}
        outcome = Lender(manifest, live=live).run(
            "identify", _board("note_array", tiles=TILES), inputs={"row": 0, "col": 1}, board_id="b1"
        )
        assert (outcome.status, outcome.message) == ("error", "No tile responded.")

    def test_every_tile_answering_is_ok(self, manifest):
        live = mock.Mock()
        live.identify_tiles.return_value = {(0, 0): True, (0, 1): True}
        outcome = Lender(manifest, live=live).run(
            "identify", _board("note_array", tiles=TILES), inputs={"target": "all"}, board_id="b1"
        )
        assert (outcome.status, outcome.message) == ("ok", "Identified 2 tile(s).")

    def test_saved_tiles_without_a_live_board_are_a_503(self, lender):
        with pytest.raises(OutputActionError) as refused:
            lender.run("identify", _board("note_array", tiles=TILES), inputs={"target": "all"}, board_id="b1")
        assert (refused.value.status_code, refused.value.detail) == (503, "Board client not initialized: b1")

    def test_a_live_board_that_is_not_a_tile_array_is_a_503(self, manifest):
        lender = Lender(manifest, live=mock.Mock(identify_tiles=None))
        with pytest.raises(OutputActionError) as refused:
            lender.run("identify", _board("note_array", tiles=TILES), inputs={"target": "all"}, board_id="b1")
        assert refused.value.status_code == 503


# --- detect_geometry ----------------------------------------------------------------------


class TestDetectGeometry:
    def test_a_local_array_is_sized_by_its_tiles_not_detected(self, lender):
        with pytest.raises(OutputActionError) as refused:
            lender.run("detect_geometry", _board("note_array", tiles=TILES))
        assert refused.value.detail.startswith("Auto-detect is not available for local-mode note arrays")

    def test_nothing_to_read_through_is_not_configured(self, lender):
        with pytest.raises(OutputActionError) as refused:
            lender.run("detect_geometry", _board(), board_id="b1")
        assert refused.value.detail == "Board b1 is not configured (missing credentials)"

    def test_a_flagship_grid_is_a_flagship(self, manifest):
        outcome = Lender(manifest, read=lambda: grid()).run("detect_geometry", _board())
        assert outcome.geometry == {"device_type": "flagship", "rows": 6, "cols": 22}

    def test_a_two_by_two_array_matches_its_preset(self, manifest):
        outcome = Lender(manifest, read=lambda: grid(0, 6, 30)).run("detect_geometry", _board())
        assert outcome.geometry["matched_preset"] == "2×2 grid"

    def test_no_layout_is_an_error_without_a_size(self, manifest):
        outcome = Lender(manifest, read=lambda: None).run("detect_geometry", _board())
        assert (outcome.status, outcome.geometry, dict(outcome.detail)) == ("error", None, {})

    def test_an_unclassifiable_grid_is_an_error_with_its_size(self, manifest):
        outcome = Lender(manifest, read=lambda: grid(0, 4, 20)).run("detect_geometry", _board())
        assert outcome.status == "error"
        assert dict(outcome.detail) == {"rows": 4, "cols": 20}


# --- enable_local_api ---------------------------------------------------------------------


class TestEnableLocalApi:
    def test_the_issued_key_comes_back_secret(self, lender):
        async def exchange(request):
            assert (request.host, request.enablement_token) == ("192.168.0.40", "test_token")
            return {"success": True, "api_key": "test_issued", "message": "Local API enabled"}

        with mock.patch("plugins.vestaboard.local_api.exchange_enablement_token", exchange):
            outcome = lender.run(
                "enable_local_api", _board(host="192.168.0.40"), inputs={"enablement_token": "test_token"}
            )
        assert outcome.fields["api_key"].value == "test_issued"
        assert outcome.fields["api_key"].secret is True
        assert outcome.detail["api_key"] == "test_issued"

    def test_a_refused_token_is_an_error_with_the_boards_reason(self, lender):
        async def exchange(request):
            return {"success": False, "message": "Invalid enablement token", "error": "HTTP 401"}

        with mock.patch("plugins.vestaboard.local_api.exchange_enablement_token", exchange):
            outcome = lender.run(
                "enable_local_api", _board(), inputs={"host": "192.168.0.40", "enablement_token": "bad"}
            )
        assert (outcome.status, outcome.guidance, outcome.detail["error"]) == ("error", ("HTTP 401",), "HTTP 401")


def test_an_action_the_plugin_does_not_implement(lender):
    with pytest.raises(NotImplementedError):
        lender.run("reticulate", _board())

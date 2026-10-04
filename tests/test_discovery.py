"""The Vestaboard ``discover`` hook: mDNS browse, then a subnet probe of port 7000.

Never simulate a missing dependency with ``patch("builtins.__import__")``:
``patch.dict("sys.modules", {"<name>": None})`` blocks the import on its own.
"""

from unittest.mock import MagicMock, patch

import pytest

from plugins.vestaboard import discovery


class TestProbeVestaboardPort:
    """Test the _probe_vestaboard_port helper."""

    def test_returns_false_when_port_closed(self):

        # Port 1 on localhost is almost certainly not listening
        assert discovery._probe_vestaboard_port("127.0.0.1", port=1, timeout=0.2) is False

    def test_returns_false_for_unreachable_host(self):

        # A connect that times out (what a non-routable address does) reads as
        # "no board here". Mocked: the suite may not leave loopback.
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = TimeoutError("timed out")
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            assert discovery._probe_vestaboard_port("192.0.2.1", port=7000, timeout=0.2) is False

    def test_returns_true_when_connected(self):

        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            assert discovery._probe_vestaboard_port("10.0.0.5", port=7000) is True


class TestScanForBoards:
    """Test the discover hook."""

    def test_returns_list(self):

        # With mocked zeroconf and no real network, should return a list
        with patch.object(discovery, "local_ipv4", return_value="127.0.0.1"):
            # loopback /24 probing is skipped when IP is 127.0.0.1
            result = discovery.discover(timeout=0.1)
        assert isinstance(result, list)

    def test_returns_empty_when_no_boards(self):

        with patch.object(discovery, "local_ipv4", return_value="127.0.0.1"):
            result = discovery.discover(timeout=0.1)
        assert result == []

    def test_mdns_discovery_returns_boards(self):
        """Simulate mDNS finding a board."""

        mock_info = MagicMock()
        mock_info.parsed_addresses.return_value = ["192.168.1.50"]
        mock_info.port = 7000
        mock_info.server = "vestaboard-abc.local."

        mock_zc = MagicMock()
        mock_zc.get_service_info.return_value = mock_info

        def fake_browser(zc, stype, listener):
            # Simulate discovering a service immediately
            listener.add_service(zc, stype, "Vestaboard._vestaboard._tcp.local.")
            return MagicMock()

        with (
            patch("zeroconf.Zeroconf", return_value=mock_zc),
            patch("zeroconf.ServiceBrowser", side_effect=fake_browser),
            patch("time.sleep"),
            patch.object(discovery, "local_ipv4", return_value="127.0.0.1"),
        ):
            result = discovery.discover(timeout=0.1)

        assert len(result) == 1
        assert result[0]["ip"] == "192.168.1.50"
        assert result[0]["port"] == 7000
        assert result[0]["source"] == "mdns"

    def test_port_probe_returns_boards(self):
        """Simulate finding a board via port probing."""

        def fake_probe(ip, port=7000, timeout=0.5):
            return ip == "10.0.0.42"

        with (
            patch.object(discovery, "_probe_vestaboard_port", side_effect=fake_probe),
            patch.object(discovery, "local_ipv4", return_value="10.0.0.1"),
            patch("zeroconf.Zeroconf", return_value=MagicMock()),
            patch("zeroconf.ServiceBrowser", return_value=MagicMock()),
            patch("time.sleep"),
        ):
            result = discovery.discover(timeout=0.1)

        ips = [b["ip"] for b in result]
        assert "10.0.0.42" in ips

    def test_deduplicates_mdns_and_probe(self):
        """Board found via both mDNS and port scan should appear once."""

        mock_info = MagicMock()
        mock_info.parsed_addresses.return_value = ["10.0.0.42"]
        mock_info.port = 7000
        mock_info.server = "board.local."

        mock_zc = MagicMock()
        mock_zc.get_service_info.return_value = mock_info

        def fake_browser(zc, stype, listener):
            listener.add_service(zc, stype, "Board._vestaboard._tcp.local.")
            return MagicMock()

        def fake_probe(ip, port=7000, timeout=0.5):
            return ip == "10.0.0.42"

        with (
            patch("zeroconf.Zeroconf", return_value=mock_zc),
            patch("zeroconf.ServiceBrowser", side_effect=fake_browser),
            patch.object(discovery, "_probe_vestaboard_port", side_effect=fake_probe),
            patch.object(discovery, "local_ipv4", return_value="10.0.0.1"),
            patch("time.sleep"),
        ):
            result = discovery.discover(timeout=0.1)

        ips = [b["ip"] for b in result]
        assert ips.count("10.0.0.42") == 1

    def test_graceful_when_zeroconf_missing(self):
        """discover does not raise if zeroconf is not installed."""

        with (
            patch.dict("sys.modules", {"zeroconf": None}),
            patch.object(discovery, "local_ipv4", return_value="127.0.0.1"),
        ):
            result = discovery.discover(timeout=0.1)
        assert isinstance(result, list)


class TestNetworkHint:
    """FiestaBoard's ``hint_host``: in Docker bridge mode this host's own /24
    is a container network, so the browser's network is probed first."""

    @staticmethod
    def _probed(hint, local_ip="172.17.0.2"):
        probed = []

        def fake_probe(ip, port=7000, timeout=0.5):
            probed.append(ip)
            return ip == "192.168.1.77"

        with (
            patch.dict("sys.modules", {"zeroconf": None}),
            patch.object(discovery, "_probe_vestaboard_port", side_effect=fake_probe),
            patch.object(discovery, "local_ipv4", return_value=local_ip),
        ):
            found = discovery.discover(timeout=0.1, hint=hint)
        return probed, found

    def test_a_board_on_the_browsers_network_is_found_from_a_bridge_container(self):
        probed, found = self._probed("192.168.1.20")
        assert found == [{"ip": "192.168.1.77", "port": 7000, "hostname": "", "source": "port_scan"}]
        assert {ip.rsplit(".", 1)[0] for ip in probed} == {"192.168.1", "172.17.0"}

    def test_without_a_hint_only_this_hosts_network_is_probed(self):
        probed, found = self._probed(None)
        assert found == []
        assert {ip.rsplit(".", 1)[0] for ip in probed} == {"172.17.0"}
        assert "172.17.0.2" not in probed

    def test_neither_fiestaboards_own_addresses_are_probed(self):
        probed, _ = self._probed("192.168.1.20")
        assert "192.168.1.20" not in probed and "172.17.0.2" not in probed

    def test_a_hint_on_this_hosts_network_probes_it_once(self):
        probed, _ = self._probed("10.0.0.20", local_ip="10.0.0.1")
        assert len(probed) == len(set(probed)) == 252

    @pytest.mark.parametrize("hint", ["8.8.8.8", "fiestaboard.local", "127.0.0.1", ""])
    def test_a_hint_that_is_not_a_private_ipv4_address_is_ignored(self, hint):
        probed, _ = self._probed(hint)
        assert {ip.rsplit(".", 1)[0] for ip in probed} == {"172.17.0"}

    def test_the_output_hook_hands_the_hint_to_the_scan(self):
        from unittest.mock import call

        from plugins.vestaboard import VestaboardOutput

        with patch.object(discovery, "discover", return_value=[]) as scan:
            VestaboardOutput.discover(3.0, hint="192.168.1.20")
            VestaboardOutput.discover(3.0)
        assert scan.call_args_list == [call(3.0, hint="192.168.1.20"), call(3.0)]

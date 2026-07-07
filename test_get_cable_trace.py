#!/usr/bin/env python3

import importlib.util
import pathlib
import unittest


SCRIPT_PATH = pathlib.Path(__file__).with_name("get-cable-trace.py")
SPEC = importlib.util.spec_from_file_location("get_cable_trace", SCRIPT_PATH)
get_cable_trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(get_cable_trace)


class FakeEndpoint:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeDcim:
    def __init__(self, interface=None, front_port=None):
        self.interfaces = FakeEndpoint(interface)
        self.front_ports = FakeEndpoint(front_port)


class FakeNetBox:
    def __init__(self, interface=None, front_port=None):
        self.dcim = FakeDcim(interface, front_port)


class FakeTermination:
    def __init__(self, termination_id, rear_port=None):
        self.id = termination_id
        self.rear_port = rear_port


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise get_cable_trace.requests.exceptions.HTTPError(response=self)

        pass

    def json(self):
        return self.payload


class FrontPortTraceTest(unittest.TestCase):
    def setUp(self):
        self.original_nb = get_cable_trace.nb
        self.original_get = get_cable_trace.requests.get
        self.original_token = get_cable_trace.api_token

    def tearDown(self):
        get_cable_trace.nb = self.original_nb
        get_cable_trace.requests.get = self.original_get
        get_cable_trace.api_token = self.original_token

    def test_trace_uses_front_port_trace_endpoint_when_interface_is_missing(self):
        requested_urls = []
        get_cable_trace.nb = FakeNetBox(front_port=FakeTermination(200))
        get_cable_trace.api_token = "token"

        def fake_get(url, **kwargs):
            requested_urls.append(url)
            return FakeResponse([])

        get_cable_trace.requests.get = fake_get

        trace = get_cable_trace.get_trace("panel-a", "1/MPO-1")

        self.assertEqual(trace["device"], "panel-a")
        self.assertEqual(trace["interface"], "1/MPO-1")
        self.assertEqual(
            requested_urls,
            [
                f"{get_cable_trace.NB_URL}/api/dcim/front-ports/200/trace/",
            ],
        )
        self.assertEqual(
            get_cable_trace.nb.dcim.interfaces.calls,
            [{"device": "panel-a", "name": "1/MPO-1"}],
        )
        self.assertEqual(
            get_cable_trace.nb.dcim.front_ports.calls,
            [{"device": "panel-a", "name": "1/MPO-1"}],
        )

    def test_front_port_trace_falls_back_to_rear_port_trace_when_missing(self):
        requested_urls = []
        get_cable_trace.nb = FakeNetBox(
            front_port=FakeTermination(200, rear_port={"id": 300})
        )
        get_cable_trace.api_token = "token"

        def fake_get(url, **kwargs):
            requested_urls.append(url)

            if url.endswith("/api/dcim/front-ports/200/trace/"):
                return FakeResponse({}, status_code=404)

            return FakeResponse([
                ([{"display": "1/MPO-1"}], None, [{"display": "#15048"}]),
            ])

        get_cable_trace.requests.get = fake_get

        trace = get_cable_trace.get_trace("panel-a", "1/1")

        self.assertEqual(
            requested_urls,
            [
                f"{get_cable_trace.NB_URL}/api/dcim/front-ports/200/trace/",
                f"{get_cable_trace.NB_URL}/api/dcim/rear-ports/300/trace/",
            ],
        )
        self.assertEqual(
            [
                (src[0]["display"], dst[0]["display"])
                for src, _, dst in trace["trace"]
            ],
            [
                ("1/MPO-1", "#15048"),
            ],
        )

    def test_trace_prefers_interface_when_name_exists_as_interface(self):
        requested_urls = []
        get_cable_trace.nb = FakeNetBox(
            interface=FakeTermination(100),
            front_port=FakeTermination(200),
        )
        get_cable_trace.api_token = "token"

        def fake_get(url, **kwargs):
            requested_urls.append(url)
            return FakeResponse([])

        get_cable_trace.requests.get = fake_get

        get_cable_trace.get_trace("router-a", "Eth1")

        self.assertEqual(
            requested_urls,
            [
                f"{get_cable_trace.NB_URL}/api/dcim/interfaces/100/trace/",
            ],
        )
        self.assertEqual(get_cable_trace.nb.dcim.front_ports.calls, [])


if __name__ == "__main__":
    unittest.main()

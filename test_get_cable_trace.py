#!/usr/bin/env python3

import importlib.util
import pathlib
import unittest


SCRIPT_PATH = pathlib.Path(__file__).with_name("get-cable-trace.py")
SPEC = importlib.util.spec_from_file_location("get_cable_trace", SCRIPT_PATH)
get_cable_trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(get_cable_trace)


def node(name):
    return {"display": name}


class SegmentEndpointPairsTest(unittest.TestCase):
    def test_pairs_equal_endpoint_groups_by_position(self):
        pairs = list(get_cable_trace.segment_endpoint_pairs(
            [node("#15052"), node("#15053")],
            [node("23"), node("24")],
        ))

        self.assertEqual(
            [(src["display"], dst["display"]) for src, dst in pairs],
            [("#15052", "23"), ("#15053", "24")],
        )

    def test_expands_single_source_to_multiple_destinations(self):
        pairs = list(get_cable_trace.segment_endpoint_pairs(
            [node("FON")],
            [node("CH27-RX"), node("CH27-TX")],
        ))

        self.assertEqual(
            [(src["display"], dst["display"]) for src, dst in pairs],
            [("FON", "CH27-RX"), ("FON", "CH27-TX")],
        )

    def test_keeps_cartesian_fallback_for_ambiguous_uneven_groups(self):
        pairs = list(get_cable_trace.segment_endpoint_pairs(
            [node("A"), node("B")],
            [node("1"), node("2"), node("3")],
        ))

        self.assertEqual(len(pairs), 6)


class PathTraceTest(unittest.TestCase):
    def test_interface_trace_uses_neighbor_front_port_paths_endpoint(self):
        requested_urls = []
        original_get_api_response = get_cable_trace.get_api_response

        def fake_get_api_response(url):
            requested_urls.append(url)
            if url.endswith("/api/dcim/interfaces/100/trace/"):
                return [
                    [
                        [node("Hu0/0/0/2/0") | {
                            "id": 100,
                            "url": "https://netbox.example/api/dcim/interfaces/100/",
                        }],
                        None,
                        [node("1/1") | {
                            "id": 200,
                            "url": "https://netbox.example/api/dcim/front-ports/200/",
                        }],
                    ],
                ]

            return [{
                "path": [
                    [node("1/1") | {
                        "id": 200,
                        "url": "https://netbox.example/api/dcim/front-ports/200/",
                    }],
                    [node("#15047")],
                    [node("Hu0/0/0/2/0")],
                ],
            }]

        get_cable_trace.get_api_response = fake_get_api_response

        try:
            trace = get_cable_trace.get_trace_segments({
                "endpoint": "interfaces",
                "id": 100,
                "type": "interface",
            })
        finally:
            get_cable_trace.get_api_response = original_get_api_response

        self.assertEqual(
            requested_urls,
            [
                f"{get_cable_trace.NB_URL}/api/dcim/interfaces/100/trace/",
                f"{get_cable_trace.NB_URL}/api/dcim/front-ports/200/paths/",
            ],
        )
        self.assertEqual(
            [(src[0]["display"], dst[0]["display"]) for src, _, dst in trace],
            [("1/1", "#15047"), ("#15047", "Hu0/0/0/2/0")],
        )


if __name__ == "__main__":
    unittest.main()

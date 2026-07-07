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


if __name__ == "__main__":
    unittest.main()

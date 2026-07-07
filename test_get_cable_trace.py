#!/usr/bin/env python3

import importlib.util
import pathlib
import tempfile
import unittest
import zipfile


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
    def http_error(self, status_code):
        response = get_cable_trace.requests.Response()
        response.status_code = status_code
        return get_cable_trace.requests.exceptions.HTTPError(response=response)

    def test_interface_trace_uses_direct_path_endpoint_without_summary_trace(self):
        requested_urls = []
        original_get_api_response = get_cable_trace.get_api_response

        def fake_get_api_response(url):
            requested_urls.append(url)
            return [{
                "path": [
                    [node("Hu0/0/0/2/0") | {
                        "id": 100,
                        "url": "https://netbox.example/api/dcim/interfaces/100/",
                    }],
                    [node("#15047")],
                    [node("1/1") | {
                        "id": 200,
                        "url": "https://netbox.example/api/dcim/front-ports/200/",
                    }],
                    [node("1/MPO-1")],
                    [node("remote")],
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
                f"{get_cable_trace.NB_URL}/api/dcim/interfaces/100/paths/",
            ],
        )
        self.assertEqual(
            [(src[0]["display"], dst[0]["display"]) for src, _, dst in trace],
            [
                ("Hu0/0/0/2/0", "#15047"),
                ("#15047", "1/1"),
                ("1/1", "1/MPO-1"),
                ("1/MPO-1", "remote"),
            ],
        )

    def test_interface_trace_falls_back_to_front_port_paths_on_missing_paths_api(self):
        requested_urls = []
        original_get_api_response = get_cable_trace.get_api_response

        def fake_get_api_response(url):
            requested_urls.append(url)

            if url.endswith("/api/dcim/interfaces/100/paths/"):
                raise self.http_error(404)

            if url.endswith("/api/dcim/interfaces/100/trace/"):
                return [
                    [
                        [node("Hu0/0/0/2/0") | {
                            "id": 100,
                            "url": "https://netbox.example/api/dcim/interfaces/100/",
                        }],
                        None,
                        [node("#15047")],
                    ],
                    [
                        [node("#15047")],
                        None,
                        [node("1/1") | {
                            "id": 200,
                            "url": "https://netbox.example/api/dcim/front-ports/200/",
                        }],
                    ],
                ]

            return [{
                "path": [
                    [node("Hu0/0/0/2/0") | {
                        "id": 100,
                        "url": "https://netbox.example/api/dcim/interfaces/100/",
                    }],
                    [node("#15047")],
                    [node("1/1") | {
                        "id": 200,
                        "url": "https://netbox.example/api/dcim/front-ports/200/",
                    }],
                    [node("1/MPO-1")],
                    [node("remote")],
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
                f"{get_cable_trace.NB_URL}/api/dcim/interfaces/100/paths/",
                f"{get_cable_trace.NB_URL}/api/dcim/interfaces/100/trace/",
                f"{get_cable_trace.NB_URL}/api/dcim/front-ports/200/paths/",
            ],
        )
        self.assertEqual(
            [(src[0]["display"], dst[0]["display"]) for src, _, dst in trace],
            [
                ("1/1", "1/MPO-1"),
                ("1/MPO-1", "remote"),
            ],
        )


class ExcelOutputTest(unittest.TestCase):
    def test_trace_name_is_only_written_once_per_trace(self):
        original_get_device_info = get_cable_trace.get_device_info
        get_cable_trace.get_device_info = lambda device: {
            "display": "",
            "rack": "",
            "site": "",
        }

        trace = [{
            "device": "p-1-tco",
            "interface": "Hu0/0/0/2/0",
            "trace": [
                (
                    [{"display": "Hu0/0/0/2/0"}],
                    None,
                    [{"display": "#15047"}],
                ),
                (
                    [{"display": "#15047"}],
                    None,
                    [{"display": "1/1"}],
                ),
            ],
        }]

        try:
            rows = list(get_cable_trace.trace_rows(trace))
        finally:
            get_cable_trace.get_device_info = original_get_device_info

        self.assertEqual(rows[0]["values"][0], "p-1-tco Hu0/0/0/2/0")
        self.assertEqual(rows[1]["values"][0], "")

    def test_writes_xlsx_with_readability_options(self):
        original_get_device_info = get_cable_trace.get_device_info

        def fake_get_device_info(device):
            if not device:
                return {"display": "", "rack": "", "site": ""}

            return {
                "display": device["name"],
                "rack": "R1",
                "site": "Site A",
            }

        trace = [{
            "device": "device-a",
            "interface": "1/1",
            "trace": [
                (
                    [{"display": "1/1", "device": {"name": "device-a"}}],
                    None,
                    [{"display": "#1"}],
                ),
                (
                    [{"display": "#1"}],
                    None,
                    [{"display": "Eth1", "device": {"name": "device-b"}}],
                ),
            ],
        }]

        get_cable_trace.get_device_info = fake_get_device_info

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_file = pathlib.Path(tmpdir) / "tracepath.xlsx"
                get_cable_trace.write_xlsx(output_file, trace)

                with zipfile.ZipFile(output_file) as xlsx:
                    names = set(xlsx.namelist())
                    sheet = xlsx.read("xl/worksheets/sheet1.xml").decode()

                self.assertIn("[Content_Types].xml", names)
                self.assertIn("xl/workbook.xml", names)
                self.assertIn("xl/styles.xml", names)
                self.assertIn('state="frozen"', sheet)
                self.assertIn('<autoFilter ref="A1:J3"/>', sheet)
                self.assertIn("device-a 1/1", sheet)
        finally:
            get_cable_trace.get_device_info = original_get_device_info

    def test_xlsx_alternates_neutral_styles_by_step(self):
        original_get_device_info = get_cable_trace.get_device_info
        get_cable_trace.get_device_info = lambda device: {
            "display": "",
            "rack": "",
            "site": "",
        }

        trace = [{
            "device": "device-a",
            "interface": "1/1",
            "trace": [
                (
                    [{"display": "1/RX"}, {"display": "1/TX"}],
                    None,
                    [{"display": "#1"}],
                ),
                (
                    [{"display": "#1"}],
                    None,
                    [{"display": "Eth1"}],
                ),
            ],
        }]

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_file = pathlib.Path(tmpdir) / "tracepath.xlsx"
                get_cable_trace.write_xlsx(output_file, trace)

                with zipfile.ZipFile(output_file) as xlsx:
                    sheet = xlsx.read("xl/worksheets/sheet1.xml").decode()
                    styles = xlsx.read("xl/styles.xml").decode()
        finally:
            get_cable_trace.get_device_info = original_get_device_info

        self.assertNotIn("FFFFF2CC", styles)
        self.assertNotIn("FF1F4E79", styles)
        self.assertIn("FF4B5563", styles)
        self.assertIn('wrapText="1"', styles)
        self.assertIn('width="42"', sheet)
        self.assertIn('width="14"', sheet)
        self.assertIn('<c r="B2" s="3"><v>1</v></c>', sheet)
        self.assertIn('<c r="B3" s="3"/>', sheet)
        self.assertIn('<c r="B4" s="2"><v>2</v></c>', sheet)


if __name__ == "__main__":
    unittest.main()

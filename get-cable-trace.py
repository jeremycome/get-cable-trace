#!/usr/bin/env python3

import csv
import argparse
import os
import requests
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime, timezone
from xml.sax.saxutils import escape

###########################################################################
# Configuration
###########################################################################

DEVICE = "p-1-th3"
INTERFACE = "FH0/0/0/1"
TRACES = [
    (DEVICE, INTERFACE),
]

OUTPUT_FILE = "tracepath.xlsx"

cert_path_relative = "~/configuration/tools/ca-alphatech.crt"
cert_path_absolute = os.path.abspath(os.path.expanduser(cert_path_relative))

os.environ["REQUESTS_CA_BUNDLE"] = cert_path_absolute

NB_URL = "https://netbox.alphalink.tech"

api_token = None
nb = None

###########################################################################
# Fonctions
###########################################################################

def csv_text(value):
    """Force LibreOffice / Excel à interpréter la cellule comme du texte."""
    if value is None or value == "":
        return ""
    return f"'{value}"


HEADERS = [
    "Trace",
    "Etape",
    "Source Interface",
    "Source Device",
    "Source Rack",
    "Source Site",
    "Destination Interface",
    "Destination Device",
    "Destination Rack",
    "Destination Site",
]


def configure_netbox():
    global api_token
    global nb

    from pynetbox import api

    api_token = os.environ.get("NETBOX_TOKEN")

    if api_token is None:
        raise SystemExit("La variable NETBOX_TOKEN n'est pas définie.")

    nb = api(
        url=NB_URL,
        token=api_token,
    )


device_cache = {}
device_cache_lock = Lock()


def get_device_info(device):
    """
    Retourne les informations d'un équipement.
    Les résultats sont mis en cache pour éviter plusieurs appels API.
    """

    if not device:
        return {
            "display": "",
            "rack": "",
            "site": "",
        }

    device_id = device["id"]

    with device_cache_lock:
        cached_device = device_cache.get(device_id)

    if cached_device is not None:
        return cached_device

    d = nb.dcim.devices.get(device_id)

    device_info = {
        "display": d.name,
        "rack": d.rack.name if d.rack else "",
        "site": d.site.name if d.site else "",
    }

    with device_cache_lock:
        device_cache[device_id] = device_info

    return device_info


def parse_args():
    parser = argparse.ArgumentParser(
        description="Exporte un ou plusieurs Trace Path NetBox vers un Excel."
    )
    parser.add_argument(
        "-t",
        "--trace",
        nargs="+",
        action="append",
        metavar="DEVICE_OR_INTERFACE",
        help=(
            "Trace à exporter: DEVICE INTERFACE_OR_FRONT_PORT ou seulement "
            "DEVICE pour tracer toutes les interfaces du device. Peut être "
            "utilisé plusieurs fois."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default=OUTPUT_FILE,
        help=(
            "Fichier de sortie .xlsx ou .csv. "
            f"Défaut: {OUTPUT_FILE}"
        ),
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=5,
        help="Nombre maximum de traces lancées en parallèle. Défaut: 5",
    )
    return parser.parse_args()


def get_interface(device_name, interface_name):
    iface = nb.dcim.interfaces.get(
        device=device_name,
        name=interface_name,
    )

    return iface


def get_front_port(device_name, front_port_name):
    front_port = nb.dcim.front_ports.get(
        device=device_name,
        name=front_port_name,
    )

    return front_port


def get_trace_target(device_name, termination_name):
    iface = get_interface(device_name, termination_name)

    if iface is not None:
        return {
            "device": device_name,
            "name": termination_name,
            "endpoint": "interfaces",
            "id": iface.id,
            "type": "interface",
        }

    front_port = get_front_port(device_name, termination_name)

    if front_port is not None:
        return {
            "device": device_name,
            "name": termination_name,
            "endpoint": "front-ports",
            "id": front_port.id,
            "type": "front_port",
        }

    raise ValueError(
        f"Interface ou front port introuvable: {device_name} {termination_name}"
    )


def get_device_interfaces(device_name):
    device = nb.dcim.devices.get(name=device_name)

    if device is None:
        raise ValueError(f"Device introuvable: {device_name}")

    interfaces = [
        (device_name, interface.name)
        for interface in nb.dcim.interfaces.filter(device=device_name)
    ]

    if not interfaces:
        raise ValueError(f"Aucune interface trouvée pour le device: {device_name}")

    return interfaces


def expand_trace_requests(trace_args):
    trace_requests = []

    for trace_arg in trace_args:
        if len(trace_arg) == 1:
            trace_requests.extend(get_device_interfaces(trace_arg[0]))
        elif len(trace_arg) == 2:
            trace_requests.append((trace_arg[0], trace_arg[1]))
        else:
            raise ValueError(
                "Chaque --trace doit contenir DEVICE ou "
                "DEVICE INTERFACE_OR_FRONT_PORT."
            )

    return trace_requests


def get_api_response(url):
    response = requests.get(
        url,
        headers={
            "Authorization": f"Token {api_token}",
            "Accept": "application/json",
        },
        verify=cert_path_absolute,
    )

    response.raise_for_status()

    return response.json()


def get_interface_trace(target):
    url = f"{NB_URL}/api/dcim/interfaces/{target['id']}/trace/"

    return get_api_response(url)


def node_key(node):
    return (
        node.get("url", ""),
        str(node.get("id", "")),
        node.get("display", ""),
    )


def path_signature(path):
    return tuple(
        tuple(sorted(node_key(node) for node in node_group))
        for node_group in path
    )


def is_target_node(node, target):
    return (
        node.get("id") == target["id"]
        and f"/{target['endpoint']}/" in node.get("url", "")
    )


def node_endpoint(node):
    url = node.get("url", "")

    if "/front-ports/" in url:
        return "front-ports"

    if "/interfaces/" in url:
        return "interfaces"

    return None


def target_from_node(node):
    endpoint = node_endpoint(node)

    if endpoint is None or node.get("id") is None:
        return None

    return {
        "endpoint": endpoint,
        "id": node["id"],
    }


def target_group_index(path, target):
    for index, node_group in enumerate(path):
        if any(is_target_node(node, target) for node in node_group):
            return index

    return None


def target_only_group(node_group, target):
    target_nodes = [
        node for node in node_group
        if is_target_node(node, target)
    ]

    return target_nodes or node_group


def unique_paths(cable_paths):
    seen = set()

    for cable_path in cable_paths:
        path = cable_path.get("path", [])
        signature = path_signature(path)
        reverse_signature = tuple(reversed(signature))
        canonical_signature = min(signature, reverse_signature)

        if canonical_signature in seen:
            continue

        seen.add(canonical_signature)
        yield path


def paths_from_target(path, target):
    index = target_group_index(path, target)

    if index is None:
        return [path]

    target_group = target_only_group(path[index], target)
    paths = []

    if index < len(path) - 1:
        paths.append([target_group] + path[index + 1:])

    if index > 0:
        paths.append([target_group] + list(reversed(path[:index])))

    return paths or [[target_group]]


def path_contains_target(path, target):
    return any(
        is_target_node(node, target)
        for node_group in path
        for node in node_group
    )


def get_path_trace(target, exclude_target=None):
    url = f"{NB_URL}/api/dcim/{target['endpoint']}/{target['id']}/paths/"
    cable_paths = get_api_response(url)
    trace = []

    for path in unique_paths(cable_paths):
        for target_path in paths_from_target(path, target):
            if exclude_target and path_contains_target(target_path[1:], exclude_target):
                continue

            for src_list, dst_list in zip(target_path, target_path[1:]):
                trace.append((src_list, None, dst_list))

    return trace


def first_front_port_target(trace):
    for segment in trace:
        for node_group in (segment[0], segment[2]):
            for node in node_group:
                if node_endpoint(node) == "front-ports":
                    return target_from_node(node)

    return None


def get_trace_segments(target):
    if target["type"] == "front_port":
        return get_path_trace(target)

    trace = get_interface_trace(target)
    front_port_target = first_front_port_target(trace)

    if front_port_target is None:
        return trace

    return trace + get_path_trace(front_port_target, exclude_target=target)


def get_trace(device_name, interface_name):
    target = get_trace_target(device_name, interface_name)
    trace = get_trace_segments(target)

    return {
        "device": target["device"],
        "interface": target["name"],
        "trace": trace,
    }


def get_traces(trace_requests, workers):
    results = [None] * len(trace_requests)
    max_workers = min(workers, len(trace_requests))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(get_trace, device_name, interface_name): index
            for index, (device_name, interface_name) in enumerate(trace_requests)
        }

        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()

    return results


def segment_endpoint_pairs(src_list, dst_list):
    if len(src_list) == len(dst_list):
        return zip(src_list, dst_list)

    if len(src_list) == 1:
        return ((src_list[0], dst) for dst in dst_list)

    if len(dst_list) == 1:
        return ((src, dst_list[0]) for src in src_list)

    return ((src, dst) for src in src_list for dst in dst_list)


def trace_rows(traces):
    for trace_result in traces:
        trace_name = f"{trace_result['device']} {trace_result['interface']}"
        previous_step = None

        for step, segment in enumerate(trace_result["trace"], start=1):
            src_list = segment[0]
            dst_list = segment[2]
            is_parallel = len(src_list) > 1 or len(dst_list) > 1

            for src, dst in segment_endpoint_pairs(src_list, dst_list):
                current_step = ""

                if step != previous_step:
                    current_step = step
                    previous_step = step

                src_device = get_device_info(src.get("device"))
                dst_device = get_device_info(dst.get("device"))

                yield {
                    "parallel": is_parallel,
                    "values": [
                        trace_name,
                        current_step,
                        src["display"],
                        src_device["display"],
                        src_device["rack"],
                        src_device["site"],
                        dst["display"],
                        dst_device["display"],
                        dst_device["rack"],
                        dst_device["site"],
                    ],
                }


def write_csv(output_file, traces):
    with open(output_file, "w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.writer(csvfile, delimiter=";")
        writer.writerow(HEADERS)

        for row in trace_rows(traces):
            writer.writerow([
                csv_text(value) if column_index != 1 else value
                for column_index, value in enumerate(row["values"])
            ])


def excel_column_name(index):
    name = ""

    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name

    return name


def excel_cell_ref(row_index, column_index):
    return f"{excel_column_name(column_index)}{row_index}"


def excel_cell(value, row_index, column_index, style_id):
    cell_ref = excel_cell_ref(row_index, column_index)
    style = f' s="{style_id}"' if style_id else ""

    if value is None or value == "":
        return f'<c r="{cell_ref}"{style}/>'

    if isinstance(value, int):
        return f'<c r="{cell_ref}"{style}><v>{value}</v></c>'

    return (
        f'<c r="{cell_ref}" t="inlineStr"{style}>'
        f"<is><t>{escape(str(value))}</t></is></c>"
    )


def xlsx_styles_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="10"/><name val="Aptos"/></font>
    <font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Aptos"/></font>
    <font><b/><sz val="10"/><color rgb="FF1F2937"/><name val="Aptos"/></font>
  </fonts>
  <fills count="6">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E79"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF6F8FB"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFEAF3F8"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD9E2EC"/></left>
      <right style="thin"><color rgb="FFD9E2EC"/></right>
      <top style="thin"><color rgb="FFD9E2EC"/></top>
      <bottom style="thin"><color rgb="FFD9E2EC"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="5">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="5" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>"""


def write_xlsx(output_file, traces):
    rows = list(trace_rows(traces))
    last_row = len(rows) + 1
    last_column = excel_column_name(len(HEADERS))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    column_widths = [34, 8, 24, 28, 14, 28, 24, 28, 14, 28]
    cols_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(column_widths, start=1)
    )

    header_cells = "".join(
        excel_cell(header, 1, column_index, 1)
        for column_index, header in enumerate(HEADERS, start=1)
    )
    sheet_rows = [f'<row r="1" ht="22" customHeight="1">{header_cells}</row>']

    for row_index, row in enumerate(rows, start=2):
        style_id = 4 if row["parallel"] else 2 + (row_index % 2)
        cells = "".join(
            excel_cell(value, row_index, column_index, style_id)
            for column_index, value in enumerate(row["values"], start=1)
        )
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft" activeCell="A2" sqref="A2"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="16"/>
  <cols>{cols_xml}</cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  <autoFilter ref="A1:{last_column}{last_row}"/>
  <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>"""

    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Trace Path" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""",
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/styles.xml": xlsx_styles_xml(),
        "docProps/core.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>get-cable-trace</dc:creator>
  <cp:lastModifiedBy>get-cable-trace</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>""",
        "docProps/app.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>get-cable-trace</Application>
</Properties>""",
    }

    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as xlsx:
        for filename, content in files.items():
            xlsx.writestr(filename, content)


def write_output(output_file, traces):
    extension = os.path.splitext(output_file)[1].lower()

    if extension == ".csv":
        write_csv(output_file, traces)
        return

    if extension in ("", ".xlsx"):
        write_xlsx(output_file, traces)
        return

    raise SystemExit("Extension de sortie non supportée. Utilisez .xlsx ou .csv.")


def main():
    args = parse_args()
    trace_args = args.trace or TRACES

    if args.workers < 1:
        raise SystemExit("Le nombre de workers doit être supérieur ou égal à 1.")

    configure_netbox()
    trace_requests = expand_trace_requests(trace_args)
    traces = get_traces(trace_requests, args.workers)
    write_output(args.output, traces)

    print(f"Fichier généré : {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()

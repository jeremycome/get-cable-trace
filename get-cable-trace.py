#!/usr/bin/env python3

import csv
import argparse
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

###########################################################################
# Configuration
###########################################################################

DEVICE = "p-1-th3"
INTERFACE = "FH0/0/0/1"
TRACES = [
    (DEVICE, INTERFACE),
]

OUTPUT_FILE = "tracepath.csv"

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
        description="Exporte un ou plusieurs Trace Path NetBox vers un CSV."
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
        help=f"Fichier CSV de sortie. Défaut: {OUTPUT_FILE}",
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


def get_path_trace(target):
    url = f"{NB_URL}/api/dcim/{target['endpoint']}/{target['id']}/paths/"
    cable_paths = get_api_response(url)
    trace = []

    for path in unique_paths(cable_paths):
        for target_path in paths_from_target(path, target):
            for src_list, dst_list in zip(target_path, target_path[1:]):
                trace.append((src_list, None, dst_list))

    return trace


def get_trace(device_name, interface_name):
    target = get_trace_target(device_name, interface_name)
    trace = get_path_trace(target)

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


def write_csv(output_file, traces):
    with open(output_file, "w", newline="", encoding="utf-8-sig") as csvfile:

        writer = csv.writer(csvfile, delimiter=";")

        writer.writerow([
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
        ])

        for trace_result in traces:
            trace_name = f"{trace_result['device']} {trace_result['interface']}"
            previous_step = None

            for step, segment in enumerate(trace_result["trace"], start=1):

                src_list = segment[0]
                dst_list = segment[2]

                for src, dst in segment_endpoint_pairs(src_list, dst_list):

                    current_step = ""

                    if step != previous_step:
                        current_step = step
                        previous_step = step

                    src_device = get_device_info(src.get("device"))
                    dst_device = get_device_info(dst.get("device"))

                    writer.writerow([
                        csv_text(trace_name),
                        current_step,

                        csv_text(src["display"]),
                        csv_text(src_device["display"]),
                        csv_text(src_device["rack"]),
                        csv_text(src_device["site"]),

                        csv_text(dst["display"]),
                        csv_text(dst_device["display"]),
                        csv_text(dst_device["rack"]),
                        csv_text(dst_device["site"]),
                    ])


def main():
    args = parse_args()
    trace_args = args.trace or TRACES

    if args.workers < 1:
        raise SystemExit("Le nombre de workers doit être supérieur ou égal à 1.")

    configure_netbox()
    trace_requests = expand_trace_requests(trace_args)
    traces = get_traces(trace_requests, args.workers)
    write_csv(args.output, traces)

    print(f"CSV généré : {os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()

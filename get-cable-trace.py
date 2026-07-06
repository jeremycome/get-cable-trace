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
            "Trace à exporter: DEVICE INTERFACE ou seulement DEVICE pour "
            "tracer toutes les interfaces du device. Peut être utilisé plusieurs fois."
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

    if iface is None:
        raise ValueError(f"Interface introuvable: {device_name} {interface_name}")

    return iface


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
                "Chaque --trace doit contenir DEVICE ou DEVICE INTERFACE."
            )

    return trace_requests


def get_trace(device_name, interface_name):
    iface = get_interface(device_name, interface_name)
    url = f"{NB_URL}/api/dcim/interfaces/{iface.id}/trace/"

    response = requests.get(
        url,
        headers={
            "Authorization": f"Token {api_token}",
            "Accept": "application/json",
        },
        verify=cert_path_absolute,
    )

    response.raise_for_status()

    return {
        "device": device_name,
        "interface": interface_name,
        "trace": response.json(),
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

                for src in src_list:
                    for dst in dst_list:

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

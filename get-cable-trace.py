#!/usr/bin/env python3

import csv
import os
import requests

from pynetbox import api

###########################################################################
# Configuration
###########################################################################

DEVICE = "p-1-th3"
INTERFACE = "FH0/0/0/1"

OUTPUT_FILE = "tracepath.csv"

api_token = os.environ.get("NETBOX_TOKEN")

if api_token is None:
    raise SystemExit("La variable NETBOX_TOKEN n'est pas définie.")

cert_path_relative = "~/configuration/tools/ca-alphatech.crt"
cert_path_absolute = os.path.abspath(os.path.expanduser(cert_path_relative))

os.environ["REQUESTS_CA_BUNDLE"] = cert_path_absolute

NB_URL = "https://netbox.alphalink.tech"

nb = api(
    url=NB_URL,
    token=api_token,
)

###########################################################################
# Fonctions
###########################################################################

def csv_text(value):
    """Force LibreOffice / Excel à interpréter la cellule comme du texte."""
    if value is None or value == "":
        return ""
    return f"'{value}"


device_cache = {}


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

    if device_id not in device_cache:

        d = nb.dcim.devices.get(device_id)

        device_cache[device_id] = {
            "display": d.name,
            "rack": d.rack.name if d.rack else "",
            "site": d.site.name if d.site else "",
        }

    return device_cache[device_id]


###########################################################################
# Recherche de l'interface
###########################################################################

iface = nb.dcim.interfaces.get(
    device=DEVICE,
    name=INTERFACE,
)

if iface is None:
    raise SystemExit("Interface introuvable.")

###########################################################################
# Trace Path
###########################################################################

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

trace = response.json()

###########################################################################
# Génération du CSV
###########################################################################

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as csvfile:

    writer = csv.writer(csvfile, delimiter=";")

    writer.writerow([
        "Etape",

        "Source",
        "Source Device",
        "Source Rack",
        "Source Site",

        "Destination",
        "Destination Device",
        "Destination Rack",
        "Destination Site",
    ])

    previous_step = None

    for step, segment in enumerate(trace, start=1):

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

print(f"CSV généré : {os.path.abspath(OUTPUT_FILE)}")

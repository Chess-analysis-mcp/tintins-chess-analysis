"""Tell requests from this computer apart from requests from other devices on the network.

Only matters when the board listens beyond loopback (Settings: "Allow other devices on my network to
connect"). A few actions stay tied to this computer even then: the app-mode quit signals (a phone
closing its tab must not quit the app) and reading back the saved Lichess token.
"""
from __future__ import annotations

import ipaddress

from starlette.requests import Request

# Starlette's TestClient reports this as the client host; treat it as local like the Host guard in
# app.py treats "testserver".
_TEST_CLIENT = "testclient"


def is_local_client(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host == _TEST_CLIENT:
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:  # "::ffff:127.0.0.1" on a "::" bind
        ip = ip.ipv4_mapped
    return ip.is_loopback

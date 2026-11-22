"""Stub DNS for SMTP tests that fake the connection but not the host guard.

``pipeline.notification_email`` resolves the configured SMTP host before it
connects and refuses an internal answer. These tests fake ``smtplib.SMTP`` and
use placeholder hosts such as ``smtp.example.test``, which do not resolve, so
the lookup is answered with a public address. The connection the test records
is then the pinned one.
"""

from __future__ import annotations

import socket

from app import outbound_guard

PUBLIC_SMTP_ADDRESS = "93.184.216.34"


def stub_smtp_dns(monkeypatch) -> None:
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_SMTP_ADDRESS, 0))]

    monkeypatch.setattr(outbound_guard.socket, "getaddrinfo", getaddrinfo)

"""Renderer socket I/O; presentation and audio ownership stay with callers."""
import json
import socket
from uuid import uuid4

ADDRESS = "\0cube-display"


def send_state(channel, message):
    channel.sendto(message if isinstance(message, bytes) else message.encode("ascii"), ADDRESS)


def request(message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        channel.bind("\0cube-control-" + uuid4().hex)
        channel.connect(ADDRESS)
        channel.send(message.encode("ascii"))
        return json.loads(channel.recv(1024))

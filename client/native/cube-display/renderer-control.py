#!/usr/bin/env python3
"""Manual Pi-local Cube Display controls; no rendering loop."""

import argparse
import json
import math
import socket
import sys
import time
from pathlib import Path
from uuid import uuid4

# Reuse the client-side canonical socket address when invoked as a standalone CLI.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
from display.transport import ADDRESS


def command_message(tokens):
    if tokens in ([mode] for mode in ("idle", "wake", "listening", "thinking", "followup")):
        return tokens[0].encode("ascii"), "send_only"
    if len(tokens) == 2 and tokens[0] == "speech":
        try:
            level = float(tokens[1])
        except ValueError:
            raise ValueError("speech requires a level from 0 to 1") from None
        if not math.isfinite(level) or not 0 <= level <= 1:
            raise ValueError("speech requires a level from 0 to 1")
        return f"speech {level}".encode("ascii"), "send_only"
    if not tokens or tokens[0] != "control" or len(tokens) not in (2, 3):
        raise ValueError("use a native control command or assistant display state")
    operation = tokens[1]
    if operation in ("get_status", "get_content_status", "clear_content"):
        if len(tokens) != 2:
            raise ValueError("this command takes no value")
    elif operation in ("show_text", "show_icon", "set_display", "set_brightness"):
        if len(tokens) != 3:
            raise ValueError("this command requires one value")
        value = tokens[2]
        if operation == "set_display" and value not in ("on", "off"):
            raise ValueError("state must be on or off")
        if operation == "set_brightness" and value not in {str(n) for n in range(101)}:
            raise ValueError("brightness must be an integer from 0 to 100")
    else:
        raise ValueError("unknown renderer command")
    message = " ".join(tokens).encode("utf-8")
    if len(message) > 128 or b"\0" in message:
        raise ValueError("command exceeds 128 bytes or contains NUL")
    kind = ("content_status" if operation == "get_content_status" else
            "accepted" if operation in ("show_text", "show_icon", "clear_content") else "status")
    return message, kind


def valid_reply(data, kind):
    if type(data) is not dict:
        return False
    if kind == "accepted":
        return set(data) == {"accepted"} and data["accepted"] is True
    if kind == "status":
        return (set(data) == {"display_enabled", "master_brightness_percent"}
                and type(data["display_enabled"]) is bool
                and type(data["master_brightness_percent"]) is int
                and 0 <= data["master_brightness_percent"] <= 100)
    if kind == "content_status":
        if set(data) == {"available"} and data["available"] is False:
            return True
        return (set(data) == {"available", "presentation", "target_pixels", "progress", "pending"}
                and data["available"] is True
                and type(data["presentation"]) is str
                and data["presentation"] in {"full", "forming_content", "content", "morphing_content", "releasing_content"}
                and type(data["target_pixels"]) is int and 1 <= data["target_pixels"] <= 4096
                and type(data["progress"]) in (int, float) and 0 <= data["progress"] <= 1
                and type(data["pending"]) is bool)
    return False


def send_command(tokens):
    message, kind = command_message(tokens)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        channel.bind("\0cube-manual-" + uuid4().hex)
        channel.connect(ADDRESS)
        channel.send(message)
        if kind == "send_only":
            return None  # Assistant messages have never had acknowledgements.
        payload, _, flags, _ = channel.recvmsg(1024)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("truncated renderer reply")
        data = json.loads(payload)
    if not valid_reply(data, kind):
        raise ValueError("renderer rejected the command or returned an incompatible reply")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="+", help="e.g. control show_text '23°', demo dissolve, or idle")
    args = parser.parse_args(argv)
    try:
        if args.command[0] == "demo":
            if len(args.command) != 2 or args.command[1] != "dissolve":
                raise ValueError("use demo dissolve")
            for command in (["control", "clear_content"],
                            ["control", "show_text", "11:37"],
                            ["control", "clear_content"]):
                send_command(command)
                time.sleep(2)
            print(json.dumps({"demo": args.command[1], "complete": True}))
            return 0
        reply = send_command(args.command)
    except (OSError, ValueError, UnicodeError) as error:
        print(f"Renderer control failed: {error}", file=sys.stderr)
        return 1
    if reply is not None:
        print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

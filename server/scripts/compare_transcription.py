#!/usr/bin/env python3
"""Compare two /transcribe endpoints without invoking commands or /voice."""
import argparse
import json
import math
from pathlib import Path
import sys
from time import perf_counter

import httpx


def positive_timeout(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return number


def upload(client, url, filename, audio):
    response = client.post(url.rstrip("/") + "/transcribe",
                           files={"file": (filename, audio, "application/octet-stream")})
    response.raise_for_status()
    result = response.json()
    if (not isinstance(result, dict)
            or not isinstance(result.get("text"), str)
            or not isinstance(result.get("language"), str)
            or type(result.get("processing_time")) not in {int, float}
            or not math.isfinite(result["processing_time"])
            or result["processing_time"] < 0):
        raise ValueError("Invalid transcription response")
    return result


def main(argv=None, *, transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--ai-node-url", required=True)
    parser.add_argument("--timeout", type=positive_timeout, default=60.0,
                        help="positive HTTP phase timeout in seconds (default: 60); no retries")
    parser.add_argument("--warmup", action="store_true",
                        help="send one disposable AI-node request before measurements")
    args = parser.parse_args(argv)
    try:
        audio = args.audio.read_bytes()
    except OSError as error:
        print(f"Cannot read recording: {error}", file=sys.stderr)
        return 1
    print(f"AI-node warm-up requested: {'yes' if args.warmup else 'no'}")
    results = {}
    failed = False
    with httpx.Client(timeout=args.timeout, transport=transport,
                      follow_redirects=False, trust_env=False) as client:
        if args.warmup:
            try:
                upload(client, args.ai_node_url, args.audio.name, audio)
            except (httpx.HTTPError, ValueError) as error:
                print(f"AI-node warm-up failed: {type(error).__name__}", file=sys.stderr)
                return 1
            print("AI-node warm-up completed.")
        for label, url in (("Backend", args.backend_url), ("AI node", args.ai_node_url)):
            started = perf_counter()
            try:
                result = upload(client, url, args.audio.name, audio)
            except (httpx.HTTPError, ValueError) as error:
                elapsed = perf_counter() - started
                print(f"{label}: failed ({type(error).__name__}); wall-clock latency: {elapsed:.3f}s")
                failed = True
                continue
            elapsed = perf_counter() - started
            results[label] = result
            print(f"{label} transcript: {json.dumps(result['text'], ensure_ascii=False)}")
            print(f"{label} language: {result['language']}")
            print(f"{label} processing time: {result['processing_time']:.3f}s")
            print(f"{label} wall-clock latency: {elapsed:.3f}s")
    if len(results) == 2:
        equal = results["Backend"]["text"] == results["AI node"]["text"]
        print(f"Exact transcript equality: {str(equal).lower()}")
    else:
        print("Exact transcript equality: unavailable")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

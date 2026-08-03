#!/usr/bin/env python3
"""
Patch py_clob_client_v2 HTTP helper for better transport resiliency:
- keep HTTP/2 primary client
- add HTTP/1.1 fallback client
- retry once on request exceptions (especially RemoteProtocolError) via HTTP/1.1

Idempotent: safe to run multiple times.
"""

from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "_pyclob_http11_fallback_retry"


def find_target_file(project_root: Path) -> Path:
    candidates = sorted(
        project_root.glob("venv/lib/python*/site-packages/py_clob_client_v2/http_helpers/helpers.py")
    )
    if not candidates:
        raise FileNotFoundError("Cannot find py_clob_client_v2 helpers.py inside venv.")
    return candidates[-1]


def apply_patch_to_text(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False

    out = text

    anchor_client = "_http_client = httpx.Client(http2=True)\n"
    if anchor_client in out and "_http_client_http1" not in out:
        out = out.replace(
            anchor_client,
            anchor_client
            + "_http_client_http1 = httpx.Client(http2=False)\n"
            + "\n"
            + "def _request_with_client(client: httpx.Client, endpoint: str, method: str, headers: dict, data):\n"
            + "    if isinstance(data, str):\n"
            + "        return client.request(\n"
            + "            method=method,\n"
            + "            url=endpoint,\n"
            + "            headers=headers,\n"
            + "            content=data.encode(\"utf-8\"),\n"
            + "        )\n"
            + "    return client.request(\n"
            + "        method=method,\n"
            + "        url=endpoint,\n"
            + "        headers=headers,\n"
            + "        json=data,\n"
            + "    )\n",
            1,
        )

    old_body = (
        "def request(endpoint: str, method: str, headers=None, data=None):\n"
        "    try:\n"
        "        headers = overloadHeaders(method, headers)\n"
        "        if isinstance(data, str):\n"
        "            # Pre-serialized body: send exact bytes\n"
        "            resp = _http_client.request(\n"
        "                method=method,\n"
        "                url=endpoint,\n"
        "                headers=headers,\n"
        "                content=data.encode(\"utf-8\"),\n"
        "            )\n"
        "        else:\n"
        "            resp = _http_client.request(\n"
        "                method=method,\n"
        "                url=endpoint,\n"
        "                headers=headers,\n"
        "                json=data,\n"
        "            )\n"
        "\n"
        "        if resp.status_code != 200:\n"
        "            raise PolyApiException(resp)\n"
        "\n"
        "        try:\n"
        "            return resp.json()\n"
        "        except ValueError:\n"
        "            return resp.text\n"
        "\n"
        "    except httpx.RequestError:\n"
        "        raise PolyApiException(error_msg=\"Request exception!\")\n"
    )
    new_body = (
        "def request(endpoint: str, method: str, headers=None, data=None):\n"
        "    # " + MARKER + "\n"
        "    try:\n"
        "        headers = overloadHeaders(method, headers)\n"
        "        resp = _request_with_client(_http_client, endpoint, method, headers, data)\n"
        "\n"
        "        if resp.status_code != 200:\n"
        "            raise PolyApiException(resp)\n"
        "\n"
        "        try:\n"
        "            return resp.json()\n"
        "        except ValueError:\n"
        "            return resp.text\n"
        "\n"
        "    except httpx.RequestError:\n"
        "        try:\n"
        "            fallback_headers = dict(overloadHeaders(method, headers))\n"
        "            fallback_headers[\"Connection\"] = \"close\"\n"
        "            resp = _request_with_client(_http_client_http1, endpoint, method, fallback_headers, data)\n"
        "            if resp.status_code != 200:\n"
        "                raise PolyApiException(resp)\n"
        "            try:\n"
        "                return resp.json()\n"
        "            except ValueError:\n"
        "                return resp.text\n"
        "        except httpx.RequestError:\n"
        "            raise PolyApiException(error_msg=\"Request exception!\")\n"
    )

    if old_body in out:
        out = out.replace(old_body, new_body, 1)

    changed = out != text
    return out, changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Only check patch status.")
    parser.add_argument("--quiet", action="store_true", help="Minimal output.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    target = find_target_file(project_root)
    original = target.read_text(encoding="utf-8")
    patched, changed = apply_patch_to_text(original)

    already = MARKER in original
    if args.check:
        if not args.quiet:
            print(f"target={target}")
            print("status=patched" if already else "status=not_patched")
        return 0 if already else 1

    if changed:
        target.write_text(patched, encoding="utf-8")
        if not args.quiet:
            print(f"patched {target}")
    else:
        if not args.quiet:
            print(f"already patched {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLOB network round-trip probe: no credentials, no orders. Python 3 stdlib only (runs on a bare VM).

    python3 clob_rtt_probe.py                 # 40 rounds, prints JSON summary
    python3 clob_rtt_probe.py --rounds 100 --out result.json

Per round, over one kept-alive TLS connection:
  - tcp_rtt:   TCP handshake time on a fresh socket (network RTT to the Cloudflare edge)
  - get_time:  GET /time (served by the origin, not cached)
  - post_401:  POST /order with an empty body and no auth -> 401 from the order path (nothing is placed)
origin_extra = request time - tcp_rtt, i.e. edge -> origin and processing.
"""
import argparse, http.client, json, platform, socket, ssl, statistics, time

HOST = "clob.polymarket.com"


def q(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))] * 1000, 1)


def summary(xs):
    return {"p10_ms": q(xs, .1), "p50_ms": q(xs, .5), "p90_ms": q(xs, .9), "n": len(xs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--gap", type=float, default=0.5, help="seconds between rounds")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    ip = socket.getaddrinfo(HOST, 443, socket.AF_INET)[0][4][0]
    tcp, get_t, post_t = [], [], []
    conn = http.client.HTTPSConnection(HOST, 443, timeout=10, context=ssl.create_default_context())
    colo = None
    for _ in range(a.rounds):
        t0 = time.perf_counter()
        s = socket.create_connection((ip, 443), timeout=10)
        tcp.append(time.perf_counter() - t0)
        s.close()
        for path, method, body, sink in (("/time", "GET", None, get_t), ("/order", "POST", b"{}", post_t)):
            t0 = time.perf_counter()
            conn.request(method, path, body=body, headers={"content-type": "application/json", "connection": "keep-alive"})
            r = conn.getresponse()
            r.read()
            sink.append(time.perf_counter() - t0)
            colo = (r.getheader("cf-ray") or "-").split("-")[-1]
        time.sleep(a.gap)
    res = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": platform.node(), "edge_ip": ip,
           "cloudflare_colo": colo, "tcp_rtt": summary(tcp), "get_time": summary(get_t), "post_order_401": summary(post_t),
           "origin_extra_p50_ms": round((statistics.median(post_t) - statistics.median(tcp)) * 1000, 1)}
    print(json.dumps(res, indent=1))
    if a.out:
        open(a.out, "w").write(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

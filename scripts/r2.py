#!/usr/bin/env python3
"""Minimal S3/SigV4 client for Cloudflare R2 — stdlib only."""
import hashlib, hmac, os, sys, urllib.request, urllib.parse
from datetime import datetime, timezone

ACCOUNT = os.environ["R2_ACCOUNT"]
AKID = os.environ["R2_KEY_ID"]
SECRET = os.environ["R2_SECRET"]
HOST = f"{ACCOUNT}.r2.cloudflarestorage.com"
REGION, SERVICE = "auto", "s3"


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def request(method, bucket, key="", payload=b"", query="", content_type=None, extra=None):
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    path = "/" + bucket + ("/" + urllib.parse.quote(key) if key else "")
    headers = {"host": HOST, "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate}
    if content_type:
        headers["content-type"] = content_type
    for k, v in (extra or {}).items():
        headers[k.lower()] = v

    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canon = f"{method}\n{path}\n{query}\n{canon_headers}\n{signed}\n{payload_hash}"

    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    sts = f"AWS4-HMAC-SHA256\n{amzdate}\n{scope}\n{hashlib.sha256(canon.encode()).hexdigest()}"
    k = _sign(("AWS4" + SECRET).encode(), datestamp)
    k = _sign(k, REGION); k = _sign(k, SERVICE); k = _sign(k, "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={AKID}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    url = f"https://{HOST}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, data=payload or None, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, r.read()


def put(bucket, key, data, content_type):
    return request("PUT", bucket, key, data, content_type=content_type,
                   extra={"cache-control": "public, max-age=31536000, immutable"})


def ls(bucket, prefix=""):
    q = "list-type=2" + (f"&prefix={urllib.parse.quote(prefix)}" if prefix else "")
    # canonical query string must be sorted by key
    q = "&".join(sorted(q.split("&")))
    _, body = request("GET", bucket, "", b"", query=q)
    import re
    return re.findall(r"<Key>(.*?)</Key>", body.decode())


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "ls":
        keys = ls(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(f"{len(keys)} objects")
        for k in keys[:40]:
            print(" ", k)
    elif cmd == "put":
        bucket, key, path, ct = sys.argv[2:6]
        st, _ = put(bucket, key, open(path, "rb").read(), ct)
        print(st, key)

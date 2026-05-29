"""S3 presigned URLs — direct upload/download, no AWS creds at the client.

Same pattern as on AWS: a backend with credentials generates a short-lived URL
that a browser / mobile app / unauthenticated client can PUT to (or GET from)
directly. S3Proxy honours the signed URL like real S3 does.

Run from the repo root:

    uv run python examples/python/s3/presigned_upload.py
"""

from __future__ import annotations

import boto3
import httpx
from botocore.config import Config

from oblako.services.platform import Oblako

BUCKET = "oblako-presigned"
KEY = "uploads/hello.txt"
BODY = b"hello from a presigned PUT (no AWS creds on the client)"


def main() -> None:
    o = Oblako()
    o.s3.wait_ready(timeout=2) or o.s3.start()
    s3 = o.s3.get_client()

    if BUCKET not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket=BUCKET)

    # 1. Backend (with credentials) issues short-lived signed URLs.
    # `payload_signing_enabled=False` is the AWS-recommended setting for
    # presigned PUTs (the client is anonymous; it can't compute the SHA256 hash
    # that signing the payload would require).
    signer = boto3.client(
        "s3",
        endpoint_url=o.s3.endpoint_url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path", "payload_signing_enabled": False},
        ),
    )
    put_url = signer.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": KEY},
        ExpiresIn=300,
    )
    get_url = signer.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": KEY},
        ExpiresIn=300,
    )
    print("PUT URL:", put_url[:90], "...")
    print("GET URL:", get_url[:90], "...")

    # 2. Anonymous client (no creds) uploads via the signed PUT.
    resp = httpx.put(put_url, content=BODY)
    print(f"upload: HTTP {resp.status_code}  ({len(BODY)} bytes)")

    # 3. Anonymous client downloads via the signed GET.
    downloaded = httpx.get(get_url).content
    print(f"download: {len(downloaded)} bytes — matches: {downloaded == BODY}")
    print(f"content:  {downloaded.decode()!r}")


if __name__ == "__main__":
    main()

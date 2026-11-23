"""Encrypt analytics snapshots before they touch the public queue repo.

Comments and follower demographics are about real people, and the repo is public, so
snapshots are committed only as AES-256 (openssl, PBKDF2) with STATS_KEY, which lives in
GitHub secrets and the laptop's .env. Same openssl on both sides; no Python packages.
"""
import os
import subprocess

ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:STATS_KEY"]


def _key() -> dict:
    if not os.environ.get("STATS_KEY"):
        raise SystemExit("STATS_KEY is not set (GitHub secret / .env)")
    return os.environ


def encrypt(data: bytes) -> bytes:
    return subprocess.run(["openssl", *ARGS], input=data, capture_output=True, check=True, env=_key()).stdout


def decrypt(blob: bytes) -> bytes:
    return subprocess.run(["openssl", *ARGS, "-d"], input=blob, capture_output=True, check=True, env=_key()).stdout

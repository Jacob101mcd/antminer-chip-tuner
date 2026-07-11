#!/usr/bin/env bash
# Regenerate tests/fixtures/whatsminer_auth_vectors.json — golden auth vectors
# for the Whatsminer (stock MicroBT) WhatsminerMinerAPI auth helpers.
#
# Inputs are fixed:
#   password = "admin"
#   salt     = "abcdefgh"
#   newsalt  = "1234567"
#   time     = "20250509120000"
#
# Outputs:
#   md5crypt_hash:  the part after the third '$' of `openssl passwd -1 -salt $salt $password`
#   aeskey:         sha256(md5crypt_hash) — 32 bytes (AES-256 key)
#   sign:           md5crypt(md5crypt(password, salt) + time[-4:], newsalt) — same suffix-only form
#   ciphertext:     AES-256-ECB(PKCS7 pad to 16) over a known plaintext, base64-encoded
#
# Run via:  bash tests/fixtures/whatsminer_auth_vectors_generate.sh
# Requires: openssl, python3 with `cryptography` installed.

set -euo pipefail

password="admin"
salt="abcdefgh"
newsalt="1234567"
time_str="20250509120000"

md5crypt_full=$(openssl passwd -1 -salt "$salt" "$password")
md5crypt_hash=$(echo -n "$md5crypt_full" | awk -F'$' '{print $4}')

aeskey_hex=$(echo -n "$md5crypt_hash" | shasum -a 256 | awk '{print $1}')

last4="${time_str: -4}"
sign_input="${md5crypt_hash}${last4}"
sign_full=$(openssl passwd -1 -salt "$newsalt" "$sign_input")
sign=$(echo -n "$sign_full" | awk -F'$' '{print $4}')

# AES-256-ECB encrypt a known plaintext for round-trip verification.
plaintext='{"cmd":"get_version"}'
ciphertext_b64=$(python3 - <<PY
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
aeskey = bytes.fromhex("$aeskey_hex")
padder = padding.PKCS7(128).padder()
padded = padder.update(b'$plaintext') + padder.finalize()
cipher = Cipher(algorithms.AES256(aeskey), modes.ECB())
enc = cipher.encryptor()
ct = enc.update(padded) + enc.finalize()
print(base64.b64encode(ct).decode("ascii"))
PY
)

cat <<JSON
{
  "_meta_generated_with": "tests/fixtures/whatsminer_auth_vectors_generate.sh — openssl passwd -1 + python cryptography",
  "password": "$password",
  "salt": "$salt",
  "newsalt": "$newsalt",
  "time": "$time_str",
  "expected_md5crypt_hash": "$md5crypt_hash",
  "expected_aeskey_hex": "$aeskey_hex",
  "expected_sign": "$sign",
  "round_trip_plaintext": "$(printf '%s' "$plaintext" | python3 -c 'import sys, json; print(json.dumps(sys.stdin.read())[1:-1])')",
  "round_trip_ciphertext_b64": "$ciphertext_b64"
}
JSON

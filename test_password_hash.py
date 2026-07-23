#!/usr/bin/env python3
"""
Test script to verify MSpa password MD5 hashing.
This helps users verify their password is being encoded correctly.

Usage:
    python test_password_hash.py

The script will prompt for your password and show you the MD5 hash that would be sent to the API.
Compare this with the logs from Home Assistant to see if they match.
"""
import hashlib
import getpass
import requests
import json

def test_password_encoding():
    print("=" * 60)
    print("MSpa Password Hash Test Utility")
    print("=" * 60)
    print()

    email_raw = input("Enter your MSpa account email: ")
    password_raw = getpass.getpass("Enter your MSpa password: ")

    # Strip whitespace (common issue from copy/paste)
    email = email_raw.strip()
    password = password_raw.strip()

    print()
    print("-" * 60)
    print("Password Analysis:")
    print("-" * 60)
    print(f"Email: {email}")

    # Warn about whitespace
    if len(email_raw) != len(email):
        print(f"⚠️  WARNING: Email had {len(email_raw) - len(email)} leading/trailing space(s) - automatically removed")
    if len(password_raw) != len(password):
        print(f"⚠️  WARNING: Password had {len(password_raw) - len(password)} leading/trailing space(s) - automatically removed")
        print(f"   This is a common issue when copy/pasting credentials!")

    print(f"Password length: {len(password)} characters")
    print(f"Password contains special chars: {any(not c.isalnum() for c in password)}")

    if any(not c.isalnum() for c in password):
        print(f"⚠️  NOTE: MSpa passwords should only contain letters and numbers (6-15 characters)")
        print(f"   Special characters may cause issues!")

    # Generate MD5 hash
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()

    print()
    print(f"MD5 Hash (full): {password_hash}")
    print(f"MD5 Hash length: {len(password_hash)} characters")
    print(f"MD5 Hash (first 6): {password_hash[:6]}")
    print()

    # Test authentication with the API
    print("-" * 60)
    print("Testing Authentication with MSpa API...")
    print("-" * 60)

    try:
        # Generate nonce and timestamp
        import time
        import random
        import string

        nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
        ts = str(int(time.time()))

        # Use the correct app_id and app_secret from mspa_api.py
        app_id = "e1c8e068f9ca11eba4dc0242ac120002"
        app_secret = "87025c9ecd18906d27225fe79cb68349"

        # Build signature using the correct algorithm
        sign_string = app_id + "," + app_secret + "," + nonce + "," + ts
        sign = hashlib.md5(sign_string.encode("utf-8")).hexdigest().upper()

        headers = {
            "push_type": "Android",
            "authorization": "token",
            "appid": app_id,
            "nonce": nonce,
            "ts": ts,
            "lan_code": "de",
            "sign": sign,
            "content-type": "application/json; charset=UTF-8",
            "accept-encoding": "gzip",
            "user-agent": "okhttp/4.9.0"
        }

        payload = {
            "account": email,
            "app_id": app_id,
            "password": password_hash,
            "brand": "",
            "registration_id": "",
            "push_type": "android",
            "lan_code": "EN",
            "country": ""
        }

        response = requests.post(
            "https://api.iot.the-mspa.com/api/enduser/get_token/",
            headers=headers,
            json=payload,
            timeout=30
        )

        print(f"HTTP Status Code: {response.status_code}")
        print()

        response_json = response.json()
        print("API Response:")
        print(json.dumps(response_json, indent=2))
        print()

        # Analyze the response
        if response_json.get("code") == 16019:
            print("❌ AUTHENTICATION FAILED: Password is incorrect")
            print()
            print("Possible reasons:")
            print("  1. The password you entered doesn't match your MSpa app password")
            print("  2. You need to reset your password in the MSpa mobile app")
            print("  3. Special characters in your password may be causing encoding issues")
            print()
            print("Recommendations:")
            print("  • Open the MSpa mobile app and verify you can log in")
            print("  • Try resetting your password to use only letters and numbers")
            print("  • Use a simple password temporarily to test if special chars are the issue")
        elif "token" in response_json.get("data", {}):
            print("✅ AUTHENTICATION SUCCESSFUL!")
            print()
            token = response_json["data"]["token"]
            print(f"Token received (first 20 chars): {token[:20]}...")
            print()
            print("Your credentials are correct. The Home Assistant integration should work.")
        else:
            print("⚠️  Unexpected response from API")
            print()
            print(f"Error code: {response_json.get('code')}")
            print(f"Message: {response_json.get('message')}")

    except Exception as e:
        print(f"❌ ERROR: {str(e)}")
        print()
        print("Could not connect to MSpa API. Check your internet connection.")

    print()
    print("=" * 60)

if __name__ == "__main__":
    test_password_encoding()


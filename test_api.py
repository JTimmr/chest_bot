#!/usr/bin/env python3
"""
Quick test script for the Chest Bot API.
Just run: python test_api.py
"""

import json
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests")
    sys.exit(1)

# ===========================================================================
# CONFIGURE THESE VALUES
# ===========================================================================
API_KEY  = "W7b9UXOVXgtR6aB2oKIVcqX4rcs5AVqIviOC14C6d1S7AFuldbRiTLfStm7Y2Y8pU4nbOkll3vpwgJmsdwCqz718TdV3X7r1jrwqPdUw7PQWfD8uiUU2SOfkCWhaywBOJIfibdV2COSQQmhB1DZYxgLuGugH69WKs83E37uJD2xAYonr0QPjKG1dAEkVmPRgtMizz90HZQuvRnHQ5g8AQJJ3rHW9gXokGGhpuBId25Ns8Nym7w1FIDHwazXbqBB0"
# ===========================================================================

BASE_URL = "https://fbctoapi.xyz"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def pretty(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def test_endpoint(path: str, auth: bool = True, params: dict | None = None):
    url = f"{BASE_URL}{path}"
    headers = {"X-API-Key": API_KEY} if auth else {}

    print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
    print(f"{BOLD}GET {url}{RESET}")
    if params:
        print(f"    params: {params}")
    print(f"{CYAN}{'='*60}{RESET}")

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.ConnectionError:
        print(f"{RED}CONNECTION FAILED — is the bot running and the port open?{RESET}")
        return False
    except requests.Timeout:
        print(f"{RED}REQUEST TIMED OUT{RESET}")
        return False

    status_color = GREEN if resp.status_code == 200 else RED
    print(f"Status: {status_color}{resp.status_code}{RESET}")

    try:
        data = resp.json()
        print(pretty(data))
    except Exception:
        print(resp.text[:500])

    return resp.status_code == 200


def main():
    print(f"{BOLD}Chest Bot API Test{RESET}")
    print(f"  Base URL : {BASE_URL}")
    print(f"  API Key  : ***{API_KEY[-4:] if len(API_KEY) > 4 else '???'}")

    results = {}

    # 1. Root (no auth)
    results["/ (root)"] = test_endpoint("/", auth=False)

    # 2. Health (no auth)
    results["/health"] = test_endpoint("/health", auth=False)

    # 3. Stats (auth required)
    results["/api/v1/stats"] = test_endpoint("/api/v1/stats")

    # 4. Leaderboard (auth required)
    results["/api/v1/leaderboard"] = test_endpoint("/api/v1/leaderboard")

    # 5. Recent transactions (auth required)
    results["/api/v1/recent"] = test_endpoint("/api/v1/recent", params={"limit": 5})

    # 6. Test auth rejection (no key should fail)
    print(f"\n{BOLD}{YELLOW}--- Testing auth rejection (no key) ---{RESET}")
    results["auth rejection"] = not test_endpoint("/api/v1/stats", auth=False)

    # Summary
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}SUMMARY{RESET}")
    print(f"{'='*60}")
    all_passed = True
    for name, passed in results.items():
        icon = f"{GREEN}✓ PASS{RESET}" if passed else f"{RED}✗ FAIL{RESET}"
        print(f"  {icon}  {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print(f"{GREEN}{BOLD}All tests passed!{RESET}")
    else:
        print(f"{RED}{BOLD}Some tests failed — check output above.{RESET}")
    print()


if __name__ == "__main__":
    main()

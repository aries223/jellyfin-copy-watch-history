#!/usr/bin/env python3
"""Copy watch data from one Jellyfin user to another.

Copies played marks, play counts, resume spots, last-played dates, favorites,
likes and ratings. Items are matched by ID.

  python3 jellyfin_copy_watch.py --users   list user names and IDs
  python3 jellyfin_copy_watch.py           dry run: change nothing, print counts
  python3 jellyfin_copy_watch.py --apply   copy for real, then check the result
  Add -O (--overwrite) to replace watch data the destination already has.

Privacy and safety:
  - The source user is only read, never changed.
  - Items where the destination user already has watch data are skipped, so
    nothing is overwritten and a second run is harmless. With -O those items
    get the source's data instead. Destination data on items the source has
    none for is never touched. The server cannot clear a last-played date,
    like or rating, so one that exists only on the destination stays.
  - Titles are dropped the moment the server's answer arrives. They are never
    printed or saved. The output holds counts and item IDs only.
  - The only network traffic goes to SERVER below.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter

# Fill in the two user IDs. Run with --users to find them.
SERVER = "http://serverip:8096"
KEY_FILE = "pathtoapikeyfile.txt"
SOURCE_USER_ID = ""  # copy FROM this user
DEST_USER_ID = ""  # copy TO this user

FAIL_LIMIT = 10  # stop after this many failed copies
BLANK = {"Played": False, "PlayCount": 0, "PlaybackPositionTicks": 0, "IsFavorite": False}
HINTS = {401: "the API key was refused", 404: "an ID was not found"}


class Stop(Exception):
    pass


step = "starting"
key = ""


def api(method, path, body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f'MediaBrowser Token="{key}"'
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        SERVER.rstrip("/") + path, data=data, method=method, headers=headers
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def clean_id(value, label):
    value = value.strip().replace("-", "").lower()
    if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise Stop(f"{label} at the top of the script is not a 32 letter/number user ID")
    return value


def watch_data(ud, is_folder):
    """The non-default watch numbers in a UserData block. {} means none."""
    data = {}
    if ud.get("IsFavorite"):
        data["IsFavorite"] = True
    for name in ("Likes", "Rating"):
        if ud.get(name) is not None:
            data[name] = ud[name]
    if not is_folder:  # a folder's played state is worked out from its children
        if ud.get("Played"):
            data["Played"] = True
        for name in ("PlayCount", "PlaybackPositionTicks", "LastPlayedDate"):
            if ud.get(name):
                data[name] = ud[name]
    return data


def read_items(user_id):
    """Return (items read, {item ID: watch data}, folder IDs). Titles are dropped here."""
    page = api(
        "GET",
        f"/Items?userId={user_id}&recursive=true&enableUserData=true&enableImages=false"
        "&enableTotalRecordCount=true",
    )
    items, total = page["Items"], page["TotalRecordCount"]
    if len(items) < total:
        raise Stop(f"the server counts {total} items but sent only {len(items)}")
    found, folders = {}, set()
    for item in items:
        # Only an explicit false counts as a normal item. If unsure, treat as a folder.
        is_folder = item.get("IsFolder") is not False
        data = watch_data(item.get("UserData") or {}, is_folder)
        if data:
            found[item["Id"]] = data
            if is_folder:
                folders.add(item["Id"])
    return len(items), found, folders


def norm(name, value):
    # Compare dates to the second; the server may round the fraction.
    return value[:19] if name == "LastPlayedDate" and value else value


def differences(want, got):
    """Names of the fields in want that got does not match. A field got lacks is empty."""
    return [k for k, v in want.items() if norm(k, got.get(k, BLANK.get(k))) != norm(k, v)]


def blanked(data, is_folder):
    """data plus explicit empty values, so a write replaces what the destination had."""
    return {**({"IsFavorite": False} if is_folder else BLANK), **data}


def main(args):
    global step, key

    step = "contacting the server"
    print(f"Connected to Jellyfin {api('GET', '/System/Info/Public')['Version']}")

    step = "reading the key file"
    try:
        with open(KEY_FILE) as f:
            key = f.read().strip()
    except OSError:
        raise Stop(f"cannot read the key file {KEY_FILE}") from None
    if not key:
        raise Stop(f"the key file {KEY_FILE} is empty")

    if args.users:
        step = "listing the users"
        for user in api("GET", "/Users"):
            print(f"{user['Name']}  {user['Id']}")
        return

    src = clean_id(SOURCE_USER_ID, "SOURCE_USER_ID")
    dst = clean_id(DEST_USER_ID, "DEST_USER_ID")
    if src == dst:
        raise Stop("SOURCE_USER_ID and DEST_USER_ID are the same user")

    step = "looking up the two users"
    src_name = api("GET", f"/Users/{src}")["Name"]
    dst_name = api("GET", f"/Users/{dst}")["Name"]
    print(f"From: {src_name}")
    print(f"To:   {dst_name}")

    step = "reading the source user's items"
    scanned, mine, folders = read_items(src)
    step = "reading the destination user's items"
    _, theirs, _ = read_items(dst)
    todo = {}
    for item_id, data in mine.items():
        if item_id not in theirs:
            todo[item_id] = data
        elif args.overwrite:
            todo[item_id] = blanked(data, item_id in folders)
    existing = sum(i in theirs for i in mine)

    print(f"Items read:                 {scanned}")
    print(f"Items with watch data:      {len(mine)}")
    print(f"  played:                   {sum('Played' in d for d in mine.values())}")
    print(f"  resumable:                {sum('PlaybackPositionTicks' in d for d in mine.values())}")
    print(f"  favorites:                {sum('IsFavorite' in d for d in mine.values())}")
    if args.overwrite:
        print(f"To overwrite (has data):    {existing}")
    else:
        print(f"Skipped (already has data): {existing}")
    print(f"To copy:                    {len(todo)}")

    if not args.apply:
        print("Dry run. Nothing was changed. Add --apply to the same command to copy.")
        return
    if not todo:
        print("Nothing to copy.")
        return

    step = "copying"
    failed = {}
    for n, (item_id, data) in enumerate(todo.items(), 1):
        try:
            api("POST", f"/UserItems/{item_id}/UserData?userId={dst}", data)
        except urllib.error.HTTPError as e:
            failed[item_id] = f"HTTP {e.code}"
        except OSError:
            failed[item_id] = "no answer"
        if len(failed) >= FAIL_LIMIT:
            raise Stop(f"{FAIL_LIMIT} copies failed, so I stopped. Failed IDs: {', '.join(failed)}")
        if n % 100 == 0:
            print(f"  {n} / {len(todo)}")
    copied = [i for i in todo if i not in failed]
    print(f"Copied: {len(copied)}   Failed: {len(failed)}")
    for item_id, why in failed.items():
        print(f"  failed: {item_id} ({why})")

    step = "checking the result"
    _, after, _ = read_items(dst)
    bad = {i: differences(todo[i], after.get(i, {})) for i in copied}
    bad = {i: fields for i, fields in bad.items() if fields}
    print(f"Check: {len(copied) - len(bad)} of {len(copied)} copied items match the source.")
    if bad:
        tally = Counter(f for fields in bad.values() for f in fields)
        print("  fields that differ: " + ", ".join(f"{k} {v}" for k, v in tally.items()))
        print("  first IDs: " + ", ".join(list(bad)[:10]))
    if failed or bad:
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Copy Jellyfin watch data between users.")
    parser.add_argument("--users", action="store_true", help="list user names and IDs, then stop")
    parser.add_argument("--apply", action="store_true", help="copy for real (default: dry run)")
    parser.add_argument("-O", "--overwrite", action="store_true",
                        help="replace watch data the destination already has (default: skip it)")
    args = parser.parse_args()
    try:
        main(args)
    except Stop as e:
        sys.exit(f"Stopped: {e}")
    except urllib.error.HTTPError as e:
        hint = HINTS.get(e.code, "unexpected")
        sys.exit(f"Stopped: the server answered HTTP {e.code} ({hint}) while {step}")
    except OSError:
        sys.exit(f"Stopped: could not reach the server while {step}")
    except Exception as e:  # name only: the error text could quote data
        sys.exit(f"Stopped: {type(e).__name__} while {step}")

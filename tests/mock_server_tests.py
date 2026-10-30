"""Test jellyfin_copy_watch.py against a fake Jellyfin server with fake data.

Run:  python3 tests/mock_server_tests.py

Never touches a real server or a real key file: every run uses a patched copy of
the script that points at the fake server and a fake key file. Every fake title
contains the word SECRET, and the last checks prove none reaches any output.
Set JF_SCRIPT to test another copy of the script, for example a deliberately
broken one, to see that the checks fail.
"""
import atexit
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse

SCRIPT = pathlib.Path(os.environ.get("JF_SCRIPT") or pathlib.Path(__file__).resolve().parent.parent / "jellyfin_copy_watch.py")
RUN = pathlib.Path(tempfile.mkdtemp(prefix="jf-mock-"))
atexit.register(shutil.rmtree, RUN, ignore_errors=True)

KEY = "FAKEKEY0123456789abcdef"
SRC, DST = "a" * 32, "b" * 32
NAMES = {SRC: "alpha", DST: "beta"}


def iid(i):
    return f"{i:032x}"


def build_world():
    items = []
    for i in range(1300):
        folder = i < 150
        items.append({
            "idx": i, "Id": iid(i), "Name": f"SECRET TITLE {i}",
            "Type": "Series" if i < 50 else "Season" if folder else "Episode",
            "IsFolder": folder,
        })
    src, dst = {}, {}
    for i in range(150, 550):  # 400 played
        src[iid(i)] = {"Played": True, "PlayCount": 1 + i % 3,
                       "LastPlayedDate": f"2025-03-{1 + i % 28:02d}T10:20:30.1234567Z"}
    for i in range(550, 555):  # 5 resumable
        src[iid(i)] = {"PlaybackPositionTicks": 600000000 + i,
                       "LastPlayedDate": "2025-04-01T08:00:00.0000000Z"}
    for i in range(560, 570):  # 10 favorites only
        src[iid(i)] = {"IsFavorite": True}
    for i in (400, 401):  # played and favorite
        src[iid(i)]["IsFavorite"] = True
    for i in (3, 63):  # favorite folders (63 also shows a computed Played)
        src[iid(i)] = {"IsFavorite": True}
    src[iid(570)] = {"Likes": True, "Rating": 8.5}
    for i in (150, 151, 560):  # destination already has data on these
        dst[iid(i)] = {"Played": True, "PlayCount": 9}
    dst[iid(1200)] = {"IsFavorite": True}
    return {"items": items, "ud": {SRC: src, DST: dst}, "posts": [], "flags": {}}


WORLD = build_world()


def item_json(user, it):
    st = WORLD["ud"][user].get(it["Id"], {})
    ud = {"PlaybackPositionTicks": st.get("PlaybackPositionTicks", 0),
          "PlayCount": st.get("PlayCount", 0), "IsFavorite": st.get("IsFavorite", False),
          "Played": st.get("Played", False), "Key": it["Id"], "ItemId": it["Id"]}
    for k in ("Likes", "Rating", "LastPlayedDate"):
        if k in st:
            ud[k] = st[k]
    if it["IsFolder"] and it["idx"] % 7 == 0:  # computed by the server, not stored
        ud["Played"], ud["UnplayedItemCount"] = True, 0
    return {"Name": it["Name"], "Id": it["Id"], "Type": it["Type"],
            "IsFolder": it["IsFolder"], "UserData": ud}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, payload=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        return self.headers.get("Authorization") == f'MediaBrowser Token="{KEY}"'

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        flags = WORLD["flags"]
        if u.path == "/System/Info/Public":
            return self.reply(200, {"ServerName": "mock", "Version": "12.2.0"})
        if not self.authed():
            return self.reply(401, raw=b"")
        if u.path == "/Users":
            return self.reply(200, [{"Name": n, "Id": i} for i, n in NAMES.items()])
        m = re.fullmatch(r"/Users/([0-9a-f]{32})", u.path)
        if m:
            if m.group(1) in NAMES:
                return self.reply(200, {"Name": NAMES[m.group(1)], "Id": m.group(1)})
            return self.reply(404, raw=b"")
        if u.path == "/Items":
            if flags.get("garbage"):
                return self.reply(200, raw=b'{"Items":[{"Name":"SECRET TITLE BROKEN" ')
            user = q["userId"][0]
            page = WORLD["items"][:flags.get("cap") or None]  # cap = server sends too few
            return self.reply(200, {"Items": [item_json(user, it) for it in page],
                                    "TotalRecordCount": len(WORLD["items"]), "StartIndex": 0})
        self.reply(404, raw=b"")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        flags = WORLD["flags"]
        if not self.authed():
            return self.reply(401, raw=b"")
        m = re.fullmatch(r"/UserItems/([0-9a-f]{32})/UserData", u.path)
        if not m:
            return self.reply(404, raw=b"")
        item, user = m.group(1), q["userId"][0]
        WORLD["posts"].append((user, item, body))
        if flags.get("fail_all") or item in flags.get("fail_items", ()):
            return self.reply(500, raw=b"boom SECRET TITLE in the error body")
        st = WORLD["ud"][user].setdefault(item, {})
        for k, v in body.items():
            if k == "PlayCount" and flags.get("drop_playcount"):
                continue
            st[k] = v
        self.reply(200, {"ItemId": item})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

OUTPUTS = []
RESULTS = []


def run(args, key=KEY, server=None, src=SRC, dst=DST):
    key_file = RUN / "fake_key.txt"
    key_file.write_text(key + "\n")
    text = SCRIPT.read_text()
    server = server or f"http://127.0.0.1:{PORT}"
    # !r writes a valid Python literal, and a function as the replacement stops re.sub reading Windows backslashes as escapes
    text = re.sub(r'^SERVER = ".*"', lambda m: f"SERVER = {server!r}", text, flags=re.M)
    text = re.sub(r'^KEY_FILE = ".*"', lambda m: f"KEY_FILE = {str(key_file)!r}", text, flags=re.M)
    text = re.sub(r'^SOURCE_USER_ID = ""', lambda m: f"SOURCE_USER_ID = {src!r}", text, flags=re.M)
    text = re.sub(r'^DEST_USER_ID = ""', lambda m: f"DEST_USER_ID = {dst!r}", text, flags=re.M)
    assert f"SERVER = {server!r}" in text and f"KEY_FILE = {str(key_file)!r}" in text, "patch missed SERVER or KEY_FILE"
    copy = RUN / "script_copy.py"
    copy.write_text(text)
    r = subprocess.run([sys.executable, "-I", str(copy), *args], capture_output=True, text=True,
                       timeout=120, cwd=RUN)
    out = r.stdout + r.stderr
    OUTPUTS.append(out)
    return r.returncode, out


def fresh(**flags):
    global WORLD
    WORLD = build_world()
    WORLD["flags"] = flags


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        print("      detail:", str(detail)[:600])


# ---- setup mistakes -------------------------------------------------------
fresh()
code, out = run([], src="", dst="")
check("empty IDs stop the script", code != 0 and "SOURCE_USER_ID" in out and not WORLD["posts"], out)
code, out = run([], src=SRC, dst=SRC)
check("same user twice is refused", code != 0 and "same user" in out, out)
code, out = run([], src="xyz", dst=DST)
check("bad ID format is refused", code != 0 and "32 letter" in out, out)
code, out = run([], dst="c" * 32)
check("unknown user gives HTTP 404 hint", code != 0 and "HTTP 404" in out and "looking up the two users" in out, out)

# ---- --users --------------------------------------------------------------
code, out = run(["--users"], src="", dst="")
check("--users lists names and IDs", code == 0 and f"alpha  {SRC}" in out and f"beta  {DST}" in out, out)

# ---- dry run --------------------------------------------------------------
fresh()
code, out = run([])
check("dry run exits 0", code == 0, out)
check("dry run changed nothing", not WORLD["posts"] and WORLD["ud"] == build_world()["ud"])
expect = {"Items read:": "1300", "Items with watch data:": "418", "played:": "400", "resumable:": "5",
          "favorites:": "14", "Skipped (already has data):": "3", "To copy:": "415"}
for label, val in expect.items():
    check(f"dry run: {label} {val}", re.search(re.escape(label) + r"\s+" + val + r"\b", out), out)
check("dry run shows the user names", "From: alpha" in out and "To:   beta" in out, out)
check("dry run says it is a dry run", "Dry run. Nothing was changed." in out, out)

# ---- apply ----------------------------------------------------------------
fresh()
orig = build_world()
code, out = run(["--apply"])
check("apply exits 0", code == 0, out)
check("apply posts 415 items", len(WORLD["posts"]) == 415, len(WORLD["posts"]))
check("apply check line is clean", "Check: 415 of 415 copied items match the source." in out and "Done." in out, out)
posts = {item: body for _, item, body in WORLD["posts"]}
check("every post goes to the destination user", all(u == DST for u, _, _ in WORLD["posts"]))
check("source user is untouched", WORLD["ud"][SRC] == orig["ud"][SRC])
check("existing destination data is not overwritten",
      all(WORLD["ud"][DST][iid(i)] == {"Played": True, "PlayCount": 9} for i in (150, 151, 560)))
check("unrelated destination data is left alone", WORLD["ud"][DST][iid(1200)] == {"IsFavorite": True})
check("favorite folders send IsFavorite only", posts[iid(3)] == {"IsFavorite": True} and posts[iid(63)] == {"IsFavorite": True})
check("computed folder Played is not stored", iid(0) not in WORLD["ud"][DST] and iid(7) not in WORLD["ud"][DST])
check("played item body", posts[iid(200)] == {"Played": True, "PlayCount": 1 + 200 % 3,
                                              "LastPlayedDate": f"2025-03-{1 + 200 % 28:02d}T10:20:30.1234567Z"}, posts[iid(200)])
check("resume item body", posts[iid(551)] == {"PlaybackPositionTicks": 600000551,
                                              "LastPlayedDate": "2025-04-01T08:00:00.0000000Z"}, posts[iid(551)])
check("likes and rating body", posts[iid(570)] == {"Likes": True, "Rating": 8.5}, posts[iid(570)])
check("played + favorite body", posts[iid(400)].get("IsFavorite") is True and posts[iid(400)].get("Played") is True)
check("items with no data are never posted", iid(1000) not in posts and iid(100) not in posts)
n = len(WORLD["posts"])
code, out = run(["--apply"])
check("second apply copies nothing", code == 0 and "Nothing to copy." in out and len(WORLD["posts"]) == n, out)

# ---- things that go wrong -------------------------------------------------
fresh()
code, out = run([], key="WRONGKEY0000")
check("wrong key: HTTP 401 with hint", code != 0 and "HTTP 401" in out and "API key was refused" in out and not WORLD["posts"], out)
code, out = run([], server="http://127.0.0.1:9")
check("server down is reported", code != 0 and "could not reach the server" in out, out)
fresh(cap=500)
code, out = run([])
check("short read is caught", code != 0 and "sent only 500" in out and not WORLD["posts"], out)
fresh(drop_playcount=True)
code, out = run(["--apply"])
check("mismatch after copy is reported", code != 0 and "PlayCount 398" in out and "Check:" in out, out)
fresh(fail_items={iid(200), iid(201), iid(202)})
code, out = run(["--apply"])
check("a few failed copies are listed by ID", code != 0 and "Failed: 3" in out and f"{iid(200)} (HTTP 500)" in out, out)
check("failed items are left out of the check", "Check: 412 of 412" in out, out)
fresh(fail_all=True)
code, out = run(["--apply"])
check("stops after 10 failures", code != 0 and "10 copies failed" in out and len(WORLD["posts"]) == 10, out)
fresh(garbage=True)
code, out = run([])
check("bad JSON prints the error name only", code != 0 and "JSONDecodeError while reading the source user's items" in out, out)

# ---- overwrite (-O) -------------------------------------------------------
def overwrite_world():
    fresh()
    d = WORLD["ud"][DST]
    d[iid(3)] = {"Rating": 5.0}  # folder the source favorited
    d[iid(200)] = {"Played": True, "PlayCount": 7, "PlaybackPositionTicks": 123}  # source: played 3 times
    d[iid(551)] = {"Played": True, "PlayCount": 2, "IsFavorite": True}  # source: resume spot only


overwrite_world()
code, out = run(["-O"])
check("-O dry run exits 0 and changes nothing", code == 0 and not WORLD["posts"], out)
check("-O dry run: To overwrite (has data): 6", re.search(r"To overwrite \(has data\):\s+6\b", out), out)
check("-O dry run: To copy: 418", re.search(r"To copy:\s+418\b", out), out)
check("-O dry run has no Skipped line", "Skipped" not in out, out)
code, out = run(["--overwrite"])
check("--overwrite is the long form of -O", code == 0 and re.search(r"To overwrite \(has data\):\s+6\b", out), out)

overwrite_world()
orig = build_world()
code, out = run(["-O", "--apply"])
check("-O apply exits 0", code == 0, out)
check("-O apply posts 418 items", len(WORLD["posts"]) == 418, len(WORLD["posts"]))
check("-O apply check line is clean", "Check: 418 of 418 copied items match the source." in out and "Done." in out, out)
posts = {item: body for _, item, body in WORLD["posts"]}
check("-O: overwritten leaf gets explicit empty values",
      posts[iid(560)] == {"Played": False, "PlayCount": 0, "PlaybackPositionTicks": 0, "IsFavorite": True}, posts[iid(560)])
check("-O: overwritten resume item clears played and favorite",
      posts[iid(551)] == {"Played": False, "PlayCount": 0, "PlaybackPositionTicks": 600000551, "IsFavorite": False,
                          "LastPlayedDate": "2025-04-01T08:00:00.0000000Z"}, posts[iid(551)])
check("-O: overwritten folder never gets played fields", posts[iid(3)] == {"IsFavorite": True}, posts[iid(3)])
check("-O: items new to the destination keep the short body",
      posts[iid(300)] == {"Played": True, "PlayCount": 1 + 300 % 3,
                          "LastPlayedDate": f"2025-03-{1 + 300 % 28:02d}T10:20:30.1234567Z"}, posts[iid(300)])
dst = WORLD["ud"][DST]
check("-O: stale destination values are replaced",
      dst[iid(200)]["PlayCount"] == 3 and dst[iid(200)]["PlaybackPositionTicks"] == 0
      and dst[iid(551)]["Played"] is False and dst[iid(551)]["IsFavorite"] is False
      and dst[iid(560)]["Played"] is False and dst[iid(560)]["PlayCount"] == 0 and dst[iid(560)]["IsFavorite"] is True
      and dst[iid(150)]["PlayCount"] == 1 and dst[iid(151)]["PlayCount"] == 2, dst[iid(200)])
check("-O: destination-only data is left alone", dst[iid(1200)] == {"IsFavorite": True})
check("-O: source user is untouched", WORLD["ud"][SRC] == orig["ud"][SRC])
n = len(WORLD["posts"])
code, out = run(["-O", "--apply"])
check("-O apply twice is harmless", code == 0 and len(WORLD["posts"]) == n + 418 and "Check: 418 of 418" in out, out)

# ---- nothing private in any output ---------------------------------------
joined = "\n".join(OUTPUTS)
check("no fake title appears in any output", "SECRET" not in joined)
check("no key appears in any output", KEY not in joined and "WRONGKEY" not in joined)

print()
print(f"{sum(RESULTS)} of {len(RESULTS)} checks passed")
sys.exit(0 if all(RESULTS) else 1)

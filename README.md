# jellyfin-copy-watch-history

A small Python script that copies watch history from one Jellyfin user to another through Jellyfin's API.

It copies played status, play counts, resume positions, last-played dates, favorites, likes and ratings, matching items by ID. Anything the destination user already has is skipped by default, so nothing is overwritten and running it twice is harmless.

## Compatibility Matrix

Where the script has been tested so far:

| Platform | Status | Python tested | Notes |
|---|---|---|---|
| Linux | Tested | 3.14 | Passes on test and real production server. |
| macOS | Tested | 3.11 | Passes on test and real production server. |
| Windows | Not yet tested | - | Use `python` or `py` instead of `python3`. In `KEY_FILE`, plain backslashes are a syntax error: use forward slashes or a raw string (`r"C:\..."`). |

The script uses only the Python standard library and f-strings, so it needs Python 3.6 or newer. Only the versions listed above have been tested.

## How To Use

You need Python 3 (nothing to install) and a Jellyfin server you can reach from this computer. Tested with Jellyfin 12.2.0.

1. **Create an API key.** In the Jellyfin Dashboard (you need an admin account) open **API Keys**, click **+**, name it and copy the key.
2. **Save the key** in a plain text file, just the key and nothing else, somewhere outside any git folder, for example `/path/to/apikey.txt`. Treat it like a password.
3. **Copy `jellyfin_copy_watch.py`** to a folder of your own. If you cloned this repo, copy it out of the clone, because you are about to put private details in it.
4. **Open your copy in a text editor** and set these two lines near the top:

   ```python
   SERVER = "http://serverip:8096"   # your Jellyfin address
   KEY_FILE = "/path/to/apikey.txt"  # full path to the key file (a ~ will not work)
   ```

5. **List the users** to find their IDs:

   ```bash
   python3 jellyfin_copy_watch.py --users
   ```

   Each line shows a user name followed by that user's ID.

6. **Set the two users** by pasting their IDs between the quotes:

   ```python
   SOURCE_USER_ID = "..."  # copy FROM this user
   DEST_USER_ID = "..."    # copy TO this user
   ```

7. **Do a dry run.** It changes nothing and prints what it would copy:

   ```bash
   python3 jellyfin_copy_watch.py
   ```

   Check that the **From** and **To** names are the right way round.

8. **Copy for real** by adding `--apply`:

   ```bash
   python3 jellyfin_copy_watch.py --apply
   ```

   When it finishes it re-reads the destination and reports how many copied items match the source.

## Options

| Option | What it does |
|---|---|
| *(none)* | Dry run: changes nothing and prints how many items would be copied. |
| `--apply` | Copies for real, then checks the result. Stops after 10 failed copies (`FAIL_LIMIT` in the script). |
| `-O`, `--overwrite` | Replaces watch data the destination already has. Without it, those items are skipped. |
| `--users` | Lists user names and IDs, then stops. Needs `SERVER` and `KEY_FILE` set. |
| `-h`, `--help` | Shows the built-in help. |

Options can be combined:

```bash
python3 jellyfin_copy_watch.py                # dry run
python3 jellyfin_copy_watch.py --apply        # copy items the destination has no data for
python3 jellyfin_copy_watch.py -O             # dry run that includes replacing existing data
python3 jellyfin_copy_watch.py -O --apply     # copy everything, replacing existing data
```

## Good to know:

- The source user is only read, never changed.
- With `-O`, items the source has no watch data for are never touched, and a last-played date, like or rating that only the destination has stays, because the server cannot clear those.
- There is no undo, so always do a dry run first.
- Media titles are never printed or saved. The output is counts, user names and item IDs.
- Favorites on people, artists and genres are not copied.

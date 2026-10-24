#!/usr/bin/env python3
"""Instagram publisher for the Pofuland account, run by GitHub Actions. Python 3 stdlib only.

Media lives in media/ of this repo and is served by GitHub Pages at SITE_BASE.
The token comes from the IG_TOKEN environment variable (a repo secret).

  whoami            check the token against the Graph API
  list              show schedule.json with publish status
  publish --id ID   dry run for one item; add --yes to publish it for real
  due [--yes]       publish every item whose time has passed and is not logged yet
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_PATH = os.path.join(HERE, "schedule.json")
LOG_PATH = os.path.join(HERE, "published_log.json")
FAIL_PATH = os.path.join(HERE, "failed_log.json")
LOCK_PATH = os.path.join(HERE, ".lock")
SITE_BASE = "https://pofuland.github.io/ig-queue"
GRAPH = "https://graph.instagram.com/v21.0"
KINDS = ("REELS", "IMAGE", "CAROUSEL")
CAPTION_MAX = 2200
MAX_ATTEMPTS = 3
MEDIA_WAIT = 6 * 60
CONTAINER_WAIT = 10 * 60


class ApiError(Exception):
    pass


# --- local state -----------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_schedule():
    return load_json(SCHEDULE_PATH, [])


def find_item(items, item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    raise SystemExit(f"no schedule item with id {item_id!r}")


def parse_at(text):
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError("time needs a UTC offset")
    return when


@contextlib.contextmanager
def lock():
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit("another ig.py run holds the lock; exiting")
    try:
        yield
    finally:
        fh.close()


# --- HTTP ------------------------------------------------------------------

def read_token():
    token = os.environ.get("IG_TOKEN", "").strip()
    if not token:
        raise SystemExit("IG_TOKEN is not set")
    return token


def http(method, url, fields):
    data = None
    if method == "GET":
        url = url + "?" + urllib.parse.urlencode(fields)
    else:
        data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"HTTP {e.code} from Graph API: {detail}") from None
    return json.loads(body) if body else {}


def graph(method, path, **params):
    params["access_token"] = read_token()
    return http(method, GRAPH + path, params)


def wait_url(url):
    deadline = time.time() + MEDIA_WAIT
    while True:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        if time.time() > deadline:
            raise ApiError(f"media URL not live after 6 min: {url}")
        time.sleep(10)


def wait_container(cid):
    deadline = time.time() + CONTAINER_WAIT
    while True:
        status = graph("GET", f"/{cid}", fields="status_code,status")
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"container {cid} {code}: {status.get('status')}")
        if time.time() > deadline:
            raise ApiError(f"container {cid} not finished after 10 min")
        time.sleep(15)


# --- media (already in this repo, served by Pages) -------------------------

def media_url(rel):
    return f"{SITE_BASE}/{urllib.parse.quote(rel)}"


def item_media(item):
    raw = item.get("file")
    return (raw if isinstance(raw, list) else [raw]) + ([item["cover"]] if item.get("cover") else [])


def prune_media(published_id):
    """Delete media used only by already published items; the workflow commits the removal."""
    done = {r["id"] for r in load_json(LOG_PATH, [])} | {published_id}
    keep = {rel for item in load_schedule() if item["id"] not in done for rel in item_media(item)}
    for item in load_schedule():
        if item["id"] in done:
            for rel in item_media(item):
                path = os.path.join(HERE, rel)
                if rel not in keep and os.path.isfile(path):
                    os.remove(path)


# --- publishing ------------------------------------------------------------

def check_item(item):
    """Return (problems, files, cover) with ~ expanded."""
    problems = []
    kind = item.get("kind")
    if kind not in KINDS:
        problems.append(f"bad kind {kind!r}")
    raw = item.get("file")
    files = [os.path.join(HERE, f) for f in (raw if isinstance(raw, list) else [raw])]
    if kind == "CAROUSEL":
        if not 2 <= len(files) <= 10:
            problems.append("carousel needs 2-10 files")
    elif len(files) != 1:
        problems.append(f"{kind} needs exactly one file")
    if kind == "REELS" and not files[0].lower().endswith(".mp4"):
        problems.append("REELS file must be .mp4")
    for f in files:
        if not os.path.isfile(f):
            problems.append(f"missing file {f}")
    cover = item.get("cover")
    if cover:
        cover = os.path.join(HERE, cover)
        if kind != "REELS":
            problems.append("cover only applies to REELS")
        if not os.path.isfile(cover):
            problems.append(f"missing cover {cover}")
    caption = item.get("caption", "")
    if not caption:
        problems.append("empty caption")
    if len(caption) > CAPTION_MAX:
        problems.append(f"caption is {len(caption)} chars (max {CAPTION_MAX})")
    try:
        parse_at(item["at"])
    except (KeyError, ValueError) as e:
        problems.append(f"bad 'at' ({e})")
    return problems, files, cover


def publish_item(item, files, cover):
    kind = item["kind"]
    urls = [media_url(os.path.relpath(p, HERE)) for p in files + ([cover] if cover else [])]
    for url in urls:
        wait_url(url)
    caption = item["caption"]
    if kind == "REELS":
        params = dict(media_type="REELS", video_url=urls[0], caption=caption, share_to_feed="true")
        if cover:
            params["cover_url"] = urls[1]
        cid = graph("POST", "/me/media", **params)["id"]
        wait_container(cid)
    elif kind == "IMAGE":
        cid = graph("POST", "/me/media", image_url=urls[0], caption=caption)["id"]
        wait_container(cid)
    else:
        children = []
        for url in urls:
            child = graph("POST", "/me/media", image_url=url, is_carousel_item="true")["id"]
            wait_container(child)
            children.append(child)
        cid = graph("POST", "/me/media", media_type="CAROUSEL",
                    children=",".join(children), caption=caption)["id"]
        wait_container(cid)
    media_id = graph("POST", "/me/media_publish", creation_id=cid)["id"]
    permalink = graph("GET", f"/{media_id}", fields="permalink")["permalink"]
    return media_id, permalink


def record(item_id, media_id, permalink):
    log = load_json(LOG_PATH, [])
    log.append({
        "id": item_id,
        "media_id": media_id,
        "permalink": permalink,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    save_json(LOG_PATH, log)


# --- commands --------------------------------------------------------------

def cmd_whoami(args):
    print(json.dumps(graph("GET", "/me", fields="user_id,username,account_type,name"), indent=2))


def cmd_list(args):
    published = {r["id"]: r for r in load_json(LOG_PATH, [])}
    fails = load_json(FAIL_PATH, {})
    now = dt.datetime.now(dt.timezone.utc)
    for item in load_schedule():
        item_id = item["id"]
        n = fails.get(item_id, 0)
        if item_id in published:
            status = "PUBLISHED " + published[item_id]["permalink"]
        elif n >= MAX_ATTEMPTS:
            status = f"GAVE UP after {n} failures"
        elif n:
            status = f"FAILED x{n}, will retry"
        elif parse_at(item["at"]) <= now:
            status = "DUE"
        else:
            status = "scheduled"
        print(f"{item['at']}  {item['kind']:<8}  {item_id:<22} {status}")


def cmd_publish(args):
    item = find_item(load_schedule(), args.id)
    problems, files, cover = check_item(item)
    print(f"id:      {item['id']}")
    print(f"kind:    {item['kind']}")
    print(f"at:      {item['at']}")
    print("files:   " + "\n         ".join(files))
    print(f"cover:   {cover or '-'}")
    print(f"caption: ({len(item.get('caption', ''))} chars)")
    print(item.get("caption", ""))
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems), file=sys.stderr)
        sys.exit(1)
    if not args.yes:
        print("\nDRY RUN: nothing published. Re-run with --yes to publish for real.")
        return
    with lock():
        if any(r["id"] == item["id"] for r in load_json(LOG_PATH, [])):
            raise SystemExit(f"{item['id']} is already in published_log.json")
        media_id, permalink = publish_item(item, files, cover)
        record(item["id"], media_id, permalink)
        prune_media(item["id"])
    print(f"PUBLISHED {item['id']} media_id={media_id} {permalink}")


def cmd_due(args):
    with lock():
        now = dt.datetime.now(dt.timezone.utc)
        done = {r["id"] for r in load_json(LOG_PATH, [])}
        fails = load_json(FAIL_PATH, {})
        due = [i for i in load_schedule()
               if parse_at(i["at"]) <= now and i["id"] not in done
               and fails.get(i["id"], 0) < MAX_ATTEMPTS]
        if not due and args.lead_minutes:
            # GitHub cron can start hours late, so runs start early and wait for the exact time.
            horizon = now + dt.timedelta(minutes=args.lead_minutes)
            soon = sorted((parse_at(i["at"]), i["id"]) for i in load_schedule()
                          if now < parse_at(i["at"]) <= horizon and i["id"] not in done
                          and fails.get(i["id"], 0) < MAX_ATTEMPTS)
            if soon:
                wait = (soon[0][0] - now).total_seconds()
                print(f"waiting {wait / 60:.0f} min for {soon[0][1]}")
                time.sleep(wait + 5)
                now = dt.datetime.now(dt.timezone.utc)
                due = [i for i in load_schedule()
                       if parse_at(i["at"]) <= now and i["id"] not in done
                       and fails.get(i["id"], 0) < MAX_ATTEMPTS]
        if not due:
            print("nothing due")
            return
        for item in due:
            item_id = item["id"]
            problems, files, cover = check_item(item)
            if not args.yes:
                print(f"would publish {item_id}" + (f" (problems: {problems})" if problems else ""))
                continue
            try:
                if problems:
                    raise ApiError("; ".join(problems))
                media_id, permalink = publish_item(item, files, cover)
            except Exception as e:
                fails[item_id] = fails.get(item_id, 0) + 1
                save_json(FAIL_PATH, fails)
                print(f"FAILED {item_id} (attempt {fails[item_id]}/{MAX_ATTEMPTS}): {e}", file=sys.stderr)
                break  # keep schedule order; the next run retries this item
            record(item_id, media_id, permalink)
            prune_media(item_id)
            print(f"PUBLISHED {item_id} media_id={media_id} {permalink}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("list").set_defaults(func=cmd_list)
    p = sub.add_parser("publish")
    p.add_argument("--id", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_publish)
    p = sub.add_parser("due")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--lead-minutes", type=int, default=0, help="if the next post is this close, wait for it")
    p.set_defaults(func=cmd_due)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

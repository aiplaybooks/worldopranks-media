"""GitHub Actions: schedule.json'daki vadesi gelen ogeleri Instagram Reel olarak yayinlar (+ ilk yorum, + zamanli FB reel yorumu).

    python .github/scripts/ig_publish.py [--dry]

- schedule.json (girdi; yalniz yerel taraf yazar) ve state.json (durum; yalniz bu betik yazar) -> push cakismasi olmaz.
- Cift yayin onleme: workflow `concurrency` ile tek calisir; 'publishing' durumu + konteyner id'si git'e checkpoint edilir;
  yeniden calismada once son medyalarda ayni caption aranir (media_publish hata verip yayinlayabilir).
- Gizli degerler yalnizca ortam degiskenlerinden okunur, asla yazdirilmaz. Hatada kisa mesaj; 4 basarisiz denemede Telegram.
"""
import json, os, pathlib, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

GRAPH = "https://graph.facebook.com/v25.0"
ROOT = pathlib.Path.cwd()
SCHED, STATE = ROOT / "schedule.json", ROOT / "state.json"
DRY = "--dry" in sys.argv
E = os.environ.get
MAX_TRIES = 4


def say(*a): print(*a, flush=True)


def now(): return datetime.now(timezone.utc)


def parse(s):
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def api(method, path, params=None):
    params = dict(params or {}); params["access_token"] = E("FB_PAGE_ACCESS_TOKEN", "")
    url = f"{GRAPH}/{path}"; data = None
    if method == "GET": url += "?" + urllib.parse.urlencode(params)
    else: data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        try: err = json.loads(ex.read() or b"{}").get("error", {})
        except ValueError: err = {}
        raise RuntimeError(f"{method} /{path}: {err.get('message', ex.code)} (kod {err.get('code')}, altkod {err.get('error_subcode')})") from None
    except (urllib.error.URLError, TimeoutError) as ex:
        raise RuntimeError(f"{method} /{path}: ag hatasi {type(ex).__name__}") from None


def notify(text):
    t, c = E("TELEGRAM_BOT_TOKEN"), E("TELEGRAM_CHAT_ID")
    if DRY or not t or not c: return
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{t}/sendMessage",
                               data=urllib.parse.urlencode({"chat_id": c, "text": text}).encode()), timeout=30).read()
    except Exception: pass  # noqa: BLE001


def load(p, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError): return default


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def checkpoint(state, msg):
    """state.json'u yazar; Actions icindeyse commit+push (rebase ile yaris durumuna dayanikli)."""
    if DRY: return
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    if not E("GITHUB_ACTIONS"): return
    git("config", "user.name", "ig-publish-bot"); git("config", "user.email", "ig-publish-bot@users.noreply.github.com")
    git("add", "state.json")
    if not git("status", "--porcelain", "state.json").stdout.strip(): return
    git("commit", "-q", "-m", f"state: {msg}")
    for n in range(4):
        git("pull", "--rebase", "-q", "origin", "gh-pages")
        if git("push", "-q", "origin", "HEAD:gh-pages").returncode == 0: return
        time.sleep(3 * (n + 1))
    say("UYARI: state.json push edilemedi")


def recent_match(ig, caption, since):
    d = api("GET", f"{ig}/media", {"fields": "id,caption,timestamp", "limit": 10})
    return next((m["id"] for m in d.get("data", []) if (m.get("caption") or "")[:80] == caption[:80] and m.get("timestamp", "")[:19] >= since[:19]), None)


def wait_container(cid, limit=600):
    t0 = time.time()
    while True:
        s = api("GET", cid, {"fields": "status_code,status"})
        if s.get("status_code") in ("FINISHED", "PUBLISHED"): return
        if s.get("status_code") in ("ERROR", "EXPIRED"): raise RuntimeError(f"konteyner {s['status_code']}: {s.get('status')}")
        if time.time() - t0 > limit: raise RuntimeError(f"konteyner {limit} sn sonra hala {s.get('status_code')}")
        time.sleep(10)


def publish_ig(it, st, state):
    ig = E("IG_BUSINESS_ACCOUNT_ID")
    st.update(status="publishing", started=st.get("started") or now().isoformat(timespec="seconds")); checkpoint(state, f"{it['id']} publishing")
    if not st.get("media_id"):
        if not st.get("container"):
            st["container"] = api("POST", f"{ig}/media", {"media_type": "REELS", "video_url": it["video_url"], "caption": it["caption"], "share_to_feed": "true"})["id"]
            checkpoint(state, f"{it['id']} container")
        mid = recent_match(ig, it["caption"], st["started"])
        if not mid:
            wait_container(st["container"])
            try: mid = api("POST", f"{ig}/media_publish", {"creation_id": st["container"]})["id"]
            except RuntimeError:  # bazen hata verir ama yayinlar
                time.sleep(20); mid = recent_match(ig, it["caption"], st["started"])
                if not mid: raise
        st["media_id"] = mid
    st["link"] = api("GET", st["media_id"], {"fields": "permalink"}).get("permalink", "")
    st.update(status="published", published_at=now().isoformat(timespec="seconds"), error="")
    checkpoint(state, f"{it['id']} published"); say(f"{it['id']}: IG yayinlandi {st['link']}")


def comment(node, text, st, key):
    if not text or st.get(key): return
    try: st[key] = api("POST", f"{node}/comments", {"message": text}).get("id", "ok")
    except RuntimeError as ex: st[key + "_error"] = str(ex)[:160]; say(f"{key}: {ex}")


def handle(it, state):
    st = state["items"].setdefault(it["id"], {"status": "queued"}); t = now()
    due = parse(it["publish_at"])
    if st["status"] not in ("published", "failed"):
        wait = st.get("next_try") and parse(st["next_try"]) > t
        if due > t or wait:
            say(f"{it['id']}: bekliyor ({'deneme beklemesi' if wait else 'yayin ' + due.isoformat(timespec='minutes')})")
        elif DRY: say(f"{it['id']}: [DRY] vadesi gelmis; gercek modda IG Reel olarak yayinlanirdi ({it['video_url'].rsplit('/', 1)[-1]})")
        else:
            try:
                publish_ig(it, st, state)
                comment(st["media_id"], it.get("comment"), st, "comment_id"); checkpoint(state, f"{it['id']} comment")
            except RuntimeError as ex:
                st["attempts"] = st.get("attempts", 0) + 1; st["error"] = str(ex)[:300]; say(f"{it['id']}: HATA {st['error']}")
                if "EXPIRED" in st["error"] or "ERROR" in st["error"]: st.pop("container", None)
                if st["attempts"] >= MAX_TRIES:
                    st["status"] = "failed"; notify(f"IG yayin BASARISIZ ({it['id']}): {st['error'][:200]}. Elle: publisher.py --steps ig_reel")
                else:
                    st["status"] = "queued"; st["next_try"] = (t + timedelta(minutes=15 * st["attempts"])).isoformat(timespec="seconds")
                checkpoint(state, f"{it['id']} error")
    elif st["status"] == "published" and st.get("media_id") and not st.get("comment_id") and not st.get("comment_id_error") and not DRY:
        comment(st["media_id"], it.get("comment"), st, "comment_id"); checkpoint(state, f"{it['id']} comment")
    # zamanli Facebook reel'e ilk yorum (yayinlandiktan sonra)
    vid, ftxt = it.get("fb_video_id"), it.get("fb_comment")
    fb_at = parse(it.get("fb_publish_at") or it["publish_at"])
    if vid and ftxt and not st.get("fb_comment_id") and not st.get("fb_comment_gave_up") and fb_at + timedelta(minutes=2) <= t:
        if DRY: return say(f"{it['id']}: [DRY] FB reel ilk yorum denenirdi")
        try:
            ph = api("GET", vid, {"fields": "status"}).get("status", {}).get("publishing_phase", {}).get("status")
            if ph == "complete": comment(vid, ftxt, st, "fb_comment_id")
            else: say(f"{it['id']}: FB reel henuz yayinda degil ({ph})")
        except RuntimeError as ex: say(f"{it['id']}: FB durum hatasi {ex}")
        if not st.get("fb_comment_id") and t - fb_at > timedelta(hours=6): st["fb_comment_gave_up"] = True
        checkpoint(state, f"{it['id']} fb comment")


def main():
    sched = load(SCHED, {"items": []}); state = load(STATE, {"items": {}}); state.setdefault("items", {})
    say(f"ig_publish {'[DRY] ' if DRY else ''}{now().isoformat(timespec='seconds')}: {len(sched['items'])} oge")
    if not E("FB_PAGE_ACCESS_TOKEN") or not E("IG_BUSINESS_ACCOUNT_ID"):
        if DRY: say("[DRY] secret'lar tanimli degil (FB_PAGE_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID)")
        else: sys.exit("HATA: FB_PAGE_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID secret'i yok")
    elif DRY:
        try: say(f"[DRY] token dogrulandi: Instagram @{api('GET', E('IG_BUSINESS_ACCOUNT_ID'), {'fields': 'username'}).get('username')}")
        except RuntimeError as ex: say(f"[DRY] token/hesap hatasi: {ex}")
    for it in sched["items"]:
        try: handle(it, state)
        except Exception as ex: say(f"{it.get('id')}: beklenmeyen hata {type(ex).__name__}: {str(ex)[:200]}")  # noqa: BLE001
    checkpoint(state, "run")


if __name__ == "__main__":
    main()

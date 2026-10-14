#!/usr/bin/env python3
"""
Job alert bot: polls company career boards + remote job boards, filters for
talent / recruiting roles that are remote, worldwide and >= your salary floor,
and pushes a notification to your phone the moment a NEW one appears.

Usage:
  python notifier.py              one pass (what GitHub Actions runs)
  python notifier.py --loop 300   keep running locally, poll every 300s
  python notifier.py --test       send a test notification and exit
  python notifier.py --dry-run    print matches, send nothing, save nothing
"""
import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional

import requests

import config as C

STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
HEADERS = {"User-Agent": "personal-job-alerts/1.0"}
TIMEOUT = 25
MAX_INDIVIDUAL_ALERTS = 20      # per run; anything beyond is rolled into one summary
SEEN_TTL_DAYS = 120


@dataclass
class Job:
    source: str
    jid: str
    title: str
    company: str
    location: str
    url: str
    remote: Optional[bool] = None
    smin: Optional[float] = None
    smax: Optional[float] = None
    cur: Optional[str] = None
    posted: Optional[float] = None  # epoch seconds


# --------------------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def epoch(v) -> Optional[float]:
    """Accept epoch s / epoch ms / ISO string / RFC822 string."""
    if v in (None, "", 0):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            v = float(v)
            return v / 1000 if v > 1e11 else v
        s = str(v).strip()
        try:
            return parsedate_to_datetime(s).timestamp()
        except Exception:
            s = s.replace("Z", "+00:00")
            d = datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
    except Exception:
        return None


def num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "", 0, "0") else None
    except Exception:
        return None


CUR_SYM = {"£": "GBP", "€": "EUR", "$": "USD"}


def parse_salary_text(s):
    """'$80,000 - $100,000', '£70K – £90K', '80-100k EUR' -> (min, max, currency)"""
    if not s:
        return None, None, None
    cur = next((code for sym, code in CUR_SYM.items() if sym in s), None)
    m = re.search(r"\b(USD|GBP|EUR|CAD|AUD|CHF|SGD|AED|INR)\b", s, re.I)
    if m:
        cur = m.group(1).upper()
    nums = []
    for n, k in re.findall(r"(\d[\d,]*\.?\d*)\s*([kK])?", s):
        try:
            v = float(n.replace(",", ""))
        except ValueError:
            continue
        if k:
            v *= 1000
        if v >= 1000:
            nums.append(v)
    if not nums:
        return None, None, cur
    return min(nums), max(nums), cur or "USD"


# --------------------------------------------------------------------------- filtering
INCLUDE = [re.compile(p, re.I) for p in C.TITLE_INCLUDE]
EXCLUDE = [re.compile(p, re.I) for p in C.TITLE_EXCLUDE]

WORLDWIDE = re.compile(
    r"\b(worldwide|world[- ]wide|global(ly)?|international|any ?where(?!\s+in\s+(the\s+)?"
    r"(us|u\.s|usa|united|uk|europe|eu|canada|emea|apac|latam|north|south|asia|africa))"
    r"|any location|all countries|work from anywhere)\b",
    re.I,
)


def title_ok(title: str) -> bool:
    return any(p.search(title) for p in INCLUDE) and not any(p.search(title) for p in EXCLUDE)


def classify_location(text: Optional[str], remote_flag: Optional[bool]) -> str:
    """-> worldwide | region_ok | unspecified | restricted | onsite"""
    t = (text or "").strip().lower()
    if not t:
        return "unspecified" if remote_flag else "onsite"
    if WORLDWIDE.search(t):
        return "worldwide"
    for r in C.ACCEPT_REGIONS:
        if re.search(r"\b" + re.escape(r.lower()) + r"\b", t):
            return "region_ok"
    if "hybrid" in t:
        return "onsite"
    if re.search(r"\bremote\b", t):
        leftover = re.sub(r"\b(remote|fully|work|from|home|first|friendly|flexible|100%)\b", "", t)
        leftover = re.sub(r"[^a-z]", "", leftover)
        return "unspecified" if not leftover else "restricted"
    return "restricted" if remote_flag else "onsite"


def to_gbp(v, cur):
    rate = C.FX_TO_GBP.get((cur or "USD").upper())
    return v * rate if (v and rate) else None


def salary_status(job: Job) -> str:
    top = job.smax or job.smin
    if not top or top < 20_000:      # missing, or looks hourly/monthly -> can't judge
        return "unknown"
    g = to_gbp(top, job.cur)
    if g is None:
        return "unknown"
    return "ok" if g >= C.MIN_SALARY_GBP else "low"


def evaluate(job: Job):
    if not title_ok(job.title):
        return None
    loc = classify_location(job.location, job.remote)
    if loc not in ("worldwide", "region_ok") and not (loc == "unspecified" and C.ALLOW_UNSPECIFIED_LOCATION):
        return None
    sal = salary_status(job)
    if sal == "low" or (sal == "unknown" and not C.ALLOW_UNKNOWN_SALARY):
        return None
    return loc, sal


# --------------------------------------------------------------------------- sources
def src_greenhouse(token) -> List[Job]:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for j in d.get("jobs", []):
        title = j.get("title", "")
        if not title_ok(title):
            continue
        loc = (j.get("location") or {}).get("name", "")
        out.append(Job("Greenhouse", f"gh:{token}:{j['id']}", title, j.get("company_name") or token.title(),
                       loc, j.get("absolute_url", ""), remote=bool(re.search("remote", loc, re.I)),
                       posted=epoch(j.get("first_published") or j.get("updated_at"))))
    return out


def src_lever(company) -> List[Job]:
    d = get_json(f"https://api.lever.co/v0/postings/{company}", {"mode": "json"})
    out = []
    for j in d:
        title = j.get("text", "")
        if not title_ok(title):
            continue
        loc = (j.get("categories") or {}).get("location", "") or ""
        wt = (j.get("workplaceType") or "").lower()
        sr = j.get("salaryRange") or {}
        smin = smax = cur = None
        if "year" in str(sr.get("interval", "")).lower():
            smin, smax, cur = num(sr.get("min")), num(sr.get("max")), sr.get("currency")
        out.append(Job("Lever", f"lv:{company}:{j['id']}", title, company.title(), loc, j.get("hostedUrl", ""),
                       remote=(wt == "remote") or bool(re.search("remote", loc, re.I)),
                       smin=smin, smax=smax, cur=cur, posted=epoch(j.get("createdAt"))))
    return out


def src_ashby(name) -> List[Job]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{name}", {"includeCompensation": "true"})
    out = []
    for j in d.get("jobs", []):
        title = j.get("title", "")
        if not title_ok(title) or j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        smin, smax, cur = parse_salary_text(comp)
        out.append(Job("Ashby", f"ab:{name}:{j['id']}", title, name.title(), j.get("location", "") or "",
                       j.get("jobUrl", ""), remote=bool(j.get("isRemote")) or bool(re.search("remote", j.get("location") or "", re.I)),
                       smin=smin, smax=smax, cur=cur, posted=epoch(j.get("publishedAt"))))
    return out


def src_remoteok() -> List[Job]:
    d = get_json("https://remoteok.com/api")
    out = []
    for j in d:
        if not isinstance(j, dict) or "position" not in j:
            continue
        if not title_ok(j["position"]):
            continue
        out.append(Job("RemoteOK", f"rok:{j.get('id')}", j["position"], j.get("company", ""), j.get("location", "") or "",
                       j.get("url", ""), remote=True, smin=num(j.get("salary_min")), smax=num(j.get("salary_max")),
                       cur="USD", posted=epoch(j.get("epoch") or j.get("date"))))
    return out


def _wwr(feed_url) -> List[Job]:
    r = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        raw = (it.findtext("title") or "").strip()
        company, _, title = raw.partition(":")
        if not title:
            company, title = "", raw
        title = title.strip()
        if not title_ok(title):
            continue
        link = (it.findtext("link") or it.findtext("guid") or "").strip()
        region = (it.findtext("region") or "").strip()
        out.append(Job("WeWorkRemotely", f"wwr:{link}", title, company.strip(), region, link, remote=True,
                       posted=epoch(it.findtext("pubDate"))))
    return out


def src_wwr() -> List[Job]:
    jobs, seen = [], set()
    for u in ("https://weworkremotely.com/remote-jobs.rss",
              "https://weworkremotely.com/categories/remote-human-resources-jobs.rss"):
        try:
            for j in _wwr(u):
                if j.jid not in seen:
                    seen.add(j.jid)
                    jobs.append(j)
        except Exception as e:
            log(f"  [WeWorkRemotely] {u} failed: {e}")
    return jobs


def src_himalayas() -> List[Job]:
    out, seen = [], set()
    for q in ("recruiter", "talent acquisition", "talent partner"):
        d = get_json("https://himalayas.app/jobs/api/search", {"q": q, "sort": "recent"})
        for j in d.get("jobs", []):
            title = j.get("title", "")
            link = j.get("applicationLink") or j.get("guid") or ""
            if not title_ok(title) or link in seen:
                continue
            seen.add(link)
            restr = j.get("locationRestrictions") or []
            out.append(Job("Himalayas", f"him:{link}", title, j.get("companyName", ""),
                           ", ".join(restr) if restr else "", link, remote=True,
                           smin=num(j.get("minSalary")), smax=num(j.get("maxSalary")), cur=j.get("currency") or "USD",
                           posted=epoch(j.get("pubDate"))))
    return out


def src_jobicy() -> List[Job]:
    out, seen = [], set()
    for tag in ("recruiter", "talent"):
        d = get_json("https://jobicy.com/api/v2/remote-jobs", {"count": 50, "tag": tag})
        for j in d.get("jobs", []):
            title = j.get("jobTitle", "")
            if not title_ok(title) or j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            geo = j.get("jobGeo", "")
            geo = ", ".join(geo) if isinstance(geo, list) else geo
            out.append(Job("Jobicy", f"jby:{j.get('id')}", title, j.get("companyName", ""), geo, j.get("url", ""),
                           remote=True, smin=num(j.get("annualSalaryMin")), smax=num(j.get("annualSalaryMax")),
                           cur=j.get("salaryCurrency") or "USD", posted=epoch(j.get("pubDate"))))
    return out


def src_remotive() -> List[Job]:
    out, seen = [], set()
    for q in ("recruiter", "talent acquisition", "talent partner"):
        d = get_json("https://remotive.com/api/remote-jobs", {"search": q})
        for j in d.get("jobs", []):
            title = j.get("title", "")
            if not title_ok(title) or j["id"] in seen:
                continue
            seen.add(j["id"])
            smin, smax, cur = parse_salary_text(j.get("salary", ""))
            out.append(Job("Remotive", f"rmt:{j['id']}", title, j.get("company_name", ""),
                           j.get("candidate_required_location", ""), j.get("url", ""), remote=True,
                           smin=smin, smax=smax, cur=cur, posted=epoch(j.get("publication_date"))))
    return out


def build_tasks():
    """(name, callable, min minutes between fetches)"""
    t = []
    for x in C.GREENHOUSE:
        t.append((f"greenhouse:{x}", lambda x=x: src_greenhouse(x), 0))
    for x in C.LEVER:
        t.append((f"lever:{x}", lambda x=x: src_lever(x), 0))
    for x in C.ASHBY:
        t.append((f"ashby:{x}", lambda x=x: src_ashby(x), 0))
    t += [("remoteok", src_remoteok, 0), ("weworkremotely", src_wwr, 0),
          ("himalayas", src_himalayas, 10), ("jobicy", src_jobicy, 30),
          ("remotive", src_remotive, 360)]   # Remotive asks API users to poll rarely (and delays listings)
    return t


# --------------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        s.setdefault("seen", {}); s.setdefault("fp", {}); s.setdefault("fetched", {})
        return s, False
    except FileNotFoundError:
        return {"seen": {}, "fp": {}, "fetched": {}}, True


def save_state(state):
    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    new = json.dumps(state, sort_keys=True, indent=0)
    try:
        if open(STATE_FILE).read() == new:
            return
    except FileNotFoundError:
        pass
    with open(STATE_FILE, "w") as f:
        f.write(new)


def fingerprint(j: Job) -> str:
    n = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return f"{n(j.company)}|{n(j.title)}"


# --------------------------------------------------------------------------- notify
def fmt_money(v, cur):
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get((cur or "").upper(), (cur or "") + " ")
    return f"{sym}{v/1000:.0f}k"


def describe(job: Job, loc: str, sal: str):
    if sal == "unknown":
        pay = "salary not listed"
    else:
        lo, hi = job.smin, job.smax
        rng = fmt_money(lo, job.cur) if (not hi or lo == hi or not lo) else f"{fmt_money(lo, job.cur)}-{fmt_money(hi, job.cur)}"
        if not lo:
            rng = fmt_money(hi, job.cur)
        g = to_gbp(job.smax or job.smin, job.cur)
        pay = rng + (f" (top end ≈ £{g/1000:.0f}k)" if (job.cur or "USD").upper() != "GBP" and g else "")
    where = {"worldwide": "Worldwide", "region_ok": f"Remote ({job.location})",
             "unspecified": "Remote - CHECK ELIGIBILITY"}.get(loc, job.location)
    age = ""
    if job.posted:
        mins = max(0, int((time.time() - job.posted) / 60))
        age = f" · posted {mins}m ago" if mins < 120 else f" · posted {mins // 60}h ago" if mins < 2880 else ""
    return f"{where} · {pay} · {job.source}{age}"


def send(title, body, url=None, priority=3, tags="briefcase") -> bool:
    ok = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            r = requests.post(os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
                              json={"topic": topic, "title": title, "message": body, "priority": priority,
                                    "tags": tags.split(","), **({"click": url} if url else {})}, timeout=TIMEOUT)
            r.raise_for_status()
            ok = True
        except Exception as e:
            log(f"  ntfy failed: {e}")
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": f"{title}\n{body}\n{url or ''}",
                                    "disable_web_page_preview": False}, timeout=TIMEOUT)
            r.raise_for_status()
            ok = True
        except Exception as e:
            log(f"  telegram failed: {e}")
    if not topic and not (tok and chat):
        print(f"[no channel configured] {title}\n  {body}\n  {url}")
    return ok


# --------------------------------------------------------------------------- main pass
def collect(state):
    now = time.time()
    tasks = [t for t in build_tasks() if now - state["fetched"].get(t[0], 0) >= t[2] * 60]
    jobs, failed = [], []

    def run(t):
        name, fn, _ = t
        try:
            return name, fn(), None
        except Exception as e:
            return name, [], e

    with ThreadPoolExecutor(max_workers=16) as ex:
        for name, res, err in ex.map(run, tasks):
            if err:
                failed.append(name)
                log(f"  [{name}] failed: {err}")
            else:
                jobs.extend(res)
                state["fetched"][name] = now
    return jobs, failed, len(tasks)


def one_pass(dry_run=False):
    state, first_run = load_state()
    jobs, failed, n_tasks = collect(state)
    log(f"checked {n_tasks - len(failed)}/{n_tasks} sources, {len(jobs)} title matches")
    if n_tasks and len(failed) == n_tasks:
        log("ALL sources failed - network problem?")
        return 1

    fresh = []
    fps = dict(state["fp"])
    for j in jobs:
        if j.jid in state["seen"]:
            continue
        verdict = evaluate(j)
        if not verdict:
            continue
        fp = fingerprint(j)
        if fp in fps and fps[fp] != j.source:        # same role already seen via another board
            state["seen"][j.jid] = time.time()
            continue
        fps.setdefault(fp, j.source)
        fresh.append((j, *verdict))

    # strongest first: worldwide + known good salary, then worldwide, then the rest; newest first within tier
    rank = lambda x: (0 if x[1] == "worldwide" and x[2] == "ok" else 1 if x[1] == "worldwide" else 2 if x[2] == "ok" else 3,
                      -(x[0].posted or 0))
    fresh.sort(key=rank)
    log(f"{len(fresh)} new matching roles")

    if dry_run:
        for j, loc, sal in fresh:
            print(f"- {j.title} @ {j.company}\n  {describe(j, loc, sal)}\n  {j.url}")
        return 0

    if first_run and not os.environ.get("FIRST_RUN_NOTIFY", "1") == "1":
        fresh_to_send = []
    else:
        fresh_to_send = fresh[:MAX_INDIVIDUAL_ALERTS]

    delivered_any = True
    for j, loc, sal in fresh_to_send:
        strong = loc == "worldwide" and sal == "ok"
        prefix = "🟢" if strong else "🔵" if loc == "worldwide" else "🟡"
        ok = send(f"{prefix} {j.title} - {j.company}", describe(j, loc, sal), j.url,
                  priority=4 if strong else 3, tags="briefcase" if not strong else "briefcase,star")
        if not ok and (os.environ.get("NTFY_TOPIC") or os.environ.get("TELEGRAM_BOT_TOKEN")):
            delivered_any = False
            log(f"  not marking as seen (delivery failed): {j.title}")
            continue
        state["seen"][j.jid] = time.time()

    overflow = fresh[MAX_INDIVIDUAL_ALERTS:]
    if overflow:
        names = "; ".join(f"{j.title} @ {j.company}" for j, *_ in overflow[:10])
        send(f"+{len(overflow)} more matching roles", names + (" ..." if len(overflow) > 10 else ""), priority=2)
    for j, *_ in overflow:
        state["seen"][j.jid] = time.time()
    if first_run and not fresh_to_send:
        for j, *_ in fresh:
            state["seen"][j.jid] = time.time()
    state["fp"] = fps
    save_state(state)
    return 0 if delivered_any else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDS")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.test:
        ok = send("✅ Job alerts are working", "You'll get a push like this whenever a matching role appears.",
                  "https://example.com", priority=3)
        return 0 if ok else 1
    if a.loop:
        while True:
            try:
                one_pass(a.dry_run)
            except Exception as e:
                log(f"pass failed: {e}")
            time.sleep(a.loop)
    return one_pass(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())

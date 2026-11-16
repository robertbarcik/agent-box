#!/usr/bin/env python3
"""box report: build status.html from the live state of the box.

Everything shown is read from what is actually configured and logged
(compose config, allowlist, self-test result, guard logs, run transcripts),
not from documentation. Standard library only.

Anything the agent can influence (file names, URLs it requested, commit
subjects, task text) is treated as untrusted and HTML-escaped.
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".box"
OUT = ROOT / "status.html"
e = html.escape
NOW = dt.datetime.now(dt.timezone.utc)


# ------------------------------------------------------------------ collect

def sh(args, timeout=25):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def compose(*args):
    return sh(["docker", "compose", "--project-directory", str(ROOT), "-f", str(ROOT / "compose.yaml"), *args])


def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def env_value(key):
    m = re.search(r"(?m)^%s=(.*)$" % re.escape(key), read(ROOT / ".env"))
    return m.group(1).strip() if m else ""


def parse_ts(s):
    """'2026-11-16T08:39:09' (UTC, optional offset) -> aware datetime, or None."""
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def local(t, fmt="%d %b %Y, %H:%M"):
    return t.astimezone().strftime(fmt) if t else "never"


def ago(t):
    if not t:
        return "never"
    s = int((NOW - t).total_seconds())
    if s < 90:
        return "just now"
    if s < 5400:
        return "%d minutes ago" % (s // 60)
    if s < 129600:
        return "%d hours ago" % (s // 3600)
    return "%d days ago" % (s // 86400)


def collect_config():
    cfg = {"ok": False, "mounts": [], "internal": None, "read_only": None, "cap_drop": [],
           "user": "", "mem": "", "cpus": "", "pids": "", "agent_key": ""}
    raw = compose("config", "--format", "json")
    if not raw:
        return cfg
    try:
        c = json.loads(raw)
    except ValueError:
        return cfg
    a = c.get("services", {}).get("agent", {})
    for v in a.get("volumes", []):
        if v.get("type") == "bind":
            cfg["mounts"].append({"host": v.get("source", ""), "box": v.get("target", ""),
                                  "ro": bool(v.get("read_only"))})
    cfg.update(ok=True, read_only=a.get("read_only"), cap_drop=a.get("cap_drop") or [],
               user=a.get("user", ""), pids=a.get("pids_limit", ""), cpus=a.get("cpus", ""),
               agent_key=(a.get("environment") or {}).get("OPENROUTER_API_KEY", ""),
               model=(a.get("environment") or {}).get("BOX_MODEL", ""))
    mem = a.get("mem_limit")
    try:
        cfg["mem"] = "%.0f GB" % (int(mem) / 2 ** 30)
    except (TypeError, ValueError):
        cfg["mem"] = str(mem or "")
    cfg["internal"] = bool(c.get("networks", {}).get("inside", {}).get("internal"))
    return cfg


def collect_running():
    raw = compose("ps", "--format", "json") or ""
    rows = []
    for chunk in ([raw] if raw.strip().startswith("[") else raw.splitlines()):
        try:
            j = json.loads(chunk)
        except ValueError:
            continue
        rows.extend(j if isinstance(j, list) else [j])
    return {r.get("Service"): r.get("State") for r in rows}


def collect_allowlist():
    out = []
    for line in read(ROOT / "egress" / "allowlist.txt").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def collect_test():
    text = read(STATE / "last-test.txt")
    if not text:
        return None
    when, groups, cur = None, [], None
    for line in text.splitlines():
        if line.startswith("# "):
            when = parse_ts(line[2:])
        elif re.match(r"^\s+(PASS|FAIL)\s", line):
            verdict, desc = line.split(None, 1)
            if cur is None:
                cur = {"name": "Checks", "checks": []}
                groups.append(cur)
            cur["checks"].append({"ok": verdict == "PASS", "desc": desc.strip()})
        elif line.strip() and not re.match(r"^\d+ passed", line):
            cur = {"name": line.strip(), "checks": []}
            groups.append(cur)
    checks = [c for g in groups for c in g["checks"]]
    return {"when": when, "groups": groups, "passed": sum(c["ok"] for c in checks),
            "failed": sum(not c["ok"] for c in checks)}


def collect_budget():
    key = env_value("OPENROUTER_API_KEY")
    if not key:
        return {"state": "nokey"}
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                     headers={"Authorization": "Bearer " + key})
        d = json.load(urllib.request.urlopen(req, timeout=10))["data"]
    except Exception:  # offline, expired key, API change: the page still renders
        return {"state": "unknown"}
    b = d.get("effective_budget") or {}
    limit = d.get("limit")
    return {"state": "ok", "limit": limit, "used": b.get("spend_usd", d.get("usage")),
            "remaining": d.get("limit_remaining"), "reset": d.get("limit_reset") or b.get("reset_interval"),
            "resets_at": parse_ts((b.get("resets_at") or "")[:19]),
            "expires": parse_ts((d.get("expires_at") or "")[:19])}


def test_windows():
    out = []
    for line in read(STATE / "test-windows").splitlines():
        p = line.split()
        if len(p) == 2 and parse_ts(p[0]) and parse_ts(p[1]):
            out.append((parse_ts(p[0]), parse_ts(p[1])))
    return out


def collect_logs():
    """Model calls (gateway) and internet requests (egress), self-test noise removed."""
    wins = test_windows()

    def in_test(t):
        return any(a <= t <= b for a, b in wins)

    calls, reqs = [], []
    for line in read(ROOT / "logs" / "gateway.log").splitlines():
        m = re.match(r"(\S+) (\S+) (\S+) status=(\d+)", line)
        t = parse_ts(m.group(1)) if m else None
        if t and not in_test(t) and "/chat/completions" in m.group(3) or t and not in_test(t) and "/responses" in m.group(3):
            calls.append({"t": t, "ok": m.group(4) == "200", "status": m.group(4)})
    for line in read(ROOT / "logs" / "egress.log").splitlines():
        m = re.match(r"(\S+) (\S+)/(\d+) (\S+) (\S+)", line)
        t = parse_ts(m.group(1)) if m else None
        if t and not in_test(t):
            target = re.sub(r"^https?://", "", m.group(5)).split("/")[0]
            target = re.sub(r":(443|80)$", "", target)
            reqs.append({"t": t, "allowed": "DENIED" not in m.group(2), "target": target[:80]})
    return calls, reqs


def collect_runs():
    runs = []
    for p in sorted((ROOT / "runs").glob("*.jsonl")):
        task = read(p.with_name(p.name.replace(".jsonl", ".task.txt"))).splitlines()
        first, tools, errors, cost = None, 0, 0, 0.0
        try:
            with p.open(errors="replace") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if first is None and isinstance(ev.get("timestamp"), (int, float)):
                        first = dt.datetime.fromtimestamp(ev["timestamp"] / 1000, dt.timezone.utc)
                    kind = ev.get("type")
                    tools += kind == "tool_use"
                    errors += kind == "error"
                    if kind == "step_finish":
                        c = (ev.get("part") or {}).get("cost")
                        cost += c if isinstance(c, (int, float)) else 0
        except OSError:
            continue
        if first is None:
            first = dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
        runs.append({"t": first, "file": "runs/" + p.name, "tools": tools, "errors": errors, "cost": cost,
                     "model": task[0] if task else "", "task": " ".join(task[1:]) if len(task) > 1 else "",
                     "workspace": p.stem.split("-", 2)[-1]})
    return runs


def collect_folder(name, limit=12):
    d = ROOT / name
    items = []
    try:
        for p in d.rglob("*"):
            if p.name in (".gitkeep", ".DS_Store"):
                continue
            st = p.lstat()
            if os.path.isdir(p) and not os.path.islink(p):
                continue
            items.append({"name": str(p.relative_to(d)), "size": st.st_size,
                          "t": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc),
                          "link": os.path.islink(p), "exec": bool(st.st_mode & 0o111) and not os.path.islink(p)})
    except OSError:
        pass
    items.sort(key=lambda i: i["t"], reverse=True)
    return {"count": len(items), "items": items[:limit]}


def collect_repos():
    repos = []
    for d in sorted((STATE / "repos").glob("*/")):
        full = read(d / "full").strip()
        if full:
            repos.append({"name": d.name, "full": full, "added": parse_ts(read(d / "added").strip())})
    prs = []
    for line in read(STATE / "prs.log").splitlines():
        p = line.split()
        if len(p) >= 4:
            pr = {"t": parse_ts(p[0]), "full": p[1], "url": p[3], "state": "", "title": ""}
            raw = sh(["gh", "pr", "view", p[3], "--json", "state,title"], timeout=15)
            if raw:
                try:
                    j = json.loads(raw)
                    pr["state"], pr["title"] = j.get("state", ""), j.get("title", "")
                except ValueError:
                    pass
            prs.append(pr)
    return repos, prs


# ------------------------------------------------------------- explanations
# Plain-language pop-ups. key -> (title, [paragraphs], command to try or "")

EXPLAIN = {
    "you": ("You", [
        "You are outside the box. You decide what goes in (a project folder, files in the inbox, "
        "an open website) and you look at everything that comes out before you use it.",
        "The agent cannot reach you or your computer. It can only answer when you talk to it, and "
        "leave files in the places you gave it."], "bin/box start"),
    "agent": ("The agent", [
        "A language model with hands. The model writes text; a program called opencode turns that "
        "text into actions: read this file, run that command, change this line.",
        "Inside its room it may do anything without asking. That is safe because of the room, not "
        "because of the agent: whatever it does, it does to a folder you chose to hand over."],
        "bin/box shell"),
    "wall": ("The wall", [
        "The agent lives on a private network that has no connection to the internet or to your "
        "home network. It is not blocked by a rule that could be switched off from inside. The "
        "road simply does not exist.",
        "There are exactly two doors in this wall, and each has a guard."], "bin/box test"),
    "workspace": ("The project folder", [
        "The one folder of your computer the agent can change. It is the project you asked it to "
        "work on.",
        "Treat it as the agent's desk: anything here may be changed or deleted. Put in a copy, "
        "never your only original."], "bin/box diff"),
    "inbox": ("The inbox", [
        "A folder where you drop things for the agent: a PDF to read, example data, a note with "
        "instructions.",
        "The agent can read what is here but cannot change or delete it. What you put in stays "
        "exactly as you left it."], "open inbox/"),
    "outbox": ("The outbox", [
        "A folder where the agent leaves things for you: a report, a generated file, notes on "
        "what it did.",
        "Everything here was written by the agent. Reading it is always safe. Running it (a "
        "script, a program) is a decision: read it first."], "open outbox/"),
    "gateway": ("Door 1: the line to the model", [
        "The agent has to talk to its model, and that costs money, so it needs your API key. If "
        "the key were inside the box, the agent could read it and leak it.",
        "So the key stays with a guard. The agent hands its message to the guard, the guard "
        "attaches the key and passes it on. The agent uses the key without ever seeing it.",
        "The guard also refuses anything that is not a conversation with the model, such as "
        "questions about your account."], "bin/box logs"),
    "model": ("The model", [
        "The model runs at a company on the internet, reached through OpenRouter. It never runs on "
        "your computer.",
        "Everything the agent reads while working is sent there as part of the conversation. That "
        "is how every coding agent works. So the rule is simple: what you put in the project "
        "folder or inbox, the model's provider gets to see."], ""),
    "egress": ("Door 2: the internet, by guest list", [
        "The second guard has a list of websites. A request to a site on the list goes through. "
        "Everything else is refused and written down.",
        "The list starts empty: no websites at all. You open a site only when a task needs it, for "
        "example the Python package index to install a library, and close it again afterwards.",
        "Every open site is a place the agent could also send things to. That is why the list is "
        "short."], "bin/box egress"),
    "internet": ("The internet", [
        "Everything else out there. From inside the box it does not exist unless you put a site on "
        "the guest list."], ""),
    "ghost-files": ("Your files", [
        "Documents, photos, downloads, other projects. They are not blocked. They are simply not "
        "there: from inside the box your computer consists of three folders.",
        "An agent that goes looking for your files finds an empty building."], "bin/box shell"),
    "ghost-secrets": ("Your passwords and keys", [
        "Passwords, SSH keys, cloud logins, browser sessions. None of them are in the box, and the "
        "one key the agent needs is held by the guard at door 1.",
        "This matters because of prompt injection: a web page or a file can contain text that "
        "talks the agent into doing something. If there is nothing to steal, a tricked agent "
        "steals nothing."], ""),
    "ghost-lan": ("Your home network", [
        "Your router, printer, NAS, other computers. An agent on your laptop would normally sit in "
        "the middle of all of them.",
        "The box's network has no road to them, and the guard at door 2 refuses home addresses."],
        ""),
    "ghost-github": ("Your GitHub account", [
        "The agent has no login to GitHub. It can make commits in its copy of a repository, and "
        "nothing more.",
        "Publishing is done by you: one command takes the agent's commits out as plain data and "
        "opens a pull request, which you review like a contribution from a stranger."],
        "bin/box pr"),
    # --- "can" rows
    "can-commands": ("Run any command inside the box", [
        "Install tools into its own folder, run tests, delete files in the project. It does not "
        "ask first.",
        "Most coding agents stop and ask before each risky command, and people get used to "
        "pressing yes. Here the walls do that job, so the agent can work without interruption."],
        ""),
    "can-budget": ("Spend your money, up to a limit", [
        "Each message to the model costs a little. An agent stuck in a loop can send thousands.",
        "The limit is set on the key itself at OpenRouter, not in the box, so nothing inside the "
        "box can raise it."], ""),
    "can-repos": ("Work on repositories you granted", [
        "Each repository is granted one by one. The agent gets a fresh copy with the full history "
        "and no connection back to GitHub.",
        "It commits in its copy. You turn those commits into a pull request with one command and "
        "review it before merging. Changes to files that run by themselves (automation, editor and "
        "agent settings) are refused unless you allow them."], "bin/box repo list"),
    "can-domains": ("Reach the websites you opened", [
        "These sites are on the guest list right now. The agent can download from them and could "
        "also send data to them.",
        "Close them when the task is done: put a # in front of the line in egress/allowlist.txt "
        "and reload."], "bin/box reload"),
    # --- structural "cannot" rows (not part of the self-test)
    "cannot-push": ("Publish anything by itself", [
        "No GitHub login, no way to push, no way to open a pull request. Publishing always goes "
        "through you."], "bin/box pr"),
    "cannot-control": ("Stop, restart or reconfigure the box", [
        "The controls are on your side of the wall. The box command does not exist inside, and "
        "the agent cannot see the files that define its own walls."], ""),
}

# Explanations for the self-test lines, keyed by the text the test prints.
CHECKS = {
    "no direct route to the internet": ("Go straight to the internet",
        "The test tries to open a website without using the guard. It gets no connection at all."),
    "no direct route by IP address either": ("Go to the internet by number",
        "Websites have names and numbers. Blocking only names is a classic mistake, so the test "
        "also tries a raw number. Still no road."),
    "outside names do not resolve (no DNS leak)": ("Look up addresses of outside sites",
        "Looking up a website's address is itself a small message to the outside, and data can be "
        "smuggled out through such lookups. Inside the box, lookups for outside names fail."),
    "proxy refuses a domain not on the allowlist": ("Visit a site that is not on the guest list",
        "The test asks the guard at door 2 for a site you never opened. Refused and logged."),
    "proxy refuses the host machine": ("Reach your computer through the guard",
        "The test asks the guard to connect it to your own computer. Refused: your machine is not "
        "on the guest list either."),
    "proxy refuses a home-network address": ("Reach your home network",
        "The test asks for a typical home router address. Refused."),
    "gateway is up": ("(Door 1 is working)",
        "A check that the line to the model works, so a failure elsewhere is not just a broken box."),
    "model catalogue is reachable": ("(The model can be reached)",
        "The agent can see which models exist. That is all it needs to start working."),
    "account endpoints are refused": ("Look at your OpenRouter account",
        "The guard at door 1 passes conversations with the model and nothing else. Questions about "
        "your credit or account are turned away."),
    "key endpoints are refused": ("Create or read API keys",
        "Same guard, same rule: nothing about keys gets through."),
    "the real API key is not in the environment": ("See your API key",
        "The agent's own copy of the key is a placeholder text. The real one never enters its room."),
    "no host home directory is mounted": ("See your personal files",
        "The folders where your documents live do not exist inside the box."),
    "no Docker socket": ("Take control of Docker",
        "With access to Docker's control channel, a program can start a new container that sees "
        "your whole disk. That channel is not handed in."),
    "no SSH keys or cloud credentials": ("Find logins to other systems",
        "No SSH keys, no cloud credentials, no GitHub login anywhere in the box."),
    "not running as root": ("Act as administrator",
        "The agent is an ordinary user. Many ways of breaking out of a container need "
        "administrator rights first."),
    "no sudo": ("Become administrator",
        "There is no command to switch to administrator, and the box is started with a setting "
        "that forbids gaining new rights."),
    "system files are read-only": ("Change the system it runs on",
        "Everything outside its three folders and a scratch area is read-only. It cannot plant a "
        "modified tool for later."),
    "cannot install system packages": ("Install system software",
        "Needs administrator rights and internet. It has neither."),
    "workspace is writable": ("(It can work in the project folder)",
        "A check that the agent can do its job."),
    "outbox is writable": ("(It can leave things in the outbox)",
        "A check that the agent can hand you results."),
    "inbox can be read but not changed": ("Change or delete what you put in the inbox",
        "The inbox is handed in read-only. Your originals stay as they are."),
}


# ------------------------------------------------------------------- render

CSS = """
:root{--bg:#E8EDF2;--surface:#fff;--ink:#12233B;--soft:#51627A;--line:#C9D3DF;--wall:#12233B;
--can:#0E7C66;--can-bg:#DDF1EC;--alert:#B4233A;--alert-bg:#F9E1E5;--wait:#8A5B00;--wait-bg:#FFF0C7;
--grid:#D5DEE8;--ghost:#8B99AB}
@media (prefers-color-scheme:dark){:root{--bg:#0D1724;--surface:#162336;--ink:#E7EDF5;--soft:#9DB0C8;
--line:#2B3D55;--wall:#B9CBE2;--can:#4FD0B4;--can-bg:#103A35;--alert:#FF8C9C;--alert-bg:#4A1822;
--wait:#FFD071;--wait-bg:#3F3110;--grid:#1D2D43;--ghost:#6F8199}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
h1,h2,h3,.num{font-family:ui-rounded,"SF Pro Rounded","Nunito","Avenir Next",system-ui,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:2.1rem;line-height:1.15;margin:0 0 6px;font-weight:800;letter-spacing:-.01em}
h2{font-size:1.35rem;margin:0 0 4px;font-weight:800}
.lead{color:var(--soft);margin:0 0 18px;max-width:64ch}
.facts{display:flex;flex-wrap:wrap;gap:8px 10px;margin:0 0 26px;padding:0;list-style:none}
.facts li{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:4px 13px;font-size:.92rem}
.facts b{font-weight:650}
.facts .on{background:var(--can-bg);border-color:transparent;color:var(--can);font-weight:700}
.facts .off{background:transparent;color:var(--soft);font-weight:700}
section{margin:0 0 30px}
.waiting{background:var(--wait-bg);border-radius:14px;padding:16px 20px}
.waiting h2{color:var(--wait)}
.waiting ul{margin:8px 0 0;padding-left:20px}
.waiting li{margin:3px 0}
.waiting.clear{background:var(--can-bg)}
.waiting.clear h2{color:var(--can)}
.plan{background:var(--surface);border-radius:14px;padding:14px 14px 6px;overflow-x:auto;
background-image:linear-gradient(var(--grid) 1px,transparent 1px),linear-gradient(90deg,var(--grid) 1px,transparent 1px);
background-size:24px 24px}
.plan svg{display:block;min-width:820px;width:100%;height:auto}
.plan text{fill:var(--ink);font:600 15px system-ui,sans-serif}
.plan .small{font:400 12.5px system-ui,sans-serif;fill:var(--soft)}
.plan .big{font:800 19px ui-rounded,"Nunito",system-ui,sans-serif}
.plan .hit{cursor:pointer;outline:none}
.plan .hit:hover .box,.plan .hit:focus-visible .box{stroke-width:3.5}
.plan .box{fill:var(--surface);stroke:var(--ink);stroke-width:2}
.plan .wallbox{fill:none;stroke:var(--wall);stroke-width:11;stroke-linejoin:miter}
.plan .door{fill:var(--can-bg);stroke:var(--can);stroke-width:2.5}
.plan .door.shut{fill:var(--surface);stroke:var(--soft);stroke-dasharray:5 4}
.plan .flow{stroke:var(--can);stroke-width:2.5;fill:none}
.plan .flow.shut{stroke:var(--soft);stroke-dasharray:5 5}
.plan .ghost .box{stroke:var(--ghost);stroke-dasharray:4 5;fill:none}
.plan .ghost text{fill:var(--ghost);text-decoration:line-through}
.plan .canfill{fill:var(--can-bg);stroke:var(--can)}
.hint{color:var(--soft);font-size:.9rem;margin:8px 4px 6px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:26px}
@media (max-width:760px){.cols{grid-template-columns:1fr}h1{font-size:1.7rem}}
.list{list-style:none;margin:12px 0 0;padding:0;background:var(--surface);border-radius:14px;overflow:hidden}
.list li+li{border-top:1px solid var(--line)}
.group{padding:9px 16px 3px;font-size:.84rem;color:var(--soft);font-weight:650}
.row{display:flex;gap:11px;align-items:flex-start;width:100%;padding:10px 16px;background:none;border:0;
color:inherit;font:inherit;text-align:left;cursor:pointer}
.row:hover{background:var(--bg)}
.row:focus-visible,.plan .hit:focus-visible,.seg button:focus-visible,.pop button:focus-visible{outline:3px solid var(--can);outline-offset:-3px}
.mark{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:.8rem;font-weight:800;margin-top:1px}
.mark.yes{background:var(--can-bg);color:var(--can)}
.mark.held{background:var(--wall);color:var(--surface)}
.mark.fail{background:var(--alert);color:#fff}
.mark.unk{border:1.5px dashed var(--soft);color:var(--soft)}
.row .what{flex:1}
.row .detail{display:block;color:var(--soft);font-size:.9rem;overflow-wrap:anywhere}
.row .q{flex:none;color:var(--soft);font-size:.85rem;border:1px solid var(--line);border-radius:50%;width:21px;height:21px;display:grid;place-items:center}
.failnote{color:var(--alert);font-weight:700}
.seg{display:inline-flex;background:var(--surface);border-radius:999px;padding:3px;margin:10px 0 0}
.seg button{border:0;background:none;color:var(--soft);font:inherit;font-size:.92rem;padding:5px 14px;border-radius:999px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--surface);font-weight:650}
.period{display:none}.period.show{display:block}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
@media (max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--surface);border-radius:14px;padding:12px 16px}
.stat .num{font-size:1.9rem;font-weight:800;line-height:1.1;display:block}
.stat span{color:var(--soft);font-size:.9rem}
table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:14px;overflow:hidden;margin:10px 0 0}
th,td{text-align:left;padding:8px 16px;font-size:.93rem;vertical-align:top;overflow-wrap:anywhere}
th{color:var(--soft);font-weight:650;font-size:.84rem;white-space:nowrap}
tr+tr td{border-top:1px solid var(--line)}
td.n{white-space:nowrap;font-variant-numeric:tabular-nums}
@media (max-width:760px){table{display:block;overflow-x:auto}}
.empty{background:var(--surface);border-radius:14px;padding:14px 16px;color:var(--soft);margin:10px 0 0}
.tag{display:inline-block;border-radius:6px;padding:0 7px;font-size:.8rem;font-weight:650}
.tag.ok{background:var(--can-bg);color:var(--can)}.tag.no{background:var(--alert-bg);color:var(--alert)}
.tag.warn{background:var(--wait-bg);color:var(--wait)}
.meter{height:14px;background:var(--line);border-radius:999px;overflow:hidden;margin:10px 0 6px}
.meter i{display:block;height:100%;background:var(--can);border-radius:999px;min-width:4px}
.meter i.high{background:var(--alert)}
.card{background:var(--surface);border-radius:14px;padding:16px 20px}
code,kbd{font:.88em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);padding:1px 6px;border-radius:5px}
h3{font-size:1.02rem;margin:22px 0 0;font-weight:750}
footer{color:var(--soft);font-size:.9rem;margin-top:36px}
a{color:inherit}
.pop{border:0;border-radius:16px;padding:24px 26px 20px;max-width:min(520px,calc(100vw - 32px));background:var(--surface);
color:var(--ink);box-shadow:0 18px 60px rgba(8,20,40,.35)}
.pop::backdrop{background:rgba(8,20,40,.45)}
.pop h3{font-size:1.3rem;margin:0 0 8px;font-weight:800}
.pop p{margin:0 0 10px}
.pop .try{background:var(--bg);border-radius:10px;padding:9px 12px;margin:12px 0 0;font-size:.92rem}
.pop .verdict{font-weight:700;margin-bottom:8px}
.pop .verdict.held{color:var(--can)}.pop .verdict.fail{color:var(--alert)}
.pop button{margin-top:14px;border:0;background:var(--ink);color:var(--surface);font:inherit;font-weight:650;padding:7px 18px;border-radius:999px;cursor:pointer}
"""

JS = """
document.addEventListener('click',function(ev){
  var hit=ev.target.closest('[data-pop]');
  if(hit){var p=document.getElementById(hit.getAttribute('data-pop'));if(p&&p.showPopover){p.showPopover()}return}
  var seg=ev.target.closest('.seg button');
  if(seg){showPeriod(seg.getAttribute('data-period'))}
});
document.addEventListener('keydown',function(ev){
  if((ev.key==='Enter'||ev.key===' ')&&ev.target.matches&&ev.target.matches('.hit[data-pop]')){ev.preventDefault();ev.target.dispatchEvent(new MouseEvent('click',{bubbles:true}))}
});
function showPeriod(k){
  document.querySelectorAll('.period').forEach(function(el){el.classList.toggle('show',el.getAttribute('data-period')===k)});
  document.querySelectorAll('.seg button').forEach(function(b){b.setAttribute('aria-pressed',b.getAttribute('data-period')===k?'true':'false')});
  try{localStorage.setItem('box-period',k)}catch(err){}
}
try{var saved=localStorage.getItem('box-period');if(saved&&document.querySelector('.period[data-period="'+saved+'"]')){showPeriod(saved)}}catch(err){}
"""

POPS = []  # collected pop-up markup


def pop(pid, title, paras, cmd="", verdict=None):
    """Register a pop-up; return its id."""
    v = ""
    if verdict is True:
        v = '<p class="verdict held">Tried in the last self-test. The wall held.</p>'
    elif verdict is False:
        v = '<p class="verdict fail">The last self-test got through here. Fix this before using the box.</p>'
    body = "".join("<p>%s</p>" % e(p) for p in paras)
    t = '<p class="try">See it yourself: <code>%s</code></p>' % e(cmd) if cmd else ""
    POPS.append('<div class="pop" popover id="%s"><h3>%s</h3>%s%s%s'
                '<button popovertarget="%s" popovertargetaction="hide">Got it</button></div>'
                % (pid, e(title), v, body, t, pid))
    return pid


def explain(key):
    title, paras, cmd = EXPLAIN[key]
    return pop("p-" + key, title, paras, cmd)


def row(mark, kind, what, detail, pid):
    d = '<span class="detail">%s</span>' % detail if detail else ""
    return ('<li><button class="row" popovertarget="%s"><span class="mark %s" aria-hidden="true">%s</span>'
            '<span class="what">%s%s</span><span class="q" aria-hidden="true">?</span></button></li>'
            % (pid, kind, mark, e(what), d))


def size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def money(x):
    return "$%.2f" % x if isinstance(x, (int, float)) else "?"


def render_plan(d):
    ws = e(d["workspace"])
    doors_open = len(d["allow"])
    egress_cls = "" if doors_open else " shut"
    egress_label = ("%d site%s open" % (doors_open, "" if doors_open == 1 else "s")) if doors_open else "no sites open"
    model = e(d["model"].replace("openrouter/", "")[:34])
    for k in ("you", "agent", "wall", "workspace", "inbox", "outbox", "gateway", "model", "egress",
              "internet", "ghost-files", "ghost-secrets", "ghost-lan", "ghost-github"):
        explain(k)

    def hit(key, inner, cls=""):
        return ('<g class="hit %s" tabindex="0" role="button" data-pop="p-%s" aria-label="%s">%s</g>'
                % (cls, key, e(EXPLAIN[key][0]), inner))

    def shelf(key, y, label, sub, arrow):
        return hit(key, '<rect class="box" x="150" y="%d" width="170" height="62" rx="8"/>'
                        '<text x="166" y="%d">%s</text><text class="small" x="166" y="%d">%s</text>'
                        % (y, y + 26, label, y + 46, sub)) + arrow

    s = ['<svg viewBox="0 0 980 470" role="img" aria-label="Floor plan of the box: the agent in a walled room, '
         'three shared folders on the left, two guarded doors on the right">']
    s.append('<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0 0L10 5L0 10z" fill="var(--can)"/></marker></defs>')
    # you
    s.append(hit("you", '<rect class="box" x="18" y="150" width="96" height="120" rx="48"/>'
                        '<circle cx="66" cy="190" r="15" fill="none" stroke="var(--ink)" stroke-width="2"/>'
                        '<text class="big" x="66" y="240" text-anchor="middle">You</text>'))
    # shelves with arrows into the room
    s.append(shelf("inbox", 62, "inbox", "it reads only",
                   '<path class="flow" d="M322 93H402" marker-end="url(#ah)"/>'))
    s.append(shelf("workspace", 179, "project: " + ws[:11], "it reads and changes",
                   '<path class="flow" d="M322 210H402" marker-end="url(#ah)" marker-start="url(#ah)"/>'))
    s.append(shelf("outbox", 296, "outbox", "it writes, you read",
                   '<path class="flow" d="M402 327H322" marker-end="url(#ah)"/>'))
    s.append('<path class="flow" d="M116 210H148" marker-end="url(#ah)" marker-start="url(#ah)"/>')
    # the room
    s.append(hit("wall", '<rect class="wallbox" x="410" y="40" width="240" height="340"/>'
                         '<text class="small" x="426" y="70">the wall: no road out</text>'))
    s.append(hit("agent", '<rect class="box canfill" x="444" y="110" width="166" height="200" rx="14"/>'
                          '<text class="big" x="527" y="204" text-anchor="middle">The agent</text>'
                          '<text class="small" x="527" y="228" text-anchor="middle">may do anything in here</text>'))
    # door 1 (set into the right wall)
    s.append('<path class="flow" d="M612 148H634" marker-end="url(#ah)"/>')
    s.append(hit("gateway", '<rect class="door" x="638" y="110" width="166" height="76" rx="8"/>'
                            '<text x="652" y="138">Door 1: guard</text>'
                            '<text class="small" x="652" y="158">holds your key</text>'
                            '<text class="small" x="652" y="174">model calls only</text>'))
    s.append('<path class="flow" d="M806 148H846" marker-end="url(#ah)"/>')
    s.append(hit("model", '<rect class="box" x="850" y="110" width="116" height="76" rx="38"/>'
                          '<text x="908" y="142" text-anchor="middle">The model</text>'
                          '<text class="small" x="908" y="162" text-anchor="middle">%s</text>' % model[:17]))
    # door 2
    s.append('<path class="flow%s" d="M612 272H634"%s/>' % (egress_cls, ' marker-end="url(#ah)"' if doors_open else ""))
    s.append(hit("egress", '<rect class="door%s" x="638" y="234" width="166" height="76" rx="8"/>'
                           '<text x="652" y="262">Door 2: guest list</text>'
                           '<text class="small" x="652" y="282">%s</text>'
                           '<text class="small" x="652" y="298">all else is refused</text>'
                           % (egress_cls, egress_label)))
    s.append('<path class="flow%s" d="M806 272H846"%s/>' % (egress_cls, ' marker-end="url(#ah)"' if doors_open else ""))
    s.append(hit("internet", '<rect class="box" x="850" y="234" width="116" height="76" rx="38"/>'
                             '<text x="908" y="277" text-anchor="middle">Internet</text>'))
    # what is not in the picture
    s.append('<text class="small" x="18" y="418">Not in the box at all:</text>')
    x = 18
    for key, label, w in (("ghost-files", "your files", 120), ("ghost-secrets", "passwords and keys", 178),
                          ("ghost-lan", "home network", 142), ("ghost-github", "your GitHub login", 168)):
        s.append(hit(key, '<rect class="box" x="%d" y="428" width="%d" height="34" rx="8"/>'
                          '<text x="%d" y="450" text-anchor="middle">%s</text>' % (x, w, x + w / 2, label), "ghost"))
        x += w + 14
    s.append("</svg>")
    return "".join(s)


def render_can(d):
    rows = []
    ws = d["workspace"]
    rows.append(row("✓", "yes", "Read and change the project folder", "<code>workspaces/%s</code>" % e(ws), "p-workspace"))
    rows.append(row("✓", "yes", "Read what you drop in the inbox",
                    "%d file%s there now" % (d["inbox"]["count"], "" if d["inbox"]["count"] == 1 else "s"), "p-inbox"))
    rows.append(row("✓", "yes", "Leave files for you in the outbox",
                    "%d file%s there now" % (d["outbox"]["count"], "" if d["outbox"]["count"] == 1 else "s"), "p-outbox"))
    rows.append(row("✓", "yes", "Talk to its model", "<code>%s</code>" % e(d["model"].replace("openrouter/", "")), "p-model"))
    rows.append(row("✓", "yes", "Run any command inside the box",
                    e("up to %s memory, %s processors" % (d["cfg"]["mem"] or "?", d["cfg"]["cpus"] or "?")),
                    explain("can-commands")))
    b = d["budget"]
    if b["state"] == "ok" and isinstance(b.get("limit"), (int, float)):
        det = "%s of %s used%s" % (money(b["used"]), money(b["limit"]), (", resets " + e(str(b["reset"]))) if b.get("reset") else "")
    elif b["state"] == "nokey":
        det = '<span class="failnote">no key set, so the model cannot be reached</span>'
    else:
        det = "limit set on the key at OpenRouter"
    rows.append(row("✓", "yes", "Spend money on model calls, up to the key's limit", det, explain("can-budget")))
    if d["repos"]:
        det = ", ".join("<code>%s</code>" % e(r["full"]) for r in d["repos"])
        rows.append(row("✓", "yes", "Commit to its copy of granted repositories", det, explain("can-repos")))
    if d["allow"]:
        det = ", ".join("<code>%s</code>" % e(a) for a in d["allow"])
        rows.append(row("✓", "yes", "Reach the websites you opened", det, explain("can-domains")))
    return '<ul class="list">%s</ul>' % "".join(rows)


def render_cannot(d):
    t = d["test"]
    out = []
    if not d["allow"]:
        out.append(row("✕", "held", "Reach any website", "the guest list is empty", "p-egress"))
    if not d["repos"]:
        out.append(row("✕", "held", "Touch any of your repositories", "none granted", explain("can-repos")))
    out.append(row("✕", "held", "Publish anything by itself", "no GitHub login in the box", explain("cannot-push")))
    out.append(row("✕", "held", "Stop, restart or reconfigure the box", "the controls are on your side",
                   explain("cannot-control")))
    if not t:
        out.append('<li class="group">The walls have not been tested on this machine yet. Run <code>bin/box test</code>.</li>')
    else:
        n = 0
        for g in t["groups"]:
            out.append('<li class="group">%s</li>' % e(g["name"]))
            for c in g["checks"]:
                n += 1
                title, text = CHECKS.get(c["desc"], (c["desc"], "A check from the containment self-test."))
                is_sanity = title.startswith("(")
                pid = pop("p-check-%d" % n, title.strip("()"), [text, "Test line: " + c["desc"]],
                          "bin/box test", None if is_sanity else c["ok"])
                if is_sanity:
                    mark, kind = ("✓", "yes") if c["ok"] else ("!", "fail")
                else:
                    mark, kind = ("✕", "held") if c["ok"] else ("!", "fail")
                detail = "" if c["ok"] else '<span class="failnote">the last self-test did not hold here</span>'
                out.append(row(mark, kind, title.strip("()"), detail, pid))
    return '<ul class="list">%s</ul>' % "".join(out)


def render_period(d, days, key, label, show):
    since = NOW - dt.timedelta(days=days) if days else None
    calls = [c for c in d["calls"] if not since or c["t"] >= since]
    reqs = [r for r in d["reqs"] if not since or r["t"] >= since]
    runs = [r for r in d["runs"] if not since or r["t"] >= since]
    denied = [r for r in reqs if not r["allowed"]]
    allowed = [r for r in reqs if r["allowed"]]
    active_days = len({c["t"].astimezone().date() for c in calls})
    h = ['<div class="period%s" data-period="%s">' % (" show" if show else "", key)]
    h.append('<div class="stats">'
             '<div class="stat"><span class="num">%d</span><span>model calls on %d day%s</span></div>'
             '<div class="stat"><span class="num">%d</span><span>tasks run without the UI</span></div>'
             '<div class="stat"><span class="num">%d</span><span>internet requests refused</span></div>'
             '<div class="stat"><span class="num">%d</span><span>internet requests let through</span></div></div>'
             % (len(calls), active_days, "" if active_days == 1 else "s", len(runs), len(denied), len(allowed)))
    failed = sum(not c["ok"] for c in calls)
    if failed:
        h.append('<p class="hint">%d model call%s failed (for example: no key, limit reached, model unavailable).</p>'
                 % (failed, "" if failed == 1 else "s"))

    h.append("<h3>Doors it knocked on</h3>")
    if not reqs:
        h.append('<p class="empty">It did not try to reach the internet in this period.</p>')
    else:
        agg = {}
        for r in reqs:
            a = agg.setdefault((r["target"], r["allowed"]), {"n": 0, "last": r["t"]})
            a["n"] += 1
            a["last"] = max(a["last"], r["t"])
        rows = sorted(agg.items(), key=lambda kv: kv[1]["last"], reverse=True)[:25]
        h.append("<table><tr><th>Destination</th><th>Result</th><th>Times</th><th>Last</th></tr>")
        for (target, ok), a in rows:
            tag = '<span class="tag ok">let through</span>' if ok else '<span class="tag no">refused</span>'
            h.append('<tr><td><code>%s</code></td><td>%s</td><td class="n">%d</td><td class="n">%s</td></tr>'
                     % (e(target), tag, a["n"], e(local(a["last"], "%d %b, %H:%M"))))
        h.append("</table>")

    h.append("<h3>Tasks it was given</h3>")
    if not runs:
        h.append('<p class="empty">No tasks were run with <code>box run</code> in this period. '
                 'Work done in the terminal UI shows up as model calls above.</p>')
    else:
        h.append("<table><tr><th>When</th><th>Task</th><th>Steps</th><th>Transcript</th></tr>")
        for r in sorted(runs, key=lambda r: r["t"], reverse=True)[:40]:
            task = r["task"] or "(task text not recorded)"
            task = task if len(task) <= 220 else task[:217] + "..."
            err = ' <span class="tag no">%d error%s</span>' % (r["errors"], "" if r["errors"] == 1 else "s") if r["errors"] else ""
            h.append('<tr><td class="n">%s</td><td>%s%s<br><span style="color:var(--soft);font-size:.86rem">%s in %s</span></td>'
                     '<td class="n">%d</td><td><code>%s</code></td></tr>'
                     % (e(local(r["t"], "%d %b, %H:%M")), e(task), err,
                        e(r["model"].replace("openrouter/", "") or "model not recorded"), e(r["workspace"]),
                        r["tools"], e(r["file"])))
        h.append("</table>")
    h.append("</div>")
    return "".join(h)


def render_waiting(d):
    items = []
    t = d["test"]
    if t and t["failed"]:
        items.append("<b>%d wall%s did not hold</b> in the last self-test. Do not use the box until "
                     "<code>bin/box test</code> passes." % (t["failed"], "" if t["failed"] == 1 else "s"))
    if not t:
        items.append("The walls have never been tested on this machine. Run <code>bin/box test</code>.")
    elif d["walls_changed"]:
        items.append("The box's configuration changed after the last self-test. Run <code>bin/box test</code> again.")
    if d["budget"]["state"] == "nokey":
        items.append("No API key is set, so the agent cannot reach its model. Add one to <code>.env</code>.")
    b = d["budget"]
    if b["state"] == "ok" and isinstance(b.get("limit"), (int, float)) and b["limit"] and (b.get("used") or 0) / b["limit"] >= 0.8:
        items.append("The key's budget is %d%% used." % round(100 * b["used"] / b["limit"]))
    if b["state"] == "ok" and b.get("expires") and (b["expires"] - NOW).days <= 5:
        items.append("The API key expires %s." % e(local(b["expires"], "%d %b %Y")))
    for pr in d["prs"]:
        if pr["state"] in ("OPEN", ""):
            label = "is open" if pr["state"] == "OPEN" else "was opened (state could not be checked)"
            items.append('A pull request from the agent %s for review: <a href="%s">%s</a>%s'
                         % (label, e(pr["url"], quote=True), e(pr["url"].replace("https://github.com/", "")),
                            (" &ndash; " + e(pr["title"])) if pr["title"] else ""))
    new = [i for i in d["outbox"]["items"] if not d["last"] or i["t"] > d["last"]]
    if d["outbox"]["count"]:
        if new:
            items.append("%d new file%s in the outbox since you last looked: %s"
                         % (len(new), "" if len(new) == 1 else "s",
                            ", ".join("<code>%s</code>" % e(i["name"]) for i in new[:5])))
        else:
            items.append("%d file%s in the outbox, none new since you last looked."
                         % (d["outbox"]["count"], "" if d["outbox"]["count"] == 1 else "s"))
    if d["allow"]:
        items.append("%d website%s on the guest list. Close what is no longer needed: %s"
                     % (len(d["allow"]), " is" if len(d["allow"]) == 1 else "s are",
                        ", ".join("<code>%s</code>" % e(a) for a in d["allow"])))
    if not items:
        return ('<section class="waiting clear"><h2>Nothing is waiting for you</h2>'
                '<p style="margin:4px 0 0">No open pull requests, nothing new in the outbox, all walls hold.</p></section>')
    return ('<section class="waiting"><h2>Waiting for you</h2><ul>%s</ul></section>'
            % "".join("<li>%s</li>" % i for i in items))


def render_outbox(d):
    o = d["outbox"]
    if not o["count"]:
        return '<p class="empty">The outbox is empty.</p>'
    h = ["<table><tr><th>File</th><th>Size</th><th>Written</th><th></th></tr>"]
    for i in o["items"]:
        flags = ""
        if i["link"]:
            flags += '<span class="tag no">link, do not follow</span> '
        if i["exec"]:
            flags += '<span class="tag warn">program, read before running</span> '
        if d["last"] and i["t"] > d["last"]:
            flags += '<span class="tag ok">new</span>'
        h.append('<tr><td><code>outbox/%s</code></td><td class="n">%s</td><td class="n">%s</td><td>%s</td></tr>'
                 % (e(i["name"]), size(i["size"]), e(local(i["t"], "%d %b, %H:%M")), flags))
    if o["count"] > len(o["items"]):
        h.append('<tr><td colspan="4">and %d older files</td></tr>' % (o["count"] - len(o["items"])))
    h.append("</table>")
    return "".join(h)


def render_budget(d):
    b = d["budget"]
    if b["state"] == "nokey":
        return '<div class="card">No API key is set. Add one to <code>.env</code> (see README, step 1).</div>'
    if b["state"] != "ok" or not isinstance(b.get("limit"), (int, float)) or not b["limit"]:
        return ('<div class="card">The key\'s limit could not be read just now (offline, or the key has no limit). '
                'Check it at openrouter.ai under Keys.</div>')
    used = b.get("used") or 0
    pct = max(0, min(100, 100 * used / b["limit"]))
    extra = []
    if b.get("reset"):
        extra.append("resets %s%s" % (e(str(b["reset"])), (", next on " + e(local(b["resets_at"], "%d %b"))) if b.get("resets_at") else ""))
    if b.get("expires"):
        extra.append("key expires " + e(local(b["expires"], "%d %b %Y")))
    return ('<div class="card"><b>%s of %s used</b><div class="meter"><i class="%s" style="width:%.1f%%"></i></div>'
            '<span style="color:var(--soft);font-size:.92rem">%s</span></div>'
            % (money(used), money(b["limit"]), "high" if pct >= 80 else "", pct,
               e("The limit lives on the key at OpenRouter. Nothing inside the box can raise it.")
               + (" " + "; ".join(extra).capitalize() + "." if extra else "")))


def build():
    cfg = collect_config()
    running = collect_running()
    calls, reqs = collect_logs()
    repos, prs = collect_repos()
    last = parse_ts(read(STATE / "last-report").strip())
    test = collect_test()
    walls_changed = False
    if test and test["when"]:
        for f in ("compose.yaml", "gateway/default.conf.template", "egress/squid.conf", "agent/Dockerfile"):
            try:
                if dt.datetime.fromtimestamp((ROOT / f).stat().st_mtime, dt.timezone.utc) > test["when"]:
                    walls_changed = True
            except OSError:
                pass
    d = {
        "cfg": cfg, "running": running, "calls": calls, "reqs": reqs, "repos": repos, "prs": prs,
        "runs": collect_runs(), "allow": collect_allowlist(), "test": test, "walls_changed": walls_changed,
        "budget": collect_budget(), "inbox": collect_folder("inbox"), "outbox": collect_folder("outbox"),
        "workspace": read(STATE / "workspace").strip() or "default",
        "model": cfg.get("model") or env_value("BOX_MODEL") or "not set", "last": last,
    }
    up = running.get("agent") == "running"
    all_calls = [c["t"] for c in calls]
    last_active = max(all_calls) if all_calls else None

    plan = render_plan(d)
    can = render_can(d)
    cannot = render_cannot(d)
    t = d["test"]
    if t:
        tested = "%d of %d escape attempts stopped, tested %s" % (
            sum(c["ok"] for g in t["groups"] for c in g["checks"]), t["passed"] + t["failed"], ago(t["when"]))
    else:
        tested = "not tested yet"
    periods = (render_period(d, 7, "7", "7 days", True) + render_period(d, 30, "30", "30 days", False)
               + render_period(d, 0, "all", "All", False))

    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-box status</title><style>%(css)s</style></head><body><main>
<h1>What the agent can and cannot do</h1>
<p class="lead">A live picture of your agent-box, read from how it is actually configured and what its guards
have logged. Click anything for a plain explanation.</p>
<ul class="facts">
<li class="%(state_cls)s">%(state)s</li>
<li>Project <b>%(ws)s</b></li>
<li>Model <b>%(model)s</b></li>
<li>Agent last active <b>%(active)s</b></li>
<li>You last looked <b>%(last)s</b></li>
</ul>
%(waiting)s
<section><h2>The floor plan</h2>
<div class="plan">%(plan)s</div>
<p class="hint">Green lines are open paths. Dashed grey means closed or not there. Every shape is clickable.</p>
</section>
<section class="cols">
<div><h2>It can</h2><p class="lead" style="margin:0">Everything you have handed in or opened.</p>%(can)s</div>
<div><h2>It cannot</h2><p class="lead" style="margin:0">%(tested)s.</p>%(cannot)s</div>
</section>
<section><h2>What it did</h2>
<p class="lead" style="margin:0">From the guards' logbooks, which the agent cannot see or change.</p>
<div class="seg" role="group" aria-label="Period">
<button data-period="7" aria-pressed="true">Last 7 days</button>
<button data-period="30" aria-pressed="false">Last 30 days</button>
<button data-period="all" aria-pressed="false">Everything</button></div>
%(periods)s
</section>
<section><h2>In the outbox</h2>
<p class="lead" style="margin:0">Written by the agent. Reading is safe; running is a decision.</p>%(outbox)s</section>
<section><h2>Budget</h2>%(budget)s</section>
<footer>Built %(now)s with <code>bin/box report</code>. This page is a snapshot: run the command again to refresh it.
Self-test attempts are left out of the activity figures.</footer>
</main>%(pops)s<script>%(js)s</script></body></html>
""" % {
        "css": CSS, "js": JS, "pops": "".join(POPS), "plan": plan, "can": can, "cannot": cannot,
        "tested": e(tested[0].upper() + tested[1:]), "periods": periods, "waiting": render_waiting(d),
        "outbox": render_outbox(d), "budget": render_budget(d),
        "state": "Box is running" if up else "Box is stopped", "state_cls": "on" if up else "off",
        "ws": e(d["workspace"]), "model": e(d["model"].replace("openrouter/", "")),
        "active": e(ago(last_active)), "last": e(ago(last) if last else "first time"),
        "now": e(local(NOW)),
    }
    OUT.write_text(page)
    STATE.mkdir(exist_ok=True)
    (STATE / "last-report").write_text(NOW.strftime("%Y-%m-%dT%H:%M:%S"))
    return OUT


if __name__ == "__main__":
    out = build()
    print("Written: %s" % out)
    if "--no-open" not in sys.argv:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, str(out)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

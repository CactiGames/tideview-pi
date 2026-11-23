#!/usr/bin/env python3
"""TideView's updater: keeps a Pi on the newest TideView, safely.

It runs as root from systemd units that it writes itself (see UNITS below):

  tideview.service       before every start of TideView:   tvupdate.py boot
  tideview-update.timer  4 minutes after power-on, then every 15 minutes:
                         tvupdate.py check

What happens in an update:
  1. check fetches latest.json from the update address (HTTPS, on GitHub by
     default). It names the newest version, where its zip is, and the zip's
     size and SHA-256.
  2. It downloads the zip, checks the size and SHA-256, unpacks it safely
     into /opt/tideview/releases/<version> and test-runs it without a
     screen. A version that fails any of this is never tried again.
  3. At a quiet time it points /opt/tideview/current at the new version in
     one step and restarts TideView. Quiet times are 2 to 5 AM, the first 15
     minutes after power-on, and straight away when someone taps "Check for
     updates now" in Settings.
  4. The new version proves itself by running for 3 minutes: it then writes
     running-ok in the state folder. If it stops 3 times before that, or
     hasn't done it within 15 minutes, current goes back to the previous
     version and the new one is marked bad.

Settings, keys and the forecast cache are kept in /var/lib/tideview, outside
the versions, so updates never touch them. The updater replaces itself only
with the copy in a version that has proved itself, after a self-test.

Commands (all but fetch and selftest need root):
  boot            before TideView starts: repair, count tries, go back
  check [--now]   look for an update; --now installs it straight away
  status          show the versions and the last check
  install DIR     install the TideView in DIR (install.sh runs this)
  units           rewrite the systemd units if they've changed
  fetch --to DIR  download and check the newest TideView into DIR (get.sh)
  selftest        check this file works

Settings in /etc/tideview/update.conf (all optional):
  UPDATE_URL=https://.../latest.json
  AUTO_UPDATE=no       only install when asked from Settings
  QUIET_HOURS=2-5      when automatic updates may install
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

UPDATER_VERSION = 1
DEFAULT_URL = "https://raw.githubusercontent.com/CactiGames/tideview-pi/main/latest.json"

CHECK_EVERY = 60 * 60  # seconds between looks at latest.json
BOOT_WINDOW = 15 * 60  # updates may install this soon after power-on
GIVE_UP_AFTER = 15 * 60  # a new version must prove itself within this...
MAX_TRIES = 3  # ...and within this many starts
MAX_ZIP = 30 << 20
MAX_UNPACKED = 100 << 20
MAX_FILES = 3000
KEEP_BAD = 20
VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}\.\d{1,3}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
KEY_FILES = (".env.txt", ".env", "keys.txt", "keys.txt.txt")
KEY_NAMES = ("NIWA_API_KEY", "METOCEAN_API_KEY")
SKIP_NAMES = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini", "System Volume Information"} | set(KEY_FILES)
REQUIRED = ("tideview.py", "tvupdate.py", "install.sh", "VERSION", os.path.join("tv", "app.py"))

DRY_RUN = False  # the self-test leaves systemd alone


class UpdateError(Exception):
    """Something that may work next time: no internet, a cut-off download."""


class BadRelease(Exception):
    """The version itself is broken, so it's never tried again."""


# ---------------------------------------------------------------- places

def _state_dir():
    return (os.environ.get("STATE_DIRECTORY", "").split(":")[0] or os.environ.get("TIDEVIEW_STATE")
            or "/var/lib/tideview")


class Paths:
    def __init__(self, root, state, units, boot_dirs, conf):
        self.root = root
        self.releases = os.path.join(root, "releases")
        self.downloads = os.path.join(root, ".download")
        self.updater = os.path.join(root, "tvupdate.py")
        self.state = state
        self.state_file = os.path.join(state, "update.json")
        self.ok_marker = os.path.join(state, "running-ok")  # written by TideView after 3 minutes
        self.request = os.path.join(state, "update-request")  # written by "Check for updates now"
        self.keys = os.path.join(state, "keys.env")
        self.units = units
        self.boot_dirs = boot_dirs
        self.conf = conf


def default_paths():
    boot = os.environ.get("TIDEVIEW_BOOT")
    return Paths(os.environ.get("TIDEVIEW_ROOT", "/opt/tideview"), _state_dir(),
                 os.environ.get("TIDEVIEW_UNIT_DIR", "/etc/systemd/system"),
                 [boot] if boot else ["/boot/firmware", "/boot"],
                 os.environ.get("TIDEVIEW_UPDATE_CONF", "/etc/tideview/update.conf"))


P = default_paths()


# ---------------------------------------------------------------- small helpers

def log(msg):
    print(msg, flush=True)


def now():
    fake = os.environ.get("TIDEVIEW_FAKE_NOW")  # tests
    return float(fake) if fake else time.time()


def uptime():
    fake = os.environ.get("TIDEVIEW_FAKE_UPTIME")
    if fake:
        return float(fake)
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def boot_id():
    fake = os.environ.get("TIDEVIEW_FAKE_BOOT_ID")
    if fake:
        return fake
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def valid_version(v):
    return isinstance(v, str) and bool(VERSION_RE.match(v))


def vkey(v):
    return tuple(int(x) for x in v.split(".")) if valid_version(v) else (-1,)


def expect(ok, what):
    if not ok:
        raise AssertionError(what)


def fsync_dir(path):
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_file(path, data, mode=0o644):
    """Writes the whole file or nothing, even if the power goes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    fsync_dir(os.path.dirname(path) or ".")


def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def read_text(path):
    data = read_bytes(path)
    return None if data is None else data.decode("utf-8", "replace")


def read_env(path):
    """KEY=VALUE lines. Copes with Notepad (BOM, CRLF), quotes and 'export'."""
    out = {}
    for line in (read_text(path) or "").lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.lower().startswith("export "):
            k = k[7:].strip()
        out[k] = v.strip().strip('"').strip("'").strip()
    return out


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True


def systemctl(*args):
    if DRY_RUN:
        log("(would run: systemctl %s)" % " ".join(args))
        return True
    try:
        return subprocess.run(["systemctl"] + list(args), timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log("systemctl %s didn't work: %s" % (" ".join(args), e))
        return False


def restart_app():
    systemctl("restart", "--no-block", "tideview.service")


def config():
    cfg = {"url": DEFAULT_URL, "auto": True, "quiet": (2, 5)}
    for key, value in read_env(P.conf).items():
        key = key.upper()
        if key == "UPDATE_URL" and value:
            cfg["url"] = value
        elif key == "AUTO_UPDATE":
            cfg["auto"] = value.lower() not in ("no", "off", "false", "0")
        elif key == "QUIET_HOURS":
            m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})$", value)
            if m and int(m.group(1)) < 24 and int(m.group(2)) <= 24:
                cfg["quiet"] = (int(m.group(1)), int(m.group(2)))
    cfg["url"] = os.environ.get("TIDEVIEW_UPDATE_URL") or cfg["url"]
    return cfg


# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(P.state_file, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    if not isinstance(st.get("bad"), list):
        st["bad"] = []
    return st


@contextlib.contextmanager
def state_lock():
    import fcntl
    os.makedirs(P.state, exist_ok=True)
    fd = os.open(os.path.join(P.state, "update.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def change_state(fn):
    """Runs fn(state) under the lock, and saves the state if it changed."""
    with state_lock():
        st = load_state()
        before = json.dumps(st, sort_keys=True)
        result = fn(st)
        if json.dumps(st, sort_keys=True) != before:
            write_file(P.state_file, json.dumps(st, indent=1, sort_keys=True) + "\n")
    return result


def set_event(st, kind, **info):
    """The last thing that happened, for TideView to show once."""
    e = {"id": int(now() * 1000), "kind": kind, "at": int(now())}
    e.update(info)
    st["event"] = e


def set_busy(text):
    def run(st):
        st["busy"] = {"text": text, "pid": os.getpid(), "since": int(now())}
    change_state(run)


def clear_busy():
    def run(st):
        b = st.get("busy")
        if isinstance(b, dict) and b.get("pid") == os.getpid():
            st.pop("busy", None)
    change_state(run)


# ---------------------------------------------------------------- versions on disk

def link(name):
    """The version that the current or previous link points at."""
    try:
        target = os.readlink(os.path.join(P.root, name))
    except OSError:
        return None
    v = os.path.basename(os.path.normpath(target))
    return v if valid_version(v) else None


def release_dir(v):
    return os.path.join(P.releases, v)


def usable(v):
    return valid_version(v) and os.path.isfile(os.path.join(release_dir(v), "tideview.py"))


def set_link(name, v):
    """Points current or previous at a version, in one step."""
    path = os.path.join(P.root, name)
    tmp = os.path.join(P.root, ".%s.new" % name)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(os.path.join("releases", v), tmp)
    os.replace(tmp, path)
    fsync_dir(P.root)


def drop_link(name):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(P.root, name))
    fsync_dir(P.root)


def installed_versions():
    try:
        names = os.listdir(P.releases)
    except OSError:
        return []
    return sorted((n for n in names if usable(n)), key=vkey)


def ok_version():
    return (read_text(P.ok_marker) or "").strip()


def confirm(st, v):
    log("Version %s works, so it stays." % v)
    st["pending"] = None
    st["tries"] = 0
    st["good"] = v


def rollback(st, why):
    """Goes back to the previous version and marks this one bad. Returns
    True if TideView needs restarting."""
    bad = st.get("pending") or link("current")
    prev = link("previous")
    st["pending"] = None
    st["tries"] = 0
    if valid_version(bad) and bad not in st["bad"]:
        st["bad"] = (st["bad"] + [bad])[-KEEP_BAD:]
    if not usable(prev) or prev == bad:
        log("Version %s isn't working (%s), and there's no earlier version to go back to." % (bad, why))
        set_event(st, "stuck", version=bad, why=why)
        return False
    log("Version %s isn't working (%s), so going back to %s." % (bad, why, prev))
    set_link("current", prev)
    drop_link("previous")
    set_event(st, "rollback", why=why, **{"from": bad, "to": prev})
    return True


def repair(st):
    """Makes sure current points at a version that's really there."""
    cur = link("current")
    if usable(cur):
        return False
    options = [v for v in [link("previous")] + installed_versions()[::-1] if usable(v)]
    good = [v for v in options if v not in st["bad"]]
    pick = (good or options or [None])[0]
    if not pick:
        log("No TideView version found in %s. Run install.sh again." % P.releases)
        return False
    log("The current version (%s) is missing, so using %s." % (cur or "none", pick))
    set_link("current", pick)
    if link("previous") == pick:
        drop_link("previous")
    set_event(st, "repair", **{"from": cur, "to": pick})
    return True


def boot_state(st):
    """Before each start of TideView."""
    busy = st.get("busy")
    if isinstance(busy, dict) and not pid_alive(busy.get("pid")):
        st.pop("busy", None)  # a check cut short, by a power cut say
    repair(st)
    p = st.get("pending")
    if not p:
        return
    if link("current") != p:
        log("Version %s is no longer the current one." % p)
        st["pending"] = None
        return
    if ok_version() == p:
        confirm(st, p)
        return
    if st.get("pending_boot") != boot_id():
        # The Pi has restarted since the switch: its 15 minutes start now.
        st["pending_boot"] = boot_id()
        st["pending_uptime"] = uptime()
    st["tries"] = int(st.get("tries") or 0) + 1
    if st["tries"] > MAX_TRIES:
        rollback(st, "it kept stopping")
    else:
        log("Starting version %s (try %d of %d)." % (p, st["tries"], MAX_TRIES))


def health(st):
    """For check: is a newly installed version working? Returns True when
    TideView needs restarting because it went back to the previous one."""
    p = st.get("pending")
    if not p:
        return False
    if link("current") != p:
        st["pending"] = None
        return False
    if ok_version() == p:
        confirm(st, p)
        return False
    if st.get("pending_boot") != boot_id():
        st["pending_boot"] = boot_id()
        st["pending_uptime"] = uptime()
        return False
    # Measured with the Pi's uptime, so a clock correction can't trigger it.
    if uptime() - float(st.get("pending_uptime") or 0) > GIVE_UP_AFTER:
        return rollback(st, "it didn't start properly")
    return False


def apply(v, notes, why):
    """Switches to an unpacked version and restarts TideView."""
    def run(st):
        cur = link("current")
        if cur == v or not usable(v):
            return False
        with contextlib.suppress(FileNotFoundError):
            os.unlink(P.ok_marker)
        if usable(cur):
            set_link("previous", cur)
        set_link("current", v)
        st.update(pending=v, tries=0, pending_boot=boot_id(), pending_uptime=uptime(), staged=None)
        set_event(st, "update", notes=notes, why=why, **{"from": cur, "to": v})
        log("Switched from version %s to %s (%s). Restarting TideView." % (cur, v, why))
        return True
    if change_state(run):
        restart_app()
        return True
    return False


def tidy(st):
    """Removes versions that aren't current, previous or on the way."""
    keep = {link("current"), link("previous"), st.get("pending"), st.get("staged")}
    try:
        names = os.listdir(P.releases)
    except OSError:
        names = []
    for name in names:
        if name in keep:
            continue
        path = os.path.join(P.releases, name)
        if valid_version(name):
            log("Removing old version %s." % name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.unlink(path)
    shutil.rmtree(P.downloads, ignore_errors=True)


# ---------------------------------------------------------------- downloading

def _open(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "TideView-Pi-updater/%d" % UPDATER_VERSION,
                                               "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=30)


def same_origin(a, b):
    from urllib.parse import urlsplit
    x, y = urlsplit(a), urlsplit(b)
    return x.scheme in ("https", "http") and (x.scheme, x.netloc.lower()) == (y.scheme, y.netloc.lower())


def net_error(e):
    import socket
    import ssl
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return "the update server said %d %s" % (e.code, e.reason)
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return "no internet connection"
    if isinstance(reason, TimeoutError):
        return "the update server didn't answer"
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper():
        return "the secure connection failed (is the Pi's clock right?)"
    return "couldn't reach the update server (%s)" % str(reason)[:100]


def fetch_manifest(url):
    import http.client
    import urllib.error
    try:
        with _open(url) as r:
            if not same_origin(r.geturl(), url):
                raise UpdateError("the update address sent TideView to a different server")
            body = r.read(65537)
    except UpdateError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise UpdateError(net_error(e)) from None
    if len(body) > 65536:
        raise UpdateError("the update list is too big")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("the update list is damaged") from None
    return parse_manifest(data, url)


def parse_manifest(data, base):
    from urllib.parse import urljoin
    if not isinstance(data, dict):
        raise UpdateError("the update list is damaged")
    if data.get("format", 1) != 1:
        raise UpdateError("the update list is in a newer format, so install TideView again to keep updating")
    v = data.get("version")
    if not valid_version(v):
        raise UpdateError("the update list has a bad version number")
    url = urljoin(base, str(data.get("url") or ""))
    if not same_origin(url, base) or url.split("?")[0] == base.split("?")[0]:
        raise UpdateError("the update points to a different server")
    sha = str(data.get("sha256") or "").lower()
    size = data.get("size")
    if (not SHA_RE.match(sha) or not isinstance(size, int) or isinstance(size, bool)
            or not 0 < size <= MAX_ZIP):
        raise UpdateError("the update list is damaged")
    notes = str(data.get("notes") or "").strip()[:400]
    return {"version": v, "url": url, "sha256": sha, "size": size, "notes": notes}


def download(m, dest):
    """Downloads the zip and checks its size and SHA-256."""
    import hashlib
    import http.client
    import urllib.error
    h = hashlib.sha256()
    n = 0
    try:
        with _open(m["url"]) as r:
            if not same_origin(r.geturl(), m["url"]):
                raise UpdateError("the download was sent to a different server")
            with open(dest, "wb") as f:
                while True:
                    try:
                        chunk = r.read(65536)
                    except (OSError, http.client.HTTPException) as e:
                        raise UpdateError("the download was cut off (%s)" % net_error(e)) from None
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > m["size"]:
                        raise UpdateError("the download is bigger than it should be")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
    except UpdateError:
        raise
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError) as e:
        raise UpdateError(net_error(e)) from None
    except OSError as e:
        raise UpdateError("couldn't save the download: %s" % (e.strerror or e)) from None
    if n != m["size"]:
        raise UpdateError("the download was cut off")
    if h.hexdigest() != m["sha256"]:
        raise UpdateError("the download didn't match its checksum")


def unpack(zip_path, dest):
    """Unpacks a release zip into dest. Refuses anything that could land
    outside dest: absolute paths, '..', links and special files."""
    import stat
    import zipfile
    import zlib
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise BadRelease("it isn't a proper zip file") from None
    real_dest = os.path.realpath(dest)
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise BadRelease("it has too many files")
        plan = []
        for info in infos:
            name = info.filename
            parts = name.split("/")
            if (not name or name.startswith("/") or "\\" in name or "\0" in name or ".." in parts
                    or ":" in parts[0]):
                raise BadRelease("it has an unsafe file name (%r)" % name[:80])
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise BadRelease("it has a link or a special file (%r)" % name[:80])
            parts = [p for p in parts if p not in ("", ".")]
            if parts:
                plan.append((info, parts))
        total = 0
        try:
            for info, parts in plan:
                target = os.path.join(real_dest, *parts)
                if not os.path.realpath(target).startswith(real_dest + os.sep):
                    raise BadRelease("it has an unsafe file name (%r)" % info.filename[:80])
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_UNPACKED:
                            raise BadRelease("it unpacks to more than %d MB" % (MAX_UNPACKED >> 20))
                        out.write(chunk)
        except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError) as e:
            raise BadRelease("its zip file is damaged (%s)" % e) from None
        except OSError as e:
            raise UpdateError("couldn't unpack it: %s" % (e.strerror or e)) from None


def check_release(d, v):
    for need in REQUIRED:
        if not os.path.isfile(os.path.join(d, need)):
            raise BadRelease("it's missing %s" % need)
    got = (read_text(os.path.join(d, "VERSION")) or "").strip()
    if got != v:
        raise BadRelease("it calls itself %s, not %s" % (got or "nothing", v))


def selftest_release(d):
    """Test-runs a new version without a screen: its updater's self-test,
    then TideView drawing each of its screens once."""
    import tempfile
    os.makedirs(P.downloads, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="selftest-", dir=P.downloads)
    try:
        for name in ("settings.json", "forecast-cache.json"):
            src = os.path.join(P.state, name)
            if os.path.isfile(src):
                shutil.copy(src, tmp)
        env = dict(os.environ, TIDEVIEW_STATE=tmp, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy",
                   PYGAME_HIDE_SUPPORT_PROMPT="1")
        env.pop("STATE_DIRECTORY", None)
        for what, cmd in (("updater", [sys.executable, os.path.join(d, "tvupdate.py"), "selftest"]),
                          ("display", [sys.executable, os.path.join(d, "tideview.py"), "--selftest"])):
            try:
                r = subprocess.run(cmd, cwd=d, env=env, timeout=900, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
            except subprocess.TimeoutExpired:
                raise BadRelease("its %s test took too long" % what) from None
            except OSError as e:
                raise UpdateError("couldn't test it: %s" % e) from None
            if r.returncode != 0:
                out = r.stdout.decode("utf-8", "replace").strip()
                log(out[-3000:])
                last = out.splitlines()[-1] if out else "exit code %d" % r.returncode
                raise BadRelease("its %s test failed: %s" % (what, last[:160]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def stage(m):
    """Downloads, checks, unpacks and test-runs a version, ready to switch to."""
    v = m["version"]
    if usable(v):
        return
    os.makedirs(P.downloads, exist_ok=True)
    os.makedirs(P.releases, exist_ok=True)
    zpath = os.path.join(P.downloads, "tideview-%s.zip" % v)
    tmp = os.path.join(P.releases, ".tmp-%s" % v)
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        set_busy("Downloading version %s..." % v)
        download(m, zpath)
        set_busy("Testing version %s..." % v)
        os.makedirs(tmp)
        unpack(zpath, tmp)
        check_release(tmp, v)
        selftest_release(tmp)
        shutil.rmtree(release_dir(v), ignore_errors=True)
        os.rename(tmp, release_dir(v))
        fsync_dir(P.releases)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.unlink(zpath)


# ---------------------------------------------------------------- the updater and systemd

def refresh_updater():
    """Takes the updater from the current version once it has proved
    itself (it's only called when nothing is pending)."""
    cur = link("current")
    if not usable(cur):
        return False
    src = os.path.join(release_dir(cur), "tvupdate.py")
    new, old = read_bytes(src), read_bytes(P.updater)
    if not new or new == old:
        return False
    try:
        ok = subprocess.run([sys.executable, src, "selftest"], timeout=600, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        log("The updater in version %s failed its self-test, so the old one stays." % cur)
        return False
    if old:
        write_file(P.updater + ".bak", old, 0o755)
    write_file(P.updater, new, 0o755)
    log("Took the updater from version %s." % cur)
    return True


UNIT_NOTE = """\
# Written by {up}, which puts it back when TideView updates.
# To change something, use: sudo systemctl edit {name}
"""

UNITS = {
    "tideview.service": """\
[Unit]
Description=TideView surf display
# TideView takes over the screen from the text console's login prompt.
Conflicts=getty@tty1.service
After=getty@tty1.service systemd-user-sessions.service
StartLimitIntervalSec=0

[Service]
# Before each start, finish or undo an update. A problem here never stops TideView.
ExecStartPre=-/bin/sh -c '{py} {up} boot || {py} {up}.bak boot'
ExecStart={py} {app}
Restart=always
RestartSec=3
StateDirectory=tideview
CacheDirectory=tideview
Environment=PYTHONPYCACHEPREFIX=/var/cache/tideview/pycache
Environment=PYTHONUNBUFFERED=1
Environment=PYGAME_HIDE_SUPPORT_PROMPT=1

[Install]
WantedBy=multi-user.target
""",
    "tideview-update.service": """\
[Unit]
Description=TideView update check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '{py} {up} check || {py} {up}.bak check'
StateDirectory=tideview
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=30min
""",
    "tideview-update.timer": """\
[Unit]
Description=Check for TideView updates

[Timer]
OnBootSec=4min
OnUnitActiveSec=15min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
""",
}


def unit_texts():
    values = {"py": "/usr/bin/python3", "up": P.updater, "app": os.path.join(P.root, "current", "tideview.py")}
    return {name: UNIT_NOTE.format(name=name.split(".")[0], **values) + text.format(**values)
            for name, text in UNITS.items()}


def refresh_units(enable=False):
    changed = []
    os.makedirs(P.units, exist_ok=True)
    for name, text in unit_texts().items():
        path = os.path.join(P.units, name)
        if read_text(path) != text:
            write_file(path, text)
            changed.append(name)
    if changed:
        log("Updated %s." % ", ".join(changed))
        systemctl("daemon-reload")
    if enable:
        systemctl("enable", "tideview.service")
        systemctl("enable", "--now", "tideview-update.timer")
    elif "tideview-update.timer" in changed:
        systemctl("try-restart", "tideview-update.timer")
    return changed


# ---------------------------------------------------------------- commands

def cmd_boot(_args):
    change_state(boot_state)
    return 0


def take_request():
    try:
        os.unlink(P.request)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log("Couldn't read the update request: %s" % e)
        return False


def cmd_check(args):
    import fcntl
    os.makedirs(P.state, exist_ok=True)
    fd = os.open(os.path.join(P.state, "check.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("Another update check is running.")
            return 0
        forced = args.now
        for _ in range(3):
            forced = take_request() or forced
            check_once(forced)
            if not os.path.exists(P.request):  # tapped again while checking
                break
            forced = False
    finally:
        clear_busy()
        os.close(fd)
    return 0


def check_once(forced):
    cfg = config()
    if change_state(health):
        restart_app()
    st = load_state()
    if st.get("pending"):
        log("Version %s is still proving itself, so no other update for now." % st["pending"])
        return
    refresh_updater()
    refresh_units()
    cur = link("current")
    staged = st.get("staged")
    if staged and (not usable(staged) or vkey(staged) <= vkey(cur) or staged in st["bad"]):
        change_state(lambda s: s.update(staged=None))
        staged = st["staged"] = None
    tidy(st)

    t = now()
    last = float(st.get("last_try") or 0)
    due = forced or t - last >= CHECK_EVERY or t < last or st.get("last_try_boot") != boot_id()
    if not due:
        if staged and cfg["auto"] and in_quiet_time(cfg):
            apply(staged, st.get("staged_notes") or "", "quiet time")
        return

    set_busy("Checking for updates...")
    url = cfg["url"]
    if forced:  # ask the server's cache for a fresh copy
        url += ("&" if "?" in url else "?") + "t=%d" % t
    common = {"last_try": int(t), "last_try_boot": boot_id(), "auto": cfg["auto"], "quiet": list(cfg["quiet"])}
    try:
        m = fetch_manifest(url)
    except UpdateError as e:
        err = str(e)
        log("Couldn't check for updates: %s." % err)
        change_state(lambda s: s.update(common, last_error=err, last_error_at=int(now())))
        return
    v = m["version"]
    change_state(lambda s: s.update(common, last_check=int(now()), last_error=None, latest=v,
                                    latest_notes=m["notes"]))
    if v in st["bad"]:
        log("Version %s didn't work on this Pi before, so it's skipped." % v)
        return
    if vkey(v) <= vkey(cur):
        log("TideView is up to date (version %s)." % cur)
        return
    log("Version %s is available (this Pi has %s)." % (v, cur))
    try:
        stage(m)
    except BadRelease as e:
        err = "version %s can't be used: %s" % (v, e)
        log(err[0].upper() + err[1:] + ".")

        def mark(s):
            if v not in s["bad"]:
                s["bad"] = (s["bad"] + [v])[-KEEP_BAD:]
            s.update(last_error=err, last_error_at=int(now()))
        change_state(mark)
        return
    except (UpdateError, OSError) as e:
        msg = str(e) if isinstance(e, UpdateError) else "couldn't save version %s: %s" % (v, e.strerror or e)
        log("Couldn't get version %s: %s." % (v, msg))
        change_state(lambda s: s.update(last_error=msg, last_error_at=int(now())))
        return
    change_state(lambda s: s.update(staged=v, staged_notes=m["notes"]))
    if forced or (cfg["auto"] and in_quiet_time(cfg)):
        apply(v, m["notes"], "asked for" if forced else "quiet time")
    else:
        log("Version %s is ready and installs at the next quiet time." % v)


def in_quiet_time(cfg):
    if uptime() < BOOT_WINDOW:
        return True
    start, end = cfg["quiet"]
    hour = time.localtime(now()).tm_hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def cmd_status(_args):
    st = load_state()
    cfg = config()
    print("Current version:  %s" % (link("current") or "none"))
    print("Previous version: %s" % (link("previous") or "none"))
    print("Update address:   %s" % cfg["url"])
    print("Installs updates: %s" % ("between %d:00 and %d:00, or when asked" % cfg["quiet"] if cfg["auto"]
                                    else "only when asked"))
    print(json.dumps(st, indent=1, sort_keys=True))
    return 0


def _copy_app(src, dest):
    """Copies a TideView folder, leaving out keys and caches."""
    os.makedirs(dest)
    for name in sorted(os.listdir(src)):
        if name in SKIP_NAMES or name.startswith("."):
            continue
        s, d = os.path.join(src, name), os.path.join(dest, name)
        if os.path.isdir(s) and not os.path.islink(s):
            _copy_app(s, d)
        elif os.path.isfile(s):
            shutil.copyfile(s, d)
            os.chmod(d, 0o644)


def copy_keys(src):
    """Copies the two API keys into the state folder, readable by root only,
    so they outlast updates. Returns the file they came from, or None."""
    candidates = [os.path.join(src, n) for n in KEY_FILES]
    for b in P.boot_dirs:
        candidates += [os.path.join(b, "tideview", n) for n in KEY_FILES] + [os.path.join(b, ".env.txt")]
    for path in candidates:
        found = read_env(path)
        if any(found.get(k) for k in KEY_NAMES):
            text = "".join("%s=%s\n" % (k, found[k]) for k in KEY_NAMES if found.get(k))
            os.makedirs(P.state, exist_ok=True)
            write_file(P.keys, "# Copied from %s by install.sh\n%s" % (path, text), 0o600)
            return path
    return None


def cmd_install(args):
    src = os.path.abspath(args.dir)
    v = (read_text(os.path.join(src, "VERSION")) or "").strip()
    if not valid_version(v):
        log("%s has no proper VERSION file. Copy the whole tideview folder again." % src)
        return 1
    missing = [n for n in REQUIRED if not os.path.isfile(os.path.join(src, n))]
    if missing:
        log("%s is missing %s. Copy the whole tideview folder again." % (src, ", ".join(missing)))
        return 1
    os.makedirs(P.releases, exist_ok=True)
    cur = link("current")
    if usable(cur) and vkey(cur) > vkey(v) and not args.force:
        log("This Pi already has a newer TideView (version %s), so it keeps that." % cur)
        target = cur
    else:
        tmp, old = os.path.join(P.releases, ".tmp-%s" % v), os.path.join(P.releases, ".old-%s" % v)
        for p in (tmp, old):
            shutil.rmtree(p, ignore_errors=True)
        _copy_app(src, tmp)
        if os.path.exists(release_dir(v)):
            os.rename(release_dir(v), old)
        os.rename(tmp, release_dir(v))
        shutil.rmtree(old, ignore_errors=True)
        if usable(cur) and cur != v:
            set_link("previous", cur)
        set_link("current", v)
        target = v
        log("Installed TideView version %s in %s." % (v, P.root))
    write_file(P.updater, read_bytes(os.path.join(release_dir(target), "tvupdate.py")), 0o755)
    keys = copy_keys(src)
    log("Keys copied from %s." % keys if keys else "No keys file found, so TideView will use the free forecasts.")
    refresh_units(enable=True)

    def record(st):
        st.update(pending=None, tries=0, good=target)
        if target in st["bad"]:
            st["bad"].remove(target)
        if st.get("staged") and vkey(st["staged"]) <= vkey(target):
            st["staged"] = None
        set_event(st, "install", to=target)
    change_state(record)
    return 0


def cmd_units(_args):
    changed = refresh_units()
    log("Units %s." % ("updated: " + ", ".join(changed) if changed else "already up to date"))
    return 0


def cmd_fetch(args):
    url = args.url or config()["url"]
    dest = os.path.abspath(args.to)
    try:
        m = fetch_manifest(url)
        log("Downloading TideView version %s..." % m["version"])
        os.makedirs(dest, exist_ok=True)
        if os.listdir(dest):
            raise UpdateError("%s isn't empty" % dest)
        zpath = dest.rstrip("/") + ".zip"
        try:
            download(m, zpath)
            unpack(zpath, dest)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(zpath)
        check_release(dest, m["version"])
    except (UpdateError, BadRelease) as e:
        log("Couldn't download TideView: %s." % e)
        return 1
    log("Downloaded and checked TideView version %s." % m["version"])
    return 0


def cmd_selftest(_args):
    """Checks this updater works, in a temporary folder."""
    import tempfile
    import zipfile
    global P, DRY_RUN
    saved = P, DRY_RUN
    base = tempfile.mkdtemp(prefix="tvupdate-test-")
    try:
        DRY_RUN = True
        P = Paths(os.path.join(base, "root"), os.path.join(base, "state"), os.path.join(base, "units"),
                  [os.path.join(base, "boot")], os.path.join(base, "update.conf"))
        os.makedirs(P.releases)
        os.makedirs(P.units)

        # Version numbers and the update list.
        expect(vkey("2026.11.23.10") > vkey("2026.11.23.9") > vkey("2026.2.28.1") > vkey("junk"), "order")
        expect(valid_version("2026.1.1.1") and not valid_version("../2026.1.1.1"), "version check")
        m = parse_manifest({"version": "2026.1.2.1", "url": "releases/t.zip", "sha256": "ab" * 32, "size": 9},
                           "https://example.org/x/latest.json?t=1")
        expect(m["url"] == "https://example.org/x/releases/t.zip", "relative address: %s" % m["url"])
        for bad in ({"version": "1.2", "url": "t.zip", "sha256": "ab" * 32, "size": 9},
                    {"version": "2026.1.2.1", "url": "https://elsewhere.example/t.zip", "sha256": "ab" * 32,
                     "size": 9},
                    {"version": "2026.1.2.1", "url": "t.zip", "sha256": "abc", "size": 9},
                    {"version": "2026.1.2.1", "url": "t.zip", "sha256": "ab" * 32, "size": MAX_ZIP + 1},
                    {"format": 2, "version": "2026.1.2.1", "url": "t.zip", "sha256": "ab" * 32, "size": 9}):
            try:
                parse_manifest(bad, "https://example.org/x/latest.json")
            except UpdateError:
                continue
            raise AssertionError("accepted a bad update list: %r" % bad)

        # Unpacking: a good zip, then ones that try to escape the folder.
        def make_zip(path, files):
            with zipfile.ZipFile(path, "w") as z:
                for name, data in files.items():
                    z.writestr(name, data)
            return path

        good = make_zip(os.path.join(base, "good.zip"),
                        {"VERSION": "2026.1.2.1\n", "tideview.py": "#\n", "tvupdate.py": "#\n",
                         "install.sh": "#\n", "tv/app.py": "#\n", "assets/": ""})
        dest = os.path.join(base, "unpacked")
        os.makedirs(dest)
        unpack(good, dest)
        check_release(dest, "2026.1.2.1")
        for evil in ("../escape.txt", "/abs.txt", "tv/../../escape.txt", "c:/x.txt", "a\\..\\b"):
            try:
                unpack(make_zip(os.path.join(base, "evil.zip"), {evil: "x"}), tempfile.mkdtemp(dir=base))
            except BadRelease:
                continue
            raise AssertionError("unpacked %r" % evil)
        with zipfile.ZipFile(os.path.join(base, "link.zip"), "w") as z:
            info = zipfile.ZipInfo("tv")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc")
        try:
            unpack(os.path.join(base, "link.zip"), tempfile.mkdtemp(dir=base))
            raise AssertionError("unpacked a link")
        except BadRelease:
            pass
        expect(not os.path.exists(os.path.join(base, "escape.txt")), "escaped")

        # Switching versions, failed starts and going back.
        a, b, c = "2026.1.1.1", "2026.1.2.1", "2026.1.3.1"
        for v in (a, b, c):
            shutil.copytree(dest, release_dir(v))
        set_link("current", a)
        expect(apply(b, "", "test") and link("current") == b and link("previous") == a, "switch")
        for i in range(MAX_TRIES):
            change_state(boot_state)
            expect(link("current") == b, "gave up after %d tries" % (i + 1))
        change_state(boot_state)
        st = load_state()
        expect(link("current") == a and b in st["bad"] and not st["pending"], "go back: %r" % st)
        expect(st["event"]["kind"] == "rollback", "rollback event")
        # A version that proves itself stays.
        expect(apply(c, "", "test") and load_state()["pending"] == c, "switch again")
        change_state(boot_state)
        write_file(P.ok_marker, c)
        change_state(boot_state)
        expect(link("current") == c and not load_state()["pending"], "kept a working version")
        # A version that never proves itself is undone by check's health test.
        expect(apply(b, "", "test"), "switch to b")
        change_state(lambda s: s.update(pending_uptime=uptime() - GIVE_UP_AFTER - 1))
        expect(change_state(health) and link("current") == c, "health gave up")
        # A broken current link is repaired.
        set_link("current", "2099.1.1.1")
        change_state(boot_state)
        expect(usable(link("current")) and link("current") != b, "repair")
        expect(all(t.startswith("# Written by") for t in unit_texts().values()), "units")
    finally:
        P, DRY_RUN = saved
        shutil.rmtree(base, ignore_errors=True)
    log("Updater self-test passed (updater %d)." % UPDATER_VERSION)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="TideView's updater")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("boot")
    c = sub.add_parser("check")
    c.add_argument("--now", action="store_true", help="install a new version straight away")
    sub.add_parser("status")
    i = sub.add_parser("install")
    i.add_argument("dir")
    i.add_argument("--force", action="store_true", help="even if this Pi has a newer version")
    sub.add_parser("units")
    f = sub.add_parser("fetch")
    f.add_argument("--to", required=True)
    f.add_argument("--url")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    commands = {"boot": cmd_boot, "check": cmd_check, "status": cmd_status, "install": cmd_install,
                "units": cmd_units, "fetch": cmd_fetch, "selftest": cmd_selftest}
    try:
        return commands[args.cmd](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

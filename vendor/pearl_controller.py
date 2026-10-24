#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pearl (PRL) - controleur de minage pour Vast.ai et SaladCloud (Python 3.10+, stdlib uniquement).

Ce qu'il fait :
  1. Classe les serveurs du pool par latence (TCP), TLS en secours.
  2. Telecharge les mineurs depuis leurs releases GitHub officielles
     (verification SHA256 quand GitHub la fournit), versions epinglees en secours.
  3. Benchmark sur la machine louee : chaque mineur / kernel tourne quelques
     minutes sur le vrai pool, score = hashrate rapporte x (1 - dev fee).
     Le meilleur est garde ; resultat mis en cache (pas de re-benchmark apres
     une interruption tant que GPU, driver et versions sont identiques).
  4. Surveillance : crash, GPU inactif, plus de shares acceptees, taux de
     rejet, chute de hashrate, throttling thermique / power -> relance,
     changement de serveur, puis changement de mineur.
  5. Mise a jour automatique des mineurs (re-benchmark de la nouvelle version).
  6. Garde-fou de rentabilite optionnel : stoppe l'instance si le cout du PRL
     mine depasse MAX_COST_PER_PRL plusieurs fois de suite.

Usage : pearl_controller.py run | status | rescreen
"""
import collections, fcntl, hashlib, io, json, os, pty, re, shutil, signal, socket
import statistics, subprocess, sys, tarfile, threading, time, urllib.request, zipfile


# ----------------------------------------------------------------- config --
def env(k, d=None):
    v = os.environ.get(k, "")
    v = v.strip() if isinstance(v, str) else v
    return v if v else d


def envf(k, d):
    try:
        return float(env(k, d))
    except (TypeError, ValueError):
        return float(d)


HOME = env("PEARL_HOME", "/opt/pearl")
LOGF = env("PEARL_LOG", "/var/log/pearl-miner.log")
STATE_F = os.path.join(HOME, "state.json")
STATUS_F = os.path.join(HOME, "status.json")

WALLET = env("PRL_WALLET", "")
MERGE = env("MERGE_SUFFIX", "")            # ex. "+ADRESSE_NOCK" si le pool fait du merge mining
WORKER = env("WORKER") or ("v" + env("CONTAINER_ID") if env("CONTAINER_ID") else "vast")
# Identite de l'instance, reprise par la passerelle locale (pearl_bridge.py) pour rattacher
# chaque mesure a l'instance Vast correspondante.
INSTANCE_ID = env("CONTAINER_ID", "")
CTL_VERSION = "1.2.1"

DEFAULT_POOLS = "pool.pearlhash.xyz:9000,hk.pearlhash.net:9000,global.pearlhash.net:9000,global2.pearlhash.net:9000"
POOLS = [p.strip() for p in env("POOLS", DEFAULT_POOLS).split(",") if p.strip()]
TLS_FALLBACK = env("TLS_FALLBACK", "1") == "1"  # ajoute host:9443 en TLS pour les serveurs Pearlhash
CANDIDATES = [c.strip() for c in env(
    "CANDIDATES", "wildrig:k2,wildrig:k1").split(",") if c.strip()]

WARMUP = envf("WARMUP_SECONDS", 75)
BENCH = envf("BENCH_SECONDS", 240)
TICK = envf("TICK_SECONDS", 15)
GRACE = envf("GRACE_SECONDS", 240)
UTIL_MIN = envf("UTIL_MIN", 35)
UTIL_WINDOW = envf("UTIL_WINDOW", 150)
SHARE_TIMEOUT = envf("SHARE_TIMEOUT", 1500)
REJECT_MAX = envf("REJECT_MAX", 0.15)
DEGRADE = envf("DEGRADE_RATIO", 0.75)
UPDATE_HOURS = envf("UPDATE_HOURS", 6)
CACHE_HOURS = envf("CACHE_HOURS", 72)
POOL_FEE = envf("POOL_FEE", 0.01)
OC_MODE = env("OC", "off")                 # off | memlock (verrouille la VRAM a 810 MHz si l'hote l'autorise)

MAX_COST_PER_PRL = env("MAX_COST_PER_PRL")  # ex. 2.0 ($ par PRL mine) ; vide = garde-fou desactive
GUARD_STRIKES = int(envf("GUARD_STRIKES", 3))
GUARD_INTERVAL = envf("GUARD_INTERVAL", 1800)
GUARD_ACTION = env("GUARD_ACTION", "stop")  # stop | log
WTM_URL = env("WTM_URL", "https://whattomine.com/coins/469.json")

FEES = {"wildrig": 0.0, "pearlminer": 0.0, "srbminer": 0.02, "bzminer": 0.02}
WILDRIG_FEE_OTHER = envf("WILDRIG_FEE_OTHER", 0.02)  # inconnu hors pool Pearlhash : hypothese prudente
for kv in env("FEES", "").split(","):
    if "=" in kv:
        k, v = kv.split("=", 1)
        try:
            FEES[k.strip()] = float(v)
        except ValueError:
            pass


# ---------------------------------------------------------------- logging --
_log_lock = threading.Lock()


def log(msg, tag="ctl"):
    line = "%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg)
    with _log_lock:
        try:
            if os.path.exists(LOGF) and os.path.getsize(LOGF) > 50_000_000:
                os.replace(LOGF, LOGF + ".1")
            with open(LOGF, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            sys.stderr.write(line)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": "pearl-controller/2.0"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if env("GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + env("GITHUB_TOKEN")
    return h


# ---------------------------------------------------------------- mineurs --
def _scheme(pool, prefix_tcp, prefix_ssl):
    return (prefix_ssl if pool["tls"] else prefix_tcp) + pool["hostport"]


MINERS = {
    "wildrig": {
        "repo": "andru-kun/wildrig-multi",
        "asset": r"^wildrig-multi-linux-[\d.]+\.tar\.(gz|xz)$",
        "pin": "0.51.7",
        "pin_url": lambda v: "https://github.com/andru-kun/wildrig-multi/releases/download/%s/wildrig-multi-linux-%s.tar.gz" % (v, v),
        "bins": ["wildrig-multi"],
        "variants": {"k1": [], "k2": ["--pearlhash-kernel", "2"]},
        "cmd": lambda b, p, u: [b, "--algo", "pearlhash", "--url", _scheme(p, "stratum+tcp://", "stratum+ssl://"),
                                "--user", u["wallet"], "--worker", u["worker"], "--pass", "x"],
    },
    # Mineurs CUDA : seuls utilisables sous WSL2 (noeuds SaladCloud), ou le pilote NVIDIA
    # n'expose aucune plateforme OpenCL et WildRig ne voit donc aucun GPU.
    # pearl-miner : mineur officiel de pearlhash.xyz, le seul mineur CUDA que ce pool accepte
    # (SRBMiner y est deconnecte des la connexion). Il n'affiche pas les shares.
    "pearlminer": {
        "repo": "pearlhash/pearlhash-miner",
        "asset": r"^pearl-miner$",
        "pin": "V14",
        "pin_url": lambda v: "https://github.com/pearlhash/pearlhash-miner/releases/download/%s/pearl-miner" % v,
        "bins": ["pearl-miner"],
        "variants": {"": []},
        "tls": False,
        "cmd": lambda b, p, u: [b, "--host", p["hostport"], "--user", u["wallet"], "--worker", u["worker"]],
        "hr_line": re.compile(r"^\s*Hashrate Total\b", re.I),
    },
    # SRBMiner et BzMiner : pools Kryptex, HeroMiners... (POOLS_SRBMINER / POOLS_BZMINER).
    "srbminer": {
        "repo": "doktor83/SRBMiner-Multi",
        "asset": r"^SRBMiner-Multi-[\d-]+-Linux\.tar\.(gz|xz)$",
        "pin": "3.8.0",
        "pin_url": lambda v: "https://github.com/doktor83/SRBMiner-Multi/releases/download/%s/SRBMiner-Multi-%s-Linux.tar.gz"
                             % (v, v.replace(".", "-")),
        "bins": ["SRBMiner-MULTI"],
        "variants": {"": []},
        # TLS en option (--tls true) et non dans l'URL ; le CPU de l'hote ne mine pas.
        "cmd": lambda b, p, u: [b, "--algorithm", "pearlhash", "--pool", p["hostport"]]
                               + (["--tls", "true"] if p["tls"] else [])
                               + ["--wallet", u["wallet"], "--worker", u["worker"], "--password", "x", "--disable-cpu"],
        # Le tableau affiche aussi des moyennes "1 min / 15 min / 1 hr / 6 hr" qui trainent au
        # demarrage : seule la ligne "Hashrate" (total instantane) fait foi.
        "hr_line": re.compile(r"(?:^|\]\s*)Hashrate\s+\d", re.I),
    },
    "bzminer": {
        "repo": "bzminer/bzminer",
        "asset": r"^bzminer_v[\d.]+_linux\.tar\.(gz|xz)$",
        "pin": "v100.50",
        "pin_url": lambda v: "https://github.com/bzminer/bzminer/releases/download/{0}/bzminer_{0}_linux.tar.gz".format(
            v if str(v).startswith("v") else "v%s" % v),
        "bins": ["bzminer"],
        "variants": {"": []},
        # -o log : dans un pty, BzMiner ouvrirait son tableau de bord plein ecran (illisible ici).
        "cmd": lambda b, p, u: [b, "-a", "pearl", "-p", _scheme(p, "stratum+tcp://", "stratum+ssl://"),
                                "-w", u["wallet"], "--worker", u["worker"], "--pass", "x", "--nvidia",
                                "-o", "log", "--log-table-interval", "300000"],
        # Seule la ligne de synthese fait foi : le tableau met le hashrate estime par le pool
        # avant celui du mineur, et l'unite s'ecrit "th" sans "/s".
        "hr_line": re.compile(r"\bpearl hashrate\b", re.I),
        "hr_re": re.compile(r"(\d+(?:\.\d+)?)\s*([kmgtpe]?)h(?:/s)?(?![a-z])", re.I),
    },
}

# Mineurs personnalises : CUSTOM_MINERS='[{"name":"x","path":"/bin/x" ou "url":"...","fee":0.01,
#   "bins":["x"],"cmd":["{bin}","--pool","{hostport}","--user","{user}"]}]'
# Variables : {bin} {host} {port} {hostport} {url} {user} {wallet} {worker} {tls}
try:
    _custom = json.loads(env("CUSTOM_MINERS", "[]"))
except ValueError:
    _custom = []
for cm in _custom:
    tpl = cm["cmd"]

    def _mk(tpl):
        def f(b, p, u):
            ctx = {"bin": b, "host": p["host"], "port": str(p["port"]), "hostport": p["hostport"],
                   "url": _scheme(p, "stratum+tcp://", "stratum+ssl://"), "user": u["user"],
                   "wallet": u["wallet"], "worker": u["worker"], "tls": "true" if p["tls"] else "false"}
            return [a.format(**ctx) for a in tpl]
        return f
    MINERS[cm["name"]] = {"repo": None, "asset": None, "pin": cm.get("version", "custom"),
                          "path": cm.get("path"), "url": cm.get("url"),
                          "bins": cm.get("bins", [os.path.basename(cm.get("path") or cm["name"])]),
                          "variants": cm.get("variants", {"": []}), "cmd": _mk(tpl)}
    FEES[cm["name"]] = float(cm.get("fee", 0.0))


def miner_fee(name, pool):
    if name == "wildrig" and "pearlhash" not in pool["host"]:
        return WILDRIG_FEE_OTHER
    return FEES.get(name, 0.02)


# Frais par pool (sous-chaine du nom d'hote) : ne compte que pour departager des mineurs
# qui minent sur des pools differents. POOL_FEES="kryptex=0.01,herominers=0".
POOL_FEES = {"pearlhash": POOL_FEE, "kryptex": 0.01, "herominers": 0.0}
for kv in env("POOL_FEES", "").split(","):
    if "=" in kv:
        k, v = kv.split("=", 1)
        try:
            POOL_FEES[k.strip()] = float(v)
        except ValueError:
            pass
MERGE_BONUS = envf("MERGE_BONUS", 0)  # valeur du merge mining (NOCK), en fraction du PRL mine


def pool_factor(pool):
    """Part du PRL mine que garde le pool, plus la valeur du merge mining s'il y est fait."""
    fee = next((v for k, v in POOL_FEES.items() if k in pool["host"]), POOL_FEE)
    merged = bool(MERGE) and ("pearlhash" in pool["host"] or env("MERGE_ANY_POOL") == "1")
    return (1 - fee) * (1 + (MERGE_BONUS if merged else 0))


def resolve_release(name):
    spec = MINERS[name]
    if spec.get("path"):
        return spec["pin"], None, None
    if spec.get("url"):
        return spec["pin"], spec["url"], None
    want = env(name.upper() + "_VER", "latest")
    if want != "latest":
        return want, spec["pin_url"](want), None
    try:
        data = json.loads(http_get("https://api.github.com/repos/%s/releases/latest" % spec["repo"],
                                   headers=gh_headers()))
        for a in data.get("assets", []):
            if re.match(spec["asset"], a.get("name", "")):
                dg = a.get("digest") or ""
                return data["tag_name"], a["browser_download_url"], (dg[7:] if dg.startswith("sha256:") else None)
        log("%s : aucun binaire Linux dans la derniere release, version epinglee" % name)
    except Exception as e:  # rate limit GitHub, reseau...
        inst = installed_versions(name)
        if inst:
            log("%s : API GitHub indisponible (%s), version deja installee %s conservee" % (name, e, inst[-1]))
            return inst[-1], spec["pin_url"](inst[-1]), None
        log("%s : API GitHub indisponible (%s), version epinglee %s" % (name, e, spec["pin"]))
    return spec["pin"], spec["pin_url"](spec["pin"]), None


def _vkey(v):
    return [int(x) for x in re.findall(r"\d+", str(v))] or [-1]


def installed_versions(name):
    d = os.path.join(HOME, "miners")
    try:
        vs = [e[len(name) + 1:] for e in os.listdir(d) if e.startswith(name + "-") and not e.endswith(".download")]
    except OSError:
        return []
    return sorted(vs, key=_vkey)


def _find_bin(root, names):
    for d, _, files in os.walk(root):
        for fn in files:
            if fn in names:
                p = os.path.join(d, fn)
                os.chmod(p, 0o755)
                return p
    return None


def _safe_extract_tar(data, dest):
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        root = os.path.realpath(dest)
        for m in t.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not target.startswith(root + os.sep) and target != root:
                continue
            if m.issym() or m.islnk():
                continue
            t.extract(m, dest)


def install(name, version, url, sha):
    spec = MINERS[name]
    if spec.get("path"):
        return spec["path"]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(version))
    d = os.path.join(HOME, "miners", "%s-%s" % (name, safe))
    b = _find_bin(d, spec["bins"]) if os.path.isdir(d) else None
    if b:
        return b
    data, err = None, None
    for i in range(5):
        try:
            data = http_get(url, timeout=120)
            break
        except Exception as e:
            err = e
            time.sleep(5 * (i + 1))
    if data is None:
        raise RuntimeError("telechargement impossible : %s" % err)
    if sha and hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError("SHA256 different de celui publie par GitHub")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    fn = url.rsplit("/", 1)[-1].lower()
    if fn.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar")):
        _safe_extract_tar(data, d)
    elif fn.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(d)
    else:
        if data[:4] != b"\x7fELF":
            raise RuntimeError("le fichier telecharge n'est pas un binaire Linux")
        with open(os.path.join(d, spec["bins"][0]), "wb") as f:
            f.write(data)
    b = _find_bin(d, spec["bins"])
    if not b:
        raise RuntimeError("binaire %s introuvable dans l'archive" % spec["bins"])
    log("%s %s installe (%s)%s" % (name, version, b, " - SHA256 verifie" if sha else ""))
    return b


class Config:
    """Un mineur + une variante (kernel), ex. wildrig:k2."""

    def __init__(self, key):
        self.key = key
        self.name, _, self.variant = key.partition(":")
        if self.name not in MINERS:
            raise KeyError(self.name)
        if self.variant not in MINERS[self.name]["variants"]:
            raise KeyError(key)
        self.version = self.bin = None

    def prepare(self, force_check=False):
        if self.bin and not force_check:
            return self
        v, url, sha = resolve_release(self.name)
        if self.bin and v == self.version:
            return self
        self.bin = install(self.name, v, url, sha)
        self.version = v
        return self

    def command(self, pool):
        # Le suffixe de merge mining (@nock=...) n'a de sens que sur Pearlhash : un autre
        # pool refuserait ce login.
        merge = MERGE if "pearlhash" in pool["host"] or env("MERGE_ANY_POOL") == "1" else ""
        u = {"wallet": WALLET + merge, "worker": WORKER, "user": "%s%s.%s" % (WALLET, merge, WORKER)}
        extra = env("EXTRA_ARGS_" + self.name.upper(), "")
        return (MINERS[self.name]["cmd"](self.bin, pool, u) + MINERS[self.name]["variants"][self.variant]
                + (extra.split() if extra else []))


# -------------------------------------------------------------- telemetrie --
_TH_SETS = [
    ["clocks_event_reasons.sw_thermal_slowdown", "clocks_event_reasons.hw_thermal_slowdown",
     "clocks_event_reasons.sw_power_cap", "clocks_event_reasons.hw_slowdown"],
    ["clocks_throttle_reasons.sw_thermal_slowdown", "clocks_throttle_reasons.hw_thermal_slowdown",
     "clocks_throttle_reasons.sw_power_cap", "clocks_throttle_reasons.hw_slowdown"],
    [],
]
_BASE_FIELDS = ["index", "name", "driver_version", "utilization.gpu", "power.draw", "power.limit",
                "power.default_limit", "temperature.gpu", "clocks.sm", "clocks.max.sm"]
_th_idx = 0


def gpus():
    global _th_idx
    while _th_idx < len(_TH_SETS):
        fields = _BASE_FIELDS + _TH_SETS[_th_idx]
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(fields),
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            if "Field" in out.stdout + out.stderr or "valid" in (out.stdout + out.stderr).lower():
                _th_idx += 1
                continue
            return []
        res = []
        for line in out.stdout.strip().splitlines():
            vals = [v.strip() for v in line.split(",")]
            if len(vals) != len(fields):
                continue
            g = {}
            for f, v in zip(fields, vals):
                key = f.split(".")[-1] if "reasons" in f else f
                if v in ("[N/A]", "N/A", "[Not Supported]", ""):
                    g[key] = None
                elif v in ("Active", "Not Active"):
                    g[key] = v == "Active"
                else:
                    try:
                        g[key] = float(v)
                    except ValueError:
                        g[key] = v
            # thermique / hw_slowdown = refroidissement insuffisant chez l'hote (mauvais signe) ;
            # power cap = la carte utilise tout son budget de puissance (normal en minage)
            g["thermal"] = any(g.get(k) for k in ("sw_thermal_slowdown", "hw_thermal_slowdown", "hw_slowdown"))
            g["power_capped"] = bool(g.get("sw_power_cap"))
            res.append(g)
        return res
    return []


def util_of(gs):
    """Utilisation moyenne des GPU qui la publient, None si aucun ne la publie."""
    v = [g["utilization.gpu"] for g in gs if isinstance(g.get("utilization.gpu"), (int, float))]
    return statistics.mean(v) if v else None


def fingerprint(cands):
    gs = gpus()
    sig = [(g.get("name"), g.get("driver_version")) for g in gs] + sorted(cands) + [OC_MODE]
    return hashlib.sha256(json.dumps(sig, default=str).encode()).hexdigest()[:16], gs


def gpu_verdict(idle_clk, load_clk, max_clk, power, limit, hr):
    """Le GPU donne-t-il ce qu'il peut ? Aucune table par modele : un GPU sain monte
    en frequence des qu'il mine et consomme une part notable de sa limite.
    'fige' = les frequences restent collees a celles du repos (verrou de l'hote),
    'sous-alimente' = peu de watts malgre du travail, 'muet' = aucun hashrate lu."""
    if not hr:
        return "muet"
    if idle_clk and load_clk and idle_clk > 800 and load_clk <= idle_clk * 1.15 \
            and max_clk and load_clk < max_clk * 0.75:
        return "fige"
    if limit and power and max_clk and power < limit * 0.55 and load_clk < max_clk * 0.6:
        return "sous-alimente"
    return "conforme"


# ------------------------------------------------------------------- pools --
def parse_pool(s, tls=False):
    if s.startswith(("ssl://", "stratum+ssl://", "tls://")):
        tls, s = True, s.split("://", 1)[1]
    elif "://" in s:
        s = s.split("://", 1)[1]
    host, _, port = s.rpartition(":")
    return {"host": host, "port": int(port), "hostport": "%s:%s" % (host, port), "tls": tls}


def tcp_rtt(host, port, tries=3):
    ts = []
    for _ in range(tries):
        t = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=3):
                ts.append((time.monotonic() - t) * 1000)
        except OSError:
            pass
    return statistics.median(ts) if ts else None


def miner_pools(name):
    """Serveurs d'un mineur : POOLS_<MINEUR> s'il est defini, sinon POOLS. Pearlhash
    n'accepte que WildRig et pearl-miner : SRBMiner et BzMiner minent ailleurs."""
    own = env("POOLS_" + str(name or "").upper())
    return [p.strip() for p in own.split(",") if p.strip()] if own else POOLS


def rank_pools(specs=None, tls_ok=True):
    entries = [parse_pool(p) for p in (specs if specs is not None else POOLS)]
    if not tls_ok:
        entries = [e for e in entries if not e["tls"]]
    if TLS_FALLBACK and tls_ok:
        for p in list(entries):
            if "pearlhash" in p["host"] and p["port"] == 9000 and not p["tls"]:
                entries.append(parse_pool("%s:9443" % p["host"], tls=True))
    for e in entries:
        e["rtt"] = tcp_rtt(e["host"], e["port"])
    entries.sort(key=lambda e: (e["rtt"] is None, e["tls"], e["rtt"] or 0))
    log("serveurs : " + ", ".join("%s%s=%s" % (e["hostport"], "(tls)" if e["tls"] else "",
                                             "%.0fms" % e["rtt"] if e["rtt"] else "KO") for e in entries))
    return entries


# --------------------------------------------------------- process mineur --
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
HR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([kKMGTPE]?)H/s")
# Ligne consacree a une seule carte, selon les mineurs : "#3 ...", "GPU0 ...",
# "GPU #0 ...", "[GPU0] ...". Les autres lignes de hashrate sont des totaux.
GPU_LINE_RE = re.compile(r"^\s*(?:#\s*|gpu\s*#?\s*|\[\s*gpu\s*)(\d+)\b", re.I)
UNIT = {"": 1e-12, "k": 1e-9, "K": 1e-9, "M": 1e-6, "G": 1e-3, "T": 1.0, "P": 1e3, "E": 1e6}
CUM_RES = [re.compile(r"accepted\s*\(\s*(\d+)\s*/\s*(\d+)", re.I),
           re.compile(r"accepted\s*(?:shares?)?\s*[:=]?\s*(\d+)\D{1,20}?rejected\s*(?:shares?)?\s*[:=]?\s*(\d+)", re.I),
           re.compile(r"\bA\s*[:=]\s*(\d+)\b.{0,30}?\bR\s*[:=]\s*(\d+)")]
ACC_RE = re.compile(r"\baccepted\b", re.I)
REJ_RE = re.compile(r"\b(rejected|invalid share|stale share|low difficulty|duplicate share)\b", re.I)
REJ0_RE = re.compile(r"rejected\s*(?:shares?)?\s*[:=]?\s*0\b", re.I)
SUMMARY_RE = re.compile(r"\b(accepted|rejected)\s*:\s*(\d+|-)\s*$", re.I)
ACC_N_RE = re.compile(r"\baccepted\s*:\s*(\d+)", re.I)
REJ_N_RE = re.compile(r"\brejected\s*:\s*(\d+)", re.I)
# Hashrate du reseau ou estime par le pool : ce n'est pas ce que la carte calcule.
NET_LINE_RE = re.compile(r"\b(?:network|net|pool)\s*(?:hash(?:rate)?|speed|hr)\b|\bdifficulty\b", re.I)
SHARES_EQ_RE = re.compile(r"\bshares\s*=\s*(\d+)\b", re.I)  # BzMiner : cumul des shares acceptees


class Run:
    def __init__(self, cfg, pool):
        self.cfg, self.pool, self.t0 = cfg, pool, time.time()
        self.hr = collections.deque(maxlen=3000)
        self.gpuhr = {}
        self.events = collections.deque(maxlen=5000)
        self.cum = None
        self.ev_since = (0, 0)  # shares vues une a une depuis le dernier compteur cumule
        self.acc = self.rej = 0
        self.last_acc = None
        self.tail = collections.deque(maxlen=15)
        self.lock = threading.Lock()
        cmd = cfg.command(pool)
        log("lancement %s %s sur %s%s" % (cfg.key, cfg.version, pool["hostport"], " (tls)" if pool["tls"] else ""))
        # Pseudo-terminal : WildRig avorte au demarrage si son entree n'est pas un
        # terminal (assertion libuv), et un pipe retient sa sortie jusqu'a 4 Ko.
        # Le pty reproduit exactement les conditions d'un lancement a la main en SSH.
        self.mfd, sfd = pty.openpty()
        try:
            self.p = subprocess.Popen(cmd, stdin=sfd, stdout=sfd, stderr=sfd,
                                      cwd=os.path.dirname(cfg.bin) or None, start_new_session=True)
        finally:
            os.close(sfd)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        fd = self.mfd
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                self._line(ANSI.sub("", raw.decode("utf-8", "replace")).strip())
        if buf:
            self._line(ANSI.sub("", buf.decode("utf-8", "replace")).strip())

    def _line(self, s):
        if not s:
            return
        with self.lock:
            self._parse(s)

    def _cumulative(self, now, a, r):
        """Compteurs cumules d'acceptees / rejetees (None = absent de cette ligne)."""
        pa, pr = self.cum or (0, 0)
        if (a is not None and a < pa) or (r is not None and r < pr):
            # compteurs repartis a zero (mineur relance) : la valeur absente
            # de cette ligne n'est pas encore connue, on repart de zero
            pa, pr = 0, 0
            a, r = (a or 0), (r or 0)
        else:
            a = pa if a is None else a
            r = pr if r is None else r
        # Les shares deja comptees une a une depuis le dernier cumul ne le sont pas deux fois
        # (un mineur qui affiche a la fois "share accepted" et un total).
        ea, er = getattr(self, "ev_since", (0, 0))
        da, dr = max(a - pa - ea, 0), max(r - pr - er, 0)
        self.ev_since = (0, 0)
        for _ in range(min(da, 1000)):
            self.events.append((now, True))
        for _ in range(min(dr, 1000)):
            self.events.append((now, False))
        if a > pa:
            self.last_acc = now
        self.acc += da
        self.rej += dr
        self.cum = (a, r)

    def _parse(self, s):
        self.tail.append(s)
        log(s[:400], self.cfg.key)
        now = time.time()
        spec = MINERS.get(getattr(self.cfg, "name", ""), {})
        hr_line = spec.get("hr_line")
        if NET_LINE_RE.search(s) or (hr_line and not hr_line.search(s)):
            vals = []
        else:
            vals = [float(n.replace(",", ".")) * UNIT[u if u in UNIT else u.upper()]
                    for n, u in (spec.get("hr_re") or HR_RE).findall(s)]
        if vals and max(vals) > 0:
            # Ligne par GPU ("#3 GeForce RTX 3080  109.89 TH/s") : on retient la valeur
            # de chaque carte et on somme. Sans cela, une instance de 8 GPU passerait
            # pour une carte unique et le garde-fou verrait 1/8 des revenus.
            m = None if hr_line else GPU_LINE_RE.match(s)
            if m:
                i = int(m.group(1))
                if i in self.gpuhr:  # l'indice revient : la passe precedente est complete
                    self.hr.append((now, sum(self.gpuhr.values())))
                    self.gpuhr = {}
                self.gpuhr[i] = max(vals)
            else:  # ligne de synthese ("10s:", "Total:") : le mineur donne deja le total
                self.hr.append((now, max(vals)))
        for rx in CUM_RES:
            m = rx.search(s)
            if m:
                self._cumulative(now, int(m.group(1)), int(m.group(2)))
                return
        if SUMMARY_RE.search(s):
            # Tableau de statistiques ("Accepted: 10", "Rejected: -") : compteurs
            # cumules, le tiret signifiant "inchange". Les traiter comme des
            # evenements elementaires faisait compter un rejet a chaque ligne
            # "Rejected: -", soit 30 % de rejets imaginaires et une relance du
            # mineur toutes les cinq minutes.
            ma, mr = ACC_N_RE.search(s), REJ_N_RE.search(s)
            self._cumulative(now, int(ma.group(1)) if ma else None, int(mr.group(1)) if mr else None)
            return
        m = SHARES_EQ_RE.search(s)
        if m:
            self._cumulative(now, int(m.group(1)), None)
            return
        ea, er = getattr(self, "ev_since", (0, 0))
        if ACC_RE.search(s) and not re.search(r"\b(not|no) accepted\b|\baccepted connection\b", s, re.I):
            self.acc += 1
            self.last_acc = now
            self.events.append((now, True))
            self.ev_since = (ea + 1, er)
        elif REJ_RE.search(s) and not REJ0_RE.search(s):
            self.rej += 1
            self.events.append((now, False))
            self.ev_since = (ea, er + 1)

    def alive(self):
        return self.p.poll() is None

    def age(self):
        return time.time() - self.t0

    def hr_median(self, since):
        with self.lock:
            snap = list(self.hr)
        v = [h for t, h in snap if t >= since]
        return statistics.median(v) if v else None

    def reject_ratio(self, window=3600):
        cut = time.time() - window
        with self.lock:
            snap = list(self.events)
        ev = [ok for t, ok in snap if t >= cut]
        return (ev.count(False) / len(ev), len(ev)) if ev else (0.0, 0)

    def stop(self):
        if self.alive():
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(12)
            except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            pass
        if self.mfd >= 0:
            try:
                os.close(self.mfd)
            except OSError:
                pass
            self.mfd = -1


# ------------------------------------------------------------- economie --
_wtm = {"t": 0, "data": None}


def network():
    """Difficulte et recompense depuis WhatToMine (cache 30 min), auto-verifiees."""
    if time.time() - _wtm["t"] < 1800 and _wtm["data"]:
        return _wtm["data"]
    _wtm["t"] = time.time()
    try:
        j = json.loads(http_get(WTM_URL, timeout=20))
        num = lambda k: float(str(j[k]).replace(",", "")) if j.get(k) not in (None, "") else None
        d, r, bt, nh = num("difficulty"), num("block_reward"), num("block_time"), num("nethash")
        if not d or not r:
            raise ValueError("champs manquants")
        if bt and nh and not 0.75 < (d * 2 ** 48 / bt) / nh < 1.33:
            raise ValueError("convention de difficulte inattendue (D*2^48/T != nethash)")
        _wtm["data"] = {"difficulty": d, "block_reward": r, "exchange_rate": num("exchange_rate")}
    except Exception as e:
        log("donnees reseau indisponibles (%s)" % e)
        _wtm["data"] = None
    return _wtm["data"]


def prl_per_hour(th_net):
    n = network()
    if not n or not th_net:
        return None
    return th_net * 1e12 * 3600 * n["block_reward"] / (n["difficulty"] * 2 ** 48) * (1 - POOL_FEE)


def vastai(*args):
    exe = shutil.which("vastai")
    if not exe:
        for extra in ([], ["--break-system-packages"]):
            if subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vastai"] + extra,
                              capture_output=True).returncode == 0:
                break
        exe = shutil.which("vastai") or "vastai"
    key = ["--api-key", env("CONTAINER_API_KEY")] if env("CONTAINER_API_KEY") else []
    return subprocess.run([exe] + list(args) + key, capture_output=True, text=True, timeout=90)


def cost_per_hour():
    if env("COST_PER_HOUR"):
        return envf("COST_PER_HOUR", 0)
    if not env("CONTAINER_ID"):
        return None
    for args in (["show", "instance", env("CONTAINER_ID"), "--raw"], ["--raw", "show", "instance", env("CONTAINER_ID")]):
        try:
            out = vastai(*args)
            j = json.loads(out.stdout)
            j = j[0] if isinstance(j, list) else j
            j = j.get("instances", j) if isinstance(j, dict) else j
            if j.get("dph_total"):
                return float(j["dph_total"])
        except Exception:
            continue
    return None


# --------------------------------------------------------------- pilote --
class Controller:
    def __init__(self):
        self.state = load_json(STATE_F, {})
        self.cfgs = {}
        for k in CANDIDATES:
            try:
                self.cfgs[k] = Config(k)
            except KeyError:
                log("candidat inconnu ignore : %s" % k)
        self.pools, self.pi = [], 0
        # serveurs propres a un mineur (POOLS_<MINEUR>) : classes une fois, puis memorises
        self.pool_key, self.pool_sets = ",".join(POOLS), {}
        self.run = None
        self.cur = None
        self.bad = {}
        self.restarts = collections.deque(maxlen=20)
        self.util = collections.deque(maxlen=400)
        self.throttle = collections.deque(maxlen=2000)
        self.last_update = time.time()
        self.last_degrade = 0
        self.guard = {"strikes": 0, "last": 0, "cost_per_prl": None, "dph": None}
        self.stopping = False
        self.oc_applied = False

    # ---- utilitaires
    def pool(self):
        return self.pools[self.pi % len(self.pools)]

    def use_pools_for(self, name):
        """Bascule sur les serveurs du mineur `name` (pearl-miner ne parle pas TLS)."""
        specs = miner_pools(name)
        tls_ok = MINERS.get(name, {}).get("tls", True)
        key = ",".join(specs) + ("" if tls_ok else "|sans-tls")
        if key == self.pool_key and self.pools:
            return
        if self.pools:
            self.pool_sets[self.pool_key] = (self.pools, self.pi)
        self.pool_key = key
        self.pools, self.pi = self.pool_sets.get(key) or (rank_pools(specs, tls_ok), 0)

    def rerank(self):
        self.pool_sets.clear()
        name = self.cfgs[self.cur].name if self.cur in self.cfgs else None
        self.pools, self.pool_key = [], None
        self.use_pools_for(name)

    def rotate(self):
        reachable = [i for i, p in enumerate(self.pools) if p.get("rtt") is not None]
        if reachable and len(reachable) < len(self.pools):
            nxt = [i for i in reachable if i > self.pi % len(self.pools)]
            self.pi = (nxt or reachable)[0]
        else:
            self.pi = (self.pi + 1) % len(self.pools)
        log("serveur suivant : %s%s" % (self.pool()["hostport"], " (tls)" if self.pool()["tls"] else ""))

    def sample(self):
        gs = gpus()
        if gs:
            # Sous WSL2 (SaladCloud), NVML peut ne pas publier l'utilisation : une valeur
            # absente est inconnue, pas 0 %, sinon le mineur serait relance en boucle.
            u = util_of(gs)
            if u is not None:
                self.util.append((time.time(), u))
            self.throttle.append(any(g.get("thermal") for g in gs))
        return gs

    def util_mean(self, window, since=0):
        cut = max(time.time() - window, since)
        v = [u for t, u in self.util if t >= cut]
        return statistics.mean(v) if v else None

    def start(self, key):
        if self.run:
            self.run.stop()
        self.cur = key
        cfg = self.cfgs[key].prepare()
        self.use_pools_for(cfg.name)
        self.run = Run(cfg, self.pool())

    def restart(self, why, rotate=False):
        log("relance (%s)" % why)
        self.restarts.append(time.time())
        if rotate:
            self.rotate()
        recent = [t for t in self.restarts if time.time() - t < 900]
        if len(recent) >= 4:
            log("%s instable (%d relances en 15 min) -> mineur suivant" % (self.cur, len(recent)))
            self.bad[self.cur] = time.time()
            self.restarts.clear()
            return self.start(self.best(exclude_bad=True))
        self.start(self.cur)

    def best(self, exclude_bad=True):
        sc = self.state.get("scores", {})
        # net = apres dev fee ; pool_factor = frais du pool et merge mining (mineurs sur des pools differents)
        ok = [(v["net"] * (v.get("pool_factor") or 1), k) for k, v in sc.items() if k in self.cfgs and v.get("net")
              and not (exclude_bad and time.time() - self.bad.get(k, 0) < 6 * 3600)]
        if ok:
            return max(ok)[1]
        alive = [k for k in self.cfgs if not (exclude_bad and time.time() - self.bad.get(k, 0) < 6 * 3600)]
        return (alive or list(self.cfgs))[0]

    # ---- OC opportuniste (souvent refuse dans un conteneur Vast)
    def apply_oc(self):
        if OC_MODE != "memlock":
            return
        r = subprocess.run(["nvidia-smi", "-lmc", "810,810"], capture_output=True, text=True)
        self.oc_applied = r.returncode == 0
        log("verrouillage VRAM 810 MHz : %s" % ("OK" if self.oc_applied else "refuse par l'hote (normal sur Vast)"))

    def restore_oc(self):
        if self.oc_applied:
            subprocess.run(["nvidia-smi", "-rmc"], capture_output=True)

    # ---- benchmark
    def screen(self, keys, reason):
        log("benchmark (%s) : %s" % (reason, ", ".join(keys)))
        scores = self.state.setdefault("scores", {})
        best_net = max([scores[k]["net"] for k in scores if k not in keys and scores[k].get("net")] or [0])
        for k in keys:
            if self.stopping:
                return
            cfg = self.cfgs[k]
            try:
                cfg.prepare(force_check=True)
            except Exception as e:
                log("%s indisponible : %s" % (k, e))
                scores[k] = {"net": 0, "error": str(e)[:200], "t": time.time()}
                continue
            if self.run:
                self.run.stop()
            self.use_pools_for(cfg.name)
            self.cur, self.run = k, Run(cfg, self.pool())
            r = self.run
            fee = miner_fee(cfg.name, self.pool())
            utils, thr = [], []
            end = r.t0 + WARMUP + BENCH
            while time.time() < end and r.alive() and not self.stopping:
                time.sleep(min(5, TICK))
                gs = self.sample()
                if gs and r.age() > WARMUP:
                    if util_of(gs) is not None:
                        utils.append(util_of(gs))
                    thr.append(any(g.get("thermal") for g in gs))
                # abandon precoce si nettement moins bon que le meilleur deja mesure
                med = r.hr_median(r.t0 + WARMUP)
                if med and best_net and r.age() > WARMUP + 60 and med * (1 - fee) < 0.6 * best_net:
                    log("%s abandonne (%.1f TH/s net, loin du meilleur)" % (k, med * (1 - fee)))
                    break
            th = r.hr_median(r.t0 + WARMUP)
            died = not r.alive()
            r.stop()
            net = (th or 0) * (1 - fee)
            if died and r.age() < WARMUP:
                net = 0
                log("%s s'est arrete au demarrage. Dernieres lignes : %s" % (k, " | ".join(list(r.tail)[-4:])))
            if r.rej > max(3, r.acc):
                net *= 0.5
            scores[k] = {"th": th, "net": round(net, 3), "fee": fee, "version": cfg.version, "acc": r.acc,
                         "rej": r.rej, "util": round(statistics.mean(utils), 1) if utils else None,
                         "throttle_pct": round(100 * sum(thr) / len(thr), 1) if thr else None, "t": time.time(),
                         "pool": self.pool()["hostport"], "pool_factor": round(pool_factor(self.pool()), 4)}
            best_net = max(best_net, net)
            log("score %s %s : brut %s TH/s, net %.1f TH/s (dev fee %.0f%%), shares %d/%d" % (
                k, cfg.version, "%.1f" % th if th else "?", net, fee * 100, r.acc, r.rej))
            self.run = None
        save_json(STATE_F, self.state)

    def preflight(self):
        """Mesure courte de chaque kernel WildRig, avant tout benchmark long : elle
        donne le hashrate des le demarrage, choisit le meilleur kernel et dit si la
        carte tient ses frequences. C'est elle qui remplace le bench de 25 minutes.
        Retourne (verdict, {"scores", "best"}) ou (None, None) si elle n'a pas pu tourner."""
        secs = envf("PREFLIGHT_SECONDS", 90)
        gs = gpus()
        if not gs or secs <= 0 or not self.cfgs:
            return None, None
        g = gs[0]
        limit, maxclk = g.get("power.limit") or 0, g.get("clocks.max.sm") or 0
        log("gpu : %s, limite %.0f W, horloge max %.0f MHz, pilote %s"
            % (g.get("name"), limit, maxclk, g.get("driver_version")))
        try:  # tenter de lever un eventuel verrou de frequence pose par l'hote
            rc = subprocess.run(["nvidia-smi", "-rgc"], capture_output=True, text=True, timeout=20).returncode
            log("horloges : remise a zero %s" % ("acceptee" if rc == 0 else "refusee par l'hote (code %d)" % rc))
        except (OSError, subprocess.TimeoutExpired):
            pass
        idle = []
        for _ in range(3):
            idle += [x.get("clocks.sm") or 0 for x in gpus()]
            time.sleep(0.5)
        idle_clk = max(idle) if idle else 0

        scores, best, bestv = {}, None, None
        for key in list(self.cfgs):
            try:
                cfg = self.cfgs[key].prepare()
            except Exception as e:
                log("%s indisponible : %s" % (key, e))
                continue
            try:
                self.use_pools_for(cfg.name)
                self.cur, self.run = key, Run(cfg, self.pool())
            except Exception as e:
                log("preflight %s : lancement impossible (%s)" % (key, e))
                continue
            # WildRig publie une moyenne par minute : sa premiere mesure suffit. Les mineurs
            # CUDA affichent un taux toutes les quelques secondes, qui monte en charge : on
            # les laisse se stabiliser avant de conclure.
            settle = MINERS[cfg.name].get("settle", 30 if cfg.name != "wildrig" else 5)
            r, load, pwr, vue = self.run, [], [], None
            while time.time() < r.t0 + secs and r.alive():
                time.sleep(2)
                for x in self.sample():
                    load.append(x.get("clocks.sm") or 0)
                    pwr.append(x.get("power.draw") or 0)
                if r.hr_median(r.t0) is not None:
                    # WildRig ne publie son tableau qu'une fois par minute : on attend
                    # la premiere mesure au lieu de couper a l'aveugle a 50 s.
                    vue = vue or time.time()
                    if time.time() - vue >= settle:
                        break
            hr, acc, rej = r.hr_median(r.t0 + 10), r.acc, r.rej
            alive, tail = r.alive(), list(r.tail)[-3:]
            r.stop()
            self.run = None
            fee = miner_fee(cfg.name, self.pool())
            net = round((hr or 0) * (1 - fee), 3)
            lc = statistics.median(load) if load else 0
            pc = statistics.median(pwr) if pwr else 0
            scores[key] = {"th": hr, "net": net, "fee": fee, "version": cfg.version,
                           "acc": acc, "rej": rej, "util": None, "t": time.time(),
                           "pool": self.pool()["hostport"], "pool_factor": round(pool_factor(self.pool()), 4)}
            log("preflight %-10s %ds : %s TH/s, %.0f W sur %.0f W, %.0f MHz sur %.0f MHz (repos %.0f), shares %d/%d"
                % (key, secs, "%.1f" % hr if hr else "aucun", pc, limit, lc, maxclk, idle_clk, acc, rej))
            if not alive and not hr:
                log("preflight %s : le mineur s'est arrete (code %s) ; dernieres lignes : %s"
                    % (key, r.p.returncode, " | ".join(tail)))
            rank = lambda k: scores[k]["net"] * scores[k]["pool_factor"]
            if best is None or rank(key) > rank(best):
                best = key
                bestv = (gpu_verdict(idle_clk, lc, maxclk, pc, limit, hr), lc, pc)
        if best is None:
            return None, None

        v, lc, pc = bestv
        if v == "fige":
            log("preflight : FREQUENCES FIGEES a %.0f MHz (%.0f%% du maximum) et %.0f W sur %.0f W : "
                "l'hote verrouille la carte, aucun reglage dans le conteneur n'y changera rien"
                % (lc, 100 * lc / maxclk if maxclk else 0, pc, limit))
        elif v == "sous-alimente":
            log("preflight : GPU SOUS-ALIMENTE (%.0f W sur %.0f W, %.0f MHz sur %.0f) : "
                "verifier le partage du GPU et la limite de puissance chez l'hote" % (pc, limit, lc, maxclk))
        elif v == "muet":
            log("preflight : AUCUN HASHRATE lu en %ds" % secs)
        elif len(scores) > 1:
            log("preflight : GPU conforme, kernel retenu %s" % best)
        else:
            log("preflight : GPU conforme")
        return v, {"scores": scores, "best": best}

    def ensure_screened(self, force=False):
        fp, gs = fingerprint(list(self.cfgs))
        self.state["gpus"] = [g.get("name") for g in gs]
        fresh = (self.state.get("fp") == fp and self.state.get("scores")
                 and time.time() - self.state.get("screened_at", 0) < CACHE_HOURS * 3600)
        if fresh and not force:
            log("benchmark en cache reutilise (meme GPU, driver et candidats)")
            return
        self.state["scores"] = {}
        self.screen(list(self.cfgs), "initial" if not force else "force")
        self.state.update({"fp": fp, "screened_at": time.time()})
        save_json(STATE_F, self.state)

    # ---- mises a jour
    def check_updates(self, only=None):
        names = sorted({c.name for k, c in self.cfgs.items() if only is None or c.name == only})
        changed = []
        for n in names:
            keys = [k for k in self.cfgs if self.cfgs[k].name == n]
            old = self.state.get("scores", {}).get(keys[0], {}).get("version")
            try:
                v, _, _ = resolve_release(n)
            except Exception:
                continue
            if old and v != old and _vkey(v) > _vkey(old):
                log("nouvelle version %s : %s -> %s" % (n, old, v))
                changed += keys
        if changed:
            cur = self.cur
            self.screen(changed + ([cur] if cur not in changed else []), "mise a jour")
            self.state["screened_at"] = time.time()
            save_json(STATE_F, self.state)
            self.start(self.best())
        return bool(changed)

    # ---- garde-fou rentabilite
    def guard_tick(self):
        if not MAX_COST_PER_PRL or time.time() - self.guard["last"] < GUARD_INTERVAL:
            return
        self.guard["last"] = time.time()
        sc = self.state.get("scores", {}).get(self.cur, {})
        th = self.run.hr_median(time.time() - 1800) if self.run else None
        th_net = (th or sc.get("th") or 0) * (1 - (sc.get("fee") or 0))
        rate, dph = prl_per_hour(th_net), cost_per_hour()
        if not rate or not dph:
            log("garde-fou : donnees insuffisantes, aucune action")
            return
        cpp = dph / rate
        self.guard.update({"cost_per_prl": round(cpp, 3), "dph": dph, "prl_h": round(rate, 4)})
        limit = float(MAX_COST_PER_PRL)
        if cpp > limit:
            self.guard["strikes"] += 1
            log("garde-fou : %.2f $/PRL > %.2f (%d/%d)" % (cpp, limit, self.guard["strikes"], GUARD_STRIKES))
        else:
            self.guard["strikes"] = 0
        if self.guard["strikes"] >= GUARD_STRIKES:
            if GUARD_ACTION == "stop" and env("CONTAINER_ID"):
                log("garde-fou : plus rentable, arret de l'instance %s" % env("CONTAINER_ID"))
                self.write_status()
                out = vastai("stop", "instance", env("CONTAINER_ID"))
                log("vastai stop : %s %s" % (out.returncode, (out.stdout + out.stderr).strip()[:200]))
            else:
                log("garde-fou : seuil depasse (GUARD_ACTION=%s, pas d'arret)" % GUARD_ACTION)
            self.guard["strikes"] = 0

    # ---- statut
    def write_status(self, gs=None):
        r = self.run
        sc = self.state.get("scores", {})
        now = time.time()
        th5 = r.hr_median(now - 300) if r else None
        net = th5 * (1 - (sc.get(self.cur, {}).get("fee") or 0)) if th5 else None
        rr, nrr = r.reject_ratio() if r else (0, 0)
        st = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "config": self.cur,
            "controller_version": CTL_VERSION,
            "instance_id": INSTANCE_ID, "worker": WORKER, "host": socket.gethostname(),
            "wallet": (WALLET[:12] + "…") if WALLET else None,
            "version": self.cfgs[self.cur].version if self.cur in self.cfgs else None,
            "pool": self.pool()["hostport"] + (" (tls)" if self.pool()["tls"] else "") if self.pools else None,
            "pool_rtt_ms": self.pool().get("rtt") if self.pools else None,
            "uptime_min": round(r.age() / 60, 1) if r else 0,
            "th_5min": round(th5, 2) if th5 else None, "th_net": round(net, 2) if net else None,
            # Ce qui rapporte vraiment : le hashrate net moins les shares rejetees.
            "th_effective": round(net * (1 - rr), 2) if net else None,
            "reject_ratio_1h": round(rr, 3),
            "baseline_net": sc.get(self.cur, {}).get("net"),
            "prl_h_est": round(prl_per_hour(net), 4) if net and network() else None,
            "shares": {"acc": r.acc if r else 0, "rej": r.rej if r else 0, "reject_ratio_1h": round(rr, 3),
                       "last_accept_s": round(now - r.last_acc) if r and r.last_acc else None},
            "throttle_pct": round(100 * sum(self.throttle) / len(self.throttle), 1) if self.throttle else None,
            "gpus": gs if gs is not None else gpus(), "scores": sc, "guard": self.guard,
            "bad": {k: round((now - t) / 60) for k, t in self.bad.items()},
        }
        save_json(STATUS_F, st)

    # ---- boucle principale
    def loop(self):
        if not WALLET:
            log("PRL_WALLET manquant, arret")
            return 1
        os.makedirs(HOME, exist_ok=True)
        if not gpus():
            log("nvidia-smi ne repond pas : pas de GPU visible, nouvel essai dans 60 s")
            time.sleep(60)
            if not gpus():
                return 1
        self.apply_oc()
        # serveurs du premier candidat : un seul classement quand tous partagent les memes
        self.pool_key, self.pools = None, []
        self.use_pools_for(next(iter(self.cfgs.values())).name if self.cfgs else None)
        force = env("FORCE_RESCREEN") == "1"
        if len(self.cfgs) == 1 and not force:
            # Rien a comparer : le preflight couterait un arret et une relance du mineur (~10 s
            # sans hashrate), ce qui pese sur des noeuds interrompus toutes les 10 minutes (Salad Batch).
            log("un seul mineur candidat (%s) : demarrage direct, sans preflight ni benchmark" % next(iter(self.cfgs)))
        else:
            verdict, info = (None, None) if force else self.preflight()
            if info:
                self.state["scores"] = info["scores"]
                fp, gs = fingerprint(list(self.cfgs))
                self.state.update({"fp": fp, "screened_at": time.time(), "gpus": [x.get("name") for x in gs]})
                save_json(STATE_F, self.state)
                log("pas de benchmark long : la mesure du preflight fait foi (%s, %.1f TH/s net)"
                    % (info["best"], info["scores"][info["best"]]["net"]))
            else:
                self.ensure_screened(force=force)
        self.start(self.best())
        log("choix : %s (scores %s)" % (self.cur, {k: v.get("net") for k, v in self.state.get("scores", {}).items()}))
        last_status = 0
        flag = os.path.join(HOME, "rescreen.flag")
        while not self.stopping:
            time.sleep(TICK)
            if os.path.exists(flag):
                os.remove(flag)
                self.ensure_screened(force=True)
                self.start(self.best())
                continue
            gs = self.sample()
            r = self.run
            now = time.time()
            if not r.alive():
                self.restart("mineur arrete, code %s" % r.p.returncode, rotate=len(self.restarts) % 2 == 1)
                continue
            if r.age() > GRACE:
                u = self.util_mean(UTIL_WINDOW, since=r.t0)
                if u is not None and u < UTIL_MIN:
                    self.restart("GPU a %.0f%% d'utilisation" % u, rotate=True)
                    continue
                if r.last_acc and now - r.last_acc > SHARE_TIMEOUT:
                    self.restart("aucune share acceptee depuis %d min" % ((now - r.last_acc) / 60), rotate=True)
                    continue
                rr, n = r.reject_ratio()
                if n >= 15 and rr > REJECT_MAX:
                    log("taux de rejet %.0f%% sur %d shares" % (rr * 100, n))
                    if not self.check_updates(only=self.cfgs[self.cur].name):
                        self.bad[self.cur] = now
                        self.start(self.best())
                    continue
                base = self.state.get("scores", {}).get(self.cur, {}).get("th")
                th10 = r.hr_median(now - 600)
                if base and th10 and r.age() > 900 and th10 < DEGRADE * base and now - self.last_degrade > 3600:
                    self.last_degrade = now
                    tp = 100 * sum(self.throttle) / len(self.throttle) if self.throttle else 0
                    self.restart("hashrate %.1f < %.0f%% du benchmark (%.1f), throttling thermique %.0f%% du temps"
                                 % (th10, DEGRADE * 100, base, tp))
                    continue
            if now - self.last_update > UPDATE_HOURS * 3600:
                self.last_update = now
                self.check_updates()
                self.rerank()
            self.guard_tick()
            if now - last_status > 30:
                last_status = now
                self.write_status(gs)
        return 0

    def shutdown(self, *_):
        self.stopping = True
        if self.run:
            self.run.stop()
        self.restore_oc()
        log("arret du controleur")
        sys.exit(0)


# ------------------------------------------------------------------ CLI --
def print_status(as_json=False):
    st = load_json(STATUS_F, None)
    if as_json:
        print(json.dumps(st if st else {"error": "pas encore de statut"}, ensure_ascii=False))
        return
    if not st:
        print("Pas encore de statut (benchmark en cours ?). Logs : tail -f %s" % LOGF)
        return
    p = print
    p("Pearl - %s%s" % (st["time"], ("  (instance %s)" % st["instance_id"]) if st.get("instance_id") else ""))
    p("  Mineur   : %s %s   |   serveur %s (%s ms)" % (st["config"], st["version"], st["pool"],
                                                      "%.0f" % st["pool_rtt_ms"] if st.get("pool_rtt_ms") else "?"))
    p("  Hashrate : %s TH/s brut, %s TH/s net, %s TH/s utile (rejets deduits)   |   en marche depuis %s min" % (
        st["th_5min"], st["th_net"], st.get("th_effective"), st["uptime_min"]))
    if st.get("prl_h_est"):
        p("  Estime   : %.4f PRL/h, %.2f PRL/jour (difficulte actuelle, frais pool inclus)" % (
            st["prl_h_est"], st["prl_h_est"] * 24))
    s = st["shares"]
    p("  Shares   : %s acceptees, %s rejetees (rejet 1 h : %.1f%%), derniere : %s" % (
        s["acc"], s["rej"], 100 * s["reject_ratio_1h"],
        "il y a %s s" % s["last_accept_s"] if s["last_accept_s"] is not None else "aucune encore"))
    p("  Throttling thermique : %s%% du temps (si eleve : hote mal refroidi, changer de machine)" % st.get("throttle_pct"))
    for g in st.get("gpus") or []:
        p("  GPU%s %s : %s%% util, %s/%s W, %s C, %s/%s MHz%s" % (
            int(g.get("index") or 0), g.get("name"), g.get("utilization.gpu"), g.get("power.draw"),
            g.get("power.limit"), g.get("temperature.gpu"), g.get("clocks.sm"), g.get("clocks.max.sm"),
            ("  THERMIQUE" if g.get("thermal") else "") + ("  limite-W" if g.get("power_capped") else "")))
    p("  Benchmark (TH/s net, apres dev fee) :")
    for k, v in sorted(st.get("scores", {}).items(), key=lambda kv: -(kv[1].get("net") or 0)):
        p("    %-14s %-10s net %6s  brut %6s  fee %s  shares %s/%s%s" % (
            k, v.get("version"), v.get("net"), "%.1f" % v["th"] if v.get("th") else "?",
            v.get("fee"), v.get("acc"), v.get("rej"), "  ERREUR " + v["error"] if v.get("error") else ""))
    g = st.get("guard") or {}
    if g.get("cost_per_prl"):
        p("  Garde-fou : %.3f $/PRL (instance %.3f $/h), alertes %s/%s" % (
            g["cost_per_prl"], g["dph"], g["strikes"], GUARD_STRIKES))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "status":
        return print_status("--json" in sys.argv[2:])
    os.makedirs(HOME, exist_ok=True)
    lock = open(os.path.join(HOME, "controller.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if mode == "rescreen":
            open(os.path.join(HOME, "rescreen.flag"), "w").close()
            print("benchmark demande au controleur en cours (voir le log)")
        else:
            print("controleur deja en cours d'execution")
        return 0
    if mode == "rescreen":
        os.environ["FORCE_RESCREEN"] = "1"
    while True:
        c = Controller()
        signal.signal(signal.SIGTERM, c.shutdown)
        signal.signal(signal.SIGINT, c.shutdown)
        log("controleur demarre : wallet %s..., worker %s, candidats %s" % (WALLET[:12], WORKER, ", ".join(c.cfgs)))
        try:
            return c.loop()
        except Exception:
            import traceback
            log("erreur inattendue, redemarrage du controleur dans 30 s :\n" + traceback.format_exc())
            if c.run:
                c.run.stop()
            time.sleep(30)
            os.environ.pop("FORCE_RESCREEN", None)


if __name__ == "__main__":
    sys.exit(main() or 0)

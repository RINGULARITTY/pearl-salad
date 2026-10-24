#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pearl (PRL) sur SaladCloud - verifications du noeud, sante, reallocation (stdlib uniquement).

Un noeud Salad est le PC d'un particulier : carte bridee par son proprietaire (limite de
puissance a 60 %), refroidissement insuffisant, reseau qui coupe le pool... Le noeud est
loue a la seconde : mieux vaut le rendre vite que miner a moitie. Cet agent tourne a cote
de pearl_controller.py dans le conteneur :

  boot    avant le controleur : GPU visible, limite de puissance fixee par l'hote,
          pool joignable. Un echec net -> reallocation immediate (IMDS).
  watch   en continu : hashrate mesure par le controleur / reference de la carte,
          temperature, shares. Un defaut qui dure -> reallocation. Le cout de
          suppression (deletion cost) suit le hashrate : si tu reduis le nombre de
          replicas, Salad retire d'abord les noeuds les plus lents.
  report  une ligne JSON (statut du controleur + sante + identite Salad + fin du journal),
          lue en SSH par le tableau de bord (pearl_dashboard.py). La surveillance ecrit aussi
          ce rapport chaque minute sur la sortie standard : Salad le stocke dans ses logs et le
          tableau de bord le relit par l'API, meme sur un noeud sans acces SSH.
  probe   code 0 si le noeud mine ou se prepare, 1 sinon (sonde liveness optionnelle).

Variables (toutes optionnelles) :
  HEALTH_ACTION      reallocate (defaut) | warn : warn journalise sans rendre le noeud
  POWER_CAP_MIN      0.70  : limite de puissance minimale, en fraction de celle d'usine
  HEALTH_MIN_RATIO   0.75  : hashrate minimal, en fraction de la valeur typique de la carte
  HASHRATE_MIN       plancher en TH/s pour tout le groupe (remplace la table ; 0 = pas de plancher)
  REF_FACTOR_<MINEUR> rendement du mineur par rapport a la table (defaut 0.8 pour pearlminer, 1 sinon)
  HEALTH_WARMUP_MIN  5     : minutes de minage avant de juger
  HEALTH_GRACE       3     : verifications defavorables d'affilee avant d'agir
  HEALTH_INTERVAL    60    : secondes entre deux verifications
  TEMP_MAX           90    : degres au-dela desquels une carte lente est jugee en surchauffe
  BOOT_TIMEOUT_MIN   20    : minutes sans statut du controleur avant d'abandonner le noeud
  SHARE_STALL_MIN    60    : minutes sans share acceptee (avec du hashrate) avant d'abandonner
"""
import json, os, re, socket, statistics, subprocess, sys, time, urllib.error, urllib.request

AGENT_VERSION = "1.1.0"


def env(k, d=None):
    v = os.environ.get(k, "").strip()
    return v if v else d


def envf(k, d):
    try:
        return float(env(k, d))
    except (TypeError, ValueError):
        return float(d)


HOME = env("PEARL_HOME", "/opt/pearl")
LOGF = env("PEARL_LOG", "/var/log/pearl-miner.log")
STATUS_F = os.path.join(HOME, "status.json")
SALAD_F = os.path.join(HOME, "salad.json")
REF_F = env("REF_FILE", os.path.join(HOME, "gpus.json"))
IMDS = env("SALAD_IMDS", "http://169.254.169.254")

ACTION = env("HEALTH_ACTION", "reallocate")
POWER_CAP_MIN = envf("POWER_CAP_MIN", 0.70)
MIN_RATIO = envf("HEALTH_MIN_RATIO", 0.75)
WARMUP_MIN = envf("HEALTH_WARMUP_MIN", 5)
GRACE = int(envf("HEALTH_GRACE", 3))
INTERVAL = envf("HEALTH_INTERVAL", 60)
TEMP_MAX = envf("TEMP_MAX", 90)
BOOT_TIMEOUT_MIN = envf("BOOT_TIMEOUT_MIN", 20)
SHARE_STALL_MIN = envf("SHARE_STALL_MIN", 60)
DEFAULT_POOLS = "pool.pearlhash.xyz:9000,hk.pearlhash.net:9000,global.pearlhash.net:9000,global2.pearlhash.net:9000"

# Portables : absents des tables publiques, valeurs relevees sur des noeuds SaladCloud
# (depot public pearl-salad-nvidia, septembre 2026). Un portable tient ~86 C par conception.
EXTRA_REFS = [
    {"name": "RTX 5090 Laptop", "th": 120, "lo": 110, "hi": 128, "conf": "faible"},
    {"name": "RTX 5080 Laptop", "th": 124, "lo": 117, "hi": 127, "conf": "faible"},
    {"name": "RTX 4090 Laptop", "th": 120, "lo": 110, "hi": 140, "conf": "faible"},
    {"name": "RTX 4080 Laptop", "th": 110, "lo": 99, "hi": 125, "conf": "faible"},
    {"name": "RTX 5070 Ti Laptop", "th": 89, "lo": 80, "hi": 95, "conf": "faible"},
]


# ---------------------------------------------------------------- outils --
def log(msg, tag="salad"):
    line = "%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag, msg)
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        sys.stderr.write(line)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def fnum(v):
    try:
        f = float(str(v).strip().split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return f


def on_salad():
    return bool(env("SALAD_MACHINE_ID") or env("SALAD_CONTAINER_GROUP_ID"))


def salad_ids():
    return {k[6:].lower(): env(k) for k in ("SALAD_MACHINE_ID", "SALAD_INSTANCE_ID", "SALAD_CONTAINER_GROUP_ID",
                                            "SALAD_CONTAINER_GROUP_NAME", "SALAD_PROJECT_NAME",
                                            "SALAD_ORGANIZATION_NAME") if env(k)}


# ------------------------------------------------------------------ IMDS --
def imds(method, path, body=None, timeout=10):
    """Appel a l'Instance Metadata Service de Salad. Retourne (code HTTP, corps) ;
    code 0 si le service est injoignable (hors SaladCloud)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(IMDS + path, data=data, method=method,
                                 headers={"Metadata": "true", "Content-Type": "application/json"})
    # pas de proxy : l'IMDS refuse les requetes relayees
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except (OSError, ValueError) as e:
        return 0, str(e)


def reallocate(reason, state):
    """Rend le noeud a Salad, qui recree le conteneur ailleurs. Le noeud quitte est exclu
    un temps du groupe : on ne le fait que sur un defaut mesure, avec la mesure dans la raison."""
    reason = reason[:1000]
    log("=== NOEUD REFUSE : %s ===" % reason)
    if state.get("realloc"):
        return True
    state["realloc"] = {"at": time.time(), "reason": reason, "action": ACTION, "http": None}
    if ACTION != "reallocate":
        log("HEALTH_ACTION=%s : noeud conserve, simple avertissement" % ACTION)
        state["realloc"]["done"] = False
        return False
    if not on_salad():
        log("hors SaladCloud (pas de SALAD_MACHINE_ID) : aucune reallocation possible")
        state["realloc"]["done"] = False
        return False
    code, body = 0, ""
    for i in range(3):
        code, body = imds("POST", "/v1/reallocate", {"reason": reason})
        if 200 <= code < 300:
            break
        time.sleep(3 * (i + 1))
    state["realloc"].update({"http": code, "done": 200 <= code < 300})
    log("reallocation %s (HTTP %s%s)" % ("acceptee : Salad va arreter ce conteneur" if 200 <= code < 300 else "refusee",
                                          code, ", " + body[:160] if body and not 200 <= code < 300 else ""))
    return 200 <= code < 300


def set_deletion_cost(cost):
    if not on_salad():
        return None
    code, _ = imds("PUT", "/v1/deletion-cost", {"deletion_cost": int(max(0, min(2_000_000_000, cost)))})
    return code


# ---------------------------------------------------------------- GPU ----
GPU_FIELDS = ["index", "name", "memory.total", "driver_version", "power.draw", "power.limit",
              "power.default_limit", "clocks.sm", "clocks.max.sm", "temperature.gpu", "utilization.gpu"]


def query_gpus():
    """nvidia-smi ; une colonne non supportee par le pilote WSL vaut None."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS), "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    res = []
    for line in out.stdout.strip().splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) != len(GPU_FIELDS):
            continue
        g = {}
        for k, v in zip(GPU_FIELDS, vals):
            if v in ("[N/A]", "N/A", "[Not Supported]", ""):
                g[k] = None
            elif k in ("name", "driver_version"):
                g[k] = v
            else:
                g[k] = fnum(v)
        res.append(g)
    return res


def is_laptop(name):
    return bool(re.search(r"laptop|mobile|max-q", name or "", re.I))


def power_cap(g):
    """Fraction de la limite d'usine laissee par l'hote, ou None si le pilote ne la publie pas."""
    lim, dft = g.get("power.limit"), g.get("power.default_limit")
    if not lim or not dft or dft <= 0:
        return None
    return lim / dft


# --------------------------------------------------------- reference -----
MODIFIERS = {"TI", "SUPER", "D", "V2", "LAPTOP", "MOBILE", "MAX-Q", "MAXQ"}


def gpu_tokens(name):
    s = str(name or "").upper().replace("GEFORCE", " ").replace("NVIDIA", " ")
    s = re.sub(r"\(.*?\)", " ", s)                     # "(24 GB)" des classes Salad
    s = re.sub(r"\bGPU\b", " ", s)
    return [t for t in re.split(r"[\s_]+", s) if t]


def ref_for(name, table):
    """Ligne de reference pour un GPU nvidia-smi ("NVIDIA GeForce RTX 4070 Ti SUPER").
    La plus longue sequence de mots qui correspond, a condition que le mot suivant ne soit
    pas une variante : "RTX 4070 Ti" n'est pas une "RTX 4070", ni un portable une carte de bureau."""
    toks = gpu_tokens(name)
    best = None
    for row in table:
        rt = gpu_tokens(row.get("name"))
        if not rt:
            continue
        for i in range(len(toks) - len(rt) + 1):
            if toks[i:i + len(rt)] != rt:
                continue
            nxt = toks[i + len(rt)] if i + len(rt) < len(toks) else None
            if nxt in MODIFIERS:
                continue
            if best is None or len(rt) > len(gpu_tokens(best.get("name"))):
                best = row
    return best


def load_refs():
    t = load_json(REF_F, []) or []
    return [r for r in t if isinstance(r, dict) and r.get("name") and r.get("th")] + EXTRA_REFS


# La table vient de WildRig, SRBMiner et BzMiner. pearl-miner, mineur historique du pool,
# n'a pas de mesure publique sur cartes grand public : 63-65 TH/s sur une RTX 5060 Ti
# partagee avec un autre mineur (WSL2, 24/10/2026), soit au moins 0,7 de la valeur typique.
# 0,8 garde le plancher a 60 % de la table : il n'ecarte que les noeuds nettement en defaut.
# A recaler sur la mediane de ta flotte (onglet Salad du tableau de bord) : REF_FACTOR_PEARLMINER.
REF_FACTORS = {"pearlminer": 0.8}


def ref_factor(config):
    name = str(config or "").split(":")[0]
    return envf("REF_FACTOR_" + name.upper(), REF_FACTORS.get(name, 1.0)) if name else 1.0


def floor_for(gpus_, refs, config=None):
    """(reference, plancher en TH/s pour le noeud entier, source)."""
    if env("HASHRATE_MIN") is not None:
        v = envf("HASHRATE_MIN", 0)
        return None, (v if v > 0 else None), "HASHRATE_MIN"
    if not gpus_:
        return None, None, "aucun GPU"
    ref = ref_for(gpus_[0].get("name"), refs)
    if not ref:
        return None, None, "carte absente de la table"
    f = ref_factor(config)
    src = "%.0f %% de %s" % (MIN_RATIO * 100, ref["name"]) + (" x %.2f (%s)" % (f, str(config).split(":")[0]) if f != 1 else "")
    return ref, round(MIN_RATIO * f * float(ref["th"]) * len(gpus_), 1), src


# ---------------------------------------------------------------- pool ---
def pool_endpoints():
    out = []
    for p in (env("POOLS") or DEFAULT_POOLS).split(","):
        p = p.strip()
        if not p:
            continue
        p = p.split("://", 1)[-1]
        host, _, port = p.rpartition(":")
        if host and port.isdigit():
            out.append((host, int(port)))
            if "pearlhash" in host and port == "9000":
                out.append((host, 9443))
    return out


def pool_reachable(timeout=5):
    """Premier serveur du pool qui accepte une connexion TCP, ou None."""
    for host, port in pool_endpoints():
        t = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return "%s:%d (%.0f ms)" % (host, port, (time.monotonic() - t) * 1000)
        except OSError:
            continue
    return None


# --------------------------------------------------------------- boot ----
def cmd_boot():
    state = load_json(SALAD_F, {}) or {}
    state.update({"agent_version": AGENT_VERSION, "salad": salad_ids(), "boot": {"at": time.time(), "checks": []}})
    checks = state["boot"]["checks"]

    def check(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})
        log("verification %-10s %s : %s" % (name, "OK" if ok else ("ECHEC" if ok is False else "--"), detail))

    gs = []
    for i in range(3):
        gs = query_gpus()
        if gs:
            break
        time.sleep(10)
    if not gs:
        check("gpu", False, "nvidia-smi ne voit aucun GPU")
        done = reallocate("nvidia-smi ne voit aucun GPU NVIDIA", state)
        state["boot"]["verdict"] = "refuse"
        save_json(SALAD_F, state)
        return 2 if done else 1  # sans GPU, rien a miner, meme en mode warn
    g = gs[0]
    state["gpu"] = {"name": g.get("name"), "count": len(gs), "memory_mb": g.get("memory.total"),
                    "driver": g.get("driver_version"), "power_limit_w": g.get("power.limit"),
                    "power_default_w": g.get("power.default_limit"), "max_sm_mhz": g.get("clocks.max.sm")}
    check("gpu", True, "%d x %s, %s Mo, pilote %s" % (len(gs), g.get("name"), g.get("memory.total"), g.get("driver_version")))

    refs = load_refs()
    ref, floor, src = floor_for(gs, refs, (env("CANDIDATES") or "").split(",")[0])
    state["ref"] = {"name": ref.get("name") if ref else None, "th": ref.get("th") if ref else None,
                    "lo": ref.get("lo") if ref else None, "hi": ref.get("hi") if ref else None,
                    "floor": floor, "src": src, "count": len(gs)}
    check("reference", None, "%s TH/s typiques, plancher %s TH/s (%s)" % (
        ref.get("th") if ref else "?", floor if floor else "aucun", src))

    bad = None
    cap = power_cap(g)
    if cap is None:
        check("puissance", None, "limite non publiee par le pilote%s" % (" (portable)" if is_laptop(g.get("name")) else ""))
    elif is_laptop(g.get("name")):
        check("puissance", None, "portable : %.0f W sur %.0f W, pas de seuil" % (g["power.limit"], g["power.default_limit"]))
    else:
        ok = cap >= POWER_CAP_MIN
        check("puissance", ok, "%.0f W sur %.0f W d'usine (%.0f %%, minimum %.0f %%)" % (
            g["power.limit"], g["power.default_limit"], cap * 100, POWER_CAP_MIN * 100))
        if not ok:
            bad = "carte bridee par l'hote : limite %.0f W sur %.0f W (%.0f %%)" % (
                g["power.limit"], g["power.default_limit"], cap * 100)

    if not bad:
        hit = pool_reachable()
        check("pool", bool(hit), hit or "aucun serveur ne repond (%s)" % ", ".join("%s:%d" % e for e in pool_endpoints()))
        if not hit:
            bad = "pool injoignable depuis ce noeud (%s)" % ", ".join("%s:%d" % e for e in pool_endpoints()[:4])

    state["boot"]["verdict"] = "refuse" if bad else "accepte"
    save_json(SALAD_F, state)
    if bad:
        done = reallocate(bad, state)
        save_json(SALAD_F, state)
        # warn ou hors Salad : on mine quand meme, l'avertissement reste visible
        return 2 if done else 0
    return 0


# ------------------------------------------------------------- watch -----
def judge(st, gpus_, floor, ref, now, mtime):
    """Verdict d'une verification : (etat, detail). etat parmi
    attente | chauffe | conforme | lent | thermique | muet | sans-share | fige."""
    if not st:
        return "attente", "pas encore de statut du controleur (preflight des mineurs)"
    up = fnum(st.get("uptime_min")) or 0
    if now - mtime > 300:
        return "fige", "statut du controleur vieux de %d s" % (now - mtime)
    if up < WARMUP_MIN:
        return "chauffe", "mineur %s lance depuis %.1f min" % (st.get("config"), up)
    th = fnum(st.get("th_5min"))
    temps = [g.get("temperature.gpu") for g in gpus_ if g.get("temperature.gpu") is not None]
    tmax = max(temps) if temps else None
    if not th:
        return "muet", "aucun hashrate lu depuis %.0f min" % up
    expected = float(ref["th"]) * max(1, len(gpus_)) if ref else None
    if floor and th < floor:
        g = gpus_[0] if gpus_ else {}
        clk, mx, w, lim = g.get("clocks.sm"), g.get("clocks.max.sm"), g.get("power.draw"), g.get("power.limit")
        why = "%.1f TH/s < plancher %.1f" % (th, floor)
        if clk and mx:
            why += ", %.0f/%.0f MHz" % (clk, mx)
        if w and lim:
            why += ", %.0f/%.0f W" % (w, lim)
        if tmax is not None:
            why += ", %.0f C" % tmax
        if tmax is not None and tmax >= TEMP_MAX:
            return "thermique", why
        return "lent", why
    sh = st.get("shares") or {}
    last = sh.get("last_accept_s")
    silent = str(st.get("config") or "").split(":")[0] in SILENT_MINERS
    if not silent and ((last is None and up > SHARE_STALL_MIN) or (last is not None and last > SHARE_STALL_MIN * 60)):
        return "sans-share", "%.1f TH/s mais aucune share acceptee depuis %s" % (
            th, "le demarrage (%.0f min)" % up if last is None else "%d min" % (last / 60))
    ratio = th / expected if expected else None
    return "conforme", "%.1f TH/s%s" % (th, " (%.0f %% de la valeur typique)" % (ratio * 100) if ratio else "")


BAD = {"lent", "thermique", "muet", "sans-share"}
SILENT_MINERS = set()  # mineurs qui n'affichent pas leurs shares (pearl-miner ecrit "Solution accepted!")


def cmd_watch(once=False):
    state = load_json(SALAD_F, {}) or {}
    state.setdefault("salad", salad_ids())
    refs = load_refs()
    started = time.time()
    strikes, last_verdict, last_cost = 0, None, None
    hist = state.setdefault("health_history", [])
    while True:
        now = time.time()
        st = load_json(STATUS_F, None)
        try:
            mtime = os.path.getmtime(STATUS_F)
        except OSError:
            mtime = now
        gs = query_gpus() or (st or {}).get("gpus") or []
        ref, floor, src = floor_for(gs, refs, (st or {}).get("config") or (env("CANDIDATES") or "").split(",")[0])
        verdict, detail = judge(st, gs, floor, ref, now, mtime)
        if verdict == "attente" and now - started > BOOT_TIMEOUT_MIN * 60:
            verdict, detail = "muet", "aucun statut du controleur en %d min" % BOOT_TIMEOUT_MIN
        strikes = strikes + 1 if verdict in BAD and verdict == last_verdict else (1 if verdict in BAD else 0)
        if verdict != last_verdict:
            log("sante : %s - %s" % (verdict, detail))
        last_verdict = verdict
        th = fnum((st or {}).get("th_5min"))
        g0 = gs[0] if gs else {}
        hist.append({"t": round(now), "v": verdict, "th": th, "temp": g0.get("temperature.gpu"),
                     "w": g0.get("power.draw"), "lim": g0.get("power.limit"), "clk": g0.get("clocks.sm")})
        del hist[:-60]
        state["health"] = {"verdict": verdict, "detail": detail, "strikes": strikes, "grace": GRACE,
                           "floor": floor, "floor_src": src, "ref": ref.get("name") if ref else None,
                           "ref_th": float(ref["th"]) * max(1, len(gs)) if ref else None,
                           "ratio": round(th / (float(ref["th"]) * max(1, len(gs))), 3) if th and ref else None,
                           "checked_at": now, "config": (st or {}).get("config")}
        state["updated_at"] = now
        # Le noeud le plus rapide est le plus cher a supprimer : en reduisant les replicas,
        # Salad retire d'abord les plus lents.
        cost = int(round((fnum((st or {}).get("th_effective")) or 0) * 10))
        if cost != last_cost and verdict not in ("attente", "chauffe"):
            if set_deletion_cost(cost) in (200, 204):
                state["deletion_cost"] = cost
            last_cost = cost
        if verdict in BAD and strikes >= GRACE and not state.get("realloc"):
            reallocate("%s : %s (%d verifications d'affilee, %s)" % (verdict, detail, strikes, src), state)
        save_json(SALAD_F, state)
        if env("LOG_REPORT", "1") == "1":
            emit_report(state, st, round(now - mtime) if st else None)
        if once:
            return 0
        time.sleep(INTERVAL)


STATUS_KEYS = ("time", "config", "controller_version", "worker", "version", "pool", "pool_rtt_ms", "uptime_min",
               "th_5min", "th_net", "th_effective", "reject_ratio_1h", "prl_h_est", "shares", "throttle_pct", "gpus")


def emit_report(state, st, status_age):
    """Le rapport sur une ligne JSON de la sortie standard. Salad range une ligne JSON dans
    json_log, que le tableau de bord lit par l'API de logs : aucun SSH necessaire (certains
    noeuds n'ont pas d'acces terminal)."""
    node = {k: state.get(k) for k in ("agent_version", "gpu", "ref", "health", "realloc", "deletion_cost")}
    boot = state.get("boot") or {}
    node["boot"] = {"verdict": boot.get("verdict"), "checks": boot.get("checks")}
    rep = {"pearl": "report", "schema": 1, "agent_version": AGENT_VERSION, "at": round(time.time()),
           "salad": salad_ids(), "status": {k: st.get(k) for k in STATUS_KEYS} if st else None,
           "node": node, "status_age_s": status_age}
    try:
        sys.stdout.write(json.dumps(rep, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------ report -----
def tail(path, n=15):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return [l[:240] for l in lines[-n:]]
    except OSError:
        return []


def cmd_report():
    """Une ligne JSON : ce que le tableau de bord lit en SSH."""
    out = {"schema": 1, "agent_version": AGENT_VERSION, "at": time.time(), "salad": salad_ids(),
           "status": load_json(STATUS_F, None), "node": load_json(SALAD_F, None), "log_tail": tail(LOGF)}
    try:
        out["status_age_s"] = round(time.time() - os.path.getmtime(STATUS_F))
    except OSError:
        out["status_age_s"] = None
    sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    return 0


def cmd_probe():
    st = load_json(SALAD_F, {}) or {}
    h = st.get("health") or {}
    if st.get("realloc", {}).get("done"):
        return 1
    if h.get("verdict") in ("fige",) and h.get("strikes", 0) >= GRACE:
        return 1
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else "report"
    os.makedirs(HOME, exist_ok=True)
    if mode == "boot":
        return cmd_boot()
    if mode == "watch":
        return cmd_watch(once="--once" in argv[2:])
    if mode == "probe":
        return cmd_probe()
    if mode == "report":
        return cmd_report()
    sys.stderr.write("usage : pearl_salad.py boot | watch | report | probe\n")
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)

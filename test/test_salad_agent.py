"""Agent Salad : table de reference, verdicts de sante, verifications de demarrage,
reallocation par l'IMDS (simulee), rapport lu en SSH. Tourne sous Windows comme sous Linux."""
import io, json, os, sys, tempfile, time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.environ["PEARL_HOME"] = tempfile.mkdtemp()
os.environ["PEARL_LOG"] = os.path.join(os.environ["PEARL_HOME"], "log")
os.environ["SALAD_MACHINE_ID"] = "eb3a4591-efc3-46c0-b06a-3d820c0ec100"
os.environ["SALAD_CONTAINER_GROUP_NAME"] = "pearl"
for k in ("HASHRATE_MIN", "HEALTH_ACTION", "POOLS"):
    os.environ.pop(k, None)
GPUS = None
for p in (os.path.join(ROOT, "vendor", "gpus.json"), os.path.join(ROOT, "..", "pearl_cloud_miner", "dashboard", "gpus.json")):
    if os.path.exists(p):
        GPUS = p
        break
os.environ["REF_FILE"] = GPUS or os.path.join(os.environ["PEARL_HOME"], "absent.json")
sys.path.insert(0, ROOT)
import pearl_salad as ps  # noqa: E402

FAIL = []


def check(nom, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + nom + ("" if cond else " (%s)" % detail))
    if not cond:
        FAIL.append(nom)


refs = ps.load_refs()
check("table de reference chargee", GPUS is not None and len(refs) > 40, (GPUS, len(refs)))
name = lambda n: (ps.ref_for(n, refs) or {}).get("name")
check("4070 Ti SUPER", name("NVIDIA GeForce RTX 4070 Ti SUPER") == "RTX 4070 Ti Super", name("NVIDIA GeForce RTX 4070 Ti SUPER"))
check("4070 Ti n'est pas une 4070", name("NVIDIA GeForce RTX 4070 Ti") == "RTX 4070 Ti", name("NVIDIA GeForce RTX 4070 Ti"))
check("4070", name("NVIDIA GeForce RTX 4070") == "RTX 4070", name("NVIDIA GeForce RTX 4070"))
check("portable 4090 : sa propre ligne", name("NVIDIA GeForce RTX 4090 Laptop GPU") == "RTX 4090 Laptop", name("NVIDIA GeForce RTX 4090 Laptop GPU"))
check("portable 3080 Ti inconnu : pas la carte de bureau", name("NVIDIA GeForce RTX 3080 Ti Laptop GPU") is None,
      name("NVIDIA GeForce RTX 3080 Ti Laptop GPU"))
check("5090 D", name("NVIDIA GeForce RTX 5090 D") == "RTX 5090 D", name("NVIDIA GeForce RTX 5090 D"))
check("classe Salad '(16 GB)'", name("RTX 5060 Ti (16 GB)") == "RTX 5060 Ti", name("RTX 5060 Ti (16 GB)"))

g4090 = {"name": "NVIDIA GeForce RTX 4090", "power.limit": 450.0, "power.default_limit": 450.0,
         "clocks.sm": 2520.0, "clocks.max.sm": 3120.0, "power.draw": 430.0, "temperature.gpu": 70.0}
ref, floor, src = ps.floor_for([g4090], refs)
check("plancher 4090 = 75 % de 308", floor == round(0.75 * 308, 1), (floor, src))
pf = ps.floor_for([g4090], refs, "pearlminer")[1]
check("pearl-miner : plancher x 0,8", pf == round(0.75 * 0.8 * 308, 1), pf)
os.environ["REF_FACTOR_PEARLMINER"] = "1"
check("REF_FACTOR_PEARLMINER=1", ps.floor_for([g4090], refs, "pearlminer")[1] == floor)
os.environ.pop("REF_FACTOR_PEARLMINER")
os.environ["HASHRATE_MIN"] = "0"
check("HASHRATE_MIN=0 : pas de plancher", ps.floor_for([g4090], refs)[1] is None)
os.environ.pop("HASHRATE_MIN")

now = time.time()
st = {"config": "pearlminer", "uptime_min": 2, "th_5min": 100, "shares": {"last_accept_s": None}}
check("chauffe", ps.judge(st, [g4090], floor, ref, now, now)[0] == "chauffe")
st.update(uptime_min=30, th_5min=290)
check("conforme", ps.judge(st, [g4090], floor, ref, now, now)[0] == "conforme", ps.judge(st, [g4090], floor, ref, now, now))
st.update(th_5min=150)
v, d = ps.judge(st, [g4090], floor, ref, now, now)
check("lent, avec les mesures dans la raison", v == "lent" and "150.0 TH/s" in d and "MHz" in d and " W" in d, (v, d))
check("thermique", ps.judge(st, [dict(g4090, **{"temperature.gpu": 92.0})], floor, ref, now, now)[0] == "thermique")
st.update(th_5min=None)
check("muet", ps.judge(st, [g4090], floor, ref, now, now)[0] == "muet")
st.update(th_5min=290, uptime_min=120, shares={"last_accept_s": 30})
check("share recente : conforme", ps.judge(st, [g4090], floor, ref, now, now)[0] == "conforme")
st.update(shares={"last_accept_s": None})
check("aucune share en 2 h : sans-share", ps.judge(st, [g4090], floor, ref, now, now)[0] == "sans-share")
st.update(config="srbminer", shares={"last_accept_s": 4000})
check("derniere share il y a 66 min : sans-share", ps.judge(st, [g4090], floor, ref, now, now)[0] == "sans-share")
check("statut fige", ps.judge(st, [g4090], floor, ref, now, now - 400)[0] == "fige")
check("pas de statut : attente", ps.judge(None, [g4090], floor, ref, now, now)[0] == "attente")

# --- IMDS simulee
calls = []
ps.imds = lambda m, p, b=None, timeout=10: calls.append((m, p, b)) or (204, "")

# demarrage : carte bridee par l'hote -> reallocation
ps.query_gpus = lambda: [dict(g4090, **{"power.limit": 250.0, "memory.total": 24564.0, "driver_version": "580.1"})]
ps.pool_reachable = lambda timeout=5: "pool.pearlhash.xyz:9000 (20 ms)"
rc = ps.cmd_boot()
check("boot : 4090 a 250 W sur 450 -> code 2", rc == 2, rc)
check("boot : POST /v1/reallocate avec la raison", calls and calls[-1][1] == "/v1/reallocate" and "250 W sur 450 W" in calls[-1][2]["reason"], calls)
os.remove(ps.SALAD_F)

# demarrage : portable sans limite publiee, pool joignable -> accepte
calls.clear()
ps.query_gpus = lambda: [{"name": "NVIDIA GeForce RTX 5080 Laptop GPU", "power.limit": None, "power.default_limit": 80.0, "memory.total": 16303.0}]
check("boot : portable accepte", ps.cmd_boot() == 0 and not calls, calls)
node = ps.load_json(ps.SALAD_F)
check("boot : reference du portable enregistree", node["ref"]["name"] == "RTX 5080 Laptop", node.get("ref"))

# demarrage : pool injoignable
ps.query_gpus = lambda: [dict(g4090, **{"memory.total": 24564.0})]
ps.pool_reachable = lambda timeout=5: None
check("boot : pool injoignable -> code 2", ps.cmd_boot() == 2 and "pool injoignable" in calls[-1][2]["reason"], calls[-1:])
os.remove(ps.SALAD_F)

# HEALTH_ACTION=warn : on journalise, on mine quand meme
calls.clear()
ps.ACTION = "warn"
check("warn : code 0, aucun appel IMDS", ps.cmd_boot() == 0 and not calls, calls)
ps.ACTION = "reallocate"
os.remove(ps.SALAD_F)

# surveillance : 3 verifications lentes d'affilee -> reallocation, une seule fois
calls.clear()
ps.query_gpus = lambda: [g4090]
ps.save_json(ps.STATUS_F, {"config": "pearlminer", "uptime_min": 30, "th_5min": 150, "th_effective": 150,
                           "shares": {"last_accept_s": None}})
ps.INTERVAL = 0
for i in range(ps.GRACE):
    buf = io.StringIO()
    with redirect_stdout(buf):
        ps.cmd_watch(once=True)
line = buf.getvalue().strip().splitlines()[-1]
logrep = json.loads(line)
check("watch : rapport JSON sur la sortie standard (logs Salad)", logrep["pearl"] == "report"
      and logrep["status"]["th_5min"] == 150 and logrep["node"]["health"]["verdict"] == "lent", line[:120])
check("rapport des logs compact (< 6 Ko, sans historique)", len(line) < 6000 and "health_history" not in line, len(line))
node = ps.load_json(ps.SALAD_F)
# chaque appel --once repart de zero : les verifications d'affilee se comptent dans une meme boucle
check("watch --once : verdict lent enregistre", node["health"]["verdict"] == "lent", node.get("health"))
check("deletion cost = TH/s utiles x 10", node.get("deletion_cost") == 1500, node.get("deletion_cost"))

loops = {"n": 0}
real_sleep = ps.time.sleep


def stop_after(_):
    loops["n"] += 1
    if loops["n"] >= ps.GRACE + 1:
        raise KeyboardInterrupt


ps.time.sleep = stop_after
calls.clear()
try:
    ps.cmd_watch()
except KeyboardInterrupt:
    pass
ps.time.sleep = real_sleep
re_calls = [c for c in calls if c[1] == "/v1/reallocate"]
check("watch : reallocation apres %d verifications lentes" % ps.GRACE, len(re_calls) == 1 and "lent" in re_calls[0][2]["reason"], re_calls)

# rapport lu par le tableau de bord : une ligne JSON
buf = io.StringIO()
with redirect_stdout(buf):
    ps.cmd_report()
out = buf.getvalue()
rep = json.loads(out)
check("report : une seule ligne", out.count("\n") == 1, out[:80])
check("report : statut + noeud + identite Salad", rep["status"]["th_5min"] == 150 and rep["node"]["health"]["verdict"]
      and rep["salad"]["machine_id"].startswith("eb3a"), list(rep))

print("\nRESULTAT: %d echec(s)" % len(FAIL))
sys.exit(1 if FAIL else 0)

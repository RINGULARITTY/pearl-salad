#!/bin/bash
# Pearl (PRL) sur SaladCloud : configuration par variables d'environnement, verification du
# noeud, puis controleur de minage + agent de sante. Relance a l'identique apres chaque
# reallocation : aucun etat a conserver entre deux noeuds.
#
#   PRL_WALLET (ou WALLET)   obligatoire, adresse prl1...
#   MERGE_SUFFIX             ex. "@nock=<adresse NOCK>" (merge mining Pearlhash)
#   WORKER                   nom sur le pool ; defaut salad-<8 premiers caracteres de la machine>
#   POOLS (ou POOL)          serveurs ; defaut Pearlhash (pool.pearlhash.xyz, hk, global, global2)
#   CANDIDATES               mineurs a comparer ; defaut pearlminer (le seul accepte par Pearlhash)
#   POOLS_SRBMINER / POOLS_BZMINER  pool de SRBMiner / BzMiner (Pearlhash les refuse)
# Le reste (seuils de sante, garde-fou...) : voir LISEZMOI.md.
set -u
B="${PEARL_HOME:-/opt/pearl}"
L="${PEARL_LOG:-/var/log/pearl-miner.log}"
mkdir -p "$B" "$(dirname "$L")" && touch "$L"
say() { echo "$(date '+%F %T') [boot] $*" >> "$L"; }

# Salad ne collecte que la sortie standard : le journal y est recopie en continu.
tail -n0 -F "$L" 2>/dev/null &
sleep 0.3

# Noms du guide « Run miners on SaladCloud » acceptes en plus de ceux du controleur.
export PRL_WALLET="${PRL_WALLET:-${WALLET:-}}"
if [ -n "${POOL:-}" ] && [ -z "${POOLS:-}" ]; then export POOLS="$POOL"; fi
if [ -z "$PRL_WALLET" ]; then
  say "PRL_WALLET manquant : renseigner la variable d'environnement du container group"
  sleep 600; exit 1
fi
case "$PRL_WALLET" in prl1*) ;; *) say "attention : PRL_WALLET ne commence pas par prl1 (adresse Pearl ?)";; esac
if [ -z "${WORKER:-}" ]; then
  if [ -n "${SALAD_MACHINE_ID:-}" ]; then WORKER="salad-${SALAD_MACHINE_ID:0:8}"; else WORKER="salad-$(hostname | cut -c1-8)"; fi
  export WORKER
fi
say "image ${IMAGE_VERSION:-dev}, worker $WORKER, mineurs ${CANDIDATES:-?}, machine ${SALAD_MACHINE_ID:-hors Salad}, groupe ${SALAD_CONTAINER_GROUP_NAME:-?}"

# Verifications du noeud : GPU, limite de puissance de l'hote, pool. Code 2 = reallocation
# demandee, Salad va arreter ce conteneur : on attend sans miner.
python3 "$B/pearl_salad.py" boot
rc=$?
if [ "$rc" = 2 ]; then say "noeud rendu a SaladCloud, en attente de l'arret du conteneur"; sleep 900; exit 1; fi
if [ "$rc" != 0 ]; then say "verification du noeud en echec (code $rc)"; sleep 120; exit 1; fi

python3 "$B/pearl_salad.py" watch &
exec python3 "$B/pearl_controller.py" run

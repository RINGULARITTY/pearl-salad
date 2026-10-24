# pearl-salad — Pearl (PRL) sur SaladCloud

Image Docker pour miner Pearl sur les GPU NVIDIA de [SaladCloud](https://portal.salad.com), selon le modèle
recommandé par Salad pour les mineurs (« Run Miners on SaladCloud ») : tout est dans l’image, la configuration
passe par les variables d’environnement, et le conteneur repart seul après chaque réallocation.

| Fichier | Rôle |
| --- | --- |
| `Dockerfile` | base `nvidia/cuda:12.8.1-base-ubuntu24.04`, mineurs CUDA épinglés et vérifiés par SHA256 à la construction |
| `entrypoint.sh` | variables → vérification du nœud → agent de santé + contrôleur |
| `pearl_salad.py` | agent : vérifications au démarrage, santé en continu, réallocation (IMDS), rapport lu par le tableau de bord |
| `vendor/pearl_controller.py` | le contrôleur de `pearl_cloud_miner` (copié par `build.sh`, ne pas modifier ici) |
| `vendor/gpus.json` | table de hashrate de référence par carte (même source que le tableau de bord) |

## Pourquoi pas WildRig

Un nœud Salad exécute les conteneurs sous **WSL2**. Le pilote NVIDIA n’y expose que CUDA, aucune plateforme
OpenCL (`/usr/lib/wsl/lib` ne contient pas `libnvidia-opencl`). WildRig est un mineur OpenCL : il ne voit aucun GPU.
L’image embarque donc des mineurs CUDA, testés le 24/10/2026 sous WSL2 sur une RTX 5060 Ti (même pile que Salad) :

| Mineur | Pool | Résultat sous WSL2 |
| --- | --- | --- |
| **pearl-miner V14** (officiel pearlhash.xyz) | pool.pearlhash.xyz + `@nock=` | connexion, login, 63–65 TH/s, « Solution accepted! » — GPU partagé avec le WildRig local pendant l’essai, donc sous-estimé |
| SRBMiner 3.8.0 (2 % de dev fee) | pool.pearlhash.xyz | GPU vu en CUDA, mais **le pool ferme la connexion** (même sans `@nock=`) : Pearlhash n’accepte que WildRig et pearl-miner |
| BzMiner 100.50 (2 %) | — | aucun backend CUDA dans un WSL nu ; l’image CUDA fournit `libcudart` (fonctionne sur Salad d’après des mesures publiées) |

Par défaut l’image mine donc avec **pearl-miner sur ton pool Pearlhash, avec le merge mining NOCK**. SRBMiner et
BzMiner restent disponibles pour un autre pool (Kryptex, HeroMiners) : le contrôleur les mesure et garde le
meilleur rendement net (dev fee, frais du pool, valeur du merge mining).

## Construire l’image

Pas de Docker sur ce PC : trois possibilités.

```powershell
.\build.ps1 -Tag ghcr.io/<toi>/pearl-salad:v1.0.0 -Push     # Docker Desktop
```

```bash
./build.sh ghcr.io/<toi>/pearl-salad:v1.0.0 --push            # Docker sous Linux / WSL
```

Sans Docker : `.\build.ps1 -SyncOnly`, puis pousser ce dossier (avec `vendor/`) dans un dépôt GitHub. Le workflow
`.github/workflows/build.yml` lance les tests, construit et publie `ghcr.io/<toi>/pearl-salad` (`:latest` sur `main`,
`:vX.Y.Z` sur un tag). Rendre le paquet **public** (Packages → Package settings → Change visibility), sinon
renseigner les identifiants du registre dans le container group.

`build.sh` / `build.ps1` recopient toujours `pearl_controller.py` et `gpus.json` depuis `../pearl_cloud_miner` :
une seule source de vérité pour le contrôleur.

## Déployer sur portal.salad.com

*Container Groups → Deploy a Container Group* :

| Réglage | Valeur |
| --- | --- |
| Image | `ghcr.io/<toi>/pearl-salad:v1.0.0` — un tag figé, jamais `:latest` (une réallocation tirerait une image non testée) |
| Replicas | **1** pour commencer, 24 h ; ensuite seulement augmenter |
| GPU | classes NVIDIA seulement (pas d’AMD dans ce groupe). L’extension affiche le profit de chaque classe sur cette page |
| Priority | **Batch / Lowest** : le minage tolère les interruptions, c’est le tarif le plus bas |
| vCPU / RAM | 4 vCPU / 8 Go — sur un groupe GPU, vCPU et RAM sont **inclus dans le prix** ; plus haut réduit le nombre de nœuds éligibles |
| Disque | le minimum |
| Container Gateway | non |
| Health probes | aucune (option : liveness *exec* `python3 /opt/pearl/pearl_salad.py probe`, période 60 s) |
| External logging | non nécessaire : le journal complet part sur la sortie standard (onglet *Logs* du portail) |

Variables d’environnement :

| Variable | Valeur |
| --- | --- |
| `PRL_WALLET` | **obligatoire** — ton adresse `prl1p…` |
| `MERGE_SUFFIX` | `@nock=<ton adresse NOCK>` (la même que dans `start-PEARL.bat`) |
| `WORKER` | optionnel ; défaut `salad-<8 premiers caractères de la machine>`, visible sur pearlhash.xyz |
| `CANDIDATES` | défaut `pearlminer`. Pour comparer : `pearlminer,srbminer,bzminer` avec `POOLS_SRBMINER` / `POOLS_BZMINER` |
| `POOLS_SRBMINER`, `POOLS_BZMINER` | ex. `ssl://prl.kryptex.network:8048` ou `ca.pearl.herominers.com:1200` (le suffixe NOCK n’y est pas envoyé) |
| `MERGE_BONUS` | valeur du NOCK en fraction du PRL miné (ex. `0.05`) pour départager Pearlhash et un autre pool |
| `MAX_COST_PER_PRL`, `COST_PER_HOUR` | garde-fou de coût du contrôleur (en mode `GUARD_ACTION=log` sur Salad) |

Seuils de santé, tous optionnels : `HEALTH_ACTION` (`reallocate` ou `warn`), `POWER_CAP_MIN` (0.70),
`HEALTH_MIN_RATIO` (0.75), `HASHRATE_MIN` (TH/s, remplace la table ; 0 = pas de plancher), `REF_FACTOR_PEARLMINER`
(0.8), `HEALTH_WARMUP_MIN` (5), `HEALTH_GRACE` (3), `TEMP_MAX` (90), `SHARE_STALL_MIN` (60), `BOOT_TIMEOUT_MIN` (20).

## Ce que l’image vérifie sur chaque nœud

Un nœud Salad est le PC d’un particulier : carte bridée par son propriétaire, refroidissement insuffisant, réseau
qui bloque le pool. Le nœud étant payé à la seconde, mieux vaut le rendre vite que miner à moitié.

**Au démarrage** (avant de lancer un mineur, quelques secondes) :
- `nvidia-smi` voit un GPU ;
- **limite de puissance** laissée par l’hôte ≥ 70 % de celle d’usine (`power.limit` / `power.default_limit`) — une
  carte bridée à 60 % mine à 60 %, inutile d’attendre pour le savoir. Les portables, qui ne publient pas de limite
  fiable, ne sont pas jugés sur ce point ;
- le pool répond (TCP) depuis ce nœud.

**En continu** (toutes les 60 s, après 5 min de minage) : hashrate mesuré par le contrôleur ≥ **75 % de la valeur
typique de la carte** (× 0,8 pour pearl-miner, voir plus bas), température, shares acceptées, statut du
contrôleur frais. Trois vérifications défavorables d’affilée → le nœud est rendu à Salad (`POST /v1/reallocate`
de l’IMDS, avec la mesure dans la raison) et le conteneur repart ailleurs. Salad exclut temporairement le nœud
quitté : c’est voulu, mais les seuils restent prudents pour ne pas épuiser la capacité disponible.

L’agent règle aussi le **coût de suppression** de chaque instance (IMDS `deletion-cost`) sur son hashrate : quand
tu réduis le nombre de replicas, Salad retire d’abord les nœuds les plus lents.

**À recaler après le premier déploiement** : la table de référence vient de WildRig, SRBMiner et BzMiner. pearl-miner
n’a aucune mesure publique sur cartes grand public ; le facteur 0,8 est une estimation prudente (≥ 0,7 mesuré avec
un GPU partagé). Une fois quelques nœuds en marche, l’onglet *Salad nodes* du tableau de bord montre chaque nœud en
% de la médiane de ta flotte ; ajuste `REF_FACTOR_PEARLMINER` (ou `HASHRATE_MIN`) en conséquence.

## Suivi dans le tableau de bord

1. Ajouter ta clé SSH publique dans le portail Salad (*SSH Keys*, Ed25519 ou RSA). Le tableau de bord lit chaque
   instance par le SSH natif de Salad — aucun port à ouvrir, aucune donnée envoyée ailleurs.
2. Créer une clé API (portail → *API Access*) et la mettre dans `SALAD_API_KEY` (ou `~/.salad_api_key`).
3. Lancer le tableau de bord (dossier `pearl_cloud_miner`) :

```powershell
$env:SALAD_API_KEY = "<clé>"
python pearl_dashboard.py --salad-org <organisation> --salad-project default --api-key $env:VAST_API_KEY
```

Onglet **Salad nodes** : par nœud, classe et prix Salad, état, verdict de santé (et la raison d’une réallocation,
gardée 6 h), hashrate utile, % de la référence et de la médiane de ta flotte, hashrate vu par le pool pour ce
worker, puissance / limite (et % de la limite d’usine), température, fréquence, profit médian et P(profit 24 h),
journal. Les nœuds Salad rejoignent aussi l’onglet *Overview* et le Monte Carlo, à côté de Vast et du mineur local.

L’empreinte de la clé d’hôte publiée par l’API Salad est comparée à celle présentée par chaque instance
(`ssh-keyscan`) avant la première connexion ; si elles diffèrent, la connexion est refusée. Rien n’est ajouté à
`~/.ssh/known_hosts`.

## Extension sur portal.salad.com

L’extension `pearl-vast` (version 1.2.0) ajoute sous chaque carte GPU de la page de création d’un container group
le profit par jour (fourchette), le hashrate retenu et, en infobulle, les PRL/jour, le coût du PRL et les prix des
quatre priorités. Un panneau trie toutes les classes par profit. Quand le tableau de bord mesure tes nœuds Salad,
la médiane de tes nœuds remplace la table pour la classe concernée (étiquette « mesuré »).

## Tests

```bash
python -X utf8 test/test_salad_agent.py      # table de référence, verdicts, démarrage, IMDS simulée, rapport
```

Dans `pearl_cloud_miner` : `test/test_cuda_miners.py` (journaux réels de pearl-miner, BzMiner, SRBMiner, pools par
mineur) et `test/test_salad_bridge.py` (API et SSH simulés, épinglage de la clé d’hôte).

Essai de bout en bout fait le 24/10/2026 sous WSL2 : `entrypoint.sh` → vérifications → contrôleur → pearl-miner sur
`global.pearlhash.net:9000` (14 ms), statut et rapport SSH conformes. **L’image elle-même n’a pas été construite ici**
(pas de Docker sur ce PC) : premier déploiement à faire avec 1 replica.

## Ordre de grandeur (24/10/2026)

PRL à 1,85 $, difficulté 45,1 M : 100 TH/s rapportent ~2,77 $/jour. Au tarif Lowest, une RTX 4090 (0,16 $/h,
~308 TH/s d’après la table) laisse ~+4,7 $/jour, une RTX 3060 (0,04 $/h, ~45 TH/s) presque rien. La difficulté
monte d’environ 2,7 %/jour : le profit d’une carte fond en quelques semaines. Le Monte Carlo du tableau de bord en
tient compte.

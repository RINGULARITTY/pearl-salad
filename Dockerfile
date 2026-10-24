# syntax=docker/dockerfile:1.7
# Pearl (PRL) pour SaladCloud, GPU NVIDIA.
#
# Construire avec build.sh / build.ps1 : ils copient pearl_controller.py et gpus.json depuis
# ../pearl_cloud_miner dans vendor/ (une seule source de verite pour le controleur).
#
# Les noeuds Salad executent les conteneurs sous WSL2 : le pilote NVIDIA n'y expose que CUDA,
# aucune plateforme OpenCL. WildRig (OpenCL) n'y voit aucun GPU ; l'image embarque donc des
# mineurs CUDA, epingles et verifies par SHA256 a la construction (aucun telechargement au
# demarrage d'un noeud, ni dependance a l'API GitHub) :
#   pearl-miner  mineur officiel de pearlhash.xyz - le seul mineur CUDA que ce pool accepte
#   SRBMiner     pools Kryptex / HeroMiners (2 % de dev fee)
#   BzMiner      pools Kryptex / HeroMiners (2 % de dev fee)
ARG CUDA_BASE=nvidia/cuda:12.8.1-base-ubuntu24.04

# ------------------------------------------------------------- mineurs --
FROM ubuntu:24.04 AS miners
ARG PEARLMINER_VERSION=V14
ARG PEARLMINER_SHA256=249696c9c91f8821d194219cbbe37f56df541c157fe3ddda2ff063aaf1c60e63
ARG SRB_VERSION=3.8.0
ARG SRB_SHA256=095c3f75905ee023e8ed85ac905afefc92825cce83a528fa955cf1db456d3dd8
ARG BZ_VERSION=v100.50
ARG BZ_SHA256=eae8465b0714c448d6897679dde7a893a1caa1b17bc418b2a0e875ebb2960f61
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
# Repertoires nommes <mineur>-<version> : ceux que pearl_controller.py cherche avant de telecharger.
RUN set -eux; \
    d=/out/pearlminer-${PEARLMINER_VERSION}; mkdir -p "$d"; \
    curl -fsSL -o "$d/pearl-miner" "https://github.com/pearlhash/pearlhash-miner/releases/download/${PEARLMINER_VERSION}/pearl-miner"; \
    echo "${PEARLMINER_SHA256}  $d/pearl-miner" | sha256sum -c -; \
    chmod 755 "$d/pearl-miner"
RUN set -eux; \
    d=/out/srbminer-${SRB_VERSION}; mkdir -p "$d"; \
    curl -fsSL -o /tmp/srb.tgz "https://github.com/doktor83/SRBMiner-Multi/releases/download/${SRB_VERSION}/SRBMiner-Multi-$(echo "$SRB_VERSION" | tr . -)-Linux.tar.gz"; \
    echo "${SRB_SHA256}  /tmp/srb.tgz" | sha256sum -c -; \
    tar xzf /tmp/srb.tgz -C "$d" --strip-components=1; rm /tmp/srb.tgz; \
    chmod 755 "$d/SRBMiner-MULTI"
RUN set -eux; \
    d=/out/bzminer-${BZ_VERSION}; mkdir -p "$d"; \
    curl -fsSL -o /tmp/bz.tgz "https://github.com/bzminer/bzminer/releases/download/${BZ_VERSION}/bzminer_${BZ_VERSION}_linux.tar.gz"; \
    echo "${BZ_SHA256}  /tmp/bz.tgz" | sha256sum -c -; \
    tar xzf /tmp/bz.tgz -C "$d" --strip-components=1; rm /tmp/bz.tgz; \
    rm -f "$d"/start_*.sh; chmod 755 "$d/bzminer"

# --------------------------------------------------------------- image --
FROM ${CUDA_BASE}
ARG PEARLMINER_VERSION=V14
ARG SRB_VERSION=3.8.0
ARG BZ_VERSION=v100.50
ARG IMAGE_VERSION=dev
LABEL org.opencontainers.image.title="pearl-salad" \
      org.opencontainers.image.description="Pearl (PRL) GPU miner for SaladCloud NVIDIA nodes, with node health checks" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

# NVIDIA_DISABLE_REQUIRE : un hote dont le pilote est plus ancien que CUDA 12.8 doit quand
# meme demarrer le conteneur (les mineurs embarquent leurs noyaux).
ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    NVIDIA_REQUIRE_CUDA= \
    NVIDIA_DISABLE_REQUIRE=true \
    PYTHONUNBUFFERED=1 \
    IMAGE_VERSION=${IMAGE_VERSION} \
    PEARL_HOME=/opt/pearl \
    PEARL_LOG=/var/log/pearl-miner.log \
    CANDIDATES=pearlminer \
    PEARLMINER_VER=${PEARLMINER_VERSION} \
    SRBMINER_VER=${SRB_VERSION} \
    BZMINER_VER=${BZ_VERSION} \
    PREFLIGHT_SECONDS=120

# ocl-icd : SRBMiner sonde OpenCL au demarrage (erreur sans consequence sous WSL2).
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates tini ocl-icd-libopencl1 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /etc/OpenCL/vendors && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

COPY --from=miners /out/ /opt/pearl/miners/
COPY vendor/pearl_controller.py vendor/gpus.json pearl_salad.py entrypoint.sh /opt/pearl/
RUN sed -i 's/\r$//' /opt/pearl/entrypoint.sh \
 && chmod 755 /opt/pearl/entrypoint.sh \
 && printf '#!/bin/sh\n[ $# -eq 0 ] && set -- status\nexec python3 /opt/pearl/pearl_controller.py "$@"\n' > /usr/local/bin/pearl \
 && printf '#!/bin/sh\n[ $# -eq 0 ] && set -- report\nexec python3 /opt/pearl/pearl_salad.py "$@"\n' > /usr/local/bin/pearl-salad \
 && chmod 755 /usr/local/bin/pearl /usr/local/bin/pearl-salad \
 && python3 -m py_compile /opt/pearl/pearl_controller.py /opt/pearl/pearl_salad.py

WORKDIR /opt/pearl
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/pearl/entrypoint.sh"]

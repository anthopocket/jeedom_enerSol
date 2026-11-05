#!/bin/bash
# Installation des dépendances enerSol : venv Python + aiohttp
# Debian 12 : Python 3.11 du système.
# Debian 11 : Python système trop ancien (3.9) → Python 3.12 autonome installé avec « uv »
#             dans le dossier du plugin (aucune modification du Python système).
PROGRESS_FILE=/tmp/jeedom/enerSol/dependency
if [ -n "$1" ]; then
    PROGRESS_FILE=$1
fi
BASEDIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$BASEDIR/venv"
UVDIR="$BASEDIR/uv"                 # outil uv
PYDIR="$BASEDIR/python"             # Python autonome (Debian 11)
PY_WANTED=3.12

mkdir -p "$(dirname "$PROGRESS_FILE")"
touch "$PROGRESS_FILE"
echo 0 > "$PROGRESS_FILE"
echo "*** Installation des dépendances enerSol ***"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    echo "Système : ${PRETTY_NAME:-inconnu} ($(uname -m))"
fi

finish() {
    rm -f "$PROGRESS_FILE"
    echo "*** Fin de l'installation ($1) ***"
    exit "$2"
}

py_ok() {   # $1 = interpréteur ; vrai si Python >= 3.10
    [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

echo 10 > "$PROGRESS_FILE"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip curl ca-certificates
echo 20 > "$PROGRESS_FILE"

# ── 1. Trouver un Python >= 3.10 sur le système ─────────────────────
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    c=$(command -v "$candidate" 2>/dev/null)
    if [ -n "$c" ] && py_ok "$c"; then
        PY="$c"
        break
    fi
done

# ── 2. Sinon (Debian 11) : Python autonome via uv ───────────────────
if [ -z "$PY" ]; then
    echo "Python système trop ancien ($(python3 --version 2>&1)) : installation d'un Python $PY_WANTED autonome dans le plugin."
    UV="$UVDIR/bin/uv"
    if [ ! -x "$UV" ]; then
        # a) uv via pip dans un petit venv dédié
        python3 -m venv "$UVDIR" \
            && "$UVDIR/bin/python3" -m pip install --upgrade pip \
            && "$UVDIR/bin/python3" -m pip install --upgrade uv
        # b) secours : installeur officiel
        if [ ! -x "$UV" ]; then
            echo "Installation de uv par pip impossible, essai avec l'installeur officiel…"
            mkdir -p "$UVDIR/bin"
            curl -LsSf https://astral.sh/uv/install.sh \
                | env UV_INSTALL_DIR="$UVDIR/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
        fi
    fi
    [ -x "$UV" ] || finish "échec : impossible d'installer uv" 1
    echo "uv : $($UV --version)"
    echo 35 > "$PROGRESS_FILE"

    export UV_PYTHON_INSTALL_DIR="$PYDIR"
    export UV_CACHE_DIR="$BASEDIR/.uv-cache"
    "$UV" python install "$PY_WANTED" || finish "échec téléchargement Python $PY_WANTED" 1
    PY=$("$UV" python find "$PY_WANTED" 2>/dev/null)
    py_ok "$PY" || finish "échec : Python $PY_WANTED introuvable après installation" 1
    rm -rf "$UV_CACHE_DIR" "$UVDIR"        # uv n'est plus utile : place libérée
fi
echo "Python utilisé : $PY ($($PY --version 2>&1))"
echo 45 > "$PROGRESS_FILE"

# ── 3. venv du plugin (recréé s'il a été fait avec un Python trop ancien) ─
if [ -e "$VENV" ] && ! py_ok "$VENV/bin/python3"; then
    echo "Ancien venv incompatible : suppression."
    sudo rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python3" ]; then
    "$PY" -m venv "$VENV" || finish "échec création venv" 1
fi
echo 50 > "$PROGRESS_FILE"

"$VENV/bin/python3" -m ensurepip --upgrade >/dev/null 2>&1
"$VENV/bin/python3" -m pip install --upgrade pip wheel
echo 60 > "$PROGRESS_FILE"
"$VENV/bin/python3" -m pip install --upgrade -r "$BASEDIR/requirements.txt" || finish "échec pip" 1
echo 90 > "$PROGRESS_FILE"

# ── 4. Vérification ─────────────────────────────────────────────────
"$VENV/bin/python3" -c 'import aiohttp; print("aiohttp", aiohttp.__version__, "OK")' \
    || finish "échec : import aiohttp impossible" 1
echo 95 > "$PROGRESS_FILE"

mkdir -p "$BASEDIR/../data"
sudo chown -R www-data:www-data "$VENV" "$BASEDIR/../data"
[ -d "$PYDIR" ] && sudo chown -R www-data:www-data "$PYDIR"
[ -d "$UVDIR" ] && sudo chown -R www-data:www-data "$UVDIR"
finish "succès" 0

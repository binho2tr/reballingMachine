#!/bin/bash
# Script de execução da máquina de reballing
# Salva em ~/reballing/run.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"
APP="$SCRIPT_DIR/reballing.py"

if [ ! -f "$APP" ]; then
    echo "ERRO: $APP não encontrado."
    echo "Copie o reballing.py para $SCRIPT_DIR/"
    exit 1
fi

echo "Iniciando reballing controller..."
echo "Script: $APP"
echo "Python: $PYTHON"
echo ""

sudo "$PYTHON" "$APP"
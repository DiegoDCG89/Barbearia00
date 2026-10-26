#!/usr/bin/env bash
# Regera static/tailwind.css. Rode depois de mudar classes do Tailwind nos templates (e faça commit do arquivo gerado).
# Na primeira vez baixa o Tailwind CLI "standalone" (v3) para .tools/ — não precisa de Node.
set -euo pipefail
cd "$(dirname "$0")/.."
VER=3.4.17
case "$(uname -m)" in x86_64) ARCH=x64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "Arquitetura não suportada: $(uname -m)"; exit 1 ;; esac
[ "$(uname -s)" = Darwin ] && OS=macos || OS=linux
BIN=.tools/tailwindcss
if [ ! -x "$BIN" ]; then
  mkdir -p .tools
  curl -fsSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VER/tailwindcss-$OS-$ARCH"
  chmod +x "$BIN"
fi
"$BIN" -c tailwind.config.js -i tailwind/input.css -o static/tailwind.css --minify
echo "static/tailwind.css atualizado ($(wc -c < static/tailwind.css) bytes)"

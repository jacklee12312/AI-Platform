#!/usr/bin/env bash
set -euo pipefail

base=${HLIELLAMA_RUNTIME_DIR:-/home/cix/houmo-HLIELLama-xh2-211-guanwang}
image=${HLIELLAMA_RUNTIME_IMAGE:-houmo-hliellama-xh2:2.1.1}
tmp=$(mktemp -d /home/cix/hliellama-image-rootfs.XXXXXX)
resolved=$(realpath "$tmp")

case "$resolved" in
  /home/cix/hliellama-image-rootfs.*) ;;
  *)
    echo "Refusing unsafe temporary path: $resolved" >&2
    exit 1
    ;;
esac

cleanup() {
  rm -rf -- "$resolved"
}
trap cleanup EXIT

mkdir -p "$tmp/opt/hliellama" "$tmp/usr/bin"
cp -a "$base/bin" "$base/lib" "$base/version.txt" "$tmp/opt/hliellama/"
cp -a /usr/bin/curl "$tmp/usr/bin/"

tar -C "$tmp" -cf - . | docker import \
  --change 'ENV LD_LIBRARY_PATH=/opt/hliellama/lib:/usr/local/houmo-sdk/hal/lib:/usr/local/houmo-sdk/lib' \
  --change 'WORKDIR /opt/hliellama' \
  --change 'ENTRYPOINT ["/opt/hliellama/bin/llama-server"]' \
  --change 'LABEL org.opencontainers.image.title="houmo-hliellama-xh2"' \
  --change 'LABEL org.opencontainers.image.version="2.1.1"' \
  - "$image"

docker image inspect "$image" \
  --format 'ID={{.Id}} Size={{.Size}} Architecture={{.Architecture}} Entrypoint={{json .Config.Entrypoint}}'

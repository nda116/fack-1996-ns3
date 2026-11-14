#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
STUDY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
NS3_ROOT="${1:-$HOME/ns-3.47}"
SELECTED="${2:-all}"
PROFILE="${3:-paper11}"
SEEDS="${4:-10}"
[[ -f "$NS3_ROOT/ns3" ]] || { echo "Missing ns-3 launcher: $NS3_ROOT/ns3" >&2; exit 2; }
NS3_ROOT="$(cd -- "$NS3_ROOT" && pwd)"
[[ "$(cat "$NS3_ROOT/VERSION")" == 3.47 ]] || { echo 'Use ns-3.47 for this study' >&2; exit 2; }
[[ "$SELECTED" =~ ^(all|2|3|4|5|6|7|8|11|12)$ ]] || { echo 'Select all, 2-8, 11 or 12' >&2; exit 2; }
[[ "$PROFILE" =~ ^(paper11|legacy|standard|ns1)$ ]] || { echo 'Profile: paper11, legacy, standard or ns1' >&2; exit 2; }
[[ "$SEEDS" =~ ^[1-9][0-9]*$ ]] && (( SEEDS >= 2 && SEEDS <= 1000 )) || { echo 'Use 2-1000 common seeds' >&2; exit 2; }
mkdir -p "$STUDY_ROOT/build" "$NS3_ROOT/scratch/fack-paper"
for source in "$STUDY_ROOT"/model/*; do
  [[ -f "$source" ]] || continue
  install_name="$(basename -- "$source")"
  [[ "$install_name" != CMakeLists.cmake ]] || install_name=CMakeLists.txt
  target="$NS3_ROOT/scratch/fack-paper/$install_name"
  if ! cmp -s "$source" "$target"; then cp "$source" "$target"; fi
done
COMMON=(-std=c++17 -Wall -Wextra -Werror -O1 -g -fsanitize=address,undefined -I"$STUDY_ROOT/model")
g++ "${COMMON[@]}" "$STUDY_ROOT/tests/core-tests.cc" "$STUDY_ROOT/model/fack-model.cc" -o "$STUDY_ROOT/build/core-tests"
"$STUDY_ROOT/build/core-tests" | tee "$STUDY_ROOT/build/core-tests.log"
python3 "$STUDY_ROOT/scripts/verify_delivery.py" --study "$STUDY_ROOT" --record-core-tests
python3 "$STUDY_ROOT/scripts/verify_delivery.py" --study "$STUDY_ROOT" --record-aux-tests
cd "$NS3_ROOT"
if [[ ! -f cmake-cache/CMakeCache.txt ]]; then ./ns3 configure --disable-examples --disable-tests; fi
./ns3 build fack-paper
python3 "$STUDY_ROOT/scripts/test-network.py" --timer-tests --ns3 "$NS3_ROOT" --output "$STUDY_ROOT/build/timer-network"
python3 "$STUDY_ROOT/scripts/experiments.py" --ns3 "$NS3_ROOT" --study "$STUDY_ROOT" --figures "$SELECTED" --profile "$PROFILE" --seed-count "$SEEDS" --reuse
python3 "$STUDY_ROOT/scripts/test-network.py" --ns3 "$NS3_ROOT" --study "$STUDY_ROOT"
python3 "$STUDY_ROOT/scripts/verify_delivery.py" --ns3 "$NS3_ROOT" --study "$STUDY_ROOT"

#!/usr/bin/env bash
set -euo pipefail

target_branch=${1:-}
case "$target_branch" in
  dev|preview|main) ;;
  *)
    echo "usage: $0 dev|preview|main" >&2
    exit 2
    ;;
esac

check_ancestor() {
  local path=$1
  local branch=$2
  local url pinned scratch
  url=$(git config -f .gitmodules --get "submodule.$path.url")
  pinned=$(git ls-tree HEAD "$path" | awk '{print $3}')
  if [[ -z "$url" || -z "$pinned" ]]; then
    echo "$path のURLまたはgitlink SHAを解決できません" >&2
    return 1
  fi

  scratch=$(mktemp -d)
  trap 'rm -rf "$scratch"' RETURN
  git -C "$scratch" init -q
  git -C "$scratch" fetch -q --no-tags --filter=blob:none "$url" "$branch"
  if ! git -C "$scratch" cat-file -e "$pinned^{commit}" 2>/dev/null; then
    git -C "$scratch" fetch -q --no-tags --filter=blob:none "$url" "$pinned"
  fi
  if ! git -C "$scratch" merge-base --is-ancestor "$pinned" FETCH_HEAD; then
    echo "$path の $pinned は子repositoryの $branch から到達できません" >&2
    echo "子repositoryを $branch へ先に昇格してから、そのbranch上のSHAへ更新してください" >&2
    return 1
  fi
  echo "$path: $pinned is reachable from $branch"
}

# editor本体は親repositoryと同じ三段階branchを使う。
check_ancestor external/soramimic "$target_branch"
# wordlistsは公開済みデータを単一のmain branchで管理している。
check_ancestor external/soramimic-wordlists main

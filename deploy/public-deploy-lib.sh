#!/usr/bin/env bash
# public deploy/rollback scripts shared helpers. This file is meant to be sourced.
# shellcheck disable=SC2154 # callers define deployment layout globals after sourcing

die() {
  echo "error: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

require_root() {
  if [[ ${EUID} -ne 0 && ${SORAMIMIC_ALLOW_NON_ROOT_TEST:-0} != 1 ]]; then
    die "rootで実行してください"
  fi
}

require_sha() {
  [[ $1 =~ ^[0-9a-f]{40}$ ]] || die "SHAは小文字の40桁commit SHAで指定してください: $1"
}

acquire_deploy_lock() {
  local lock_file=${SORAMIMIC_DEPLOY_LOCK:-/run/lock/soramimic-video-public-deploy.lock}
  command -v flock >/dev/null || die "flockが必要です"
  exec 9>"$lock_file"
  flock -n 9 || die "別のdeploy/rollback処理が実行中です: $lock_file"
}

symlink_digest() {
  local release_dir=$1
  (cd "$release_dir" && find . -type l -printf '%p\0%l\0' | sort -z | sha256sum | cut -d' ' -f1)
}

assert_final_venv_shebang() {
  local release_dir=$1 expected actual
  expected="#!$release_dir/.venv/bin/python"
  actual=$(head -n 1 "$release_dir/.venv/bin/soramimic-video" 2>/dev/null || true)
  [[ $actual == "$expected" ]] || \
    die "venv scriptのshebangが最終release pathではありません: $actual"
}

verify_release_integrity() {
  local release_dir=$1 expected_sha=$2 record manifest expected_manifest expected_links
  record="$deployments_dir/prepared-$expected_sha.json"
  [[ -f $record ]] || die "prepared recordがありません"
  if jq -e '.bootstrap == true' "$record" >/dev/null 2>&1; then return 0; fi
  manifest=$(jq -er .manifest "$record")
  expected_manifest=$(jq -er .manifest_sha256 "$record")
  expected_links=$(jq -er .symlink_sha256 "$record")
  [[ -f $manifest ]] || die "release manifestがありません"
  [[ $(sha256sum "$manifest" | cut -d' ' -f1) == "$expected_manifest" ]] || \
    die "release manifest自体が変更されています"
  (cd "$release_dir" && sha256sum --check --strict "$manifest" >/dev/null) || \
    die "release fileがprepare後に変更されています"
  [[ $(symlink_digest "$release_dir") == "$expected_links" ]] || \
    die "release symlinkがprepare後に変更されています"
}

release_for_sha() {
  local sha=$1 record
  record="$deployments_dir/prepared-$sha.json"
  [[ -f $record ]] || die "prepare済み記録がありません: $record"
  jq -er '.release_dir' "$record"
}

assert_release() {
  local release_dir=$1 expected_sha=$2 actual record
  [[ $release_dir == "$releases_dir/"* ]] || die "releaseがreleases配下ではありません"
  [[ -d $release_dir && ! -L $release_dir ]] || die "release directoryがありません: $release_dir"
  [[ -x $release_dir/.venv/bin/soramimic-video ]] || die "実行ファイルがありません"
  if [[ ! -f $release_dir/deploy-release.json ]]; then
    record="$deployments_dir/prepared-$expected_sha.json"
    [[ -f $record ]] || die "bootstrap release記録がありません"
    jq -e --arg dir "$release_dir" --arg commit "$expected_sha" \
      '.bootstrap == true and .release_dir == $dir and .commit == $commit' \
      "$record" >/dev/null || die "bootstrap release記録が一致しません"
    return
  fi
  actual=$(jq -er '.commit' "$release_dir/deploy-release.json")
  [[ $actual == "$expected_sha" ]] || die "release metadataのSHAが一致しません"
  [[ -f $release_dir/external/soramimic/frontend/dist/editor.html ]] || \
    die "editor buildがありません"
  verify_release_integrity "$release_dir" "$expected_sha"
}

atomic_link() {
  local target=$1 link=$2 next
  next="${link}.next.$$"
  ln -s "$target" "$next"
  mv -Tf "$next" "$link"
}

wait_json_status() {
  local url=$1 expected=$2 attempts=${3:-30} body
  for _ in $(seq 1 "$attempts"); do
    if body=$(curl -fsS --max-time 3 "$url" 2>/dev/null) && \
      jq -e --arg expected "$expected" '.status == $expected' <<<"$body" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

smoke_public_surface() {
  local base_url=$1 tmp_dir tmp_asset tmp_config simple_ui editor_status asset
  local -a logos ogps assets
  tmp_dir=$(mktemp -d) || return 1
  tmp_config="$tmp_dir/config.json"
  curl -fsS --max-time 5 "$base_url/" >"$tmp_dir/index.html" || \
    { rm -rf -- "$tmp_dir"; return 1; }
  mapfile -t logos < <(grep -Eo '/logo-soramimic-video-v[0-9]+\.png' \
    "$tmp_dir/index.html" | sort -u)
  mapfile -t ogps < <(grep -Eo '/ogp-soramimic-v[0-9]+\.png' \
    "$tmp_dir/index.html" | sort -u)
  [[ ${#logos[@]} -eq 1 && ${#ogps[@]} -eq 1 ]] || \
    { rm -rf -- "$tmp_dir"; return 1; }
  assets=("${logos[0]}" "${ogps[0]}")
  for asset in "${assets[@]}"; do
    tmp_asset="$tmp_dir/$(basename "$asset")"
    curl -fsS --max-time 5 "$base_url$asset" >"$tmp_asset" || \
      { rm -rf -- "$tmp_dir"; return 1; }
    if [[ $(od -An -tx1 -N8 "$tmp_asset" | tr -d ' \n') != 89504e470d0a1a0a ]]; then
      rm -rf -- "$tmp_dir"
      return 1
    fi
  done
  curl -fsS --max-time 5 "$base_url/api/config" >"$tmp_config" || \
    { rm -rf -- "$tmp_dir"; return 1; }
  jq -e '.editor | type == "boolean"' "$tmp_config" >/dev/null || \
    { rm -rf -- "$tmp_dir"; return 1; }
  simple_ui=$(jq -r '.simple_ui // false' "$tmp_config")
  if [[ $simple_ui == true ]]; then
    jq -e '.editor == false' "$tmp_config" >/dev/null || { rm -rf -- "$tmp_dir"; return 1; }
    editor_status=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
      "$base_url/editor/editor.html") || { rm -rf -- "$tmp_dir"; return 1; }
    [[ $editor_status == 404 ]] || { rm -rf -- "$tmp_dir"; return 1; }
  elif [[ $simple_ui == false ]]; then
    jq -e '.editor == true' "$tmp_config" >/dev/null || { rm -rf -- "$tmp_dir"; return 1; }
    curl -fsS --max-time 5 "$base_url/editor/editor.html" >/dev/null || \
      { rm -rf -- "$tmp_dir"; return 1; }
  else
    rm -rf -- "$tmp_dir"
    return 1
  fi
  curl -fsS --max-time 5 "$base_url/editor/wordlists/scientist.csv" | \
    grep -F ',小田,おだ,family,' >/dev/null || { rm -rf -- "$tmp_dir"; return 1; }
  rm -rf -- "$tmp_dir"
  return 0
}

current_release() {
  if [[ -L $current_link ]]; then
    readlink -f "$current_link"
  fi
}

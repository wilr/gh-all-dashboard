#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IGNORE_FILE="${IGNORE_FILE:-$SCRIPT_DIR/ignore}"

# Check dependencies
for cmd in gh jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: $cmd is required." >&2
    exit 1
  fi
done

if ! gh auth status >/dev/null 2>&1; then
  echo "Error: Run 'gh auth login' first." >&2
  exit 1
fi

# Normalize ignore pattern: strip GitHub URL prefixes to owner[/repo] form.
normalize_ignore_pattern() {
  local p="$1"
  local lower
  lower="$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')"

  case "$lower" in
    https://www.github.com/*)
      p="${p:23}" ;;
    http://www.github.com/*)
      p="${p:22}" ;;
    https://github.com/*)
      p="${p:19}" ;;
    http://github.com/*)
      p="${p:18}" ;;
    github.com/*)
      p="${p:11}" ;;
  esac

  # https://github.com/orgs/OrgName → OrgName
  lower="$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]')"
  if [[ "$lower" == orgs/* ]]; then
    p="${p:5}"
  fi

  p="${p%%\?*}"
  p="${p%%\#*}"
  if [[ "$p" == */ && "$p" != */\* ]]; then
    p="${p%/}"
  fi
  printf '%s' "$p"
}

# Returns 0 if repo (owner/name) matches an ignore pattern.
should_ignore() {
  local repo="$1"
  local org="${repo%%/*}"
  local pattern

  [[ -f "$IGNORE_FILE" ]] || return 1

  while IFS= read -r pattern || [[ -n "$pattern" ]]; do
    # Trim whitespace
    pattern="${pattern#"${pattern%%[![:space:]]*}"}"
    pattern="${pattern%"${pattern##*[![:space:]]}"}"

    [[ -z "$pattern" || "$pattern" == \#* ]] && continue

    pattern="$(normalize_ignore_pattern "$pattern")"
    [[ -z "$pattern" ]] && continue

    # Org-wide: "Org", "Org/*", or GitHub org URL
    if [[ "$pattern" == */* ]]; then
      if [[ "$pattern" == */\* ]]; then
        local ignore_org="${pattern%/*}"
        [[ "$org" == "$ignore_org" ]] && return 0
      elif [[ "$repo" == "$pattern" ]]; then
        return 0
      fi
    else
      [[ "$org" == "$pattern" ]] && return 0
    fi
  done < "$IGNORE_FILE"

  return 1
}

# OSC 8 hyperlink for terminals that support it (iTerm2, Terminal, VS Code, etc.)
hyperlink() {
  local url="$1"
  local text="$2"
  printf '\033]8;;%s\033\\%s\033]8;;\033\\' "$url" "$text"
}

echo "Finding repositories where you have admin access..." >&2

# Discover repositories and filter by admin permission.
repos=$(
  gh api --paginate -X GET /user/repos \
    -f affiliation=owner,collaborator,organization_member \
    -F per_page=100 \
    --jq '.[] | select(.permissions.admin == true and .archived == false and .fork == false) | .full_name'
)

if [[ -z "$repos" ]]; then
  echo "No repositories with admin permissions found."
  exit 0
fi

# Apply ignore file
filtered_repos=""
ignored_count=0
while IFS= read -r repo; do
  [[ -z "$repo" ]] && continue
  if should_ignore "$repo"; then
    ignored_count=$((ignored_count + 1))
    continue
  fi
  filtered_repos+="$repo"$'\n'
done <<< "$repos"

repos="${filtered_repos%$'\n'}"

if [[ -z "$repos" ]]; then
  echo "No repositories left after applying ignore rules ($ignored_count ignored)."
  exit 0
fi

repo_count=$(printf '%s\n' "$repos" | wc -l | tr -d ' ')
echo "Found $repo_count repositories ($ignored_count ignored)." >&2
echo >&2

# Collect issue rows as TSV: repo, number, assignee, updated, title, url
rows=$(
  while IFS= read -r repo; do
    [[ -z "$repo" ]] && continue

    echo "Fetching $repo..." >&2

    gh api --paginate -X GET "repos/$repo/issues" \
      -f state=open \
      -F per_page=100 \
      --jq '
        .[]
        | select(.pull_request == null)
        | [
            (.repository_url | split("/")[-2:] | join("/")),
            (.number | tostring),
            ([.assignees[].login] | if length == 0 then "—" else join(", ") end),
            (.updated_at[:10]),
            (.title | gsub("[\t\r\n]"; " ")),
            .html_url
          ]
        | @tsv
      '
  done <<< "$repos"
)

if [[ -z "${rows:-}" ]]; then
  echo "No open issues found."
  exit 0
fi

# Column widths (display text, not raw URLs)
w_repo=0
w_issue=0
w_assignee=0
w_updated=10
w_title=0

while IFS=$'\t' read -r repo number assignee updated title url; do
  [[ ${#repo} -gt $w_repo ]] && w_repo=${#repo}
  issue_label="#$number"
  [[ ${#issue_label} -gt $w_issue ]] && w_issue=${#issue_label}
  [[ ${#assignee} -gt $w_assignee ]] && w_assignee=${#assignee}
  [[ ${#title} -gt $w_title ]] && w_title=${#title}
done <<< "$rows"

# Cap title width for readability
max_title=60
[[ $w_title -gt $max_title ]] && w_title=$max_title

# Minimum widths from headers
[[ $w_repo -lt 10 ]] && w_repo=10
[[ $w_issue -lt 5 ]] && w_issue=5
[[ $w_assignee -lt 8 ]] && w_assignee=8

print_row() {
  local repo="$1" number="$2" assignee="$3" updated="$4" title="$5" url="$6"
  local issue_label="#$number"
  local display_title="$title"

  if [[ ${#display_title} -gt $max_title ]]; then
    display_title="${display_title:0:$((max_title - 1))}…"
  fi

  local repo_url="https://github.com/$repo"
  local linked_repo linked_issue

  linked_repo=$(hyperlink "$repo_url" "$(printf "%-*s" "$w_repo" "$repo")")
  linked_issue=$(hyperlink "$url" "$(printf "%-*s" "$w_issue" "$issue_label")")

  printf '%s  %s  %-*s  %-*s  %s\n' \
    "$linked_repo" \
    "$linked_issue" \
    "$w_assignee" "$assignee" \
    "$w_updated" "$updated" \
    "$display_title"
}

# Header
header_width=$((w_repo + w_issue + w_assignee + w_updated + w_title + 8))
printf '%-*s  %-*s  %-*s  %-*s  %s\n' \
  "$w_repo" "REPOSITORY" \
  "$w_issue" "ISSUE" \
  "$w_assignee" "ASSIGNEE" \
  "$w_updated" "UPDATED" \
  "TITLE"
printf '%*s\n' "$header_width" '' | tr ' ' '─'

while IFS=$'\t' read -r repo number assignee updated title url; do
  print_row "$repo" "$number" "$assignee" "$updated" "$title" "$url"
done <<< "$rows"

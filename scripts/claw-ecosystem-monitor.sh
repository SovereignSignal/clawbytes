#!/bin/bash
# claw-ecosystem-monitor.sh - Monitor the Claw ecosystem for news and releases
# Now with dynamic discovery layer!
#
# Usage:
#   ./claw-ecosystem-monitor.sh --mode check     # Check known sources (default)
#   ./claw-ecosystem-monitor.sh --mode discover  # Hunt for new projects
#   ./claw-ecosystem-monitor.sh --mode both      # Check + discover

set -euo pipefail
# allexport (a parent `set -a`, or SHELLOPTS=allexport) marks every assignment
# for export. `local` also keeps -x when that name arrived already exported.
# A growing JSON value then sits in the environment of every child. Linux
# rejects execve once any argument or environment string exceeds MAX_ARG_STRLEN
# (128 KiB), which bash logs as "jq: Argument list too long". The failing
# append is inside a command substitution, so the discover run still exits 0
# with whatever was accumulated after the wipe. Turn allexport off before any
# JSON is built; call sites below also unexport names that may have inherited -x.
set +a

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="${WORKSPACE:-$(dirname "$SCRIPT_DIR")}"
MEMORY_DIR="${CLAWBYTES_MEMORY_DIR:-${WORKSPACE_DIR}/memory}"
STATE_FILE="${MEMORY_DIR}/claw-ecosystem-state.json"
SOURCES_FILE="${MEMORY_DIR}/claw-ecosystem-sources.json"
OUTPUT_FILE="${MEMORY_DIR}/claw-ecosystem-new-items.json"
DISCOVERIES_FILE="${MEMORY_DIR}/claw-ecosystem-discoveries.json"
CREDS_FILE="${WORKSPACE_DIR}/CREDS.md"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# Rate limiting - be nice to APIs
GITHUB_DELAY=0.2  # seconds between GitHub requests (0.5s = 7200/hr, well within 5000/hr auth'd limit)
HN_DELAY=0.5
# /search is not the core API. Authenticated search is about 30 requests
# per minute; unauthenticated is about 10. discover_github stays under that
# cap. It does not space every query out to 2s/6s — nine queries already fit,
# and a 6s gap times the awesome-list crawl would blow the script budget
# (SCRIPT_MAX_DURATION in main; the older comment there also mentions 60s).
GITHUB_SEARCH_LIMIT_AUTH=30
GITHUB_SEARCH_LIMIT_ANON=10
# One 403/429 backoff. Clipped to the time left under SCRIPT_MAX_DURATION
# and hard-capped so a multi-hour Retry-After cannot stall the run.
GITHUB_SEARCH_BACKOFF_CAP=20

# Star thresholds for discovery
MIN_STARS_NEW=50      # Min stars for repos <7 days old
MIN_STARS_DEFAULT=100 # Min stars for older repos

MODE="check"

get_cred() {
    local section="$1"
    local key="$2"
    [[ -f "$CREDS_FILE" ]] || return 0
    awk -v section="$section" -v key="$key" '
        $0 ~ /^## / { in_section = ($0 == "## " section) }
        in_section {
            line = $0
            gsub(/\*\*/, "", line)
            if (index(line, "- " key ": ") == 1) {
                sub("- " key ": ", "", line)
                print line
                exit
            }
        }
    ' "$CREDS_FILE" 2>/dev/null
}

load_tokens() {
    if [[ -z "$GITHUB_TOKEN" ]]; then GITHUB_TOKEN="$(get_cred "GitHub API" "Token" || true)"; fi
}

github_api() {
    local url="$1"
    if [[ -n "$GITHUB_TOKEN" ]]; then
        curl -sf --max-time 15 -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" "$url"
    else
        curl -sf --max-time 15 "$url"
    fi
}

# Brave Search helper removed 2026-06-25 (Brave deprecated).

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode)
            MODE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Initialize files if missing
init_files() {
    mkdir -p "${WORKSPACE_DIR}/memory"
    
    if [[ ! -f "$STATE_FILE" ]]; then
        cat > "$STATE_FILE" << 'EOF'
{
  "lastCheck": null,
  "lastDiscovery": null,
  "lastWeeklyDigest": null,
  "lastSeenReleases": {},
  "lastSeenHNStories": [],
  "lastSeenSkills": [],
  "knownRepoIds": [],
  "channelId": null,
  "botToken": null
}
EOF
    fi
    
    if [[ ! -f "$SOURCES_FILE" ]]; then
        cat > "$SOURCES_FILE" << 'EOF'
{
  "curated": [],
  "dynamic": [],
  "_meta": {
    "lastUpdated": null,
    "totalDiscovered": 0
  }
}
EOF
    fi
}

# Load JSON file
load_json() {
    local file="$1"
    cat "$file" 2>/dev/null || echo "{}"
}

# Save JSON to file
save_json() {
    local file="$1"
    local data="$2"
    # `data` may have inherited -x. Drop it before jq so a large document
    # is not also an environment string.
    unexport data
    echo "$data" | jq '.' > "$file"
}

# Scratch dir for JSON handed to jq through --slurpfile / stdin instead of argv.
JQ_TMP_DIR=""

jq_tmp_init() {
    if [[ -z "$JQ_TMP_DIR" ]]; then
        JQ_TMP_DIR=$(mktemp -d)
    fi
}

# Clear an inherited export flag. No-op when the name is unset.
# `export -n` sees the caller's local; it does not create a new one.
unexport() {
    local name
    for name in "$@"; do
        # ${name?} is the name to unexport; the ? quiets shellcheck SC2163.
        export -n "${name?}"
    done
}

# Append the single JSON value in $2 to the JSON array stored in $1.
# --slurpfile binds the value as a one-element array, so this matches
# the old `. + [$repo]` / `. + [$item]` programs.
jq_append_element() {
    local array_file="$1"
    local elem_file="$2"
    local tmp
    tmp=$(mktemp "$JQ_TMP_DIR/append.XXXXXX")
    jq --slurpfile elem "$elem_file" '. + $elem' "$array_file" > "$tmp"
    mv "$tmp" "$array_file"
}

# Merge one {repo, tag} object into a repo→tag map. Matches
# `. + {($item.repo): $item.tag}` when $item was a single object.
jq_merge_baseline() {
    local object_file="$1"
    local elem_file="$2"
    local tmp
    tmp=$(mktemp "$JQ_TMP_DIR/base.XXXXXX")
    jq --slurpfile item "$elem_file" '. + {($item[0].repo): $item[0].tag}' "$object_file" > "$tmp"
    mv "$tmp" "$object_file"
}

# Write a shell variable to a file. printf is a builtin, so the value
# never becomes an execve argument.
write_json_var() {
    local name="$1"
    local dest="$2"
    printf '%s\n' "${!name}" > "$dest"
}

# Get all repos from sources (curated + dynamic)
get_all_repos() {
    jq -r '(.curated + .dynamic) | .[].repo // empty' "$SOURCES_FILE" 2>/dev/null | sort -u
}

# Check if repo already known
is_repo_known() {
    local repo="$1"
    local normalized
    normalized=$(echo "$repo" | tr '[:upper:]' '[:lower:]')
    
    # Check both curated and dynamic
    local found
    found=$(jq -r --arg repo "$normalized" '
        (.curated + .dynamic) | 
        [.[].repo | ascii_downcase] | 
        if index($repo) then "yes" else "no" end
    ' "$SOURCES_FILE" 2>/dev/null || echo "no")
    
    [[ "$found" == "yes" ]]
}

# Fetch repo metadata from GitHub
fetch_repo_metadata() {
    unexport response
    local repo="$1"
    local url="https://api.github.com/repos/${repo}"

    local response
    response=$(github_api "$url" 2>/dev/null) || {
        echo "{}"
        return
    }
    
    echo "$response" | jq '{
        repo: .full_name,
        name: .name,
        description: (.description // ""),
        stars: .stargazers_count,
        url: .html_url,
        topics: (.topics // []),
        language: .language,
        createdAt: .created_at,
        updatedAt: .updated_at,
        pushedAt: .pushed_at,
        forks: .forks_count,
        openIssues: .open_issues_count
    }'
}

# Add repo to sources.json (curated or dynamic)
add_repo_to_sources() {
    unexport repo_data sources
    local repo_data="$1"
    local section="$2"  # "curated" or "dynamic"

    local sources
    sources=$(load_json "$SOURCES_FILE")

    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Add discoveredAt timestamp
    repo_data=$(printf '%s\n' "$repo_data" | jq --arg ts "$now" '. + {discoveredAt: $ts}')

    jq_tmp_init
    local repo_file sources_file
    repo_file=$(mktemp "$JQ_TMP_DIR/repo.XXXXXX")
    sources_file=$(mktemp "$JQ_TMP_DIR/sources.XXXXXX")
    write_json_var repo_data "$repo_file"
    write_json_var sources "$sources_file"

    # $repo is a one-element array from --slurpfile, so add $repo, not [$repo].
    sources=$(jq --slurpfile repo "$repo_file" --arg section "$section" '
        .[$section] = (.[$section] + $repo) |
        ._meta.lastUpdated = now |
        ._meta.totalDiscovered = ((.curated | length) + (.dynamic | length))
    ' "$sources_file")

    save_json "$SOURCES_FILE" "$sources"
}

# ============ DISCOVERY FUNCTIONS ============

# Clip a search wait so it cannot run past the script budget. script_start
# and SCRIPT_MAX_DURATION are set in main and visible here (bash locals).
github_search_bound() {
    local want="$1"
    local now elapsed remaining cap start budget reserve
    reserve=15
    start="${script_start:-}"
    budget="${SCRIPT_MAX_DURATION:-300}"
    if [[ ! "$want" =~ ^[0-9]+$ ]]; then
        want=0
    fi
    if [[ -z "$start" || ! "$start" =~ ^[0-9]+$ ]]; then
        if (( want > GITHUB_SEARCH_BACKOFF_CAP )); then
            printf '%s\n' "$GITHUB_SEARCH_BACKOFF_CAP"
        else
            printf '%s\n' "$want"
        fi
        return 0
    fi
    now=$(date +%s)
    elapsed=$((now - start))
    if (( elapsed < 0 )); then
        elapsed=0
    fi
    remaining=$((budget - elapsed))
    if (( remaining <= reserve )); then
        printf '%s\n' 0
        return 0
    fi
    cap=$((remaining - reserve))
    if (( cap > GITHUB_SEARCH_BACKOFF_CAP )); then
        cap=$GITHUB_SEARCH_BACKOFF_CAP
    fi
    if (( want > cap )); then
        printf '%s\n' "$cap"
    else
        printf '%s\n' "$want"
    fi
}

# Seconds to wait after a 403/429. Retry-After wins over X-RateLimit-Reset.
# A present Retry-After of 0 means "retry immediately" — do not fall through
# to a far-future reset timestamp.
github_search_retry_wait() {
    local now want ra rs ts
    now=$(date +%s)
    ra="${GITHUB_SEARCH_RETRY_AFTER:-}"
    rs="${GITHUB_SEARCH_RESET:-}"
    want=0
    if [[ -n "$ra" ]]; then
        if [[ "$ra" =~ ^[0-9]+$ ]]; then
            want=$ra
        else
            ts=$(date -d "$ra" +%s 2>/dev/null || true)
            if [[ "${ts:-}" =~ ^[0-9]+$ ]] && (( ts > now )); then
                want=$((ts - now))
            else
                want=2
            fi
        fi
    elif [[ "$rs" =~ ^[0-9]+$ ]] && (( rs > now )); then
        want=$((rs - now))
    else
        want=2
    fi
    github_search_bound "$want"
}

# Stay at or under the search per-minute cap. Returns 1 when the only way
# to comply is a wait the script budget cannot afford — caller skips the
# rest of the queries instead of hammering.
github_search_pace() {
    local limit="$1"
    local now ts needed wait
    local -a kept=()
    now=$(date +%s)
    for ts in "${GITHUB_SEARCH_AT[@]}"; do
        if (( now - ts < 60 )); then
            kept+=("$ts")
        fi
    done
    if (( ${#kept[@]} >= limit )); then
        needed=$((60 - (now - kept[0])))
        if (( needed < 1 )); then
            needed=1
        fi
        wait=$(github_search_bound "$needed")
        if (( wait < needed )); then
            return 1
        fi
        if (( wait > 0 )); then
            sleep "$wait"
        fi
        now=$(date +%s)
        kept=()
        for ts in "${GITHUB_SEARCH_AT[@]}"; do
            if (( now - ts < 60 )); then
                kept+=("$ts")
            fi
        done
    fi
    GITHUB_SEARCH_AT=("${kept[@]}")
    GITHUB_SEARCH_AT+=("$now")
    return 0
}

github_search_read_headers() {
    local hdr="$1"
    local parsed status retry_after reset_at
    GITHUB_SEARCH_STATUS=""
    GITHUB_SEARCH_RETRY_AFTER=""
    GITHUB_SEARCH_RESET=""
    [[ -f "$hdr" ]] || return 0
    parsed=$(tr -d '\r' < "$hdr" | awk '
        {
            key = tolower($1)
        }
        key ~ /^http\// { status = $2 }
        key == "retry-after:" { retry = $2 }
        key == "x-ratelimit-reset:" { reset = $2 }
        END {
            printf "%s\n%s\n%s\n", status, retry, reset
        }
    ')
    status=$(printf '%s\n' "$parsed" | sed -n '1p')
    retry_after=$(printf '%s\n' "$parsed" | sed -n '2p')
    reset_at=$(printf '%s\n' "$parsed" | sed -n '3p')
    GITHUB_SEARCH_STATUS=$status
    GITHUB_SEARCH_RETRY_AFTER=$retry_after
    GITHUB_SEARCH_RESET=$reset_at
}

# Search request. Body stays in $2 — never a shell variable — so a large
# result cannot trip MAX_ARG_STRLEN the way --argjson used to.
# Sends Authorization only when GITHUB_TOKEN is already set (env or load_tokens).
# A curl stub that ignores -D and prints JSON still counts as HTTP 200.
github_search_once() {
    local url="$1"
    local dest="$2"
    local hdr
    local -a args
    jq_tmp_init
    hdr=$(mktemp "$JQ_TMP_DIR/gh-hdr.XXXXXX")
    : > "$hdr"
    args=(-s --max-time 15 -D "$hdr" -H "Accept: application/vnd.github+json" -H "User-Agent: ClawBytes-Monitor/1.0")
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
    fi
    if ! curl "${args[@]}" "$url" > "$dest"; then
        return 1
    fi
    github_search_read_headers "$hdr"
    if [[ -z "$GITHUB_SEARCH_STATUS" ]]; then
        if jq empty "$dest" >/dev/null 2>&1; then
            GITHUB_SEARCH_STATUS=200
        else
            return 1
        fi
    fi
    return 0
}

github_search_skip() {
    local status="$1"
    echo "⚠️ GitHub search blocked (HTTP ${status}); skipping remaining search queries" >&2
}

# GitHub Search Discovery
discover_github() {
    echo "🔍 GitHub Discovery..." >&2
    unexport discoveries response items item repo_data
    jq_tmp_init
    local discoveries_file repo_file body_file
    discoveries_file=$(mktemp "$JQ_TMP_DIR/gh-disc.XXXXXX")
    repo_file=$(mktemp "$JQ_TMP_DIR/gh-repo.XXXXXX")
    body_file=$(mktemp "$JQ_TMP_DIR/gh-body.XXXXXX")
    printf '%s\n' '[]' > "$discoveries_file"

    # Search queries
    local queries=(
        "topic:ai-agent+topic:openclaw&sort=updated&per_page=10"
        "openclaw+alternative+agent&sort=stars&per_page=10"
        "personal+ai+agent+self-hosted&sort=updated&per_page=10"
        "claw+agent+in:name&sort=stars&per_page=20"
        "ai+coding+agent+framework&sort=stars&per_page=10"
        "mcp+agent+framework&sort=updated&per_page=10"
        "topic:coding-agent&sort=stars&per_page=10"
        "topic:mcp-server&sort=stars&per_page=10"
        "topic:ai-agent+created:>2026-01-01&sort=stars&per_page=10"
    )

    local -a GITHUB_SEARCH_AT=()
    local search_retried=0
    local limit=$GITHUB_SEARCH_LIMIT_ANON
    local query url status wait
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        limit=$GITHUB_SEARCH_LIMIT_AUTH
    fi

    for query in "${queries[@]}"; do
        url="https://api.github.com/search/repositories?q=${query}"
        if ! github_search_pace "$limit"; then
            echo "⚠️ GitHub search rate limit reached; skipping remaining search queries" >&2
            break
        fi
        if ! github_search_once "$url" "$body_file"; then
            sleep "$GITHUB_DELAY"
            continue
        fi
        status="$GITHUB_SEARCH_STATUS"
        if [[ "$status" == "403" || "$status" == "429" ]]; then
            if (( search_retried == 0 )); then
                search_retried=1
                wait=$(github_search_retry_wait)
                if (( wait > 0 )); then
                    sleep "$wait"
                fi
                if ! github_search_pace "$limit"; then
                    github_search_skip "$status"
                    break
                fi
                if ! github_search_once "$url" "$body_file"; then
                    github_search_skip "$status"
                    break
                fi
                status="$GITHUB_SEARCH_STATUS"
            fi
            if [[ "$status" == "403" || "$status" == "429" ]]; then
                github_search_skip "$status"
                break
            fi
        fi
        if [[ ! "$status" =~ ^2[0-9][0-9]$ ]]; then
            sleep "$GITHUB_DELAY"
            continue
        fi

        while IFS= read -r item; do
            [[ -z "$item" || "$item" == "null" ]] && continue

            local repo_name stars created_at
            repo_name=$(echo "$item" | jq -r '.full_name')
            stars=$(echo "$item" | jq -r '.stargazers_count // 0')
            created_at=$(echo "$item" | jq -r '.created_at // ""')

            # Skip if already known
            if is_repo_known "$repo_name"; then
                continue
            fi

            # Check star threshold
            local threshold=$MIN_STARS_DEFAULT
            if [[ -n "$created_at" ]]; then
                local created_epoch
                created_epoch=$(date -d "$created_at" +%s 2>/dev/null || echo 0)
                local week_ago
                week_ago=$(date -d "7 days ago" +%s)

                if [[ $created_epoch -gt $week_ago ]]; then
                    threshold=$MIN_STARS_NEW
                fi
            fi

            if [[ $stars -ge $threshold ]]; then
                printf '%s\n' "$item" | jq '{
                    repo: .full_name,
                    name: .name,
                    description: (.description // ""),
                    stars: .stargazers_count,
                    url: .html_url,
                    topics: (.topics // []),
                    language: .language,
                    createdAt: .created_at,
                    updatedAt: .updated_at,
                    source: "github-search",
                    isNew: true
                }' > "$repo_file"

                jq_append_element "$discoveries_file" "$repo_file"
                echo "   📦 Found: $repo_name (⭐ $stars)" >&2
            fi
        done < <(jq -c '.items // [] | .[]' "$body_file")

        sleep "$GITHUB_DELAY"
    done

    cat "$discoveries_file"
}

# Awesome List Crawler
discover_awesome_lists() {
    echo "📚 Crawling Awesome Lists..." >&2
    unexport discoveries content urls repo_data
    jq_tmp_init
    local discoveries_file repo_file
    discoveries_file=$(mktemp "$JQ_TMP_DIR/aw-disc.XXXXXX")
    repo_file=$(mktemp "$JQ_TMP_DIR/aw-repo.XXXXXX")
    printf '%s\n' '[]' > "$discoveries_file"

    local lists=(
        "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md"
        "https://raw.githubusercontent.com/kyrolabs/awesome-agents/main/README.md"
        "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"
        "https://raw.githubusercontent.com/hesreallyhim/awesome-claude-code/main/README.md"
        "https://raw.githubusercontent.com/sourcegraph/awesome-code-ai/main/README.md"
        "https://raw.githubusercontent.com/bradAGI/awesome-cli-coding-agents/main/README.md"
    )
    
    for list_url in "${lists[@]}"; do
        local content
        content=$(curl -sf --max-time 30 "$list_url" 2>/dev/null) || continue
        
        # Extract GitHub URLs
        local urls
        urls=$(echo "$content" | grep -oE 'https://github\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+' | sort -u)

        # Large lists (awesome-mcp-servers has thousands of entries) would blow
        # the run budget at one metadata call per repo. Cap unknown candidates
        # per list per run; known repos skip before counting, so successive
        # weekly runs resume deeper into the list.
        local checked=0
        local max_per_list=30

        while IFS= read -r url; do
            [[ -z "$url" ]] && continue

            if (( checked >= max_per_list )); then
                echo "   ⚠️ Hit $max_per_list-candidate cap for this list; deferring the rest to the next run" >&2
                break
            fi

            # Extract repo name
            local repo_name
            repo_name=$(echo "$url" | sed 's|https://github.com/||')

            # Skip if already known
            if is_repo_known "$repo_name"; then
                continue
            fi

            checked=$((checked + 1))

            # Fetch metadata
            local repo_data
            repo_data=$(fetch_repo_metadata "$repo_name")
            
            if [[ -n "$repo_data" && "$repo_data" != "{}" ]]; then
                local stars
                stars=$(echo "$repo_data" | jq -r '.stars // 0')
                
                if [[ $stars -ge $MIN_STARS_DEFAULT ]]; then
                    printf '%s\n' "$repo_data" | jq '. + {source: "awesome-list", isNew: true}' > "$repo_file"
                    jq_append_element "$discoveries_file" "$repo_file"
                    echo "   📚 Found: $repo_name (⭐ $stars)" >&2
                fi
            fi
            
            sleep $GITHUB_DELAY
        done <<< "$urls"
    done

    cat "$discoveries_file"
}

# Hacker News Discovery
discover_hackernews() {
    echo "🔶 HN Discovery..." >&2
    unexport discoveries github_repos response hits hit repo_data
    jq_tmp_init
    local discoveries_file repo_file
    discoveries_file=$(mktemp "$JQ_TMP_DIR/hn-disc.XXXXXX")
    repo_file=$(mktemp "$JQ_TMP_DIR/hn-repo.XXXXXX")
    printf '%s\n' '[]' > "$discoveries_file"
    local github_repos="[]"
    
    local queries=(
        "self-hosted+ai+agent"
        "personal+ai+agent+framework"
        "openclaw+alternative"
        "ai+coding+assistant+local"
        "mcp+model+context+protocol"
    )
    
    for query in "${queries[@]}"; do
        local url="https://hn.algolia.com/api/v1/search?query=${query}&tags=story&hitsPerPage=5"
        local response
        response=$(curl -sf --max-time 30 "$url" 2>/dev/null) || continue
        
        # Look for GitHub links in story URLs or comments
        local hits
        hits=$(echo "$response" | jq '.hits // []')
        
        while IFS= read -r hit; do
            local story_url
            story_url=$(echo "$hit" | jq -r '.url // ""')
            
            # Check if it's a GitHub repo
            if [[ "$story_url" =~ github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+) ]]; then
                local repo_name="${BASH_REMATCH[1]}"
                
                if ! is_repo_known "$repo_name"; then
                    github_repos=$(echo "$github_repos" | jq --arg repo "$repo_name" '. + [$repo]')
                fi
            fi
        done < <(echo "$hits" | jq -c '.[]')
        
        sleep $HN_DELAY
    done
    
    # Fetch metadata for found repos
    local unique_repos
    unique_repos=$(echo "$github_repos" | jq -r '.[] | @text' | sort -u)
    
    while IFS= read -r repo_name; do
        [[ -z "$repo_name" ]] && continue
        
        local repo_data
        repo_data=$(fetch_repo_metadata "$repo_name")
        
        if [[ -n "$repo_data" && "$repo_data" != "{}" ]]; then
            local stars
            stars=$(echo "$repo_data" | jq -r '.stars // 0')
            
            if [[ $stars -ge $MIN_STARS_NEW ]]; then
                printf '%s\n' "$repo_data" | jq '. + {source: "hackernews", isNew: true}' > "$repo_file"
                jq_append_element "$discoveries_file" "$repo_file"
                echo "   🔶 Found: $repo_name (⭐ $stars)" >&2
            fi
        fi
        
        sleep $GITHUB_DELAY
    done <<< "$unique_repos"

    cat "$discoveries_file"
}

# Brave Search discovery removed 2026-06-25 (Brave deprecated).

# ============ CHECK FUNCTIONS ============

# Fetch GitHub releases for a repo
fetch_github_releases() {
    local repo="$1"
    local url="https://api.github.com/repos/${repo}/releases?per_page=5"
    
    github_api "$url" 2>/dev/null || echo "[]"
}

check_clawhub_skills() {
    # ClawHub skill discovery removed 2026-06-25 (Brave deprecated; the only
    # backend was a Brave web search). Returns no items until a replacement
    # source is wired in. State (lastSeenSkills) stays untouched.
    local state="$1"
    echo "[]"
}

# Check all known repos for new releases (parallelized with progress)
check_github_releases() {
    unexport state new_releases baselines releases item repos
    local state="$1"
    local batch_count=0
    local start_time
    start_time=$(date +%s)
    local max_duration=50  # hard cap at 50s
    
    local repos
    repos=$(get_all_repos)
    local total_repos
    total_repos=$(echo "$repos" | grep -c '^' || echo 0)
    
    echo "   Checking $total_repos repos..." >&2
    
    # Use temp dir for parallel workers
    local tmpdir
    tmpdir=$(mktemp -d)
    local worker_count=0
    local max_workers=8
    
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        
        # Timeout guard - script-wide + per-loop
        local now
        now=$(date +%s)
        if (( now - start_time > max_duration )); then
            echo "   ⚠️ Approaching timeout, skipping remaining repos" >&2
            break
        fi
        # Global script timeout
        if (( now - script_start > SCRIPT_MAX_DURATION )); then
            echo "   ⏱️ Global timeout reached, skipping remaining repos" >&2
            break
        fi
        
        # Throttle workers
        while [[ $(jobs -p | wc -l) -ge $max_workers ]]; do
            sleep 0.1
        done
        
        # Background worker per repo
        (
            unexport releases
            local releases
            releases=$(fetch_github_releases "$repo")
            
            if [[ "$releases" != "[]" && "$releases" != "" ]]; then
                local latest_tag
                latest_tag=$(echo "$releases" | jq -r '.[0].tag_name // empty' 2>/dev/null || echo "")
                
                if [[ -n "$latest_tag" ]]; then
                    local seen_tag
                    seen_tag=$(echo "$state" | jq -r --arg repo "$repo" '.lastSeenReleases[$repo] // empty')
                    local safe_name="${repo//\//_}"

                    if [[ -z "$seen_tag" ]]; then
                        # First sighting records the current tag and emits nothing.
                        jq -n --arg repo "$repo" --arg tag "$latest_tag" \
                            '{repo:$repo, tag:$tag}' > "$tmpdir/${safe_name}.baseline"
                    elif [[ "$latest_tag" != "$seen_tag" ]]; then
                        # Do not mark this tag seen here. collect hands it to the
                        # backlog and then writes lastSeenReleases.
                        echo "$releases" | jq --arg repo "$repo" '.[0] | {
                            repo: $repo,
                            tag: .tag_name,
                            name: .name,
                            url: .html_url,
                            published: .published_at,
                            body: (.body | if . then .[0:500] else "" end),
                            prerelease: .prerelease,
                            draft: .draft
                        }' > "$tmpdir/${safe_name}.json"
                    fi
                fi
            fi
        ) &
        
        worker_count=$((worker_count + 1))
        # Progress output every 10 repos
        if (( worker_count % 10 == 0 )); then
            echo "   ... $worker_count/$total_repos" >&2
        fi
        
        sleep 0.05  # Reduced from 0.2s since we're parallel
    done <<< "$repos"
    
    # Wait for all workers
    wait
    
    # Collect results. Baselines are repo→tag and are not news.
    # Worker files are already JSON; slurp them instead of --argjson so a
    # long release name never lands on argv or in an exported variable.
    jq_tmp_init
    local baselines_file releases_file
    baselines_file=$(mktemp "$JQ_TMP_DIR/baselines.XXXXXX")
    releases_file=$(mktemp "$JQ_TMP_DIR/releases.XXXXXX")
    printf '%s\n' '{}' > "$baselines_file"
    printf '%s\n' '[]' > "$releases_file"
    for f in "$tmpdir"/*.baseline; do
        [[ -f "$f" ]] || continue
        jq_merge_baseline "$baselines_file" "$f"
    done

    for f in "$tmpdir"/*.json; do
        [[ -f "$f" ]] || continue
        jq_append_element "$releases_file" "$f"
        batch_count=$((batch_count + 1))
    done

    rm -rf "$tmpdir"

    if [[ -n "${BASELINE_MAP_FILE:-}" ]]; then
        cat "$baselines_file" > "$BASELINE_MAP_FILE"
    fi

    echo "   Found $batch_count new release(s)" >&2
    cat "$releases_file"
}

# Check Hacker News for relevant stories
check_hackernews() {
    unexport state new_stories response hits seen_ids stories
    local state="$1"
    local new_stories="[]"
    jq_tmp_init
    local seen_file
    seen_file=$(mktemp "$JQ_TMP_DIR/seen.XXXXXX")
    
    local queries=("openclaw" "claw+agent" "hermes+agent" "claude+code" "ai+coding+agent" "mcp+agent" "codex+agent" "browser+agent")
    local min_created
    min_created=$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print(int((datetime.now(timezone.utc) - timedelta(days=14)).timestamp()))
PY
)
    
    for query in "${queries[@]}"; do
        local url="https://hn.algolia.com/api/v1/search?query=${query}&tags=story&hitsPerPage=10&numericFilters=created_at_i>${min_created}"
        local response
        response=$(curl -sf --max-time 30 "$url" 2>/dev/null || echo '{"hits":[]}')
        
        local hits
        hits=$(echo "$response" | jq '.hits // []')
        
        printf '%s\n' "$state" | jq '.lastSeenHNStories' > "$seen_file"

        local stories
        stories=$(printf '%s\n' "$hits" | jq --slurpfile seen "$seen_file" '
            [.[] | select(.objectID as $id | ($seen[0] | index($id)) == null)] |
            [.[] | {
                id: .objectID,
                title: .title,
                url: .url,
                hn_url: ("https://news.ycombinator.com/item?id=" + .objectID),
                points: .points,
                author: .author,
                created: .created_at,
                comments: .num_comments
            }]
        ')
        
        new_stories=$(echo "$new_stories $stories" | jq -s 'add | unique_by(.id)')
        
        sleep $HN_DELAY
    done
    
    echo "$new_stories"
}

# Update state with new findings
update_state() {
    unexport state baselines new_hn new_skills new_hn_ids new_skill_ids
    local state="$1"
    local baselines="$2"  # object of repo → tag for first sightings only
    local new_hn="$3"
    local new_skills="$4"

    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    jq_tmp_init
    local base_file hn_file skill_file state_file tmp
    base_file=$(mktemp "$JQ_TMP_DIR/upd-base.XXXXXX")
    hn_file=$(mktemp "$JQ_TMP_DIR/upd-hn.XXXXXX")
    skill_file=$(mktemp "$JQ_TMP_DIR/upd-skill.XXXXXX")
    state_file=$(mktemp "$JQ_TMP_DIR/upd-state.XXXXXX")
    tmp=$(mktemp "$JQ_TMP_DIR/upd-out.XXXXXX")

    # First-sight baselines only. An emitted release stays unseen until
    # collect hands it to the backlog.
    write_json_var baselines "$base_file"
    write_json_var state "$state_file"
    jq --slurpfile new "$base_file" '
        .lastSeenReleases = (.lastSeenReleases + $new[0])
    ' "$state_file" > "$tmp"
    mv "$tmp" "$state_file"

    # Update HN story IDs
    printf '%s\n' "$new_hn" | jq '[.[].id]' > "$hn_file"
    jq --slurpfile ids "$hn_file" '
        .lastSeenHNStories = ((.lastSeenHNStories + $ids[0]) | unique | .[-100:])
    ' "$state_file" > "$tmp"
    mv "$tmp" "$state_file"

    printf '%s\n' "$new_skills" | jq '[.[].id]' > "$skill_file"
    jq --slurpfile ids "$skill_file" '
        .lastSeenSkills = ((.lastSeenSkills + $ids[0]) | unique | .[-100:])
    ' "$state_file" > "$tmp"
    mv "$tmp" "$state_file"

    jq --arg ts "$now" '.lastCheck = $ts' "$state_file"
}

# ============ MAIN FUNCTIONS ============

run_check() {
    echo "🦀 Claw Ecosystem Monitor - CHECK MODE" >&2
    echo "=======================================" >&2
    echo "📅 $(date)" >&2
    echo "" >&2

    unexport state new_releases baselines new_hn new_hf new_skills output
    local state
    state=$(load_json "$STATE_FILE")

    load_tokens
    
    local repo_count
    repo_count=$(get_all_repos | wc -l)
    echo "📊 Monitoring $repo_count repositories" >&2
    echo "" >&2
    
    local baseline_map_file
    baseline_map_file=$(mktemp)
    echo '{}' > "$baseline_map_file"
    BASELINE_MAP_FILE="$baseline_map_file"
    export BASELINE_MAP_FILE

    echo "📦 Checking GitHub releases..." >&2
    local new_releases
    new_releases=$(check_github_releases "$state")
    local baselines
    baselines=$(cat "$baseline_map_file")
    rm -f "$baseline_map_file"
    local release_count
    release_count=$(echo "$new_releases" | jq 'length')
    echo "   Found $release_count new release(s)" >&2

    local new_hn new_hf new_skills
    # Test hook: exercise the release baseline without hitting HN or HF.
    if [[ "${CLAWBYTES_ECOSYSTEM_RELEASES_ONLY:-}" == "1" ]]; then
        new_hn="[]"
        new_hf="[]"
        new_skills="[]"
    else
        echo "🔶 Checking Hacker News..." >&2
        new_hn=$(check_hackernews "$state")
        local hn_count
        hn_count=$(echo "$new_hn" | jq 'length')
        echo "   Found $hn_count new story/stories" >&2

        echo "📄 Checking HuggingFace Papers..." >&2
        new_hf=$(python3 "${SCRIPT_DIR}/claw-hf-papers.py" --quiet 2>/dev/null || echo "[]")
        local hf_count
        hf_count=$(echo "$new_hf" | jq 'length' 2>/dev/null || echo 0)
        echo "   Found $hf_count new paper(s)" >&2

        echo "🛍️ Checking ClawHub..." >&2
        new_skills=$(check_clawhub_skills "$state")
        local skill_count
        skill_count=$(echo "$new_skills" | jq 'length')
        echo "   Found $skill_count new skill item(s)" >&2
    fi
    
    # Build output. Payloads stay in files so a long release name cannot
    # exceed the 128 KiB per-argument limit.
    jq_tmp_init
    local ts releases_file hn_file skills_file hf_file output
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    releases_file=$(mktemp "$JQ_TMP_DIR/out-rel.XXXXXX")
    hn_file=$(mktemp "$JQ_TMP_DIR/out-hn.XXXXXX")
    skills_file=$(mktemp "$JQ_TMP_DIR/out-skills.XXXXXX")
    hf_file=$(mktemp "$JQ_TMP_DIR/out-hf.XXXXXX")
    write_json_var new_releases "$releases_file"
    write_json_var new_hn "$hn_file"
    write_json_var new_skills "$skills_file"
    write_json_var new_hf "$hf_file"
    output=$(jq -n \
        --arg ts "$ts" \
        --arg mode "check" \
        --slurpfile releases "$releases_file" \
        --slurpfile hn "$hn_file" \
        --slurpfile skills "$skills_file" \
        --slurpfile hf "$hf_file" \
        '{
            timestamp: $ts,
            mode: $mode,
            newReleases: $releases[0],
            newHNStories: $hn[0],
            newSkills: $skills[0],
            newHFPapers: $hf[0],
            summary: {
                releaseCount: ($releases[0] | length),
                hnCount: ($hn[0] | length),
                skillCount: ($skills[0] | length),
                hfCount: ($hf[0] | length),
                hasNews: ((($releases[0] | length) + ($hn[0] | length) + ($skills[0] | length) + ($hf[0] | length)) > 0)
            }
        }'
    )
    
    echo "$output" > "$OUTPUT_FILE"
    
    local new_state
    new_state=$(update_state "$state" "$baselines" "$new_hn" "$new_skills")
    save_json "$STATE_FILE" "$new_state"
    
    echo "" >&2
    echo "✅ Check complete. Output: $OUTPUT_FILE" >&2
    
    echo "$output"
}

run_discover() {
    echo "🦀 Claw Ecosystem Monitor - DISCOVER MODE" >&2
    echo "==========================================" >&2
    echo "📅 $(date)" >&2
    echo "" >&2

    unexport all_discoveries github_discoveries awesome_discoveries hn_discoveries state discovery output
    local all_discoveries="[]"

    load_tokens

    jq_tmp_init
    local all_file part_file
    all_file=$(mktemp "$JQ_TMP_DIR/all-disc.XXXXXX")
    part_file=$(mktemp "$JQ_TMP_DIR/part-disc.XXXXXX")

    # Run all discovery sources. Merge from files so neither side is an
    # argv element or an exported environment string.
    local github_discoveries
    github_discoveries=$(discover_github)
    write_json_var all_discoveries "$all_file"
    printf '%s\n' "$github_discoveries" > "$part_file"
    all_discoveries=$(jq -s 'add | unique_by(.repo)' "$all_file" "$part_file")

    local awesome_discoveries
    awesome_discoveries=$(discover_awesome_lists)
    write_json_var all_discoveries "$all_file"
    printf '%s\n' "$awesome_discoveries" > "$part_file"
    all_discoveries=$(jq -s 'add | unique_by(.repo)' "$all_file" "$part_file")

    local hn_discoveries
    hn_discoveries=$(discover_hackernews)
    write_json_var all_discoveries "$all_file"
    printf '%s\n' "$hn_discoveries" > "$part_file"
    all_discoveries=$(jq -s 'add | unique_by(.repo)' "$all_file" "$part_file")

    # Brave Search discovery removed 2026-06-25 (Brave deprecated).

    # Add discoveries to sources.json
    local count=0
    while IFS= read -r discovery; do
        [[ -z "$discovery" || "$discovery" == "null" ]] && continue
        add_repo_to_sources "$discovery" "dynamic"
        count=$((count + 1))
    done < <(echo "$all_discoveries" | jq -c '.[]')
    
    # Update state
    local state
    state=$(load_json "$STATE_FILE")
    state=$(echo "$state" | jq --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" '.lastDiscovery = $ts')
    save_json "$STATE_FILE" "$state"
    
    # Build output. The accumulated array is a file, not --argjson.
    local ts disc_file output
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    disc_file=$(mktemp "$JQ_TMP_DIR/disc-out.XXXXXX")
    write_json_var all_discoveries "$disc_file"
    output=$(jq -n \
        --arg ts "$ts" \
        --arg mode "discover" \
        --slurpfile discoveries "$disc_file" \
        '{
            timestamp: $ts,
            mode: $mode,
            newDiscoveries: $discoveries[0],
            summary: {
                discoveryCount: ($discoveries[0] | length),
                hasDiscoveries: (($discoveries[0] | length) > 0)
            }
        }'
    )
    
    echo "$output" > "$DISCOVERIES_FILE"
    
    echo "" >&2
    echo "=======================================" >&2
    echo "🎉 Discovery complete!" >&2
    echo "   Found $count new project(s)" >&2
    echo "   Output: $DISCOVERIES_FILE" >&2
    echo "   Updated: $SOURCES_FILE" >&2
    
    echo "$output"
}

# Main with global timeout
cleanup_workers() {
    # Kill any background jobs on exit
    jobs -p | xargs -r kill 2>/dev/null || true
    if [[ -n "${JQ_TMP_DIR:-}" && -d "$JQ_TMP_DIR" ]]; then
        rm -rf "$JQ_TMP_DIR"
        JQ_TMP_DIR=""
    fi
}

trap cleanup_workers EXIT

main() {
    init_files
    jq_tmp_init

    # Set a hard timeout for the entire script.
    # An older note said the cron job times out at 60s. The enforced budget
    # in this script is SCRIPT_MAX_DURATION (300s; the cron comment says 600s).
    # GitHub search's one 403/429 backoff uses this same clock and will not
    # sleep past it.
    local script_start
    script_start=$(date +%s)
    local SCRIPT_MAX_DURATION=300  # 5 min budget; cron timeout is 600s
    
    case "$MODE" in
        check)
            run_check
            ;;
        discover)
            run_discover
            ;;
        both)
            run_check
            echo "" >&2
            run_discover
            ;;
        *)
            echo "Unknown mode: $MODE" >&2
            echo "Usage: $0 --mode [check|discover|both]" >&2
            exit 1
            ;;
    esac
    
    local script_end
    script_end=$(date +%s)
    echo "⏱️ Total time: $((script_end - script_start))s" >&2
}

main "$@"

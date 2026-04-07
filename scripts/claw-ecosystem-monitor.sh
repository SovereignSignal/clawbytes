#!/bin/bash
# claw-ecosystem-monitor.sh - Monitor the Claw ecosystem for news and releases
# Now with dynamic discovery layer!
#
# Usage:
#   ./claw-ecosystem-monitor.sh --mode check     # Check known sources (default)
#   ./claw-ecosystem-monitor.sh --mode discover  # Hunt for new projects
#   ./claw-ecosystem-monitor.sh --mode both      # Check + discover

set +e  # Don't exit on error
set -o pipefail

SCRIPT_DIR="${BASH_SOURCE[0]:-$(pwd)}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_DIR")" && pwd)"
WORKSPACE_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="${WORKSPACE_DIR}/memory/claw-ecosystem-state.json"
SOURCES_FILE="${WORKSPACE_DIR}/memory/claw-ecosystem-sources.json"
OUTPUT_FILE="${WORKSPACE_DIR}/memory/claw-ecosystem-new-items.json"
DISCOVERIES_FILE="${WORKSPACE_DIR}/memory/claw-ecosystem-discoveries.json"
CREDS_FILE="${WORKSPACE_DIR}/CREDS.md"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
BRAVE_API_KEY="${BRAVE_API_KEY:-}"

# Rate limiting - be nice to APIs
GITHUB_DELAY=2  # seconds between GitHub requests
HN_DELAY=1
BRAVE_DELAY=1

# Star thresholds for discovery
MIN_STARS_NEW=50      # Min stars for repos <7 days old
MIN_STARS_DEFAULT=100 # Min stars for older repos

MODE="check"

get_cred() {
    local section="$1"
    local key="$2"
    if [[ ! -f "$CREDS_FILE" ]]; then
        return
    fi
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
    [[ -z "$GITHUB_TOKEN" ]] && GITHUB_TOKEN="$(get_cred "GitHub API" "Token")"
    [[ -z "$BRAVE_API_KEY" ]] && BRAVE_API_KEY="$(get_cred "Brave Search API" "API Key")"
}

github_api() {
    local url="$1"
    if [[ -n "$GITHUB_TOKEN" ]]; then
        curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" "$url"
    else
        curl -sf "$url"
    fi
}

brave_search_json() {
    local query="$1"
    local count="${2:-10}"
    if [[ -z "$BRAVE_API_KEY" ]]; then
        echo '{"web":{"results":[]}}'
        return
    fi

    curl -sf --get "https://api.search.brave.com/res/v1/web/search" \
        --data-urlencode "q=${query}" \
        --data-urlencode "count=${count}" \
        --data-urlencode "freshness=pw" \
        -H "Accept: application/json" \
        -H "X-Subscription-Token: ${BRAVE_API_KEY}" || echo '{"web":{"results":[]}}'
}

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
    if [[ -f "$file" ]]; then
        cat "$file" 2>/dev/null || echo "{}"
    else
        echo "{}"
    fi
}

# Save JSON to file
save_json() {
    local file="$1"
    local data="$2"
    echo "$data" | jq '.' > "$file"
}

# Get all repos from sources (curated + dynamic)
get_all_repos() {
    if [[ ! -f "$SOURCES_FILE" ]]; then
        echo ""
        return
    fi
    jq -r '(.curated + .dynamic) | .[].repo // empty' "$SOURCES_FILE" 2>/dev/null | sort -u || echo ""
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
    local repo_data="$1"
    local section="$2"  # "curated" or "dynamic"
    
    local sources
    sources=$(load_json "$SOURCES_FILE")
    
    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    
    # Add discoveredAt timestamp
    repo_data=$(echo "$repo_data" | jq --arg ts "$now" '. + {discoveredAt: $ts}')
    
    # Add to appropriate section
    sources=$(echo "$sources" | jq --argjson repo "$repo_data" --arg section "$section" '
        .[$section] = (.[$section] + [$repo]) |
        ._meta.lastUpdated = now |
        ._meta.totalDiscovered = ((.curated | length) + (.dynamic | length))
    ')
    
    save_json "$SOURCES_FILE" "$sources"
}

# ============ DISCOVERY FUNCTIONS ============

# GitHub Search Discovery
discover_github() {
    echo "🔍 GitHub Discovery..." >&2
    local discoveries="[]"
    
    # Search queries
    local queries=(
        "topic:ai-agent+topic:openclaw&sort=updated&per_page=10"
        "openclaw+alternative+agent&sort=stars&per_page=10"
        "personal+ai+agent+self-hosted&sort=updated&per_page=10"
        "claw+agent+in:name&sort=stars&per_page=20"
        "ai+coding+agent+framework&sort=stars&per_page=10"
        "mcp+agent+framework&sort=updated&per_page=10"
    )
    
    for query in "${queries[@]}"; do
        local url="https://api.github.com/search/repositories?q=${query}"
        local response
        response=$(github_api "$url" 2>/dev/null) || continue
        
        # Parse results
        local items
        items=$(echo "$response" | jq -c '.items // []')
        
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
                local repo_data
                repo_data=$(echo "$item" | jq '{
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
                }')
                
                discoveries=$(echo "$discoveries" | jq --argjson repo "$repo_data" '. + [$repo]')
                echo "   📦 Found: $repo_name (⭐ $stars)" >&2
            fi
        done < <(echo "$items" | jq -c '.[]')
        
        sleep $GITHUB_DELAY
    done
    
    echo "$discoveries"
}

# Awesome List Crawler
discover_awesome_lists() {
    echo "📚 Crawling Awesome Lists..." >&2
    local discoveries="[]"
    
    local lists=(
        "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md"
        "https://raw.githubusercontent.com/kyrolabs/awesome-agents/main/README.md"
    )
    
    for list_url in "${lists[@]}"; do
        local content
        content=$(curl -sf "$list_url" 2>/dev/null) || continue
        
        # Extract GitHub URLs
        local urls
        urls=$(echo "$content" | grep -oE 'https://github\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+' | sort -u)
        
        while IFS= read -r url; do
            [[ -z "$url" ]] && continue
            
            # Extract repo name
            local repo_name
            repo_name=$(echo "$url" | sed 's|https://github.com/||')
            
            # Skip if already known
            if is_repo_known "$repo_name"; then
                continue
            fi
            
            # Fetch metadata
            local repo_data
            repo_data=$(fetch_repo_metadata "$repo_name")
            
            if [[ -n "$repo_data" && "$repo_data" != "{}" ]]; then
                local stars
                stars=$(echo "$repo_data" | jq -r '.stars // 0')
                
                if [[ $stars -ge $MIN_STARS_DEFAULT ]]; then
                    repo_data=$(echo "$repo_data" | jq '. + {source: "awesome-list", isNew: true}')
                    discoveries=$(echo "$discoveries" | jq --argjson repo "$repo_data" '. + [$repo]')
                    echo "   📚 Found: $repo_name (⭐ $stars)" >&2
                fi
            fi
            
            sleep $GITHUB_DELAY
        done <<< "$urls"
    done
    
    echo "$discoveries"
}

# Hacker News Discovery
discover_hackernews() {
    echo "🔶 HN Discovery..." >&2
    local discoveries="[]"
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
        response=$(curl -sf "$url" 2>/dev/null) || continue
        
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
                repo_data=$(echo "$repo_data" | jq '. + {source: "hackernews", isNew: true}')
                discoveries=$(echo "$discoveries" | jq --argjson repo "$repo_data" '. + [$repo]')
                echo "   🔶 Found: $repo_name (⭐ $stars)" >&2
            fi
        fi
        
        sleep $GITHUB_DELAY
    done <<< "$unique_repos"
    
    echo "$discoveries"
}

# Brave Search Discovery
discover_brave() {
    local api_key="${BRAVE_API_KEY:-}"
    
    if [[ -z "$api_key" ]]; then
        echo "⚠️  BRAVE_API_KEY not set, skipping Brave search" >&2
        echo "[]"
        return
    fi
    
    echo "🔎 Brave Search Discovery..." >&2
    local discoveries="[]"
    
    local queries=(
        "new AI agent framework 2026"
        "openclaw alternative site:github.com"
        "self-hosted personal AI agent"
        "local AI coding assistant github"
    )
    
    for query in "${queries[@]}"; do
        local encoded_query
        encoded_query=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$query'))")
        
        local url="https://api.search.brave.com/res/v1/web/search?q=${encoded_query}&count=5"
        local response
        response=$(curl -sf -H "Accept: application/json" -H "X-Subscription-Token: ${api_key}" "$url" 2>/dev/null) || continue
        
        # Extract GitHub URLs from results
        local results
        results=$(echo "$response" | jq '.web.results // []')
        
        while IFS= read -r result; do
            local url
            url=$(echo "$result" | jq -r '.url // ""')
            
            if [[ "$url" =~ github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+) ]]; then
                local repo_name="${BASH_REMATCH[1]}"
                
                if ! is_repo_known "$repo_name"; then
                    local repo_data
                    repo_data=$(fetch_repo_metadata "$repo_name")
                    
                    if [[ -n "$repo_data" && "$repo_data" != "{}" ]]; then
                        local stars
                        stars=$(echo "$repo_data" | jq -r '.stars // 0')
                        
                        if [[ $stars -ge $MIN_STARS_NEW ]]; then
                            repo_data=$(echo "$repo_data" | jq '. + {source: "brave-search", isNew: true}')
                            discoveries=$(echo "$discoveries" | jq --argjson repo "$repo_data" '. + [$repo]')
                            echo "   🔎 Found: $repo_name (⭐ $stars)" >&2
                        fi
                    fi
                    
                    sleep $GITHUB_DELAY
                fi
            fi
        done < <(echo "$results" | jq -c '.[]')
        
        sleep $BRAVE_DELAY
    done
    
    echo "$discoveries"
}

# ============ CHECK FUNCTIONS ============

# Fetch GitHub releases for a repo
fetch_github_releases() {
    local repo="$1"
    local url="https://api.github.com/repos/${repo}/releases?per_page=5"
    
    github_api "$url" 2>/dev/null || echo "[]"
}

check_clawhub_skills() {
    local state="$1"
    local seen_urls
    seen_urls=$(echo "$state" | jq '.lastSeenSkills // []' 2>/dev/null || echo '[]')

    local response
    response=$(brave_search_json 'site:clawhub.ai (skill OR skills OR marketplace) openclaw' 10 2>/dev/null || echo '{"web":{"results":[]}}')

    echo "$response" | jq --argjson seen "$seen_urls" '
        [.web.results[]?
         | select(.url | contains("clawhub.ai"))
         | {id: .url, name: (.title // ""), url: .url, description: (.description // "")}
         | select(($seen | index(.id)) == null)
        ]
        | unique_by(.id)
        | map(. + {source: "clawhub-search"})
    ' 2>/dev/null || echo '[]'
}

# Check all known repos for new releases
check_github_releases() {
    local state="$1"
    local new_releases="[]"
    
    local repos
    repos=$(get_all_repos)
    
    # If no repos, return empty
    if [[ -z "$repos" ]]; then
        echo "[]"
        return
    fi
    
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        
        local releases
        releases=$(fetch_github_releases "$repo")
        
        if [[ "$releases" != "[]" && "$releases" != "" ]]; then
            local latest_tag
            latest_tag=$(echo "$releases" | jq -r '.[0].tag_name // empty' 2>/dev/null || echo "")
            
            if [[ -n "$latest_tag" ]]; then
                local seen_tag
                seen_tag=$(echo "$state" | jq -r --arg repo "$repo" '.lastSeenReleases[$repo] // empty' 2>/dev/null || echo "")
                
                if [[ "$latest_tag" != "$seen_tag" ]]; then
                    local release_info
                    release_info=$(echo "$releases" | jq --arg repo "$repo" '.[0] | {
                        repo: $repo,
                        tag: .tag_name,
                        name: .name,
                        url: .html_url,
                        published: .published_at,
                        body: (.body | if . then .[0:500] else "" end)
                    }')
                    new_releases=$(echo "$new_releases" | jq --argjson item "$release_info" '. + [$item]')
                fi
            fi
        fi
        
        sleep $GITHUB_DELAY
    done <<< "$repos"
    
    echo "$new_releases"
}

# Check Hacker News for relevant stories
check_hackernews() {
    local state="$1"
    local new_stories="[]"
    
    local queries=("openclaw" "claw+agent" "hermes+agent" "claude+code" "ai+coding+agent")
    
    for query in "${queries[@]}"; do
        local url="https://hn.algolia.com/api/v1/search?query=${query}&tags=story&hitsPerPage=5"
        local response
        response=$(curl -sf "$url" 2>/dev/null || echo '{"hits":[]}')
        
        local hits
        hits=$(echo "$response" | jq '.hits // []')
        
        local seen_ids
        seen_ids=$(echo "$state" | jq '.lastSeenHNStories // []' 2>/dev/null || echo '[]')
        
        local stories
        stories=$(echo "$hits" | jq --argjson seen "$seen_ids" '
            [.[] | select(.objectID as $id | ($seen | index($id)) == null)] |
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
        
        new_stories=$(echo "$new_stories $stories" | jq -s 'add | unique_by(.id)' 2>/dev/null || echo '[]')
        
        sleep $HN_DELAY
    done
    
    echo "$new_stories"
}

# Update state with new findings
update_state() {
    local state="$1"
    local new_releases="$2"
    local new_hn="$3"
    local new_skills="$4"
    
    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    
    # Update lastSeenReleases
    local updated_releases
    updated_releases=$(echo "$new_releases" | jq 'reduce .[] as $r ({}; .[$r.repo] = $r.tag)')
    
    state=$(echo "$state" | jq --argjson new "$updated_releases" '
        .lastSeenReleases = (.lastSeenReleases + $new)
    ')
    
    # Update HN story IDs
    local new_hn_ids
    new_hn_ids=$(echo "$new_hn" | jq '[.[].id]')
    state=$(echo "$state" | jq --argjson ids "$new_hn_ids" '
        .lastSeenHNStories = ((.lastSeenHNStories + $ids) | unique | .[-100:])
    ')

    local new_skill_ids
    new_skill_ids=$(echo "$new_skills" | jq '[.[].id]')
    state=$(echo "$state" | jq --argjson ids "$new_skill_ids" '
        .lastSeenSkills = ((.lastSeenSkills + $ids) | unique | .[-100:])
    ')
    
    state=$(echo "$state" | jq --arg ts "$now" '.lastCheck = $ts')
    
    echo "$state"
}

# ============ MAIN FUNCTIONS ============

run_check() {
    echo "🦀 Claw Ecosystem Monitor - CHECK MODE" >&2
    echo "=======================================" >&2
    echo "📅 $(date)" >&2
    echo "" >&2
    
    # Initialize files first, then load state
    echo "DEBUG: About to init_files" >&2
    init_files
    echo "DEBUG: init_files complete" >&2
    
    local state
    state=$(load_json "$STATE_FILE")
    echo "DEBUG: state loaded" >&2

    load_tokens
    echo "DEBUG: tokens loaded" >&2
    
    echo "🔑 Tokens loaded: GitHub=${GITHUB_TOKEN:+yes}, Brave=${BRAVE_API_KEY:+yes}" >&2
    
    local repo_count
    repo_count=$(get_all_repos | wc -l || echo 0)
    echo "📊 Monitoring $repo_count repositories" >&2
    echo "" >&2
    
    echo "📦 Checking GitHub releases..." >&2
    local new_releases
    new_releases=$(check_github_releases "$state")
    local release_count
    release_count=$(echo "$new_releases" | jq 'length')
    echo "   Found $release_count new release(s)" >&2
    
    echo "🔶 Checking Hacker News..." >&2
    local new_hn
    new_hn=$(check_hackernews "$state")
    local hn_count
    hn_count=$(echo "$new_hn" | jq 'length')
    echo "   Found $hn_count new story/stories" >&2

    echo "🛍️ Checking ClawHub..." >&2
    local new_skills
    new_skills=$(check_clawhub_skills "$state")
    local skill_count
    skill_count=$(echo "$new_skills" | jq 'length')
    echo "   Found $skill_count new skill item(s)" >&2
    
    # Build output
    local output
    output=$(jq -n \
        --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
        --arg mode "check" \
        --argjson releases "$new_releases" \
        --argjson hn "$new_hn" \
        --argjson skills "$new_skills" \
        '{
            timestamp: $ts,
            mode: $mode,
            newReleases: $releases,
            newHNStories: $hn,
            newSkills: $skills,
            summary: {
                releaseCount: ($releases | length),
                hnCount: ($hn | length),
                skillCount: ($skills | length),
                hasNews: ((($releases | length) + ($hn | length) + ($skills | length)) > 0)
            }
        }'
    )
    
    echo "$output" > "$OUTPUT_FILE"
    
    local new_state
    new_state=$(update_state "$state" "$new_releases" "$new_hn" "$new_skills")
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
    
    local all_discoveries="[]"

    load_tokens
    
    # Run all discovery sources
    local github_discoveries
    github_discoveries=$(discover_github)
    all_discoveries=$(echo "$all_discoveries $github_discoveries" | jq -s 'add | unique_by(.repo)')
    
    local awesome_discoveries
    awesome_discoveries=$(discover_awesome_lists)
    all_discoveries=$(echo "$all_discoveries $awesome_discoveries" | jq -s 'add | unique_by(.repo)')
    
    local hn_discoveries
    hn_discoveries=$(discover_hackernews)
    all_discoveries=$(echo "$all_discoveries $hn_discoveries" | jq -s 'add | unique_by(.repo)')
    
    local brave_discoveries
    brave_discoveries=$(discover_brave)
    all_discoveries=$(echo "$all_discoveries $brave_discoveries" | jq -s 'add | unique_by(.repo)')
    
    # Add discoveries to sources.json
    local count=0
    while IFS= read -r discovery; do
        [[ -z "$discovery" || "$discovery" == "null" ]] && continue
        add_repo_to_sources "$discovery" "dynamic"
        ((count++))
    done < <(echo "$all_discoveries" | jq -c '.[]')
    
    # Update state
    local state
    state=$(load_json "$STATE_FILE")
    state=$(echo "$state" | jq --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" '.lastDiscovery = $ts')
    save_json "$STATE_FILE" "$state"
    
    # Build output
    local output
    output=$(jq -n \
        --arg ts "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
        --arg mode "discover" \
        --argjson discoveries "$all_discoveries" \
        '{
            timestamp: $ts,
            mode: $mode,
            newDiscoveries: $discoveries,
            summary: {
                discoveryCount: ($discoveries | length),
                hasDiscoveries: (($discoveries | length) > 0)
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

# Main
main() {
    case "$MODE" in
        check)
            run_check
            ;;
        discover)
            init_files
            run_discover
            ;;
        both)
            run_check
            echo "" >&2
            init_files
            run_discover
            ;;
        *)
            echo "Unknown mode: $MODE" >&2
            echo "Usage: $0 --mode [check|discover|both]" >&2
            exit 1
            ;;
    esac
}

main "$@"

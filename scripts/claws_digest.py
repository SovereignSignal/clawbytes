"""
claws_digest.py — Daily Claws ecosystem digest
Reads: Notion Claws page changes, ClawBytes signal
Writes: Clean HTML email, sends via Proton Mail

Editorial model: What moved, why it matters. Not a database dump.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Config ────────────────────────────────────────────────────────────────────
NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
PROTON_USER = "clawback1@proton.me"
PROTON_PASS = os.environ.get("PROTON_PASS", "")
RECIPIENT = "sov@sovereignsignal.com"
WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = WORKSPACE / "memory"
BACKLOG_FILE = MEMORY_DIR / "clawbytes-backlog.json"
NOTION_SYNC_FILE = MEMORY_DIR / "notion-claws-sync.md"
# ──────────────────────────────────────────────────────────────────────────────


def llm_call(prompt, max_tokens=120, temperature=0.7):
    """Call LLM via xAI (primary) or Claude CLI (fallback). Returns text or empty string."""
    # Primary: xAI API
    try:
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if api_key:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = Request(
                f"{os.environ.get('CLAWBYTES_LLM_URL', 'https://api.tenspire.ai/v1')}/chat/completions",
                data=json.dumps({
                    "model": os.environ.get('CLAWBYTES_LLM_MODEL', 'gemma4:31b-cloud'),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urlopen(req, timeout=25, context=ctx) as resp:
                result = json.loads(resp.read().decode())
            text = result['choices'][0]['message']['content'].strip().strip('"')
            if text:
                return text
    except Exception as e:
        print(f"  llm_call xAI failed: {e}", file=sys.stderr)

    # Fallback: Claude CLI
    try:
        import subprocess
        result = subprocess.run(
            ['claude', '--dangerously-skip-permissions', '-p', prompt],
            capture_output=True, text=True, timeout=30
        )
        text = result.stdout.strip().strip('"')
        if result.returncode == 0 and text:
            return text
    except Exception as e:
        print(f"  llm_call Claude CLI failed: {e}", file=sys.stderr)

    return ''


def truncate_text(text, max_len=160, min_cutoff=20):
    """Truncate text at sentence boundary, then comma, then word boundary."""
    if len(text) <= max_len:
        return text
    tr = text[:max_len]
    # Try sentence boundary first
    last_period = max(tr.rfind('.'), tr.rfind('!'), tr.rfind('?'))
    if last_period > min_cutoff:
        return text[:last_period + 1]
    # Try comma
    last_comma = tr.rfind(',')
    if last_comma > min_cutoff + 20:
        return text[:last_comma] + '…'
    # Try word boundary
    last_space = tr.rfind(' ')
    if last_space > min_cutoff + 20:
        return text[:last_space] + '…'
    # Fallback
    return tr + '…'


def strip_engagement_from_summary(summary):
    """Strip embedded engagement metrics like (14↑ / 27💬) from summaries."""
    import re as _re
    cleaned = _re.sub(r'\s*\(\d+[↑⬆]\s*/\s*\d+[💬]\.?\s*\)', '', summary)
    return cleaned.strip()


def generate_hook(name, details, cache_path=None):
    """Generate a punchy editorial hook for a Notion entry via Claude Code CLI."""
    if cache_path is None:
        cache_path = MEMORY_DIR / '.claws-hook-cache.json'
    
    # Load cache
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    
    # Check cache
    cache_key = f"{name}:{details.get('why it matters', [''])[0][:80]}:{details.get('best_for', [''])[0][:80]}"
    if cache_key in cache:
        return cache[cache_key]
    
    # Build context for LLM
    why = details.get('why it matters', [])
    best = details.get('best_for', [])
    what = details.get('what it is', [])
    context_parts = []
    if why:
        context_parts.append(f"Why it matters: {why[0]}")
    if best:
        context_parts.append(f"Best for: {best[0]}")
    if what:
        context_parts.append(f"What it is: {what[0]}")
    
    if not context_parts:
        # Fallback to raw text
        raw = why[0] if why else (best[0] if best else (details.get('opening', '')))
        hook = truncate_text(raw, max_len=140) if raw else ''
        cache[cache_key] = hook
        cache_path.write_text(json.dumps(cache, indent=2))
        return hook
    
    prompt = f"""Write a punchy one-line hook (max 20 words) for a newsletter about {name}.

Context:
{chr(10).join(context_parts)}

Rules:
- Be opinionated and specific, not generic
- No corporate filler: no 'explores', 'reveals', 'highlights', 'offering insights', 'worth watching', 'empowers'
- Say what it DOES and why it DIFFERS, not what it IS
- Max 20 words, one sentence, end with a period
- Output ONLY the hook sentence, nothing else"""
    
    try:
        hook = llm_call(prompt, max_tokens=80, temperature=0.7).rstrip('.')
        if not hook:
            raise ValueError("LLM returned empty")
        if len(hook) > 200:
            hook = truncate_text(hook, max_len=180)
    except Exception as e:
        print(f"Hook generation failed for {name}: {e}", file=sys.stderr)
        raw = why[0] if why else (best[0] if best else (details.get('opening', '')))
        hook = truncate_text(raw, max_len=140) if raw else ''
    
    cache[cache_key] = hook
    try:
        cache_path.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass
    
    return hook


def notion_get(path, retries=2):
    url = f"https://api.notion.com/v1/{path}"
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "Authorization": f"Bearer {NOTION_KEY}",
                "Notion-Version": "2022-06-28",
            })
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except URLError as e:
            if attempt < retries - 1:
                import time
                time.sleep(2)
                continue
            print(f"Notion API error after {retries} attempts: {e}", file=sys.stderr)
            return {}
    return {}


def get_block_text(block):
    btype = block.get('type', '')
    if btype in ('paragraph', 'heading_1', 'heading_2', 'heading_3', 'callout',
                 'bulleted_list_item', 'numbered_list_item', 'quote'):
        rich_text = block.get(btype, {}).get('rich_text', [])
        return ''.join(t.get('plain_text', '') for t in rich_text)
    return ''


def get_page_details(pid):
    data = notion_get(f"blocks/{pid}/children?page_size=100")
    if not data:
        return {}

    sections = {}
    current_section = None
    for block in data.get('results', []):
        text = get_block_text(block)
        btype = block.get('type', '')

        if btype == 'heading_2':
            section_key = text.lower().strip()
            known = ('what it is', 'recent updates', 'why it matters',
                      'strengths', 'weaknesses / gotchas', 'signals / momentum',
                      'pricing', 'gotchas', 'open questions')
            if section_key in known:
                current_section = section_key
                sections.setdefault(current_section, [])
            elif 'best for' in section_key:
                current_section = 'best_for'
                sections.setdefault(current_section, [])
            else:
                current_section = None
            continue

        if current_section and text:
            sections.setdefault(current_section, []).append(text)

    opening = []
    for block in data.get('results', []):
        btype = block.get('type', '')
        if btype == 'heading_2':
            break
        text = get_block_text(block)
        if text and btype == 'paragraph':
            opening.append(text)

    result = {}
    if opening:
        result['opening'] = ' '.join(opening)
    for key in ('what it is', 'recent updates', 'why it matters', 'best_for',
                'strengths', 'weaknesses / gotchas', 'signals / momentum',
                'gotchas', 'pricing'):
        if key in sections:
            result[key] = sections[key]
    return result


def get_notion_changes(hours=48):
    with open(NOTION_SYNC_FILE) as f:
        content = f.read()

    page_entries = re.findall(r'- (.+?) \(([0-9a-f-]+)\)', content)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    changes = []
    for name, pid in page_entries:
        page_data = notion_get(f"pages/{pid}")
        if not page_data:
            continue
        edited = page_data.get('last_edited_time', '')
        created = page_data.get('created_time', '')
        is_new = created > cutoff
        is_edited = edited > cutoff and not is_new

        if not (is_new or is_edited):
            continue

        category = "Infrastructure"
        if any(x in name.lower() for x in ['nanobot', 'zeroclaw', 'picoclaw', 'nanoclaw', 'microclaw', 'smolagent']):
            category = "Tiny Claw"
        elif any(x in name.lower() for x in ['openfang', 'hermes', 'moltis', 'ironclaw', 'agent zero', 'openclaw', 'openhands', 'kortix']):
            category = "Self-Hosted"
        elif any(x in name.lower() for x in ['openai frontier', 'nemoclaw', 'google adk', 'microsoft agent', 'amazon bedrock', 'salesforce', 'mastra', 'claude agent', 'meta llama']):
            category = "Big Tech"
        elif any(x in name.lower() for x in ['crewai', 'warp', 'taskade', 'adopt', 'openserv', 'airtable', 'superhuman', 'perplexity', 'dust', 'devin', 'windsurf', 'replit', 'cursor', 'agno', 'bolt']):
            category = "Startup"

        details = get_page_details(pid)

        changes.append({
            'name': name,
            'id': pid,
            'is_new': is_new,
            'is_edited': is_edited,
            'edited': edited[:10],
            'category': category,
            'details': details,
        })

    changes.sort(key=lambda x: x['edited'], reverse=True)
    return changes


def get_clawbytes_posts(hours=48):
    """Get recent ClawBytes items — posted AND queued, not just what hit Telegram."""
    if not BACKLOG_FILE.exists():
        return []

    backlog = json.loads(BACKLOG_FILE.read_text())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    recent = [
        i for i in backlog.get('items', [])
        if i.get('status') in ('posted', 'queued') and i.get('discoveredAt', '') > cutoff
    ]
    recent.sort(key=lambda x: -(x.get('score') or 0))
    return recent


def dedupe_releases(items):
    """Collapse rapid-fire releases from the same project into one entry.
    e.g. 4 Moltis releases in 48h → 'Moltis (4 releases)' with the latest URL."""
    seen = {}
    deduped = []
    for item in items:
        title = item.get('title', '')
        # Match patterns like 'ProjectName 20260413.04' or 'ProjectName v1.2.3' or 'ProjectName 0.121.0-alpha.4'
        match = re.match(r'^(.+?)\s+(?:v?\d[\d.\-a-zA-Z]+|20\d{6}\.\d+)$', title)
        if match:
            project = match.group(1).strip()
            if project in seen:
                seen[project]['count'] += 1
                # Keep the latest (highest score or newest)
                if (item.get('score') or 0) > (seen[project]['item'].get('score') or 0):
                    seen[project]['item'] = item
                continue
            seen[project] = {'item': item, 'count': 1}
            deduped.append(project)
        else:
            deduped.append(item)

    result = []
    for entry in deduped:
        if isinstance(entry, str):
            info = seen[entry]
            item = dict(info['item'])
            if info['count'] > 1:
                item['title'] = f"{entry} ({info['count']} releases)"
            result.append(item)
        else:
            result.append(entry)
    return result


def generate_lead_hook(item, cache_path=None):
    """Generate an editorial hook for the top ClawBytes story."""
    if cache_path is None:
        cache_path = MEMORY_DIR / '.claws-hook-cache.json'

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    cache_key = f"lead:{item.get('url', '')}:{item.get('title', '')[:60]}"
    if cache_key in cache:
        return cache[cache_key]

    title = item.get('title', '')
    summary = item.get('summary', '')
    source = item.get('sourceType', '')
    cat = item.get('primaryCategory', '')

    prompt = f"""Write a sharp 1-2 sentence editorial take (max 40 words) for a newsletter lead story.

Title: {title}
Summary: {summary}
Source: {source}
Category: {cat}

Rules:
- Be opinionated and direct — say what matters and why
- No corporate filler: no 'explores', 'reveals', 'highlights', 'offering insights'
- If it's a community discussion, capture the sentiment
- If it's a release, say what changed and why it matters
- Output ONLY the take, nothing else"""

    try:
        hook = llm_call(prompt, max_tokens=200, temperature=0.7)
        if not hook:
            raise ValueError("LLM returned empty")
        if len(hook) > 300:
            hook = truncate_text(hook, max_len=250)
    except Exception as e:
        print(f"Lead hook generation failed: {e}", file=sys.stderr)
        hook = strip_engagement_from_summary(summary) if summary else ''

    cache[cache_key] = hook
    try:
        cache_path.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass

    return hook


def render_signal_item(item):
    """Render a single ClawBytes item as an HTML list item."""
    title = item.get('title', '?')
    url = item.get('url', '#')
    src = item.get('sourceType', '')
    summary = (item.get('summary') or '').strip()

    html = f'<li><a href="{url}">{title}</a>'

    if src == 'reddit':
        upvotes = item.get('rawScore', 0)
        comments = item.get('rawComments', 0)
        html += f' <span style="color:#999;font-size:12px;">({upvotes}\u2191 / {comments}\U0001f4ac)</span>'
    elif src == 'hackernews':
        upvotes = item.get('rawScore', 0)
        html += f' <span style="color:#999;font-size:12px;">({upvotes}pts)</span>'

    generic_summaries = ('New release', 'New Moltis release', 'New Hermes release',
                         'Worth reading', 'New OpenClaw release')
    if summary and summary not in generic_summaries:
        s = summary
        for prefix in ('user sentiment and ', 'cost pressure and ', 'operator pain points', 'pricing pain'):
            s = s.replace(prefix, '')
        s = strip_engagement_from_summary(s)
        if s and len(s) > 5:
            display = truncate_text(s, max_len=120, min_cutoff=10)
            html += f'<br><span style="color:#888;font-size:12px;">{display}</span>'

    html += '</li>\n'
    return html


def generate_opening_paragraph(lead_item, shipped, radar, community):
    """Generate a conversational 2-3 sentence opening that connects the dots across today's stories."""
    # Build a summary of today's stories for the LLM
    stories = []
    if lead_item:
        stories.append(f"Lead: {lead_item.get('title', '')} — {lead_item.get('summary', '')}")
    for item in shipped[:3]:
        stories.append(f"Shipped: {item.get('title', '')}")
    for item in radar[:3]:
        stories.append(f"Radar: {item.get('title', '')} — {item.get('summary', '')}")
    for item in community[:2]:
        stories.append(f"Community: {item.get('title', '')} — {item.get('summary', '')}")

    if not stories:
        return ''

    prompt = f"""Write a conversational 2-3 sentence opening paragraph for today's Claws Daily newsletter.
Connect the dots across these stories — find the thread, the tension, or the theme:

{chr(10).join(stories)}

Rules:
- 2-3 sentences max, ~40-60 words
- Conversational and sharp, like a smart friend's morning briefing
- Find a connecting thread or contrast between the stories
- No generic openings like "Today in AI", "It's been a busy day", "Hey there", or "Hey crew"
- No filler: no 'landscape', 'ecosystem', 'space', 'offering insights'
- Start with the signal, not a greeting
- End on a forward-looking note or a question if natural
- Output ONLY the paragraph, nothing else"""

    try:
        # Try xAI first
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if api_key:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = Request(
                f"{os.environ.get('CLAWBYTES_LLM_URL', 'https://api.tenspire.ai/v1')}/chat/completions",
                data=json.dumps({
                    "model": os.environ.get('CLAWBYTES_LLM_MODEL', 'gemma4:31b-cloud'),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.7,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urlopen(req, timeout=20, context=ctx) as resp:
                result = json.loads(resp.read().decode())
            paragraph = result['choices'][0]['message']['content'].strip().strip('"')
            if paragraph:
                return paragraph
    except Exception as e:
        print(f"Opening paragraph generation via xAI failed: {e}", file=sys.stderr)
    
    # Fallback: try Claude CLI
    try:
        import subprocess
        result = subprocess.run(
            ['claude', '--dangerously-skip-permissions', '-p', prompt],
            capture_output=True, text=True, timeout=30
        )
        paragraph = result.stdout.strip().strip('"')
        if result.returncode != 0 or not paragraph:
            return ''
        return paragraph
    except Exception as e:
        print(f"Opening paragraph generation failed: {e}", file=sys.stderr)
        return ''


def generate_lead_deepdive(item, cache_path=None):
    """Generate a 3-4 sentence editorial deep-dive for the lead story."""
    if cache_path is None:
        cache_path = MEMORY_DIR / '.claws-hook-cache.json'

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    cache_key = f"deepdive:{item.get('url', '')}:{item.get('title', '')[:60]}"
    if cache_key in cache:
        return cache[cache_key]

    title = item.get('title', '')
    summary = item.get('summary', '')
    source = item.get('sourceType', '')
    cat = item.get('primaryCategory', '')

    prompt = f"""Write a sharp 3-4 sentence editorial deep-dive (60-90 words) for a newsletter's top story.

Title: {title}
Summary: {summary}
Source: {source}
Category: {cat}

Rules:
- First sentence: what happened, stated directly
- Middle: why it matters — the implication, the shift, the tension
- Last sentence: an opinionated take or what to watch for next
- Be specific, not vague. Name names, cite details from the summary
- No corporate filler: no 'explores', 'reveals', 'highlights', 'offering insights'
- Voice: sharp, informed, slightly opinionated — like a smart analyst friend
- Output ONLY the paragraph, nothing else"""

    try:
        import subprocess
        result = subprocess.run(
            ['claude', '--dangerously-skip-permissions', '-p', prompt],
            capture_output=True, text=True, timeout=30
        )
        deepdive = result.stdout.strip().strip('"')
        if result.returncode != 0 or not deepdive:
            raise ValueError(f"Claude CLI failed: {result.stderr[:200]}")
        if len(deepdive) > 600:
            deepdive = truncate_text(deepdive, max_len=500)
    except Exception as e:
        print(f"Lead deep-dive generation failed: {e}", file=sys.stderr)
        # Fallback to the shorter hook
        deepdive = generate_lead_hook(item, cache_path)

    cache[cache_key] = deepdive
    try:
        cache_path.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass

    return deepdive


CATEGORY_TAGS = frozenset({
    'user sentiment and operator pain points', 'cost pressure and pricing pain',
    'security and safety risk', 'model choice and competitive pressure',
    'operator pain points', 'pricing pain', 'user sentiment',
    'cost pressure', 'competitive pressure',
    'community signal', 'model discussion', 'model selection',
    'cost and access', 'security concerns', 'use case fit',
})


def pulse_topic(title: str) -> str:
    """Return a short topic tag for community pulse dedup."""
    low = (title or '').lower()
    if any(x in low for x in ['thanks', 'anthropic', 'gratitude']):
        return 'anthropic sentiment'
    if any(x in low for x in ['free', 'cheap', 'cost', 'pricing', 'expensive', 'token', 'spend', 'budget']):
        return 'cost and access'
    if any(x in low for x in ['switch to', 'switched to', 'best model', 'which model', 'glm-5', 'gpt-5', 'claude to', 'anthropic ban']):
        return 'model switching'
    if any(x in low for x in ['security', 'unsafe', 'sandbox', 'permission']):
        return 'security concerns'
    if any(x in low for x in ['breakage', 'broken', 'bug', 'debugging', 'fix']):
        return 'reliability issues'
    if any(x in low for x in ['unagentic', 'agent not', 'not working', 'stopped']):
        return 'agent behavior'
    return 'community signal'


def compress_community_pulse(items):
    """Merge community threads on the same topic into a single entry."""
    from collections import defaultdict
    topic_groups = defaultdict(list)

    for item in items:
        topic = pulse_topic(item.get('title', ''))
        topic_groups[topic].append(item)

    merged = []
    for topic, group in topic_groups.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            group.sort(key=lambda x: -(x.get('score') or 0))
            lead = dict(group[0])
            rest_count = len(group) - 1
            total_ups = sum(i.get('rawScore', 0) for i in group)
            total_comments = sum(i.get('rawComments', 0) for i in group)
            lead['summary'] = f"{len(group)} threads on {topic} ({total_ups}\u2191 / {total_comments}\U0001f4ac)"
            merged.append(lead)

    merged.sort(key=lambda x: -(x.get('score') or 0))
    return merged


def generate_radar_oneliner(title, item):
    """Generate a radar one-liner via xAI, or empty string on failure."""
    prompt = f"""Write ONE sentence (max 10 words) stating the concrete risk, implication, or action.
Title: {title}
Rules:
- Do NOT restate the title — say what the actual risk or implication IS
- Be specific, not vague: name the risk, name the tool, name the change
- No filler: no 'could', 'might', 'may', 'potentially', 'worth watching'
- Output ONLY the sentence, nothing else."""
    return _llm_call_short(prompt)


def generate_community_oneliner(title, item):
    """Generate a community pulse one-liner via xAI, or empty string on failure."""
    prompt = f"""Write ONE sentence (max 12 words) capturing the core tension or question.
Title: {title}
Rules:
- Do NOT restate the title — capture the underlying debate or need
- Be specific: what are users actually arguing about or asking for?
- No filler: no 'debate', 'discuss', 'explore', 'highlights'
- Output ONLY the sentence, nothing else."""
    return _llm_call_short(prompt)


def _llm_call_short(prompt, max_tokens=60):
    """Short LLM call via xAI API. Returns text or empty string."""
    try:
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if not api_key:
            return ''
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = Request(
            f"{os.environ.get('CLAWBYTES_LLM_URL', 'https://api.tenspire.ai/v1')}/chat/completions",
            data=json.dumps({
                'model': os.environ.get('CLAWBYTES_LLM_MODEL', 'gemma4:31b-cloud'),
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
                'temperature': 0.5,
            }).encode(),
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
        )
        with urlopen(req, timeout=15, context=ctx) as resp:
            result = json.loads(resp.read().decode())
        text = result['choices'][0]['message']['content'].strip().strip('"')
        if text:
            return text
    except Exception as e:
        print(f'  _llm_call_short failed: {e}', file=sys.stderr)
    return ''


def is_category_tag(text):
    """Return True if text is just a category tag, not a real summary."""
    return text.lower().strip() in CATEGORY_TAGS


def generate_shipped_context(item, cache_path=None):
    """Generate a one-line context blurb for a shipped item via Claude CLI."""
    if cache_path is None:
        cache_path = MEMORY_DIR / '.claws-hook-cache.json'

    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    title = item.get('title', '')
    url = item.get('url', '')
    summary = (item.get('summary') or '').strip()

    cache_key = f"ship:{title}"
    if cache_key in cache:
        return cache[cache_key]

    # If we have a real summary already, use it
    generic_summaries = ('New release', 'New Moltis release', 'New Hermes release',
                         'Worth reading', 'New OpenClaw release', 'New Codex release')
    if summary and summary not in generic_summaries:
        cleaned = strip_engagement_from_summary(summary)
        if cleaned and len(cleaned) > 5:
            result = truncate_text(cleaned, max_len=80, min_cutoff=10)
            cache[cache_key] = result
            try:
                cache_path.write_text(json.dumps(cache, indent=2))
            except OSError:
                pass
            return result

    # Use Claude CLI to generate a one-liner from the title + URL
    prompt = f"""Write ONE sentence (max 12 words) describing what this software release does or changes.
Title: {title}
URL: {url}
If you can't tell what changed from the title, describe what the project IS in one line.
Output ONLY the sentence, nothing else. No quotes."""

    try:
        import subprocess
        result = subprocess.run(
            ['claude', '--dangerously-skip-permissions', '-p', prompt],
            capture_output=True, text=True, timeout=20
        )
        ctx = result.stdout.strip().strip('"')
        if result.returncode == 0 and ctx and len(ctx) < 120:
            cache[cache_key] = ctx
            try:
                cache_path.write_text(json.dumps(cache, indent=2))
            except OSError:
                pass
            return ctx
    except Exception:
        pass

    # Minimal fallback — describe what the project IS, not just "X release"
    project_descriptions = {
        'Moltis': 'Agent framework',
        'Hermes': 'Agent framework',
        'Codex': 'AI coding agent',
        'OpenClaw': 'AI agent platform',
        'IronClaw': 'Agent framework',
        'NemoClaw': 'Agent framework',
    }
    match = re.match(r'^(.+?)\s+(?:v?\d[\d.\-a-zA-Z]+|20\d{6}\.\d+|\(\d+ releases?\))$', title)
    if match:
        project = match.group(1).strip()
        desc = project_descriptions.get(project, 'release')
        return f"{project} — {desc}"
    return ''


def generate_tomorrow_line():
    """Generate a real 'Tomorrow, watch for...' preview from the queued backlog."""
    if not BACKLOG_FILE.exists():
        return ''

    try:
        backlog = json.loads(BACKLOG_FILE.read_text())
        queued = [i for i in backlog.get('items', []) if i.get('status') == 'queued']
        # Filter out version-number releases — they're noise for a preview
        queued = [q for q in queued if not re.match(
            r'^.+?\s+(?:v?\d[\d.\-a-zA-Z]+|20\d{6}\.\d+)$', q.get('title', '')
        )]
        queued.sort(key=lambda x: -(x.get('score') or 0))
        if not queued:
            return ''

        top = queued[0]
        title = top.get('title', '')
        if not title:
            return ''

        # Clean up Reddit prefix for readability
        display_title = re.sub(r'^r/\w+:\s*', '', title)
        return f"Tomorrow: {display_title}"
    except Exception:
        return ''


def generate_email():
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %-d, %Y")

    clawbytes_posts = get_clawbytes_posts(hours=48)
    notion_changes = get_notion_changes(hours=48)

    # Split ClawBytes by category
    shipped = [i for i in clawbytes_posts if i.get('primaryCategory') == 'ship']
    watch = [i for i in clawbytes_posts if i.get('primaryCategory') == 'watch']
    reads = [i for i in clawbytes_posts if i.get('primaryCategory') == 'read']
    community = [i for i in clawbytes_posts if i.get('primaryCategory') == 'community']

    # Deduplicate rapid-fire releases
    shipped = dedupe_releases(shipped)

    # The Lead: top non-ship story by score (releases are noise, insight is signal)
    lead_candidates = watch + reads + community
    lead_item = lead_candidates[0] if lead_candidates else None

    # Radar: exclude lead item
    radar = watch + reads
    if lead_item:
        radar = [i for i in radar if i.get('id') != lead_item.get('id')]

    # Community: exclude lead item AND items already shown in Radar
    radar_ids = {i.get('id') for i in radar}
    community_display = community
    if lead_item:
        community_display = [i for i in community if i.get('id') != lead_item.get('id')]
    # Cross-section dedup: don't repeat items that appeared in Radar
    community_display = [i for i in community_display if i.get('id') not in radar_ids]

    # Community dedup: merge same-topic Reddit threads
    community_display = compress_community_pulse(community_display)

    # Notion: only genuinely new entries from TODAY (not just recently edited)
    today_str = now.strftime("%Y-%m-%d")
    new_entries = [c for c in notion_changes if c['is_new'] and c['edited'] == today_str]

    # ── Build HTML ──
    html = """<!DOCTYPE html>
<html>
<head><style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; color: #1a1a1a; background: #ffffff; max-width: 580px; margin: 0 auto; padding: 24px 16px; line-height: 1.55; }
h1 { font-size: 21px; font-weight: 700; color: #111; margin: 0 0 2px; letter-spacing: -0.3px; }
.subtitle { color: #666; font-size: 13px; margin-bottom: 8px; }
.opening { font-size: 14px; color: #333; line-height: 1.6; margin-bottom: 24px; }
h2 { font-size: 15px; font-weight: 600; color: #111; margin-top: 28px; margin-bottom: 10px; padding-bottom: 4px; border-bottom: 1px solid #e5e5e5; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
ul { padding-left: 18px; margin-top: 4px; }
li { margin-bottom: 6px; font-size: 14px; color: #333; }
li .ctx { color: #888; font-size: 12px; }
.footer { color: #999; font-size: 11px; margin-top: 32px; border-top: 1px solid #e5e5e5; padding-top: 10px; }
.lead { margin-bottom: 20px; }
.lead .title { font-weight: 700; font-size: 16px; color: #111; }
.lead .deepdive { font-size: 14px; color: #333; margin: 6px 0 0; line-height: 1.6; }
.lead .source { font-size: 12px; color: #999; margin-top: 4px; }
.quick-hit { margin-bottom: 4px; font-size: 14px; }
.quick-hit .headline { font-weight: 600; }
.quick-hit .oneliner { color: #666; font-size: 13px; }
.map-entry { margin-bottom: 8px; }
.map-entry .name { font-weight: 600; font-size: 13.5px; color: #333; }
.map-entry .cat { font-size: 11px; color: #888; margin-left: 6px; }
.map-entry .hook { font-size: 13px; color: #555; margin: 2px 0 0; }
.tomorrow { font-size: 12px; color: #777; font-style: italic; margin-top: 16px; }
</style></head>
<body>

<h1>Claws Daily</h1>
<div class="subtitle">""" + date_str + """ &middot; The agent ecosystem, briefed</div>
"""

    # ── 0. Opening Paragraph — connect the dots ──
    opening = generate_opening_paragraph(lead_item, shipped, radar, community_display)
    if opening:
        html += f'<p class="opening">{opening}</p>\n'

    # ── 1. The Lead — mini deep-dive ──
    if lead_item:
        deepdive = generate_lead_deepdive(lead_item)
        title = lead_item.get('title', '')
        url = lead_item.get('url', '#')
        src = lead_item.get('sourceType', '')
        score_str = ''
        if src == 'reddit':
            upvotes = lead_item.get('rawScore', 0)
            comments = lead_item.get('rawComments', 0)
            score_str = f' &middot; {upvotes}&#8593; / {comments}&#x1f4ac;'
        elif src == 'hackernews':
            score_str = f' &middot; {lead_item.get("rawScore", 0)}pts'

        html += '<h2>The Lead</h2>\n'
        html += '<div class="lead">\n'
        html += f'<div class="title"><a href="{url}">{title}</a></div>\n'
        if deepdive:
            html += f'<p class="deepdive">{deepdive}</p>\n'
        html += f'<div class="source">{src}{score_str}</div>\n'
        html += '</div>\n'

    # ── 2. Shipped — consolidated, with real context ──
    if shipped:
        html += '<h2>&#x1F680; Shipped</h2>\n<ul>\n'
        for item in shipped[:6]:
            title = item.get('title', '?')
            url = item.get('url', '#')
            ctx = generate_shipped_context(item)
            html += f'<li><a href="{url}"><strong>{title}</strong></a>'
            if ctx:
                html += f'<br><span class="ctx">{ctx}</span>'
            html += '</li>\n'
        html += '</ul>\n'

    # ── 3. Radar — top 3 with context, rest as "Also on our radar" ──
    if radar:
        html += '<h2>&#x1F4E1; Radar</h2>\n'
        top_radar = radar[:3]
        rest_radar = radar[3:8]
        for item in top_radar:
            title = item.get('title', '?')
            url = item.get('url', '#')
            summary = (item.get('summary') or '').strip()
            summary = strip_engagement_from_summary(summary)
            if is_category_tag(summary):
                summary = ''
            # Generate one-liner via xAI (or Claude CLI fallback) for top items if summary is empty
            if not summary:
                summary = generate_radar_oneliner(title, item)
            oneliner = truncate_text(summary, max_len=120, min_cutoff=10) if summary else ''
            src = item.get('sourceType', '')
            score_str = ''
            if src == 'reddit':
                score_str = f' ({item.get("rawScore", 0)}&#8593; / {item.get("rawComments", 0)}&#x1f4ac;)'
            elif src == 'hackernews':
                score_str = f' ({item.get("rawScore", 0)}pts)'
            html += '<div class="quick-hit">'
            html += f'<span class="headline"><a href="{url}">{title}</a></span>'
            html += f'<span class="ctx">{score_str}</span>'
            if oneliner:
                html += f'<br><span class="oneliner">{oneliner}</span>'
            html += '</div>\n'

        if rest_radar:
            html += '<p style="font-size:12px;color:#888;margin:10px 0 4px;">Also on our radar:</p>\n<ul>\n'
            for item in rest_radar:
                title = item.get('title', '?')
                url = item.get('url', '#')
                src = item.get('sourceType', '')
                score_str = ''
                if src == 'reddit':
                    score_str = f' <span class="ctx">({item.get("rawScore", 0)}&#8593;)</span>'
                html += f'<li><a href="{url}">{title}</a>{score_str}</li>\n'
            html += '</ul>\n'

    # ── 4. Community Pulse — conversations with context ──
    if community_display:
        html += '<h2>&#x1f4ac; Community Pulse</h2>\n'
        for item in community_display[:5]:
            title = item.get('title', '?')
            url = item.get('url', '#')
            summary = (item.get('summary') or '').strip()
            summary = strip_engagement_from_summary(summary)
            if is_category_tag(summary):
                summary = ''
            # Generate context for community items that lack summaries
            if not summary:
                summary = generate_community_oneliner(title, item)
            oneliner = truncate_text(summary, max_len=100, min_cutoff=10) if summary else ''
            src = item.get('sourceType', '')
            score_parts = []
            if src == 'reddit':
                score_parts.append(f'{item.get("rawScore", 0)}&#8593;')
                score_parts.append(f'{item.get("rawComments", 0)}&#x1f4ac;')
            elif src == 'hackernews':
                score_parts.append(f'{item.get("rawScore", 0)}pts')
            score_str = f' <span class="ctx">({" / ".join(score_parts)})</span>' if score_parts else ''
            html += '<div class="quick-hit">'
            html += f'<span class="headline"><a href="{url}">{title}</a></span>{score_str}'
            if oneliner:
                html += f'<br><span class="oneliner">{oneliner}</span>'
            html += '</div>\n'

    # ── 5. From the Map — ONLY genuinely new TODAY ──
    if new_entries:
        map_items = []
        for c in new_entries[:4]:
            details = c.get('details', {})
            hook = generate_hook(c['name'], details)
            map_items.append((c, hook))

        if map_items:
            html += '<h2>&#x1F5FA; New on the Map</h2>\n'
            for c, text in map_items:
                html += '<div class="map-entry">\n'
                html += f'<span class="map-entry name">{c["name"]}</span>'
                html += f'<span class="cat">NEW &middot; {c["category"]}</span>\n'
                if text:
                    html += f'<p class="hook">{text}</p>\n'
                html += '</div>\n'

    # ── Footer with Tomorrow preview ──
    tomorrow = generate_tomorrow_line()
    html += '\n<div class="footer">\n'
    if tomorrow:
        html += f'<p class="tomorrow">{tomorrow}</p>\n'
    html += """<p>&#x1F980; <a href="https://sovs.notion.site/Claws-337000c0d590805daf74f27d19215184">Full map</a> &middot; <a href="https://t.me/clawbytes">@clawbytes</a> &middot; <a href="https://t.me/modelbytes">@modelbytes</a></p>
<p>Reply with feedback &middot; <a href="mailto:clawback1@proton.me">clawback1@proton.me</a></p>
</div>
</body></html>"""

    return html


def send_email(html):
    from protonmail import ProtonMail
    from protonmail.models import LoginType

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%B %-d, %Y")
    subject = f"Claws Daily \u2014 {date_str}"

    print(f"Logging into Proton...")
    pm = ProtonMail()
    pm.login(PROTON_USER, PROTON_PASS, login_type=LoginType.DEV)

    print(f"Sending to {RECIPIENT}...")
    message = pm.create_message(
        recipients=[RECIPIENT],
        subject=subject,
        body=html,
    )
    result = pm.send_message(message)
    print(f"Done \u2014 {result}")
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claws Daily Digest")
    parser.add_argument("--send", action="store_true", help="Send via email")
    parser.add_argument("--preview", action="store_true", help="Preview text to stdout")
    parser.add_argument("--html", action="store_true", help="Output raw HTML to file")
    parser.add_argument("--output", default="/tmp/claws-digest.html", help="Output file for --html")
    args = parser.parse_args()

    html = generate_email()

    if args.html:
        Path(args.output).write_text(html)
        print(f"Wrote HTML to {args.output}")
    elif args.send:
        send_email(html)
    elif args.preview:
        from html.parser import HTMLParser
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
            def handle_data(self, data):
                self.text.append(data.strip())
        extractor = TextExtractor()
        extractor.feed(html)
        print('\n'.join(t for t in extractor.text if t))
    else:
        print("Use --preview, --html, or --send")


if __name__ == "__main__":
    main()
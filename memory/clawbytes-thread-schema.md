# ClawBytes Thread Architecture Schema

## Overview

This document defines the thread-based architecture for ClawBytes, where content
is organized into topical threads rather than isolated category bundles.

## Key Concepts

### Thread
A thread is a topical grouping of related content items. Threads persist across
multiple publishing cycles and can be updated with new related content.

### Thread Lifecycle
1. **Create** - New high-signal item creates a thread
2. **Update** - New related items are added to the thread
3. **Publish** - Thread summary is posted to Telegram
4. **Close** - Thread expires after TTL or manual closure

## Data Schema

### Thread State (`memory/clawbytes-threads.json`)

```json
{
  "threads": [
    {
      "id": "thread_abc123",
      "title": "OpenClaw 2026.4.x Release Series",
      "slug": "openclaw-2026-4-release-series",
      "topic": ["openclaw", "release", "version"],
      "category": "ship",
      "status": "active",
      "sourceType": "rss",
      "sourceName": "OpenClaw Releases",
      "priority": 100,
      "createdAt": "2026-04-08T15:00:00Z",
      "updatedAt": "2026-04-09T02:00:00Z",
      "publishedAt": "2026-04-08T16:00:00Z",
      "lastPublishedItemCount": 3,
      "itemCount": 5,
      "items": [
        "item_id_1",
        "item_id_2",
        "item_id_3"
      ],
      "publishedItems": [
        "item_id_1",
        "item_id_2"
      ],
      "pendingItems": [
        "item_id_3"
      ]
    }
  ],
  "items": [
    {
      "id": "item_abc123",
      "threadId": "thread_abc123",
      "url": "https://github.com/openclaw/openclaw/releases/tag/v2026.4.8",
      "title": "OpenClaw 2026.4.8",
      "summary": "New release with bug fixes",
      "sourceType": "rss",
      "sourceName": "OpenClaw Releases",
      "score": 110.5,
      "publishedAt": "2026-04-08T02:59:56Z",
      "discoveredAt": "2026-04-08T15:48:13Z",
      "addedToThreadAt": "2026-04-08T15:48:13Z"
    }
  ],
  "threadIndex": {
    "openclaw-releases": "thread_abc123"
  },
  "metadata": {
    "lastCollection": "2026-04-09T04:00:00Z",
    "lastPublish": {
      "ship": "2026-04-08T16:00:00Z",
      "watch": "2026-04-07T19:00:00Z",
      "read": "2026-04-08T22:00:00Z",
      "community": "2026-04-09T02:00:00Z"
    },
    "threadCount": 15,
    "activeThreadCount": 8,
    "closedThreadCount": 7
  }
}
```

### Thread Matching Rules

#### Ship Threads
- Group by repository name (e.g., all OpenClaw releases → one thread)
- Group by version family (e.g., 2026.4.x releases)
- Thread TTL: 96 hours

#### Watch Threads
- Group by security issue family (e.g., all advisories for a repo)
- Group by topic keywords (e.g., "sandbox", "permission", "credential")
- Thread TTL: 120 hours

#### Read Threads
- Group by author/source (e.g., Simon Willison posts)
- Group by topic (e.g., agent engineering, MCP, security)
- Thread TTL: 96 hours

#### Community Threads
- Group by discussion topic (e.g., "cost concerns", "model comparison")
- Group by subreddit thread clusters
- Thread TTL: 72 hours

### Thread Creation Logic

1. **Check for existing thread match**
   - Ship: Same repo name or version family
   - Watch: Same advisory family or related keywords
   - Read: Same author or topic cluster
   - Community: Same discussion topic

2. **If match found, add item to thread**
   - Update thread's updatedAt
   - Add item to pendingItems
   - Increment itemCount

3. **If no match, create new thread**
   - Generate thread ID
   - Set category and priority
   - Add item as first pendingItem

### Thread Publishing Logic

1. **Check readiness per category**
   - Has pending (unpublished) items
   - Meets minimum item count
   - Within time window
   - Top item score exceeds threshold

2. **Format thread summary**
   - Include thread title/topic
   - Show new items since last publish
   - Link to thread's context

3. **Update thread state**
   - Move pendingItems to publishedItems
   - Update lastPublishedItemCount
   - Update publishedAt

### Thread Closure Logic

1. **Automatic closure conditions**
   - Thread TTL exceeded
   - No updates for 24 hours
   - All items expired

2. **Closed threads are retained**
   - For historical reference
   - For "related threads" suggestions

## Thread ID Generation

Thread IDs are based on stable identifiers:
- Ship: `ship_{repo_name}` (e.g., `ship_openclaw`)
- Watch: `watch_{topic_hash}` (topic keyword hash)
- Read: `read_{source}_{topic_hash}`
- Community: `community_{topic_hash}`

## Backward Compatibility

The existing backlog system (`clawbytes-backlog.json`) remains in place:
- Source monitors continue to write to their state files
- Thread manager reads from both the old backlog AND source states
- Items can exist in both the backlog AND threads

This allows:
- Gradual migration
- Fallback to category bundles if threads fail
- Both systems to run in parallel during transition

## Publishing Schedule

Threads follow the same staggered schedule as category bundles:
- Ship: 9:00 AM PT
- Watch: 12:00 PM PT (noon)
- Read: 3:00 PM PT
- Community: 7:00 PM PT

Each publish cycle:
1. Check for threads with pending items
2. For threads with new items, post an update
3. For newly created threads, post thread introduction

## Thread Update Message Format

### New Thread
```
⚙️ **Ship — OpenClaw Releases**

New thread tracking OpenClaw 2026.4.x releases.

📦 OpenClaw 2026.4.8
   New release — bug fixes and improvements
   https://github.com/openclaw/openclaw/releases/tag/v2026.4.8
```

### Thread Update
```
⚙️ **Ship — OpenClaw Releases** (2 new)

📦 OpenClaw 2026.4.9
   Latest release — performance improvements
   https://github.com/openclaw/openclaw/releases/tag/v2026.4.9

📦 OpenClaw 2026.4.10
   Latest release — security patch
   https://github.com/openclaw/openclaw/releases/tag/v2026.4.10
```

### Thread Closed
```
⚙️ **Ship — OpenClaw Releases** [closed]

Thread closed after 96 hours. 5 releases tracked.
```
#!/usr/bin/env python3
"""
Generate profile/README.md from profile/README.md.tpl using GitHub GraphQL (markscribe-compatible
queries merged across multiple accounts) plus RSS for the blog section.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from calendar import timegm
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser

ROOT = Path(__file__).resolve().parents[1]
TPL_PATH = ROOT / "profile" / "README.md.tpl"
OUT_PATH = ROOT / "profile" / "README.md"
ACCOUNTS_JSON = ROOT / "profile" / "accounts.json"
GRAPHQL_URL = "https://api.github.com/graphql"
RSS_URL = "https://blog.ddsrem.com/rss.xml"

CONTRIBUTIONS_QUERY = """
query($username: String!) {
  user(login: $username) {
    contributionsCollection {
      commitContributionsByRepository(maxRepositories: 100) {
        contributions(first: 1) {
          edges {
            node {
              occurredAt
            }
          }
        }
        repository {
          nameWithOwner
          url
          description
          isPrivate
        }
      }
    }
  }
}
"""

PULL_REQUESTS_QUERY = """
query($username: String!, $count: Int!) {
  user(login: $username) {
    pullRequests(first: $count, orderBy: {field: CREATED_AT, direction: DESC}) {
      edges {
        node {
          title
          url
          createdAt
          repository {
            nameWithOwner
            url
            isPrivate
          }
        }
      }
    }
  }
}
"""

RELEASES_PAGE_QUERY = """
query($username: String!, $after: String) {
  user(login: $username) {
    repositoriesContributedTo(
      first: 100
      after: $after
      includeUserRepositories: true
      contributionTypes: COMMIT
      privacy: PUBLIC
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      edges {
        cursor
        node {
          nameWithOwner
          url
          description
          isPrivate
          stargazers {
            totalCount
          }
          releases(first: 10, orderBy: {field: CREATED_AT, direction: DESC}) {
            nodes {
              name
              tagName
              publishedAt
              url
              isPrerelease
              isDraft
            }
          }
        }
      }
    }
  }
}
"""


def humanize(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    diff = now - dt
    s = diff.total_seconds()
    if s < 0:
        return dt.strftime("%Y-%m-%d")
    if s < 60:
        return "just now"
    if s < 3600:
        m = int(s // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if s < 86400 * 7:
        d = int(s // 86400)
        return f"{d} day{'s' if d != 1 else ''} ago"
    if s < 86400 * 30:
        w = int(s // (86400 * 7))
        return f"{w} week{'s' if w != 1 else ''} ago"
    return dt.strftime("%Y-%m-%d")


def graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "DDSRem-Dev-profile-readme-generator",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


@dataclass
class Contribution:
    occurred_at: datetime
    repo_name: str
    repo_url: str
    description: str


def fetch_contributions(token: str, username: str) -> list[Contribution]:
    data = graphql(token, CONTRIBUTIONS_QUERY, {"username": username})
    user = data.get("user")
    if not user:
        return []
    coll = (user.get("contributionsCollection") or {}).get("commitContributionsByRepository") or []
    meta = f"{username}/{username}"
    out: list[Contribution] = []
    for block in coll:
        repo = block.get("repository") or {}
        if repo.get("isPrivate"):
            continue
        nwo = repo.get("nameWithOwner") or ""
        if nwo == meta:
            continue
        edges = ((block.get("contributions") or {}).get("edges")) or []
        if not edges:
            continue
        occurred = edges[0].get("node", {}).get("occurredAt")
        if not occurred:
            continue
        occurred_at = parse_iso(occurred)
        out.append(
            Contribution(
                occurred_at=occurred_at,
                repo_name=nwo,
                repo_url=repo.get("url") or "",
                description=(repo.get("description") or "").strip(),
            )
        )
    out.sort(key=lambda c: c.occurred_at, reverse=True)
    return out


@dataclass
class PullRequest:
    title: str
    url: str
    created_at: datetime
    repo_name: str
    repo_url: str


def fetch_pull_requests(token: str, username: str, count: int) -> list[PullRequest]:
    # +1 like markscribe (meta-repo skip)
    data = graphql(token, PULL_REQUESTS_QUERY, {"username": username, "count": count + 1})
    user = data.get("user")
    if not user:
        return []
    edges = ((user.get("pullRequests") or {}).get("edges")) or []
    meta = f"{username}/{username}"
    out: list[PullRequest] = []
    for e in edges:
        node = e.get("node") or {}
        repo = node.get("repository") or {}
        if repo.get("isPrivate"):
            continue
        nwo = repo.get("nameWithOwner") or ""
        if nwo == meta:
            continue
        out.append(
            PullRequest(
                title=node.get("title") or "",
                url=node.get("url") or "",
                created_at=parse_iso(node["createdAt"]),
                repo_name=nwo,
                repo_url=repo.get("url") or "",
            )
        )
        if len(out) >= count:
            break
    return out


@dataclass
class ReleaseRow:
    name: str
    repo_url: str
    description: str
    stargazers: int
    tag_name: str
    release_url: str
    published_at: datetime


def fetch_releases(token: str, username: str) -> list[ReleaseRow]:
    """Mirror markscribe recentReleases: paginate repositoriesContributedTo, collect repos with a valid release."""
    meta = f"{username}/{username}"
    repos_with_release: list[ReleaseRow] = []
    after: str | None = None
    while True:
        variables: dict[str, Any] = {"username": username, "after": after}
        data = graphql(token, RELEASES_PAGE_QUERY, variables)
        user = data.get("user")
        if not user:
            break
        conn = user.get("repositoriesContributedTo") or {}
        edges = conn.get("edges") or []
        if not edges:
            break
        for e in edges:
            node = e.get("node") or {}
            if node.get("isPrivate"):
                continue
            nwo = node.get("nameWithOwner") or ""
            if nwo == meta:
                continue
            rel_nodes = ((node.get("releases") or {}).get("nodes")) or []
            chosen = None
            for rel in rel_nodes:
                if rel.get("isPrerelease") or rel.get("isDraft"):
                    continue
                tag = rel.get("tagName") or ""
                pub = rel.get("publishedAt")
                if not tag or not pub:
                    continue
                chosen = rel
                break
            if not chosen:
                continue
            pub_at = parse_iso(chosen["publishedAt"])
            repos_with_release.append(
                ReleaseRow(
                    name=nwo,
                    repo_url=node.get("url") or "",
                    description=(node.get("description") or "").strip(),
                    stargazers=int(((node.get("stargazers") or {}).get("totalCount")) or 0),
                    tag_name=chosen.get("tagName") or "",
                    release_url=chosen.get("url") or "",
                    published_at=pub_at,
                )
            )
        page = conn.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    repos_with_release.sort(
        key=lambda r: (r.published_at, r.stargazers),
        reverse=True,
    )
    return repos_with_release


def format_contributions_md(items: list[Contribution], limit: int) -> str:
    lines = []
    for c in items[:limit]:
        desc = f" - {c.description}" if c.description else ""
        lines.append(
            f"- [{c.repo_name}]({c.repo_url}){desc} ({humanize(c.occurred_at)})"
        )
    return "\n".join(lines) if lines else "- _(no recent public contributions)_"


def format_prs_md(items: list[PullRequest], limit: int) -> str:
    lines = []
    for p in items[:limit]:
        lines.append(
            f"- [{p.title}]({p.url}) on [{p.repo_name}]({p.repo_url}) ({humanize(p.created_at)})"
        )
    return "\n".join(lines) if lines else "- _(no recent pull requests)_"


def format_releases_md(items: list[ReleaseRow], limit: int) -> str:
    lines = []
    for r in items[:limit]:
        desc = f" - {r.description}" if r.description else ""
        lines.append(
            f"- [{r.name}]({r.repo_url}) ([{r.tag_name}]({r.release_url}), "
            f"{humanize(r.published_at)}){desc}"
        )
    return "\n".join(lines) if lines else "- _(no recent releases)_"


def _http_get(url: str, timeout: int = 45) -> bytes:
    """Fetch URL with a real browser User-Agent; feedparser's default fetch is often blocked in CI."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; DDSRem-Dev/.github readme-generator; "
                "+https://github.com/DDSRem-Dev/.github)"
            ),
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_rss_block(url: str, limit: int) -> str:
    try:
        body = _http_get(url)
    except (urllib.error.URLError, OSError) as e:
        print(f"RSS fetch failed ({url}): {e}", file=sys.stderr)
        return "- _(feed unavailable)_"

    parsed = feedparser.parse(body)
    if getattr(parsed, "bozo", False) and not (parsed.entries or []):
        exc = getattr(parsed, "bozo_exception", None)
        print(f"RSS parse warning: {exc}", file=sys.stderr)

    lines = []
    for entry in (parsed.entries or [])[:limit]:
        title_raw = entry.get("title") or ""
        title = title_raw.strip() if isinstance(title_raw, str) else str(title_raw).strip()
        link = (entry.get("link") or "").strip()
        if not link and entry.get("links"):
            for href in entry["links"]:
                if href.get("rel") == "alternate" and href.get("href"):
                    link = href["href"].strip()
                    break
        if not link:
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            dt = datetime.fromtimestamp(timegm(published), tz=timezone.utc)
            when = humanize(dt)
        else:
            when = ""
        suffix = f" ({when})" if when else ""
        lines.append(f"- [{title}]({link}){suffix}")
    return "\n".join(lines) if lines else "- _(feed unavailable)_"


def load_accounts() -> list[str]:
    raw = os.environ.get("PROFILE_ACCOUNTS", "").strip()
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
    if ACCOUNTS_JSON.is_file():
        data = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
        acc = data.get("accounts")
        if isinstance(acc, list):
            return [str(a).strip() for a in acc if str(a).strip()]
    return ["DDSRem", "DDSRem-Bot"]


def merge_contributions(all_lists: list[list[Contribution]], limit: int) -> list[Contribution]:
    merged: list[Contribution] = []
    for lst in all_lists:
        merged.extend(lst)
    merged.sort(key=lambda c: c.occurred_at, reverse=True)
    # dedupe same repo keeping newest occurrence only (timeline clarity)
    seen: set[str] = set()
    unique: list[Contribution] = []
    for c in merged:
        key = c.repo_url or c.repo_name
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
        if len(unique) >= limit:
            break
    return unique[:limit]


def merge_pull_requests(all_lists: list[list[PullRequest]], limit: int) -> list[PullRequest]:
    merged: list[PullRequest] = []
    for lst in all_lists:
        merged.extend(lst)
    merged.sort(key=lambda p: p.created_at, reverse=True)
    seen: set[str] = set()
    out: list[PullRequest] = []
    for p in merged:
        if p.url in seen:
            continue
        seen.add(p.url)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def merge_releases(all_lists: list[list[ReleaseRow]], limit: int) -> list[ReleaseRow]:
    merged: list[ReleaseRow] = []
    for lst in all_lists:
        merged.extend(lst)
    merged.sort(
        key=lambda r: (r.published_at, r.stargazers),
        reverse=True,
    )
    seen: set[str] = set()
    out: list[ReleaseRow] = []
    for r in merged:
        key = r.name
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        sys.exit(1)
    accounts = load_accounts()
    if not accounts:
        print("No accounts configured", file=sys.stderr)
        sys.exit(1)

    contrib_lists: list[list[Contribution]] = []
    pr_lists: list[list[PullRequest]] = []
    rel_lists: list[list[ReleaseRow]] = []
    for user in accounts:
        contrib_lists.append(fetch_contributions(token, user))
        pr_lists.append(fetch_pull_requests(token, user, 5))
        rel_lists.append(fetch_releases(token, user))

    contributions_md = format_contributions_md(merge_contributions(contrib_lists, 6), 6)
    prs_md = format_prs_md(merge_pull_requests(pr_lists, 5), 5)
    releases_md = format_releases_md(merge_releases(rel_lists, 5), 5)
    rss_md = fetch_rss_block(RSS_URL, 6)

    if not TPL_PATH.is_file():
        print(f"Missing template {TPL_PATH}", file=sys.stderr)
        sys.exit(1)
    text = TPL_PATH.read_text(encoding="utf-8")
    replacements = {
        "%%CONTRIBUTIONS%%": contributions_md,
        "%%RELEASES%%": releases_md,
        "%%PULL_REQUESTS%%": prs_md,
        "%%RSS%%": rss_md,
    }
    for key, val in replacements.items():
        if key not in text:
            print(f"Warning: placeholder {key} not found in template", file=sys.stderr)
        text = text.replace(key, val)
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()

"""Pi-only read-only presentation; business projections stay in Compass core."""

import re

from compass_brief.task_contract.extraction import requirement_entries


async def brief_view(session, brief):
    snapshot = session.claims.snapshot()
    goal = session.goals.snapshot()
    coverage = snapshot.state.requirement_coverage
    contracts = requirement_entries(goal)
    lookup = {}
    groups = []

    def group(identity, title, entries):
        groups.append({"id": identity, "title": title, "entries": entries})
        lookup.update((entry["id"], entry) for entry in entries)

    async def entry(identity, title, summary, meta="", links=()):
        return {
            "id": identity,
            "title": title,
            "summary": summary,
            "meta": meta,
            "body": await session.inspect([identity]),
            "links": list(links),
        }

    group(
        "goal",
        "Goal",
        [await entry("goal", "Goal", goal.description)] if goal.description else [],
    )
    for key, title, prefix in (
        ("requirements", "Requirements", "R"),
        ("constraints", "Constraints", "K"),
    ):
        rows = []
        for identity, content in contracts:
            if not identity.startswith(prefix):
                continue
            row = coverage.get(identity)
            links = row.claim_ids if row else ()
            meta = (
                f"Associated Claims: {', '.join(links)}"
                if links
                else ("Associated Claims: none" if row else "Associated Claims: not assessed")
            )
            item = await entry(identity, identity, content, meta, links)
            item["body"] += "\n\nAssociation means relevance, not satisfaction."
            rows.append(item)
        group(key, title, rows)

    active = set(snapshot.state.active_ids)
    rows = []
    for claim in snapshot.claims:
        related = [identity for identity, row in coverage.items() if claim.id in row.claim_ids]
        links = list(
            dict.fromkeys(
                [
                    *related,
                    *claim.evidence_refs,
                    *claim.supporting_claim_ids,
                    *claim.conflicts_with,
                    *([claim.superseded_by] if claim.superseded_by else []),
                ]
            )
        )
        item = await entry(
            claim.id,
            claim.id,
            claim.statement,
            f"{claim.grounding} · {claim.verdict}",
            links,
        )
        item["body"] += f"\n\nAssociated Requirements / Constraints: {', '.join(related) or '(none)'}"
        lookup[claim.id] = item
        if claim.id in active:
            rows.append(item)
    group("claims", "Claims", rows)

    for key, title, values in (
        ("tensions", "Tensions", brief.get("tensions", [])),
        ("already-read", "Already read", brief.get("already_read", [])),
        ("attention", "Weak coverage", brief.get("attention", [])),
        (
            "freshness",
            "Evidence status",
            [brief["freshness"]] if brief.get("freshness") else [],
        ),
    ):
        group(
            key,
            title,
            [
                {
                    "id": f"{key}:{index}",
                    "title": f"{title} {index + 1}",
                    "summary": text,
                    "body": text,
                    "meta": "",
                    "links": list(dict.fromkeys(re.findall(r"\b(?:C-\d{3,}|E-\d{3,}|[RK]\d+)\b", text))),
                }
                for index, text in enumerate(values)
            ],
        )
    # Evidence is reachable from a Claim, not a separate Evidence tab.
    for record in session.evidence.snapshot():
        identity = f"E-{record.sequence:03}"
        lookup[identity] = await entry(identity, identity, record.summary or record.tool_name)
    group(
        "preview",
        "Preview model Brief",
        [
            {
                "id": "model-brief",
                "title": "Model Brief",
                "summary": "Current model-facing text",
                "body": session.brief_for_request() or "No Brief yet.",
                "links": [],
                "meta": "",
            }
        ],
    )
    return {"groups": groups, "lookup": lookup}

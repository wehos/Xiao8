"""Combined local-memory totals and five-choice archive supplementation."""

import json
from datetime import datetime, timezone

import pytest

from main_logic import card_forge_facts as F

pytestmark = pytest.mark.unit


def memory(key, **overrides):
    return {
        "id": key,
        "hash": f"hash-{key}",
        "text": f"Memory {key}",
        "importance": 8,
        "created_at": "2020-01-01T00:00:00Z",
        **overrides,
    }


@pytest.fixture
def query_pool(tmp_path, monkeypatch):
    monkeypatch.delenv("NEKO_FORGE_FACTS_URL", raising=False)
    monkeypatch.delenv("NEKO_CARD_FORGE_ALLOW_CHARACTER_OVERRIDE", raising=False)

    async def context(*_args, **_kwargs):
        return F.ActiveNekoContext(
            master_name="Master", lanlan_name="Neko", memory_dir=tmp_path,
            facts_path=tmp_path / "facts.json", source="test",
        )

    monkeypatch.setattr(F, "resolve_active_neko_context", context)

    async def query(active, archive, **kwargs):
        (tmp_path / "facts.json").write_text(json.dumps(active), encoding="utf-8")
        (tmp_path / "facts_archive.json").write_text(json.dumps(archive), encoding="utf-8")
        return await F.build_forge_facts_payload(
            runtime_character_hint="Neko", min_importance=0, limit=5, **kwargs,
        )

    return query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_count,archive_count,expected_archive",
    [(2, 13, 3), (0, 15, 5), (4, 11, 1), (15, 90, 1), (0, 4, 4), (3, 1, 1)],
)
async def test_archive_fills_five_choices(query_pool, active_count, archive_count, expected_archive):
    payload = await query_pool(
        [memory(f"active-{i}") for i in range(active_count)],
        [memory(f"archive-{i}") for i in range(archive_count)],
    )
    facts = payload["facts"]
    expected = min(5, active_count + archive_count)
    assert payload["totalMemoryCount"] == active_count + archive_count
    assert payload["rawCount"] == active_count
    assert payload["archiveRawCount"] == archive_count
    assert payload["returnedCount"] == len(facts) == expected
    assert len({f["id"] for f in facts}) == expected
    assert len({f["hash"] for f in facts}) == expected
    assert sum(f["sourceCollection"] == "facts_archive" for f in facts) == expected_archive
    assert facts[-1]["distantGuaranteed"] is True
    assert payload["fallbackReason"] == ("" if expected == 5 else "insufficient_facts")
    assert "error" not in payload


@pytest.mark.asyncio
async def test_total_deduplicates_both_files_and_counts_used_memories(query_pool):
    active = [memory(f"a-{i}") for i in range(10)]
    archive = [*active, *[memory(f"b-{i}") for i in range(5)]]
    archive += [memory("same-hash", hash=active[0]["hash"]), dict(active[1], hash="other-hash")]
    archive += [None, "invalid", {}]
    payload = await query_pool(active, archive, exclude_fact_ids="a-0,b-0")
    assert payload["totalMemoryCount"] == 15
    assert payload["returnedCount"] == 5
    assert not ({"a-0", "b-0", "same-hash"} & {f["id"] for f in payload["facts"]})


@pytest.mark.asyncio
async def test_fallback_identity_deduplicates_missing_ids_and_hashes(query_pool):
    active = [{"text": "Shared text", "importance": 8}]
    archive = [{"id": "archive-copy", "text": "Shared text", "importance": 8}]
    archive += [memory(f"unique-{i}") for i in range(14)]
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 15
    assert payload["returnedCount"] == 5
    assert "archive-copy" not in {f["id"] for f in payload["facts"]}


@pytest.mark.asyncio
async def test_supplementation_keeps_privacy_arbitration_and_used_exclusions(query_pool):
    active = [memory("active")]
    archive = [memory(f"usable-{i}", absorbed=True) for i in range(4)]
    archive += [memory("private", private=True), memory("redacted", redacted=True)]
    archive += [memory("rejected", arbitration_reason="trust_superseded")]
    archive += [memory("archived", arbitration_archived_at="2026-08-01")]
    archive += [memory("forged-id"), memory("forged-hash"), memory("empty", text="")]
    archive += [memory("active-copy", hash="hash-active")]
    payload = await query_pool(
        active, archive, exclude_fact_ids="forged-id", exclude_hashes="hash-forged-hash",
    )
    assert {f["id"] for f in payload["facts"]} == {"active", *[f"usable-{i}" for i in range(4)]}
    assert payload["returnedCount"] == 5


@pytest.mark.asyncio
async def test_large_total_does_not_fabricate_five_usable_memories(query_pool):
    payload = await query_pool(
        [memory("active")],
        [memory(f"usable-{i}") for i in range(3)]
        + [memory(f"private-{i}", private=True) for i in range(20)],
    )
    assert payload["totalMemoryCount"] == 24
    assert payload["returnedCount"] == 4
    assert payload["fallbackReason"] == "insufficient_facts"


@pytest.mark.asyncio
async def test_undated_archive_memories_can_fill_empty_slots(query_pool):
    payload = await query_pool([], [memory(f"a-{i}", created_at=None) for i in range(15)])
    assert payload["returnedCount"] == 5
    assert payload["archiveDistantCount"] == 0
    assert payload["fallbackReason"] == ""


@pytest.mark.asyncio
async def test_full_active_pool_keeps_recent_and_archive_distant_slots(query_pool):
    now = datetime.now(timezone.utc).isoformat()
    active = [memory(f"recent-{i}", created_at=now) for i in range(2)]
    active += [memory(f"active-{i}") for i in range(13)]
    archive = [memory("oldest", created_at="2010-01-01T00:00:00Z")]
    archive += [memory(f"archive-{i}") for i in range(3)]
    payload = await query_pool(active, archive)
    assert {f["id"] for f in payload["facts"][:2]} == {"recent-0", "recent-1"}
    assert payload["facts"][-1]["id"] == "oldest"
    assert payload["recentGuaranteedCount"] == 2
    assert payload["weightedRandomCount"] == 2
    assert payload["archiveDistantCount"] == 1

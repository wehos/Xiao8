"""Combined local-memory totals and five-choice archive supplementation."""

import hashlib
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


@pytest.mark.asyncio
@pytest.mark.parametrize("active_private", [False, True])
async def test_same_text_with_distinct_ids_and_hashes_is_one_memory(query_pool, active_private):
    active = [memory("active", text="Shared memory", private=active_private)]
    archive = [memory("archive-copy", text="Shared memory")]
    archive += [memory(f"unique-{i}") for i in range(3)]
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 4
    assert payload["returnedCount"] == (3 if active_private else 4)
    assert "archive-copy" not in {item["id"] for item in payload["facts"]}
    assert payload["fallbackReason"] == "insufficient_facts"
    if not active_private:
        selected = next(item for item in payload["facts"] if item["id"] == "active")
        assert selected["hash"] == "hash-active"


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive"])
async def test_same_text_with_distinct_raw_hashes_deduplicates_candidates(query_pool, collection):
    rows = [memory("first", text="Shared memory"), memory("second", text="Shared memory")]
    rows += [memory(f"unique-{i}") for i in range(3)]
    payload = await query_pool(rows if collection == "active" else [], rows if collection == "archive" else [])
    assert payload["totalMemoryCount"] == 4
    assert payload["returnedCount"] == 4
    assert len({item["text"] for item in payload["facts"]}) == 4
    assert payload["fallbackReason"] == "insufficient_facts"


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive"])
@pytest.mark.parametrize("exclude_by", ["raw_hash", "text_hash"])
async def test_hash_alias_exclusion_preserves_original_response_hash(query_pool, collection, exclude_by):
    target = memory("target")
    rows = [target, memory("kept")]
    excluded = target["hash"] if exclude_by == "raw_hash" else hashlib.sha1(target["text"].encode("utf-8")).hexdigest()
    payload = await query_pool(
        rows if collection == "active" else [], rows if collection == "archive" else [],
        exclude_hashes=excluded,
    )
    assert payload["totalMemoryCount"] == 2
    assert [(item["id"], item["hash"]) for item in payload["facts"]] == [("kept", "hash-kept")]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_id,second_id", [(1, "1"), (True, "True"), (1, 1.0), (0, False)])
async def test_total_preserves_legacy_scalar_id_types(query_pool, first_id, second_id):
    active = [memory("first", id=first_id)]
    archive = [memory("second", id=second_id)]
    archive += [memory(f"unique-{i}") for i in range(13)]
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 15


def test_total_deduplicates_repeated_legacy_id_and_counts_falsey_ids():
    active = [{"id": 0}, {"id": False}, {"id": 0.0}]
    archive = [{"id": 0}, {"id": "0"}, {"id": False}]
    count, _, _ = F._memory_identity_stats(active, archive)
    assert count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [0, 1, 5])
async def test_subject_archived_memories_require_explicit_restoration(query_pool, active_count):
    active = [memory(f"active-{i}") for i in range(active_count)]
    archive = [memory(f"subject-{i}", subject_archived_at="2026-08-01T00:00:00Z") for i in range(5)]
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == active_count + 5
    assert payload["returnedCount"] == active_count
    assert payload["archiveFilteredCount"] == 0
    assert not any(item["sourceCollection"] == "facts_archive" for item in payload["facts"])
    for item in archive:
        item.pop("subject_archived_at")
    restored = await query_pool(active, archive)
    assert restored["returnedCount"] == 5
    assert any(item["sourceCollection"] == "facts_archive" for item in restored["facts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_id", [0, False, 0.0])
async def test_falsey_id_excludes_changed_archive_copy(query_pool, fact_id):
    payload = await query_pool(
        [memory("active", id=fact_id)], [memory("archive-copy", id=fact_id)],
    )
    assert payload["totalMemoryCount"] == 1
    assert [item["text"] for item in payload["facts"]] == ["Memory active"]
    assert payload["facts"][0]["id"].startswith("__neko_forge_id_v1__:")
    assert payload["missingIdCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive", "mixed"])
@pytest.mark.parametrize("ids", [[0, False, 0.0, "0", "False"], [1, "1", 1.0, True, "True"]])
async def test_candidate_identity_keeps_scalar_types_and_wire_ids(query_pool, collection, ids):
    rows = [memory(f"row-{i}", id=fact_id) for i, fact_id in enumerate(ids)]
    active, archive = (rows, []) if collection == "active" else ([], rows) if collection == "archive" else (rows[:2], rows[2:])
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 5
    assert payload["returnedCount"] == 5
    assert {item["text"] for item in payload["facts"]} == {f"Memory row-{i}" for i in range(5)}
    assert len({item["id"] for item in payload["facts"]}) == 5
    for i, fact_id in enumerate(ids):
        selected = next(item for item in payload["facts"] if item["text"] == f"Memory row-{i}")
        assert selected["id"].startswith("__neko_forge_id_v1__:")
    assert payload["fallbackReason"] == ""
    assert payload["missingIdCount"] == 0
    assert all(not any(key.startswith("_forge_") for key in item) for item in payload["facts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive"])
@pytest.mark.parametrize("fact_id", [0, False, 0.0, "id:int:0"])
async def test_wire_id_exclusion_remains_compatible(query_pool, collection, fact_id):
    rows = [memory("target", id=fact_id), memory("kept")]
    initial = await query_pool(rows, [])
    target_id = next(item["id"] for item in initial["facts"] if item["text"] == "Memory target")
    payload = await query_pool(
        rows if collection == "active" else [], rows if collection == "archive" else [],
        exclude_fact_ids=target_id,
    )
    assert [item["id"] for item in payload["facts"]] == ["kept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive", "mixed"])
async def test_fallback_id_namespace_does_not_collide_with_literal_ids(query_pool, collection):
    rows = [memory("fallback", id=None, hash="a"), memory("literal", id="hash:a")]
    active, archive = (rows, []) if collection == "active" else ([], rows) if collection == "archive" else (rows[:1], rows[1:])
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 2
    assert payload["returnedCount"] == 2
    assert {item["text"] for item in payload["facts"]} == {"Memory fallback", "Memory literal"}
    assert len({item["id"] for item in payload["facts"]}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [("id",), ("hash",), ("id", "hash")])
async def test_archive_fill_normalizes_missing_identity_fields(query_pool, missing):
    archive = [memory(f"row-{i}") for i in range(5)]
    for item in archive:
        for key in missing:
            item.pop(key)
    payload = await query_pool([], archive)
    assert payload["totalMemoryCount"] == 5
    assert payload["returnedCount"] == 5
    assert payload["fallbackReason"] == ""
    assert all(item["id"] and item["hash"] for item in payload["facts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive", "mixed"])
async def test_wire_ids_are_unique_and_exclude_only_the_selected_memory(query_pool, collection):
    ids = [1, "1", True, "True", None]
    rows = [memory(f"row-{i}", id=fact_id) for i, fact_id in enumerate(ids)]
    active, archive = (rows, []) if collection == "active" else ([], rows) if collection == "archive" else (rows[:2], rows[2:])
    initial = await query_pool(active, archive)
    assert initial["returnedCount"] == 5
    assert len({item["id"] for item in initial["facts"]}) == 5
    for selected in initial["facts"]:
        assert isinstance(selected["id"], str) and 1 <= len(selected["id"]) <= 128
        remaining = await query_pool(active, archive, exclude_fact_ids=selected["id"])
        assert remaining["totalMemoryCount"] == 5
        assert remaining["returnedCount"] == 4
        assert {item["text"] for item in remaining["facts"]} == {
            item["text"] for item in initial["facts"] if item is not selected
        }


@pytest.mark.asyncio
async def test_wire_ids_escape_literal_reserved_prefix_and_remain_stable(query_pool):
    original = memory("numeric", id=1)
    initial = await query_pool([original], [])
    numeric_id = initial["facts"][0]["id"]
    literal = memory("literal", id=numeric_id)
    rows = [original, literal]
    active = await query_pool(rows, [])
    archive = await query_pool([], list(reversed(rows)))
    by_text = lambda payload: {item["text"]: item["id"] for item in payload["facts"]}
    assert by_text(active) == by_text(archive)
    assert len(set(by_text(active).values())) == 2
    assert by_text(active)[original["text"]] == numeric_id
    remaining = await query_pool([], rows, exclude_fact_ids=numeric_id)
    assert [item["text"] for item in remaining["facts"]] == [literal["text"]]


@pytest.mark.asyncio
async def test_wire_id_change_keeps_historical_forged_hash_exclusions(query_pool):
    rows = [memory("numeric", id=1), memory("string", id="1")]
    remaining = await query_pool(rows, [], exclude_hashes="hash-numeric")
    assert [item["hash"] for item in remaining["facts"]] == ["hash-string"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_id,string_id", [(1, "1"), (True, "True"), (0.0, "0.0")])
async def test_historical_forged_id_and_hash_do_not_exclude_a_different_scalar_type(query_pool, legacy_id, string_id):
    rows = [memory("previously-forged", id=legacy_id), memory("unused", id=string_id)]
    payload = await query_pool(
        rows, [], exclude_fact_ids=string_id, exclude_hashes="hash-previously-forged",
    )
    assert [item["text"] for item in payload["facts"]] == ["Memory unused"]
    assert payload["facts"][0]["id"] != string_id


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_id", ["fact_20260907_unique", "__neko_forge_id_v1__:literal", "hash:a", "text:b", "x" * 200, "comma,id", " padded "])
async def test_wire_ids_fit_community_query_and_forge_constraints(query_pool, fact_id):
    initial = await query_pool([memory("row", id=fact_id)], [])
    wire_id = initial["facts"][0]["id"]
    assert 1 <= len(wire_id) <= 128
    assert wire_id == wire_id.strip() and "," not in wire_id
    assert not any(ord(char) < 32 for char in wire_id)
    if fact_id == "fact_20260907_unique":
        assert wire_id == fact_id
    else:
        assert wire_id != fact_id
    remaining = await query_pool([], [memory("row", id=fact_id)], exclude_fact_ids=wire_id)
    assert remaining["returnedCount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["active", "archive", "mixed"])
@pytest.mark.parametrize("different_field", ["subject_kind", "subject_id", "scope"])
@pytest.mark.parametrize("identity_mode", ["same_id", "text_only"])
async def test_scoped_equal_text_keeps_distinct_candidates(query_pool, collection, different_field, identity_mode):
    subject = {"subject_kind": "group_chat", "subject_id": "qq:group-a", "scope": "group_chat:qq:group-a"}
    other = {**subject, different_field: {"subject_kind": "participant", "subject_id": "qq:group-b", "scope": "custom:other"}[different_field]}
    first = memory("first", id="shared-id", text="Shared scoped memory", **subject)
    second = memory("second", id="shared-id", text="Shared scoped memory", **other)
    if identity_mode == "text_only":
        for item in (first, second):
            item.pop("id")
            item.pop("hash")
    rows = [first, second, *[memory(f"unique-{i}") for i in range(3)]]
    private = [memory(f"private-{i}", private=True) for i in range(10)]
    active, archive = (rows, private) if collection == "active" else ([], rows + private) if collection == "archive" else (rows[:1], rows[1:] + private)
    payload = await query_pool(active, archive)
    assert payload["totalMemoryCount"] == 15
    assert payload["returnedCount"] == 5
    assert len({item["id"] for item in payload["facts"]}) == 5
    assert len({item["hash"] for item in payload["facts"]}) == 5
    assert payload["fallbackReason"] == ""
    for selected in (item for item in payload["facts"] if item["text"] == "Shared scoped memory"):
        remaining = await query_pool(active, archive, exclude_fact_ids=selected["id"], exclude_hashes=selected["hash"])
        assert remaining["returnedCount"] == 4
        assert sum(item["text"] == "Shared scoped memory" for item in remaining["facts"]) == 1


@pytest.mark.asyncio
async def test_same_scope_text_alias_deduplicates_active_archive_copy(query_pool):
    subject = {"subject_kind": "group_chat", "subject_id": "qq:group-a", "scope": "group_chat:qq:group-a"}
    first = memory("active", text="Shared memory", **subject)
    second = memory("archive", text="Shared memory", **subject)
    payload = await query_pool([first], [second])
    assert payload["totalMemoryCount"] == payload["returnedCount"] == 1
    assert payload["facts"][0]["hash"] == first["hash"]
    archived = await query_pool([], [first])
    assert archived["facts"][0]["id"] == payload["facts"][0]["id"]


@pytest.mark.asyncio
async def test_scoped_text_alias_does_not_collide_with_legacy_private(query_pool):
    legacy = memory("legacy", id=None, hash=None, text="Same text")
    scoped = dict(legacy, subject_kind="participant", subject_id="qq:user", scope="participant:qq:user")
    payload = await query_pool([legacy], [scoped])
    assert payload["totalMemoryCount"] == payload["returnedCount"] == 2
    assert len({item["id"] for item in payload["facts"]}) == 2
    assert len({item["hash"] for item in payload["facts"]}) == 2

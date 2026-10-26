import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source, Submission

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_separates_cities_and_unknown_runtime() -> None:
    result = snapshot(ROOT)
    bg, ns = result["cities"]
    assert bg["id"] == "belgrade" and ns["id"] == "novi_sad"
    assert bg["items"] is None and bg["counts"] is None
    assert ns["items"] is None and ns["counts"] is None
    assert len([s for s in bg["sources"] if s["platform"] == "telegram"]) >= 14
    assert all(s["runtime_enabled"] is None for s in bg["sources"])
    assert not any(s["url"] == "https://t.me/s/afisha_rs" for s in ns["sources"])
    viber = [s for s in bg["sources"] if s["platform"] == "viber"]
    assert viber and all(s["configured_status"] == "catalog_only" for s in viber)


def test_public_projection_is_read_only_and_does_not_leak_private_material() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for sid, ownership in [("official", "official"), ("private", "unknown")]:
            session.add(Source(id=sid, city="belgrade", name=sid, url="https://example.org/",
                               source_type="news", trust=Decimal("1"), ownership=ownership))
        session.flush()
        for sid, title in [("official", "Public transport notice"), ("private", "PRIVATE_NAME")]:
            raw = RawItem(source_id=sid, external_id=sid, url=f"https://example.org/{sid}",
                          title=title, body="PRIVATE_BODY", published_at=datetime.now(UTC))
            session.add(raw)
            session.flush()
            session.add(EditorialRun(raw_item_id=raw.id,
                outcome={"status": "human_review", "reason": "PRIVATE_NAME / token=SECRET",
                         "draft": {"body": "PRIVATE_DRAFT"},
                         "_package": {"reviews": [{"role": "facts", "verdict": "hold",
                             "score": 40, "reviewer_id": "SECRET", "flags": ["PRIVATE_NAME"]}]}},
                evidence={"secret": "SECRET"}, delivery_status="uncertain"))
        session.add(Submission(kind="tip", payload={"city": "belgrade", "text": "PRIVATE_TIP"}))
        session.commit()
        before = [(r.raw_item_id, r.delivery_status, r.outcome)
                  for r in session.scalars(select(EditorialRun))]
        result = snapshot(ROOT, session)
        serialized = json.dumps(result)
        for secret in ("PRIVATE_NAME", "PRIVATE_BODY", "PRIVATE_DRAFT", "PRIVATE_TIP", "SECRET"):
            assert secret not in serialized
        assert "Public transport notice" in serialized
        bg = result["cities"][0]
        assert bg["counts"] == {"uncertain": 2}
        assert all(item["discovered_by"] is None for item in bg["items"])
        assert all(item["reviews"][0]["verdict"] == "hold" for item in bg["items"])
        assert any(item['url'] == 'https://example.org/official' for item in bg['items'])
        assert all(item['url'] is None for item in bg['items'] if item['source_id'] == 'private')
        assert not session.dirty and not session.new and not session.deleted
        after = [(r.raw_item_id, r.delivery_status, r.outcome)
                 for r in session.scalars(select(EditorialRun))]
        assert before == after


def test_cli_missing_database_does_not_create_queue(tmp_path: Path) -> None:
    database = tmp_path / "does-not-exist.sqlite"
    result = subprocess.run([sys.executable, "-m", "relocant_media.ops.newsroom_export",
                             "--database", str(database), "--output", str(tmp_path / "data.json")],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not database.exists()
    assert not (tmp_path / "data.json").exists()


def test_build_does_not_retimestamp_runtime_or_cross_city() -> None:
    catalog = snapshot(ROOT)
    old = "2026-10-20T10:00:00+00:00"
    runtime = {"schema_version": 1, "mode": "public_read_only", "secret": "SECRET",
               "cities": [{"id": "belgrade", "observed_at": old, "runtime_status": "available",
                           "runtime_source_count": 1, "items": [], "counts": {},
                           "total_events": 0, "secret": "SECRET"}]}
    result = merge(catalog, runtime)
    assert result["cities"][0]["observed_at"] == old
    assert result["cities"][1]["items"] is None
    assert "SECRET" not in json.dumps(result)


def test_after_cycle_records_missing_credential_without_network(
    tmp_path: Path, monkeypatch,
) -> None:
    from relocant_media.ops import newsroom_after_cycle

    monkeypatch.setenv("DATABASE_URL", "sqlite:////canonical.sqlite")
    monkeypatch.delenv("NEWSROOM_GITHUB_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["after-cycle", "--output", str(tmp_path)])
    import pytest
    with pytest.raises(SystemExit):
        newsroom_after_cycle.main()
    status = json.loads((tmp_path / "newsroom-refresh.json").read_text())
    assert status["export"] == "completed"
    assert status["upload"] == "credential_not_configured"
    assert len(calls) == 1


def test_runtime_merge_drops_nested_unapproved_fields() -> None:
    runtime = snapshot(ROOT)
    runtime["cities"][0].update(items=[{
        "raw_item_ids": [1], "state": "review", "secret": "SECRET",
        "reviews": [{"role": "facts", "verdict": "hold", "prompt": "SECRET"}],
    }], last_cycle={"status": "FAILED", "payload": "SECRET", "jev": {
        "mode": "shadow_only", "token": "SECRET", "signals": [{
            "raw_item_id": 1, "prompt": "SECRET", "practical_value_probability": 0.2,
        }],
    }})
    result = merge(snapshot(ROOT), runtime)
    assert "SECRET" not in json.dumps(result)
    assert result["cities"][0]["items"][0]["reviews"][0]["verdict"] == "hold"


def test_upload_uses_sha_and_allowlist_without_transmitting_private_fields(
    tmp_path: Path, monkeypatch,
) -> None:
    import httpx

    from relocant_media.ops import newsroom_publish

    payload = snapshot(ROOT)
    payload["private_payload"] = "PRIVATE_SECRET"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "main-sha"}})
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "old-sha"})
        return httpx.Response(200, json={})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handler)))
    newsroom_publish.publish(path, "test-only-token")
    sent = json.loads(requests[-1].content)
    import base64
    assert sent["sha"] == "old-sha" and sent["branch"] == "newsroom-data"
    assert b"PRIVATE_SECRET" not in base64.b64decode(sent["content"])


def test_server_cycle_hook_remains_executable() -> None:
    assert (ROOT / "deploy/server/run-cycle.sh").stat().st_mode & 0o111


def test_pages_workflows_use_matching_artifacts_unique_per_run_and_attempt() -> None:
    import yaml

    for filename in ("newsroom-pages.yml", "editorial-actions.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
        upload = next(step for step in steps
                      if step.get("uses", "").startswith("actions/upload-pages-artifact@"))
        deploy = next(step for step in steps
                      if step.get("uses", "").startswith("actions/deploy-pages@"))
        name = upload["with"]["name"]
        assert "${{ github.run_id }}" in name
        assert "${{ github.run_attempt }}" in name
        assert deploy["with"]["artifact_name"] == name


def test_archive_export_does_not_refresh_observation_time(tmp_path: Path) -> None:
    from relocant_media.storage.sqlite_schema import initialize_sqlite
    database = tmp_path / 'archive.sqlite3'
    initialize_sqlite(database)
    engine = create_engine(f'sqlite:///{database}')
    with Session(engine) as session:
        session.add(Source(id='official', city='belgrade', name='Official',
                           url='https://example.org', source_type='news', trust=Decimal('1')))
        session.commit()
    engine.dispose()
    output = tmp_path / 'public.json'
    observed = '2026-01-01T00:00:00+00:00'
    result = subprocess.run([sys.executable, '-m', 'relocant_media.ops.newsroom_export',
        '--database', str(database), '--output', str(output), '--observed-at', observed],
        capture_output=True, text=True)
    assert result.returncode == 0
    bg, ns = json.loads(output.read_text())['cities']
    assert bg['observed_at'] == observed
    assert ns['observed_at'] is None and ns['runtime_status'] == 'unavailable'


def test_legacy_empty_city_is_not_a_running_newsroom() -> None:
    runtime = snapshot(ROOT)
    runtime['cities'][1].update(runtime_status='available', counts={}, total_events=0,
                                items=[], observed_at='2026-10-24T09:24:05+00:00')
    city = merge(snapshot(ROOT), runtime)['cities'][1]
    assert city['runtime_status'] == 'unavailable'
    assert city['observed_at'] is None and city['counts'] is None


def test_coverage_is_allowlisted_city_scoped_and_survives_rebuild() -> None:
    from relocant_media.ops.public_observations import public_coverage

    runtime = snapshot(ROOT)
    bg = runtime['cities'][0]
    bg.update(runtime_status='available', runtime_source_count=1)
    audit = {'city': 'belgrade', 'observed_at': '2026-10-26T12:00:00+00:00',
             'scope': 'editorial_discovery', 'private': 'PRIVATE_TEXT', 'sources': [
                 {'channel': 'Serbia', 'status': 'dated_messages', 'leads': 2,
                  'body': 'PRIVATE_TEXT', 'url': 'https://private.example/SECRET'},
                 {'channel': 'SECRET', 'status': 'access_error'},
             ]}
    bg['last_cycle'] = {'messenger_coverage': public_coverage(audit, bg['sources'])}
    result = merge(snapshot(ROOT), runtime)
    coverage = result['cities'][0]['last_cycle']['messenger_coverage']
    assert coverage['interval_coverage'] == 'partial'
    assert coverage['sources'][0]['leads'] == 2
    assert len(coverage['sources']) == 1
    assert 'PRIVATE_TEXT' not in json.dumps(result) and 'SECRET' not in json.dumps(result)
    assert 'last_cycle' not in result['cities'][1]
    audit['scope'] = 'submissions_only'
    safe = public_coverage(audit, bg['sources'])
    assert public_coverage(safe, bg['sources'])['interval_coverage'] == 'not_checked'


def test_malformed_coverage_does_not_abort_checkpoint_projection() -> None:
    from relocant_media.ops.public_observations import public_coverage

    sources = snapshot(ROOT)['cities'][0]['sources']
    for rows in (None, 123, {'channel': 'Serbia'}):
        assert public_coverage({'city': 'belgrade', 'sources': rows}, sources) is None
    safe = public_coverage({'city': 'belgrade', 'sources': [
        {'channel': 'Serbia', 'status': [], 'leads': 'PRIVATE_TEXT'},
        {'source_id': ['SECRET'], 'status': 'access_error'},
    ]}, sources)
    assert len(safe['sources']) == 1
    assert safe['sources'][0]['status'] == 'not_checked'
    assert safe['sources'][0]['leads'] is None

import hashlib
import io
import json
import re

import pytest

from csv_cleaner import web
from csv_cleaner.engine import MAX_INPUT_BYTES, parse_csv


def token(response):
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.data[:500]
    return match.group(1).decode()


def upload(client, content=b"id,name\n001,  Alex  \n", filename="customers.csv"):
    csrf = token(client.get("/"))
    return client.post(
        "/upload",
        data={"csrf_token": csrf, "file": (io.BytesIO(content), filename)},
        follow_redirects=True,
    )


def preview(client, form=None):
    csrf = token(client.get("/configure"))
    return client.post("/preview", data={"csrf_token": csrf, **(form or {})}, follow_redirects=True)


def result_id(response):
    assert response.status_code == 200, response.data[:1000]
    return response.request.path.rsplit("/", 1)[-1]


def test_full_workflow_snapshot_downloads_and_original_preservation(client, app, sample_bytes):
    page = upload(client, sample_bytes)
    assert page.status_code == 200
    assert b"Nothing has been changed" in page.data
    assert b" checked" not in page.data
    page = preview(
        client,
        {
            "trim": ["1", "2", "4"],
            "remove_empty": "on",
            "fill": "4",
            "replacement_4": "Unknown",
            "remove_duplicates": "on",
        },
    )
    identity = result_id(page)
    csv_path = f"/download/{identity}/cleaned.csv"
    download = client.get(csv_path)
    summary = client.get(f"/download/{identity}/summary.json")
    assert download.status_code == summary.status_code == 200
    assert client.get(csv_path).data == download.data
    assert download.headers["X-Result-ID"] == summary.headers["X-Result-ID"] == identity
    assert "attachment" in download.headers["Content-Disposition"]
    assert download.headers["Cache-Control"] == "no-store"
    report = json.loads(summary.data)
    assert report["output_rows"] == 6
    assert report["output_sha256"] == hashlib.sha256(download.data).hexdigest()
    with client.session_transaction() as cookie:
        key = cookie["job_id"]
        assert set(cookie) <= {"job_id", "csrf_token"}
    job = app.extensions["jobs"].get(key)
    assert job.dataset.original_bytes == sample_bytes
    assert job.result.csv_bytes == download.data
    assert job.result.summary_bytes == summary.data


def test_old_preview_invalidated_by_new_preview(client):
    upload(client)
    old = result_id(preview(client))
    latest = result_id(preview(client, {"trim": "1"}))
    assert old != latest
    assert client.get(f"/preview/{old}").status_code == 410
    assert client.get(f"/download/{old}/cleaned.csv").status_code == 410
    assert b"001,Alex\r\n" in client.get(f"/download/{latest}/cleaned.csv").data


def test_preview_uses_one_job_snapshot_when_session_is_replaced(client, app, monkeypatch):
    upload(client, b"id,name\n1,old one\n2,old two\n")
    identity = result_id(preview(client))
    with client.session_transaction() as cookie:
        key = cookie["job_id"]

    jobs = app.extensions["jobs"]
    replacement = parse_csv(b"id,name\nnew,MIXED_SENTINEL_42\n")
    original_current_job = web.current_job
    calls = 0

    def replace_after_first_lookup():
        nonlocal calls
        calls += 1
        job = original_current_job()
        if calls == 1:
            jobs.put(key, replacement)
        return job

    monkeypatch.setattr(web, "current_job", replace_after_first_lookup)
    response = client.get(f"/preview/{identity}")

    assert response.status_code == 200
    assert calls == 1
    assert b"old one" in response.data and b"old two" in response.data
    assert b"MIXED_SENTINEL_42" not in response.data


def test_download_never_recomputes_cleaning(client, monkeypatch):
    upload(client)
    identity = result_id(preview(client))

    def unexpected(*args):
        pytest.fail("Download recomputed cleaning")

    monkeypatch.setattr("csv_cleaner.web.clean", unexpected)
    assert client.get(f"/download/{identity}/cleaned.csv").status_code == 200
    assert client.get(f"/download/{identity}/summary.json").status_code == 200


def test_other_sessions_cannot_read_preview_or_download(client, app):
    upload(client)
    identity = result_id(preview(client))
    other = app.test_client()
    assert other.get(f"/preview/{identity}").status_code == 410
    assert other.get(f"/download/{identity}/cleaned.csv").status_code == 410


def test_reset_invalidates_result_and_releases_session(client, app):
    upload(client)
    identity = result_id(preview(client))
    csrf = token(client.get(f"/preview/{identity}"))
    assert client.post("/reset", data={"csrf_token": csrf}).status_code == 303
    assert client.get(f"/download/{identity}/summary.json").status_code == 410
    assert not app.extensions["jobs"]._jobs


def test_successful_reupload_invalidates_result(client):
    upload(client)
    identity = result_id(preview(client))
    assert upload(client, b"x\nnew\n").status_code == 200
    assert client.get(f"/download/{identity}/cleaned.csv").status_code == 410


def test_invalid_upload_does_not_destroy_existing_original_or_snapshot(client):
    upload(client)
    identity = result_id(preview(client))
    assert upload(client, b"a,b\nx\n").status_code == 400
    assert client.get(f"/download/{identity}/cleaned.csv").status_code == 200


@pytest.mark.parametrize(
    "form",
    [
        {"trim": "not-a-column"},
        {"trim": "-1"},
        {"trim": "99"},
        {"fill": "1", "replacement_1": "  "},
        {"fill": "99", "replacement_99": "x"},
    ],
)
def test_invalid_options_have_no_partial_result(client, form):
    upload(client)
    response = preview(client, form)
    assert response.status_code == 400
    assert b"Check your choices" in response.data
    assert b"Download CSV" not in response.data


def test_inactive_replacement_text_is_ignored(client):
    upload(client, b"a,b\nx,\n")
    identity = result_id(preview(client, {"replacement_1": "Do not use"}))
    assert client.get(f"/download/{identity}/cleaned.csv").data == b"a,b\r\nx,\r\n"


def test_csrf_required_for_mutation(client):
    response = client.post("/upload", data={"file": (io.BytesIO(b"a\nx\n"), "x.csv")})
    assert response.status_code == 400
    assert client.post("/reset").status_code == 400


def test_untrusted_content_escaped_and_filenames_never_used_as_paths(client):
    page = upload(
        client, b"<script>alert(1)</script>,name\n<img src=x onerror=alert(2)>,x\n", "../../x.csv"
    )
    assert page.status_code == 200
    assert b"<script>" not in page.data and b"<img src=x" not in page.data
    assert b"&lt;script&gt;" in page.data
    assert b"frame-ancestors 'none'" in page.headers["Content-Security-Policy"].encode()
    identity = result_id(preview(client))
    download = client.get(f"/download/{identity}/cleaned.csv")
    assert b"<script>" in download.data
    assert download.headers["Content-Disposition"] == "attachment; filename=cleaned.csv"


def test_pagination_all_records_and_operations_accessible(client):
    data = ("id,name\n" + "".join(f"{i},  person {i}  \n" for i in range(1, 122))).encode()
    upload(client, data)
    page = client.get("/configure?page=3")
    assert b"101" in page.data and b"121" in page.data
    identity = result_id(preview(client, {"trim": "1"}))
    response = client.get(f"/preview/{identity}?page=3&changes_page=3")
    assert b'"person 121"' in response.data  # Literal quotes distinguish the proposed value.
    assert b"person 121" in response.data
    assert b"101\xe2\x80\x93121 of 121" in response.data
    assert client.get(f"/preview/{identity}?page=bad").status_code == 200


def test_removed_row_pagination(client):
    upload(client, b"a\n" + b"same\n" * 122)
    identity = result_id(preview(client, {"remove_duplicates": "on"}))
    page = client.get(f"/preview/{identity}?removed_page=3")
    assert b"122" in page.data and b"Kept source #1" in page.data


def test_session_expiry(client, app):
    upload(client)
    identity = result_id(preview(client))
    jobs = app.extensions["jobs"]
    now = jobs.clock()
    jobs.clock = lambda: now + jobs.ttl + 1
    assert client.get(f"/download/{identity}/cleaned.csv").status_code == 410
    assert not jobs._jobs


def test_capacity_is_bounded_without_evicting_existing_users(app):
    clients = [app.test_client() for _ in range(5)]
    for browser in clients[:4]:
        assert upload(browser).status_code == 200
    assert upload(clients[4]).status_code == 503
    assert clients[0].get("/configure").status_code == 200


def test_oversized_file_and_request_rejected(client):
    assert upload(client, b"a\n" + b"x" * MAX_INPUT_BYTES).status_code == 400
    assert upload(client, b"x" * (MAX_INPUT_BYTES + 300_000)).status_code == 413


def test_sample_health_and_packaged_sample_agree(client, sample_bytes):
    assert client.get("/healthz").json == {"status": "ok"}
    assert client.get("/sample.csv").data == sample_bytes
    assert client.get("/missing").status_code == 404

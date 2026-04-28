from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from inbox_da import db
from inbox_da.config import Settings
from inbox_da.synthetic import seed_database
from inbox_da.web import create_app


def test_web_dashboard_process_review_and_costs(tmp_path: Path) -> None:
    database = tmp_path / "web.sqlite"
    attachments = tmp_path / "attachments"
    settings = Settings(
        database_path=database,
        attachments_dir=attachments,
        attachment_text_min_chars=5,
    )
    with closing(db.connect(database)) as conn:
        db.init_db(conn)
        seed_database(conn, count=2, attachments_dir=attachments)

    app = create_app(settings, process_offline_demo=True)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Inbox Monitor" in response.text
        assert "Invoice INV-0001" in response.text

        response = client.get("/emails/1")
        assert response.status_code == 200
        assert "Structured Extraction" in response.text

        response = client.post("/emails/1/process")
        assert response.status_code == 200
        assert "verified" in response.text
        assert "Process with Gemini LLM" in response.text
        assert "Candidate Review" in response.text
        assert "Accept extractor_a" in response.text

        response = client.post("/emails/1/process-offline")
        assert response.status_code == 200
        assert "verified" in response.text

        response = client.post(
            "/review/1/select-candidate",
            data={
                "run_id": "1",
                "reviewer": "tester",
                "note": "Prefer candidate A.",
            },
        )
        assert response.status_code == 200
        assert "Candidate selection applied to the final structured extraction." in response.text
        assert "select_candidate" in response.text

        response = client.post(
            "/review/2/select-candidate",
            data={
                "run_id": "1",
                "reviewer": "tester",
                "note": "Wrong email.",
            },
        )
        assert response.status_code == 400

        response = client.post(
            "/review/1/select-fields",
            data={
                "reviewer": "tester",
                "field_document_type": "left",
                "field_amount": "right",
                "note": "Mixed field choice.",
            },
        )
        assert response.status_code == 200
        assert "Field choices applied to the final structured extraction." in response.text
        assert "Mixed field choice." in response.text

        response = client.post(
            "/review/1/approve",
            data={"reviewer": "tester", "note": "Looks correct."},
        )
        assert response.status_code == 200
        assert "Review approved and applied to the final structured extraction." in response.text
        assert "Looks correct." in response.text

        response = client.post(
            "/review/1/override",
            data={
                "reviewer": "tester",
                "amount": "1037.25",
                "currency": "USD",
                "note": "Manual amount confirmation.",
            },
        )
        assert response.status_code == 200
        assert "Manual override applied to the final structured extraction." in response.text
        assert "Manual amount confirmation." in response.text

        response = client.get("/review")
        assert response.status_code == 200
        assert "Review Queue" in response.text

        response = client.get("/reports/costs")
        assert response.status_code == 200
        assert "Cost Report" in response.text

    with closing(db.connect(database)) as conn:
        rows = db.list_review_actions(conn, 1)
        assert len(rows) == 4
        assert rows[0]["action"] == "override"
        latest = db.latest_structured_extraction(conn, 1)
        assert latest is not None
        assert latest["amount"] == 1037.25
        assert latest["verification_status"] == "verified"

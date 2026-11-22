import csv
from decimal import Decimal
from io import BytesIO, StringIO
from zipfile import ZipFile

from app.evidence import SourceRow
from app.manifest import digest
from app.modules.m2_calibration.crossing import Crossing
from app.modules.m8_identity.fingerprint import fingerprint_match, fingerprint_of
from app.modules.m8_identity.plates import normalize_plate, plate_distance, wellformed
from app.modules.m8_identity.service import identify_vehicles
from app.sources import Source
from app.values import instant


def profile(result, plate):
    return next(item for item in result["vehicles"]["profiles"] if item["plate"] == plate)


def pair(result, left, right):
    return next(item for item in result["vehicles"]["pairs"] if (item["left"], item["right"]) == (left, right))


def test_plate_normalization_strips_separators_case_and_fullwidth():
    assert normalize_plate("粤B·07612") == "粤B07612"
    assert normalize_plate("粤ｂ－０７６１２") == "粤B07612"
    assert normalize_plate("粤B 076 12") == "粤B07612"
    assert wellformed(normalize_plate("粤B·07612"))
    assert not wellformed(normalize_plate("粤B·0761"))


def test_plate_distance_covers_common_ocr_confusions():
    assert plate_distance("粤B07612", "粤B07612") == 0
    assert plate_distance("粤B07612", "粤B0761Z") == 1  # 错字
    assert plate_distance("粤B07612", "粤B0761") == 1  # 漏字
    assert plate_distance("粤B0761", "粤B07612") == 1  # 多字
    assert plate_distance("粤B88204", "粤B88240") == 1  # 相邻换位
    assert plate_distance("粤B07612", "粤S07612") == 1
    assert plate_distance("粤B07612", "粤B07622") == 1
    assert plate_distance("粤B07612", "粤A0761Z") == 2
    assert plate_distance("粤B07612", "粤B076") == 2


def test_fingerprint_requires_two_confirmed_axles_without_gaps():
    axes = lambda gaps: [{"gap_m": gap} for gap in gaps]
    assert fingerprint_of(axes([None, Decimal("1.3")])) == (2, (Decimal("1.3"),))
    assert fingerprint_of(axes([None])) is None
    assert fingerprint_of(axes([None, None])) is None
    assert fingerprint_match((2, (Decimal("1.30"),)), (2, (Decimal("1.34"),)))
    assert not fingerprint_match((2, (Decimal("1.30"),)), (2, (Decimal("1.36"),)))
    assert not fingerprint_match((2, (Decimal("1.3"),)), (3, (Decimal("1.3"), Decimal("3.4"))))


# TRAP-26：漏字车牌与正牌轴距序列一致，归并为同一车辆档案
def test_missing_character_plate_merges_with_matching_fingerprint(repeat_result):
    merged = profile(repeat_result, "粤B·07612")
    assert [member["pass_id"] for member in merged["members"]] == ["0101", "0102"]
    assert merged["vehicle_key"] == f"VH-{digest('粤B0761'.encode())[:10]}"
    assert [entry["code"] for entry in merged["evidence"]] == ["MERGE_FUZZY"]
    assert "1.3 m" in merged["evidence"][0]["detail"]
    assert merged["totals"] == {"passes": 2, "axles": 4, "wheel_kg": "30400.00", "review": 0}
    assert [entry["code"] for entry in merged["uncertainties"]] == ["PLATE_MALFORMED"]
    verdict = pair(repeat_result, "0101", "0102")
    assert (verdict["verdict"], verdict["code"]) == ("merged", "MERGE_FUZZY")


# TRAP-27：换位车牌外观相近，但轴数不同，拒绝归并，重量历史互不混入
def test_transposed_lookalike_plates_stay_separate(repeat_result):
    first, second = profile(repeat_result, "粤B·88204"), profile(repeat_result, "粤B·88240")
    assert [m["pass_id"] for m in first["members"]] == ["0103"]
    assert [m["pass_id"] for m in second["members"]] == ["0104"]
    assert (first["totals"]["axles"], second["totals"]["axles"]) == (2, 3)
    assert (first["totals"]["wheel_kg"], second["totals"]["wheel_kg"]) == ("24200.00", "24000.00")
    verdict = pair(repeat_result, "0103", "0104")
    assert (verdict["verdict"], verdict["code"]) == ("rejected", "PAIR_REJECTED")
    assert "2 轴" in verdict["detail"] and "3 轴" in verdict["detail"]
    assert [u["code"] for u in first["uncertainties"]] == ["PAIR_REJECTED"]


# 载重随货载变化，不作归并证据：差 10000 kg 的同车归并，差 200 kg 的相近两车不并
def test_weight_never_supplies_merge_evidence(repeat_result):
    merged = profile(repeat_result, "粤B·07612")
    assert {item["wheel_kg"] for item in merged["history"]} == {"20200.00", "10200.00"}
    rejected = pair(repeat_result, "0103", "0104")
    assert rejected["verdict"] == "rejected"


def test_exact_plate_passes_merge_and_late_ticket_attaches(repeat_result):
    merged = profile(repeat_result, "粤S·15982")
    assert [member["pass_id"] for member in merged["members"]] == ["0105", "0106"]
    assert [entry["code"] for entry in merged["evidence"]] == ["MERGE_EXACT"]
    late = next(entry for entry in merged["uncertainties"] if entry["code"] == "LATE_TICKET_ATTACHED")
    assert "WB-0106" in late["detail"] and late["refs"] == ["repeat-tickets.csv:7"]
    visit = next(item for item in merged["history"] if item["pass_id"] == "0106")
    assert (visit["ticket"], visit["status"]) == (None, "review")
    assert "TICKET_TIME" in visit["codes"]
    assert merged["status"] == "review"


# TRAP-29：一侧轮质态异常导致指纹不可测，疑似同车暂缓归并，留给下一班补证
def test_incomplete_evidence_defers_suspected_duplicate(repeat_result):
    verdict = pair(repeat_result, "0107", "0108")
    assert (verdict["verdict"], verdict["code"]) == ("pending", "PAIR_PENDING")
    assert "0108" in verdict["detail"]
    complete = profile(repeat_result, "粤A·58041")
    broken = profile(repeat_result, "粤A·5804")
    assert [m["pass_id"] for m in complete["members"]] == ["0107"]
    assert [m["pass_id"] for m in broken["members"]] == ["0108"]
    codes = [entry["code"] for entry in broken["uncertainties"]]
    assert codes == ["PAIR_PENDING", "PLATE_MALFORMED", "EVIDENCE_INCOMPLETE"]


def test_handover_summary_counts(repeat_result):
    summary = repeat_result["vehicles"]["summary"]
    assert summary == {
        "passes": 8, "profiles": 6, "merged": 2, "singletons": 4,
        "pending": 1, "rejected": 1, "review": 2,
    }


def test_profile_history_keeps_source_refs_for_every_number(repeat_result):
    merged = profile(repeat_result, "粤B·07612")
    first = next(item for item in merged["history"] if item["pass_id"] == "0101")
    assert first["refs"][0] == "repeat-passes.csv:2"
    assert "repeat-tickets.csv:2" in first["refs"]
    assert any(ref.startswith("repeat-triggers.csv:") for ref in first["refs"])


def test_dirty_and_clean_samples_have_no_false_merges(dirty_result, clean_result):
    for result in (dirty_result, clean_result):
        vehicles = result["vehicles"]
        assert vehicles["pairs"] == []
        assert vehicles["summary"]["merged"] == 0
        assert all(len(item["members"]) == 1 for item in vehicles["profiles"])
    ticket_plate = profile(dirty_result, "粤B·88204")
    assert "PLATE_VARIANTS" in [entry["code"] for entry in ticket_plate["uncertainties"]]


def commit(client, sample="repeat"):
    return client.post("/api/batches", json={"sample": sample})


def vehicle_keys(payload):
    return {item["vehicle_key"] for item in payload["profiles"]}


def test_repeat_batch_commit_rerun_never_adds_profiles(client):
    first = commit(client).json()["manifest"]
    run_id = first["run_id"]
    assert first["rules"] == "axle-1.1.0"
    before = client.get(f"/api/vehicles?run_id={run_id}").json()["data"]
    assert before["summary"]["profiles"] == 6
    second = commit(client)
    assert (second.status_code, second.json()["reused"]) == (200, True)
    assert second.json()["manifest"]["run_id"] == run_id
    replayed = client.post(f"/api/batches/{run_id}/rerun")
    assert replayed.json()["replayed"] is True
    after = client.get(f"/api/vehicles?run_id={run_id}").json()["data"]
    assert after == before
    assert len(client.get("/api/batches").json()["batches"]) == 1


def test_late_arriving_ticket_joins_existing_profile_without_new_keys(client, settings):
    first = commit(client).json()["manifest"]
    keys_before = vehicle_keys(client.get(f"/api/vehicles?run_id={first['run_id']}").json()["data"])
    path = settings.source_dir / "repeat-tickets.csv"
    late = "WB-0101,0101,2,粤B·07612,2026-11-21T22:25:00.000+08:00,20200,kg\n"
    path.write_bytes(path.read_bytes() + late.encode())
    second = commit(client)
    assert second.status_code == 201
    run_id = second.json()["manifest"]["run_id"]
    assert run_id != first["run_id"]
    data = client.get(f"/api/vehicles?run_id={run_id}").json()["data"]
    assert vehicle_keys(data) == keys_before
    assert data["summary"]["profiles"] == 6
    merged = next(item for item in data["profiles"] if item["plate"] == "粤B·07612")
    assert [member["pass_id"] for member in merged["members"]] == ["0101", "0102"]
    assert "LATE_TICKET_ATTACHED" in [entry["code"] for entry in merged["uncertainties"]]
    visit = next(item for item in merged["history"] if item["pass_id"] == "0101")
    assert "TICKET_TIME" in visit["codes"]


def test_export_contains_vehicle_book_and_stays_byte_identical(client):
    run_id = commit(client).json()["manifest"]["run_id"]
    client.post(f"/api/batches/{run_id}/rerun")
    first = client.get(f"/api/batches/{run_id}/export").content
    client.post(f"/api/batches/{run_id}/rerun")
    assert client.get(f"/api/batches/{run_id}/export").content == first
    with ZipFile(BytesIO(first)) as bundle:
        assert {"车辆档案.csv", "车辆核对.csv"} <= set(bundle.namelist())
        book = {row["主车牌"]: row for row in csv.DictReader(
            StringIO(bundle.read("车辆档案.csv").decode("utf-8-sig")))}
        assert book["粤B·07612"]["成员车次"] == "0101 / 0102"
        assert book["粤B·07612"]["归并依据"].startswith("MERGE_FUZZY")
        assert book["粤B·88204"]["状态"] == "已核平"
        assert book["粤S·15982"]["状态"] == "待复秤"
        pairs = list(csv.DictReader(StringIO(bundle.read("车辆核对.csv").decode("utf-8-sig"))))
        verdicts = {(row["左车次"], row["右车次"]): row["判定"] for row in pairs}
        assert verdicts[("0103", "0104")] == "已排除"
        assert verdicts[("0107", "0108")] == "待补证"


def test_vehicles_endpoint_preview_matches_archive(client):
    preview = client.get("/api/vehicles?sample=repeat").json()
    assert preview["run_id"] is None
    assert preview["data"]["summary"]["merged"] == 2
    run_id = commit(client).json()["manifest"]["run_id"]
    archived = client.get(f"/api/vehicles?run_id={run_id}").json()["data"]
    assert archived == preview["data"]
    missing = client.get("/api/vehicles?run_id=absent")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "RUN_UNKNOWN")


def test_same_plate_conflicting_fingerprint_flagged_as_possible_clone():
    header = "磅单号,车次,版次,车牌,签发时间,总重,单位"
    lines = (
        "WB-1,0201,1,粤B·07612,2026-11-21T22:00:05.000+08:00,10000,kg\n"
        "WB-2,0202,1,粤B·07612,2026-11-21T23:00:05.000+08:00,10000,kg"
    )
    tickets = Source("tickets.csv", f"{header}\n{lines}\n".encode())
    passes = {
        "0201": Crossing("0201", "粤B·07612", instant("2026-11-21T22:00:00.000+08:00"),
                         instant("2026-11-21T22:00:03.000+08:00"), Decimal("10"), SourceRow("passes.csv", 2, 2, ())),
        "0202": Crossing("0202", "粤B·07612", instant("2026-11-21T23:00:00.000+08:00"),
                         instant("2026-11-21T23:00:03.000+08:00"), Decimal("10"), SourceRow("passes.csv", 3, 3, ())),
    }
    axes = [
        {"id": "0201/A01", "pass_id": "0201", "group": "0201/G01", "gap_m": None},
        {"id": "0201/A02", "pass_id": "0201", "group": "0201/G01", "gap_m": Decimal("1.30")},
        {"id": "0202/A01", "pass_id": "0202", "group": "0202/G01", "gap_m": None},
        {"id": "0202/A02", "pass_id": "0202", "group": "0202/G01", "gap_m": Decimal("1.80")},
    ]
    rows = [{
        "pass_id": pass_id, "ticket": None, "wheel_kg": Decimal("10000.00"), "difference_kg": None,
        "status": "matched", "codes": [], "pass_ref": f"passes.csv:{line}",
        "ticket_refs": (), "wheel_refs": (), "review_refs": (),
    } for pass_id, line in (("0201", 2), ("0202", 3))]
    result = identify_vehicles(passes, axes, rows, [], tickets)
    [merged] = result["profiles"]
    assert [member["pass_id"] for member in merged["members"]] == ["0201", "0202"]
    assert [entry["code"] for entry in merged["evidence"]] == ["MERGE_EXACT"]
    conflict = next(entry for entry in merged["uncertainties"] if entry["code"] == "FINGERPRINT_CONFLICT")
    assert "套牌" in conflict["detail"]

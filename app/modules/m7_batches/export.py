import csv
import json
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from app.reasons import REASONS


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def csv_bytes(header, rows):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def issue_rows(results):
    families = (
        ("触发隔离", results["quarantine"]),
        ("触发重复", results["debounce"]["duplicates"]),
        ("磅单隔离", results["reconcile"]["ticket_issues"]),
    )
    for kind, issues in families:
        for issue in issues:
            source = issue["source"]
            yield (kind, safe_cell(issue["pass_id"]), issue["code"], issue["message"],
                   source["file"], source["line"], source["end_line"],
                   " / ".join(issue["related"]), safe_cell(issue["kept_id"]),
                   json.dumps(source["cells"], ensure_ascii=False))


def ticket_rows(results):
    for row in results["reconcile"]["rows"]:
        ticket = row["ticket"]
        yield (safe_cell(row["pass_id"]), safe_cell(row["plate"]), safe_cell(ticket["id"]) if ticket else "",
               ticket["revision"] if ticket else "", ticket["kg"] if ticket else "",
               row["wheel_kg"], row["difference_kg"], "勾稽通过" if row["status"] == "matched" else "待复秤",
               " / ".join(row["codes"]), " / ".join(REASONS[code] for code in row["codes"]),
               row["pass_ref"], " / ".join(row["ticket_refs"]), " / ".join(row["wheel_refs"]),
               " / ".join(row["review_refs"]))


def axle_rows(results):
    grades = {row["id"]: row for row in results["balance"]["rows"]}
    for axis in results["axles"]["axes"]:
        left, right = axis["left"], axis["right"]
        grade = grades[axis["id"]]
        level = {"pass": "正常", "warn": "偏载", "severe": "严重"}[grade["level"]]
        yield (safe_cell(axis["id"]), safe_cell(axis["group"]), axis["time"], axis["gap_m"], axis["skew_ms"],
               left["kg"], right["kg"], axis["total_kg"], grade["percent"], level,
               left["trigger"]["source"]["ref"], right["trigger"]["source"]["ref"], left["cert_ref"], right["cert_ref"])


def vehicle_rows(results):
    for profile in results["vehicles"]["profiles"]:
        totals = profile["totals"]
        yield (safe_cell(profile["vehicle_key"]), safe_cell(profile["plate"]),
               " / ".join(member["pass_id"] for member in profile["members"]),
               " / ".join(item["plate"] for item in profile["plates"]),
               totals["passes"], totals["axles"], totals["wheel_kg"],
               "待复秤" if profile["status"] == "review" else "已核平",
               " / ".join(f"{item['code']}：{item['detail']}" for item in profile["evidence"]),
               " / ".join(f"{item['code']}：{item['detail']}" for item in profile["uncertainties"]))


def pair_rows(results):
    verdicts = {"merged": "已归并", "pending": "待补证", "rejected": "已排除"}
    for pair in results["vehicles"]["pairs"]:
        yield (safe_cell(pair["left"]), safe_cell(pair["right"]), verdicts[pair["verdict"]],
               pair["code"], safe_cell(pair["detail"]), " / ".join(pair["refs"]))


def export_bundle(manifest, results):
    files = {
        "运行清单.json": json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "触发复核.csv": csv_bytes(
            ("类别", "车次", "原因码", "说明", "文件", "起始行", "结束行", "关联证据", "保留记录", "原始字段"),
            issue_rows(results),
        ),
        "整车勾稽.csv": csv_bytes(
            ("车次", "车牌", "磅单号", "版次", "静态总重kg", "有效轮载kg", "差额kg", "状态", "原因码", "说明",
             "车次档案", "磅单证据", "轮载证据", "复秤证据"), ticket_rows(results),
        ),
        "轴组轮载.csv": csv_bytes(
            ("轴号", "轴组", "轴时刻UTC", "轴距m", "右减左ms", "左kg", "右kg", "轴重kg", "偏载率%", "判级",
             "左轮证据", "右轮证据", "左标定证书", "右标定证书"), axle_rows(results),
        ),
        "车辆档案.csv": csv_bytes(
            ("档案键", "主车牌", "成员车次", "车牌版本", "过磅次数", "确认车轴", "累计轮载kg", "状态",
             "归并依据", "不确定项"), vehicle_rows(results),
        ),
        "车辆核对.csv": csv_bytes(
            ("左车次", "右车次", "判定", "依据码", "说明", "证据行"), pair_rows(results),
        ),
    }
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in files.items():
            # DOS 起始时刻使同一归档反复下载得到同一字节序列。
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    return output.getvalue()

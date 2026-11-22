from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from app.config import PLATE_CANDIDATE_DISTANCE, TICKET_GRACE_MIN
from app.manifest import digest
from app.modules.m6_reconcile.tickets import ticket_book
from app.modules.m8_identity.fingerprint import describe, fingerprint_match, fingerprint_of
from app.modules.m8_identity.plates import normalize_plate, plate_distance, wellformed
from app.reasons import REASONS

VERDICTS = {"MERGE_EXACT": "merged", "MERGE_FUZZY": "merged", "PAIR_PENDING": "pending", "PAIR_REJECTED": "rejected"}


def note(code, detail, refs):
    return {"code": code, "message": REASONS[code], "detail": detail, "refs": tuple(refs)}


def identify_vehicles(passes, axes, rows, quarantine, ticket_source):
    book, _ = ticket_book(ticket_source, passes)
    axes_by_pass = defaultdict(list)
    for axis in axes:
        axes_by_pass[axis["pass_id"]].append(axis)
    issues_by_pass = defaultdict(list)
    for issue in quarantine:
        issues_by_pass[issue.pass_id].append(issue.source.ref)
    rows_by_pass = {row["pass_id"]: row for row in rows}

    records = {}
    for pass_id in sorted(passes):
        crossing = passes[pass_id]
        norm = normalize_plate(crossing.plate)
        confirmed = sorted(axes_by_pass[pass_id], key=lambda axis: axis["id"])
        fingerprint = fingerprint_of(confirmed)
        issue_refs = tuple(issues_by_pass[pass_id])
        records[pass_id] = {
            "crossing": crossing,
            "plate_raw": crossing.plate,
            "plate_norm": norm,
            "plate_ok": wellformed(norm),
            "axes": confirmed,
            "fingerprint": fingerprint,
            "measurable": fingerprint is not None and not issue_refs,
            "issue_refs": issue_refs,
            "tickets": sorted(book[pass_id], key=lambda ticket: (ticket.revision, ticket.id)),
        }

    parent = {pass_id: pass_id for pass_id in records}

    def find(pass_id):
        while parent[pass_id] != pass_id:
            parent[pass_id] = parent[parent[pass_id]]
            pass_id = parent[pass_id]
        return pass_id

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    edges = []
    by_norm = defaultdict(list)
    for pass_id, record in records.items():
        by_norm[record["plate_norm"]].append(pass_id)
    for norm in sorted(by_norm):
        members = sorted(by_norm[norm])
        for left, right in zip(members, members[1:]):
            union(left, right)
            refs = (records[left]["crossing"].source.ref, records[right]["crossing"].source.ref)
            edges.append((left, right, "MERGE_EXACT", f"规范化车牌一致：{norm}", refs))

    ids = sorted(records)
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            lrec, rrec = records[left], records[right]
            if lrec["plate_norm"] == rrec["plate_norm"]:
                continue
            if plate_distance(lrec["plate_norm"], rrec["plate_norm"]) > PLATE_CANDIDATE_DISTANCE:
                continue
            refs = (lrec["crossing"].source.ref, rrec["crossing"].source.ref)
            pair = f"{lrec['plate_raw']} ↔ {rrec['plate_raw']}"
            if lrec["measurable"] and rrec["measurable"]:
                if fingerprint_match(lrec["fingerprint"], rrec["fingerprint"]):
                    union(left, right)
                    detail = f"车牌相差 1 字（{pair}），轴组指纹一致：{describe(lrec['fingerprint'])}"
                    edges.append((left, right, "MERGE_FUZZY", detail, refs))
                else:
                    detail = f"车牌相差 1 字（{pair}）但轴组指纹不符：{describe(lrec['fingerprint'])} ↔ {describe(rrec['fingerprint'])}"
                    edges.append((left, right, "PAIR_REJECTED", detail, refs))
            else:
                lacking = "、".join(pass_id for pass_id in (left, right) if not records[pass_id]["measurable"])
                edges.append((left, right, "PAIR_PENDING", f"车牌相差 1 字（{pair}），车次 {lacking} 轴组证据不足", refs))

    components = defaultdict(list)
    for pass_id in ids:
        components[find(pass_id)].append(pass_id)

    edges_by_pass = defaultdict(list)
    for edge in edges:
        edges_by_pass[edge[0]].append(edge)
        edges_by_pass[edge[1]].append(edge)

    profiles = []
    for members in components.values():
        members.sort()
        anchor = min(records[pass_id]["plate_norm"] for pass_id in members)
        profiles.append(assemble(anchor, members, records, rows_by_pass, edges_by_pass))
    profiles.sort(key=lambda profile: profile["anchor"])

    pairs = [
        {"left": left, "right": right, "verdict": VERDICTS[code], "code": code,
         "message": REASONS[code], "detail": detail, "refs": refs}
        for left, right, code, detail, refs in edges
    ]
    summary = {
        "passes": len(ids),
        "profiles": len(profiles),
        "merged": sum(len(profile["members"]) > 1 for profile in profiles),
        "singletons": sum(len(profile["members"]) == 1 for profile in profiles),
        "pending": sum(pair["verdict"] == "pending" for pair in pairs),
        "rejected": sum(pair["verdict"] == "rejected" for pair in pairs),
        "review": sum(profile["status"] == "review" for profile in profiles),
    }
    return {"profiles": profiles, "pairs": pairs, "summary": summary}


def assemble(anchor, members, records, rows_by_pass, edges_by_pass):
    observed = defaultdict(int)
    plate_refs = defaultdict(list)
    for pass_id in members:
        record = records[pass_id]
        observed[record["plate_raw"]] += 1
        plate_refs[record["plate_raw"]].append(record["crossing"].source.ref)
        for ticket in record["tickets"]:
            observed[ticket.plate] += 1
            plate_refs[ticket.plate].append(ticket.source.ref)
    display = sorted(observed, key=lambda raw: (not wellformed(normalize_plate(raw)), -observed[raw], raw))[0]

    evidence, uncertainties = [], []
    seen_edges = set()
    for pass_id in members:
        for edge in sorted(edges_by_pass[pass_id]):
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            left, right, code, detail, refs = edge
            entry = note(code, detail, refs)
            (evidence if VERDICTS[code] == "merged" else uncertainties).append(entry)
    for pass_id in members:
        record = records[pass_id]
        if not record["plate_ok"]:
            uncertainties.append(note(
                "PLATE_MALFORMED", f"车次 {pass_id} 车牌 {record['plate_raw']} 不符合标准格式",
                (record["crossing"].source.ref,),
            ))
        if record["issue_refs"]:
            uncertainties.append(note(
                "EVIDENCE_INCOMPLETE", f"车次 {pass_id} 有 {len(record['issue_refs'])} 条轮载隔离，轴组指纹不可全信",
                record["issue_refs"],
            ))
        late = [ticket for ticket in record["tickets"] if is_late(ticket, record["crossing"])]
        if late:
            uncertainties.append(note(
                "LATE_TICKET_ATTACHED",
                f"晚到磅单 {'、'.join(ticket.id for ticket in late)} 未参与勾稽，已归入档案",
                tuple(ticket.source.ref for ticket in late),
            ))
    norms = {normalize_plate(raw) for raw in observed}
    if len(norms) > 1 and not any(entry["code"] == "MERGE_FUZZY" for entry in evidence):
        uncertainties.append(note(
            "PLATE_VARIANTS", "档案内车牌版本：" + "、".join(sorted(observed)),
            tuple(ref for raw in sorted(plate_refs) for ref in plate_refs[raw]),
        ))
    conflicts = fingerprint_conflicts(members, records)
    if conflicts:
        uncertainties.append(note("FINGERPRINT_CONFLICT", conflicts, tuple(records[m]["crossing"].source.ref for m in members)))

    history = []
    for pass_id in members:
        record, row = records[pass_id], rows_by_pass[pass_id]
        ticket = row["ticket"]
        history.append({
            "pass_id": pass_id,
            "entry": record["crossing"].start,
            "axles": len(record["axes"]),
            "groups": len({axis["group"] for axis in record["axes"]}),
            "wheel_kg": row["wheel_kg"],
            "ticket": ticket.id if ticket else None,
            "revision": ticket.revision if ticket else None,
            "ticket_kg": ticket.kg if ticket else None,
            "difference_kg": row["difference_kg"],
            "status": row["status"],
            "codes": row["codes"],
            "refs": (row["pass_ref"], *row["ticket_refs"], *row["wheel_refs"], *row["review_refs"]),
        })
    history.sort(key=lambda item: (item["entry"], item["pass_id"]))

    return {
        "vehicle_key": f"VH-{digest(anchor.encode('utf-8'))[:10]}",
        "anchor": anchor,
        "plate": display,
        "members": [{
            "pass_id": pass_id,
            "plate_raw": records[pass_id]["plate_raw"],
            "plate_norm": records[pass_id]["plate_norm"],
            "plate_ok": records[pass_id]["plate_ok"],
            "pass_ref": records[pass_id]["crossing"].source.ref,
        } for pass_id in members],
        "plates": [{"plate": raw, "refs": tuple(plate_refs[raw])} for raw in sorted(plate_refs)],
        "evidence": evidence,
        "uncertainties": uncertainties,
        "history": history,
        "status": "review" if any(item["status"] == "review" for item in history) else "matched",
        "totals": {
            "passes": len(members),
            "axles": sum(item["axles"] for item in history),
            "wheel_kg": sum((item["wheel_kg"] for item in history), Decimal("0.00")),
            "review": sum(item["status"] == "review" for item in history),
        },
    }


def is_late(ticket, crossing):
    return ticket.time < crossing.start or ticket.time - crossing.end >= timedelta(minutes=TICKET_GRACE_MIN)


def fingerprint_conflicts(members, records):
    measurable = [pass_id for pass_id in members if records[pass_id]["measurable"]]
    for index, left in enumerate(measurable):
        for right in measurable[index + 1:]:
            if not fingerprint_match(records[left]["fingerprint"], records[right]["fingerprint"]):
                pair = f"{describe(records[left]['fingerprint'])} ↔ {describe(records[right]['fingerprint'])}"
                return f"同牌车次 {left}、{right} 轴组指纹不一致（{pair}），疑似套牌或档案错误"
    return ""
